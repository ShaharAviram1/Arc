"""The ``transcode`` handler against the database, with a fake ffmpeg (M7).

The handler is exercised through :class:`JobContext` rather than through the
worker loop — the loop has its own tests — so what is asserted here is what one
run does to ``episodes``, to ``renditions``, to the job row and to the disk.

Sessions are real and committing (``api_factory``): the handler's whole design
turns on committing as it goes, and a fixture that rolled everything back at
the end would prove nothing about the ``preparing`` state being visible or the
``failed`` state surviving the exception that follows it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    DEFAULT_PRIORITY,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    Rendition,
    ReviewState,
    Want,
)
from arc.services.jobs.registry import JobContext
from arc.services.library.link import link
from arc.services.media import jobs as media_jobs
from arc.services.media.jobs import (
    NO_SOURCE,
    STAGE_DONE,
    episode_lock,
    language_rules,
    sweep_transcodes,
    transcode_episode,
)
from arc.services.media.names import (
    TRANSCODE,
    enqueue_transcode,
    latest_transcode_jobs,
    transcode_dedupe_key,
    transcode_priority,
)
from arc.services.media.transcode import (
    TranscodeError,
    TranscodeResult,
    reset_semaphore,
    transcode_semaphore,
)
from tests.acquisition_helpers import (
    acquisition_settings,
    make_anime,
    make_entry,
    make_episodes,
    make_user,
    set_setting,
)
from tests.media_helpers import install_fake_ffmpeg

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _fresh_semaphore() -> None:
    reset_semaphore()


def context(session: AsyncSession, settings: Settings, job: Job) -> JobContext:
    return JobContext(
        job=job,
        session=session,
        settings=settings,
        log=logging.getLogger("arc.jobs.test"),
    )


async def a_matched_episode(
    session: AsyncSession,
    settings: Settings,
    *,
    anilist_id: int,
    number: int = 3,
    state: EpisodeState = EpisodeState.MATCHED,
    with_file: bool = True,
) -> Episode:
    """One episode with a (real, tiny) source file linked to it."""
    anime = await make_anime(session, anilist_id=anilist_id)
    episodes = await make_episodes(session, anime, number)
    episode = episodes[-1]
    episode.state = state
    if with_file:
        source = settings.downloads_dir / str(episode.id) / "[Group] Show - 03 [1080p].mkv"
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


async def a_job(session: AsyncSession, episode_id: int, **payload: object) -> Job:
    job = Job(
        type=TRANSCODE,
        payload={"episode_id": episode_id, **payload},
        status=JobStatus.RUNNING,
        attempts=1,
        locked_by="test:1",
        locked_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(job)
    await session.commit()
    return job


# --- The happy path ---------------------------------------------------------


async def test_a_matched_episode_becomes_ready_with_a_rendition(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=3)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970001)
        job = await a_job(session, episode.id)
        await transcode_episode(context(session, settings, job))

        await session.refresh(episode)
        assert episode.state is EpisodeState.READY

        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == episode.id)
        )
        assert rendition is not None
        assert rendition.duration == pytest.approx(12.0)
        assert (rendition.width, rendition.height) == (1920, 1080)
        assert rendition.subtitle_lang == "en"
        assert rendition.audio_lang == "ja"
        assert rendition.ready_at is not None
        assert Path(rendition.dir) == settings.renditions_dir / str(episode.id)
        assert Path(rendition.playlist_path).name == "index.m3u8"
        assert Path(rendition.playlist_path).exists()

        await session.refresh(job)
        assert job.payload["stage"] == STAGE_DONE
        assert job.payload["progress"] == 1.0
        assert job.payload["error_tail"] is None


async def test_the_episode_is_preparing_while_it_encodes(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The show page must see ``preparing`` before the encode finishes (FR-P4)."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=3, sleep=0.05)
    seen: list[tuple[str, float]] = []

    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970002)
        job = await a_job(session, episode.id)

        original = media_jobs._Reporter.write

        async def spy(self, **kwargs):  # type: ignore[no-untyped-def]
            await original(self, **kwargs)
            # Read the episode through a *second* session, so what is asserted
            # is what another process would see.
            async with api_factory() as watcher:
                row = await watcher.get(Episode, episode.id)
                assert row is not None
                seen.append((row.state.value, float(self.payload.get("progress") or 0.0)))

        monkeypatch.setattr(media_jobs._Reporter, "write", spy)
        await transcode_episode(context(session, settings, job))

    states = [state for state, _ in seen]
    assert states[0] == "preparing"
    assert states[-1] == "ready"
    assert max(progress for _, progress in seen) == 1.0


async def test_the_job_keeps_its_own_lock_warm(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``locked_at`` moves forward while ffmpeg runs, so the sweep leaves it alone."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=3)
    monkeypatch.setattr(media_jobs, "HEARTBEAT_SECONDS", 0.0)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970003)
        job = await a_job(session, episode.id)
        stale = job.locked_at
        assert stale is not None

        await transcode_episode(context(session, settings, job))

        async with api_factory() as other:
            fresh = await other.get(Job, job.id)
            assert fresh is not None and fresh.locked_at is not None
            assert fresh.locked_at > stale


async def beat_past(
    factory: SessionFactory, job_id: int, after: datetime, *, timeout: float = 10.0
) -> datetime:
    """Wait until ``jobs.locked_at`` has moved past ``after``; return the new value."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        async with factory() as watcher:
            row = await watcher.get(Job, job_id)
            if row is not None and row.locked_at is not None and row.locked_at > after:
                return row.locked_at
        await asyncio.sleep(0.02)
    raise AssertionError(f"jobs.locked_at never moved past {after}")


async def test_the_lock_stays_warm_while_the_job_waits_for_an_encode_slot(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait is the longest a transcode is ever quiet, and it must not look dead.

    ffmpeg has not started, so there is no progress to piggyback a heartbeat
    on; without a beat of its own a job queued behind two twenty-minute encodes
    would be silent for longer than ``WORKER_STALE_AFTER`` and requeued
    underneath itself.
    """
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=1)
    monkeypatch.setattr(media_jobs, "HEARTBEAT_SECONDS", 0.05)

    # Every slot taken, by nobody: the handler will wait for this one.
    semaphore = transcode_semaphore(1)
    await semaphore.acquire()

    async with api_factory() as setup:
        episode = await a_matched_episode(setup, settings, anilist_id=970081)
        job = await a_job(setup, episode.id)
    stale = job.locked_at
    assert stale is not None

    async def claim() -> None:
        async with api_factory() as session:
            await transcode_episode(context(session, settings, job))

    waiting = asyncio.create_task(claim())
    try:
        first = await beat_past(api_factory, job.id, stale)
        second = await beat_past(api_factory, job.id, first)
        assert second > first > stale
        # And it really is still waiting: the beats are not the encode's.
        assert not waiting.done()
    finally:
        semaphore.release()
        await waiting

    async with api_factory() as after:
        done = await after.get(Episode, episode.id)
        assert done is not None and done.state is EpisodeState.READY


# --- One claim at a time ----------------------------------------------------


async def test_only_one_session_can_hold_an_episodes_transcode_lock(
    api_factory: SessionFactory,
) -> None:
    """Two sessions, one lock: the second is told no rather than made to wait."""
    async with api_factory() as first, api_factory() as second:
        async with episode_lock(first, 4242) as held:
            assert held is True
            async with episode_lock(second, 4242) as again:
                assert again is False
            # A different episode is a different lock, not a queue.
            async with episode_lock(second, 4243) as other:
                assert other is True
        # And it goes when the transaction holding it does.
        async with episode_lock(second, 4242) as after:
            assert after is True


async def test_a_second_claim_leaves_the_episode_to_the_one_holding_the_lock(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale sweep that fires a moment early must not start a second ffmpeg."""
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=2, marker=marker)

    async with api_factory() as setup:
        episode = await a_matched_episode(setup, settings, anilist_id=970090)
        second_job = await a_job(setup, episode.id)

    async with api_factory() as owner:
        async with episode_lock(owner, episode.id) as owned:
            assert owned
            async with api_factory() as session:
                await transcode_episode(context(session, settings, second_job))

    # Nothing ran, nothing moved, nothing was written.
    assert not marker.exists()
    async with api_factory() as check:
        row = await check.get(Episode, episode.id)
        assert row is not None and row.state is EpisodeState.MATCHED
        made = await check.scalar(select(Rendition).where(Rendition.episode_id == episode.id))
        assert made is None
    assert not (settings.renditions_dir / str(episode.id)).exists()


async def test_five_claims_at_once_run_two_encodes_at_once(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MAX_TRANSCODES`` caps the *handler*, not just the function it calls.

    ``test_transcode_runner`` proves the semaphore; this proves the handler is
    inside it — five whole jobs, five episodes, five sessions, two ffmpegs.
    """
    settings = acquisition_settings(tmp_path)
    assert settings.max_transcodes == 2
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=4, sleep=0.05)

    live = 0
    peak = 0
    original = media_jobs.transcode

    async def counted(*args: Any, **kwargs: Any) -> TranscodeResult:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            return await original(*args, **kwargs)
        finally:
            live -= 1

    monkeypatch.setattr(media_jobs, "transcode", counted)

    jobs: list[Job] = []
    async with api_factory() as setup:
        for index in range(5):
            episode = await a_matched_episode(setup, settings, anilist_id=970100 + index)
            jobs.append(await a_job(setup, episode.id))

    async def claim(job: Job) -> None:
        async with api_factory() as session:
            await transcode_episode(context(session, settings, job))

    await asyncio.gather(*(claim(job) for job in jobs))

    assert peak == 2


# --- Nothing half-written on disk -------------------------------------------


def staging_dirs(settings: Settings, episode_id: int) -> list[Path]:
    return sorted(settings.renditions_dir.glob(f"{episode_id}.tmp-*"))


async def test_the_encode_is_built_beside_the_rendition_and_renamed_into_place(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader sees the old rendition or the new one, never one being filled in."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=2)

    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970110)
        job = await a_job(session, episode.id)
        output = settings.renditions_dir / str(episode.id)

        seen: dict[str, Any] = {}
        original = media_jobs.transcode

        async def spy(plan: Any, **kwargs: Any) -> TranscodeResult:
            seen["encoded_into"] = plan.output_dir
            seen["output_exists_yet"] = output.exists()
            return await original(plan, **kwargs)

        monkeypatch.setattr(media_jobs, "transcode", spy)
        await transcode_episode(context(session, settings, job))

        # ffmpeg wrote into the sibling, not into the rendition directory.
        assert seen["encoded_into"] == output.with_suffix(f".tmp-{job.id}")
        assert seen["output_exists_yet"] is False

        # And what is left is the rendition and nothing else.
        assert (output / "index.m3u8").exists()
        assert not (output / "_work").exists()
        assert staging_dirs(settings, episode.id) == []

        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == episode.id)
        )
        assert rendition is not None and Path(rendition.dir) == output


async def test_a_failed_encode_leaves_nothing_behind_on_disk(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempts run out; the disk must look as it did before the first one."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, fail=True)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970111)
        job = await a_job(session, episode.id)

        with pytest.raises(TranscodeError):
            await transcode_episode(context(session, settings, job))

    assert staging_dirs(settings, episode.id) == []
    assert not (settings.renditions_dir / str(episode.id)).exists()


async def test_a_cancelled_encode_cleans_up_after_itself(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker draining at shutdown is a cancellation, not an exception."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=200, sleep=0.05)

    async with api_factory() as setup:
        episode = await a_matched_episode(setup, settings, anilist_id=970112)
        job = await a_job(setup, episode.id)

    async def claim() -> None:
        async with api_factory() as session:
            await transcode_episode(context(session, settings, job))

    running = asyncio.create_task(claim())
    deadline = monotonic() + 10.0
    while not staging_dirs(settings, episode.id) and monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert staging_dirs(settings, episode.id), "the encode never started"

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert staging_dirs(settings, episode.id) == []
    assert not (settings.renditions_dir / str(episode.id)).exists()


async def test_a_temporary_directory_a_killed_worker_left_is_swept_up(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SIGKILL`` runs no ``finally``; the next attempt is what tidies up."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=2)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970113)
        abandoned = settings.renditions_dir / f"{episode.id}.tmp-99999"
        abandoned.mkdir(parents=True)
        (abandoned / "seg_00000.m4s").write_bytes(b"from a worker that was killed")

        job = await a_job(session, episode.id)
        await transcode_episode(context(session, settings, job))

    assert not abandoned.exists()
    assert staging_dirs(settings, episode.id) == []


# --- Failure ----------------------------------------------------------------


async def test_a_failed_encode_marks_the_episode_and_keeps_the_tail(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, fail=True)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970004)
        job = await a_job(session, episode.id)

        with pytest.raises(TranscodeError):
            await transcode_episode(context(session, settings, job))

        # Both must survive the rollback the runner does on the way out.
        async with api_factory() as other:
            row = await other.get(Episode, episode.id)
            failed = await other.get(Job, job.id)
            assert row is not None and row.state is EpisodeState.FAILED
            assert failed is not None
            assert "Error opening output file" in failed.payload["error_tail"]
            assert (
                await other.scalar(select(Rendition).where(Rendition.episode_id == episode.id))
                is None
            )


async def test_a_failed_episode_is_retried_from_failed_to_preparing(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, fail=True)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970005)
        job = await a_job(session, episode.id)
        with pytest.raises(TranscodeError):
            await transcode_episode(context(session, settings, job))
        await session.refresh(episode)
        assert episode.state is EpisodeState.FAILED

    # The retry: the same job row, a working ffmpeg this time.
    install_fake_ffmpeg(tmp_path / "bin2", monkeypatch, segments=2)
    async with api_factory() as session:
        again = await session.get(Job, job.id)
        assert again is not None
        await transcode_episode(context(session, settings, again))
        row = await session.get(Episode, episode.id)
        assert row is not None and row.state is EpisodeState.READY
        assert again.payload["error_tail"] is None


async def test_an_episode_with_no_file_fails_with_a_readable_reason(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970006, with_file=False)
        job = await a_job(session, episode.id)
        with pytest.raises(TranscodeError, match=NO_SOURCE):
            await transcode_episode(context(session, settings, job))
        await session.refresh(episode)
        assert episode.state is EpisodeState.FAILED
        await session.refresh(job)
        assert job.payload["error_tail"] == NO_SOURCE


async def test_a_source_that_has_gone_missing_fails_rather_than_silently_succeeding(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970007)
        media_file = await session.scalar(
            select(MediaFile).where(MediaFile.episode_id == episode.id)
        )
        assert media_file is not None
        Path(media_file.path).unlink()
        job = await a_job(session, episode.id)
        with pytest.raises(TranscodeError, match="source file is missing"):
            await transcode_episode(context(session, settings, job))
        await session.refresh(episode)
        assert episode.state is EpisodeState.FAILED


# --- Idempotency and force --------------------------------------------------


async def test_a_rendition_already_on_disk_is_not_encoded_again(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=2, marker=marker)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970008)
        first = await a_job(session, episode.id)
        await transcode_episode(context(session, settings, first))
        assert marker.read_text().count("encode") == 1

        await session.refresh(episode)
        settled = episode.state_changed_at

        second = await a_job(session, episode.id)
        await transcode_episode(context(session, settings, second))

        assert marker.read_text().count("encode") == 1
        await session.refresh(episode)
        assert episode.state is EpisodeState.READY
        # And it never left: a job with nothing to do must not flick a playable
        # episode through ``preparing`` and back while somebody is looking.
        assert episode.state_changed_at == settled
        assert second.payload["stage"] == STAGE_DONE
        rows = await session.scalars(select(Rendition).where(Rendition.episode_id == episode.id))
        assert len(list(rows.all())) == 1


async def test_force_deletes_the_old_rendition_and_encodes_again(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=4, marker=marker)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=970009)
        await transcode_episode(context(session, settings, await a_job(session, episode.id)))
        stale = settings.renditions_dir / str(episode.id) / "seg_00009.m4s"
        stale.write_bytes(b"left over from a longer encode")

        forced = await a_job(session, episode.id, force=True)
        await transcode_episode(context(session, settings, forced))

        assert marker.read_text().count("encode") == 2
        assert not stale.exists()
        await session.refresh(episode)
        assert episode.state is EpisodeState.READY
        rows = list(
            (
                await session.scalars(select(Rendition).where(Rendition.episode_id == episode.id))
            ).all()
        )
        assert len(rows) == 1


async def test_an_episode_that_has_moved_on_is_left_alone(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, marker=marker)
    async with api_factory() as session:
        episode = await a_matched_episode(
            session, settings, anilist_id=970010, state=EpisodeState.DOWNLOADING
        )
        job = await a_job(session, episode.id)
        await transcode_episode(context(session, settings, job))
        await session.refresh(episode)
        assert episode.state is EpisodeState.DOWNLOADING
        assert not marker.exists()


async def test_an_episode_that_went_away_is_not_an_error(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        job = await a_job(session, 999_999)
        await transcode_episode(context(session, settings, job))


# --- Languages --------------------------------------------------------------


async def test_the_configured_languages_choose_the_tracks(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=1)
    async with api_factory() as session:
        await set_setting(session, "sub_lang", "pt")
        await session.commit()
        assert await language_rules(session) == ("pt", "ja")

        episode = await a_matched_episode(session, settings, anilist_id=970011)
        await transcode_episode(context(session, settings, await a_job(session, episode.id)))
        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == episode.id)
        )
        assert rendition is not None and rendition.subtitle_lang == "pt"


async def test_a_nonsense_language_row_falls_back_to_the_default(
    api_factory: SessionFactory,
) -> None:
    async with api_factory() as session:
        await set_setting(session, "sub_lang", 7)
        await session.commit()
        assert await language_rules(session) == ("en", "ja")


# --- Priority ---------------------------------------------------------------


async def test_priority_follows_how_close_the_nearest_watcher_is(
    api_factory: SessionFactory,
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=970020)
        episodes = await make_episodes(session, anime, 6)
        far = await make_user(session, "far@arc.test")
        near = await make_user(session, "near@arc.test")
        await make_entry(session, far, anime, progress=1)
        await make_entry(session, near, anime, progress=4)
        target = episodes[4]  # episode 5

        # Nobody wants it yet: the default, which sits behind anybody waiting.
        assert await transcode_priority(session, target.id) == DEFAULT_PRIORITY

        session.add(Want(user_id=far.id, episode_id=target.id))
        await session.flush()
        assert await transcode_priority(session, target.id) == 40

        # The nearest watcher wins: one episode away is priority 10.
        session.add(Want(user_id=near.id, episode_id=target.id))
        await session.flush()
        assert await transcode_priority(session, target.id) == 10

        # A dropped want does not count.
        dropped = await session.get(Want, (near.id, target.id))
        assert dropped is not None
        dropped.dropped_at = datetime.now(UTC)
        await session.flush()
        assert await transcode_priority(session, target.id) == 40


async def test_a_watcher_who_is_already_past_it_gives_the_top_priority(
    api_factory: SessionFactory,
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=970021)
        episodes = await make_episodes(session, anime, 4)
        user = await make_user(session, "rewatch@arc.test")
        await make_entry(session, user, anime, progress=4)
        session.add(Want(user_id=user.id, episode_id=episodes[0].id))
        await session.flush()
        # max(1 - 4, 0) == 0: they are ready for it now.
        assert await transcode_priority(session, episodes[0].id) == 0


async def test_the_enqueue_carries_the_priority_and_deduplicates(
    api_factory: SessionFactory,
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=970022)
        episodes = await make_episodes(session, anime, 3)
        user = await make_user(session, "want@arc.test")
        await make_entry(session, user, anime, progress=2)
        session.add(Want(user_id=user.id, episode_id=episodes[2].id))
        await session.flush()

        first = await enqueue_transcode(session, episodes[2].id)
        await session.commit()
        assert first.priority == 10
        assert first.payload["dedupe_key"] == transcode_dedupe_key(episodes[2].id)

        second = await enqueue_transcode(session, episodes[2].id, force=True)
        await session.commit()
        assert second.id == first.id
        # A dedupe hit returns the queued job as it stands, force and all.
        assert "force" not in second.payload

        found = await latest_transcode_jobs(session, [episodes[2].id, episodes[0].id])
        assert set(found) == {episodes[2].id}


# --- The link hook ----------------------------------------------------------


async def test_linking_a_file_queues_its_transcode(api_factory: SessionFactory) -> None:
    """FR-P1: as soon as a file is matched, a transcode job runs."""
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=970030)
        await make_episodes(session, anime, 3)
        media_file = MediaFile(path="/data/downloads/x/[G] Show - 02.mkv", size=1)
        session.add(media_file)
        await session.flush()

        episode = await link(
            session,
            media_file,
            anime_id=anime.id,
            episode_number=2,
            review_state=ReviewState.AUTO,
        )
        await session.commit()

        queued = list((await session.scalars(select(Job).where(Job.type == TRANSCODE))).all())
        assert [job.payload["episode_id"] for job in queued] == [episode.id]
        assert episode.state is EpisodeState.MATCHED

        # Linking the same file again does not queue a second encode.
        await link(
            session,
            media_file,
            anime_id=anime.id,
            episode_number=2,
            review_state=ReviewState.CONFIRMED,
        )
        await session.commit()
        again = list((await session.scalars(select(Job).where(Job.type == TRANSCODE))).all())
        assert len(again) == 1


async def test_linking_a_file_to_a_ready_episode_queues_nothing(
    api_factory: SessionFactory,
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=970031)
        episodes = await make_episodes(session, anime, 2, state=EpisodeState.READY)
        media_file = MediaFile(path="/data/downloads/y/[G] Show - 01.mkv", size=1)
        session.add(media_file)
        await session.flush()

        await link(
            session,
            media_file,
            anime_id=anime.id,
            episode_number=episodes[0].number,
            review_state=ReviewState.CONFIRMED,
        )
        await session.commit()
        assert list((await session.scalars(select(Job).where(Job.type == TRANSCODE))).all()) == []


# --- The startup sweep ------------------------------------------------------


async def test_the_sweep_queues_what_a_restart_would_have_lost(
    api_factory: SessionFactory,
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=970040)
        episodes = await make_episodes(session, anime, 7)
        matched, preparing, failed, exhausted, already, done, downloading = episodes

        matched.state = EpisodeState.MATCHED
        preparing.state = EpisodeState.PREPARING
        failed.state = EpisodeState.FAILED
        exhausted.state = EpisodeState.FAILED
        already.state = EpisodeState.MATCHED
        done.state = EpisodeState.READY
        downloading.state = EpisodeState.DOWNLOADING

        # A failed job with attempts left: due a retry the restart lost.
        session.add(
            Job(
                type=TRANSCODE,
                payload={"episode_id": failed.id},
                status=JobStatus.FAILED,
                attempts=1,
                max_attempts=3,
            )
        )
        # And one that has genuinely run out: a person's problem now.
        session.add(
            Job(
                type=TRANSCODE,
                payload={"episode_id": exhausted.id},
                status=JobStatus.FAILED,
                attempts=3,
                max_attempts=3,
            )
        )
        # And one still queued: nothing to do.
        session.add(
            Job(
                type=TRANSCODE,
                payload={"episode_id": already.id},
                status=JobStatus.PENDING,
                attempts=0,
            )
        )
        # A ready episode with a rendition is finished business.
        session.add(Rendition(episode_id=done.id, dir="/data/renditions/1", playlist_path="x"))
        await session.commit()

        assert await sweep_transcodes(session) == 3

        queued = await session.scalars(
            select(Job).where(Job.type == TRANSCODE, Job.status == JobStatus.PENDING)
        )
        ids = {job.payload["episode_id"] for job in queued.all()}
        assert ids == {matched.id, preparing.id, failed.id, already.id}

        # Running it again queues nothing: everything now has an active job.
        assert await sweep_transcodes(session) == 0


async def test_the_sweep_ignores_an_episode_that_already_has_a_rendition(
    api_factory: SessionFactory,
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=970041)
        episodes = await make_episodes(session, anime, 1)
        episodes[0].state = EpisodeState.PREPARING
        session.add(
            Rendition(episode_id=episodes[0].id, dir="/data/renditions/2", playlist_path="y")
        )
        await session.commit()
        assert await sweep_transcodes(session) == 0
