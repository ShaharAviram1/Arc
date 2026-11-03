"""M21 T2: the latest finish, walks and rewatch mode (FR-S4, FR-S8, FR-W3, FR-W5, FR-W7).

Most of these are about what a rewatch pass must **not** do: write to the list
entry or to MyAnimeList, create or activate an entry, auto-complete a show, or
reopen a pass a late offline replay belongs to. The MAL tests link the user so
that "no write" is not vacuous: an unlinked user writes nothing anyway.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.db import SessionFactory
from arc.models import (
    REWATCH_END_FINISHED,
    REWATCH_END_OFF,
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    MalLink,
    MalWriteLog,
    Rewatch,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    Walk,
    WatchProgress,
)
from arc.services.acquisition.names import COMPUTE_WANTS
from arc.services.mal.names import PUSH
from arc.services.media.names import LiveDuration
from arc.services.playback import rewatch as rewatch_service
from arc.services.playback.progress import ProgressOutcome, record_progress, unmark_watched
from arc.services.playback.rewatch import (
    TAIL_GRACE,
    NothingAired,
    in_pass,
    open_rewatch,
    open_rewatches,
    pass_end_number,
    pass_finished,
    pass_position,
    rewatch_at,
    start,
    stop,
    tail_applies,
    watch_view,
)
from arc.services.playback.sync import SyncItem, SyncKind, SyncStatus, apply_batch, apply_item
from arc.services.trips.names import TRIP_SWEEP
from arc.services.trips.rules import held_episode_ids
from arc.services.trips.sweep import end_if_complete
from tests.acquisition_helpers import make_anime, make_entry, make_episodes, make_user
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

DURATION = 1420.0
#: Real time, because :func:`tests.acquisition_helpers.make_episodes` dates
#: its episodes from the real clock and a pass's end reads "aired" from them.
NOW = datetime.now(UTC).replace(microsecond=0)


def minutes(n: float) -> datetime:
    return NOW + timedelta(minutes=n)


# --- helpers ------------------------------------------------------------------


async def show(
    session: AsyncSession,
    *,
    anilist_id: int,
    count: int = 12,
    status: str = "FINISHED",
    aired: int | None = None,
    episodes: int | None = -1,
) -> tuple[Anime, dict[int, Episode]]:
    anime = await make_anime(
        session,
        anilist_id=anilist_id,
        romaji=f"Rewatch {anilist_id}",
        english=None,
        status=status,
        episodes=count if episodes == -1 else episodes,
    )
    rows = await make_episodes(session, anime, count, aired_through=aired)
    return anime, {row.number: row for row in rows}


async def link(session: AsyncSession, user: User) -> None:
    """A MAL link row: enough for ``is_linked``, so writes would be logged."""
    session.add(MalLink(user_id=user.id, access_token_enc="x", refresh_token_enc="y"))
    await session.flush()


async def report(
    session: AsyncSession,
    user: User,
    episode: Episode,
    fraction: float,
    *,
    at: datetime,
    force: bool = False,
    reported_at: datetime | None = None,
    duration: float = DURATION,
) -> ProgressOutcome:
    return await record_progress(
        session,
        user_id=user.id,
        episode=episode,
        position_s=duration * fraction,
        duration_s=duration,
        now=at,
        force_complete=force,
        reported_at=reported_at,
    )


async def mal_rows(session: AsyncSession, user: User) -> list[MalWriteLog]:
    return list(
        (await session.scalars(select(MalWriteLog).where(MalWriteLog.user_id == user.id))).all()
    )


async def jobs(session: AsyncSession, kind: str) -> list[Job]:
    return list((await session.scalars(select(Job).where(Job.type == kind))).all())


async def clear_jobs(session: AsyncSession) -> None:
    await session.execute(delete(Job))
    await session.flush()


async def row_of(session: AsyncSession, user: User, episode: Episode) -> WatchProgress | None:
    return await session.get(WatchProgress, (user.id, episode.id), populate_existing=True)


async def entry_snapshot(
    session: AsyncSession, user: User, anime: Anime
) -> tuple[object, ...] | None:
    entry = await session.get(ListEntry, (user.id, anime.id), populate_existing=True)
    if entry is None:
        return None
    return (
        entry.status,
        entry.progress,
        entry.activated_at,
        entry.mal_dirty,
        entry.updated_at,
        entry.updated_by,
    )


async def pass_of(session: AsyncSession, rewatch: Rewatch) -> Rewatch:
    found = await session.get(Rewatch, rewatch.id, populate_existing=True)
    assert found is not None
    return found


# --- pure rules ---------------------------------------------------------------


def test_the_pass_ends_on_the_count_of_a_finished_show_else_the_newest_aired() -> None:
    assert pass_end_number(anime_status="FINISHED", episode_count=12, newest_aired=12) == 12
    assert pass_end_number(anime_status="FINISHED", episode_count=None, newest_aired=11) == 11
    assert pass_end_number(anime_status="FINISHED", episode_count=0, newest_aired=11) == 11
    assert pass_end_number(anime_status="RELEASING", episode_count=24, newest_aired=6) == 6
    assert pass_end_number(anime_status=None, episode_count=24, newest_aired=3) == 3


# --- a whole pass: nothing reaches the list or MyAnimeList -----------------------


async def test_a_full_pass_writes_nothing_to_the_list_or_to_mal(db_session: AsyncSession) -> None:
    """FR-W7 item 3 through every episode, the last included, with the list at 5/12."""
    user = await make_user(db_session, "rw-full@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990001)
    await make_entry(db_session, user, anime, progress=5, activated=False)
    before = await entry_snapshot(db_session, user, anime)

    rewatch = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    ended_on: list[int] = []
    for number in range(1, 13):
        base = number * 30
        playing = await report(db_session, user, eps[number], 0.2, at=minutes(base))
        assert playing.rewatching and not playing.completed
        finished = await report(db_session, user, eps[number], 0.95, at=minutes(base + 20))
        assert finished.rewatching
        assert finished.completed and finished.newly_completed, number
        assert finished.list_progress is None
        if finished.rewatch_ended:
            ended_on.append(number)
        # The credits, after the pass may have ended on this very episode.
        credits = await report(db_session, user, eps[number], 0.98, at=minutes(base + 21))
        assert credits.rewatching, number
        assert not credits.newly_completed and not credits.rewatch_ended
        assert credits.list_progress is None

    assert ended_on == [12]
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []
    assert await jobs(db_session, PUSH) == []
    ended = await pass_of(db_session, rewatch)
    assert ended.end_reason == REWATCH_END_FINISHED
    assert ended.ended_at == minutes(12 * 30 + 20)
    # No ``completed`` flag either: a pass completion is a latest finish only.
    for number in range(1, 13):
        row = await row_of(db_session, user, eps[number])
        assert row is not None
        assert row.completed is False and row.completed_at is None
        assert row.finished_at is not None and row.finished_at >= rewatch.started_at
    # And a pass never reopens by itself.
    assert await open_rewatch(db_session, user_id=user.id, anime_id=anime.id) is None


async def test_no_auto_complete_in_a_pass(db_session: AsyncSession) -> None:
    """FR-W5's auto-complete is never reached from a pass, even at 11/12 → 12."""
    user = await make_user(db_session, "rw-auto@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990002)
    await make_entry(db_session, user, anime, progress=11)
    before = await entry_snapshot(db_session, user, anime)
    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))

    outcome = await report(db_session, user, eps[12], 0.95, at=minutes(5))

    assert outcome.rewatch_ended and outcome.newly_completed
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []


async def test_a_pass_on_an_unlisted_show_creates_no_entry(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-unlisted@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990003)
    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))

    await report(db_session, user, eps[1], 0.95, at=minutes(1))
    await report(db_session, user, eps[2], 0.0, at=minutes(2), force=True)

    assert await entry_snapshot(db_session, user, anime) is None
    assert await mal_rows(db_session, user) == []


async def test_manual_mark_and_unmark_in_a_pass_move_only_the_pass(
    db_session: AsyncSession,
) -> None:
    """FR-W3 in a pass: ``finished_at`` and ``unmarked_at`` move, nothing else."""
    user = await make_user(db_session, "rw-mark@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990004)
    await make_entry(db_session, user, anime, progress=3)
    # Episode 3 was watched normally before the pass.
    await report(db_session, user, eps[3], 0.95, at=minutes(-60))
    await db_session.execute(delete(MalWriteLog))
    before = await entry_snapshot(db_session, user, anime)
    old = await row_of(db_session, user, eps[3])
    assert old is not None and old.completed and old.finished_at == minutes(-60)

    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))

    # An un-mark of a finish from before the pass: the pass does not own it.
    undone = await unmark_watched(db_session, user_id=user.id, episode=eps[3], now=minutes(1))
    assert undone.rewatching and not undone.cleared and undone.list_progress is None
    row = await row_of(db_session, user, eps[3])
    assert row is not None
    assert row.completed is True and row.completed_at == minutes(-60)
    assert row.finished_at == minutes(-60)
    assert row.unmarked_at == minutes(1)

    marked = await report(db_session, user, eps[3], 0.0, at=minutes(2), force=True)
    assert marked.rewatching and marked.newly_completed and marked.completed
    row = await row_of(db_session, user, eps[3])
    assert row is not None and row.finished_at == minutes(2)
    assert row.completed_at == minutes(-60)

    undone = await unmark_watched(db_session, user_id=user.id, episode=eps[3], now=minutes(3))
    assert undone.cleared and undone.rewatching
    row = await row_of(db_session, user, eps[3])
    assert row is not None
    assert row.finished_at is None
    assert row.unmarked_at == minutes(3)
    assert row.completed is True and row.completed_at == minutes(-60)

    # An un-mark of an episode the pass never touched, below the list: no row,
    # no list change (normal mode would lower the list at 3).
    await unmark_watched(db_session, user_id=user.id, episode=eps[2], now=minutes(4))
    assert await row_of(db_session, user, eps[2]) is None
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []


async def test_the_normal_unmark_clears_the_latest_finish(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-normal-unmark@arc.test")
    anime, eps = await show(db_session, anilist_id=990005)
    await report(db_session, user, eps[1], 0.95, at=minutes(0))

    await unmark_watched(db_session, user_id=user.id, episode=eps[1], now=minutes(1))

    row = await row_of(db_session, user, eps[1])
    assert row is not None
    assert row.completed is False and row.completed_at is None and row.finished_at is None


# --- offline replay -------------------------------------------------------------


async def test_a_replay_recorded_inside_a_pass_that_has_ended_is_mal_free(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "rw-replay@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990006)
    await make_entry(db_session, user, anime, progress=2)
    before = await entry_snapshot(db_session, user, anime)
    rewatch = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    assert await stop(db_session, user_id=user.id, anime_id=anime.id, now=minutes(60))

    # Recorded at +10 and +20, inside the pass; arriving at +90, after it.
    position = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("p", SyncKind.POSITION, eps[3].id, minutes(10), 1300.0, DURATION),
        now=minutes(90),
    )
    completion = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("c", SyncKind.COMPLETION, eps[4].id, minutes(20), 1350.0, DURATION),
        now=minutes(90),
    )

    assert position.status is SyncStatus.APPLIED
    assert completion.status is SyncStatus.APPLIED
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []
    for number, at in ((3, minutes(10)), (4, minutes(20))):
        row = await row_of(db_session, user, eps[number])
        assert row is not None and row.completed is False and row.finished_at == at
    ended = await pass_of(db_session, rewatch)
    assert ended.ended_at == minutes(60) and ended.end_reason == REWATCH_END_OFF
    assert await open_rewatches(db_session, user_id=user.id) == []


async def test_a_replay_of_the_last_episode_does_not_reopen_or_re_end_a_pass(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "rw-replay-last@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990007)
    rewatch = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    await stop(db_session, user_id=user.id, anime_id=anime.id, now=minutes(60))

    result = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("c", SyncKind.COMPLETION, eps[12].id, minutes(30), 1350.0, DURATION),
        now=minutes(90),
    )

    assert result.status is SyncStatus.APPLIED
    ended = await pass_of(db_session, rewatch)
    assert ended.end_reason == REWATCH_END_OFF and ended.ended_at == minutes(60)
    assert await entry_snapshot(db_session, user, anime) is None
    assert await mal_rows(db_session, user) == []


async def test_a_replay_recorded_before_the_pass_is_normal_mode(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-prepass@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990008)
    await make_entry(db_session, user, anime, progress=2)
    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(30))

    result = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("c", SyncKind.COMPLETION, eps[3].id, minutes(10), 1350.0, DURATION),
        now=minutes(40),
    )

    assert result.status is SyncStatus.APPLIED
    entry = await db_session.get(ListEntry, (user.id, anime.id), populate_existing=True)
    assert entry is not None and entry.progress == 3 and entry.mal_dirty
    assert [row.new_value for row in await mal_rows(db_session, user)] == [3]
    row = await row_of(db_session, user, eps[3])
    assert row is not None and row.completed and row.finished_at == minutes(10)


async def test_an_unmark_replay_in_a_pass_is_stale_against_a_later_pass_finish(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "rw-unmark-replay@arc.test")
    anime, eps = await show(db_session, anilist_id=990009)
    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    await report(db_session, user, eps[1], 0.95, at=minutes(20))

    stale = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("u", SyncKind.UNMARK, eps[1].id, minutes(10)),
        now=minutes(30),
    )
    assert stale.status is SyncStatus.STALE

    applied = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("u2", SyncKind.UNMARK, eps[1].id, minutes(25)),
        now=minutes(30),
    )
    assert applied.status is SyncStatus.APPLIED
    row = await row_of(db_session, user, eps[1])
    assert row is not None and row.finished_at is None and row.unmarked_at == minutes(25)


async def test_a_normal_unmark_replay_is_stale_against_a_later_re_finish(
    db_session: AsyncSession,
) -> None:
    """Normal mode: an un-mark recorded before a re-watch finished later is dropped."""
    user = await make_user(db_session, "rw-unmark-refinish@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990025)
    await make_entry(db_session, user, anime, progress=2)
    # Completed at -60 (list to 3), re-watched and finished again at +20.
    await report(db_session, user, eps[3], 0.95, at=minutes(-60))
    await report(db_session, user, eps[3], 0.1, at=minutes(0))
    await report(db_session, user, eps[3], 0.95, at=minutes(20))
    writes = len(await mal_rows(db_session, user))

    # Recorded offline at +10: after the first watch, before the re-finish.
    stale = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("u", SyncKind.UNMARK, eps[3].id, minutes(10)),
        now=minutes(30),
        list_changed_at=minutes(-60),
    )

    assert stale.status is SyncStatus.STALE
    row = await row_of(db_session, user, eps[3])
    assert row is not None and row.completed and row.finished_at == minutes(20)
    entry = await db_session.get(ListEntry, (user.id, anime.id), populate_existing=True)
    assert entry is not None and entry.progress == 3
    assert len(await mal_rows(db_session, user)) == writes

    # Recorded after the re-finish: applied, and the list comes down by one.
    applied = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("u2", SyncKind.UNMARK, eps[3].id, minutes(25)),
        now=minutes(30),
        list_changed_at=minutes(-60),
    )
    assert applied.status is SyncStatus.APPLIED
    entry = await db_session.get(ListEntry, (user.id, anime.id), populate_existing=True)
    assert entry is not None and entry.progress == 2


# --- the end of a pass ------------------------------------------------------------


async def test_a_pass_on_an_airing_show_ends_on_the_newest_aired_episode(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "rw-airing@arc.test")
    anime, eps = await show(db_session, anilist_id=990010, status="RELEASING", aired=6)
    rewatch = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))

    fifth = await report(db_session, user, eps[5], 0.95, at=minutes(1))
    assert not fifth.rewatch_ended
    assert (await pass_of(db_session, rewatch)).ended_at is None

    sixth = await report(db_session, user, eps[6], 0.95, at=minutes(2))
    assert sixth.rewatch_ended
    ended = await pass_of(db_session, rewatch)
    assert ended.end_reason == REWATCH_END_FINISHED and ended.ended_at == minutes(2)


async def test_a_truncated_live_duration_stamps_no_finish(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-S4's live guard holds for the latest finish, in both modes."""

    async def live(_session: AsyncSession, _episode_id: int) -> LiveDuration:
        return LiveDuration(live=True, expected=DURATION)

    monkeypatch.setattr("arc.services.playback.progress.live_duration", live)
    user = await make_user(db_session, "rw-live@arc.test")
    anime, eps = await show(db_session, anilist_id=990011)
    assert eps[1].state is not EpisodeState.READY

    normal = await report(db_session, user, eps[1], 0.95, at=minutes(1), duration=300.0)
    assert not normal.completed
    row = await row_of(db_session, user, eps[1])
    assert row is not None and row.finished_at is None and row.completed is False

    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(2))
    in_pass = await report(db_session, user, eps[2], 0.95, at=minutes(3), duration=300.0)
    assert in_pass.rewatching and not in_pass.completed and not in_pass.newly_completed
    row = await row_of(db_session, user, eps[2])
    assert row is not None and row.finished_at is None


# --- normal mode: the latest finish ----------------------------------------------


async def test_a_second_normal_completion_restamps_the_finish_without_a_second_write(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "rw-restamp@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990012)
    await make_entry(db_session, user, anime, progress=2)

    first = await report(db_session, user, eps[3], 0.95, at=minutes(0))
    assert first.newly_completed and first.list_progress == 3 and not first.rewatching
    await report(db_session, user, eps[3], 0.1, at=minutes(60))
    second = await report(db_session, user, eps[3], 0.95, at=minutes(80))

    assert not second.newly_completed and second.list_progress is None
    row = await row_of(db_session, user, eps[3])
    assert row is not None
    assert row.completed_at == minutes(0)
    assert row.finished_at == minutes(80)
    assert len(await mal_rows(db_session, user)) == 1


async def test_a_late_replay_never_moves_the_finish_backwards(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-backwards@arc.test")
    _anime, eps = await show(db_session, anilist_id=990013)
    await report(db_session, user, eps[1], 0.95, at=minutes(50))

    await report(db_session, user, eps[1], 0.95, at=minutes(60), reported_at=minutes(10))

    row = await row_of(db_session, user, eps[1])
    assert row is not None and row.finished_at == minutes(50)


# --- walks ------------------------------------------------------------------------


async def walk_of(session: AsyncSession, user: User, anime: Anime) -> Walk | None:
    return await session.get(Walk, (user.id, anime.id), populate_existing=True)


async def test_walks_follow_plays_and_completions(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-walk@arc.test")
    anime, eps = await show(db_session, anilist_id=990014)
    await clear_jobs(db_session)

    # A play of episode 4: the walk starts after 3. New → a reconciliation.
    await report(db_session, user, eps[4], 0.1, at=minutes(0))
    walk = await walk_of(db_session, user, anime)
    assert walk is not None and (walk.after_number, walk.touched_at) == (3, minutes(0))
    assert len(await jobs(db_session, COMPUTE_WANTS)) == 1

    # Still playing: touched, not moved → nothing queued.
    await clear_jobs(db_session)
    await report(db_session, user, eps[4], 0.2, at=minutes(5))
    walk = await walk_of(db_session, user, anime)
    assert walk is not None and (walk.after_number, walk.touched_at) == (3, minutes(5))
    assert await jobs(db_session, COMPUTE_WANTS) == []

    # Finished: the walk moves to 4 → a reconciliation.
    await report(db_session, user, eps[4], 0.95, at=minutes(20))
    walk = await walk_of(db_session, user, anime)
    assert walk is not None and walk.after_number == 4
    assert len(await jobs(db_session, COMPUTE_WANTS)) == 1

    # A replay recorded before the walk's last touch moves nothing.
    await clear_jobs(db_session)
    await report(db_session, user, eps[8], 0.1, at=minutes(30), reported_at=minutes(10))
    walk = await walk_of(db_session, user, anime)
    assert walk is not None and (walk.after_number, walk.touched_at) == (4, minutes(20))
    assert await jobs(db_session, COMPUTE_WANTS) == []


async def test_a_manual_mark_does_not_move_the_walk(db_session: AsyncSession) -> None:
    """FR-A1 (b) counts a walk from the episode being *played*; a hand mark is not one."""
    user = await make_user(db_session, "rw-walk-mark@arc.test")
    anime, eps = await show(db_session, anilist_id=990026)
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    await clear_jobs(db_session)

    marked = await report(db_session, user, eps[9], 0.0, at=minutes(0), force=True)

    assert marked.completed and marked.newly_completed
    assert await walk_of(db_session, user, anime) is None
    # The first mark's one job is FR-S4's completion reconciliation (every
    # newly completed episode queues it); a second mark, completing nothing
    # new, shows the walk queues nothing of its own.
    await clear_jobs(db_session)
    again = await report(db_session, user, eps[9], 0.0, at=minutes(1), force=True)
    assert not again.newly_completed
    assert await walk_of(db_session, user, anime) is None
    assert await jobs(db_session, COMPUTE_WANTS) == []

    await report(db_session, user, eps[9], 0.1, at=minutes(5))
    walk = await walk_of(db_session, user, anime)
    assert walk is not None and walk.after_number == 8
    assert len(await jobs(db_session, COMPUTE_WANTS)) == 1


async def test_a_play_in_a_pass_moves_the_walk_too(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-walk-pass@arc.test")
    anime, eps = await show(db_session, anilist_id=990015)
    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))

    await report(db_session, user, eps[2], 0.95, at=minutes(1))

    walk = await walk_of(db_session, user, anime)
    assert walk is not None and walk.after_number == 2


# --- the trip hook ------------------------------------------------------------------


async def test_a_completion_in_a_pass_still_queues_the_trip_sweep(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "rw-trip@arc.test")
    anime, eps = await show(db_session, anilist_id=990016)
    trip = Trip(
        user_id=user.id,
        anime_id=anime.id,
        first_number=3,
        last_number=4,
        count=2,
        deadline_at=minutes(60 * 24),
        created_at=minutes(-1),
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add_all([TripEpisode(trip_id=trip.id, episode_id=eps[n].id) for n in (3, 4)])
    await db_session.flush()
    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    await clear_jobs(db_session)

    await report(db_session, user, eps[1], 0.95, at=minutes(1))
    assert await jobs(db_session, TRIP_SWEEP) == []
    outcome = await report(db_session, user, eps[3], 0.95, at=minutes(2))

    assert outcome.rewatching and outcome.newly_completed
    assert len(await jobs(db_session, TRIP_SWEEP)) == 1


async def test_a_pass_finish_is_a_watch_for_trips(db_session: AsyncSession) -> None:
    """A trip episode never completed normally, finished in a pass, is watched."""
    user = await make_user(db_session, "rw-trip-watched@arc.test")
    anime, eps = await show(db_session, anilist_id=990024)
    trip = Trip(
        user_id=user.id,
        anime_id=anime.id,
        first_number=3,
        last_number=4,
        count=2,
        deadline_at=minutes(60 * 24 * 7),
        created_at=minutes(-1),
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add_all(
        [
            TripEpisode(
                trip_id=trip.id,
                episode_id=eps[n].id,
                state=TripEpisodeState.DELIVERED,
                delivered_at=minutes(-60),
            )
            for n in (3, 4)
        ]
    )
    await db_session.flush()
    held = {eps[3].id, eps[4].id}
    assert await held_episode_ids(db_session, held) == held

    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    await report(db_session, user, eps[3], 0.95, at=minutes(1))
    assert await held_episode_ids(db_session, held) == {eps[4].id}
    locked = await db_session.get(Trip, trip.id, with_for_update=True, populate_existing=True)
    assert locked is not None
    assert await end_if_complete(db_session, locked, now=minutes(2)) is None

    await report(db_session, user, eps[4], 0.95, at=minutes(3))
    for number in (3, 4):
        row = await row_of(db_session, user, eps[number])
        assert row is not None and row.completed is False and row.finished_at is not None
    assert await held_episode_ids(db_session, held) == set()
    assert await end_if_complete(db_session, locked, now=minutes(4)) == []
    assert locked.state is TripState.FINISHED


# --- the service ----------------------------------------------------------------------


async def test_start_is_idempotent_and_refuses_a_show_with_nothing_aired(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "rw-start@arc.test")
    anime, _eps = await show(db_session, anilist_id=990017)
    await clear_jobs(db_session)

    first = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    second = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(5))

    assert first.id == second.id and second.started_at == minutes(0)
    assert len(await jobs(db_session, COMPUTE_WANTS)) == 1

    future, _ = await show(db_session, anilist_id=990018, status="NOT_YET_RELEASED", aired=0)
    with pytest.raises(NothingAired):
        await start(db_session, user_id=user.id, anime_id=future.id, now=minutes(0))
    empty = await make_anime(db_session, anilist_id=990019, status="FINISHED", episodes=None)
    with pytest.raises(NothingAired):
        await start(db_session, user_id=user.id, anime_id=empty.id, now=minutes(0))
    assert await open_rewatches(db_session, user_id=user.id) == [first]


async def test_start_handles_the_open_pass_index_refusal(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race: the first read saw no pass, another request opened one since."""
    user = await make_user(db_session, "rw-race@arc.test")
    anime, _eps = await show(db_session, anilist_id=990020)
    other = Rewatch(user_id=user.id, anime_id=anime.id, started_at=minutes(0))
    db_session.add(other)
    await db_session.flush()

    real = rewatch_service.open_rewatch
    calls = 0

    async def blind_first(session: AsyncSession, *, user_id: int, anime_id: int) -> Rewatch | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await real(session, user_id=user_id, anime_id=anime_id)

    monkeypatch.setattr(rewatch_service, "open_rewatch", blind_first)
    got = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(5))

    assert got.id == other.id
    rows = (await db_session.scalars(select(Rewatch).where(Rewatch.user_id == user.id))).all()
    assert [row.id for row in rows] == [other.id]


async def test_stop_is_idempotent(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-stop@arc.test")
    anime, _eps = await show(db_session, anilist_id=990021)
    rewatch = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))

    assert await stop(db_session, user_id=user.id, anime_id=anime.id, now=minutes(10))
    assert not await stop(db_session, user_id=user.id, anime_id=anime.id, now=minutes(20))

    ended = await pass_of(db_session, rewatch)
    assert ended.ended_at == minutes(10) and ended.end_reason == REWATCH_END_OFF


async def test_rewatch_at_reads_the_pass_open_at_a_moment(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-at@arc.test")
    anime, _eps = await show(db_session, anilist_id=990022)
    first = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    await stop(db_session, user_id=user.id, anime_id=anime.id, now=minutes(10))
    second = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(20))

    async def at(n: float) -> int | None:
        found = await rewatch_at(db_session, user_id=user.id, anime_id=anime.id, at=minutes(n))
        return None if found is None else found.id

    assert await at(-1) is None
    assert await at(0) == first.id
    assert await at(9) == first.id
    assert await at(10) is None
    assert await at(20) == second.id
    assert await at(10_000) == second.id


async def test_the_pass_position_and_the_watch_view(db_session: AsyncSession) -> None:
    user = await make_user(db_session, "rw-view@arc.test")
    anime, eps = await show(db_session, anilist_id=990023)
    await make_entry(db_session, user, anime, progress=5)
    await report(db_session, user, eps[2], 0.95, at=minutes(-10))

    outside = await watch_view(db_session, user_id=user.id, anime_id=anime.id)
    assert outside.completed_ids == frozenset({eps[2].id}) and outside.list_progress == 5
    assert outside.rewatch is None

    rewatch = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    assert await pass_position(db_session, rewatch) == 0
    await report(db_session, user, eps[1], 0.95, at=minutes(1))
    await report(db_session, user, eps[3], 0.95, at=minutes(2))

    completed, progress, open_pass = await watch_view(
        db_session, user_id=user.id, anime_id=anime.id
    )
    assert completed == frozenset({eps[1].id, eps[3].id}) and progress == 0
    assert open_pass is not None and open_pass.id == rewatch.id
    assert await pass_position(db_session, rewatch) == 3
    assert await pass_finished(db_session, rewatch, episode_ids=[eps[1].id]) == frozenset(
        {eps[1].id}
    )
    narrowed = await watch_view(
        db_session, user_id=user.id, anime_id=anime.id, episode_ids=[eps[3].id]
    )
    assert narrowed.completed_ids == frozenset({eps[3].id})


# --- concurrency and routes -------------------------------------------------------------

USER_EMAIL = "rewatcher@arc.test"
USER_PASSWORD = "rewatcher-password"


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


async def api_show(
    factory: SessionFactory, *, anilist_id: int, aired: int | None = None, status: str = "FINISHED"
) -> tuple[int, dict[int, int]]:
    async with factory() as session:
        anime, eps = await show(session, anilist_id=anilist_id, aired=aired, status=status)
        await session.commit()
        return anime.id, {number: row.id for number, row in eps.items()}


async def test_concurrent_completions_in_a_pass_yield_one_newly_completed(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    anime_id, ids = await api_show(api_factory, anilist_id=990030)
    assert (await client.put(f"/api/anime/{anime_id}/rewatch")).status_code == 200

    for number in (1, 2):
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/progress",
                    json={
                        "episode_id": ids[number],
                        "position_s": DURATION * 0.95,
                        "duration_s": DURATION,
                    },
                )
                for _ in range(12)
            )
        )
        bodies = [r.json() for r in responses]
        assert all(r.status_code == 200 for r in responses), bodies
        assert all(body["rewatching"] and body["completed"] for body in bodies)
        assert sum(1 for body in bodies if body["newly_completed"]) == 1, bodies


async def test_the_rewatch_routes(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    anime_id, ids = await api_show(api_factory, anilist_id=990031)

    first = await client.put(f"/api/anime/{anime_id}/rewatch")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["position"] == 0 and body["finished_in_pass"] == 0 and body["total"] == 12

    progress = await client.post(
        "/api/progress",
        json={"episode_id": ids[2], "position_s": DURATION * 0.95, "duration_s": DURATION},
    )
    assert progress.json() == {
        "completed": True,
        "newly_completed": True,
        "list_progress": None,
        "rewatching": True,
        "rewatch_ended": False,
    }

    again = await client.put(f"/api/anime/{anime_id}/rewatch")
    assert again.status_code == 200
    assert again.json()["started_at"] == body["started_at"]
    assert again.json()["position"] == 2 and again.json()["finished_in_pass"] == 1

    unmarked = await client.delete(f"/api/episodes/{ids[2]}/watched")
    assert unmarked.json()["rewatching"] is True and unmarked.json()["list_progress"] is None

    assert (await client.delete(f"/api/anime/{anime_id}/rewatch")).status_code == 204
    assert (await client.delete(f"/api/anime/{anime_id}/rewatch")).status_code == 204
    async with api_factory() as session:
        assert await open_rewatches(session, user_id=user.id) == []
        assert await session.get(ListEntry, (user.id, anime_id)) is None

    assert (await client.put("/api/anime/987654321/rewatch")).status_code == 404
    assert (await client.delete("/api/anime/987654321/rewatch")).status_code == 404

    future_id, _ = await api_show(
        api_factory, anilist_id=990032, aired=0, status="NOT_YET_RELEASED"
    )
    refused = await client.put(f"/api/anime/{future_id}/rewatch")
    assert refused.status_code == 409
    assert refused.json()["detail"] == rewatch_service.NOTHING_AIRED_DETAIL


async def test_the_rewatch_routes_need_a_session_and_an_origin(
    api_app: FastAPI, api_factory: SessionFactory
) -> None:
    anime_id, _ids = await api_show(api_factory, anilist_id=990033)
    await add_user(api_factory, "rw-originless@arc.test", "rw-originless-password")

    async with api_transport(api_app) as anon:
        assert (await anon.put(f"/api/anime/{anime_id}/rewatch")).status_code == 401
        assert (await anon.delete(f"/api/anime/{anime_id}/rewatch")).status_code == 401

    async with api_transport(api_app, origin=None) as http:
        await http.post(
            "/api/auth/login",
            json={"email": "rw-originless@arc.test", "password": "rw-originless-password"},
            headers={"Origin": "http://localhost:5173"},
        )
        put = await http.put(f"/api/anime/{anime_id}/rewatch")
        delete_ = await http.delete(f"/api/anime/{anime_id}/rewatch")

    assert put.status_code == 403 and delete_.status_code == 403
    async with api_factory() as session:
        assert (await session.scalars(select(Rewatch))).all() == []


async def test_completed_episodes_include_pass_finishes(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    anime_id, ids = await api_show(api_factory, anilist_id=990034)
    first = await client.post(
        "/api/progress",
        json={"episode_id": ids[1], "position_s": DURATION * 0.95, "duration_s": DURATION},
    )
    assert first.json()["newly_completed"]
    assert (await client.put(f"/api/anime/{anime_id}/rewatch")).status_code == 200
    for number in (1, 2):
        await client.post(
            "/api/progress",
            json={
                "episode_id": ids[number],
                "position_s": DURATION * 0.95,
                "duration_s": DURATION,
            },
        )

    answer = await client.get(
        "/api/progress/completed", params={"episode_ids": [ids[1], ids[2], ids[3]]}
    )

    assert answer.status_code == 200
    got = {row["episode_id"]: row["completed_at"] for row in answer.json()["completed"]}
    assert set(got) == {ids[1], ids[2]}
    async with api_factory() as session:
        one = await session.get(WatchProgress, (user.id, ids[1]))
        assert one is not None and one.finished_at is not None and one.completed_at is not None
        assert one.finished_at > one.completed_at
        assert datetime.fromisoformat(got[ids[1]]) == one.finished_at


# --- fix round 2: the end at the server's time, trips since creation, the tail --


async def test_a_replayed_pass_end_ends_the_pass_at_the_servers_time(
    db_session: AsyncSession,
) -> None:
    """Offline in the pass: ep 12 finished at +10, ep 5 watched at +20, both replayed at +30."""
    user = await make_user(db_session, "rw-end-now@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990040)
    await make_entry(db_session, user, anime, progress=2)
    before = await entry_snapshot(db_session, user, anime)
    rewatch = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))

    last = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("c", SyncKind.COMPLETION, eps[12].id, minutes(10), 1350.0, DURATION),
        now=minutes(30),
    )
    fifth = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("p", SyncKind.POSITION, eps[5].id, minutes(20), 1350.0, DURATION),
        now=minutes(30),
    )

    assert last.status is SyncStatus.APPLIED and fifth.status is SyncStatus.APPLIED
    ended = await pass_of(db_session, rewatch)
    assert ended.ended_at == minutes(30) and ended.end_reason == REWATCH_END_FINISHED
    # Episode 5 was judged in the pass: a pass finish, no completion, no list.
    row = await row_of(db_session, user, eps[5])
    assert row is not None and row.completed is False and row.finished_at == minutes(20)
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []


async def a_trip_over(
    session: AsyncSession, user: User, anime: Anime, eps: dict[int, Episode]
) -> Trip:
    trip = Trip(
        user_id=user.id,
        anime_id=anime.id,
        first_number=3,
        last_number=4,
        count=2,
        deadline_at=minutes(60 * 24 * 7),
        created_at=minutes(0),
    )
    session.add(trip)
    await session.flush()
    session.add_all(
        [
            TripEpisode(
                trip_id=trip.id,
                episode_id=eps[n].id,
                state=TripEpisodeState.DELIVERED,
                delivered_at=minutes(1),
            )
            for n in (3, 4)
        ]
    )
    await session.flush()
    return trip


async def test_a_trip_made_after_an_old_pass_stays_active_and_held(
    db_session: AsyncSession,
) -> None:
    """A finish from an older pass over the trip's episodes is not a watch of the trip."""
    user = await make_user(db_session, "rw-trip-old-pass@arc.test")
    anime, eps = await show(db_session, anilist_id=990041)
    await make_entry(db_session, user, anime, progress=2)
    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(-600))
    await report(db_session, user, eps[3], 0.95, at=minutes(-590))
    await report(db_session, user, eps[4], 0.95, at=minutes(-580))
    await stop(db_session, user_id=user.id, anime_id=anime.id, now=minutes(-570))

    trip = await a_trip_over(db_session, user, anime, eps)
    held = {eps[3].id, eps[4].id}
    assert await held_episode_ids(db_session, held) == held
    locked = await db_session.get(Trip, trip.id, with_for_update=True, populate_existing=True)
    assert locked is not None
    assert await end_if_complete(db_session, locked, now=minutes(2)) is None
    assert locked.state is TripState.ACTIVE

    # Watched again during the trip, in a new pass: now it ends.
    await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(3))
    await report(db_session, user, eps[3], 0.95, at=minutes(4))
    await report(db_session, user, eps[4], 0.95, at=minutes(5))
    assert await held_episode_ids(db_session, held) == set()
    assert await end_if_complete(db_session, locked, now=minutes(6)) == []
    assert locked.state is TripState.FINISHED


# --- the tail: the pure rule at its edges ------------------------------------------

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
END = T0 + timedelta(hours=2)


def tail(
    *,
    finished: datetime | None,
    position: float = 0.95,
    at: datetime,
    ended_at: datetime = END,
    ending: bool = False,
) -> bool:
    return tail_applies(
        started_at=T0,
        ended_at=ended_at,
        old_finished_at=finished,
        old_position_s=DURATION * position,
        old_duration_s=DURATION,
        at=at,
        completion_fraction=0.90,
        ended_on_this_episode=ending,
    )


def test_the_tail_keeps_the_episode_that_ended_the_pass_for_good() -> None:
    assert tail(finished=END, at=END + timedelta(days=3), ending=True)
    # Ended by a replay: the finish is the device's earlier moment.
    assert tail(finished=END - timedelta(minutes=20), at=END + timedelta(days=3), ending=True)
    # The timestamp alone says nothing: not the ending episode, past the grace.
    assert not tail(finished=END, at=END + timedelta(days=3))


def test_the_tail_of_any_other_episode_lasts_the_grace() -> None:
    earlier = T0 + timedelta(minutes=30)
    assert TAIL_GRACE == timedelta(hours=1)
    assert tail(finished=earlier, at=END + timedelta(minutes=10))
    assert tail(finished=earlier, at=END + TAIL_GRACE)
    assert not tail(finished=earlier, at=END + TAIL_GRACE + timedelta(microseconds=1))
    assert not tail(finished=earlier, at=END + timedelta(days=3))


def test_the_tail_needs_the_row_still_past_the_mark() -> None:
    finished = T0 + timedelta(minutes=30)
    assert not tail(finished=finished, position=0.89, at=END + timedelta(minutes=1))
    assert not tail(finished=finished, position=0.0, at=END + timedelta(minutes=1))
    assert not tail(finished=END, position=0.5, at=END + timedelta(days=1), ending=True)
    assert not tail_applies(
        started_at=T0,
        ended_at=END,
        old_finished_at=END,
        old_position_s=None,
        old_duration_s=None,
        at=END,
        completion_fraction=0.90,
        ended_on_this_episode=True,
    )


def test_the_tail_needs_a_finish_in_the_pass() -> None:
    assert not tail(finished=None, at=END, ending=True)
    assert not tail(finished=T0 - timedelta(microseconds=1), at=END, ending=True)
    assert tail(finished=T0, at=END)
    assert in_pass(T0, T0)
    assert not in_pass(T0 - timedelta(microseconds=1), T0)
    assert not in_pass(None, T0)


# --- the tail, end to end --------------------------------------------------------


async def ended_by_finishing(
    session: AsyncSession, email: str, anilist_id: int
) -> tuple[User, Anime, dict[int, Episode], tuple[object, ...] | None]:
    """A pass over a 12-episode finished show, ep 9 left at 92 %, ended on ep 12 at +20."""
    user = await make_user(session, email)
    await link(session, user)
    anime, eps = await show(session, anilist_id=anilist_id)
    await make_entry(session, user, anime, progress=5)
    before = await entry_snapshot(session, user, anime)
    await start(session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    await report(session, user, eps[9], 0.92, at=minutes(10))
    assert (await report(session, user, eps[12], 0.95, at=minutes(20))).rewatch_ended
    return user, anime, eps, before


async def test_the_ending_episode_days_later_is_still_the_tail(db_session: AsyncSession) -> None:
    user, anime, eps, before = await ended_by_finishing(db_session, "rw-tail-a@arc.test", 990050)

    later = await report(db_session, user, eps[12], 0.97, at=minutes(20) + timedelta(days=3))

    assert later.rewatching and not later.newly_completed
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []


async def test_a_non_final_episode_days_later_is_normal_mode(db_session: AsyncSession) -> None:
    user, anime, eps, _before = await ended_by_finishing(db_session, "rw-tail-b@arc.test", 990051)

    later = await report(db_session, user, eps[9], 0.95, at=minutes(20) + timedelta(days=3))

    assert not later.rewatching and later.newly_completed and later.list_progress == 9
    assert [row.new_value for row in await mal_rows(db_session, user)] == [9]


async def test_a_non_final_episode_within_the_hour_is_the_tail(db_session: AsyncSession) -> None:
    user, anime, eps, before = await ended_by_finishing(db_session, "rw-tail-c@arc.test", 990052)

    within = await report(db_session, user, eps[9], 0.95, at=minutes(50))

    assert within.rewatching and not within.newly_completed
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []


async def switched_off_at_93(
    session: AsyncSession, email: str, anilist_id: int
) -> tuple[User, Anime, dict[int, Episode], tuple[object, ...] | None]:
    user = await make_user(session, email)
    await link(session, user)
    anime, eps = await show(session, anilist_id=anilist_id)
    await make_entry(session, user, anime, progress=5)
    before = await entry_snapshot(session, user, anime)
    await start(session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    await report(session, user, eps[8], 0.93, at=minutes(1))
    assert await stop(session, user_id=user.id, anime_id=anime.id, now=minutes(2))
    return user, anime, eps, before


async def test_off_at_93_then_the_credits_within_the_hour_are_the_tail(
    db_session: AsyncSession,
) -> None:
    user, anime, eps, before = await switched_off_at_93(db_session, "rw-tail-d@arc.test", 990053)

    credits = await report(db_session, user, eps[8], 0.98, at=minutes(3))

    assert credits.rewatching and not credits.newly_completed
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []


async def test_off_then_two_hours_later_is_normal_mode(db_session: AsyncSession) -> None:
    user, anime, eps, _before = await switched_off_at_93(db_session, "rw-tail-e@arc.test", 990054)

    later = await report(db_session, user, eps[8], 0.98, at=minutes(122))

    assert not later.rewatching and later.newly_completed and later.list_progress == 8
    assert len(await mal_rows(db_session, user)) == 1


async def test_a_scrub_under_the_mark_leaves_the_tail(db_session: AsyncSession) -> None:
    user, anime, eps, _before = await ended_by_finishing(db_session, "rw-tail-f@arc.test", 990055)

    under = await report(db_session, user, eps[12], 0.5, at=minutes(25))
    back = await report(db_session, user, eps[12], 0.95, at=minutes(40))

    assert under.rewatching  # still at the mark before this report
    assert not back.rewatching and back.newly_completed
    assert len(await mal_rows(db_session, user)) >= 1


async def test_a_fresh_open_from_zero_leaves_the_tail(db_session: AsyncSession) -> None:
    user, anime, eps, _before = await ended_by_finishing(db_session, "rw-tail-g@arc.test", 990056)
    days = minutes(20) + timedelta(days=2)

    opening = await report(db_session, user, eps[12], 0.0, at=days)
    playing = await report(db_session, user, eps[12], 0.3, at=days + timedelta(minutes=8))
    done = await report(db_session, user, eps[12], 0.95, at=days + timedelta(minutes=22))

    # The first report still finds the row past the mark (the open itself); the
    # play after it is a new watch.
    assert opening.rewatching
    assert not playing.rewatching and not done.rewatching
    assert done.newly_completed and done.list_progress == 12


# --- D: a clamped replay fails closed ----------------------------------------------


async def test_a_clamped_replay_from_inside_a_long_ended_pass_is_mal_free(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "rw-clamped@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990060)
    await make_entry(db_session, user, anime, progress=2)
    before = await entry_snapshot(db_session, user, anime)
    day = timedelta(days=1)
    await start(db_session, user_id=user.id, anime_id=anime.id, now=NOW - 45 * day)
    await stop(db_session, user_id=user.id, anime_id=anime.id, now=NOW - 35 * day)

    # Recorded 40 days ago, inside the pass; the sync clamp moves it to -30 d.
    results = await apply_batch(
        db_session,
        user_id=user.id,
        items=[
            SyncItem("c", SyncKind.COMPLETION, eps[3].id, NOW - 40 * day, 1350.0, DURATION),
            SyncItem("p", SyncKind.POSITION, eps[4].id, NOW - 40 * day, 1350.0, DURATION),
        ],
        now=NOW,
    )

    assert [r.status for r in results] == [SyncStatus.APPLIED, SyncStatus.APPLIED]
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []
    for number in (3, 4):
        row = await row_of(db_session, user, eps[number])
        assert row is not None and row.completed is False
        assert row.finished_at == NOW - 30 * day


async def test_the_episode_a_replay_ended_the_pass_on_keeps_its_tail(
    db_session: AsyncSession,
) -> None:
    """Ended by an offline replay: ``ended_at`` is the server's time, the finish the device's."""
    user = await make_user(db_session, "rw-tail-replay@arc.test")
    await link(db_session, user)
    anime, eps = await show(db_session, anilist_id=990061)
    await make_entry(db_session, user, anime, progress=5)
    before = await entry_snapshot(db_session, user, anime)
    rewatch = await start(db_session, user_id=user.id, anime_id=anime.id, now=minutes(0))
    result = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem("c", SyncKind.POSITION, eps[12].id, minutes(10), 0.92 * DURATION, DURATION),
        now=minutes(30),
    )
    assert result.status is SyncStatus.APPLIED
    ended = await pass_of(db_session, rewatch)
    assert ended.ended_at == minutes(30) and ended.end_reason == REWATCH_END_FINISHED
    row = await row_of(db_session, user, eps[12])
    assert row is not None and row.finished_at == minutes(10)

    days = minutes(30) + timedelta(days=3)
    reopened = await report(db_session, user, eps[12], 0.92, at=days)
    credits = await report(db_session, user, eps[12], 0.98, at=days + timedelta(minutes=2))

    assert reopened.rewatching and credits.rewatching
    assert not credits.newly_completed
    assert await entry_snapshot(db_session, user, anime) == before
    assert await mal_rows(db_session, user) == []
