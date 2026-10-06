"""The ``offline_encode`` handler against the database, with a fake ffmpeg (FR-P6).

Like :mod:`tests.test_transcode_jobs`: the handler is driven through a
:class:`JobContext` with real, committing sessions, because what is asserted is
what one run leaves in ``offline_copies``, in the job row and on the disk — and
the handler's design turns on committing as it goes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
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
    ReviewState,
)
from arc.services.jobs.registry import JobContext, get_handler
from arc.services.media.copies import (
    CopyUnavailable,
    SourceGone,
    request_copy,
    settings_key,
)
from arc.services.media.download import stat_etag
from arc.services.media.jobs import STAGE_DONE, episode_lock
from arc.services.media.names import (
    OFFLINE_ENCODE,
    OFFLINE_REQUEST_PRIORITY,
    offline_dedupe_key,
    offline_path_for,
)
from arc.services.media.offline import (
    OFFLINE_LOCK_KEY,
    SOURCE_GONE_MESSAGE,
    offline_encode,
)
from arc.services.media.plan import OfflineOptions
from arc.services.media.transcode import TranscodeError, reset_semaphore
from tests.acquisition_helpers import acquisition_settings, make_anime, make_episodes
from tests.media_helpers import OFFLINE_PROBE, install_fake_ffmpeg

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _fresh_semaphore() -> None:
    reset_semaphore()


def context(session: AsyncSession, settings: Settings, job: Job) -> JobContext:
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("arc.jobs.test")
    )


async def a_ready_episode(
    session: AsyncSession,
    settings: Settings,
    *,
    anilist_id: int,
    with_file: bool = True,
) -> Episode:
    """One ready episode with a (tiny, fake) source file linked to it."""
    anime = await make_anime(session, anilist_id=anilist_id)
    episode = (await make_episodes(session, anime, 1))[-1]
    episode.state = EpisodeState.READY
    if with_file:
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
    await session.flush()
    await session.commit()
    return episode


async def a_job(session: AsyncSession, episode_id: int, why: str = "request") -> Job:
    job = Job(
        type=OFFLINE_ENCODE,
        payload={"episode_id": episode_id, "why": why},
        status=JobStatus.RUNNING,
        attempts=1,
        locked_by="test:1",
        locked_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    session.add(job)
    await session.commit()
    return job


def test_the_handler_is_registered() -> None:
    assert get_handler(OFFLINE_ENCODE) is offline_encode


# --- The happy path -------------------------------------------------------------


async def test_a_ready_episode_gets_a_validated_copy(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=3, marker=marker)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976001)
        job = await a_job(session, episode.id)
        await offline_encode(context(session, settings, job))

        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None
        await session.refresh(copy)
        target = offline_path_for(settings, episode.id)
        assert copy.state is OfflineCopyState.READY
        assert target.is_file()
        info = target.stat()
        assert copy.size == info.st_size
        # The very string the media route will answer with (FR-P6).
        assert copy.etag == stat_etag(info.st_size, info.st_mtime_ns)
        assert (copy.codec, copy.height, copy.crf, copy.audio_bitrate) == ("h264", 720, 26, "96k")
        assert copy.settings_key == settings_key(OfflineOptions(), sub_lang="en", audio_lang="ja")
        assert copy.media_file_id is not None
        assert copy.ready_at is not None and copy.error is None

        await session.refresh(job)
        assert job.payload["stage"] == STAGE_DONE
        assert job.payload["progress"] == 1.0
        assert job.payload["error_tail"] is None

        # The episode itself is untouched: still ready, still streaming.
        await session.refresh(episode)
        assert episode.state is EpisodeState.READY

    # Fonts and the subtitle were extracted before the encode, exactly as for
    # a rendition, and the staging directory is gone: only the copy remains.
    assert marker.read_text().splitlines() == ["fonts", "subtitle", "encode"]
    argv = (tmp_path / "calls.txt.argv").read_text().strip().split("\0")
    assert "+faststart" in argv and argv[-1] == "offline.mp4"
    assert [path.name for path in settings.offline_dir.iterdir()] == [f"{episode.id}.mp4"]


async def test_a_rerun_on_a_valid_copy_is_a_no_op(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent: a ready row whose file still matches needs no second encode."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976002)
        await offline_encode(context(session, settings, await a_job(session, episode.id)))
        before = offline_path_for(settings, episode.id).stat()

    marker = tmp_path / "again.txt"
    install_fake_ffmpeg(tmp_path / "bin2", monkeypatch, marker=marker)
    async with api_factory() as session:
        again = await a_job(session, episode.id)
        await offline_encode(context(session, settings, again))
        await session.refresh(again)
        assert again.payload["stage"] == STAGE_DONE

    assert not marker.exists(), "ffmpeg must not run for a copy that is already there"
    after = offline_path_for(settings, episode.id).stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


async def test_a_ready_row_whose_file_has_gone_is_made_again(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976003)
        await offline_encode(context(session, settings, await a_job(session, episode.id)))
    offline_path_for(settings, episode.id).unlink()

    async with api_factory() as session:
        await offline_encode(context(session, settings, await a_job(session, episode.id)))
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.READY
    assert offline_path_for(settings, episode.id).is_file()


# --- Requests -------------------------------------------------------------------


async def test_a_second_request_dedupes_onto_the_first(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    """Two devices asking at once make one job and one row."""
    settings = acquisition_settings(tmp_path)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976004)
        first = await request_copy(session, settings, episode.id)
        await session.commit()
        second = await request_copy(session, settings, episode.id)
        await session.commit()

        assert first.job is not None and second.job is not None
        assert first.job.id == second.job.id
        assert first.job.priority == OFFLINE_REQUEST_PRIORITY
        assert first.job.payload["dedupe_key"] == offline_dedupe_key(episode.id)
        assert first.job.payload["why"] == "request"
        jobs = (await session.scalars(select(Job).where(Job.type == OFFLINE_ENCODE))).all()
        assert len(jobs) == 1
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.QUEUED


async def test_a_request_for_an_available_copy_queues_nothing(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976005)
        await offline_encode(context(session, settings, await a_job(session, episode.id)))
        asked = await request_copy(session, settings, episode.id)

        assert asked.job is None
        assert asked.copy is not None and asked.copy.state is OfflineCopyState.READY


async def test_a_request_without_a_source_is_source_gone(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976006, with_file=False)
        with pytest.raises(SourceGone):
            await request_copy(session, settings, episode.id)


async def test_a_request_for_a_source_row_whose_file_is_gone_is_source_gone(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976007)
        source = await session.scalar(select(MediaFile).where(MediaFile.episode_id == episode.id))
        assert source is not None
        Path(source.path).unlink()
        with pytest.raises(SourceGone):
            await request_copy(session, settings, episode.id)


async def test_a_request_for_an_episode_that_is_not_ready_is_refused(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976008)
        episode.state = EpisodeState.MATCHED
        await session.commit()
        with pytest.raises(CopyUnavailable):
            await request_copy(session, settings, episode.id)
        with pytest.raises(CopyUnavailable):
            await request_copy(session, settings, 99_999_999)


async def test_a_failed_copy_is_queued_again_on_request(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976009)
        session.add(
            OfflineCopy(episode_id=episode.id, state=OfflineCopyState.FAILED, error="broken")
        )
        await session.commit()
        asked = await request_copy(session, settings, episode.id)
        await session.commit()

        assert asked.copy is not None
        assert asked.copy.state is OfflineCopyState.QUEUED
        assert asked.copy.error is None
        assert asked.job is not None


# --- Failures -------------------------------------------------------------------


async def test_a_failed_encode_is_committed_then_raised(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason survives the runner's rollback, and the exception schedules the retry."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, fail=True)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976010)
        job = await a_job(session, episode.id)
        with pytest.raises(TranscodeError):
            await offline_encode(context(session, settings, job))
        # What the runner does next.
        await session.rollback()

    async with api_factory() as check:
        copy = await check.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.FAILED
        assert copy.error is not None and "exited 1" in copy.error
        assert "stub complaint" in copy.error
        row = await check.get(Job, job.id)
        assert row is not None and "exited 1" in row.payload["error_tail"]
        ready = await check.get(Episode, episode.id)
        assert ready is not None and ready.state is EpisodeState.READY, "the episode still streams"

    assert not offline_path_for(settings, episode.id).exists()
    assert list(settings.offline_dir.glob("*.tmp-*")) == [], "the staging directory is cleaned up"


@pytest.mark.parametrize(
    ("probe", "faststart", "reason"),
    [
        (
            {
                **OFFLINE_PROBE,
                "streams": [
                    {**OFFLINE_PROBE["streams"][0], "codec_name": "hevc"},
                    OFFLINE_PROBE["streams"][1],
                ],
            },
            True,
            "not 'h264'",
        ),
        (
            {
                **OFFLINE_PROBE,
                "streams": [
                    {**OFFLINE_PROBE["streams"][0], "height": 1080},
                    OFFLINE_PROBE["streams"][1],
                ],
            },
            True,
            "1080 lines",
        ),
        ({**OFFLINE_PROBE, "format": {"duration": "3.0"}}, True, "3.0s long"),
        (None, False, "moov"),
    ],
    ids=["wrong-codec", "too-tall", "short", "no-faststart"],
)
async def test_validation_refuses_a_bad_copy_and_publishes_nothing(
    api_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: dict[str, object] | None,
    faststart: bool,
    reason: str,
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(
        tmp_path / "bin",
        monkeypatch,
        offline_probe=probe,
        faststart=faststart,  # type: ignore[arg-type]
    )
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976020)
        job = await a_job(session, episode.id)
        # A refusal no retry can change: committed and ended, never raised —
        # raising would make the runner spend two more whole encodes on it.
        await offline_encode(context(session, settings, job))

    async with api_factory() as check:
        copy = await check.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.FAILED
        assert copy.error is not None and reason in copy.error
        row = await check.get(Job, job.id)
        assert row is not None and reason in row.payload["error_tail"]
    assert not offline_path_for(settings, episode.id).exists(), "a bad copy is never renamed in"
    assert list(settings.offline_dir.glob("*.tmp-*")) == []


async def test_a_failure_leaves_the_previous_copy_in_place(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic: the old file stays until a validated new one replaces it."""
    settings = acquisition_settings(tmp_path)
    target = None
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976021)
        target = offline_path_for(settings, episode.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"the previous copy")
        # A row that does not match the file, so the encode runs.
        session.add(OfflineCopy(episode_id=episode.id, state=OfflineCopyState.QUEUED))
        await session.commit()
        install_fake_ffmpeg(tmp_path / "bin", monkeypatch, fail=True)
        with pytest.raises(TranscodeError):
            await offline_encode(context(session, settings, await a_job(session, episode.id)))

    assert target.read_bytes() == b"the previous copy"


async def test_a_source_that_is_gone_fails_the_copy_without_a_retry(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No retry can bring a deleted source back, so the job ends, failed and said."""
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, marker=marker)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976022, with_file=False)
        job = await a_job(session, episode.id)
        await offline_encode(context(session, settings, job))  # does not raise

        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.FAILED
        assert copy.error == SOURCE_GONE_MESSAGE
    assert not marker.exists()


async def test_a_second_claim_leaves_the_copy_to_the_lock_holder(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, marker=marker)
    async with api_factory() as setup:
        episode = await a_ready_episode(setup, settings, anilist_id=976023)
        job = await a_job(setup, episode.id)

    async with api_factory() as owner:
        async with episode_lock(owner, episode.id, key=OFFLINE_LOCK_KEY) as owned:
            assert owned
            # The transcode's lock on the same episode is a different lock: a
            # rendition and a copy may be made side by side.
            async with api_factory() as other, episode_lock(other, episode.id) as transcode:
                assert transcode
            async with api_factory() as session:
                await offline_encode(context(session, settings, job))

    assert not marker.exists()
    async with api_factory() as check:
        assert await check.get(OfflineCopy, episode.id) is None


async def test_the_hevc_setting_reaches_ffmpeg(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path, offline_codec="hevc")
    marker = tmp_path / "calls.txt"
    hevc = {
        **OFFLINE_PROBE,
        "streams": [
            {**OFFLINE_PROBE["streams"][0], "codec_name": "hevc", "codec_tag_string": "hvc1"},
            OFFLINE_PROBE["streams"][1],
        ],
    }
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, marker=marker, offline_probe=hevc)
    async with api_factory() as session:
        episode = await a_ready_episode(session, settings, anilist_id=976024)
        await offline_encode(context(session, settings, await a_job(session, episode.id)))
        copy = await session.get(OfflineCopy, episode.id)
        assert copy is not None and copy.state is OfflineCopyState.READY
        assert copy.codec == "hevc"

    argv = (tmp_path / "calls.txt.argv").read_text().strip().split("\0")
    assert argv[argv.index("-c:v") + 1] == "libx265"
    assert argv[argv.index("-tag:v") + 1] == "hvc1"
