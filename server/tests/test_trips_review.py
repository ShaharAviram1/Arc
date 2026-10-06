"""Regression tests from the M19 T3 review (FR-A12).

One test (or a few) per finding, named for what must stay true: a trip never
takes a held or dormant show's own rows with it (B1); a cancelled trip's copy
is never left on the disk for ever (S1); a file landing after its trip was
cancelled is not transcoded for nobody (S2); the release never deletes a
source a queued transcode needs, and the startup sweep never transcodes a
trip-only ``failed`` episode (S3); a copy a pending trip waits for is neither
idled away nor left missing (S4); a ready episode is never ``trip_only`` (S5);
and the nits that change behaviour.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.api.anime_schemas import EpisodeOut
from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    ReviewState,
    User,
    Want,
)
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.acquisition.wants import (
    REASON_TRIP_ENDED,
    compute_wants,
    slot_view,
)
from arc.services.library.link import link
from arc.services.media.download import stat_etag
from arc.services.media.jobs import sweep_transcodes
from arc.services.media.names import OFFLINE_ENCODE, TRANSCODE, offline_path_for
from arc.services.retention.sweep import idle_copies
from arc.services.trips.cancel import cancel_trip
from arc.services.trips.create import TripNotFound, create_trip
from arc.services.trips.names import TRIP_RELEASE
from arc.services.trips.release import ReleaseDeferred, release_episode
from tests.acquisition_helpers import (
    acquisition_settings,
    fake_free_space,
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)
from tests.retention_helpers import write_source_file

pytestmark = pytest.mark.pg


def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake_free_space(monkeypatch, acquisition_rules, 90 * BYTES_PER_GB)
    return acquisition_settings(tmp_path)


async def show(session: AsyncSession, anilist_id: int) -> tuple[Anime, list[Episode]]:
    anime = await make_anime(session, anilist_id=anilist_id, status="FINISHED", episodes=12)
    return anime, await make_episodes(session, anime, 12, aired_through=12)


async def jobs_of(session: AsyncSession, job_type: str) -> list[Job]:
    rows = await session.scalars(select(Job).where(Job.type == job_type).order_by(Job.id))
    return list(rows.all())


async def ready_copy(session: AsyncSession, settings: Settings, episode_id: int) -> Path:
    path = offline_path_for(settings, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"c" * 512)
    info = os.stat(path)
    copy = await session.get(OfflineCopy, episode_id)
    if copy is None:
        copy = OfflineCopy(episode_id=episode_id)
        session.add(copy)
    copy.state = OfflineCopyState.READY
    copy.size = info.st_size
    copy.etag = stat_etag(info.st_size, info.st_mtime_ns)
    copy.codec = "h264"
    copy.ready_at = now()
    await session.flush()
    return path


async def land(
    session: AsyncSession, settings: Settings, episode: Episode, state: EpisodeState
) -> MediaFile:
    path = write_source_file(settings, episode.id)
    media = MediaFile(episode_id=episode.id, path=str(path.resolve()), size=path.stat().st_size)
    session.add(media)
    episode.state = state
    await session.flush()
    return media


# --- B1: held and dormant shows keep their own rows -------------------------


async def held_show_with_a_ready_want(
    session: AsyncSession, email: str, base: int
) -> tuple[User, Anime, list[Episode]]:
    busy, busy_rows = await show(session, base)
    held, held_rows = await show(session, base + 1)
    user = await make_user(session, email)
    await set_setting(session, "slot_cap_k", 1)
    await make_entry(session, user, busy)
    await compute_wants(session)
    busy_rows[0].state = EpisodeState.DOWNLOADING
    held_rows[0].state = EpisodeState.READY
    session.add(Want(user_id=user.id, episode_id=held_rows[0].id))
    await session.flush()
    await make_entry(session, user, held)
    await compute_wants(session)
    assert (await slot_view(session, user.id)).waiting == frozenset({held.id})
    return user, held, held_rows


async def test_a_trip_on_a_held_show_leaves_the_window_rows_flag_alone(
    db_session: AsyncSession, settings: Settings
) -> None:
    user, held, rows = await held_show_with_a_ready_want(db_session, "rev-held-a@arc.test", 991001)
    await create_trip(db_session, settings, user=user, anime_id=held.id, count=3, now=now())
    await compute_wants(db_session)

    want = await db_session.get(Want, (user.id, rows[0].id))
    assert want is not None and want.trip is False and want.dropped_at is None
    brought = await db_session.get(Want, (user.id, rows[1].id))
    assert brought is not None and brought.trip is True


async def test_cancelling_a_trip_on_a_held_show_keeps_its_window_row(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The reviewer's probe: the held show's live want on a ready episode survives."""
    user, held, rows = await held_show_with_a_ready_want(db_session, "rev-held-b@arc.test", 991003)
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=held.id, count=3, now=now())
    ).trip
    await compute_wants(db_session)

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    await compute_wants(db_session)

    left = await db_session.get(Want, (user.id, rows[0].id))
    assert left is not None and left.dropped_at is None, "the held row is left as found"
    assert rows[0].state is EpisodeState.READY
    # The rows the trip brought in are shelved, not deleted, on a held show.
    brought = await db_session.get(Want, (user.id, rows[1].id))
    assert brought is not None and brought.drop_reason == REASON_TRIP_ENDED


async def test_a_trip_on_a_dormant_show_is_shelved_when_it_ends(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991005)
    user = await make_user(db_session, "rev-dormant@arc.test")
    await make_entry(db_session, user, anime, activated=False)
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=2, now=now())
    ).trip
    await compute_wants(db_session)
    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    await compute_wants(db_session)

    for row in rows[:2]:
        want = await db_session.get(Want, (user.id, row.id))
        assert want is not None, "shelved, not deleted"
        assert want.dropped_at is not None and want.drop_reason == REASON_TRIP_ENDED


async def test_cancel_writes_no_want_rows(db_session: AsyncSession, settings: Settings) -> None:
    anime, rows = await show(db_session, 991006)
    user = await make_user(db_session, "rev-nowrite@arc.test")
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=2, now=now())
    ).trip
    before = {
        (want.episode_id, want.trip, want.dropped_at)
        for want in (await db_session.scalars(select(Want))).all()
    }

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())

    after = {
        (want.episode_id, want.trip, want.dropped_at)
        for want in (await db_session.scalars(select(Want))).all()
    }
    assert after == before
    # ...and still released the episodes nothing else wants.
    assert rows[0].state is EpisodeState.NOT_WANTED


# --- S1: a cancelled trip's copy is never orphaned --------------------------


async def test_a_copy_goes_even_when_somebody_else_holds_a_dropped_want(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991010)
    user = await make_user(db_session, "rev-orphan@arc.test")
    other = await make_user(db_session, "rev-orphan-b@arc.test")
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    ).trip
    episode = rows[0]
    episode.state = EpisodeState.NOT_WANTED  # the source was released earlier
    path = await ready_copy(db_session, settings, episode.id)
    db_session.add(
        Want(user_id=other.id, episode_id=episode.id, dropped_at=now(), drop_reason="shelved")
    )
    await db_session.flush()

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    released = await release_episode(db_session, settings, episode.id)

    assert released.outcome == "all_deleted"
    assert not path.exists()
    assert await db_session.get(OfflineCopy, episode.id) is None


async def test_a_dropped_want_still_protects_landed_source_bytes(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Only a ``not_wanted`` episode ignores dropped wants; a source keeps its grace."""
    anime, rows = await show(db_session, 991011)
    user = await make_user(db_session, "rev-grace@arc.test")
    other = await make_user(db_session, "rev-grace-b@arc.test")
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    ).trip
    media = await land(db_session, settings, rows[0], EpisodeState.MATCHED)
    db_session.add(
        Want(user_id=other.id, episode_id=rows[0].id, dropped_at=now(), drop_reason="shelved")
    )
    await db_session.flush()

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    released = await release_episode(db_session, settings, rows[0].id)

    assert released.outcome == "kept"
    assert Path(media.path).exists()


# --- S2: a file landing after its trip ended is not transcoded --------------


async def test_a_file_matched_after_its_trip_was_cancelled_is_released(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991020)
    user = await make_user(db_session, "rev-matching@arc.test")
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    ).trip
    rows[0].state = EpisodeState.MATCHING
    await db_session.flush()

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    assert [job.payload["episode_id"] for job in await jobs_of(db_session, TRIP_RELEASE)] == [
        rows[0].id
    ]
    media = MediaFile(path=str(write_source_file(settings, rows[0].id).resolve()))
    db_session.add(media)
    await db_session.flush()
    await link(
        db_session, media, anime_id=anime.id, episode_number=1, review_state=ReviewState.AUTO
    )

    assert rows[0].state is EpisodeState.MATCHED
    assert await jobs_of(db_session, TRANSCODE) == [], "no transcode for nobody"
    released = await release_episode(db_session, settings, rows[0].id)
    assert released.outcome == "all_deleted"
    assert rows[0].state is EpisodeState.NOT_WANTED


async def test_a_downloaded_episode_of_a_cancelled_trip_is_deleted(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991021)
    user = await make_user(db_session, "rev-downloaded@arc.test")
    trip = (
        await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    ).trip
    source = write_source_file(settings, rows[0].id)
    rows[0].state = EpisodeState.DOWNLOADED
    await db_session.flush()

    await cancel_trip(db_session, user=user, trip_id=trip.id, now=now())
    released = await release_episode(db_session, settings, rows[0].id)

    assert released.outcome == "all_deleted"
    assert not source.exists()


# --- S3: transcodes and the release -----------------------------------------


async def test_the_release_defers_while_a_transcode_is_queued(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991030)
    user = await make_user(db_session, "rev-transcode@arc.test")
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    await compute_wants(db_session)
    media = await land(db_session, settings, rows[0], EpisodeState.MATCHED)
    await ready_copy(db_session, settings, rows[0].id)
    db_session.add(
        Job(type=TRANSCODE, payload={"episode_id": rows[0].id}, status=JobStatus.PENDING)
    )
    await db_session.flush()

    with pytest.raises(ReleaseDeferred):
        await release_episode(db_session, settings, rows[0].id)
    assert Path(media.path).exists()


async def test_the_startup_sweep_never_transcodes_a_trip_only_failed_episode(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991031)
    user = await make_user(db_session, "rev-failed@arc.test")
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    await compute_wants(db_session)
    await land(db_session, settings, rows[0], EpisodeState.FAILED)
    for job in await jobs_of(db_session, OFFLINE_ENCODE):
        job.status = JobStatus.DONE
    await db_session.flush()

    await sweep_transcodes(db_session)

    assert await jobs_of(db_session, TRANSCODE) == []
    live = [
        job for job in await jobs_of(db_session, OFFLINE_ENCODE) if job.status is JobStatus.PENDING
    ]
    assert [job.payload["episode_id"] for job in live] == [rows[0].id]


# --- S4: a copy a pending trip waits for -------------------------------------


async def test_the_idle_rule_leaves_a_copy_a_trip_is_waiting_for(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991040)
    user = await make_user(db_session, "rev-idle@arc.test")
    await land(db_session, settings, rows[0], EpisodeState.READY)
    await ready_copy(db_session, settings, rows[0].id)
    copy = await db_session.get(OfflineCopy, rows[0].id)
    assert copy is not None
    copy.ready_at = now() - timedelta(days=10)
    await db_session.flush()
    idle = await idle_copies(db_session, settings, now=now(), idle=timedelta(days=7))
    assert [entry.episode_id for entry in idle] == [rows[0].id], "idle before the trip"

    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())

    assert await idle_copies(db_session, settings, now=now(), idle=timedelta(days=7)) == []


async def test_the_reconciler_queues_a_missing_copy_for_a_pending_trip(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991041)
    user = await make_user(db_session, "rev-requeue@arc.test")
    await land(db_session, settings, rows[0], EpisodeState.READY)
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    # The copy create queued is lost: job gone, row deleted (retention, say).
    for job in await jobs_of(db_session, OFFLINE_ENCODE):
        await db_session.delete(job)
    copy = await db_session.get(OfflineCopy, rows[0].id)
    if copy is not None:
        await db_session.delete(copy)
    await db_session.flush()

    result = await compute_wants(db_session)

    assert result.copies == 1
    assert [job.payload["episode_id"] for job in await jobs_of(db_session, OFFLINE_ENCODE)] == [
        rows[0].id
    ]
    assert (await compute_wants(db_session)).copies == 0, "queued once"


# --- S5: a ready episode is never trip_only ---------------------------------


def test_a_ready_episode_is_never_reported_trip_only() -> None:
    at = now()
    ready = Episode(id=1, anime_id=1, number=1, air_at=at, state=EpisodeState.READY)
    matched = Episode(id=2, anime_id=1, number=2, air_at=at, state=EpisodeState.MATCHED)

    assert (
        EpisodeOut.from_episode(ready, now=at, anime_status=None, trip_only=True).trip_only is False
    )
    assert EpisodeOut.from_episode(matched, now=at, anime_status=None, trip_only=True).trip_only


# --- Nits that change behaviour ---------------------------------------------


async def test_promotion_leaves_a_transcode_an_admin_cancelled(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991050)
    user = await make_user(db_session, "rev-cancelled@arc.test")
    await make_entry(db_session, user, anime, progress=0)
    await compute_wants(db_session)
    await land(db_session, settings, rows[0], EpisodeState.MATCHED)
    db_session.add(
        Job(type=TRANSCODE, payload={"episode_id": rows[0].id}, status=JobStatus.CANCELLED)
    )
    await db_session.flush()

    result = await compute_wants(db_session)

    assert result.promoted == 0
    assert [job.status for job in await jobs_of(db_session, TRANSCODE)] == [JobStatus.CANCELLED]


async def test_an_unknown_show_is_404_before_any_409(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, _ = await show(db_session, 991051)
    user = await make_user(db_session, "rev-404@arc.test")
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())

    with pytest.raises(TripNotFound):
        await create_trip(db_session, settings, user=user, anime_id=99_999_999, count=1, now=now())


async def test_a_ready_copy_row_whose_file_is_gone_does_not_stop_the_search(
    db_session: AsyncSession, settings: Settings
) -> None:
    anime, rows = await show(db_session, 991052)
    user = await make_user(db_session, "rev-gone@arc.test")
    await create_trip(db_session, settings, user=user, anime_id=anime.id, count=1, now=now())
    await compute_wants(db_session, settings=settings)
    path = await ready_copy(db_session, settings, rows[0].id)
    rows[0].state = EpisodeState.NOT_WANTED
    await db_session.flush()

    await compute_wants(db_session, settings=settings)
    assert rows[0].state is EpisodeState.NOT_WANTED, "the copy stands: not searched"

    path.unlink()
    await compute_wants(db_session, settings=settings)
    assert rows[0].state is EpisodeState.WANTED, "the file is gone: fetched again"
