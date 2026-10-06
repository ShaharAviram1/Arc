"""Trips, the device's half: delivery, settle, expiry, the end of a trip (FR-A12, M19 T4).

On the services: :mod:`arc.services.trips.deliver` (confirm, release, again),
the ``offline_settle`` rule (:mod:`arc.services.trips.settle`), the hourly
sweep (:mod:`arc.services.trips.sweep`) and the authorisation the media route
asks (:func:`~arc.services.media.copies.may_fetch_copy`). The HTTP side is
:mod:`tests.test_trip_delivery_api`. Nothing here may touch a list entry or
MyAnimeList, and one test says so.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    MalWriteLog,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    Torrent,
    TorrentFile,
    TorrentKind,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    Want,
)
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.names import COMPUTE_WANTS, SEARCH_RELEASE
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.acquisition.wants import compute_wants
from arc.services.jobs.registry import registered_types
from arc.services.media.copies import may_fetch_copy
from arc.services.media.download import stat_etag
from arc.services.media.names import OFFLINE_ENCODE, offline_path_for
from arc.services.trips import jobs as trip_jobs  # noqa: F401  (registers the handlers)
from arc.services.trips.create import TRIP_NOT_ACTIVE, TripConflict, TripNotFound, create_trip
from arc.services.trips.deliver import ask_again, confirm_delivered, release_delivered
from arc.services.trips.hooks import stamp_available, trip_copy_ready
from arc.services.trips.names import (
    OFFLINE_SETTLE,
    TRIP_RELEASE,
    TRIP_SETTLE_DELAY,
    TRIP_SWEEP,
)
from arc.services.trips.release import release_episode
from arc.services.trips.rules import needs_rendition
from arc.services.trips.settle import settle_due, settle_episode
from arc.services.trips.sweep import ending_state, expiry_due, sweep_trips
from arc.services.trips.view import trip_facts
from tests.acquisition_helpers import (
    acquisition_settings,
    fake_free_space,
    make_anime,
    make_entry,
    make_episodes,
    make_user,
)
from tests.retention_helpers import write_source_file

pytestmark = pytest.mark.pg

FINISHED = "FINISHED"


def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    return acquisition_settings(tmp_path)


async def show(session: AsyncSession, anilist_id: int, count: int = 6) -> list[Episode]:
    anime = await make_anime(session, anilist_id=anilist_id, status=FINISHED, episodes=count)
    return await make_episodes(session, anime, count, aired_through=count)


async def ready_copy(session: AsyncSession, settings: Settings, episode_id: int) -> Path:
    """A copy on disk whose row matches it, as the encode leaves one."""
    path = offline_path_for(settings, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"c" * 4096)
    info = os.stat(path)
    session.add(
        OfflineCopy(
            episode_id=episode_id,
            state=OfflineCopyState.READY,
            size=info.st_size,
            etag=stat_etag(info.st_size, info.st_mtime_ns),
            codec="h264",
            ready_at=now(),
        )
    )
    await session.flush()
    return path


async def trip_with_copies(
    session: AsyncSession,
    settings: Settings,
    episodes: list[Episode],
    email: str,
    *,
    count: int = 1,
    at: datetime | None = None,
) -> tuple[User, Trip]:
    """A user's trip whose first ``count`` episodes are trip-only with a ready copy.

    The state T3's ``trip_release`` leaves them in: the source deleted, the
    episode ``not_wanted``, the copy waiting, ``available_at`` stamped — at
    ``at`` when given (the trip itself is made now: "aired" is real time).
    """
    user = await make_user(session, email)
    trip = (
        await create_trip(
            session,
            settings,
            user=user,
            anime_id=episodes[0].anime_id,
            count=count,
            now=now(),
        )
    ).trip
    for episode in episodes[:count]:
        episode.state = EpisodeState.NOT_WANTED
        if await session.get(OfflineCopy, episode.id) is None:
            await ready_copy(session, settings, episode.id)
        await stamp_available(session, episode.id, now=at)
    await session.flush()
    return user, trip


async def row_of(session: AsyncSession, trip: Trip, episode: Episode) -> TripEpisode:
    row = await session.get(TripEpisode, (trip.id, episode.id), populate_existing=True)
    assert row is not None
    return row


async def jobs_of(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(
        select(Job).where(Job.type == job_type, Job.status == JobStatus.PENDING).order_by(Job.id)
    )
    return list(rows.all())


# --- Pure rules -------------------------------------------------------------


def test_expiry_is_counted_from_the_copy_else_from_the_deadline() -> None:
    at = now()
    deadline = at + timedelta(days=3)
    # 14 days from available_at, to the second.
    assert not expiry_due(
        available_at=at - timedelta(days=14) + timedelta(seconds=1),
        deadline_at=deadline,
        copy_days=14,
        now=at,
    )
    assert expiry_due(
        available_at=at - timedelta(days=14), deadline_at=deadline, copy_days=14, now=at
    )
    # A copy that came late still gets its full 14 days past the deadline.
    assert not expiry_due(
        available_at=at - timedelta(days=1),
        deadline_at=at - timedelta(days=5),
        copy_days=14,
        now=at,
    )
    # No copy: the deadline decides.
    assert not expiry_due(available_at=None, deadline_at=deadline, copy_days=14, now=at)
    assert expiry_due(available_at=None, deadline_at=at, copy_days=14, now=at)


def test_a_trip_ends_finished_if_anything_reached_a_device() -> None:
    assert ending_state(any_delivered=True) is TripState.FINISHED
    assert ending_state(any_delivered=False) is TripState.EXPIRED


def test_settle_waits_an_hour_from_the_latest_confirmation() -> None:
    at = now()
    assert settle_due(None, now=at) is None
    assert settle_due(at - TRIP_SETTLE_DELAY, now=at) is None
    assert settle_due(at - timedelta(minutes=10), now=at) == at + timedelta(minutes=50)


def test_the_job_types_are_registered() -> None:
    assert {OFFLINE_SETTLE, TRIP_SWEEP, TRIP_RELEASE} <= registered_types()


# --- Delivery and settle ----------------------------------------------------


async def test_a_confirmed_copy_is_deleted_after_the_delay(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985001)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-basic@arc.test")
    path = offline_path_for(settings, episode.id)
    copy = await db_session.get(OfflineCopy, episode.id)
    assert copy is not None
    at = now()

    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=copy.etag, now=at
    )

    row = await row_of(db_session, trip, episode)
    assert row.state is TripEpisodeState.DELIVERED and row.delivered_at == at
    settles = await jobs_of(db_session, OFFLINE_SETTLE)
    assert [job.payload["episode_id"] for job in settles] == [episode.id]
    assert settles[0].run_after is not None
    assert settles[0].run_after - at == TRIP_SETTLE_DELAY
    assert await jobs_of(db_session, COMPUTE_WANTS), "the trip want ends promptly"

    # Too early: a second device still has its hour.
    early = await settle_episode(db_session, settings, episode.id, now=at + timedelta(minutes=30))
    assert early.outcome == "deferred" and early.retry_at == at + TRIP_SETTLE_DELAY
    assert path.exists()

    settled = await settle_episode(
        db_session, settings, episode.id, now=at + TRIP_SETTLE_DELAY + timedelta(seconds=1)
    )

    assert settled.outcome == "copy_deleted"
    assert not path.exists()
    assert await db_session.get(OfflineCopy, episode.id, populate_existing=True) is None
    await db_session.refresh(episode)
    assert episode.state is EpisodeState.NOT_WANTED
    row = await row_of(db_session, trip, episode)
    assert row.state is TripEpisodeState.DELIVERED, "kept as history"
    assert row.delivered_at == at

    # Repeats change nothing.
    again = await settle_episode(db_session, settings, episode.id, now=at + TRIP_SETTLE_DELAY * 2)
    assert again.outcome == "settled"


async def test_repeat_confirms_are_no_ops(db_session: AsyncSession, settings: Settings) -> None:
    rows = await show(db_session, 985002)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-twice@arc.test")
    first = now()
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=first
    )
    jobs_before = await db_session.scalar(select(func.count()).select_from(Job))

    await confirm_delivered(
        db_session,
        user=user,
        trip_id=trip.id,
        episode_id=episode.id,
        etag="stale-etag",  # accepted, only logged
        now=first + timedelta(minutes=5),
    )

    row = await row_of(db_session, trip, episode)
    assert row.delivered_at == first, "first time only"
    assert await db_session.scalar(select(func.count()).select_from(Job)) == jobs_before


async def test_another_users_trip_is_not_found(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985003)
    _, trip = await trip_with_copies(db_session, settings, rows, "deliver-owner@arc.test")
    stranger = await make_user(db_session, "deliver-stranger@arc.test")

    with pytest.raises(TripNotFound):
        await confirm_delivered(
            db_session, user=stranger, trip_id=trip.id, episode_id=rows[0].id, etag=None, now=now()
        )
    with pytest.raises(TripNotFound):
        await release_delivered(
            db_session, user=stranger, trip_id=trip.id, episode_id=rows[0].id, now=now()
        )
    owner = await db_session.get(User, trip.user_id)
    assert owner is not None
    with pytest.raises(TripNotFound):  # an episode the trip does not hold
        await confirm_delivered(
            db_session, user=owner, trip_id=trip.id, episode_id=rows[4].id, etag=None, now=now()
        )


async def test_settle_leaves_a_ready_episodes_copy(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985004)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-ready@arc.test")
    episode.state = EpisodeState.READY
    await db_session.flush()
    at = now()
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=at
    )

    settled = await settle_episode(db_session, settings, episode.id, now=at + timedelta(hours=2))

    assert settled.outcome == "kept"
    assert offline_path_for(settings, episode.id).exists()
    assert await db_session.get(OfflineCopy, episode.id) is not None


async def test_settle_does_not_touch_a_pack(db_session: AsyncSession, settings: Settings) -> None:
    rows = await show(db_session, 985005)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-pack@arc.test")
    # T3 gave the file back already: the claim is un-wanted, the pack stays.
    torrent = Torrent(kind=TorrentKind.BATCH, episode_id=None, info_hash="d" * 40)
    db_session.add(torrent)
    await db_session.flush()
    claim = TorrentFile(
        torrent_id=torrent.id,
        file_index=0,
        path="pack/01.mkv",
        size=1024,
        episode_id=episode.id,
        wanted=False,
    )
    db_session.add(claim)
    at = now()
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=at
    )

    settled = await settle_episode(db_session, settings, episode.id, now=at + timedelta(hours=2))

    assert settled.outcome == "copy_deleted"
    assert await db_session.get(Torrent, torrent.id, populate_existing=True) is not None
    kept = await db_session.get(TorrentFile, claim.id, populate_existing=True)
    assert kept is not None and kept.episode_id == episode.id and kept.wanted is False
    assert await jobs_of(db_session, TRIP_RELEASE) == [], "nothing landed to hand on"


async def test_two_users_deletion_waits_for_both(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985006)
    episode = rows[0]
    first, first_trip = await trip_with_copies(db_session, settings, rows, "deliver-a@arc.test")
    second, second_trip = await trip_with_copies(db_session, settings, rows, "deliver-b@arc.test")
    at = now()
    await confirm_delivered(
        db_session, user=first, trip_id=first_trip.id, episode_id=episode.id, etag=None, now=at
    )

    waiting = await settle_episode(db_session, settings, episode.id, now=at + timedelta(hours=2))
    assert waiting.outcome == "kept", "the second trip is still pending"
    assert offline_path_for(settings, episode.id).exists()

    later = at + timedelta(hours=3)
    await confirm_delivered(
        db_session, user=second, trip_id=second_trip.id, episode_id=episode.id, etag=None, now=later
    )
    # The first confirmation's settle running now sees the second's hour.
    deferred = await settle_episode(
        db_session, settings, episode.id, now=later + timedelta(minutes=1)
    )
    assert deferred.outcome == "deferred" and deferred.retry_at == later + TRIP_SETTLE_DELAY

    done = await settle_episode(db_session, settings, episode.id, now=later + timedelta(hours=1))
    assert done.outcome == "copy_deleted"
    assert not offline_path_for(settings, episode.id).exists()


async def test_settle_defers_while_an_encode_is_live(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985007)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-enc@arc.test")
    at = now()
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=at
    )
    db_session.add(Job(type=OFFLINE_ENCODE, payload={"episode_id": episode.id}))
    await db_session.flush()

    settled = await settle_episode(db_session, settings, episode.id, now=at + timedelta(hours=2))

    assert settled.outcome == "deferred" and settled.retry_at is not None
    assert offline_path_for(settings, episode.id).exists()


async def test_release_keeps_a_copy_confirmed_within_the_hour(
    db_session: AsyncSession, settings: Settings
) -> None:
    """T3's release job must not cut a second device's hour short."""
    rows = await show(db_session, 985008)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-rel@arc.test")
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=now()
    )
    await compute_wants(db_session)  # the trip want ends

    released = await release_episode(db_session, settings, episode.id)

    assert released.outcome == "kept"
    assert offline_path_for(settings, episode.id).exists()


async def test_a_delivered_episode_stays_in_the_window(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Owner, 2026-10-06: on the iPad is not watched; the Mac still streams it."""
    rows = await show(db_session, 985009)
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-win@arc.test", count=2)
    anime = await db_session.get(Anime, rows[0].anime_id)
    assert anime is not None
    await make_entry(db_session, user, anime, progress=0)
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[0].id, etag=None, now=now()
    )

    await compute_wants(db_session)

    live = {
        want.episode_id: want
        for want in (
            await db_session.scalars(
                select(Want).where(Want.user_id == user.id, Want.dropped_at.is_(None))
            )
        ).all()
    }
    assert rows[0].id in live and not live[rows[0].id].trip, "wanted for streaming"
    await db_session.refresh(rows[0])
    assert rows[0].state is EpisodeState.WANTED, "fetched again by the ordinary path"
    # Releasing the device copy is trip bookkeeping only.
    await release_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[0].id, now=now()
    )
    assert (await row_of(db_session, trip, rows[0])).released_at is not None


async def test_an_episode_in_a_window_and_a_trip_keeps_its_source_and_rendition(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Not trip-only, so the copy hook's release never touches its source."""
    rows = await show(db_session, 985029)
    episode = rows[0]
    user = await make_user(db_session, "both-ways@arc.test")
    anime = await db_session.get(Anime, episode.anime_id)
    assert anime is not None
    await make_entry(db_session, user, anime, progress=0)
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, episode.id))
    assert want is not None and not want.trip
    assert await needs_rendition(db_session, episode.id), "gets the HLS rendition"
    source = write_source_file(settings, episode.id)
    db_session.add(
        MediaFile(episode_id=episode.id, path=str(source.resolve()), size=source.stat().st_size)
    )
    episode.state = EpisodeState.READY
    await ready_copy(db_session, settings, episode.id)

    await trip_copy_ready(db_session, episode)
    assert await jobs_of(db_session, TRIP_RELEASE) == [], "a ready episode queues no release"
    released = await release_episode(db_session, settings, episode.id)

    assert released.outcome == "kept"
    assert source.exists()
    assert episode.state is EpisodeState.READY


# --- Again ------------------------------------------------------------------


async def test_again_re_pends_and_re_acquires(db_session: AsyncSession, settings: Settings) -> None:
    rows = await show(db_session, 985010)
    episode = rows[0]
    user, trip = await trip_with_copies(
        db_session, settings, rows, "deliver-again@arc.test", count=2
    )
    at = now()
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=at
    )
    await settle_episode(db_session, settings, episode.id, now=at + timedelta(hours=2))
    await compute_wants(db_session)
    assert await db_session.get(OfflineCopy, episode.id, populate_existing=True) is None

    await ask_again(
        db_session, settings, user=user, trip_id=trip.id, episode_id=episode.id, now=now()
    )

    row = await row_of(db_session, trip, episode)
    assert row.state is TripEpisodeState.PENDING
    assert (row.delivered_at, row.released_at, row.available_at) == (None, None, None)
    await compute_wants(db_session)
    want = await db_session.get(Want, (user.id, episode.id), populate_existing=True)
    assert want is not None and want.dropped_at is None and want.trip
    await db_session.refresh(episode)
    assert episode.state is EpisodeState.WANTED, "fetched again by the ordinary path"
    assert await jobs_of(db_session, SEARCH_RELEASE)
    facts = await trip_facts(db_session, settings, trip)
    assert facts.episodes[0].phase == "searching" and facts.episodes[0].delivered is False


async def test_again_with_the_copy_still_there_restarts_the_clock(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985011)
    episode = rows[0]
    long_ago = now() - timedelta(days=20)
    user, trip = await trip_with_copies(
        db_session, settings, rows, "deliver-again2@arc.test", at=long_ago
    )
    await sweep_trips(db_session, now=now())
    assert (await row_of(db_session, trip, episode)).state is TripEpisodeState.EXPIRED
    await db_session.refresh(trip)
    trip.state = TripState.ACTIVE  # still running for the sake of this test
    trip.ended_at = None
    await db_session.flush()
    at = now()

    await ask_again(db_session, settings, user=user, trip_id=trip.id, episode_id=episode.id, now=at)

    row = await row_of(db_session, trip, episode)
    assert row.state is TripEpisodeState.PENDING and row.available_at == at
    facts = await trip_facts(db_session, settings, trip)
    assert facts.episodes[0].phase == "available"


async def test_again_after_the_trip_ended_is_a_conflict(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985012)
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-again3@arc.test")
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[0].id, etag=None, now=now()
    )
    await sweep_trips(db_session, now=now())
    await db_session.refresh(trip)
    assert trip.state is TripState.FINISHED

    with pytest.raises(TripConflict) as refused:
        await ask_again(
            db_session, settings, user=user, trip_id=trip.id, episode_id=rows[0].id, now=now()
        )
    assert str(refused.value) == TRIP_NOT_ACTIVE


# --- The sweep --------------------------------------------------------------


async def test_a_copy_expires_fourteen_days_after_it_was_made(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985013)
    episode = rows[0]
    made = now() - timedelta(days=10)
    _, trip = await trip_with_copies(db_session, settings, rows, "expire@arc.test", at=made)

    nothing = await sweep_trips(db_session, now=made + timedelta(days=13, hours=23))
    assert nothing.expired_rows == [] and nothing.ended == {}

    swept = await sweep_trips(db_session, now=made + timedelta(days=14))

    assert swept.expired_rows == [(trip.id, episode.id)]
    assert (await row_of(db_session, trip, episode)).state is TripEpisodeState.EXPIRED
    assert swept.ended == {trip.id: TripState.EXPIRED}, "nothing reached a device"
    await db_session.refresh(trip)
    assert trip.state is TripState.EXPIRED and trip.ended_at is not None
    settles = await jobs_of(db_session, OFFLINE_SETTLE)
    assert [job.payload["episode_id"] for job in settles] == [episode.id]
    assert settles[0].run_after is None or settles[0].run_after <= now()
    assert await jobs_of(db_session, COMPUTE_WANTS)

    settled = await settle_episode(db_session, settings, episode.id, now=now())
    assert settled.outcome == "copy_deleted"

    # Repeats are no-ops.
    again = await sweep_trips(db_session, now=made + timedelta(days=15))
    assert again.expired_rows == [] and again.ended == {} and again.settles == []


async def test_a_trip_whose_copies_never_came_expires_at_its_deadline(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985014)
    user = await make_user(db_session, "expire-none@arc.test")
    trip = (
        await create_trip(
            db_session, settings, user=user, anime_id=rows[0].anime_id, count=2, now=now()
        )
    ).trip

    assert (await sweep_trips(db_session, now=trip.deadline_at - timedelta(minutes=1))).ended == {}
    swept = await sweep_trips(db_session, now=trip.deadline_at)

    assert sorted(swept.expired_rows) == [(trip.id, rows[0].id), (trip.id, rows[1].id)]
    assert swept.ended == {trip.id: TripState.EXPIRED}
    await compute_wants(db_session)
    live = (
        await db_session.scalars(
            select(Want).where(Want.user_id == user.id, Want.dropped_at.is_(None))
        )
    ).all()
    assert live == [], "the trip wants end with it"


async def test_a_trip_finishes_at_its_last_confirmation(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985015)
    user, trip = await trip_with_copies(db_session, settings, rows, "finish@arc.test", count=2)
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[0].id, etag=None, now=now()
    )
    await db_session.refresh(trip)
    assert trip.state is TripState.ACTIVE, "one still pending"

    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[1].id, etag=None, now=now()
    )

    await db_session.refresh(trip)
    assert trip.state is TripState.FINISHED and trip.ended_at is not None
    states = (
        await db_session.scalars(select(TripEpisode.state).where(TripEpisode.trip_id == trip.id))
    ).all()
    assert set(states) == {TripEpisodeState.DELIVERED}
    assert (await sweep_trips(db_session, now=now())).ended == {}, "nothing left for the sweep"
    # The next trip may start at once.
    anime = await db_session.get(Anime, rows[0].anime_id)
    assert anime is not None
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())


async def test_the_sweep_finishes_a_trip_when_confirmations_do_not(
    db_session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hourly sweep is the safety net (the switch off, or a row expired)."""
    from arc.services.trips import deliver

    monkeypatch.setattr(deliver, "FINISH_ON_LAST_CONFIRM", False)
    rows = await show(db_session, 985030)
    user, trip = await trip_with_copies(db_session, settings, rows, "finish-sweep@arc.test")
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[0].id, etag=None, now=now()
    )

    swept = await sweep_trips(db_session, now=now())

    assert swept.ended == {trip.id: TripState.FINISHED}


async def test_a_partly_delivered_trip_stays_active(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985016)
    user, trip = await trip_with_copies(db_session, settings, rows, "partial@arc.test", count=2)
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[0].id, etag=None, now=now()
    )

    swept = await sweep_trips(db_session, now=now())

    assert swept.ended == {}
    await db_session.refresh(trip)
    assert trip.state is TripState.ACTIVE


async def test_the_sweep_settles_a_leftover_copy(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A lost settle: delivered long ago, the copy still on disk."""
    rows = await show(db_session, 985017)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "leftover@arc.test")
    await confirm_delivered(
        db_session,
        user=user,
        trip_id=trip.id,
        episode_id=episode.id,
        etag=None,
        now=now() - timedelta(days=2),
    )
    await db_session.execute(delete(Job).where(Job.type == OFFLINE_SETTLE))  # the job was lost

    swept = await sweep_trips(db_session, now=now())

    assert swept.settles == [episode.id]
    assert [job.payload["episode_id"] for job in await jobs_of(db_session, OFFLINE_SETTLE)] == [
        episode.id
    ]


# --- Authorisation ----------------------------------------------------------


async def test_who_may_fetch_a_trip_copy(db_session: AsyncSession, settings: Settings) -> None:
    rows = await show(db_session, 985018)
    episode = rows[0]
    owner, trip = await trip_with_copies(db_session, settings, rows, "fetch-owner@arc.test")
    other = await make_user(db_session, "fetch-other@arc.test")
    demo = await make_user(db_session, "fetch-demo@arc.test")
    demo.is_demo = True
    copy = await db_session.get(OfflineCopy, episode.id)
    assert copy is not None

    assert await may_fetch_copy(db_session, owner, episode, copy), "owner, pending"
    assert not await may_fetch_copy(db_session, other, episode, copy), "another user"
    assert not await may_fetch_copy(db_session, demo, episode, copy), "demo"

    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=episode.id, etag=None, now=now()
    )
    assert await may_fetch_copy(db_session, owner, episode, copy), "owner, delivered"

    row = await row_of(db_session, trip, episode)
    row.state = TripEpisodeState.EXPIRED
    await db_session.flush()
    assert not await may_fetch_copy(db_session, owner, episode, copy), "owner, expired"

    episode.state = EpisodeState.READY
    await db_session.flush()
    assert await may_fetch_copy(db_session, other, episode, copy), "a ready episode: anyone"
    assert not await may_fetch_copy(db_session, demo, episode, copy), "never demo"


# --- MyAnimeList ------------------------------------------------------------


async def test_nothing_here_writes_the_list_or_mal(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985019)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "deliver-mal@arc.test", count=2)
    anime = await db_session.get(Anime, episode.anime_id)
    assert anime is not None
    entry = await make_entry(db_session, user, anime, progress=0)
    before = (entry.status, entry.progress, entry.updated_at)
    at = now()

    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=at
    )
    await settle_episode(db_session, settings, episode.id, now=at + timedelta(hours=2))
    await release_delivered(db_session, user=user, trip_id=trip.id, episode_id=episode.id, now=at)
    await ask_again(db_session, settings, user=user, trip_id=trip.id, episode_id=episode.id, now=at)
    await sweep_trips(db_session, now=at + timedelta(days=30))

    refreshed = await db_session.get(ListEntry, (user.id, anime.id), populate_existing=True)
    assert refreshed is not None
    assert (refreshed.status, refreshed.progress, refreshed.updated_at) == before
    assert await db_session.scalar(select(func.count()).select_from(MalWriteLog)) == 0
    mal_jobs = await db_session.scalars(select(Job.type).where(Job.type.like("mal%")))
    assert mal_jobs.all() == []


# --- Fix loop (review of M19 T4) --------------------------------------------


async def test_a_confirmation_on_a_cancelled_row_restores_no_rights(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985020)
    episode = rows[0]
    owner, trip = await trip_with_copies(db_session, settings, rows, "late-cancel@arc.test")
    row = await row_of(db_session, trip, episode)
    row.state = TripEpisodeState.CANCELLED
    trip.state = TripState.CANCELLED
    trip.ended_at = now()
    await db_session.flush()

    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=episode.id, etag=None, now=now()
    )

    row = await row_of(db_session, trip, episode)
    assert row.state is TripEpisodeState.CANCELLED, "state kept"
    assert row.delivered_at is not None, "the window skip still reads it"
    copy = await db_session.get(OfflineCopy, episode.id)
    assert copy is not None
    assert not await may_fetch_copy(db_session, owner, episode, copy)


async def test_a_confirmation_on_an_expired_row(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985021)
    owner, trip = await trip_with_copies(
        db_session, settings, rows, "late-expire@arc.test", count=2
    )
    for episode in rows[:2]:
        (await row_of(db_session, trip, episode)).state = TripEpisodeState.EXPIRED
    await db_session.flush()
    copy = await db_session.get(OfflineCopy, rows[0].id)
    assert copy is not None

    # The trip is still active: the device finished as the clock ran out.
    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=rows[0].id, etag=None, now=now()
    )
    assert (await row_of(db_session, trip, rows[0])).state is TripEpisodeState.DELIVERED

    # The trip has ended: the stamp only.
    trip.state = TripState.EXPIRED
    trip.ended_at = now()
    await db_session.flush()
    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=rows[1].id, etag=None, now=now()
    )
    row = await row_of(db_session, trip, rows[1])
    assert row.state is TripEpisodeState.EXPIRED and row.delivered_at is not None
    second = await db_session.get(OfflineCopy, rows[1].id)
    assert second is not None
    assert not await may_fetch_copy(db_session, owner, rows[1], second)


async def test_a_stamped_cancelled_row_counts_as_delivered_for_the_ending_and_settle(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985022)
    episode = rows[0]
    owner, trip = await trip_with_copies(db_session, settings, rows, "late-latest@arc.test")
    (await row_of(db_session, trip, episode)).state = TripEpisodeState.EXPIRED
    trip.state = TripState.EXPIRED
    await db_session.flush()
    at = now()
    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=episode.id, etag=None, now=at
    )

    early = await settle_episode(db_session, settings, episode.id, now=at + timedelta(minutes=5))

    assert early.outcome == "deferred", "latest_delivery reads delivered_at, not the state"


async def test_a_re_confirmation_after_a_release_restarts_the_hour(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985023)
    episode = rows[0]
    owner, trip = await trip_with_copies(db_session, settings, rows, "reconfirm@arc.test")
    first = now() - timedelta(hours=3)
    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=episode.id, etag=None, now=first
    )
    await release_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=episode.id, now=first
    )
    again = now()

    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=episode.id, etag=None, now=again
    )

    row = await row_of(db_session, trip, episode)
    assert row.delivered_at == again and row.released_at is None


async def test_a_confirmation_cancels_a_queued_re_make_nobody_needs(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985024)
    episode = rows[0]
    owner, trip = await trip_with_copies(db_session, settings, rows, "drop-encode@arc.test")
    queued = Job(type=OFFLINE_ENCODE, payload={"episode_id": episode.id, "why": "trip"})
    db_session.add(queued)
    await db_session.flush()

    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=episode.id, etag=None, now=now()
    )

    await db_session.refresh(queued)
    assert queued.status is JobStatus.CANCELLED


async def test_a_confirmation_keeps_an_encode_another_trip_waits_for(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985025)
    episode = rows[0]
    owner, trip = await trip_with_copies(db_session, settings, rows, "keep-enc-a@arc.test")
    await trip_with_copies(db_session, settings, rows, "keep-enc-b@arc.test")
    queued = Job(type=OFFLINE_ENCODE, payload={"episode_id": episode.id, "why": "trip"})
    db_session.add(queued)
    await db_session.flush()

    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=episode.id, etag=None, now=now()
    )

    await db_session.refresh(queued)
    assert queued.status is JobStatus.PENDING


async def test_a_deleted_users_trip_copy_is_settled_by_the_sweep(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985026)
    episode = rows[0]
    owner, _ = await trip_with_copies(db_session, settings, rows, "gone-user@arc.test")
    await db_session.execute(delete(User).where(User.id == owner.id))
    db_session.expunge_all()
    assert await db_session.scalar(select(func.count()).select_from(TripEpisode)) == 0

    swept = await sweep_trips(db_session, now=now() + timedelta(days=30))

    assert episode.id in swept.settles
    settled = await settle_episode(db_session, settings, episode.id, now=now())
    assert settled.outcome == "copy_deleted"
    assert not offline_path_for(settings, episode.id).exists()


async def test_the_sweep_leaves_a_preparing_episodes_copy(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985027)
    await ready_copy(db_session, settings, rows[0].id)
    rows[0].state = EpisodeState.PREPARING
    await db_session.flush()

    swept = await sweep_trips(db_session, now=now())

    assert rows[0].id not in swept.settles
    assert (await settle_episode(db_session, settings, rows[0].id, now=now())).outcome == "kept"


# --- The owner's switches (on) ----------------------------------------------


def test_fetch_hold_is_on_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.services.trips import settle as settle_module

    at = now()
    served = at - timedelta(minutes=2)
    assert settle_module.fetch_hold(served, at - timedelta(hours=2), now=at) == served + timedelta(
        minutes=10
    )
    assert settle_module.fetch_hold(at - timedelta(minutes=11), at, now=at) is None, "quiet"
    assert settle_module.fetch_hold(served, at - timedelta(hours=6), now=at) is None, "ceiling"
    assert settle_module.fetch_hold(None, at, now=at) is None, "never served"

    monkeypatch.setattr(settle_module, "SETTLE_WAITS_FOR_FETCH", False)
    assert settle_module.fetch_hold(served, at - timedelta(hours=2), now=at) is None


async def test_settle_waits_for_a_download_still_running(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985031)
    episode = rows[0]
    user, trip = await trip_with_copies(db_session, settings, rows, "fetching@arc.test", count=2)
    at = now()
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=episode.id, etag=None, now=at
    )
    later = at + timedelta(hours=2)
    copy = await db_session.get(OfflineCopy, episode.id)
    assert copy is not None
    copy.last_served_at = later - timedelta(minutes=3)  # a second device, mid-download
    await db_session.flush()

    held = await settle_episode(db_session, settings, episode.id, now=later)

    assert held.outcome == "deferred"
    assert held.retry_at == later + timedelta(minutes=7)
    assert offline_path_for(settings, episode.id).exists()
    done = await settle_episode(db_session, settings, episode.id, now=later + timedelta(minutes=8))
    assert done.outcome == "copy_deleted"


def test_the_trip_touch_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.services.media import copies

    trip_only = Episode(id=1, anime_id=1, number=1, state=EpisodeState.NOT_WANTED)
    ready = Episode(id=2, anime_id=1, number=2, state=EpisodeState.READY)
    assert copies.touch_interval(trip_only) == 300.0
    assert copies.touch_interval(ready) == copies.SERVED_TOUCH_SECONDS

    monkeypatch.setattr(copies, "TRIP_TOUCH_ENABLED", False)
    assert copies.touch_interval(trip_only) == copies.SERVED_TOUCH_SECONDS


async def test_finishing_on_the_last_confirmation_is_on(
    db_session: AsyncSession, settings: Settings
) -> None:
    rows = await show(db_session, 985028)
    owner, trip = await trip_with_copies(db_session, settings, rows, "finish-now@arc.test")

    await confirm_delivered(
        db_session, user=owner, trip_id=trip.id, episode_id=rows[0].id, etag=None, now=now()
    )

    await db_session.refresh(trip)
    assert trip.state is TripState.FINISHED and trip.ended_at is not None
