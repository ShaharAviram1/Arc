"""The M20 window rules at the default N = 1 (FR-A1, FR-A10, FR-A14, FR-A15).

Owner, 2026-10-13: one episode ready per watcher, one more at the halfway mark,
nothing fetched that a device of the user already holds, and play on demand for
what the window covers. The first half is the pure :func:`window` table; the
second runs the reconciler, the demand and the progress paths against the
database. No ``look_ahead_n`` override here except in the one test that needs a
window with a second, hungry episode in it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    DeviceHolding,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    User,
    Want,
    WatchProgress,
)
from arc.services.acquisition import rules
from arc.services.acquisition.demand import (
    ACQUISITION_PAUSED,
    NOT_IN_WINDOW,
    STORAGE_HELD,
    DemandRefused,
    demand,
)
from arc.services.acquisition.holdings import replace_holdings
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    DEMAND_PRIORITY,
    QBIT_TOP,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
)
from arc.services.acquisition.rules import PAUSED_KEY, SLOT_CAP_KEY
from arc.services.acquisition.wants import (
    DEVICE_HOLDING_DAYS,
    STALE_DROP_REASON,
    compute_wants,
    coverable,
    is_halfway,
    slot_view,
    window,
)
from arc.services.catalog.airing import FINISHED
from arc.services.playback.progress import crosses_prefetch, record_progress
from arc.services.playback.sync import SyncItem, SyncKind, SyncStatus, apply_item
from arc.services.trips.create import create_trip
from arc.services.trips.deliver import confirm_delivered, release_delivered
from tests.acquisition_helpers import (
    acquisition_settings,
    fake_free_space,
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

#: A 24-minute episode, the length every progress report below is measured against.
LENGTH = 1440.0


# --- window(), pure -----------------------------------------------------------


def episodes_with(*, count: int, aired: int) -> list[Episode]:
    """``count`` episodes, ids equal to their numbers, the first ``aired`` in the past."""
    return [
        Episode(
            id=number,
            anime_id=1,
            number=number,
            air_at=NOW - timedelta(days=aired - number + 1)
            if number <= aired
            else NOW + timedelta(days=number - aired),
        )
        for number in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    ("progress", "look_ahead", "aired", "held", "prefetch", "expected"),
    [
        # N = 1: the one next episode.
        (4, 1, 12, frozenset(), False, [5]),
        # Half-way through 5: one more.
        (4, 1, 12, frozenset(), True, [5, 6]),
        # The device holds 5: nothing — and the window does not slide to 6.
        (4, 1, 12, frozenset({5}), False, []),
        # The device holds 6 and the prefetch reaches it: only 5.
        (4, 1, 12, frozenset({6}), True, [5]),
        # The device holds 5 and the user is half-way through it: 6 only.
        (4, 1, 12, frozenset({5}), True, [6]),
        # 6 has not aired: the prefetch has nothing to add.
        (4, 1, 5, frozenset(), True, [5]),
        # N = 0 is "fetch nothing", prefetch or not.
        (4, 0, 12, frozenset(), True, []),
        # N = 2 with the prefetch reaches p+3.
        (4, 2, 12, frozenset(), True, [5, 6, 7]),
        # A held episode outside the window changes nothing.
        (4, 1, 12, frozenset({9}), False, [5]),
    ],
)
def test_the_m20_window_table(
    progress: int,
    look_ahead: int,
    aired: int,
    held: frozenset[int],
    prefetch: bool,
    expected: list[int],
) -> None:
    picked = window(
        episodes_with(count=12, aired=aired),
        progress=progress,
        look_ahead=look_ahead,
        now=NOW,
        anime_status="RELEASING",
        next_airing=None,
        held=held,
        prefetch=prefetch,
    )

    assert [episode.number for episode in picked] == expected


@pytest.mark.parametrize(
    ("position", "duration", "expected"),
    [
        (0.49 * LENGTH, LENGTH, False),
        (0.5 * LENGTH, LENGTH, True),
        (0.89 * LENGTH, LENGTH, True),
        (0.9 * LENGTH, LENGTH, False),
        (0.0, 0.0, False),
        (100.0, None, False),
    ],
)
def test_the_halfway_band(position: float, duration: float | None, expected: bool) -> None:
    assert is_halfway(position, duration) is expected


@pytest.mark.parametrize(
    ("old_position", "old_duration", "position", "expected"),
    [
        (None, None, 0.51 * LENGTH, True),
        (None, None, 0.49 * LENGTH, False),
        (0.4 * LENGTH, LENGTH, 0.55 * LENGTH, True),
        (0.55 * LENGTH, LENGTH, 0.6 * LENGTH, False),
        (0.6 * LENGTH, LENGTH, 0.3 * LENGTH, False),
        (10.0, 0.0, 0.7 * LENGTH, True),
    ],
)
def test_the_prefetch_crossing(
    old_position: float | None, old_duration: float | None, position: float, expected: bool
) -> None:
    assert crosses_prefetch(old_position, old_duration, position, LENGTH) is expected


# --- the reconciler ---------------------------------------------------------------

pg = pytest.mark.pg


async def a_show(
    session: AsyncSession, anilist_id: int, *, status: str = FINISHED
) -> tuple[Anime, list[Episode]]:
    anime = await make_anime(session, anilist_id=anilist_id, status=status, episodes=12)
    return anime, await make_episodes(session, anime, 12, aired_through=12)


async def wanted(session: AsyncSession, user: User) -> set[int]:
    rows = await session.scalars(
        select(Want.episode_id).where(Want.user_id == user.id, Want.dropped_at.is_(None))
    )
    return set(rows.all())


async def watched_to(session: AsyncSession, user: User, episode: Episode, fraction: float) -> None:
    session.add(
        WatchProgress(
            user_id=user.id,
            episode_id=episode.id,
            position_s=fraction * LENGTH,
            duration_s=LENGTH,
        )
    )
    await session.flush()


async def pending_jobs(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(
        select(Job).where(Job.type == job_type, Job.status == JobStatus.PENDING)
    )
    return list(rows.all())


@pg
async def test_the_default_window_is_one_episode(db_session: AsyncSession) -> None:
    _anime, rows = await a_show(db_session, 970001)
    user = await make_user(db_session, "m20-n1@arc.test")
    await make_entry(db_session, user, _anime, progress=4)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[4].id}


@pg
async def test_49_percent_wants_only_the_next_episode(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 970002)
    user = await make_user(db_session, "m20-49@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await watched_to(db_session, user, rows[4], 0.49)

    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[4].id}


@pg
async def test_51_percent_after_one_reconciliation_wants_the_one_after(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 970003)
    user = await make_user(db_session, "m20-51@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[4].id}

    # The report that crosses half-way queues the reconciliation…
    outcome = await record_progress(
        db_session,
        user_id=user.id,
        episode=rows[4],
        position_s=0.51 * LENGTH,
        duration_s=LENGTH,
    )
    assert outcome.prefetch is True
    assert len(await pending_jobs(db_session, COMPUTE_WANTS)) == 1
    # …and that one reconciliation wants 6 as well.
    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[4].id, rows[5].id}


@pg
async def test_a_delivered_trip_copy_is_skipped_until_released(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await a_show(db_session, 970004)
    user = await make_user(db_session, "m20-trip@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=NOW)
    ).trip
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[4].id, etag=None, now=NOW
    )

    await compute_wants(db_session)
    assert await wanted(db_session, user) == set(), "held on the device, and no slide to 6"

    await release_delivered(db_session, user=user, trip_id=trip.id, episode_id=rows[4].id, now=NOW)
    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[4].id}


@pg
async def test_a_device_holding_skips_the_episode(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 970005)
    user = await make_user(db_session, "m20-hold@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await replace_holdings(
        db_session,
        user_id=user.id,
        device_id="0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69",
        episode_ids=[rows[4].id],
        now=datetime.now(UTC),
    )

    await compute_wants(db_session)

    assert await wanted(db_session, user) == set()


@pg
async def test_a_holding_reported_15_days_ago_does_not_skip(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 970006)
    user = await make_user(db_session, "m20-ttl@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await replace_holdings(
        db_session,
        user_id=user.id,
        device_id="0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69",
        episode_ids=[rows[4].id],
        now=datetime.now(UTC) - timedelta(days=DEVICE_HOLDING_DAYS + 1),
    )

    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[4].id}


@pg
async def test_another_users_holding_does_not_skip_yours(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 970007)
    user = await make_user(db_session, "m20-mine@arc.test")
    other = await make_user(db_session, "m20-theirs@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await replace_holdings(
        db_session,
        user_id=other.id,
        device_id="0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69",
        episode_ids=[rows[4].id],
        now=datetime.now(UTC),
    )

    await compute_wants(db_session)

    assert await wanted(db_session, user) == {rows[4].id}


# --- play on demand ----------------------------------------------------------------


@pg
async def test_demanding_a_held_episode_wants_it_at_the_top_of_the_queue(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 970010)
    user = await make_user(db_session, "m20-demand@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await replace_holdings(
        db_session,
        user_id=user.id,
        device_id="0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69",
        episode_ids=[rows[4].id],
        now=datetime.now(UTC),
    )
    await compute_wants(db_session)
    assert await wanted(db_session, user) == set()

    outcome = await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))

    assert outcome.started is True
    assert rows[4].state is EpisodeState.WANTED
    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.demanded_at is not None and want.dropped_at is None
    searches = await pending_jobs(db_session, SEARCH_RELEASE)
    assert [job.priority for job in searches] == [DEMAND_PRIORITY]

    # The reconciler keeps it, held or not.
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[4].id}


@pg
async def test_demanding_outside_the_window_is_refused(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 970011)
    user = await make_user(db_session, "m20-far@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)

    with pytest.raises(DemandRefused) as refused:
        await demand(db_session, user_id=user.id, episode=rows[8], now=datetime.now(UTC))

    assert refused.value.code == NOT_IN_WINDOW
    assert await db_session.get(Want, (user.id, rows[8].id)) is None


@pg
async def test_the_coverable_set_counts_the_prefetch_and_the_users_own_wants(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 970012)
    user = await make_user(db_session, "m20-cover@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    assert await coverable(db_session, user.id) == {rows[4].id}
    await watched_to(db_session, user, rows[4], 0.6)
    assert await coverable(db_session, user.id) == {rows[4].id, rows[5].id}


@pg
async def test_demand_moves_a_queued_window_search_up(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 970013)
    user = await make_user(db_session, "m20-bump@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    (search,) = await pending_jobs(db_session, SEARCH_RELEASE)
    assert search.priority == SEARCH_RELEASE_PRIORITY

    outcome = await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))

    assert outcome.started is False, "already wanted"
    assert search.priority == DEMAND_PRIORITY


@pg
async def test_demanding_a_downloading_episode_moves_its_torrent_up(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 970014)
    user = await make_user(db_session, "m20-top@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    rows[4].state = EpisodeState.DOWNLOADING
    await db_session.flush()

    outcome = await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))

    assert outcome.topped is True
    assert len(await pending_jobs(db_session, QBIT_TOP)) == 1


@pg
async def test_demand_is_refused_while_paused_or_held(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    anime, rows = await a_show(db_session, 970015)
    user = await make_user(db_session, "m20-paused@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    await set_setting(db_session, PAUSED_KEY, True)
    with pytest.raises(DemandRefused) as paused:
        await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))
    assert paused.value.code == ACQUISITION_PAUSED

    await set_setting(db_session, PAUSED_KEY, False)
    fake_free_space(monkeypatch, rules, 1)
    with pytest.raises(DemandRefused) as held:
        await demand(
            db_session,
            user_id=user.id,
            episode=rows[4],
            now=datetime.now(UTC),
            settings=acquisition_settings(tmp_path),
        )
    assert held.value.code == STORAGE_HELD

    # An episode already on its way is not held up by either.
    rows[4].state = EpisodeState.DOWNLOADING
    await db_session.flush()
    outcome = await demand(
        db_session,
        user_id=user.id,
        episode=rows[4],
        now=datetime.now(UTC),
        settings=acquisition_settings(tmp_path),
    )
    assert outcome.state is EpisodeState.DOWNLOADING


@pg
async def test_a_demanded_episode_of_a_waiting_show_is_wanted_anyway(
    db_session: AsyncSession,
) -> None:
    """FR-A10: never refused and never counted; the show still waits for its slot.

    N = 2 here, on purpose: a show only waits while its window has something it
    has not asked for, so the window needs a second episode for the show to go
    on waiting once the first is demanded.
    """
    await set_setting(db_session, "look_ahead_n", 2)
    await set_setting(db_session, SLOT_CAP_KEY, 1)
    airing, airing_rows = await a_show(db_session, 970020, status="RELEASING")
    waiting, waiting_rows = await a_show(db_session, 970021)
    user = await make_user(db_session, "m20-cap@arc.test")
    await make_entry(db_session, user, airing, progress=0)
    await make_entry(db_session, user, waiting, progress=4)
    await compute_wants(db_session)
    view = await slot_view(db_session, user.id)
    assert view.waiting == frozenset({waiting.id})
    assert await wanted(db_session, user) == {airing_rows[0].id, airing_rows[1].id}

    await demand(db_session, user_id=user.id, episode=waiting_rows[4], now=datetime.now(UTC))
    await compute_wants(db_session)

    assert waiting_rows[4].id in await wanted(db_session, user)
    assert waiting_rows[5].id not in await wanted(db_session, user)
    view = await slot_view(db_session, user.id)
    assert view.waiting == frozenset({waiting.id}), "still waiting for a slot"
    assert view.fetching == 1, "the demand occupies no slot"


@pg
async def test_a_demanded_want_ends_when_the_user_watches_past_it(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 970022)
    user = await make_user(db_session, "m20-past@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))

    entry.progress = 5
    await db_session.flush()
    await compute_wants(db_session)

    assert await db_session.get(Want, (user.id, rows[4].id)) is None
    assert await wanted(db_session, user) == {rows[5].id}


@pg
async def test_the_stale_drop_still_applies_to_a_demanded_want(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 970023)
    user = await make_user(db_session, "m20-stale@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))
    long_ago = datetime.now(UTC) - timedelta(days=60)
    rows[4].state = EpisodeState.READY
    rows[4].state_changed_at = long_ago
    entry = await db_session.get(ListEntry, (user.id, anime.id))
    assert entry is not None
    entry.updated_at = long_ago
    await db_session.flush()

    first = await compute_wants(db_session)
    second = await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.drop_reason == STALE_DROP_REASON
    assert first.dropped == 1 and second.dropped == 0, "and it stays dropped"


@pg
async def test_a_demand_on_a_show_that_stops_being_watched_is_shelved(
    db_session: AsyncSession,
) -> None:
    anime, rows = await a_show(db_session, 970024)
    user = await make_user(db_session, "m20-shelve@arc.test")
    entry = await make_entry(db_session, user, anime, progress=4)
    await demand(db_session, user_id=user.id, episode=rows[4], now=datetime.now(UTC))

    entry.status = ListStatus.ON_HOLD
    await db_session.flush()
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[4].id))
    assert want is not None and want.dropped_at is not None


# --- the progress paths -----------------------------------------------------------


@pg
async def test_the_halfway_crossing_enqueues_once(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 970030)
    user = await make_user(db_session, "m20-cross@arc.test")

    first = await record_progress(
        db_session, user_id=user.id, episode=rows[0], position_s=0.4 * LENGTH, duration_s=LENGTH
    )
    assert first.prefetch is False
    assert await pending_jobs(db_session, COMPUTE_WANTS) == []

    second = await record_progress(
        db_session, user_id=user.id, episode=rows[0], position_s=0.55 * LENGTH, duration_s=LENGTH
    )
    assert second.prefetch is True
    assert second.previous_position_s == pytest.approx(0.4 * LENGTH)
    assert second.previous_duration_s == pytest.approx(LENGTH)
    (job,) = await pending_jobs(db_session, COMPUTE_WANTS)
    job.status = JobStatus.DONE
    await db_session.flush()

    third = await record_progress(
        db_session, user_id=user.id, episode=rows[0], position_s=0.6 * LENGTH, duration_s=LENGTH
    )
    assert third.prefetch is False
    assert await pending_jobs(db_session, COMPUTE_WANTS) == [], "not on the second report"


@pg
async def test_a_replayed_offline_sample_crosses_too(db_session: AsyncSession) -> None:
    """Owner Q1: on reconnect the outbox replays the samples and the window catches up."""
    anime, rows = await a_show(db_session, 970031)
    user = await make_user(db_session, "m20-sync@arc.test")
    await make_entry(db_session, user, anime, progress=4)
    await compute_wants(db_session)
    await watched_to(db_session, user, rows[4], 0.2)
    progress = await db_session.get(WatchProgress, (user.id, rows[4].id))
    assert progress is not None
    progress.updated_at = datetime.now(UTC) - timedelta(hours=2)
    await db_session.flush()

    result = await apply_item(
        db_session,
        user_id=user.id,
        item=SyncItem(
            client_id="c1",
            kind=SyncKind.POSITION,
            episode_id=rows[4].id,
            at=datetime.now(UTC) - timedelta(hours=1),
            position_s=0.7 * LENGTH,
            duration_s=LENGTH,
        ),
        now=datetime.now(UTC),
    )

    assert result.status is SyncStatus.APPLIED
    assert len(await pending_jobs(db_session, COMPUTE_WANTS)) == 1
    await compute_wants(db_session)
    assert await wanted(db_session, user) == {rows[4].id, rows[5].id}


# --- holdings ---------------------------------------------------------------------


@pg
async def test_a_report_replaces_the_devices_set(db_session: AsyncSession) -> None:
    anime, rows = await a_show(db_session, 970040)
    user = await make_user(db_session, "m20-replace@arc.test")
    device = "0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69"
    other_device = "7d1e9c2a-1b3f-4e5d-8c6b-0a9f8e7d6c5b"
    now = datetime.now(UTC)
    await replace_holdings(
        db_session, user_id=user.id, device_id=other_device, episode_ids=[rows[0].id], now=now
    )

    changed = await replace_holdings(
        db_session,
        user_id=user.id,
        device_id=device,
        episode_ids=[rows[4].id, rows[5].id, 99_999_999],
        now=now,
    )
    assert changed is True
    again = await replace_holdings(
        db_session, user_id=user.id, device_id=device, episode_ids=[rows[5].id], now=now
    )
    same = await replace_holdings(
        db_session, user_id=user.id, device_id=device, episode_ids=[rows[5].id], now=now
    )

    assert again is True and same is False
    held = await db_session.execute(
        select(DeviceHolding.device_id, DeviceHolding.episode_id).where(
            DeviceHolding.user_id == user.id
        )
    )
    assert set(held.all()) == {(device, rows[5].id), (other_device, rows[0].id)}
