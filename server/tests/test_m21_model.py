"""M21 T1: the new tables, the two new rules and the ``awaiting_pick`` state.

Nothing acts on any of it yet (T2–T8 do); what is held here is the shape the
later tasks build on: one open rewatch pass and one pending pack pick per
pair, enforced by the database; a walk per (user, show); the write path's
bounds on ``demand_max_open`` and ``batch_sweep_dry_run`` and their lenient
readers; and ``awaiting_pick`` counted as a decided state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    DEFAULT_SETTINGS,
    PACK_PICK_DISMISSED,
    PACK_PICK_PICKED,
    REWATCH_END_FINISHED,
    REWATCH_END_OFF,
    Episode,
    PackPick,
    Rewatch,
    Setting,
    Torrent,
    TorrentKind,
    Walk,
    WatchProgress,
)
from arc.models.user import User
from arc.services.acquisition.batch import UNATTACHABLE_STATES
from arc.services.acquisition.manual import TRIED
from arc.services.acquisition.qbit import (
    DECIDED_STATES,
    DELETE_ON_SIGHT,
    QBIT_AWAITING_PICK,
    QBIT_CANCELLED,
)
from arc.services.acquisition.rules import MAX_DEMAND_MAX_OPEN, demand_max_open
from arc.services.retention.rules import batch_sweep_dry_run
from arc.services.settings import SettingsInvalid, validate
from tests.acquisition_helpers import make_anime, make_episodes, make_user, set_setting

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


# --- The two rules: defaults and the write path -------------------------------


def test_the_defaults() -> None:
    assert DEFAULT_SETTINGS["demand_max_open"] == 3
    assert DEFAULT_SETTINGS["batch_sweep_dry_run"] is True
    assert MAX_DEMAND_MAX_OPEN == 20


def _refused(key: str, value: Any) -> str:
    with pytest.raises(SettingsInvalid) as caught:
        validate({key: value})
    return caught.value.errors[key]


@pytest.mark.parametrize("value", [0, 1, 3, 20])
def test_the_demand_cap_takes_0_to_20(value: int) -> None:
    """0 is legal: no cap, like ``slot_cap_k``."""
    assert validate({"demand_max_open": value}) == {"demand_max_open": value}


@pytest.mark.parametrize("value", [-1, 21, True, "3", 3.5, None, [3]])
def test_a_bad_demand_cap_is_refused(value: Any) -> None:
    assert _refused("demand_max_open", value)


@pytest.mark.parametrize("value", [True, False])
def test_the_dry_run_takes_a_boolean(value: bool) -> None:
    assert validate({"batch_sweep_dry_run": value}) == {"batch_sweep_dry_run": value}


@pytest.mark.parametrize("value", [1, 0, "true", "false", None, [True]])
def test_a_non_boolean_dry_run_is_refused(value: Any) -> None:
    assert _refused("batch_sweep_dry_run", value)


# --- The readers --------------------------------------------------------------


@pytest.mark.pg
async def test_demand_max_open_reads_the_row_and_is_clamped(db_session: AsyncSession) -> None:
    assert await demand_max_open(db_session) == 3
    await set_setting(db_session, "demand_max_open", 0)
    assert await demand_max_open(db_session) == 0
    await set_setting(db_session, "demand_max_open", 7)
    assert await demand_max_open(db_session) == 7
    await set_setting(db_session, "demand_max_open", 500)
    assert await demand_max_open(db_session) == MAX_DEMAND_MAX_OPEN


@pytest.mark.pg
async def test_a_missing_demand_cap_reads_as_the_default(db_session: AsyncSession) -> None:
    await db_session.execute(delete(Setting).where(Setting.key == "demand_max_open"))
    assert await demand_max_open(db_session) == 3


@pytest.mark.pg
@pytest.mark.parametrize("stored", ["3", True, -1, 3.5, [3]])
async def test_a_mangled_demand_cap_reads_as_the_default_and_says_so(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture, stored: Any
) -> None:
    await set_setting(db_session, "demand_max_open", stored)
    with caplog.at_level(logging.WARNING, logger="arc.services.acquisition.rules"):
        assert await demand_max_open(db_session) == 3
    assert any("using the default" in record.getMessage() for record in caplog.records)


@pytest.mark.pg
async def test_batch_sweep_dry_run_reads_the_row(db_session: AsyncSession) -> None:
    assert await batch_sweep_dry_run(db_session) is True
    await set_setting(db_session, "batch_sweep_dry_run", False)
    assert await batch_sweep_dry_run(db_session) is False


@pytest.mark.pg
async def test_a_missing_dry_run_row_reads_as_dry(db_session: AsyncSession) -> None:
    await db_session.execute(delete(Setting).where(Setting.key == "batch_sweep_dry_run"))
    assert await batch_sweep_dry_run(db_session) is True


@pytest.mark.pg
@pytest.mark.parametrize("stored", [0, "false", None, [False], {"dry": False}])
async def test_a_mangled_dry_run_reads_as_dry_and_says_so(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture, stored: Any
) -> None:
    """A mangled row can never be what turns the deletions on."""
    await set_setting(db_session, "batch_sweep_dry_run", stored)
    with caplog.at_level(logging.WARNING, logger="arc.services.retention.rules"):
        assert await batch_sweep_dry_run(db_session) is True
    if stored is not None:
        assert any("using the default" in record.getMessage() for record in caplog.records)


# --- awaiting_pick ------------------------------------------------------------


def test_awaiting_pick_is_a_decided_state() -> None:
    """Held for a person's pick: a poll must not overwrite it, nor attach to it."""
    assert QBIT_AWAITING_PICK == "awaiting_pick"
    assert QBIT_AWAITING_PICK in DECIDED_STATES
    assert QBIT_AWAITING_PICK in UNATTACHABLE_STATES
    assert QBIT_AWAITING_PICK in TRIED
    # Held, never chased for deletion: that would take the pack's files with it.
    assert QBIT_AWAITING_PICK not in DELETE_ON_SIGHT
    assert QBIT_AWAITING_PICK != QBIT_CANCELLED


def test_the_reason_constants() -> None:
    assert (REWATCH_END_OFF, REWATCH_END_FINISHED) == ("off", "finished")
    assert (PACK_PICK_PICKED, PACK_PICK_DISMISSED) == ("picked", "dismissed")


# --- The tables ---------------------------------------------------------------


async def _user_and_show(session: AsyncSession) -> tuple[User, int, list[Episode]]:
    user = await make_user(session, "m21-model@arc.test")
    anime = await make_anime(session, anilist_id=982101)
    episodes = await make_episodes(session, anime, 3)
    return user, anime.id, episodes


async def _refused_by_the_database(session: AsyncSession, row: object) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(row)
            await session.flush()


@pytest.mark.pg
async def test_one_open_rewatch_pass_per_user_and_show(db_session: AsyncSession) -> None:
    user, anime_id, _ = await _user_and_show(db_session)
    first = Rewatch(user_id=user.id, anime_id=anime_id, started_at=NOW)
    db_session.add(first)
    await db_session.flush()

    await _refused_by_the_database(
        db_session,
        Rewatch(user_id=user.id, anime_id=anime_id, started_at=NOW + timedelta(hours=1)),
    )

    # Once the first pass has ended, a new one opens; the ended row is kept.
    first.ended_at = NOW + timedelta(hours=2)
    first.end_reason = REWATCH_END_OFF
    await db_session.flush()
    db_session.add(Rewatch(user_id=user.id, anime_id=anime_id, started_at=NOW + timedelta(hours=3)))
    await db_session.flush()

    passes = (await db_session.scalars(select(Rewatch).where(Rewatch.user_id == user.id))).all()
    assert len(passes) == 2
    assert sum(row.ended_at is None for row in passes) == 1


@pytest.mark.pg
async def test_a_walk_is_one_row_per_user_and_show(db_session: AsyncSession) -> None:
    user, anime_id, _ = await _user_and_show(db_session)
    db_session.add(Walk(user_id=user.id, anime_id=anime_id, after_number=2, touched_at=NOW))
    await db_session.flush()
    db_session.expunge_all()

    await _refused_by_the_database(
        db_session, Walk(user_id=user.id, anime_id=anime_id, after_number=3, touched_at=NOW)
    )

    walk = await db_session.get(Walk, (user.id, anime_id))
    assert walk is not None and walk.after_number == 2


async def _pack(session: AsyncSession) -> Torrent:
    torrent = Torrent(
        info_hash="m21" + "0" * 37,
        kind=TorrentKind.BATCH,
        qbit_state=QBIT_AWAITING_PICK,
    )
    session.add(torrent)
    await session.flush()
    return torrent


def _pick(torrent: Torrent, episode: Episode) -> PackPick:
    return PackPick(
        torrent_id=torrent.id,
        episode_id=episode.id,
        reason="two files read as episode 2",
        candidates=[
            {"file_index": 0, "path": "Show - 02.mkv", "size": 1, "parsed_number": 2},
            {"file_index": 1, "path": "Show - 02v2.mkv", "size": 1, "parsed_number": None},
        ],
    )


@pytest.mark.pg
async def test_one_pending_pick_per_pack_and_episode(db_session: AsyncSession) -> None:
    user, _, episodes = await _user_and_show(db_session)
    torrent = await _pack(db_session)
    first = _pick(torrent, episodes[1])
    db_session.add(first)
    await db_session.flush()
    assert first.created_at is not None

    await _refused_by_the_database(db_session, _pick(torrent, episodes[1]))

    # Another episode of the same pack is a pick of its own.
    db_session.add(_pick(torrent, episodes[2]))
    await db_session.flush()

    # Once resolved, the pair may wait for a pick again; the old row stays.
    first.resolved_at = NOW
    first.resolution = PACK_PICK_PICKED
    first.picked_file_index = 0
    first.resolved_by = user.id
    await db_session.flush()
    db_session.add(_pick(torrent, episodes[1]))
    await db_session.flush()

    rows = (
        await db_session.scalars(select(PackPick).where(PackPick.torrent_id == torrent.id))
    ).all()
    assert len(rows) == 3


@pytest.mark.pg
async def test_a_deleted_reviewer_leaves_the_pick(db_session: AsyncSession) -> None:
    user, _, episodes = await _user_and_show(db_session)
    torrent = await _pack(db_session)
    pick = _pick(torrent, episodes[0])
    pick.resolved_at = NOW
    pick.resolution = PACK_PICK_DISMISSED
    pick.resolved_by = user.id
    db_session.add(pick)
    await db_session.flush()

    await db_session.delete(user)
    await db_session.flush()
    await db_session.refresh(pick)

    assert pick.resolved_by is None


@pytest.mark.pg
async def test_finished_at_is_a_nullable_column_of_its_own(db_session: AsyncSession) -> None:
    user, _, episodes = await _user_and_show(db_session)
    row = WatchProgress(user_id=user.id, episode_id=episodes[0].id, completed=True)
    row.completed_at = NOW
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.finished_at is None

    row.finished_at = NOW + timedelta(days=1)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.completed_at == NOW
    assert row.finished_at == NOW + timedelta(days=1)
