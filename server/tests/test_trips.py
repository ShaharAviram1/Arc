"""Trips on the server: data and acquisition (FR-A12, M19 T3).

Four halves. :func:`create_trip` and its five refusals; the reconciler with
trip wants in it (trip-only marking, the slot cap, dormancy, the window's
skip, the D-day drop, promotion, idempotency); the trip-only episode's path
through the linker, the transcode sweep and the release job that deletes its
source once the copy is made; and :func:`cancel_trip` for each state an
episode can be in when its trip is called off. Nothing here may touch a list
entry or MyAnimeList, and the last test says so.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    MalWriteLog,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    ReviewState,
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
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    QBIT_CANCEL,
    QBIT_RESELECT,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
)
from arc.services.acquisition.qbit import QBIT_CANCELLED, QbitClient
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.acquisition.samples import request_sample
from arc.services.acquisition.wants import (
    REASON_TRIP_ENDED,
    STALE_DROP_REASON,
    compute_wants,
    slot_view,
)
from arc.services.library.link import link
from arc.services.media.download import stat_etag
from arc.services.media.jobs import sweep_transcodes
from arc.services.media.names import (
    OFFLINE_ENCODE,
    TRANSCODE,
    offline_path_for,
    output_dir_for,
    transcode_priority,
)
from arc.services.trips.cancel import cancel_trip
from arc.services.trips.create import (
    COUNT_OUT_OF_RANGE,
    NOTHING_AIRED,
    STORAGE_HELD,
    TRIP_ACTIVE,
    TripConflict,
    TripForbidden,
    TripInvalid,
    TripNotFound,
    create_trip,
    pick_episodes,
)
from arc.services.trips.deliver import confirm_delivered
from arc.services.trips.hooks import trip_copy_ready
from arc.services.trips.names import TRIP_RELEASE, TRIP_SEARCH_PRIORITY, copy_priority
from arc.services.trips.phase import PhaseFacts, trip_phase
from arc.services.trips.release import ReleaseDeferred, release_episode
from arc.services.trips.rules import needs_rendition, trip_only_episode_ids
from tests.acquisition_helpers import (
    QbitStub,
    acquisition_settings,
    fake_free_space,
    force_transport,
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)
from tests.retention_helpers import write_source_file

pytestmark = pytest.mark.pg

FINISHED = "FINISHED"


def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A throwaway data directory and a disk with plenty of room (FR-T6)."""
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    return acquisition_settings(tmp_path)


async def show(
    session: AsyncSession, anilist_id: int, *, count: int = 12, aired: int = 12
) -> tuple[Anime, list[Episode]]:
    status = FINISHED if aired >= count else "RELEASING"
    anime = await make_anime(session, anilist_id=anilist_id, status=status, episodes=count)
    return anime, await make_episodes(session, anime, count, aired_through=aired)


async def wants_of(session: AsyncSession, user: User) -> dict[int, Want]:
    rows = await session.scalars(select(Want).where(Want.user_id == user.id))
    return {want.episode_id: want for want in rows.all()}


async def jobs_of(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == job_type).order_by(Job.id))
    return list(rows.all())


async def ready_copy(session: AsyncSession, settings: Settings, episode_id: int) -> Path:
    """A copy on disk whose row matches it (size and ETag), as the encode leaves one."""
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


async def land(
    session: AsyncSession, settings: Settings, episode: Episode, *, state: EpisodeState
) -> MediaFile:
    """Put a source on disk for ``episode`` and link it, leaving it in ``state``."""
    path = write_source_file(settings, episode.id)
    media = MediaFile(episode_id=episode.id, path=str(path.resolve()), size=path.stat().st_size)
    session.add(media)
    episode.state = state
    await session.flush()
    return media


# --- create_trip ------------------------------------------------------------


async def test_a_trip_takes_the_next_aired_episodes_after_progress(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980001, aired=8)
    user = await make_user(db_session, "trip-basic@arc.test")
    await make_entry(db_session, user, anime, status=ListStatus.ON_HOLD, progress=2)

    created = await create_trip(
        db_session, settings, user=user, anime_id=anime.id, count=10, now=now()
    )

    trip = created.trip
    assert [episode.number for episode in created.episodes] == [3, 4, 5, 6, 7, 8]
    assert (trip.first_number, trip.last_number, trip.count) == (3, 8, 6), "took what aired"
    assert created.requested == 10
    assert trip.state is TripState.ACTIVE
    assert trip.deadline_at - trip.created_at == timedelta(days=14)
    rows_of_trip = (
        await db_session.scalars(select(TripEpisode).where(TripEpisode.trip_id == trip.id))
    ).all()
    assert {row.state for row in rows_of_trip} == {TripEpisodeState.PENDING}
    wants = await wants_of(db_session, user)
    assert set(wants) == {rows[n - 1].id for n in range(3, 9)}
    assert all(want.trip for want in wants.values())
    # Searches started at once, behind the window's own priority.
    searches = await jobs_of(db_session, SEARCH_RELEASE)
    assert len(searches) == 6
    assert {job.priority for job in searches} == {TRIP_SEARCH_PRIORITY}
    assert rows[2].state is EpisodeState.WANTED
    assert len(await jobs_of(db_session, COMPUTE_WANTS)) == 1


async def test_the_demo_account_cannot_make_a_trip(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, _ = await show(db_session, 980002)
    user = await make_user(db_session, "trip-demo@arc.test")
    user.is_demo = True

    with pytest.raises(TripForbidden):
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=2, now=now())


async def test_one_active_trip_per_user(db_session: AsyncSession, settings: Settings) -> None:
    first, _ = await show(db_session, 980003)
    second, _ = await show(db_session, 980004)
    user = await make_user(db_session, "trip-one@arc.test")
    await create_trip(db_session, settings, user=user, anime_id=first.id, count=2, now=now())

    with pytest.raises(TripConflict) as refused:
        await create_trip(db_session, settings, user=user, anime_id=second.id, count=2, now=now())
    assert str(refused.value) == TRIP_ACTIVE


async def test_the_database_holds_one_active_trip_per_user(db_session: AsyncSession) -> None:
    anime, _ = await show(db_session, 980005)
    user = await make_user(db_session, "trip-index@arc.test")
    at = now()

    def trip(state: TripState) -> Trip:
        return Trip(
            user_id=user.id,
            anime_id=anime.id,
            first_number=1,
            last_number=2,
            count=2,
            state=state,
            deadline_at=at,
        )

    db_session.add_all(
        [trip(TripState.CANCELLED), trip(TripState.FINISHED), trip(TripState.ACTIVE)]
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(trip(TripState.ACTIVE))
            await db_session.flush()


async def test_a_trip_is_refused_while_storage_is_held(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_free_space(monkeypatch, acquisition_rules, 1 * BYTES_PER_GB)
    anime, _ = await show(db_session, 980006)
    user = await make_user(db_session, "trip-held@arc.test")

    with pytest.raises(TripConflict) as refused:
        await create_trip(
            db_session,
            acquisition_settings(tmp_path),
            user=user,
            anime_id=anime.id,
            count=2,
            now=now(),
        )
    assert str(refused.value) == STORAGE_HELD


@pytest.mark.parametrize("count", [0, -1, 51])
async def test_a_count_outside_the_cap_is_refused(
    db_session: AsyncSession, settings: Settings, count: int
) -> None:
    anime, _ = await show(db_session, 980007 + count % 7)
    user = await make_user(db_session, f"trip-count{count}@arc.test")

    with pytest.raises(TripInvalid) as refused:
        await create_trip(
            db_session, settings, user=user, anime_id=anime.id, count=count, now=now()
        )
    assert str(refused.value) == COUNT_OUT_OF_RANGE


async def test_the_cap_is_the_admin_setting(db_session: AsyncSession, settings: Settings) -> None:
    anime, _ = await show(db_session, 980015)
    user = await make_user(db_session, "trip-cap@arc.test")
    await set_setting(db_session, "trip_max_episodes", 3)

    with pytest.raises(TripInvalid):
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=4, now=now())
    created = await create_trip(
        db_session, settings, user=user, anime_id=anime.id, count=3, now=now()
    )
    assert created.trip.count == 3


async def test_nothing_aired_after_progress_is_refused(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, _ = await show(db_session, 980016, aired=4)
    user = await make_user(db_session, "trip-caught-up@arc.test")
    await make_entry(db_session, user, anime, progress=4)

    with pytest.raises(TripInvalid) as refused:
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=3, now=now())
    assert str(refused.value) == NOTHING_AIRED


def test_pick_skips_what_a_device_already_holds() -> None:
    at = now()
    rows = [
        Episode(id=index, anime_id=1, number=index, air_at=at - timedelta(days=30))
        for index in range(1, 8)
    ]
    picked = pick_episodes(
        rows, progress=1, count=3, now=at, anime_status=FINISHED, next_airing=None, skip={3}
    )
    assert [episode.number for episode in picked] == [2, 4, 5]


# --- The reconciler ---------------------------------------------------------


async def test_a_trip_overlapping_the_window_is_trip_only_beyond_it(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Episodes in the user's window are wanted for streaming; the rest only for the trip."""
    anime, rows = await show(db_session, 980020)
    user = await make_user(db_session, "trip-overlap@arc.test")
    await make_entry(db_session, user, anime, progress=0)  # N = 2: episodes 1 and 2
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=5, now=now())

    await compute_wants(db_session)

    wants = await wants_of(db_session, user)
    assert {rows[n].id: wants[rows[n].id].trip for n in range(5)} == {
        rows[0].id: False,
        rows[1].id: False,
        rows[2].id: True,
        rows[3].id: True,
        rows[4].id: True,
    }
    assert await trip_only_episode_ids(db_session, [row.id for row in rows[:5]]) == {
        rows[2].id,
        rows[3].id,
        rows[4].id,
    }


async def test_an_overlapping_episode_gets_both_a_rendition_and_a_copy(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980021)
    user = await make_user(db_session, "trip-both@arc.test")
    await make_entry(db_session, user, anime, progress=0)
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=3, now=now())
    await compute_wants(db_session)

    media = MediaFile(path=str(write_source_file(settings, rows[0].id).resolve()))
    db_session.add(media)
    await db_session.flush()
    await link(
        db_session, media, anime_id=anime.id, episode_number=1, review_state=ReviewState.AUTO
    )

    assert [job.payload["episode_id"] for job in await jobs_of(db_session, TRANSCODE)] == [
        rows[0].id
    ]
    copies = await jobs_of(db_session, OFFLINE_ENCODE)
    assert [job.payload["episode_id"] for job in copies] == [rows[0].id]
    assert copies[0].priority == copy_priority(0)


async def test_a_trip_only_episode_is_copied_and_never_transcoded(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980022)
    user = await make_user(db_session, "trip-only@arc.test")
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=3, now=now())
    await compute_wants(db_session)
    assert not await needs_rendition(db_session, rows[1].id)

    media = MediaFile(path=str(write_source_file(settings, rows[1].id).resolve()))
    db_session.add(media)
    await db_session.flush()
    await link(
        db_session, media, anime_id=anime.id, episode_number=2, review_state=ReviewState.AUTO
    )

    assert rows[1].state is EpisodeState.MATCHED
    assert await jobs_of(db_session, TRANSCODE) == []
    copies = await jobs_of(db_session, OFFLINE_ENCODE)
    assert [(job.payload["episode_id"], job.payload["why"]) for job in copies] == [
        (rows[1].id, "trip")
    ]
    assert copies[0].priority == copy_priority(1), "second in the trip"
    # The startup sweep agrees: no transcode, and a lost copy is queued again.
    for job in copies:
        job.status = JobStatus.DONE
    await db_session.flush()
    await sweep_transcodes(db_session)
    assert await jobs_of(db_session, TRANSCODE) == []
    assert len(await jobs_of(db_session, OFFLINE_ENCODE)) == 2
    # And no HLS output ever appears.
    assert not (output_dir_for(settings, rows[1].id) / "index.m3u8").exists()


async def test_a_normal_want_on_a_trip_only_matched_episode_promotes_it(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980023)
    traveller = await make_user(db_session, "trip-promote-a@arc.test")
    viewer = await make_user(db_session, "trip-promote-b@arc.test")
    await create_trip(db_session, settings, user=traveller, anime_id=anime.id, count=3, now=now())
    await compute_wants(db_session)
    await land(db_session, settings, rows[2], state=EpisodeState.MATCHED)

    # Somebody else starts watching: episode 3 is in their window.
    await make_entry(db_session, viewer, anime, progress=2)
    result = await compute_wants(db_session)

    assert result.promoted == 1
    assert [job.payload["episode_id"] for job in await jobs_of(db_session, TRANSCODE)] == [
        rows[2].id
    ]
    assert await needs_rendition(db_session, rows[2].id)
    again = await compute_wants(db_session)
    assert again.promoted == 0, "the queued transcode is not queued twice"


async def test_trips_never_take_a_slot(db_session: AsyncSession, settings: Settings) -> None:
    """FR-A10: K = 1, one show fetching, a trip on another: nothing waits on the trip."""
    fetching, fetching_rows = await show(db_session, 980024)
    travelled, _ = await show(db_session, 980025)
    waiting, _ = await show(db_session, 980026)
    user = await make_user(db_session, "trip-slots@arc.test")
    await set_setting(db_session, "slot_cap_k", 1)
    await make_entry(db_session, user, fetching)
    await compute_wants(db_session)
    fetching_rows[0].state = EpisodeState.DOWNLOADING
    await db_session.flush()

    await create_trip(db_session, settings, user=user, anime_id=travelled.id, count=3, now=now())
    await make_entry(db_session, user, waiting)
    await compute_wants(db_session)

    view = await slot_view(db_session, user.id)
    assert view.fetching == 1, "the trip's in-flight episodes hold no slot"
    assert view.waiting == frozenset({waiting.id})
    trip_wants = [want for want in (await wants_of(db_session, user)).values() if want.trip]
    assert len(trip_wants) == 3


async def test_a_trip_on_a_held_show_stays_trip_only(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A show waiting for a slot does not get its window through a trip."""
    busy, busy_rows = await show(db_session, 980027)
    held, held_rows = await show(db_session, 980028)
    user = await make_user(db_session, "trip-heldshow@arc.test")
    await set_setting(db_session, "slot_cap_k", 1)
    await make_entry(db_session, user, busy)
    await compute_wants(db_session)
    busy_rows[0].state = EpisodeState.DOWNLOADING
    await make_entry(db_session, user, held)
    await compute_wants(db_session)
    assert (await slot_view(db_session, user.id)).waiting == frozenset({held.id})

    await create_trip(db_session, settings, user=user, anime_id=held.id, count=2, now=now())
    await compute_wants(db_session)

    wants = await wants_of(db_session, user)
    assert wants[held_rows[0].id].trip and wants[held_rows[1].id].trip
    assert (await slot_view(db_session, user.id)).waiting == frozenset({held.id})


async def test_a_trip_never_wakes_a_dormant_import(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980029)
    user = await make_user(db_session, "trip-dormant@arc.test")
    entry = await make_entry(db_session, user, anime, activated=False)
    updated = entry.updated_at

    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=2, now=now())
    await compute_wants(db_session)

    wants = await wants_of(db_session, user)
    assert set(wants) == {rows[0].id, rows[1].id}
    assert all(want.trip for want in wants.values())
    await db_session.refresh(entry)
    assert entry.activated_at is None
    assert entry.updated_at == updated


async def test_a_sample_and_a_trip_on_the_same_episode(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The sample keeps the row; the trip is not the only reason, so not trip-only."""
    anime, rows = await show(db_session, 980030)
    user = await make_user(db_session, "trip-sample@arc.test")
    await request_sample(
        db_session, user_id=user.id, anime_id=anime.id, now=now(), settings=settings
    )
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=2, now=now())
    ).trip
    await compute_wants(db_session)

    wants = await wants_of(db_session, user)
    assert wants[rows[0].id].sample and not wants[rows[0].id].trip
    assert wants[rows[1].id].trip

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    await compute_wants(db_session)
    wants = await wants_of(db_session, user)
    live = {episode_id for episode_id, want in wants.items() if want.dropped_at is None}
    assert live == {rows[0].id}, "the sample outlives the trip"
    # The row the trip brought in is shelved (no list entry), not deleted.
    assert wants[rows[1].id].drop_reason == REASON_TRIP_ENDED


async def test_the_window_keeps_delivered_episodes(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Owner, 2026-10-06: an episode on the iPad may be watched on the Mac."""
    anime, rows = await show(db_session, 980031)
    user = await make_user(db_session, "trip-delivered@arc.test")
    await make_entry(db_session, user, anime, progress=0)
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=2, now=now())
    ).trip
    await confirm_delivered(
        db_session, user=user, trip_id=trip.id, episode_id=rows[1].id, etag=None, now=now()
    )

    await compute_wants(db_session)

    wants = await wants_of(db_session, user)
    assert set(wants) == {rows[0].id, rows[1].id}, "the window's two, delivered or not"
    assert not wants[rows[1].id].trip, "wanted for streaming"


async def test_the_unwatched_drop_leaves_a_trip_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980032)
    user = await make_user(db_session, "trip-stale@arc.test")
    await make_entry(db_session, user, anime, progress=0)
    await compute_wants(db_session)
    long_ago = now() - timedelta(days=60)
    rows[0].state = EpisodeState.READY
    rows[0].state_changed_at = long_ago
    entry = await db_session.get(ListEntry, (user.id, anime.id))
    assert entry is not None
    entry.updated_at = long_ago
    await db_session.flush()

    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=2, now=now())
    first = await compute_wants(db_session)
    second = await compute_wants(db_session)

    assert first.dropped == second.dropped == 0
    assert (await wants_of(db_session, user))[rows[0].id].dropped_at is None


async def test_a_trip_revives_a_want_dropped_before_it(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980033)
    user = await make_user(db_session, "trip-revive@arc.test")
    db_session.add(
        Want(
            user_id=user.id,
            episode_id=rows[0].id,
            dropped_at=now() - timedelta(days=3),
            drop_reason=STALE_DROP_REASON,
        )
    )
    await db_session.flush()

    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    await compute_wants(db_session)

    want = (await wants_of(db_session, user))[rows[0].id]
    assert want.dropped_at is None and want.trip


async def test_the_reconciler_is_idempotent_with_trips(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, _ = await show(db_session, 980034)
    user = await make_user(db_session, "trip-idem@arc.test")
    await make_entry(db_session, user, anime, progress=1)
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=6, now=now())
    await compute_wants(db_session)
    before = {
        key: (want.trip, want.dropped_at)
        for key, want in (await wants_of(db_session, user)).items()
    }
    jobs_before = await db_session.scalar(select(func.count()).select_from(Job))

    again = await compute_wants(db_session)

    assert (again.added, again.removed, again.revived, again.shelved, again.dropped) == (
        0,
        0,
        0,
        0,
        0,
    )
    assert (again.started, again.searches, again.released, again.cancelled, again.promoted) == (
        0,
        0,
        0,
        0,
        0,
    )
    after = {
        key: (want.trip, want.dropped_at)
        for key, want in (await wants_of(db_session, user)).items()
    }
    assert after == before
    assert await db_session.scalar(select(func.count()).select_from(Job)) == jobs_before


async def test_two_trips_on_one_show_share_episodes_and_one_copy(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980035)
    first = await make_user(db_session, "trip-share-a@arc.test")
    second = await make_user(db_session, "trip-share-b@arc.test")
    await land(db_session, settings, rows[0], state=EpisodeState.READY)

    await create_trip(db_session, settings, user=first, anime_id=anime.id, count=2, now=now())
    await create_trip(db_session, settings, user=second, anime_id=anime.id, count=2, now=now())
    await compute_wants(db_session)

    assert len(await jobs_of(db_session, OFFLINE_ENCODE)) == 1, "one copy for both"
    assert len(await jobs_of(db_session, SEARCH_RELEASE)) == 1, "one search for episode 2"
    for user in (first, second):
        wants = await wants_of(db_session, user)
        assert set(wants) == {rows[0].id, rows[1].id}
        assert all(want.trip for want in wants.values())


async def test_transcode_priority_ignores_trip_wants(
    db_session: AsyncSession, settings: Settings
) -> None:
    from arc.models import DEFAULT_PRIORITY

    anime, rows = await show(db_session, 980036)
    user = await make_user(db_session, "trip-prio@arc.test")
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())

    assert await transcode_priority(db_session, rows[0].id) == DEFAULT_PRIORITY


# --- The source, once the copy is made --------------------------------------


async def trip_only_matched(
    session: AsyncSession, settings: Settings, anilist_id: int, email: str
) -> tuple[User, Trip, Episode, MediaFile]:
    anime, rows = await show(session, anilist_id)
    user = await make_user(session, email)
    trip = (
        await create_trip(session, settings, user=user, anime_id=anime.id, count=1, now=now())
    ).trip
    await compute_wants(session)
    media = await land(session, settings, rows[0], state=EpisodeState.MATCHED)
    return user, trip, rows[0], media


async def test_the_source_goes_once_the_copy_is_made(
    db_session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, trip, episode, media = await trip_only_matched(
        db_session, settings, 980040, "trip-src@arc.test"
    )
    stub = QbitStub()
    stub.add_torrent("a" * 40)
    monkeypatch.setattr(QbitClient, "__init__", force_transport(QbitClient, stub.transport()))
    db_session.add(Torrent(episode_id=episode.id, info_hash="a" * 40, qbit_state="stalledUP"))
    copy_path = await ready_copy(db_session, settings, episode.id)
    source = Path(media.path)

    await trip_copy_ready(db_session, episode)
    assert [job.payload["episode_id"] for job in await jobs_of(db_session, TRIP_RELEASE)] == [
        episode.id
    ]
    row = await db_session.get(TripEpisode, (trip.id, episode.id))
    assert row is not None and row.available_at is not None, "the 14-day clock starts"

    released = await release_episode(db_session, settings, episode.id)

    assert released.outcome == "source_deleted"
    assert not source.exists()
    assert copy_path.exists(), "the copy stays for the device"
    copy = await db_session.get(OfflineCopy, episode.id)
    assert copy is not None and copy.state is OfflineCopyState.READY
    assert episode.state is EpisodeState.NOT_WANTED
    assert await db_session.scalar(select(func.count()).select_from(MediaFile)) == 0
    assert [entry["hashes"] for entry in stub.deleted] == ["a" * 40], "removed as retention would"

    # While the copy stands, the trip want does not fetch the episode again.
    await compute_wants(db_session)
    assert episode.state is EpisodeState.NOT_WANTED
    assert (await wants_of(db_session, user))[episode.id].trip


async def test_a_missing_copy_is_fetched_again(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, _, episode, _ = await trip_only_matched(
        db_session, settings, 980041, "trip-refetch@arc.test"
    )
    await ready_copy(db_session, settings, episode.id)
    episode.state = EpisodeState.NOT_WANTED  # the source was deleted earlier
    copy = await db_session.get(OfflineCopy, episode.id)
    assert copy is not None
    copy.state = OfflineCopyState.FAILED
    await db_session.flush()

    await compute_wants(db_session)

    assert episode.state is EpisodeState.WANTED
    searches = await jobs_of(db_session, SEARCH_RELEASE)
    assert searches and searches[-1].priority == TRIP_SEARCH_PRIORITY


async def test_a_normal_want_after_the_source_went_fetches_by_the_ordinary_path(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, _, episode, _ = await trip_only_matched(
        db_session, settings, 980042, "trip-later@arc.test"
    )
    await ready_copy(db_session, settings, episode.id)
    await release_episode(db_session, settings, episode.id)
    assert episode.state is EpisodeState.NOT_WANTED

    anime = await db_session.get(Anime, episode.anime_id)
    assert anime is not None
    await make_entry(db_session, user, anime, progress=0)
    await compute_wants(db_session)

    assert not (await wants_of(db_session, user))[episode.id].trip
    assert episode.state is EpisodeState.WANTED
    assert (await jobs_of(db_session, SEARCH_RELEASE))[-1].priority == SEARCH_RELEASE_PRIORITY


async def test_the_source_stays_when_a_normal_want_arrived_first(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, _, episode, media = await trip_only_matched(
        db_session, settings, 980043, "trip-race@arc.test"
    )
    await ready_copy(db_session, settings, episode.id)
    other = await make_user(db_session, "trip-race-b@arc.test")
    db_session.add(Want(user_id=other.id, episode_id=episode.id))
    await db_session.flush()

    released = await release_episode(db_session, settings, episode.id)

    assert released.outcome == "kept"
    assert Path(media.path).exists()
    assert episode.state is EpisodeState.MATCHED


async def test_the_release_waits_for_an_encode_still_holding_the_source(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The hook queues the release before the encode job has finished: retry later."""
    _, _, episode, media = await trip_only_matched(
        db_session, settings, 980044, "trip-defer@arc.test"
    )
    await ready_copy(db_session, settings, episode.id)
    db_session.add(
        Job(
            type=OFFLINE_ENCODE,
            payload={"episode_id": episode.id, "why": "trip"},
            status=JobStatus.RUNNING,
        )
    )
    await db_session.flush()

    with pytest.raises(ReleaseDeferred):
        await release_episode(db_session, settings, episode.id)
    assert Path(media.path).exists()


# --- cancel_trip ------------------------------------------------------------


async def test_cancel_stops_a_single_mid_download(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980050)
    user = await make_user(db_session, "trip-cancel-single@arc.test")
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=2, now=now())
    ).trip
    rows[0].state = EpisodeState.DOWNLOADING
    torrent = Torrent(episode_id=rows[0].id, info_hash="b" * 40, qbit_state="downloading")
    db_session.add(torrent)
    await db_session.flush()

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())

    assert trip.state is TripState.CANCELLED and trip.ended_at is not None
    assert rows[0].state is EpisodeState.NOT_WANTED
    assert rows[1].state is EpisodeState.NOT_WANTED, "the search is released too"
    assert torrent.qbit_state == QBIT_CANCELLED
    assert [job.payload["episode_id"] for job in await jobs_of(db_session, QBIT_CANCEL)] == [
        rows[0].id
    ]
    # The trip's rows are the queued reconciliation's to end; cancel writes none.
    assert all(want.trip for want in (await wants_of(db_session, user)).values())
    await compute_wants(db_session)
    wants = await wants_of(db_session, user)
    assert all(want.drop_reason == REASON_TRIP_ENDED for want in wants.values())
    assert rows[0].state is EpisodeState.NOT_WANTED
    states = (
        await db_session.scalars(select(TripEpisode.state).where(TripEpisode.trip_id == trip.id))
    ).all()
    assert set(states) == {TripEpisodeState.CANCELLED}


async def test_cancel_gives_a_pack_file_back(db_session: AsyncSession, settings: Settings) -> None:
    anime, rows = await show(db_session, 980051)
    user = await make_user(db_session, "trip-cancel-pack@arc.test")
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    ).trip
    rows[0].state = EpisodeState.DOWNLOADING
    torrent = Torrent(
        kind=TorrentKind.BATCH, episode_id=None, info_hash="c" * 40, qbit_state="downloading"
    )
    db_session.add(torrent)
    await db_session.flush()
    claim = TorrentFile(
        torrent_id=torrent.id,
        file_index=0,
        path="pack/01.mkv",
        size=1024,
        episode_id=rows[0].id,
        wanted=True,
    )
    db_session.add(claim)
    await db_session.flush()

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())

    assert rows[0].state is EpisodeState.NOT_WANTED
    assert claim.wanted is False
    assert torrent.qbit_state == "downloading", "a pack is never marked cancelled"
    assert [job.payload["torrent_id"] for job in await jobs_of(db_session, QBIT_RESELECT)] == [
        torrent.id
    ]


async def test_cancel_deletes_landed_trip_bytes_without_the_grace(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, trip, episode, media = await trip_only_matched(
        db_session, settings, 980052, "trip-cancel-landed@arc.test"
    )
    db_session.add(
        Job(
            type=OFFLINE_ENCODE,
            payload={"episode_id": episode.id, "why": "trip"},
            status=JobStatus.PENDING,
        )
    )
    await db_session.flush()

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())

    encodes = await jobs_of(db_session, OFFLINE_ENCODE)
    assert {job.status for job in encodes} == {JobStatus.CANCELLED}, "the copy is not made"
    assert [job.payload["episode_id"] for job in await jobs_of(db_session, TRIP_RELEASE)] == [
        episode.id
    ]
    released = await release_episode(db_session, settings, episode.id)
    assert released.outcome == "all_deleted"
    assert not Path(media.path).exists()
    assert episode.state is EpisodeState.NOT_WANTED


async def test_cancel_deletes_a_copy_whose_source_is_already_gone(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, trip, episode, _ = await trip_only_matched(
        db_session, settings, 980053, "trip-cancel-copy@arc.test"
    )
    copy_path = await ready_copy(db_session, settings, episode.id)
    await release_episode(db_session, settings, episode.id)

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    released = await release_episode(db_session, settings, episode.id)

    assert released.outcome == "all_deleted"
    assert not copy_path.exists()
    assert await db_session.get(OfflineCopy, episode.id) is None


async def test_cancel_leaves_a_ready_episode_to_retention(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980054)
    user = await make_user(db_session, "trip-cancel-ready@arc.test")
    media = await land(db_session, settings, rows[0], state=EpisodeState.READY)
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    ).trip

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())

    assert rows[0].state is EpisodeState.READY
    assert Path(media.path).exists()
    assert await jobs_of(db_session, TRIP_RELEASE) == []
    released = await release_episode(db_session, settings, rows[0].id)
    assert released.outcome == "kept"


async def test_cancel_leaves_what_another_trip_still_wants(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 980055)
    first = await make_user(db_session, "trip-cancel-a@arc.test")
    second = await make_user(db_session, "trip-cancel-b@arc.test")
    trip = (
        await create_trip(db_session, settings, user=first, anime_id=anime.id, count=1, now=now())
    ).trip
    await create_trip(db_session, settings, user=second, anime_id=anime.id, count=1, now=now())

    await cancel_trip(db_session, user=first, trip_id=trip.id, now=now())

    assert rows[0].state is EpisodeState.WANTED
    assert set(await wants_of(db_session, second)) == {rows[0].id}


async def test_only_the_owner_can_cancel(db_session: AsyncSession, settings: Settings) -> None:
    anime, _ = await show(db_session, 980056)
    owner = await make_user(db_session, "trip-owner@arc.test")
    other = await make_user(db_session, "trip-other@arc.test")
    trip = (
        await create_trip(db_session, settings, user=owner, anime_id=anime.id, count=1, now=now())
    ).trip

    with pytest.raises(TripNotFound):
        await cancel_trip(db_session, user=other, trip_id=trip.id, now=now())
    assert trip.state is TripState.ACTIVE
    # Cancelling twice is a no-op, not an error.
    await cancel_trip(db_session, user=owner, trip_id=trip.id, now=now())
    await cancel_trip(db_session, user=owner, trip_id=trip.id, now=now())


# --- MAL: nothing, ever -----------------------------------------------------


async def test_a_trip_never_touches_the_list_or_myanimelist(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, _ = await show(db_session, 980060)
    user = await make_user(db_session, "trip-mal@arc.test")
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=3, now=now())
    ).trip
    await compute_wants(db_session)
    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    await compute_wants(db_session)

    assert await db_session.get(ListEntry, (user.id, anime.id)) is None
    assert await db_session.scalar(select(func.count()).select_from(MalWriteLog)) == 0
    mal_jobs = await db_session.scalars(select(Job.type).where(Job.type.like("mal%")))
    assert mal_jobs.all() == []


# --- The phase --------------------------------------------------------------


@pytest.mark.parametrize(
    ("facts", "phase"),
    [
        (PhaseFacts(EpisodeState.NOT_WANTED), "searching"),
        (PhaseFacts(EpisodeState.NOT_WANTED, held=True), "waiting_space"),
        (PhaseFacts(EpisodeState.WANTED, held=True), "searching"),
        (PhaseFacts(EpisodeState.SEARCHING), "searching"),
        (PhaseFacts(EpisodeState.DOWNLOADING), "downloading"),
        (PhaseFacts(EpisodeState.DOWNLOADED), "preparing"),
        (PhaseFacts(EpisodeState.MATCHED, has_source=True), "preparing"),
        (
            PhaseFacts(
                EpisodeState.MATCHED,
                copy_state=OfflineCopyState.PREPARING,
                has_source=True,
                encoding=True,
            ),
            "preparing",
        ),
        (
            PhaseFacts(EpisodeState.MATCHED, copy_state=OfflineCopyState.QUEUED, has_source=True),
            "unavailable",
        ),
        (PhaseFacts(EpisodeState.NOT_WANTED, copy_state=OfflineCopyState.READY), "available"),
        (PhaseFacts(EpisodeState.READY, copy_state=OfflineCopyState.READY), "available"),
        (PhaseFacts(EpisodeState.READY, has_source=True), "preparing"),
        (PhaseFacts(EpisodeState.READY), "unavailable"),
        (PhaseFacts(EpisodeState.NOT_WANTED, copy_state=OfflineCopyState.FAILED), "unavailable"),
        (
            PhaseFacts(
                EpisodeState.MATCHED,
                copy_state=OfflineCopyState.FAILED,
                has_source=True,
                encoding=True,
            ),
            "preparing",
        ),
        # A failed copy is not retried (FR-P6): with no live encode it is over.
        (
            PhaseFacts(EpisodeState.MATCHED, copy_state=OfflineCopyState.FAILED, has_source=True),
            "unavailable",
        ),
        (PhaseFacts(EpisodeState.UNAVAILABLE), "unavailable"),
        (
            PhaseFacts(EpisodeState.WANTED, row_state=TripEpisodeState.CANCELLED),
            "unavailable",
        ),
        (
            PhaseFacts(
                EpisodeState.NOT_WANTED,
                row_state=TripEpisodeState.DELIVERED,
                copy_state=OfflineCopyState.READY,
            ),
            "delivered",
        ),
    ],
)
def test_trip_phase(facts: PhaseFacts, phase: str) -> None:
    assert trip_phase(facts) == phase


# --- qBittorrent ------------------------------------------------------------


async def test_bottom_prio(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = QbitStub()
    monkeypatch.setattr(QbitClient, "__init__", force_transport(QbitClient, stub.transport()))
    async with QbitClient.from_settings(settings) as qbit:
        assert await qbit.bottom_prio("D" * 40) is True
        stub.queueing = False
        assert await qbit.bottom_prio("e" * 40) is False
    assert stub.bottomed == ["d" * 40]
