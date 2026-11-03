"""M21 T3: the want rules rewritten — any episode on click, walks, rewatch windows.

Owner, 2026-11-03: "every episode on every show is playable when clicked on".
A demand is wanted unconditionally until finished, stale or withdrawn, at most
``demand_max_open`` at once (FR-A14); a play opens a window from the walk's
position on any show (FR-A1); an open rewatch pass replaces the list window
with one from the pass's position (FR-W7). The first section is the pure
:func:`walk_counts` table; the rest runs the reconciler and the demand against
the database (plan §9 T3's acceptance tests, and §2.4's defensive ones).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import arc
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    MediaFile,
    Torrent,
    UpdatedBy,
    User,
    Walk,
    Want,
    WatchProgress,
)
from arc.services.acquisition.demand import (
    DEMAND_CAP,
    NOT_AIRED,
    DemandRefused,
    demand,
    demand_cap_message,
    withdraw,
)
from arc.services.acquisition.holdings import replace_holdings
from arc.services.acquisition.names import COMPUTE_WANTS, QBIT_CANCEL
from arc.services.acquisition.qbit import QBIT_CANCELLED
from arc.services.acquisition.reject import reject_download
from arc.services.acquisition.rules import DEMAND_MAX_OPEN_KEY, PAUSED_KEY, SLOT_CAP_KEY
from arc.services.acquisition.wants import (
    REASON_NOT_WANTING,
    REASON_WITHDRAWN,
    STALE_DROP_REASON,
    _current_plan,
    compute_wants,
    slot_view,
    walk_counts,
)
from arc.services.catalog.airing import FINISHED, RELEASING
from arc.services.catalog.lists import set_list_entry
from arc.services.playback.progress import record_progress
from arc.services.playback.rewatch import start as start_rewatch
from arc.services.playback.rewatch import stop as stop_rewatch
from arc.services.trips.cancel import cancel_trip
from arc.services.trips.create import create_trip
from arc.services.trips.release import release_episode
from tests.acquisition_helpers import (
    acquisition_settings,
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)

LENGTH = 1440.0
DEVICE = "0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


# --- walk_counts, pure ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("pass_started", "expected"),
    [
        # No pass: a walk touched within D counts; nothing about the entry is read.
        (None, True),
        # A pass open: only a walk that moved since it began counts.
        (T0 - timedelta(hours=1), True),
        (T0, True),
        (T0 + timedelta(hours=1), False),
    ],
)
def test_walk_counts(pass_started: datetime | None, expected: bool) -> None:
    assert walk_counts(T0, pass_started_at=pass_started) is expected


# --- the reconciler --------------------------------------------------------------------

pg = pytest.mark.pg


async def a_show(
    session: AsyncSession, anilist_id: int, *, status: str = FINISHED, aired: int = 12
) -> tuple[Anime, list[Episode]]:
    anime = await make_anime(session, anilist_id=anilist_id, status=status, episodes=12)
    return anime, await make_episodes(session, anime, 12, aired_through=aired)


async def wanted(session: AsyncSession, user: User) -> set[int]:
    rows = await session.scalars(
        select(Want.episode_id).where(Want.user_id == user.id, Want.dropped_at.is_(None))
    )
    return set(rows.all())


async def jobs(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(
        select(Job).where(Job.type == job_type, Job.status == JobStatus.PENDING)
    )
    return list(rows.all())


async def downloading(session: AsyncSession, episode: Episode, info_hash: str) -> Torrent:
    """Put ``episode`` mid-download with a single torrent of its own."""
    episode.state = EpisodeState.DOWNLOADING
    torrent = Torrent(
        episode_id=episode.id, info_hash=info_hash, qbit_state="downloading", progress=0.4
    )
    session.add(torrent)
    await session.flush()
    return torrent


async def walk(session: AsyncSession, user: User, anime: Anime, after: int, at: datetime) -> Walk:
    row = Walk(user_id=user.id, anime_id=anime.id, after_number=after, touched_at=at)
    session.add(row)
    await session.flush()
    return row


async def finished(
    session: AsyncSession, user: User, episode: Episode, at: datetime
) -> WatchProgress:
    """A watch of ``episode`` finished at ``at`` (written directly: no walk moves)."""
    row = await session.get(WatchProgress, (user.id, episode.id))
    if row is None:
        row = WatchProgress(user_id=user.id, episode_id=episode.id, position_s=0.0)
        session.add(row)
    row.position_s = LENGTH
    row.duration_s = LENGTH
    row.completed = True
    row.completed_at = row.completed_at or at
    row.finished_at = at
    await session.flush()
    return row


@pg
@pytest.mark.parametrize("case", ["completed", "n0", "dormant", "device", "k_full"])
async def test_a_click_on_episode_3_is_never_shelved_or_cancelled_while_downloading(
    db_session: AsyncSession, case: str
) -> None:
    """FR-A14: whatever the list status, N, dormancy, device holding or slot cap."""
    anime, rows = await a_show(db_session, 972001)
    user = await make_user(db_session, f"m21-click-{case}@arc.test")
    if case == "dormant":
        await make_entry(db_session, user, anime, status=ListStatus.PLANNED, activated=False)
    elif case == "k_full":
        # Held by the cap: an airing show takes the one slot first, and episode
        # 3 is below this show's progress (the held show's watched-past delete).
        await set_setting(db_session, SLOT_CAP_KEY, 1)
        busy, _ = await a_show(db_session, 972002, status=RELEASING)
        await make_entry(db_session, user, busy, progress=0)
        await make_entry(db_session, user, anime, progress=4)
    else:
        await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    if case == "n0":
        await set_setting(db_session, "look_ahead_n", 0)
    if case == "device":
        await replace_holdings(
            db_session, user_id=user.id, device_id=DEVICE, episode_ids=[rows[2].id], now=T0
        )

    await demand(db_session, user_id=user.id, episode=rows[2], now=datetime.now(UTC))
    torrent = await downloading(db_session, rows[2], "c" * 40)
    await compute_wants(db_session)
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[2].id))
    assert want is not None and want.dropped_at is None and want.demanded_at is not None
    assert rows[2].state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state != QBIT_CANCELLED
    assert await jobs(db_session, QBIT_CANCEL) == []
    if case == "k_full":
        assert rows[4].id not in await wanted(db_session, user), "the show still waits"


@pg
async def test_n0_still_honours_a_demand(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972003)
    user = await make_user(db_session, "m21-n0@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await set_setting(db_session, "look_ahead_n", 0)

    await demand(db_session, user_id=user.id, episode=rows[7], now=datetime.now(UTC))
    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[7].id}, "no window, the demand only"


@pg
async def test_a_finished_demand_is_deleted_not_shelved(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972004)
    user = await make_user(db_session, "m21-finish@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    pressed = datetime.now(UTC)
    await demand(db_session, user_id=user.id, episode=rows[2], now=pressed)
    # An old finish from the first watch does not end the new demand.
    await finished(db_session, user, rows[2], pressed - timedelta(days=200))
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[2].id}

    await finished(db_session, user, rows[2], pressed + timedelta(minutes=25))
    result = await compute_wants(db_session)

    assert await db_session.get(Want, (user.id, rows[2].id)) is None
    assert result.removed == 1 and result.shelved == 0


@pg
async def test_a_finished_demand_on_a_window_episode_lives_on_as_an_ordinary_want(
    db_session: AsyncSession,
) -> None:
    """Still asked for by a window (here a re-watch of a listed show's next episode)."""
    anime, rows = await a_show(db_session, 972005)
    user = await make_user(db_session, "m21-finish-window@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    pressed = datetime.now(UTC)
    await demand(db_session, user_id=user.id, episode=rows[4], now=pressed)
    row = await finished(db_session, user, rows[4], pressed + timedelta(minutes=25))
    # The list did not move (a watch that set no ``completed`` in this test).
    row.completed = False
    await db_session.flush()

    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is None
    assert want.demanded_at is None, "no longer a demand: it counts for nothing"


@pg
async def test_a_stale_demand_is_dropped_and_its_download_cancelled(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 972006)
    user = await make_user(db_session, "m21-stale@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    long_ago = datetime.now(UTC) - timedelta(days=60)
    await demand(db_session, user_id=user.id, episode=rows[2], now=long_ago)
    await demand(db_session, user_id=user.id, episode=rows[3], now=long_ago)
    # Episode 4 was played yesterday: that restarts its clock.
    db_session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=rows[3].id,
            position_s=300.0,
            duration_s=LENGTH,
            updated_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    torrent = await downloading(db_session, rows[2], "d" * 40)
    # Episode 7 is ready: its stale demand is dropped (retention's anchor).
    await demand(db_session, user_id=user.id, episode=rows[6], now=long_ago)
    rows[6].state = EpisodeState.READY
    await db_session.flush()

    first = await compute_wants(db_session)
    ready = await db_session.get(Want, (user.id, rows[6].id))
    assert ready is not None and ready.dropped_at is not None
    assert ready.drop_reason == STALE_DROP_REASON
    second = await compute_wants(db_session)

    # Not ready: deleted, not dropped (nothing to anchor; a dropped row would
    # keep a later window from fetching it).
    assert await db_session.get(Want, (user.id, rows[2].id)) is None
    assert ready.dropped_at is not None and ready.demanded_at is not None, "not revived"
    assert rows[2].state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    assert [job.payload["episode_id"] for job in await jobs(db_session, QBIT_CANCEL)] == [
        rows[2].id
    ]
    assert first.dropped == 1 and second.dropped == 0, "and it stays dropped"
    assert first.removed >= 1
    assert await wanted(db_session, user) == {rows[3].id}


@pg
async def test_a_stale_demand_a_window_still_wants_keeps_downloading_as_a_window_want(
    db_session: AsyncSession,
) -> None:
    """Orchestrator 2026-11-03: stale, but the window's next episode — not cancelled."""
    anime, rows = await a_show(db_session, 972007)
    user = await make_user(db_session, "m21-stale-window@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await demand(
        db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC) - timedelta(days=60)
    )
    torrent = await downloading(db_session, rows[4], "b" * 40)

    result = await compute_wants(db_session)
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is None
    assert want.demanded_at is None, "an ordinary window want now"
    assert result.dropped == 0
    assert rows[4].state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state != QBIT_CANCELLED
    assert await jobs(db_session, QBIT_CANCEL) == []


# --- the cap -------------------------------------------------------------------------


@pg
async def test_the_fourth_demand_is_refused_with_the_three_open_ones(
    db_session: AsyncSession,
) -> None:
    await set_setting(db_session, DEMAND_MAX_OPEN_KEY, 3)
    anime, rows = await a_show(db_session, 972010)
    user = await make_user(db_session, "m21-cap@arc.test")
    for row in rows[:3]:
        await demand(db_session, user_id=user.id, episode=row, now=datetime.now(UTC))

    with pytest.raises(DemandRefused) as refused:
        await demand(db_session, user_id=user.id, episode=rows[3], now=datetime.now(UTC))

    assert refused.value.code == DEMAND_CAP
    assert refused.value.message == demand_cap_message(3)
    assert refused.value.message == (
        "You already have 3 episodes on request. Finish or cancel one first."
    )
    assert [(found.episode_id, found.number) for found in refused.value.open] == [
        (rows[0].id, 1),
        (rows[1].id, 2),
        (rows[2].id, 3),
    ]
    assert {found.anime_id for found in refused.value.open} == {anime.id}
    assert {found.title for found in refused.value.open} == {"Frieren: Beyond Journey's End"}
    assert await db_session.get(Want, (user.id, rows[3].id)) is None

    # Pressing an open one again restamps it and never counts twice.
    again = await demand(db_session, user_id=user.id, episode=rows[1], now=datetime.now(UTC))
    assert again.want is not None
    # A finished demand frees its place.
    await finished(db_session, user, rows[0], datetime.now(UTC) + timedelta(minutes=1))
    await demand(db_session, user_id=user.id, episode=rows[3], now=datetime.now(UTC))
    # And 0 is no cap.
    await set_setting(db_session, DEMAND_MAX_OPEN_KEY, 0)
    for row in rows[4:9]:
        await demand(db_session, user_id=user.id, episode=row, now=datetime.now(UTC))


@pg
async def test_two_clicks_at_the_cap_let_exactly_one_through(
    api_factory: SessionFactory,
) -> None:
    """The count is read under the want lock, so the last place is taken once."""
    async with api_factory() as session:
        await set_setting(session, DEMAND_MAX_OPEN_KEY, 3)
        _, rows = await a_show(session, 972011)
        user = await make_user(session, "m21-race@arc.test")
        for row in rows[:2]:
            await demand(session, user_id=user.id, episode=row, now=datetime.now(UTC))
        await session.commit()
        user_id, ids = user.id, [row.id for row in rows]

    async def press(episode_id: int) -> str:
        async with api_factory() as session:
            episode = await session.get(Episode, episode_id)
            assert episode is not None
            try:
                await demand(session, user_id=user_id, episode=episode, now=datetime.now(UTC))
            except DemandRefused as exc:
                await session.rollback()
                return exc.code
            await session.commit()
            return "accepted"

    outcomes = await asyncio.gather(*(press(episode_id) for episode_id in ids[2:6]))

    assert sorted(outcomes) == ["accepted", DEMAND_CAP, DEMAND_CAP, DEMAND_CAP]
    async with api_factory() as session:
        demanded = await session.scalars(
            select(Want.episode_id).where(Want.user_id == user_id, Want.demanded_at.is_not(None))
        )
        assert len(demanded.all()) == 3


@pg
async def test_an_unaired_episode_is_refused(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972012, status=RELEASING, aired=4)
    user = await make_user(db_session, "m21-unaired@arc.test")

    with pytest.raises(DemandRefused) as refused:
        await demand(db_session, user_id=user.id, episode=rows[6], now=datetime.now(UTC))

    assert refused.value.code == NOT_AIRED
    assert await db_session.get(Want, (user.id, rows[6].id)) is None


# --- withdraw ------------------------------------------------------------------------


@pg
async def test_withdraw_ends_a_demand(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972013)
    user = await make_user(db_session, "m21-withdraw@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    await demand(db_session, user_id=user.id, episode=rows[2], now=datetime.now(UTC))
    torrent = await downloading(db_session, rows[2], "e" * 40)
    await compute_wants(db_session)
    for job in await jobs(db_session, COMPUTE_WANTS):
        await db_session.delete(job)
    await db_session.flush()

    now = datetime.now(UTC)
    assert await withdraw(db_session, user_id=user.id, episode_id=rows[2].id, now=now) is True

    # Shelved and cancelled by the withdrawal itself, not at the next tick.
    want = await db_session.get(Want, (user.id, rows[2].id))
    assert want is not None and want.demanded_at is None
    assert want.dropped_at == now and want.drop_reason == REASON_WITHDRAWN
    assert rows[2].state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    assert [job.payload["episode_id"] for job in await jobs(db_session, QBIT_CANCEL)] == [
        rows[2].id
    ]
    assert len(await jobs(db_session, COMPUTE_WANTS)) == 1
    assert await withdraw(db_session, user_id=user.id, episode_id=rows[2].id, now=now) is False
    await compute_wants(db_session)
    assert want.dropped_at == now, "stays shelved"
    assert want.drop_reason == REASON_NOT_WANTING, "relabelled: the show is not followed"


@pg
async def test_withdrawing_leaves_another_users_demanded_download_running(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 972014)
    user = await make_user(db_session, "m21-withdraw-a@arc.test")
    other = await make_user(db_session, "m21-withdraw-b@arc.test")
    for someone in (user, other):
        await demand(db_session, user_id=someone.id, episode=rows[2], now=datetime.now(UTC))
    torrent = await downloading(db_session, rows[2], "5" * 40)

    await withdraw(db_session, user_id=user.id, episode_id=rows[2].id, now=datetime.now(UTC))
    await compute_wants(db_session)

    mine = await db_session.get(Want, (user.id, rows[2].id))
    theirs = await db_session.get(Want, (other.id, rows[2].id))
    assert mine is not None and mine.dropped_at is not None
    assert theirs is not None and theirs.dropped_at is None and theirs.demanded_at is not None
    assert rows[2].state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state != QBIT_CANCELLED
    assert await jobs(db_session, QBIT_CANCEL) == []


@pg
async def test_withdrawing_a_window_wanted_download_keeps_it_as_a_window_want(
    db_session: AsyncSession,
) -> None:
    """The user's own admitted window still wants it: no shelve, no cancel."""
    anime, rows = await a_show(db_session, 972015)
    user = await make_user(db_session, "m21-withdraw-window@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))
    torrent = await downloading(db_session, rows[4], "6" * 40)

    await withdraw(db_session, user_id=user.id, episode_id=rows[4].id, now=datetime.now(UTC))
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is None and want.demanded_at is None
    result = await compute_wants(db_session)

    assert want.dropped_at is None and want.demanded_at is None
    assert result.added == result.removed == result.shelved == result.cancelled == 0
    assert rows[4].state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state != QBIT_CANCELLED
    assert await jobs(db_session, QBIT_CANCEL) == []


# --- walks ---------------------------------------------------------------------------


@pg
async def test_a_play_on_an_unlisted_show_wants_the_next_at_halfway(
    db_session: AsyncSession,
) -> None:
    """Ep 4 played on an unlisted show, N = 1: ep 5 is wanted once at halfway."""
    anime, rows = await a_show(db_session, 972020)
    user = await make_user(db_session, "m21-walk@arc.test")

    await record_progress(
        db_session, user_id=user.id, episode=rows[3], position_s=0.3 * LENGTH, duration_s=LENGTH
    )
    await compute_wants(db_session)
    assert rows[4].id not in await wanted(db_session, user)

    await record_progress(
        db_session, user_id=user.id, episode=rows[3], position_s=0.6 * LENGTH, duration_s=LENGTH
    )
    await compute_wants(db_session)

    assert rows[4].id in await wanted(db_session, user)
    assert rows[5].id not in await wanted(db_session, user)
    assert await db_session.get(ListEntry, (user.id, anime.id)) is None, "no list entry"


class _FreshCatalog:
    """A catalogue stub for ``set_list_entry`` on a row that is fresh enough."""

    def healthy(self, source: str) -> bool:
        return True

    async def media(self, anime_id: int) -> None:  # pragma: no cover
        raise AssertionError("the cached row is fresh enough")


async def set_status(
    session: AsyncSession, user: User, anime: Anime, status: ListStatus, **fields: object
) -> None:
    """The real Arc-side status write behind ``PUT /api/list/{id}``."""
    anime.detail_source = "anilist"
    anime.refreshed_at = datetime.now(UTC)
    await session.flush()
    await set_list_entry(
        session,
        _FreshCatalog(),  # type: ignore[arg-type]
        user_id=user.id,
        anime_id=anime.id,
        status=status,
        **fields,  # type: ignore[arg-type]
    )


@pg
@pytest.mark.parametrize("status", [ListStatus.DROPPED, ListStatus.ON_HOLD])
async def test_a_walk_survives_finishing_an_episode_on_a_dropped_or_on_hold_show(
    db_session: AsyncSession, status: ListStatus
) -> None:
    """FR-S4's list advance is not the user setting the status (review P7)."""
    anime, rows = await a_show(db_session, 972027 + (status is ListStatus.ON_HOLD))
    user = await make_user(db_session, f"m21-walk-{status.value}@arc.test")
    entry = await make_entry(db_session, user, anime, status=status, progress=3)
    entry.updated_by = UpdatedBy.ARC
    entry.updated_at = datetime.now(UTC) - timedelta(days=3)
    await db_session.flush()

    await record_progress(
        db_session, user_id=user.id, episode=rows[3], position_s=0.6 * LENGTH, duration_s=LENGTH
    )
    await compute_wants(db_session)
    assert rows[4].id in await wanted(db_session, user), "ep 5 at halfway through ep 4"
    torrent = await downloading(db_session, rows[4], "1" * 40)

    await record_progress(
        db_session, user_id=user.id, episode=rows[3], position_s=LENGTH, duration_s=LENGTH
    )
    await compute_wants(db_session)

    assert entry.status is status and entry.progress == 4, "FR-S4 advanced the list"
    walk_row = await db_session.get(Walk, (user.id, anime.id))
    assert walk_row is not None and walk_row.after_number == 4
    assert rows[4].id in await wanted(db_session, user)
    assert rows[4].state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state != QBIT_CANCELLED


@pg
async def test_setting_the_show_dropped_in_arc_ends_the_walk(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972021)
    user = await make_user(db_session, "m21-walk-drop@arc.test")
    await make_entry(db_session, user, anime, progress=2)
    await walk(db_session, user, anime, 4, datetime.now(UTC) - timedelta(hours=2))
    await compute_wants(db_session)
    assert rows[4].id in await wanted(db_session, user)

    await set_status(db_session, user, anime, ListStatus.DROPPED)
    await compute_wants(db_session)

    assert await db_session.get(Walk, (user.id, anime.id)) is None
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is not None
    assert want.drop_reason == REASON_NOT_WANTING


@pg
async def test_the_fr_w5_auto_complete_ends_the_walk(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972029)
    user = await make_user(db_session, "m21-walk-complete@arc.test")
    entry = await make_entry(db_session, user, anime, progress=11)

    await record_progress(
        db_session, user_id=user.id, episode=rows[11], position_s=LENGTH, duration_s=LENGTH
    )

    assert entry.status is ListStatus.COMPLETED
    assert await db_session.get(Walk, (user.id, anime.id)) is None


@pg
async def test_a_mal_side_status_change_or_a_score_edit_leaves_the_walk(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 972031)
    user = await make_user(db_session, "m21-walk-keep@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    await walk(db_session, user, anime, 4, datetime.now(UTC) - timedelta(hours=2))

    # A score edit in Arc that re-sends the status the show already has.
    await set_status(db_session, user, anime, ListStatus.COMPLETED, score=7, score_given=True)
    assert await db_session.get(Walk, (user.id, anime.id)) is not None
    # A MyAnimeList-side change to dropped (the sync writes the row; no end_walk).
    entry = await db_session.get(ListEntry, (user.id, anime.id))
    assert entry is not None
    entry.status = ListStatus.DROPPED
    entry.updated_by = UpdatedBy.MAL
    entry.updated_at = datetime.now(UTC)
    await db_session.flush()
    await compute_wants(db_session)

    assert await db_session.get(Walk, (user.id, anime.id)) is not None
    assert await wanted(db_session, user) == {rows[4].id}


@pg
async def test_a_lapsed_walk_shelves_its_leftovers(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972022)
    user = await make_user(db_session, "m21-walk-lapse@arc.test")
    row = await walk(db_session, user, anime, 4, datetime.now(UTC) - timedelta(days=1))
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[4].id}

    row.touched_at = datetime.now(UTC) - timedelta(days=30)
    await db_session.flush()
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is not None
    assert want.drop_reason == REASON_NOT_WANTING


@pg
async def test_a_finished_walk_row_is_deleted_not_shelved(db_session: AsyncSession) -> None:
    """``following`` includes walking: the watched-past row goes, the next comes."""
    anime, rows = await a_show(db_session, 972023)
    user = await make_user(db_session, "m21-walk-past@arc.test")
    row = await walk(db_session, user, anime, 4, datetime.now(UTC) - timedelta(hours=1))
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[4].id}

    row.after_number = 5
    row.touched_at = datetime.now(UTC)
    await db_session.flush()
    await compute_wants(db_session)

    assert await db_session.get(Want, (user.id, rows[4].id)) is None
    assert await wanted(db_session, user) == {rows[5].id}


@pg
async def test_a_listed_show_with_a_backwards_walk_wants_both_windows(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 972024)
    user = await make_user(db_session, "m21-walk-back@arc.test")
    await make_entry(db_session, user, anime, progress=8)
    await walk(db_session, user, anime, 2, datetime.now(UTC))

    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[2].id, rows[8].id}


@pg
async def test_the_device_skip_applies_on_walks(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972025)
    user = await make_user(db_session, "m21-walk-held@arc.test")
    await walk(db_session, user, anime, 4, datetime.now(UTC))
    await replace_holdings(
        db_session,
        user_id=user.id,
        device_id=DEVICE,
        episode_ids=[rows[4].id],
        now=datetime.now(UTC),
    )

    await compute_wants(db_session)

    assert await wanted(db_session, user) == set(), "held on the device, and no slide to 6"


@pg
async def test_a_walk_never_wants_an_unaired_episode(db_session: AsyncSession) -> None:
    """A walk row from a crafted report past the broadcast asks for nothing unaired."""
    anime, rows = await a_show(db_session, 972026, status=RELEASING, aired=4)
    user = await make_user(db_session, "m21-walk-unaired@arc.test")
    await walk(db_session, user, anime, 6, datetime.now(UTC))
    other = await make_user(db_session, "m21-walk-edge@arc.test")
    await walk(db_session, other, anime, 4, datetime.now(UTC))

    await compute_wants(db_session)

    assert await wanted(db_session, user) == set()
    assert await wanted(db_session, other) == set(), "episode 5 has not aired"


# --- rewatch -------------------------------------------------------------------------


@pg
async def test_a_rewatch_window_counts_from_the_pass_with_the_list_window_suspended(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 972030)
    user = await make_user(db_session, "m21-rewatch@arc.test")
    await make_entry(db_session, user, anime, progress=8)
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[8].id}

    began = datetime.now(UTC) - timedelta(hours=1)
    await start_rewatch(db_session, user_id=user.id, anime_id=anime.id, now=began)
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[0].id}, "position 0: episode 1"

    for row in rows[:2]:
        await finished(db_session, user, row, began + timedelta(minutes=30))
    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[2].id}, "from the pass, not the list"
    assert await db_session.get(Want, (user.id, rows[8].id)) is None


# --- trips -----------------------------------------------------------------------------


@pg
async def test_a_demanded_trip_episode_enters_desired(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await a_show(db_session, 972040)
    user = await make_user(db_session, "m21-trip@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=3, now=T0)
    await compute_wants(db_session)
    trip_row = await db_session.get(Want, (user.id, rows[6].id))
    assert trip_row is not None and trip_row.trip is True

    await demand(db_session, user_id=user.id, episode=rows[6], now=datetime.now(UTC))
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[6].id))
    assert want is not None and want.trip is False and want.demanded_at is not None


# --- 2.4: nothing that cancels on "nobody wants this" stops an open demand -----------------


@pg
async def test_a_trip_cancel_leaves_a_demanded_download_running(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await a_show(db_session, 972041)
    user = await make_user(db_session, "m21-trip-cancel@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=4)
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=3, now=T0)
    ).trip
    await compute_wants(db_session)
    # Demanded before the next reconciliation has re-read the trip flag.
    await demand(db_session, user_id=user.id, episode=rows[5], now=datetime.now(UTC))
    torrent = await downloading(db_session, rows[5], "f" * 40)

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=datetime.now(UTC))
    await compute_wants(db_session)

    assert rows[5].state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state != QBIT_CANCELLED
    assert all(
        job.payload["episode_id"] != rows[5].id for job in await jobs(db_session, QBIT_CANCEL)
    )
    assert rows[5].id in await wanted(db_session, user)


@pg
async def test_a_trip_release_keeps_a_demanded_episode(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    anime, rows = await a_show(db_session, 972042)
    user = await make_user(db_session, "m21-trip-release@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=4)
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=3, now=T0)
    await compute_wants(db_session)
    await demand(db_session, user_id=user.id, episode=rows[5], now=datetime.now(UTC))
    rows[5].state = EpisodeState.MATCHED
    await db_session.flush()

    released = await release_episode(db_session, settings, rows[5].id)

    assert released.outcome == "kept"
    assert released.why == "somebody wants it for streaming"


@pg
async def test_a_rejected_file_leaves_the_demand_open(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    anime, rows = await a_show(db_session, 972043)
    user = await make_user(db_session, "m21-reject@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    await demand(db_session, user_id=user.id, episode=rows[2], now=datetime.now(UTC))
    await downloading(db_session, rows[2], "a" * 40)
    rows[2].state = EpisodeState.MATCHING
    media = MediaFile(path=str(settings.downloads_dir / str(rows[2].id) / "wrong.mkv"))
    db_session.add(media)
    await db_session.flush()

    moved = await reject_download(db_session, media, downloads_dir=settings.downloads_dir)
    await compute_wants(db_session)

    assert moved is rows[2] and rows[2].state is EpisodeState.UNAVAILABLE
    want = await db_session.get(Want, (user.id, rows[2].id))
    assert want is not None and want.dropped_at is None and want.demanded_at is not None


# --- the window refusal is gone ---------------------------------------------------------


def test_the_window_refusal_is_gone() -> None:
    """M21: ``not_in_window`` is retired and the demand no longer asks the window."""
    root = Path(arc.__file__).parent
    pattern = re.compile(r"not_in_window|NOT_IN_WINDOW")
    hits = [str(path) for path in root.rglob("*.py") if pattern.search(path.read_text())]
    assert hits == []
    assert "coverable" not in (root / "services" / "acquisition" / "demand.py").read_text()


# --- fix round 2: rewatch off, the slot cap, stale demands, quiet_since, pause ---------


@pg
async def test_turning_a_rewatch_off_stops_its_fetching(db_session: AsyncSession) -> None:
    """The pass's walk goes with the pass (owner decision 6: it "ends on off")."""
    anime, rows = await a_show(db_session, 972050)
    user = await make_user(db_session, "m21-rw-off@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    await start_rewatch(
        db_session, user_id=user.id, anime_id=anime.id, now=datetime.now(UTC) - timedelta(hours=1)
    )
    await record_progress(
        db_session, user_id=user.id, episode=rows[0], position_s=LENGTH, duration_s=LENGTH
    )
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[1].id}
    torrent = await downloading(db_session, rows[1], "2" * 40)

    await stop_rewatch(db_session, user_id=user.id, anime_id=anime.id, now=datetime.now(UTC))
    await compute_wants(db_session)

    assert await db_session.get(Walk, (user.id, anime.id)) is None
    assert await wanted(db_session, user) == set()
    assert rows[1].state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED


async def held_show(
    session: AsyncSession, user: User, anilist_id: int
) -> tuple[Anime, list[Episode]]:
    """A watching show at progress 4 that K = 1 holds back behind an airing one."""
    await set_setting(session, SLOT_CAP_KEY, 1)
    busy, _ = await a_show(session, anilist_id + 1, status=RELEASING)
    await make_entry(session, user, busy, progress=0)
    anime, rows = await a_show(session, anilist_id)
    await make_entry(session, user, anime, progress=4)
    return anime, rows


@pg
async def test_a_withdrawn_demand_on_a_held_show_stays_shelved_and_holds_no_slot(
    db_session: AsyncSession,
) -> None:
    """Withdrawn outside the window of a show the cap holds: shelved, no slot held."""
    user = await make_user(db_session, "m21-held-withdraw@arc.test")
    anime, rows = await held_show(db_session, user, 972052)
    await demand(db_session, user_id=user.id, episode=rows[8], now=datetime.now(UTC))
    torrent = await downloading(db_session, rows[8], "3" * 40)
    await compute_wants(db_session)

    await withdraw(db_session, user_id=user.id, episode_id=rows[8].id, now=datetime.now(UTC))
    await compute_wants(db_session)
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[8].id))
    assert want is not None and want.dropped_at is not None
    assert want.drop_reason == REASON_WITHDRAWN, "kept: the show is followed, only held"
    assert rows[8].state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    view = await slot_view(db_session, user.id)
    assert view.waiting == frozenset({anime.id}) and view.fetching == 1, "only the busy show"


@pg
async def test_a_stale_demand_on_a_held_shows_window_episode_is_kept_as_a_window_want(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, "m21-held-stale@arc.test")
    _, rows = await held_show(db_session, user, 972054)
    await demand(
        db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC) - timedelta(days=60)
    )
    torrent = await downloading(db_session, rows[4], "4" * 40)

    await compute_wants(db_session)
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is None and want.demanded_at is None
    assert rows[4].state is EpisodeState.DOWNLOADING
    assert torrent.qbit_state != QBIT_CANCELLED


@pg
async def test_walk_only_and_rewatch_shows_compete_for_one_slot(db_session: AsyncSession) -> None:
    """K = 1: the pass started later than the walk last moved, so it takes the slot."""
    await set_setting(db_session, SLOT_CAP_KEY, 1)
    walked, walked_rows = await a_show(db_session, 972056)
    rewatched, rewatched_rows = await a_show(db_session, 972057)
    user = await make_user(db_session, "m21-slots@arc.test")
    await walk(db_session, user, walked, 4, datetime.now(UTC) - timedelta(hours=3))
    await make_entry(db_session, user, rewatched, status=ListStatus.COMPLETED, progress=12)
    await start_rewatch(
        db_session,
        user_id=user.id,
        anime_id=rewatched.id,
        now=datetime.now(UTC) - timedelta(hours=1),
    )

    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rewatched_rows[0].id}
    assert (await slot_view(db_session, user.id)).waiting == frozenset({walked.id})
    assert walked_rows[4].id not in await wanted(db_session, user)


async def ready_long_ago(session: AsyncSession, episode: Episode) -> None:
    episode.state = EpisodeState.READY
    episode.state_changed_at = datetime.now(UTC) - timedelta(days=60)
    await session.flush()


@pg
@pytest.mark.parametrize("walked", [False, True])
async def test_a_walks_move_restarts_the_stale_clock_of_window_wants(
    db_session: AsyncSession, walked: bool
) -> None:
    anime, rows = await a_show(db_session, 972058 + int(walked))
    user = await make_user(db_session, f"m21-quiet-walk-{walked}@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    await ready_long_ago(db_session, rows[4])
    entry.updated_at = datetime.now(UTC) - timedelta(days=60)
    if walked:
        # Re-playing episode 2 yesterday: the user is plainly still at the show.
        await walk(db_session, user, anime, 1, datetime.now(UTC) - timedelta(days=1))
    await db_session.flush()

    result = await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None
    assert (want.dropped_at is None) is walked
    assert result.dropped == (0 if walked else 1)


@pg
async def test_a_pass_start_restarts_the_stale_clock_of_its_window(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 972060)
    user = await make_user(db_session, "m21-quiet-pass@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    entry = await db_session.get(ListEntry, (user.id, anime.id))
    assert entry is not None
    entry.updated_at = datetime.now(UTC) - timedelta(days=60)
    began = datetime.now(UTC) - timedelta(days=1)
    await start_rewatch(db_session, user_id=user.id, anime_id=anime.id, now=began)
    for row in rows[:4]:
        await finished(db_session, user, row, began + timedelta(hours=1))
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[4].id}
    await ready_long_ago(db_session, rows[4])

    result = await compute_wants(db_session)

    assert result.dropped == 0
    assert await wanted(db_session, user) == {rows[4].id}


@pg
async def test_a_dormant_entry_with_a_walk_has_a_window_and_is_not_dormant(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 972061)
    user = await make_user(db_session, "m21-dormant-walk@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.PLANNED, activated=False)
    await walk(db_session, user, anime, 2, datetime.now(UTC))

    plan = await _current_plan(db_session)
    await compute_wants(db_session)

    assert (user.id, anime.id) not in plan.dormant
    assert (user.id, anime.id) in plan.following
    assert await wanted(db_session, user) == {rows[2].id}


@pg
async def test_the_pause_leaves_walks_and_passes_untouched(db_session: AsyncSession) -> None:
    anime, _ = await a_show(db_session, 972062)
    other, _ = await a_show(db_session, 972063)
    user = await make_user(db_session, "m21-paused@arc.test")
    moved = datetime.now(UTC) - timedelta(days=30)
    await walk(db_session, user, anime, 4, moved)
    rewatch = await start_rewatch(
        db_session, user_id=user.id, anime_id=other.id, now=datetime.now(UTC)
    )
    await set_setting(db_session, PAUSED_KEY, True)

    result = await compute_wants(db_session)

    assert result.wanted == 0 and await wanted(db_session, user) == set()
    walk_row = await db_session.get(Walk, (user.id, anime.id))
    assert walk_row is not None and walk_row.touched_at == moved
    assert rewatch.ended_at is None


@pg
async def test_a_walk_older_than_the_pass_is_ignored(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 972064)
    user = await make_user(db_session, "m21-old-walk@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    await walk(db_session, user, anime, 6, datetime.now(UTC) - timedelta(hours=2))
    await start_rewatch(
        db_session, user_id=user.id, anime_id=anime.id, now=datetime.now(UTC) - timedelta(hours=1)
    )

    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[0].id}, "the pass's window only"


@pg
async def test_finishing_a_pass_deletes_its_walk(db_session: AsyncSession) -> None:
    """``rewatch.finish``: the last episode of the pass ends it, and its walk with it."""
    anime, rows = await a_show(db_session, 972066)
    user = await make_user(db_session, "m21-rw-finish@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.COMPLETED, progress=12)
    rewatch = await start_rewatch(
        db_session, user_id=user.id, anime_id=anime.id, now=datetime.now(UTC) - timedelta(hours=1)
    )
    await record_progress(
        db_session, user_id=user.id, episode=rows[10], position_s=LENGTH, duration_s=LENGTH
    )
    assert await db_session.get(Walk, (user.id, anime.id)) is not None

    outcome = await record_progress(
        db_session, user_id=user.id, episode=rows[11], position_s=LENGTH, duration_s=LENGTH
    )

    assert outcome.rewatch_ended is True
    await db_session.refresh(rewatch)
    assert rewatch.ended_at is not None
    assert await db_session.get(Walk, (user.id, anime.id)) is None


@pg
async def test_withdrawing_on_a_held_show_does_not_let_the_row_admit_its_own_show(
    db_session: AsyncSession,
) -> None:
    """Review: the plan is asked while the row is still a demand (never an occupant).

    K = 1, N = 2: show A downloads and holds the slot, show B at progress 4
    waits. A demand on B's episode 5, downloading, then withdrawn.
    """
    await set_setting(db_session, SLOT_CAP_KEY, 1)
    await set_setting(db_session, "look_ahead_n", 2)
    user = await make_user(db_session, "m21-withdraw-k1@arc.test")
    show_a, rows_a = await a_show(db_session, 972068)
    await make_entry(db_session, user, show_a, progress=0)
    show_b, rows_b = await a_show(db_session, 972069)
    await make_entry(db_session, user, show_b, progress=4)
    await compute_wants(db_session)
    await downloading(db_session, rows_a[0], "7" * 40)
    await compute_wants(db_session)
    assert (await slot_view(db_session, user.id)).waiting == frozenset({show_b.id})
    await demand(db_session, user_id=user.id, episode=rows_b[4], now=datetime.now(UTC))
    torrent = await downloading(db_session, rows_b[4], "8" * 40)

    await withdraw(db_session, user_id=user.id, episode_id=rows_b[4].id, now=datetime.now(UTC))

    want = await db_session.get(Want, (user.id, rows_b[4].id))
    assert want is not None and want.dropped_at is not None
    assert want.drop_reason == REASON_WITHDRAWN and want.demanded_at is None
    assert torrent.qbit_state == QBIT_CANCELLED
    assert [job.payload["episode_id"] for job in await jobs(db_session, QBIT_CANCEL)] == [
        rows_b[4].id
    ]
    await compute_wants(db_session)

    view = await slot_view(db_session, user.id)
    assert view.waiting == frozenset({show_b.id}) and view.fetching == 1
    assert {row.id for row in rows_b} & await wanted(db_session, user) == set(), "nothing new on B"
