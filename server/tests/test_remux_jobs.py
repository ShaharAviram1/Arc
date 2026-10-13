"""The ``remux`` handler against the database, with a fake ffmpeg (M20 T2).

The same shape as :mod:`tests.test_transcode_jobs`: the handler is driven
through a :class:`JobContext` with real, committing sessions, and ffmpeg and
ffprobe are scripts on ``PATH`` (:mod:`tests.media_helpers`) — so what is
asserted is what one run does to ``episodes``, ``renditions``, the job rows
and the disk. The real ffmpeg is used by ``test_remux_slow``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.db import SessionFactory
from arc.models import (
    RENDITION_REMUX,
    RENDITION_TRANSCODE,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    Rendition,
    ReviewState,
)
from arc.services.media.jobs import STAGE_DONE, episode_lock, transcode_episode
from arc.services.media.names import (
    REMUX,
    REMUX_INFO_NAME,
    REMUX_MAX_ATTEMPTS,
    SUBTITLES_VTT_NAME,
    TRANSCODE,
    RemuxInfo,
    enqueue_remux,
    latest_prepare_jobs,
    latest_transcode_jobs,
    remux_dedupe_key,
)
from arc.services.media.remux import (
    KEY_FALLBACK_REASON,
    KEY_TRANSCODE_JOB,
    NOTE_NO_DIALOGUE,
    REMUX_LOCK_KEY,
    STAGE_FALLBACK,
    codecs_string,
    peak_bandwidth,
    remux_episode,
    subtitle_label,
)
from arc.services.media.subtitles import vtt_to_cues
from arc.services.media.transcode import TranscodeError, reset_semaphore
from tests.acquisition_helpers import acquisition_settings, make_anime, make_episodes
from tests.media_helpers import (
    HI10P_SOURCE_PROBE,
    REMUX_SOURCE_PROBE,
    install_fake_ffmpeg,
)
from tests.test_transcode_jobs import a_matched_episode, context

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _fresh_semaphore() -> None:
    reset_semaphore()


async def a_remux_job(session: AsyncSession, episode_id: int, **payload: object) -> Job:
    job = Job(
        type=REMUX,
        payload={"episode_id": episode_id, **payload},
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=REMUX_MAX_ATTEMPTS,
        locked_by="test:1",
        locked_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    session.add(job)
    await session.commit()
    return job


def calls(marker: Path) -> list[str]:
    return marker.read_text().split() if marker.exists() else []


# --- The happy path ---------------------------------------------------------


async def test_a_copyable_episode_is_remuxed_with_a_text_track(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(
        tmp_path / "bin", monkeypatch, segments=3, marker=marker, source_probe=REMUX_SOURCE_PROBE
    )
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975001)
        job = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, job))

        await session.refresh(episode)
        assert episode.state is EpisodeState.READY
        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == episode.id)
        )
        assert rendition is not None
        assert rendition.kind == RENDITION_REMUX
        assert rendition.text_subtitles is True
        assert rendition.subtitle_lang == "en"
        assert rendition.audio_lang == "ja"
        assert (rendition.width, rendition.height) == (1920, 1080)
        assert rendition.duration == pytest.approx(12.0)
        assert Path(rendition.playlist_path).name == "video.m3u8"

        await session.refresh(job)
        assert job.payload["stage"] == STAGE_DONE
        assert job.payload["progress"] == 1.0
        assert job.payload["error_tail"] is None
        assert job.payload["audio"] == "copy"

    # One ffmpeg, the remux; no burn-in encode and no separate extraction.
    assert calls(marker) == ["remux"]
    argv = (tmp_path / "calls.txt.argv").read_text().split("\0")
    assert argv.count("copy") >= 2  # video and audio both copied
    assert "-vf" not in argv

    directory = settings.renditions_dir / str(episode.id)
    assert (directory / "video.m3u8").is_file()
    assert not (directory / "index.m3u8").exists()
    assert not (directory / "_work").exists()
    cues = vtt_to_cues((directory / SUBTITLES_VTT_NAME).read_text())
    assert [cue.text for cue in cues] == ["Hello there.", "<i>See you</i> later."]
    info = RemuxInfo.from_json((directory / REMUX_INFO_NAME).read_text())
    assert info.mpegts == round(0.083 * 90_000)
    assert info.codecs == "avc1.640028,mp4a.40.2"
    assert info.subtitle_name == "English"
    assert info.bandwidth > 0
    assert not list(settings.renditions_dir.glob(f"{episode.id}.tmp-*"))


async def test_flac_audio_is_converted_and_the_bitrate_comes_from_the_settings(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path).model_copy(update={"remux_audio_bitrate": "128k"})
    marker = tmp_path / "calls.txt"
    flac = json.loads(json.dumps(REMUX_SOURCE_PROBE))
    flac["streams"][1] = {**flac["streams"][1], "codec_name": "flac"}
    del flac["streams"][1]["profile"]
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, marker=marker, source_probe=flac)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975002)
        job = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, job))
        await session.refresh(job)
        assert job.payload["audio"] == "aac"

    argv = (tmp_path / "calls.txt.argv").read_text().split("\0")
    assert argv[argv.index("-c:a") + 1 : argv.index("-c:a") + 4] == ["aac", "-b:a", "128k"]


async def test_a_source_whose_track_has_no_dialogue_plays_without_subtitles(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    signs_only = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Signs,,0,0,0,,SHOP\n"
    )
    install_fake_ffmpeg(
        tmp_path / "bin",
        monkeypatch,
        source_probe=REMUX_SOURCE_PROBE,
        subtitle_ass=signs_only,
    )
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975003)
        job = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, job))
        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == episode.id)
        )
        assert rendition is not None
        assert rendition.text_subtitles is False
        assert rendition.subtitle_lang is None
        await session.refresh(job)
        assert NOTE_NO_DIALOGUE in job.payload["notes"]
    assert not (settings.renditions_dir / str(episode.id) / SUBTITLES_VTT_NAME).exists()


# --- The fallback (FR-P7) ---------------------------------------------------


async def test_a_10_bit_source_is_handed_to_the_burn_in_encode(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remux → done with ``stage=fallback``, one queued transcode, ready after it."""
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(
        tmp_path / "bin", monkeypatch, marker=marker, source_probe=HI10P_SOURCE_PROBE
    )
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975010)
        job = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, job))

        await session.refresh(episode)
        assert episode.state is EpisodeState.MATCHED, "the transcode moves it, not the remux"
        await session.refresh(job)
        assert job.payload["stage"] == STAGE_FALLBACK
        assert job.payload[KEY_FALLBACK_REASON] == "10-bit"
        transcodes = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.type == TRANSCODE, Job.payload["episode_id"].astext == str(episode.id)
                    )
                )
            ).all()
        )
        assert len(transcodes) == 1
        queued = transcodes[0]
        assert queued.status is JobStatus.PENDING
        assert job.payload[KEY_TRANSCODE_JOB] == queued.id
        assert calls(marker) == [], "no ffmpeg ran for the remux"
        assert (await latest_transcode_jobs(session, [episode.id]))[episode.id].id == queued.id

        # The handed-over transcode makes the episode ready, burned in.
        queued.status = JobStatus.RUNNING
        await session.commit()
        await transcode_episode(context(session, settings, queued))
        await session.refresh(episode)
        assert episode.state is EpisodeState.READY
        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == episode.id)
        )
        assert rendition is not None
        assert rendition.kind == RENDITION_TRANSCODE
        assert rendition.text_subtitles is False
    assert "encode" in calls(marker)


async def test_a_fallback_run_twice_queues_one_transcode(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=HI10P_SOURCE_PROBE)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975011)
        job = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, job))
        await remux_episode(context(session, settings, job))
        count = len(
            (
                await session.scalars(
                    select(Job).where(
                        Job.type == TRANSCODE, Job.payload["episode_id"].astext == str(episode.id)
                    )
                )
            ).all()
        )
        assert count == 1


# --- Idempotency, locks and failures ----------------------------------------


async def test_rerunning_the_job_is_a_no_op(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(
        tmp_path / "bin", monkeypatch, marker=marker, source_probe=REMUX_SOURCE_PROBE
    )
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975020)
        job = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, job))
        playlist = settings.renditions_dir / str(episode.id) / "video.m3u8"
        before = playlist.stat().st_mtime_ns

        again = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, again))
        await session.refresh(again)
        await session.refresh(episode)
        assert again.payload["stage"] == STAGE_DONE
        assert episode.state is EpisodeState.READY
    assert calls(marker) == ["remux"], "the second run started no ffmpeg"
    assert playlist.stat().st_mtime_ns == before


async def test_a_second_claim_leaves_the_episode_to_the_one_holding_the_lock(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two claims of one episode: one remuxes, the other does nothing at all."""
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(
        tmp_path / "bin", monkeypatch, marker=marker, source_probe=REMUX_SOURCE_PROBE
    )
    async with api_factory() as setup:
        episode = await a_matched_episode(setup, settings, anilist_id=975021)
        second = await a_remux_job(setup, episode.id)

    async with api_factory() as owner:
        async with episode_lock(owner, episode.id, key=REMUX_LOCK_KEY) as owned:
            assert owned
            async with api_factory() as session:
                await remux_episode(context(session, settings, second))

    assert not marker.exists()
    async with api_factory() as check:
        row = await check.get(Episode, episode.id)
        assert row is not None and row.state is EpisodeState.MATCHED
        assert (
            await check.scalar(select(Rendition).where(Rendition.episode_id == episode.id)) is None
        )


async def test_the_remux_lock_is_not_the_transcode_lock(api_factory: SessionFactory) -> None:
    """The fallback queues a transcode while the remux still holds its lock;
    the transcode's claim must not find the episode taken."""
    async with api_factory() as first, api_factory() as second:
        async with episode_lock(first, 4343, key=REMUX_LOCK_KEY) as held:
            assert held
            async with episode_lock(second, 4343) as transcode_held:
                assert transcode_held
            async with episode_lock(second, 4343, key=REMUX_LOCK_KEY) as again:
                assert not again


async def test_a_running_transcode_is_left_alone(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(
        tmp_path / "bin", monkeypatch, marker=marker, source_probe=REMUX_SOURCE_PROBE
    )
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975022)
        session.add(
            Job(type=TRANSCODE, payload={"episode_id": episode.id}, status=JobStatus.RUNNING)
        )
        await session.commit()
        job = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, job))
        await session.refresh(episode)
        assert episode.state is EpisodeState.MATCHED
    assert not marker.exists()


async def test_a_failed_remux_marks_the_episode_and_leaves_nothing_on_disk(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, fail=True, source_probe=REMUX_SOURCE_PROBE)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975023)
        job = await a_remux_job(session, episode.id)
        with pytest.raises(TranscodeError):
            await remux_episode(context(session, settings, job))

    async with api_factory() as check:
        row = await check.get(Episode, episode.id)
        assert row is not None and row.state is EpisodeState.FAILED
        stored = await check.get(Job, job.id)
        assert stored is not None
        assert "exited 1" in stored.payload["error_tail"]
        assert "stub complaint" in stored.payload["error_tail"]
    assert not (settings.renditions_dir / str(episode.id)).exists()
    assert not list(settings.renditions_dir.glob(f"{episode.id}.tmp-*"))


async def test_a_broken_playlist_fails_rather_than_publishing(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, broken=True, source_probe=REMUX_SOURCE_PROBE)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975024)
        job = await a_remux_job(session, episode.id)
        with pytest.raises(TranscodeError):
            await remux_episode(context(session, settings, job))
        await session.refresh(episode)
        assert episode.state is EpisodeState.FAILED
    assert not (settings.renditions_dir / str(episode.id)).exists()


async def test_an_episode_with_no_file_fails_with_a_readable_reason(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=REMUX_SOURCE_PROBE)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975025, with_file=False)
        job = await a_remux_job(session, episode.id)
        with pytest.raises(TranscodeError):
            await remux_episode(context(session, settings, job))
        await session.refresh(episode)
        await session.refresh(job)
        assert episode.state is EpisodeState.FAILED
        assert "no media file" in job.payload["error_tail"]


async def test_force_remuxes_a_ready_episode_again(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(
        tmp_path / "bin", monkeypatch, marker=marker, source_probe=REMUX_SOURCE_PROBE
    )
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=975026)
        await remux_episode(context(session, settings, await a_remux_job(session, episode.id)))
        forced = await a_remux_job(session, episode.id, force=True)
        await remux_episode(context(session, settings, forced))
        await session.refresh(episode)
        assert episode.state is EpisodeState.READY
    assert calls(marker) == ["remux", "remux"]


# --- Names, priorities, the sweep -------------------------------------------


async def test_the_enqueue_deduplicates_and_has_two_attempts(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=975030)
        episodes = await make_episodes(session, anime, 2)
        first = await enqueue_remux(session, episodes[0].id)
        second = await enqueue_remux(session, episodes[0].id, force=True)
        await session.commit()
        assert first.id == second.id
        assert first.max_attempts == 2
        assert first.payload["dedupe_key"] == remux_dedupe_key(episodes[0].id)


async def test_latest_prepare_jobs_spans_both_types(api_factory: SessionFactory) -> None:
    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=975031)
        episodes = await make_episodes(session, anime, 2)
        remux = Job(type=REMUX, payload={"episode_id": episodes[0].id}, status=JobStatus.DONE)
        session.add(remux)
        await session.flush()
        transcode = Job(type=TRANSCODE, payload={"episode_id": episodes[0].id})
        only_remux = Job(type=REMUX, payload={"episode_id": episodes[1].id})
        session.add_all([transcode, only_remux])
        await session.commit()

        found = await latest_prepare_jobs(session, [episodes[0].id, episodes[1].id])
        assert found[episodes[0].id].id == transcode.id
        assert found[episodes[1].id].id == only_remux.id
        assert latest_prepare_jobs is latest_transcode_jobs


async def test_the_sweep_remuxes_a_matched_episode_with_no_prepare_job(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    from arc.services.media.jobs import sweep_transcodes

    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=975032)
        episodes = await make_episodes(session, anime, 2)
        lost, after_remux = episodes
        lost.state = EpisodeState.MATCHED
        after_remux.state = EpisodeState.FAILED
        session.add(
            Job(
                type=REMUX,
                payload={"episode_id": after_remux.id},
                status=JobStatus.FAILED,
                attempts=1,
                max_attempts=2,
            )
        )
        await session.commit()

        assert await sweep_transcodes(session) == 2
        remuxes = await session.scalars(
            select(Job).where(Job.type == REMUX, Job.status == JobStatus.PENDING)
        )
        assert {job.payload["episode_id"] for job in remuxes.all()} == {lost.id, after_remux.id}
        transcodes = await session.scalars(select(Job).where(Job.type == TRANSCODE))
        assert list(transcodes.all()) == []


def test_the_master_playlist_facts() -> None:
    assert codecs_string({"profile": "High", "level": 40}, {"profile": "LC"}, audio_copy=True) == (
        "avc1.640028,mp4a.40.2"
    )
    assert codecs_string({"profile": "Main", "level": 31}, None, audio_copy=False) == "avc1.4D401F"
    assert codecs_string(
        {"profile": "High", "level": 41}, {"profile": "HE-AAC"}, audio_copy=True
    ).endswith("mp4a.40.5")
    assert codecs_string(
        {"profile": "High", "level": 41}, {"profile": "HE-AAC"}, audio_copy=False
    ).endswith("mp4a.40.2")
    playlist = "#EXTM3U\n#EXTINF:6.0,\nseg_00000.m4s\n#EXTINF:2.0,\nseg_00001.m4s\n"
    assert peak_bandwidth(playlist, [6_000_000, 1_000_000]) == 8_000_000
    assert subtitle_label("en") == "English"
    assert subtitle_label(None) == "Subtitles"
    assert subtitle_label("xx") == "xx"


async def test_a_trip_promotion_queues_a_remux(api_factory: SessionFactory, tmp_path: Path) -> None:
    """The trip-only → streaming promotion prepares by remux too (M20)."""
    from arc.services.acquisition import wants as wants_module

    async with api_factory() as session:
        anime = await make_anime(session, anilist_id=975033)
        episodes = await make_episodes(session, anime, 1)
        episode = episodes[0]
        episode.state = EpisodeState.MATCHED
        session.add(
            MediaFile(
                episode_id=episode.id,
                path=str(tmp_path / "x.mkv"),
                size=1,
                review_state=ReviewState.AUTO,
            )
        )
        await session.commit()
        promoted = await wants_module._promote(session, [episode], trip_only=[], copied=[])
        await session.commit()
        assert promoted == 1
        queued = (await session.scalars(select(Job).where(Job.type == REMUX))).all()
        assert [job.payload["episode_id"] for job in queued] == [episode.id]
