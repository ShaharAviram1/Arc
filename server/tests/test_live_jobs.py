"""The ``remux_live`` handler and what surrounds it, against the database (M20 T6).

qBittorrent is :class:`tests.acquisition_helpers.QbitStub` with pieces that
arrive as the job polls; ffmpeg is the live fake of :mod:`tests.live_helpers`,
which records every byte it is fed. The real ffmpeg is ``test_live_slow``'s.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    RENDITION_REMUX,
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    Rendition,
    ReviewState,
    Torrent,
    TorrentKind,
)
from arc.services.acquisition import demand as demand_module
from arc.services.acquisition.jobs import qbit_sequential
from arc.services.acquisition.names import QBIT_SEQUENTIAL
from arc.services.catalog import Breaker, CatalogService
from arc.services.jobs.registry import JobContext
from arc.services.library import prematch as prematch_module
from arc.services.library.prematch import PreMatch
from arc.services.media import live as live_module
from arc.services.media import remux as remux_module
from arc.services.media.live import remux_live, sweep_live_leftovers
from arc.services.media.names import (
    LIVE_OWNER_NAME,
    REMUX,
    REMUX_LIVE,
    STAGE_LIVE_DONE,
    STAGE_LIVE_GONE,
    STAGE_LIVE_IDLE,
    STAGE_LIVE_REFUSED,
    live_dedupe_key,
    live_dir_for,
    output_dir_for,
)
from arc.services.media.remux import KEY_ADOPTED, remux_episode
from arc.services.media.transcode import reset_semaphore
from arc.services.playback.progress import record_progress
from tests.acquisition_helpers import acquisition_settings, make_user
from tests.anilist_mock import FakeAniList, frieren_fake
from tests.live_helpers import KIB, MIB, a_download, install_live_ffmpeg, write_served
from tests.mal_mock import frieren_fake as mal_frieren_fake

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll every 10 ms instead of every 2 s; no wait before adoption."""
    reset_semaphore()
    monkeypatch.setattr(live_module, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(live_module, "PAYLOAD_SECONDS", 0.0)
    monkeypatch.setattr(remux_module, "LIVE_ADOPT_POLL_SECONDS", 0.01)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return acquisition_settings(tmp_path / "data", live_head_bytes=1 * MIB)


def sure(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True, reason: str | None = None) -> None:
    """Make the pre-match gate answer without a catalogue."""

    async def fake(*args: object, **kwargs: object) -> PreMatch:
        return PreMatch(ok, None if ok else reason or "no", 0.97 if ok else 0.4)

    monkeypatch.setattr(live_module, "prematch", fake)


def context(session: AsyncSession, settings: Settings, job: Job) -> JobContext:
    return JobContext(job=job, session=session, settings=settings, log=logging.getLogger("t"))


async def a_live_job(session: AsyncSession, episode_id: int) -> Job:
    job = Job(
        type=REMUX_LIVE,
        payload={"episode_id": episode_id, "dedupe_key": live_dedupe_key(episode_id)},
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=3,
        locked_by="test:1",
        locked_at=datetime.now(UTC),
    )
    session.add(job)
    await session.commit()
    return job


async def finish(session: AsyncSession, job: Job) -> None:
    """What the runner does after a handler returns."""
    await session.execute(
        update(Job).where(Job.id == job.id).values(status=JobStatus.DONE, locked_at=None)
    )
    await session.commit()


# --- The happy path ------------------------------------------------------------------


async def test_a_demanded_single_is_remuxed_as_it_downloads_and_never_past_the_front(
    api_factory: SessionFactory,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "fed.bin"
    marker = tmp_path / "calls.txt"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record, marker=marker)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976001)
        download.advance(per_poll=2)
        # Every byte fed must already be covered by checked pieces at that
        # moment: wrap the poller to remember the largest front reported.
        fronts: list[int] = []
        original = live_module._Poller.__call__

        async def spy(self: live_module._Poller) -> live_module.Front:
            front = await original(self)
            fronts.append(front.bytes)
            if record.exists():
                assert record.stat().st_size <= max(fronts)
            return front

        monkeypatch.setattr(live_module._Poller, "__call__", spy)
        job = await a_live_job(session, download.episode.id)
        await remux_live(context(session, settings, job))

        await session.refresh(job)
        payload = job.payload
        assert payload["stage"] == STAGE_LIVE_DONE
        assert payload["expected_duration"] == pytest.approx(12.0)
        assert payload["file_size"] == len(download.data)
        assert payload["front_bytes"] == len(download.data)
        assert payload["segments"] >= 6
        assert payload["front_seconds"] == pytest.approx(6.0 * payload["segments"])
        assert payload["seeders"] == 7
        assert payload["width"] == 1920 and payload["subtitle_lang"] == "en"

    # ffmpeg was fed the file exactly, in order, once.
    assert record.read_bytes() == download.data
    argv = (tmp_path / "calls.txt.argv").read_text().split("\0")
    assert "pipe:0" in argv and "event" in argv
    directory = live_dir_for(settings, download.episode.id)
    text = (directory / "video.m3u8").read_text()
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
    owner = json.loads((directory / LIVE_OWNER_NAME).read_text())
    assert owner["file_size"] == len(download.data)
    assert owner["path"] == str(download.source.resolve())
    assert (directory / "remux.json").is_file()
    assert (directory / "_work" / "sub.ass").is_file()
    # The front only ever covered checked pieces from the first byte.
    assert fronts == sorted(fronts)


# --- Refusals ------------------------------------------------------------------------


async def run_once(
    factory: SessionFactory, settings: Settings, episode_id: int
) -> dict[str, object]:
    async with factory() as session:
        job = await a_live_job(session, episode_id)
        await remux_live(context(session, settings, job))
        await session.refresh(job)
        return dict(job.payload)


async def test_an_mp4_is_not_remuxed_live(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "fed.bin"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(
            session,
            settings,
            monkeypatch,
            anilist_id=976002,
            file_name="[Group] Sousou no Frieren - 03 (1080p).mp4",
        )
        download.advance(per_poll=100)
    payload = await run_once(api_factory, settings, download.episode.id)
    assert payload["stage"] == STAGE_LIVE_REFUSED
    assert payload["reason"] == live_module.REFUSED_NOT_MKV
    assert not record.exists()
    assert not live_dir_for(settings, download.episode.id).exists()


async def test_a_batch_backed_episode_is_not_remuxed_live(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "fed.bin"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(
            session, settings, monkeypatch, anilist_id=976003, kind=TorrentKind.BATCH
        )
    payload = await run_once(api_factory, settings, download.episode.id)
    assert payload["stage"] == STAGE_LIVE_REFUSED
    assert payload["reason"] == live_module.REFUSED_NOT_SINGLE
    assert not record.exists()


async def test_an_episode_nobody_pressed_play_on_is_not_remuxed_live(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin")
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(
            session, settings, monkeypatch, anilist_id=976004, demanded=False
        )
    payload = await run_once(api_factory, settings, download.episode.id)
    assert payload["stage"] == STAGE_LIVE_REFUSED
    assert payload["reason"] == live_module.REFUSED_NOT_DEMANDED


@pytest.fixture
def frieren_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real matcher over the AniList and MAL fakes (as ``test_match_job`` does)."""
    anilist: FakeAniList = frieren_fake()
    anilist.search["sousou no frieren"] = anilist.search["frieren"]
    service = CatalogService(anilist.source(), mal_frieren_fake().source(), Breaker(300.0))

    class _Holder:
        async def __aenter__(self) -> CatalogService:
            return service

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(prematch_module, "catalog_for", lambda settings: _Holder())


async def season_two(session: AsyncSession, anilist_id: int) -> Anime:
    anime = Anime(
        anilist_id=anilist_id,
        title_romaji="Mairimashita! Iruma-kun Season 2",
        format="TV",
        episodes=21,
        status="FINISHED",
    )
    session.add(anime)
    await session.flush()
    return anime


async def test_the_pre_match_gate_with_the_real_matcher(
    api_factory: SessionFactory, settings: Settings, frieren_catalog: None
) -> None:
    """The gate is the matcher's own decision on the in-flight name, prior and all."""
    async with api_factory() as session:
        anime = await season_two(session, 976010)
        sure_name = "[SubsPlease] Mairimashita! Iruma-kun S2 - 11 (1080p).mkv"
        wrong_season = "[SubsPlease] Mairimashita! Iruma-kun S4 - 11 (1080p).mkv"
        other_episode = "[SubsPlease] Mairimashita! Iruma-kun S2 - 12 (1080p).mkv"
        good = await prematch_module.prematch(
            session, settings, sure_name, anime_id=anime.id, number=11
        )
        bad = await prematch_module.prematch(
            session, settings, wrong_season, anime_id=anime.id, number=11
        )
        elsewhere = await prematch_module.prematch(
            session, settings, other_episode, anime_id=anime.id, number=11
        )
        batch = await prematch_module.prematch(
            session,
            settings,
            "[SubsPlease] Mairimashita! Iruma-kun S2 (01-21) (1080p) [Batch]",
            anime_id=anime.id,
            number=11,
        )
        # Nothing was indexed by any of it.
        assert await session.scalar(select(MediaFile.id).limit(1)) is None
    assert good.ok, good
    assert not bad.ok and bad.reason == "file names season 4, the download expected season 2"
    assert not elsewhere.ok
    assert not batch.ok


async def test_a_pre_match_refusal_means_no_live_remux_and_demand_says_so(
    api_factory: SessionFactory,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frieren_catalog: None,
) -> None:
    record = tmp_path / "fed.bin"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record)
    async with api_factory() as session:
        download = await a_download(
            session,
            settings,
            monkeypatch,
            anilist_id=976011,
            file_name="[SubsPlease] Mairimashita! Iruma-kun S4 - 03 (1080p).mkv",
        )
        anime = await season_two(session, 976012)
        episode = await session.get(Episode, download.episode.id)
        assert episode is not None
        episode.anime_id = anime.id
        await session.commit()
        download.advance(per_poll=100)

        user = await make_user(session, "live-demander@arc.test")
        await session.commit()
        monkeypatch.setattr(demand_module, "coverable", _covering(episode.id))
        # The request path never asks the catalogue (it holds the want lock).
        lookups: list[object] = []
        real_catalog_for = prematch_module.catalog_for

        def counting(settings: Settings) -> object:
            lookups.append(settings)
            return real_catalog_for(settings)

        monkeypatch.setattr(prematch_module, "catalog_for", counting)
        outcome = await demand_module.demand(
            session, user_id=user.id, episode=episode, now=datetime.now(UTC), settings=settings
        )
        await session.commit()
        assert lookups == []
        # Optimistic until the live remux has looked at the file.
        assert outcome.live is True and outcome.live_note is None
        queued = (
            await session.scalars(
                select(Job).where(Job.type.in_([REMUX_LIVE, QBIT_SEQUENTIAL])).order_by(Job.id)
            )
        ).all()
        assert sorted(job.type for job in queued) == [QBIT_SEQUENTIAL, REMUX_LIVE]
        live_job = next(job for job in queued if job.type == REMUX_LIVE)
        live_job.status = JobStatus.RUNNING
        await session.commit()

        # The job refuses on the file's own name (the real matcher).
        await remux_live(context(session, settings, live_job))
        await finish(session, live_job)
        await session.refresh(live_job)
        assert live_job.payload["stage"] == STAGE_LIVE_REFUSED
        assert "season 4" in str(live_job.payload["reason"])
        assert lookups  # the gate ran in the job

        # And now the demand says it will play when the download finishes,
        # without queueing the refused remux again.
        again = await demand_module.demand(
            session, user_id=user.id, episode=episode, now=datetime.now(UTC), settings=settings
        )
        await session.commit()
        assert again.live is False
        assert again.live_note == demand_module.PLAYS_WHEN_DOWNLOADED_MESSAGE
        count = len((await session.scalars(select(Job.id).where(Job.type == REMUX_LIVE))).all())
        assert count == 1
    assert not record.exists()
    assert not live_dir_for(settings, download.episode.id).exists()


def _covering(*episode_ids: int):  # type: ignore[no-untyped-def]
    async def coverable(session: object, user_id: int, *, now: datetime) -> set[int]:
        return set(episode_ids)

    return coverable


async def test_a_demand_on_a_downloading_single_queues_sequential_and_live(
    api_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976013)
        user = await make_user(session, "live-demander-2@arc.test")
        await session.commit()
        monkeypatch.setattr(demand_module, "coverable", _covering(download.episode.id))
        episode = await session.get(Episode, download.episode.id)
        assert episode is not None
        outcome = await demand_module.demand(
            session, user_id=user.id, episode=episode, now=datetime.now(UTC), settings=settings
        )
        await session.commit()
        assert outcome.live is True and outcome.live_note is None
        jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
        types = {job.type: job for job in jobs}
        assert types[REMUX_LIVE].priority == 5
        assert types[REMUX_LIVE].payload["dedupe_key"] == live_dedupe_key(episode.id)
        assert QBIT_SEQUENTIAL in types

        # And the toggle job, run against the stub: sequential, top, idempotent.
        sequential = types[QBIT_SEQUENTIAL]
        await qbit_sequential(context(session, settings, sequential))
        await qbit_sequential(context(session, settings, sequential))
        assert download.stub.toggles == [
            ("seq_dl", download.torrent.info_hash),
            ("f_l_piece_prio", download.torrent.info_hash),
        ]
        assert download.stub.topped == [download.torrent.info_hash] * 2


# --- Gone, idle, failure ---------------------------------------------------------------


async def test_a_torrent_cancelled_mid_remux_ends_the_job_and_removes_the_directory(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin")
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976020, size=6 * MIB)
        download.advance(per_poll=1)
        job = await a_live_job(session, download.episode.id)
        directory = live_dir_for(settings, download.episode.id)

        async def cancel_when_playing() -> float:
            while not (directory / "video.m3u8").exists():
                await asyncio.sleep(0.01)
            async with api_factory() as other:
                await other.execute(
                    update(Torrent)
                    .where(Torrent.id == download.torrent.id)
                    .values(qbit_state="cancelled")
                )
                await other.commit()
            return asyncio.get_running_loop().time()

        canceller = asyncio.create_task(cancel_when_playing())
        await asyncio.wait_for(remux_live(context(session, settings, job)), timeout=20)
        cancelled_at = await canceller
        ended_at = asyncio.get_running_loop().time()
        await session.refresh(job)
    assert job.payload["stage"] == STAGE_LIVE_GONE
    assert "cancelled" in job.payload["reason"]
    assert not directory.exists()
    # Within one poll (10 ms here) plus ffmpeg's kill: well under a second.
    assert ended_at - cancelled_at < 2.0


async def test_no_progress_stops_ffmpeg_keeps_the_playlist_and_requeues(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin")
    sure(monkeypatch)
    idle = settings.model_copy(update={"live_idle_minutes": 0})
    async with api_factory() as session:
        download = await a_download(session, idle, monkeypatch, anilist_id=976021)
        download.stall_after(6)  # 1.5 MiB: past the head, then nothing
        job = await a_live_job(session, download.episode.id)
        await remux_live(context(session, idle, job))
        await session.refresh(job)
        assert job.payload["stage"] == STAGE_LIVE_IDLE
        await finish(session, job)
        again = (
            await session.scalars(select(Job).where(Job.type == REMUX_LIVE, Job.id != job.id))
        ).all()
    assert len(again) == 1 and again[0].status is JobStatus.PENDING
    assert again[0].run_after > datetime.now(UTC) + timedelta(minutes=4)
    directory = live_dir_for(idle, download.episode.id)
    text = (directory / "video.m3u8").read_text()
    assert "#EXT-X-ENDLIST" not in text
    assert "seg_00000.m4s" in text


async def test_a_failed_ffmpeg_fails_the_job_for_a_retry(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.services.media.transcode import TranscodeError

    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin", fail=True)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976022)
        download.advance(per_poll=100)
        job = await a_live_job(session, download.episode.id)
        with pytest.raises(TranscodeError):
            await remux_live(context(session, settings, job))
    assert not live_dir_for(settings, download.episode.id).exists()


async def test_a_restart_begins_again_from_byte_zero(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "fed.bin"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=record)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976023)
        download.advance(per_poll=100)
        directory = live_dir_for(settings, download.episode.id)
        directory.mkdir(parents=True)
        (directory / "seg_00099.m4s").write_bytes(b"stale")
        first = await a_live_job(session, download.episode.id)
        await remux_live(context(session, settings, first))
        await finish(session, first)
        segments_first = sorted(p.name for p in directory.glob("seg_*.m4s"))
        bytes_first = {name: (directory / name).read_bytes() for name in segments_first}
        record.unlink()
        second = await a_live_job(session, download.episode.id)
        await remux_live(context(session, settings, second))
    assert "seg_00099.m4s" not in segments_first
    assert record.read_bytes() == download.data  # fed from byte 0 again
    assert {name: (directory / name).read_bytes() for name in segments_first} == bytes_first


# --- Adoption by the remux --------------------------------------------------------------


async def complete_and_link(session: AsyncSession, download_episode_id: int, source: Path) -> None:
    episode = await session.get(Episode, download_episode_id)
    assert episode is not None
    episode.state = EpisodeState.MATCHED
    session.add(
        MediaFile(
            episode_id=episode.id,
            path=str(source),
            size=source.stat().st_size,
            review_state=ReviewState.AUTO,
        )
    )
    await session.commit()


async def a_remux_job(session: AsyncSession, episode_id: int) -> Job:
    job = Job(
        type=REMUX,
        payload={"episode_id": episode_id},
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=2,
        locked_by="test:1",
        locked_at=datetime.now(UTC),
    )
    session.add(job)
    await session.commit()
    return job


async def test_the_remux_adopts_a_finished_live_directory_without_running_ffmpeg(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "calls.txt"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin", marker=marker)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976030)
        download.advance(per_poll=100)
        live_job = await a_live_job(session, download.episode.id)
        await remux_live(context(session, settings, live_job))
        await finish(session, live_job)
        live_segments = {
            p.name: p.read_bytes()
            for p in live_dir_for(settings, download.episode.id).glob("seg_*.m4s")
        }

        await complete_and_link(session, download.episode.id, download.source)
        job = await a_remux_job(session, download.episode.id)
        await remux_episode(context(session, settings, job))

        episode = await session.get(Episode, download.episode.id, populate_existing=True)
        assert episode is not None and episode.state is EpisodeState.READY
        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == download.episode.id)
        )
        assert rendition is not None
        assert rendition.kind == RENDITION_REMUX
        assert rendition.text_subtitles is True
        assert (rendition.width, rendition.height) == (1920, 1080)
        await session.refresh(job)
        assert job.payload[KEY_ADOPTED] is True

    # One ffmpeg, the live one: the remux ran none.
    assert marker.read_text().split() == ["live"]
    output = output_dir_for(settings, download.episode.id)
    assert not live_dir_for(settings, download.episode.id).exists()
    text = (output / "video.m3u8").read_text()
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and "EVENT" not in text
    assert (output / "subs.vtt").is_file()
    assert not (output / "_work").exists()
    assert not (output / LIVE_OWNER_NAME).exists()
    assert {p.name: p.read_bytes() for p in output.glob("seg_*.m4s")} == live_segments


async def test_a_live_directory_cut_from_another_file_is_not_adopted(
    api_factory: SessionFactory, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "calls.txt"
    install_live_ffmpeg(tmp_path / "bin", monkeypatch, record=tmp_path / "fed.bin", marker=marker)
    sure(monkeypatch)
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976031)
        download.advance(per_poll=100)
        live_job = await a_live_job(session, download.episode.id)
        await remux_live(context(session, settings, live_job))
        await finish(session, live_job)
        other = download.source.with_name("[Other] Frieren - 03.mkv")
        other.write_bytes(b"x" * 100)
        await complete_and_link(session, download.episode.id, other)
        job = await a_remux_job(session, download.episode.id)
        await remux_episode(context(session, settings, job))
        await session.refresh(job)
        assert KEY_ADOPTED not in job.payload
    assert marker.read_text().split() == ["live", "remux"]
    assert not live_dir_for(settings, download.episode.id).exists()


# --- The progress guard ------------------------------------------------------------------


async def test_a_truncated_duration_never_completes_a_downloading_episode(
    api_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976040)
        user = await make_user(session, "live-watcher@arc.test")
        job = await a_live_job(session, download.episode.id)
        job.payload = {**job.payload, "expected_duration": 1440.0}
        await session.commit()
        episode = await session.get(Episode, download.episode.id)
        assert episode is not None

        outcome = await record_progress(
            session, user_id=user.id, episode=episode, position_s=290.0, duration_s=300.0
        )
        await session.commit()
        assert outcome.completed is False and outcome.newly_completed is False

        # The whole length, reported: it completes as it always did.
        outcome = await record_progress(
            session, user_id=user.id, episode=episode, position_s=1300.0, duration_s=1440.0
        )
        await session.commit()
        assert outcome.newly_completed is True


# --- Leftovers ------------------------------------------------------------------------------


async def test_sweep_live_leftovers(
    api_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with api_factory() as session:
        download = await a_download(session, settings, monkeypatch, anilist_id=976050)
        kept = live_dir_for(settings, download.episode.id)
        write_served(kept)
        empty = live_dir_for(settings, download.episode.id + 1000)
        empty.mkdir(parents=True)
        job = await a_live_job(session, download.episode.id)
        job.payload = {**job.payload, "stage": STAGE_LIVE_DONE}
        await session.commit()
        await finish(session, job)

        orphan = live_dir_for(settings, 999_999)
        orphan.mkdir(parents=True)
        junk = settings.live_dir / "not-an-id"
        junk.mkdir()
        link = settings.live_dir / "424242"
        link.symlink_to(kept)

        restart = settings.live_dir / f"{download.episode.id}.new"
        restart.mkdir()

        removed = await sweep_live_leftovers(session, settings)
        # The orphan, the junk, the link, the restart directory of a job that
        # is not running, and a numbered directory with no episode.
        assert removed == 5
        assert kept.is_dir()
        assert not orphan.exists() and not junk.exists() and not link.is_symlink()
        assert not restart.exists() and not empty.exists()

        # A refused job stands behind nothing: its directory goes too.
        job.payload = {**job.payload, "stage": STAGE_LIVE_REFUSED}
        await session.commit()
        assert await sweep_live_leftovers(session, settings) == 1
        assert not kept.exists()


@pytest.mark.parametrize("elsewhere", [False, True])
async def test_a_file_in_review_or_linked_elsewhere_stops_the_live_directory_being_kept(
    api_factory: SessionFactory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    elsewhere: bool,
) -> None:
    async with api_factory() as session:
        download = await a_download(
            session, settings, monkeypatch, anilist_id=976051 + int(elsewhere)
        )
        directory = live_dir_for(settings, download.episode.id)
        write_served(directory)
        job = await a_live_job(session, download.episode.id)
        job.payload = {**job.payload, "stage": STAGE_LIVE_DONE}
        await session.commit()
        await finish(session, job)
        assert await sweep_live_leftovers(session, settings) == 0
        others = (
            await session.scalars(
                select(Episode.id).where(
                    Episode.anime_id == download.episode.anime_id,
                    Episode.id != download.episode.id,
                )
            )
        ).all()
        session.add(
            MediaFile(
                path=str(download.source),
                size=len(download.data),
                episode_id=others[0] if elsewhere else None,
                review_state=ReviewState.AUTO if elsewhere else ReviewState.PENDING,
            )
        )
        await session.commit()
        assert await sweep_live_leftovers(session, settings) == 1
    assert not directory.exists()


def test_the_settings_defaults() -> None:
    settings = acquisition_settings(Path("/tmp/arc-live-defaults"))
    assert settings.max_live_remuxes == 1
    assert settings.live_idle_minutes == 10
    assert settings.live_head_bytes == 8 * MIB
    assert settings.live_dir.name == "live"
    from arc.services.retention.sweep import allowed_roots

    assert settings.live_dir in allowed_roots(settings)
    assert KIB == 1024
