"""The operator CLI: ``python -m arc.cli <command>`` (roadmap M11).

These are the commands a person runs on a host with no browser session yet, so
what matters is that they are honest (an invite link that actually works, a
count that is really the count) and that re-running one is safe — a deploy
script that is not idempotent is a deploy script nobody dares re-run.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.cli import _human_bytes, _table, build_parser, cmd_invite, cmd_status, cmd_warm_catalogue
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Anime, Invite, Job, JobStatus, ListEntry, User, UserRole
from arc.services.auth import DEFAULT_EXPIRY_HOURS, create_user, get_valid
from arc.services.catalog.names import REFRESH_ALL, SEASON_SWEEP
from arc.services.jobs.queue import find_active

pytestmark = pytest.mark.pg


def args(**values: Any) -> Any:
    """A stand-in for the ``argparse.Namespace`` a handler receives.

    ``expires_in_hours`` is defaulted here rather than at every call site: it is
    the parser's default, and a test about ``--admin`` should not have to
    restate it.
    """
    return type("Args", (), {"expires_in_hours": DEFAULT_EXPIRY_HOURS, **values})()


# --- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1536, "1.5 KB"),
        (2_000_000, "2.0 MB"),
        (3_500_000_000, "3.5 GB"),
    ],
)
def test_human_bytes(count: int, expected: str) -> None:
    assert _human_bytes(count) == expected


def test_the_status_table_shows_zeros_and_keeps_its_order() -> None:
    """A state that vanishes at zero is a line that moves between two runs."""
    assert _table({"ready": 3}, ["wanted", "ready", "failed"]) == "wanted 0, ready 3, failed 0"


def test_the_status_table_still_shows_a_state_it_was_not_told_about() -> None:
    """A value the enum grew after this code was written must not disappear."""
    assert _table({"ready": 1, "surprise": 2}, ["ready"]) == "ready 1, surprise 2"


# --- the parser -------------------------------------------------------------


def test_every_command_is_reachable() -> None:
    parser = build_parser()

    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["warm-catalogue"]).command == "warm-catalogue"
    assert parser.parse_args(["invite", "--email", "a@b.test"]).email == "a@b.test"
    assert parser.parse_args(["demo-list", "--user-email", "a@b.test", "--add", "X"]).add == ["X"]


def test_several_shows_can_be_added_at_once() -> None:
    parsed = build_parser().parse_args(
        ["demo-list", "--user-email", "a@b.test", "--add", "One", "--add", "Two"]
    )

    assert parsed.add == ["One", "Two"]


def test_the_invite_default_is_seven_days() -> None:
    """architecture.md §7. The API's default and this one must not drift."""
    parsed = build_parser().parse_args(["invite", "--email", "a@b.test"])

    assert parsed.expires_in_hours == DEFAULT_EXPIRY_HOURS == 168


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --- invite -----------------------------------------------------------------


async def test_invite_prints_a_link_that_actually_resolves(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The token in the URL has to be the one the accept route will accept.

    Arc stores only ``sha256(token)``, so a link printed with the wrong half of
    that pair fails silently and looks exactly like an expired invite.
    """
    async with api_factory() as session:
        code = await cmd_invite(session, settings, args(email="Prof@Example.EDU", admin=False))

    assert code == 0
    url = capsys.readouterr().out.strip()
    assert url.startswith(f"{settings.public_url}/invite/")

    token = url.rsplit("/", 1)[-1]
    async with api_factory() as session:
        invite = await get_valid(session, token)

    assert invite is not None
    # Normalised, so the invitee is matched against what is actually stored.
    assert invite.email == "prof@example.edu"


async def test_the_invite_token_is_never_written_down(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    async with api_factory() as session:
        await cmd_invite(session, settings, args(email="a@b.test", admin=False))

    token = capsys.readouterr().out.strip().rsplit("/", 1)[-1]
    async with api_factory() as session:
        stored = await session.scalar(select(Invite.token_hash))

    assert stored is not None
    assert token not in stored


async def test_invite_admin_promotes_an_existing_account(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invites carry no role, so this is what ``--admin`` can honestly do."""
    async with api_factory() as session:
        await create_user(session, "prof@example.edu", "a-good-password", role=UserRole.USER)
        await session.commit()

    async with api_factory() as session:
        code = await cmd_invite(session, settings, args(email="prof@example.edu", admin=True))

    assert code == 0
    assert "promoted to admin" in capsys.readouterr().out

    async with api_factory() as session:
        user = await session.scalar(select(User).where(User.email == "prof@example.edu"))
    assert user is not None and user.role is UserRole.ADMIN

    # …and doing it twice is a no-op, not an error.
    async with api_factory() as session:
        assert await cmd_invite(session, settings, args(email="prof@example.edu", admin=True)) == 0
    assert "already an admin" in capsys.readouterr().out


async def test_invite_admin_without_an_account_still_issues_the_invite(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Because the account has to exist before it can be promoted, and the
    operator has to be told that in the same breath."""
    async with api_factory() as session:
        code = await cmd_invite(session, settings, args(email="new@example.edu", admin=True))

    assert code == 0
    captured = capsys.readouterr()
    assert "/invite/" in captured.out
    assert "re-run this command with --admin" in captured.err


# --- warm-catalogue ---------------------------------------------------------


async def test_warm_catalogue_queues_the_two_sweeps(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    async with api_factory() as session:
        assert await cmd_warm_catalogue(session, settings, args()) == 0

    async with api_factory() as session:
        assert await find_active(session, SEASON_SWEEP, SEASON_SWEEP) is not None
        assert await find_active(session, REFRESH_ALL, REFRESH_ALL) is not None

    out = capsys.readouterr().out
    assert "season sweep" in out
    assert "refresh sweep" in out


async def test_warm_catalogue_is_idempotent(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run it twice on a slow deploy and get two jobs, not four.

    The dedupe key is the job type, and it only matches pending or running
    rows — so this does not stop tomorrow's scheduled sweep either.
    """
    async with api_factory() as session:
        await cmd_warm_catalogue(session, settings, args())
    async with api_factory() as session:
        await cmd_warm_catalogue(session, settings, args())

    async with api_factory() as session:
        pending = await session.scalar(
            select(func.count()).select_from(Job).where(Job.status == JobStatus.PENDING)
        )

    assert pending == 2
    capsys.readouterr()


# --- status -----------------------------------------------------------------


async def test_status_reports_what_is_there(
    api_factory: SessionFactory,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read-only, and safe to run against a live deployment."""
    async with api_factory() as session:
        await create_user(session, "one@example.test", "a-good-password", role=UserRole.ADMIN)
        await create_user(session, "two@example.test", "a-good-password", role=UserRole.USER)
        await session.commit()

    async with api_factory() as session:
        assert await cmd_status(session, settings, args()) == 0

    out = capsys.readouterr().out
    assert "users           2 total, 2 active, 1 admin" in out
    assert "acquisition     running" in out
    # Every grouped line names all of its states, zeros included.
    assert "not_wanted 0" in out
    assert "pending 0" in out
    # And the catalogue's own health, which is what says whether a fresh
    # deployment can look anything up at all.
    assert "active source:" in out
    assert "anilist" in out


async def test_status_writes_nothing(
    api_factory: SessionFactory, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    async with api_factory() as session:
        before = await _row_counts(session)
        await cmd_status(session, settings, args())
        after = await _row_counts(session)

    assert before == after
    capsys.readouterr()


async def _row_counts(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in (User, Anime, Job, ListEntry):
        total = await session.scalar(select(func.count()).select_from(model))
        counts[model.__name__] = int(total or 0)
    return counts
