"""The small offline copy after review (FR-P6, M19 T1 fix loop).

One test group per finding: dead encodes read ``failed`` (S1); validation
measures the chosen tracks and refusals are not retried (S2); the request queue
is bounded and respects the storage guard (S3); concurrent requests share one
row (S4); retention and a request cannot leave a copy on an episode that is
gone (S7); and the nits — start-up sweep (N1), the rendition's own languages
(N2), no touch on a refused range (N3), no copy for the demo account (N4), a
cancelled encode (N9).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    Rendition,
    ReviewState,
    User,
)
from arc.services.jobs.registry import JobContext
from arc.services.media import offline as offline_module
from arc.services.media.copies import (
    MAX_HOST_REQUEST_COPIES,
    MAX_USER_REQUEST_COPIES,
    offline_options,
    offline_state,
    request_copy,
    settings_key,
)
from arc.services.media.names import OFFLINE_ENCODE, offline_path_for
from arc.services.media.offline import (
    NOT_READY_MESSAGE,
    offline_encode,
    sweep_offline_leftovers,
)
from arc.services.media.plan import build_plan, stream_duration
from arc.services.media.transcode import reset_semaphore
from arc.services.retention.delete import delete_episode_files
from arc.services.retention.sweep import targets_for_episode
from tests.acquisition_helpers import acquisition_settings, make_anime, make_episodes
from tests.conftest import add_user, api_transport, login
from tests.media_helpers import SOURCE_PROBE, install_fake_ffmpeg
from tests.test_media_stream import add_episode, write_rendition
from tests.test_offline_copy_routes import add_source, write_copy

pytestmark = pytest.mark.pg

USER_EMAIL = "reviewed@arc.test"
USER_PASSWORD = "reviewed-password"


@pytest.fixture(autouse=True)
def _fresh_semaphore() -> None:
    reset_semaphore()


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        env="test", database_url=test_database_url, data_dir=tmp_path, _env_file=None
    )


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


def context(session: AsyncSession, settings: Settings, job: Job) -> JobContext:
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("arc.jobs.test")
    )


async def ready_with_source(
    session: AsyncSession, settings: Settings, *, anilist_id: int
) -> Episode:
    anime = await make_anime(session, anilist_id=anilist_id)
    episode = (await make_episodes(session, anime, 1))[-1]
    episode.state = EpisodeState.READY
    source = settings.downloads_dir / str(episode.id) / "[Group] Show - 01 [1080p].mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"not really a video")
    session.add(
        MediaFile(
            episode_id=episode.id,
            path=str(source),
            size=source.stat().st_size,
            review_state=ReviewState.AUTO,
        )
    )
    await session.commit()
    return episode


async def running_job(session: AsyncSession, episode_id: int, **payload: Any) -> Job:
    job = Job(
        type=OFFLINE_ENCODE,
        payload={"episode_id": episode_id, "why": "request", **payload},
        status=JobStatus.RUNNING,
        attempts=1,
        locked_by="test:1",
        locked_at=datetime.now(UTC),
    )
    session.add(job)
    await session.commit()
    return job


def api_path(episode_id: int) -> str:
    return f"/api/episodes/{episode_id}/offline"


# --- S1: a dead encode reads failed -----------------------------------------------


@pytest.mark.parametrize("state", [OfflineCopyState.QUEUED, OfflineCopyState.PREPARING])
def test_an_in_flight_row_without_a_live_job_reads_failed(state: OfflineCopyState) -> None:
    assert offline_state(copy_state=state, has_source=True, job_alive=False) == "failed"
    assert offline_state(copy_state=state, has_source=False, job_alive=False) == "unavailable"
    assert offline_state(copy_state=state, has_source=True, job_alive=True) == state.value


async def test_a_preparing_row_whose_job_died_reads_failed_and_a_post_requeues_it(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    ready = await add_episode(api_factory, anilist_id=942001)
    await add_source(api_factory, settings, ready)
    async with api_factory() as session:
        session.add(OfflineCopy(episode_id=ready, state=OfflineCopyState.PREPARING, codec="h264"))
        session.add(
            Job(
                type=OFFLINE_ENCODE,
                payload={"episode_id": ready, "why": "request", "progress": 0.4},
                status=JobStatus.FAILED,
                attempts=3,
            )
        )
        await session.commit()

    assert (await client.get(api_path(ready))).json()["state"] == "failed"
    page = await client.get(f"/api/episodes/{ready}/play")
    assert page.status_code == 200
    assert page.json()["episode"]["offline"]["state"] == "failed"

    again = await client.post(api_path(ready))
    assert again.status_code == 202
    assert again.json()["state"] == "queued"
    async with api_factory() as session:
        live = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.type == OFFLINE_ENCODE, Job.status == JobStatus.PENDING)
        )
        assert live == 1


# --- S2: the chosen tracks' length, and no retry for a refusal ----------------------


def test_a_matroska_duration_tag_is_read() -> None:
    assert stream_duration({"tags": {"DURATION": "00:23:40.123000000"}}) == pytest.approx(1420.123)
    assert stream_duration({"tags": {"DURATION-eng": "00:00:12.000000000"}}) == 12.0
    assert stream_duration({"duration": "5.5"}) == 5.5
    assert stream_duration({}) is None


#: The source: Japanese audio and the video at 12 s, an English dub that runs
#: to 40 s, and a container that says 40 s accordingly.
LONG_DUB_PROBE: dict[str, Any] = {
    **SOURCE_PROBE,
    "format": {"format_name": "matroska,webm", "duration": "40.0"},
    "streams": [
        {**SOURCE_PROBE["streams"][0], "tags": {"DURATION": "00:00:12.000000000"}},
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "tags": {"language": "eng", "DURATION": "00:00:40.000000000"},
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "aac",
            "tags": {"language": "jpn", "DURATION": "00:00:12.000000000"},
        },
        *SOURCE_PROBE["streams"][2:],
    ],
}


def test_the_plan_measures_the_chosen_tracks_not_the_container() -> None:
    plan = build_plan(LONG_DUB_PROBE, source=Path("/x.mkv"), output_dir=Path("/o"))

    assert plan.audio_lang == "ja"
    assert plan.duration == 40.0
    assert plan.chosen_duration == 12.0


async def test_a_longer_unchosen_dub_does_not_reject_a_good_copy(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=LONG_DUB_PROBE)
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942010)
        await offline_encode(context(session, settings, await running_job(session, episode.id)))
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.READY


async def test_an_ffmpeg_without_libass_fails_once_and_is_not_retried(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, filters=(), marker=marker)
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942011)
        await offline_encode(context(session, settings, await running_job(session, episode.id)))
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.FAILED
        assert copy.error is not None and "libass" in copy.error
    assert "encode" not in marker.read_text()


# --- S3: the queue is bounded, and the storage guard holds ---------------------------


async def test_a_users_eleventh_request_copy_is_refused(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    async with api_factory() as session:
        for offset in range(MAX_USER_REQUEST_COPIES):
            await running_job(session, 900_000 + offset, user_id=user.id)
        # A trip's copies are the trip's cap, not this one.
        for offset in range(5):
            await running_job(session, 910_000 + offset, why="trip")
    ready = await add_episode(api_factory, anilist_id=942020)
    await add_source(api_factory, settings, ready)

    response = await client.post(api_path(ready))

    assert response.status_code == 429
    assert response.json() == {"detail": "copy_queue_full"}


async def test_the_host_cap_refuses_every_user(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    async with api_factory() as session:
        for offset in range(MAX_HOST_REQUEST_COPIES):
            await running_job(session, 920_000 + offset, user_id=999_999)
    ready = await add_episode(api_factory, anilist_id=942021)
    await add_source(api_factory, settings, ready)

    response = await client.post(api_path(ready))

    assert response.status_code == 429
    assert response.json() == {"detail": "copy_queue_full"}


async def test_an_available_copy_is_still_answered_when_the_queue_is_full(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    """Only a *new* encode is refused: a copy already made is just returned."""
    async with api_factory() as session:
        for offset in range(MAX_USER_REQUEST_COPIES):
            await running_job(session, 930_000 + offset, user_id=user.id)
    ready = await add_episode(api_factory, anilist_id=942022)
    await write_copy(api_factory, settings, ready)

    response = await client.post(api_path(ready))

    assert response.status_code == 202
    assert response.json()["state"] == "available"


async def test_a_request_while_storage_is_held_is_409_storage_held(
    client: AsyncClient,
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def held(*_: object) -> bool:
        return True

    monkeypatch.setattr("arc.api.offline_copies.is_storage_held", held)
    ready = await add_episode(api_factory, anilist_id=942023)
    await add_source(api_factory, settings, ready)

    response = await client.post(api_path(ready))

    assert response.status_code == 409
    assert response.json() == {"detail": "storage_held"}
    async with api_factory() as session:
        assert await session.get(OfflineCopy, ready) is None


async def test_the_requester_is_recorded_on_the_job(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    ready = await add_episode(api_factory, anilist_id=942024)
    await add_source(api_factory, settings, ready)

    assert (await client.post(api_path(ready))).status_code == 202
    async with api_factory() as session:
        job = await session.scalar(select(Job).where(Job.type == OFFLINE_ENCODE))
        assert job is not None and job.payload["user_id"] == user.id


# --- S4: two requests at once --------------------------------------------------------


async def test_two_concurrent_requests_make_one_row_and_one_job(
    api_app: FastAPI, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    ready = await add_episode(api_factory, anilist_id=942030)
    await add_source(api_factory, settings, ready)
    async with api_transport(api_app) as one, api_transport(api_app) as two:
        await login(one, USER_EMAIL, USER_PASSWORD)
        await login(two, USER_EMAIL, USER_PASSWORD)
        first, second = await asyncio.gather(one.post(api_path(ready)), two.post(api_path(ready)))

    assert (first.status_code, second.status_code) == (202, 202)
    async with api_factory() as session:
        jobs = await session.scalar(
            select(func.count()).select_from(Job).where(Job.type == OFFLINE_ENCODE)
        )
        rows = await session.scalar(select(func.count()).select_from(OfflineCopy))
        assert (jobs, rows) == (1, 1)


async def test_the_encode_and_a_request_share_the_row(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job creates the row itself the same way: insert-or-keep, never a clash."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942031)
        job = await running_job(session, episode.id)
    async with api_factory() as other:
        await request_copy(other, settings, episode.id)
        await other.commit()
    async with api_factory() as session:
        await offline_encode(context(session, settings, job))
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.READY


# --- S7: retention and a copy cannot cross -------------------------------------------


async def test_an_episode_that_leaves_ready_before_the_claim_gets_no_copy(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N9: POST, then retention resets the episode, then the job is claimed."""
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, marker=marker)
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942040)
        asked = await request_copy(session, settings, episode.id)
        await session.commit()
        assert asked.job is not None
        job_id = asked.job.id
    async with api_factory() as session:
        row = await session.get(Episode, episode.id)
        assert row is not None
        row.state = EpisodeState.NOT_WANTED
        await session.commit()

    async with api_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        await offline_encode(context(session, settings, job))
        assert await session.get(OfflineCopy, episode.id) is None
        await session.refresh(job)
        assert job.payload["error_tail"] == NOT_READY_MESSAGE
    assert not marker.exists()


async def test_an_episode_that_leaves_ready_during_the_encode_keeps_no_copy(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    original = offline_module.encode_copy
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942041)
        job = await running_job(session, episode.id)

    async def encode_then_retention(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        async with api_factory() as other:
            row = await other.get(Episode, episode.id)
            assert row is not None
            row.state = EpisodeState.NOT_WANTED
            await other.commit()
        return result

    monkeypatch.setattr(offline_module, "encode_copy", encode_then_retention)
    async with api_factory() as session:
        await offline_encode(context(session, settings, job))
        assert await session.get(OfflineCopy, episode.id) is None
    assert not offline_path_for(settings, episode.id).exists()
    assert list(settings.offline_dir.glob("*.tmp-*")) == []


async def test_retention_refuses_once_a_request_has_queued_an_encode(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    """The other order: the request first, then the deletion sees its job."""
    settings = acquisition_settings(tmp_path)
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942042)
        targets = await targets_for_episode(session, settings, episode.id)
    async with api_factory() as other:
        await request_copy(other, settings, episode.id)
        await other.commit()
    async with api_factory() as session:
        row = await session.get(Episode, episode.id)
        assert row is not None
        removed = await delete_episode_files(session, settings, row, targets)
        assert removed.acted is False
    assert (settings.downloads_dir / str(episode.id)).exists()


async def test_a_copy_made_after_the_plan_still_goes_with_the_episode(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942043)
        targets = await targets_for_episode(session, settings, episode.id)
        assert targets.offline_copy is False
        path = offline_path_for(settings, episode.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"made since")
        session.add(OfflineCopy(episode_id=episode.id, state=OfflineCopyState.READY, size=10))
        await session.commit()

        removed = await delete_episode_files(session, settings, episode, targets)
        await session.commit()

        assert removed.offline_copies == 1
        assert await session.get(OfflineCopy, episode.id) is None
    assert not path.exists()


# --- N1: start-up sweep --------------------------------------------------------------


async def test_the_startup_sweep_removes_dead_staging_and_orphan_copies(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    root = settings.offline_dir
    async with api_factory() as session:
        kept = await ready_with_source(session, settings, anilist_id=942050)
        busy = await ready_with_source(session, settings, anilist_id=942051)
        session.add(OfflineCopy(episode_id=kept.id, state=OfflineCopyState.READY, size=1))
        await session.commit()
        await running_job(session, busy.id)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{kept.id}.mp4").write_bytes(b"a copy with a row")
    (root / "777777.mp4").write_bytes(b"a copy with no row")
    (root / "777777.tmp-5").mkdir()
    (root / f"{busy.id}.tmp-9").mkdir()
    (root / "notes.txt").write_text("not Arc's")

    async with api_factory() as session:
        removed = await sweep_offline_leftovers(session, settings)

    assert removed == 2
    assert sorted(path.name for path in root.iterdir()) == sorted(
        [f"{kept.id}.mp4", f"{busy.id}.tmp-9", "notes.txt"]
    )


# --- N2: the rendition's languages --------------------------------------------------


async def test_the_copy_follows_the_renditions_recorded_languages(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendition burned in Portuguese; the copy must match what is streamed."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942060)
        session.add(
            Rendition(
                episode_id=episode.id,
                dir="/nowhere",
                playlist_path="/nowhere/index.m3u8",
                subtitle_lang="pt",
                audio_lang="ja",
            )
        )
        await session.commit()
        await offline_encode(context(session, settings, await running_job(session, episode.id)))
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.READY
        assert copy.settings_key == settings_key(
            offline_options(settings), sub_lang="pt", audio_lang="ja"
        )


# --- N3: no touch on a refused range ------------------------------------------------


async def test_an_unsatisfiable_range_does_not_count_as_a_fetch(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    ready = await add_episode(api_factory, anilist_id=942070)
    path = await write_copy(api_factory, settings, ready)
    size = path.stat().st_size

    response = await client.get(f"/media/{ready}/offline.mp4", headers={"Range": f"bytes={size}-"})

    assert response.status_code == 416
    async with api_factory() as session:
        row = await session.get(OfflineCopy, ready)
        assert row is not None and row.last_served_at is None


# --- N4: the demo account is sent no copy --------------------------------------------


async def test_the_demo_account_sees_no_offline_field(
    api_app: FastAPI, api_factory: SessionFactory, settings: Settings
) -> None:
    ready = await add_episode(api_factory, anilist_id=942080)
    write_rendition(settings, ready)
    await write_copy(api_factory, settings, ready)
    demo = await add_user(api_factory, "demo-n4@arc.test", "demo-password")
    async with api_factory() as session:
        row = await session.get(User, demo.id)
        assert row is not None
        row.is_demo = True
        await session.commit()

    async with api_transport(api_app) as http:
        await login(http, "demo-n4@arc.test", "demo-password")
        play = await http.get(f"/api/episodes/{ready}/play")

    assert play.status_code == 200
    assert play.json()["episode"]["offline"] is None


# --- N9: a cancelled encode -----------------------------------------------------------


async def test_a_cancelled_encode_leaves_the_row_preparing_and_no_staging(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's drain cancels the task; the requeued job takes it from there."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=50, sleep=0.1)
    async with api_factory() as session:
        episode = await ready_with_source(session, settings, anilist_id=942090)
        job = await running_job(session, episode.id)

    async def run() -> None:
        async with api_factory() as session:
            await offline_encode(context(session, settings, job))

    task = asyncio.create_task(run())
    deadline = asyncio.get_running_loop().time() + 10
    while not list(settings.offline_dir.glob("*.tmp-*")):
        assert asyncio.get_running_loop().time() < deadline, "the encode never started"
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with api_factory() as check:
        copy = await check.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.PREPARING
    assert list(settings.offline_dir.glob("*.tmp-*")) == []
    assert not offline_path_for(settings, episode.id).exists()
