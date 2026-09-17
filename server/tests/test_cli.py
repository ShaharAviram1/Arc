"""The operator CLI: ``python -m arc.cli <command>`` (roadmap M11).

These are the commands a person runs on a host with no browser session yet, so
what matters is that they are honest (an invite link that actually works, a
count that is really the count) and that re-running one is safe — a deploy
script that is not idempotent is a deploy script nobody dares re-run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.cli import (
    EXIT_FAILED,
    _human_bytes,
    _table,
    build_parser,
    cmd_demo_list,
    cmd_invite,
    cmd_recs,
    cmd_status,
    cmd_warm_catalogue,
)
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Anime,
    Invite,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    MalWriteLog,
    RecRun,
    User,
    UserRole,
)
from arc.services.auth import DEFAULT_EXPIRY_HOURS, create_user, get_valid
from arc.services.catalog import CatalogMedia, MediaTitle, SearchPage, SourceUnavailable
from arc.services.catalog.names import REFRESH_ALL, SEASON_SWEEP
from arc.services.jobs.queue import find_active
from tests.recs_helpers import FakeModel
from tests.recs_helpers import anime as anime_row
from tests.recs_helpers import entry as list_row

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
    assert parser.parse_args(["recs", "--user-email", "a@b.test"]).command == "recs"


def test_demo_list_seeds_watching_with_no_progress_unless_told_otherwise() -> None:
    """The defaults are the old behaviour, so the M11 invocation still works."""
    parsed = build_parser().parse_args(["demo-list", "--user-email", "a@b.test", "--add", "X"])

    assert parsed.status == ListStatus.WATCHING.value
    assert parsed.progress is None
    assert parsed.demo is False


def test_demo_list_takes_one_status_group_at_a_time() -> None:
    parsed = build_parser().parse_args(
        [
            "demo-list",
            "--user-email",
            "a@b.test",
            "--add",
            "X",
            "--status",
            "completed",
            "--progress",
            "12",
            "--demo",
        ]
    )

    assert (parsed.status, parsed.progress, parsed.demo) == ("completed", 12, True)


def test_demo_list_refuses_a_status_arc_does_not_have() -> None:
    """The choices are ``ListStatus``, so the flag cannot name MAL's spelling."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["demo-list", "--user-email", "a@b.test", "--add", "X", "--status", "planning"]
        )


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


# --- demo-list --------------------------------------------------------------
#
# The catalogue is faked rather than reached: what these tests are about is the
# seeding rules (one status group, one progress, the demo flag, FR-A9's
# activation, and the MAL silence of an unlinked account), and none of that is
# a question about AniList's search endpoint.

BOCCHI = "Bocchi the Rock!"
BOCCHI_ANILIST = 130003


def media(title: str, anilist_id: int, *, full: bool, episodes: int = 12) -> CatalogMedia:
    return CatalogMedia(
        source="anilist",
        anilist_id=anilist_id,
        title=MediaTitle(romaji=title),
        format="TV",
        episodes=episodes,
        status="FINISHED",
        full=full,
    )


class FakeCatalog:
    """A catalogue that answers one title from a script.

    ``search`` answers with a *summary* record and ``by_anilist_id`` with the
    full one, which is the pair the real service returns and what
    ``ensure_anime`` walks through on the way to a list entry.
    """

    def __init__(self, titles: dict[str, int], *, unavailable: bool = False) -> None:
        self.titles = titles
        self.unavailable = unavailable
        self.searches: list[str] = []

    async def search(self, term: str, *, page: int = 1) -> SearchPage:
        self.searches.append(term)
        if self.unavailable:
            raise SourceUnavailable("anilist", "down")
        anilist_id = self.titles.get(term)
        results = [] if anilist_id is None else [media(term, anilist_id, full=False)]
        return SearchPage(results=results, page=page, has_next=False)

    async def by_anilist_id(self, anilist_id: int) -> CatalogMedia | None:
        for title, known in self.titles.items():
            if known == anilist_id:
                return media(title, anilist_id, full=True)
        return None

    async def by_mal_id(self, mal_id: int) -> CatalogMedia | None:
        return None

    def healthy(self, name: str) -> bool:
        return True


def use_catalog(monkeypatch: pytest.MonkeyPatch, catalog: FakeCatalog) -> None:
    """Hand ``catalog`` to whichever command asks ``catalog_for`` for one."""

    @asynccontextmanager
    async def factory(_settings: Settings) -> AsyncIterator[FakeCatalog]:
        yield catalog

    monkeypatch.setattr("arc.cli.catalog_for", factory)


def demo_args(**values: Any) -> Any:
    """``demo-list``'s namespace with the parser's own defaults filled in."""
    defaults: dict[str, Any] = {
        "status": ListStatus.WATCHING.value,
        "progress": None,
        "demo": False,
    }
    return args(**{**defaults, **values})


async def add_account(factory: SessionFactory, email: str) -> User:
    async with factory() as session:
        user = await create_user(session, email, "a-good-password")
        await session.commit()
        return user


async def test_demo_list_seeds_one_status_group_and_flags_the_account(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The M16 seeding command, in the shape the demo account is built with."""
    user = await add_account(api_factory, "prof@example.edu")
    use_catalog(monkeypatch, FakeCatalog({BOCCHI: BOCCHI_ANILIST}))

    async with api_factory() as session:
        code = await cmd_demo_list(
            session,
            settings,
            demo_args(
                user_email="Prof@Example.EDU",
                add=[BOCCHI],
                status=ListStatus.COMPLETED.value,
                progress=12,
                demo=True,
            ),
        )

    assert code == 0
    out = capsys.readouterr().out
    assert "flagged as the demo account" in out
    assert "completed · 12 / 12" in out

    async with api_factory() as session:
        row = await session.get(User, user.id)
        assert row is not None and row.is_demo is True
        entry = await session.scalar(select(ListEntry))
        assert entry is not None
        assert entry.status is ListStatus.COMPLETED
        assert entry.progress == 12
        # FR-A9: seeded by hand *is* somebody acting in Arc, so the entry is
        # active and acquisition will look at it. An imported one would not be.
        assert entry.activated_at is not None
        # FR-M7: the account has no MAL link, so nothing was queued for it.
        assert await session.scalar(select(func.count()).select_from(MalWriteLog)) == 0


async def test_demo_list_twice_leaves_one_entry_and_one_flag(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A seeding script has to be safe to re-run after a half-done deploy."""
    await add_account(api_factory, "prof@example.edu")
    use_catalog(monkeypatch, FakeCatalog({BOCCHI: BOCCHI_ANILIST}))
    command = demo_args(user_email="prof@example.edu", add=[BOCCHI], progress=4, demo=True)

    async with api_factory() as session:
        assert await cmd_demo_list(session, settings, command) == 0
    capsys.readouterr()
    async with api_factory() as session:
        assert await cmd_demo_list(session, settings, command) == 0

    assert "is already the demo account" in capsys.readouterr().out
    async with api_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ListEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(Anime)) == 1
        entry = await session.scalar(select(ListEntry))
        assert entry is not None and entry.progress == 4
        assert await session.scalar(select(func.count()).select_from(User).where(User.is_demo)) == 1


async def test_demo_list_leaves_the_flag_alone_without_the_flag(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Seeding a list and nominating the demo account are two decisions."""
    user = await add_account(api_factory, "owner@example.edu")
    use_catalog(monkeypatch, FakeCatalog({BOCCHI: BOCCHI_ANILIST}))

    async with api_factory() as session:
        assert (
            await cmd_demo_list(
                session, settings, demo_args(user_email="owner@example.edu", add=[BOCCHI])
            )
            == 0
        )

    capsys.readouterr()
    async with api_factory() as session:
        row = await session.get(User, user.id)
        assert row is not None and row.is_demo is False
        entry = await session.scalar(select(ListEntry))
        assert entry is not None and entry.status is ListStatus.WATCHING


async def test_demo_list_reports_the_rule_the_endpoint_would_have_reported(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--progress -1`` is refused by ``set_list_entry``, not by a copy of it."""
    await add_account(api_factory, "prof@example.edu")
    use_catalog(monkeypatch, FakeCatalog({BOCCHI: BOCCHI_ANILIST}))

    async with api_factory() as session:
        code = await cmd_demo_list(
            session,
            settings,
            demo_args(user_email="prof@example.edu", add=[BOCCHI], progress=-1),
        )

    assert code == EXIT_FAILED
    assert "progress must not be negative" in capsys.readouterr().err
    async with api_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ListEntry)) == 0


async def test_demo_list_says_so_when_progress_runs_past_the_episode_count(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Accepted, exactly as the endpoint accepts it — and said out loud."""
    await add_account(api_factory, "prof@example.edu")
    use_catalog(monkeypatch, FakeCatalog({BOCCHI: BOCCHI_ANILIST}))

    async with api_factory() as session:
        code = await cmd_demo_list(
            session,
            settings,
            demo_args(user_email="prof@example.edu", add=[BOCCHI], progress=14),
        )

    assert code == 0
    captured = capsys.readouterr()
    assert "14 / 12" in captured.out
    assert "past the catalogue's 12 episodes" in captured.err


async def test_demo_list_reports_an_unknown_account_rather_than_seeding_one(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = FakeCatalog({BOCCHI: BOCCHI_ANILIST})
    use_catalog(monkeypatch, catalog)

    async with api_factory() as session:
        code = await cmd_demo_list(
            session, settings, demo_args(user_email="nobody@example.edu", add=[BOCCHI])
        )

    assert code == EXIT_FAILED
    assert "no user with the address nobody@example.edu" in capsys.readouterr().err
    assert catalog.searches == [], "the catalogue was never asked"


async def test_demo_list_reports_a_title_the_catalogue_does_not_have(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One bad title fails the command; the good ones are still seeded."""
    await add_account(api_factory, "prof@example.edu")
    use_catalog(monkeypatch, FakeCatalog({BOCCHI: BOCCHI_ANILIST}))

    async with api_factory() as session:
        code = await cmd_demo_list(
            session,
            settings,
            demo_args(user_email="prof@example.edu", add=["Not A Show", BOCCHI]),
        )

    assert code == EXIT_FAILED
    assert "no match in the catalogue" in capsys.readouterr().err
    async with api_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ListEntry)) == 1


# --- recs -------------------------------------------------------------------


def use_model(monkeypatch: pytest.MonkeyPatch, model: FakeModel | None) -> None:
    """Hand ``model`` — or nothing at all — to ``cmd_recs``."""

    @asynccontextmanager
    async def factory(_settings: Settings) -> AsyncIterator[FakeModel | None]:
        yield model

    monkeypatch.setattr("arc.cli.model_for", factory)


async def seed_a_pool(factory: SessionFactory, user: User) -> int:
    """One completed show on the list and one unlisted show that shares genres.

    The genre half of the pool (FR-R2) rather than the seasonal half, because
    "this season" moves with the calendar and a test that seeds it would start
    failing on its own three weeks from now. Two genres because
    ``GENRE_OVERLAP`` is two — one shared genre is a coincidence, not a taste.
    """
    genres = ["Action", "Adventure"]
    async with factory() as session:
        listed = anime_row(0, "A Finished Favourite", anilist_id=1, genres=genres)
        candidate = anime_row(0, "An Unlisted Neighbour", anilist_id=2, genres=genres)
        listed.id = None  # type: ignore[assignment]
        candidate.id = None  # type: ignore[assignment]
        session.add_all([listed, candidate])
        await session.flush()
        session.add(list_row(listed.id, ListStatus.COMPLETED, user_id=user.id, score=9))
        await session.commit()
        return candidate.id


async def test_recs_produces_the_same_run_the_page_button_does(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """So the demo account's Recommendations page has content on first sight."""
    user = await add_account(api_factory, "prof@example.edu")
    candidate_id = await seed_a_pool(api_factory, user)
    use_catalog(monkeypatch, FakeCatalog({}))
    model = FakeModel([(candidate_id, "An Unlisted Neighbour", "because you finished the other")])
    use_model(monkeypatch, model)

    async with api_factory() as session:
        code = await cmd_recs(
            session, settings, args(user_email="prof@example.edu", prompt="something short")
        )

    assert code == 0
    out = capsys.readouterr().out
    assert "An Unlisted Neighbour" in out
    assert "runs left for this user today" in out
    assert model.calls == 1

    async with api_factory() as session:
        run = await session.scalar(select(RecRun))
        assert run is not None
        assert run.user_id == user.id
        assert run.prompt == "something short"
        assert [pick["anime_id"] for pick in run.picks or []] == [candidate_id]


async def test_recs_says_so_when_no_provider_is_configured(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The router's 503; here it is one line and an exit code."""
    await add_account(api_factory, "prof@example.edu")
    use_catalog(monkeypatch, FakeCatalog({}))
    use_model(monkeypatch, None)

    async with api_factory() as session:
        code = await cmd_recs(session, settings, args(user_email="prof@example.edu", prompt=None))

    assert code == EXIT_FAILED
    assert "no recommendation provider is configured" in capsys.readouterr().err


async def test_recs_says_so_when_there_is_nothing_to_recommend_from(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty catalogue is the fresh-deployment order-of-operations mistake."""
    await add_account(api_factory, "prof@example.edu")
    use_catalog(monkeypatch, FakeCatalog({}))
    model = FakeModel([])
    use_model(monkeypatch, model)

    async with api_factory() as session:
        code = await cmd_recs(session, settings, args(user_email="prof@example.edu", prompt=None))

    assert code == EXIT_FAILED
    assert "nothing to recommend from" in capsys.readouterr().err
    assert model.calls == 0
    async with api_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RecRun)) == 0


async def test_recs_reports_an_unknown_account(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_catalog(monkeypatch, FakeCatalog({}))
    model = FakeModel([])
    use_model(monkeypatch, model)

    async with api_factory() as session:
        code = await cmd_recs(session, settings, args(user_email="nobody@example.edu", prompt=None))

    assert code == EXIT_FAILED
    assert "no user with the address" in capsys.readouterr().err
    assert model.calls == 0


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
