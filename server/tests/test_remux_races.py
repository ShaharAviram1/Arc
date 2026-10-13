"""A remux and a transcode of one episode, and the edges between them (M20 T2 review).

The two prepare jobs hold different advisory locks (the remux's fallback
queues a transcode while it still holds its own), so they are kept apart by
looking at each other's job rows: a remux steps aside for a running
transcode, a transcode defers to a running remux, and neither deletes the
other's staging directory. Same fixtures and fake ffmpeg as
:mod:`tests.test_remux_jobs`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Episode, EpisodeState, Job, JobStatus, Rendition
from arc.services.media.jobs import (
    PrepareDeferred,
    discard_stale_staging,
    transcode_episode,
)
from arc.services.media.names import (
    REMUX,
    STAGE_DEFERRED,
    TRANSCODE,
    RemuxInfo,
    latest_transcode_jobs,
    output_dir_for,
)
from arc.services.media.remux import KEY_TRANSCODE_JOB, remux_episode
from arc.services.media.transcode import TranscodeError, reset_semaphore
from tests.acquisition_helpers import acquisition_settings
from tests.conftest import add_user, api_transport, login
from tests.media_helpers import HI10P_SOURCE_PROBE, REMUX_SOURCE_PROBE, install_fake_ffmpeg
from tests.test_remux_jobs import a_remux_job
from tests.test_transcode_jobs import a_job, a_matched_episode, context

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _fresh_semaphore() -> None:
    reset_semaphore()


def running_job(job_type: str, episode_id: int, **payload: object) -> Job:
    return Job(
        type=job_type,
        payload={"episode_id": episode_id, **payload},
        status=JobStatus.RUNNING,
        attempts=1,
    )


# --- Staging directories ----------------------------------------------------


async def test_only_a_dead_jobs_staging_is_discarded(
    api_factory: SessionFactory, tmp_path: Path
) -> None:
    settings = acquisition_settings(tmp_path)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=978001, with_file=False)
        alive = running_job(TRANSCODE, episode.id, stage="encode")
        dead = Job(type=REMUX, payload={"episode_id": episode.id}, status=JobStatus.FAILED)
        mine = running_job(REMUX, episode.id)
        session.add_all([alive, dead, mine])
        await session.commit()

        output_dir = output_dir_for(settings, episode.id)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        dirs = {
            name: output_dir.parent / f"{episode.id}.tmp-{name}"
            for name in (str(alive.id), str(dead.id), str(mine.id), "999999", "junk")
        }
        for path in dirs.values():
            path.mkdir()

        await discard_stale_staging(session, output_dir, episode.id, mine.id)

    assert dirs[str(alive.id)].exists(), "a running job's work in progress is never touched"
    assert not dirs[str(dead.id)].exists()
    assert not dirs[str(mine.id)].exists(), "this job's own leftover (a retry) goes"
    assert not dirs["999999"].exists(), "a job row that is gone is dead"
    assert not dirs["junk"].exists()


# --- Transcode defers to a running remux -------------------------------------


async def test_a_transcode_defers_to_a_running_remux(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admin's ``mode=transcode`` while a remux is writing: retried, nothing touched."""
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, marker=marker)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=978010)
        remux = running_job(REMUX, episode.id, stage="remux")
        session.add(remux)
        await session.commit()
        staging = output_dir_for(settings, episode.id).with_suffix(f".tmp-{remux.id}")
        staging.mkdir(parents=True)
        (staging / "seg_00000.m4s").write_bytes(b"in progress")

        job = await a_job(session, episode.id, force=True)
        with pytest.raises(PrepareDeferred):
            await transcode_episode(context(session, settings, job))

        await session.refresh(episode)
        assert episode.state is EpisodeState.MATCHED
    assert (staging / "seg_00000.m4s").read_bytes() == b"in progress"
    assert not marker.exists(), "no ffmpeg ran"


@pytest.mark.parametrize("stage", ["fallback", "deferred", "done"])
async def test_a_remux_that_is_not_writing_does_not_hold_the_transcode(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """The remux that handed over is still ``running`` when its transcode is claimed."""
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=978011 + len(stage))
        session.add(running_job(REMUX, episode.id, stage=stage))
        await session.commit()
        job = await a_job(session, episode.id)
        await transcode_episode(context(session, settings, job))
        await session.refresh(episode)
        assert episode.state is EpisodeState.READY


# --- Remux steps aside for a running transcode -------------------------------


async def test_a_remux_steps_aside_for_a_running_transcode_and_says_so(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(
        tmp_path / "bin", monkeypatch, marker=marker, source_probe=REMUX_SOURCE_PROBE
    )
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=978020)
        transcode = running_job(TRANSCODE, episode.id, stage="encode", progress=0.4)
        session.add(transcode)
        await session.commit()
        staging = output_dir_for(settings, episode.id).with_suffix(f".tmp-{transcode.id}")
        staging.mkdir(parents=True)

        remux = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, remux))

        await session.refresh(remux)
        await session.refresh(episode)
        assert remux.payload["stage"] == STAGE_DEFERRED
        assert remux.payload[KEY_TRANSCODE_JOB] == transcode.id
        assert episode.state is EpisodeState.MATCHED
        # The episode keeps showing the encode that is working, not the
        # newer remux row that did nothing.
        latest = (await latest_transcode_jobs(session, [episode.id]))[episode.id]
        assert latest.id == transcode.id
    assert staging.exists()
    assert not marker.exists()


async def test_a_second_claim_that_cannot_lock_writes_deferred(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.services.media.jobs import episode_lock
    from arc.services.media.remux import REMUX_LOCK_KEY

    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=REMUX_SOURCE_PROBE)
    async with api_factory() as setup:
        episode = await a_matched_episode(setup, settings, anilist_id=978021)
        second = await a_remux_job(setup, episode.id)
    async with api_factory() as owner:
        async with episode_lock(owner, episode.id, key=REMUX_LOCK_KEY):
            async with api_factory() as session:
                await remux_episode(context(session, settings, second))
    async with api_factory() as check:
        row = await check.get(Job, second.id)
        assert row is not None and row.payload["stage"] == STAGE_DEFERRED


# --- The hand-over carries the reason and the force ---------------------------


async def test_a_forced_fallback_onto_a_pending_transcode_keeps_the_force(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=HI10P_SOURCE_PROBE)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=978030)
        pending = Job(
            type=TRANSCODE,
            payload={"episode_id": episode.id, "dedupe_key": f"transcode:{episode.id}"},
        )
        session.add(pending)
        await session.commit()

        remux = await a_remux_job(session, episode.id, force=True)
        await remux_episode(context(session, settings, remux))

        await session.refresh(pending)
        assert pending.payload["force"] is True
        assert pending.payload["fallback_reason"] == "10-bit"
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


async def test_the_episode_says_why_it_is_a_full_encode(
    api_app: FastAPI, api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=HI10P_SOURCE_PROBE)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=978031)
        remux = await a_remux_job(session, episode.id)
        await remux_episode(context(session, settings, remux))
        anime_id = episode.anime_id
        episode_id = episode.id

    await add_user(api_factory, "note@arc.test", "viewer-password")
    async with api_transport(api_app) as client:
        await login(client, "note@arc.test", "viewer-password")
        body = (await client.get(f"/api/anime/{anime_id}")).json()
    row = next(item for item in body["episodes"] if item["id"] == episode_id)
    assert row["state"] == "matched"
    assert row["prepare_note"] == "10-bit"

    # And once the encode is running, the note stays with its progress.
    async with api_factory() as session:
        stored = await session.get(Episode, episode_id)
        assert stored is not None
        stored.state = EpisodeState.PREPARING
        await session.commit()
    async with api_transport(api_app) as client:
        await login(client, "note@arc.test", "viewer-password")
        body = (await client.get(f"/api/anime/{anime_id}")).json()
    row = next(item for item in body["episodes"] if item["id"] == episode_id)
    assert row["prepare_note"] == "10-bit"
    assert row["prepare_progress"] == 0.0


# --- Smaller edges ------------------------------------------------------------


async def test_a_forced_remux_without_a_source_leaves_a_playable_episode_ready(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = acquisition_settings(tmp_path)
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=REMUX_SOURCE_PROBE)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=978040)
        await remux_episode(context(session, settings, await a_remux_job(session, episode.id)))
        await session.refresh(episode)
        assert episode.state is EpisodeState.READY
        source = settings.downloads_dir / str(episode.id)
        for path in source.iterdir():
            path.unlink()

        forced = await a_remux_job(session, episode.id, force=True)
        with pytest.raises(TranscodeError):
            await remux_episode(context(session, settings, forced))
        await session.refresh(episode)
        await session.refresh(forced)
        assert episode.state is EpisodeState.READY
        assert "missing" in forced.payload["error_tail"]
        assert await session.scalar(select(Rendition).where(Rendition.episode_id == episode.id))


def test_the_sidecar_refuses_booleans_for_numbers() -> None:
    info = RemuxInfo.from_json('{"mpegts": true, "bandwidth": true, "codecs": "avc1.640028"}')
    assert info.mpegts == 0
    assert info.bandwidth == 0
    assert RemuxInfo.from_json("not json") == RemuxInfo()


def test_the_remux_timeout_is_its_own_setting(tmp_path: Path) -> None:
    settings: Settings = acquisition_settings(tmp_path)
    assert settings.remux_timeout_seconds == 600.0
    assert settings.remux_timeout_seconds < settings.transcode_timeout_seconds


async def test_the_remux_runs_ffmpeg_with_the_remux_timeout(
    api_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.services.media import remux as remux_module

    settings = acquisition_settings(tmp_path).model_copy(update={"remux_timeout_seconds": 42.0})
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, source_probe=REMUX_SOURCE_PROBE)
    seen: list[float] = []
    original = remux_module.run_ffmpeg

    async def spy(*args: object, **kwargs: object) -> str:
        seen.append(float(kwargs["timeout"]))  # type: ignore[arg-type]
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(remux_module, "run_ffmpeg", spy)
    async with api_factory() as session:
        episode = await a_matched_episode(session, settings, anilist_id=978041)
        await remux_episode(context(session, settings, await a_remux_job(session, episode.id)))
    assert seen == [42.0]
