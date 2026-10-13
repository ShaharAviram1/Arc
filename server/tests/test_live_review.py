"""The progressive remux after review (M20 T6, second round).

The reviewer's findings, each pinned: no live remux without a duration and no
completion without one; pad files; the first segment's timestamp; a head too
small for its fonts; a hung or dead ffmpeg; nothing handed out before the first
segment; restarts that never empty a served playlist; the remux leaving a
running live job alone; FR-A7's "playable"; a request path without the
catalogue; the idle requeue cap; the confined open.
"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from arc.api.episode_extras import episode_extras
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Episode, EpisodeState, Job, JobStatus, Rendition
from arc.services.acquisition import demand as demand_module
from arc.services.acquisition.names import QBIT_SEQUENTIAL
from arc.services.media import live as live_module
from arc.services.media import remux as remux_module
from arc.services.media.copies import SOURCE_READER_TYPES
from arc.services.media.live import (
    MAX_IDLE_REQUEUES,
    REFUSED_NO_DURATION,
    REFUSED_PADDED,
    REFUSED_UNREADABLE,
    LiveRefused,
    open_confined,
    remux_live,
)
from arc.services.media.names import (
    REMUX_LIVE,
    STAGE_LIVE_DONE,
    STAGE_LIVE_GONE,
    STAGE_LIVE_IDLE,
    STAGE_LIVE_REFUSED,
    RemuxInfo,
    live_dir_for,
)
from arc.services.media.remux import remux_episode
from arc.services.playback.progress import record_progress
from tests.acquisition_helpers import acquisition_settings, make_user
from tests.live_helpers import KIB, MIB, a_download, install_live_ffmpeg, write_served
from tests.media_helpers import REMUX_SOURCE_PROBE
from tests.test_live_jobs import (  # noqa: F401  (the fixtures are used by name)
    _covering,
    _fast,
    a_live_job,
    a_remux_job,
    complete_and_link,
    context,
    finish,
    run_once,
    sure,
)

pytestmark = pytest.mark.pg


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return acquisition_settings(tmp_path / "data", live_head_bytes=1 * MIB)


# --- 1. The duration ----------------------------------------------------------------


async def test_a_head_without_a_duration_is_refused(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "fed.bin"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record)
    sure(monkeypatch)
    no_duration = copy.deepcopy(REMUX_SOURCE_PROBE)
    no_duration["format"] = {"format_name": "matroska,webm"}

    async def probe(binary: str, head: bytes) -> dict[str, Any]:
        return no_duration

    monkeypatch.setattr(live_module, "probe_head", probe)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978001)
        download.advance(per_poll=100)
    payload = await run_once(api_factory, settings, download.episode.id)
    assert payload["stage"] == STAGE_LIVE_REFUSED
    assert payload["reason"] == REFUSED_NO_DURATION
    assert not record.exists()
    assert not live_dir_for(settings, download.episode.id).exists()


async def test_no_completion_while_live_without_an_expected_duration(
    api_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978002)
        user = await make_user(session, "live-guard@arc.test")
        await a_live_job(session, download.episode.id)  # running, nothing probed yet
        episode = await session.get(Episode, download.episode.id)
        assert episode is not None
        outcome = await record_progress(
            session, user_id=user.id, episode=episode, position_s=1400.0, duration_s=1440.0
        )
        await session.commit()
        assert outcome.completed is False


# --- 2. Pad files ----------------------------------------------------------------------


async def test_a_file_behind_a_hidden_pad_file_is_refused(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "fed.bin"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978003)
        info_hash = download.torrent.info_hash.lower()
        size = len(download.data)
        # A 100-byte .nfo listed first, then (hidden) padding to the piece
        # boundary: the episode starts at piece 1, the listed sizes say 0.
        download.stub.file_lists[info_hash] = [
            {"index": 0, "name": "notes.nfo", "size": 100, "priority": 1, "progress": 0.0,
             "piece_range": [0, 0]},
            {"index": 1, "name": download.file_name, "size": size, "priority": 1,
             "progress": 0.0, "piece_range": [1, 1 + size // download.piece_size]},
        ]  # fmt: skip
        download.advance(per_poll=100)
    payload = await run_once(api_factory, settings, download.episode.id)
    assert payload["stage"] == STAGE_LIVE_REFUSED
    assert payload["reason"] == REFUSED_PADDED
    assert not record.exists()


# --- 3. The first segment's timestamp ----------------------------------------------------


async def test_the_timestamp_comes_from_the_first_segment_not_the_playlist(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin")
    sure(monkeypatch)
    probed: list[tuple[str, bytes]] = []

    async def fake_probe(path: Path | str, *, binary: str = "ffprobe") -> dict[str, Any]:
        target = Path(path)
        probed.append((target.name, target.read_bytes()))
        return {"streams": [{"codec_type": "video", "start_time": "0.083000"}]}

    monkeypatch.setattr(live_module, "ffprobe_json", fake_probe)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978004)
        download.advance(per_poll=2)
    await run_once(api_factory, settings, download.episode.id)
    assert probed, "the first segment was probed"
    assert all(name == "first.mp4" for name, _ in probed)
    name, data = probed[0]
    directory = live_dir_for(settings, download.episode.id)
    init = (directory / "init.mp4").read_bytes()
    assert data == init + (directory / "seg_00000.m4s").read_bytes()
    info = RemuxInfo.from_json((directory / "remux.json").read_text())
    assert info.mpegts == round(0.083 * 90_000)
    assert not (directory / "_work" / "first.mp4").exists()


# --- 4. A head too small for its fonts -------------------------------------------------


async def test_the_head_grows_until_it_probes(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin")
    sure(monkeypatch)
    sizes: list[int] = []

    async def probe(binary: str, head: bytes) -> dict[str, Any] | None:
        sizes.append(len(head))
        return REMUX_SOURCE_PROBE if len(head) >= 2 * MIB else None

    monkeypatch.setattr(live_module, "probe_head", probe)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978005)
        download.advance(per_poll=1)
    payload = await run_once(api_factory, settings, download.episode.id)
    assert payload["stage"] == STAGE_LIVE_DONE
    assert sizes[0] == 1 * MIB and sizes[-1] == 2 * MIB


async def test_a_head_that_never_probes_is_refused_once_the_file_is_in(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin")
    sure(monkeypatch)

    async def probe(binary: str, head: bytes) -> None:
        return None

    monkeypatch.setattr(live_module, "probe_head", probe)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978006)
        download.advance(per_poll=100)
    payload = await run_once(api_factory, settings, download.episode.id)
    assert payload["stage"] == STAGE_LIVE_REFUSED
    assert payload["reason"] == REFUSED_UNREADABLE


# --- 7. Restarts never empty a served playlist -----------------------------------------


async def test_a_restart_that_has_not_caught_up_leaves_the_served_playlist(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_live_ffmpeg(
        tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin", segment_bytes=512 * KIB
    )
    sure(monkeypatch)
    idle = settings.model_copy(update={"live_idle_minutes": 0})
    async with api_factory() as session:
        download = await a_download(session, idle, monkeypatch, anilist_id=978007)
        target = live_dir_for(idle, download.episode.id)
        write_served(target, segments=5)
        before = {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()}
        download.stall_after(6)  # 1.5 MiB: three new segments, fewer than five
        job = await a_live_job(session, download.episode.id)
        await remux_live(context(session, idle, job))
        await session.refresh(job)
    assert job.payload["stage"] == STAGE_LIVE_IDLE
    after = {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()}
    assert after == before
    assert not target.with_name(target.name + ".new").exists()


async def test_a_restart_that_catches_up_is_swapped_in(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "fed.bin"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978008)
        target = live_dir_for(settings, download.episode.id)
        write_served(target, segments=2)
        download.advance(per_poll=1)
        job = await a_live_job(session, download.episode.id)
        await remux_live(context(session, settings, job))
        await session.refresh(job)
    assert job.payload["stage"] == STAGE_LIVE_DONE
    assert record.read_bytes() == download.data
    text = (target / "video.m3u8").read_text()
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
    assert not (target / "seg_00000.m4s").read_bytes().startswith(b"OLD")
    assert not target.with_name(target.name + ".new").exists()
    assert not target.with_name(target.name + ".old").exists()


@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.DONE])
async def test_a_demand_does_not_restart_a_running_or_finished_live_remux(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    status: JobStatus,
) -> None:
    async with api_factory() as session:
        download = await a_download(
            session, settings, monkeypatch, anilist_id=978009 + (status is JobStatus.DONE)
        )
        job = await a_live_job(session, download.episode.id)
        job.status = status
        job.payload = {**job.payload, "stage": STAGE_LIVE_DONE}
        await session.commit()
        user = await make_user(session, f"live-again-{status.value}@arc.test")
        await session.commit()
        monkeypatch.setattr(demand_module, "coverable", _covering(download.episode.id))
        episode = await session.get(Episode, download.episode.id)
        assert episode is not None
        outcome = await demand_module.demand(
            session, user_id=user.id, episode=episode, now=datetime.now(UTC), settings=settings
        )
        await session.commit()
        assert outcome.live is True
        types = (await session.scalars(select(Job.type))).all()
    assert types.count(REMUX_LIVE) == 1
    assert QBIT_SEQUENTIAL in types


# --- 8. The remux leaves a running live job alone ---------------------------------------


async def test_the_remux_does_not_delete_a_live_directory_still_being_written(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "calls.txt"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin", marker=marker)
    monkeypatch.setattr(remux_module, "LIVE_ADOPT_WAIT_SECONDS", 0.05)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978011)
        directory = live_dir_for(settings, download.episode.id)
        write_served(directory, segments=3, ended=True)
        await a_live_job(session, download.episode.id)  # still running
        await complete_and_link(session, download.episode.id, download.source)
        job = await a_remux_job(session, download.episode.id)
        await remux_episode(context(session, settings, job))
        episode = await session.get(Episode, download.episode.id, populate_existing=True)
        assert episode is not None and episode.state is EpisodeState.READY
    # The ordinary remux ran, and the live directory was left to its job.
    assert marker.read_text().split() == ["remux"]
    assert directory.is_dir()


# --- 10. FR-A7's "playable" ---------------------------------------------------------------


async def test_live_playable_on_the_episode_extras(
    api_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978012)
        job = await a_live_job(session, download.episode.id)
        directory = live_dir_for(settings, download.episode.id)
        directory.mkdir(parents=True)
        extras = await episode_extras(session, [download.episode.id], live_settings=settings)
        assert extras.live_playable == frozenset()  # nothing to play yet
        write_served(directory, segments=1)
        extras = await episode_extras(session, [download.episode.id], live_settings=settings)
        assert extras.live_playable == {download.episode.id}
        # Without the settings nothing is looked at.
        assert (await episode_extras(session, [download.episode.id])).live_playable == set()
        job.payload = {**job.payload, "stage": STAGE_LIVE_REFUSED}
        job.status = JobStatus.DONE
        await session.commit()
        extras = await episode_extras(session, [download.episode.id], live_settings=settings)
        assert extras.live_playable == frozenset()


# --- Nits ---------------------------------------------------------------------------------


async def test_idle_requeues_are_capped(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin")
    sure(monkeypatch)
    idle = settings.model_copy(update={"live_idle_minutes": 0})
    async with api_factory() as session:
        download = await a_download(session, idle, monkeypatch, anilist_id=978013)
        download.stall_after(6)
        job = await a_live_job(session, download.episode.id)
        job.payload = {**job.payload, "requeues": MAX_IDLE_REQUEUES}
        await session.commit()
        await remux_live(context(session, idle, job))
        await session.refresh(job)
        others = (await session.scalars(select(Job.id).where(Job.id != job.id))).all()
    assert job.payload["stage"] == STAGE_LIVE_GONE
    assert "no progress" in job.payload["reason"]
    assert others == []
    assert not live_dir_for(idle, download.episode.id).exists()


def test_the_open_is_confined_to_what_was_opened(tmp_path: Path) -> None:
    root = tmp_path / "downloads" / "7"
    root.mkdir(parents=True)
    inside = root / "ep.mkv"
    inside.write_bytes(b"x")
    os.close(open_confined(inside, root))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "ep.mkv").write_bytes(b"y")
    (root / "linked").symlink_to(outside)
    if live_module.fd_path(os.open(inside, os.O_RDONLY)) is None:  # pragma: no cover
        pytest.skip("this platform cannot say where a descriptor points")
    with pytest.raises(LiveRefused):
        open_confined(root / "linked" / "ep.mkv", root)


def test_retention_counts_the_live_remux_as_reading_the_source() -> None:
    assert REMUX_LIVE in SOURCE_READER_TYPES


# --- Validator A: the subtitle file grows while the episode downloads ----------------------


async def test_the_subtitle_track_reaches_the_disk_before_the_download_ends(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "fed.bin"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record)
    sure(monkeypatch)
    seen: list[tuple[int, int]] = []
    original = live_module._Poller.__call__

    async def spy(self: live_module._Poller) -> live_module.Front:
        front = await original(self)
        sub = self.directory / "_work" / "sub.ass"
        fed = record.stat().st_size if record.exists() else 0
        seen.append((fed, sub.stat().st_size if sub.exists() else 0))
        return front

    monkeypatch.setattr(live_module._Poller, "__call__", spy)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978020)
        download.advance(per_poll=1)
    payload = await run_once(api_factory, settings, download.episode.id)
    assert payload["stage"] == STAGE_LIVE_DONE
    total = len(download.data)
    assert any(0 < fed < total and size > 0 for fed, size in seen), seen


# --- Validator B/E: adoption without a gap, and without waiting -----------------------------


async def test_ready_is_committed_before_the_live_directory_is_renamed(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop the adoption at the rename: ``ready`` and the row are already committed."""
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin")
    sure(monkeypatch)

    def interrupted(settings_: Settings, episode_id: int) -> Path:
        raise RuntimeError("stopped at the rename")

    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978021)
        download.advance(per_poll=100)
        live_job = await a_live_job(session, download.episode.id)
        await remux_live(context(session, settings, live_job))
        await finish(session, live_job)
        await complete_and_link(session, download.episode.id, download.source)
        job = await a_remux_job(session, download.episode.id)
        monkeypatch.setattr(live_module, "publish_adopted", interrupted)
        with pytest.raises(RuntimeError):
            await remux_episode(context(session, settings, job))
    async with api_factory() as other:
        state = await other.scalar(select(Episode.state).where(Episode.id == download.episode.id))
        row = await other.scalar(
            select(Rendition.id).where(Rendition.episode_id == download.episode.id)
        )
    assert state is EpisodeState.READY and row is not None
    # The live directory is still there, whole, for the ready route to fall back on.
    assert (live_dir_for(settings, download.episode.id) / "video.m3u8").is_file()


async def test_a_finished_live_directory_is_adopted_at_once_under_a_running_job(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ENDLIST`` written, the job still finishing: adopted, no wait, no second ffmpeg."""
    marker = tmp_path / "calls.txt"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin", marker=marker)
    sure(monkeypatch)
    monkeypatch.setattr(remux_module, "LIVE_ADOPT_WAIT_SECONDS", 3600.0)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=978022)
        download.advance(per_poll=100)
        live_job = await a_live_job(session, download.episode.id)
        await remux_live(context(session, settings, live_job))  # left "running"
        await complete_and_link(session, download.episode.id, download.source)
        job = await a_remux_job(session, download.episode.id)
        await asyncio_wait(remux_episode(context(session, settings, job)), 10)
        await session.refresh(job)
        assert job.payload.get("adopted") is True
    assert marker.read_text().split() == ["live"]


async def asyncio_wait(awaitable: Any, seconds: float) -> None:
    import asyncio

    await asyncio.wait_for(awaitable, timeout=seconds)


# --- Validator D: the remaining gaps ---------------------------------------------------------


async def test_a_name_below_the_auto_link_threshold_does_not_pre_match(
    api_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.models import Anime
    from arc.services.library import prematch as prematch_module
    from arc.services.library.matcher import MatchResult, Scored

    async with api_factory() as session:
        anime = Anime(
            anilist_id=978023, title_romaji="Live Show", format="TV", episodes=12,
            status="FINISHED",
        )  # fmt: skip
        session.add(anime)
        await session.flush()
        unsure = MatchResult(
            candidates=(Scored(anime.id, 3, 0.62, title=0.7, prior=True),), confidence=0.62
        )

        async def fake_match(*args: object, **kwargs: object) -> MatchResult:
            return unsure

        class _Holder:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(prematch_module, "match", fake_match)
        monkeypatch.setattr(prematch_module, "catalog_for", lambda settings: _Holder())
        verdict = await prematch_module.prematch(
            session, settings, "[Group] Live Show - 03 (1080p).mkv", anime_id=anime.id, number=3
        )
    assert not verdict.ok
    assert verdict.reason == prematch_module.REFUSED_LOW_CONFIDENCE
    assert verdict.confidence == pytest.approx(0.62)


async def test_the_worker_start_and_the_hourly_sweep_remove_leftovers(
    api_factory: SessionFactory, settings: Settings
) -> None:
    from arc import worker
    from arc.services.retention.jobs import retention_sweep

    orphan = live_dir_for(settings, 999_001)
    orphan.mkdir(parents=True)
    await worker._sweep_live_leftovers(api_factory, settings)
    assert not orphan.exists()

    orphan.mkdir(parents=True)
    async with api_factory() as session:
        job = Job(type="retention_sweep", payload={}, status=JobStatus.RUNNING, attempts=1)
        session.add(job)
        await session.commit()
        await retention_sweep(context(session, settings, job))
    assert not orphan.exists()
