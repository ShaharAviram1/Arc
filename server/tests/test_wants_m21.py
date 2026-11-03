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
from arc.services.acquisition.rules import DEMAND_MAX_OPEN_KEY, SLOT_CAP_KEY
from arc.services.acquisition.wants import (
    REASON_NOT_WANTING,
    STALE_DROP_REASON,
    compute_wants,
    walk_counts,
)
from arc.services.catalog.airing import FINISHED, RELEASING
from arc.services.playback.progress import record_progress
from arc.services.playback.rewatch import start as start_rewatch
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


def an_entry(status: ListStatus, *, by: UpdatedBy, at: datetime) -> ListEntry:
    return ListEntry(user_id=1, anime_id=1, status=status, progress=0, updated_by=by, updated_at=at)


@pytest.mark.parametrize(
    ("pass_started", "entry", "expected"),
    [
        # No list entry: the walk counts.
        (None, None, True),
        # Watching, changed after the walk: still counts.
        (None, an_entry(ListStatus.WATCHING, by=UpdatedBy.ARC, at=T0 + timedelta(hours=1)), True),
        # Dropped in Arc before the walk moved: the walk is newer, it counts.
        (None, an_entry(ListStatus.DROPPED, by=UpdatedBy.ARC, at=T0 - timedelta(days=1)), True),
        # Dropped / on hold / completed in Arc after it moved: ended.
        (None, an_entry(ListStatus.DROPPED, by=UpdatedBy.ARC, at=T0 + timedelta(hours=1)), False),
        (None, an_entry(ListStatus.ON_HOLD, by=UpdatedBy.ARC, at=T0 + timedelta(hours=1)), False),
        # FR-W5's auto-complete stamps the entry with the walk's own moment.
        (None, an_entry(ListStatus.COMPLETED, by=UpdatedBy.ARC, at=T0), False),
        # A MyAnimeList-side change is not the user acting in Arc.
        (None, an_entry(ListStatus.DROPPED, by=UpdatedBy.MAL, at=T0 + timedelta(hours=1)), True),
        # A pass open: only a walk that moved since it began counts, status aside.
        (T0 - timedelta(hours=1), None, True),
        (T0 + timedelta(hours=1), None, False),
        (
            T0 - timedelta(hours=1),
            an_entry(ListStatus.DROPPED, by=UpdatedBy.ARC, at=T0 + timedelta(hours=2)),
            True,
        ),
    ],
)
def test_walk_counts(
    pass_started: datetime | None, entry: ListEntry | None, expected: bool
) -> None:
    assert walk_counts(T0, pass_started_at=pass_started, entry=entry) is expected


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

    first = await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, rows[2].id))
    assert want is not None and want.dropped_at is not None
    assert want.drop_reason == STALE_DROP_REASON
    second = await compute_wants(db_session)

    assert want.dropped_at is not None and want.demanded_at is not None, "not revived"
    assert rows[2].state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED
    assert [job.payload["episode_id"] for job in await jobs(db_session, QBIT_CANCEL)] == [
        rows[2].id
    ]
    assert first.dropped == 1 and second.dropped == 0, "and it stays dropped"
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
    assert refused.value.message.startswith("You already have 3 episodes fetching on request.")
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

    assert await withdraw(db_session, user_id=user.id, episode_id=rows[2].id) is True
    assert len(await jobs(db_session, COMPUTE_WANTS)) == 1
    assert await withdraw(db_session, user_id=user.id, episode_id=rows[2].id) is False
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[2].id))
    assert want is not None and want.dropped_at is not None, "an ordinary want: shelved"
    assert want.drop_reason == REASON_NOT_WANTING
    assert rows[2].state is EpisodeState.NOT_WANTED
    assert torrent.qbit_state == QBIT_CANCELLED


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


@pg
async def test_setting_the_show_dropped_after_the_walk_stops_it(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 972021)
    user = await make_user(db_session, "m21-walk-drop@arc.test")
    moved = datetime.now(UTC) - timedelta(hours=2)
    await walk(db_session, user, anime, 4, moved)
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[4].id}

    entry = await make_entry(db_session, user, anime, status=ListStatus.DROPPED, progress=4)
    entry.updated_by = UpdatedBy.ARC
    entry.updated_at = moved + timedelta(hours=1)
    await db_session.flush()
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is not None
    assert want.drop_reason == REASON_NOT_WANTING


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
    assert [job.payload["episode_id"] for job in await jobs(db_session, QBIT_CANCEL)] != [
        rows[5].id
    ]
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
