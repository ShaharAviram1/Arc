"""M20 end to end: demand → live → adopt → watch → delete → remembered re-fetch (T10).

One episode's whole life under the M20 rules, with qBittorrent and Nyaa
stubbed (never a real torrent) and **ffmpeg real**. A SubsPlease 1080p episode
from the developer's library is cloned (``cp -c``: copy-on-write, the library
file is never opened for writing) into this test's own ``DATA_DIR`` and plays
the part of the download; the stub's ``pieceStates`` decide how much of it
exists.

1. A user watching the show (list progress 6, every episode aired): the window
   wants the next episode, and only it (N = 1).
2. They press play (``POST /api/episodes/{id}/demand``): ``search_release``
   asks Nyaa once and adds the single **sequentially**, first and last pieces
   first, and queues the live remux.
3. The download advances; the live remux (real ffmpeg, fed through the front
   gate) serves a **growing EVENT playlist with subtitle cues**, fetched
   through the API with a real session. A short report and a truncated one
   (290 of 300 s) complete nothing.
4. The download completes → ``poll_qbit`` → the real matcher (catalogue
   fakes) links it → the ordinary remux **adopts** the live output: no second
   ffmpeg, and the episode is ``ready`` behind the very same URLs, now VOD.
5. The user completes it (≥ 0.9 × duration): the first MyAnimeList write and
   the first ``mal_*`` job appear, and not before.
6. With ``watched_grace_hours = 0``, the next sweep (once the 30-minute
   "being played" hold has passed) deletes the files and keeps the torrent
   row as ``evicted``.
7. The user takes the watched mark back and demands it again:
   ``search_release`` re-adds the **same hash** with zero Nyaa requests.

Skipped when there is no SubsPlease 1080p MKV under ``data/downloads`` or no
ffmpeg (a fresh checkout, CI).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, update

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    RENDITION_REMUX,
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    MalWriteCause,
    MalWriteLog,
    Rendition,
    Torrent,
    TorrentKind,
    Want,
    WatchProgress,
)
from arc.services.acquisition import jobs as acquisition_jobs  # noqa: F401  (handlers)
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition import qbit as qbit_module
from arc.services.acquisition import rules as acquisition_rules
from arc.services.acquisition.names import COMPUTE_WANTS, POLL_QBIT, SEARCH_RELEASE
from arc.services.acquisition.qbit import QBIT_EVICTED
from arc.services.catalog import Breaker, CatalogService
from arc.services.jobs.queue import enqueue
from arc.services.jobs.runner import run_job
from arc.services.library import jobs as library_jobs
from arc.services.library import prematch as prematch_module
from arc.services.library.names import MATCH_FILE
from arc.services.media import live as live_module
from arc.services.media import remux as remux_module
from arc.services.media.names import (
    REMUX,
    REMUX_LIVE,
    STAGE_LIVE_DONE,
    live_dir_for,
    output_dir_for,
)
from arc.services.media.remux import KEY_ADOPTED
from arc.services.media.transcode import reset_semaphore
from arc.services.retention import floor as retention_floor
from arc.services.retention import jobs as retention_jobs  # noqa: F401  (handlers)
from arc.services.retention.names import RETENTION_SWEEP
from tests.acquisition_helpers import (
    NyaaStub,
    QbitStub,
    acquisition_settings,
    fake_free_space,
    force_transport,
    make_entry,
    make_episodes,
    read_fixture,
    set_setting,
)
from tests.anilist_mock import FRIEREN_ID, FakeAniList, frieren_fake
from tests.conftest import TEST_FERNET_KEY, add_user, api_transport, login
from tests.mal_helpers import link_user
from tests.mal_mock import frieren_fake as mal_frieren_fake
from tests.test_live_slow import LIBRARY

pytestmark = [pytest.mark.slow, pytest.mark.pg]

EMAIL = "m20-flow@arc.test"
PASSWORD = "m20-flow-password"
GB = 1024**3
MIB = 1 << 20

#: The season-one 1080p SubsPlease upload in the Nyaa fixture: the release the
#: rules pick with SubsPlease preferred, and the single file its torrent holds.
SUBSPLEASE_1080 = "42d462368aed5f620f28ae99eacbbea776ed776d"
FILE_NAME = "[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv"
FEED = read_fixture("search_frieren_07.xml")

#: Pieces the size a real 1.4 GB single has; the download advances
#: :data:`STEP_PIECES` per ``pieceStates`` call up to :data:`FIRST_CUT` of the
#: file, holds there while the test watches the live playlist, then completes.
PIECE_SIZE = 4 * MIB
STEP_PIECES = 4
FIRST_CUT = 0.3

#: The whole flow must stay well inside the three minutes T10 allows.
BUDGET_SECONDS = 180


# --- Fixtures ----------------------------------------------------------------------


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    """A ``DATA_DIR`` of the test's own and an ffmpeg that counts its runs."""
    real = shutil.which("ffmpeg")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "ffmpeg"
    log = tmp_path / "ffmpeg-runs.txt"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    return acquisition_settings(
        tmp_path / "data",
        database_url=test_database_url,
        fernet_key=TEST_FERNET_KEY,
        ffmpeg_bin=str(shim),
    )


@pytest.fixture
def ffmpeg_runs(tmp_path: Path) -> Path:
    return tmp_path / "ffmpeg-runs.txt"


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield http


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> tuple[NyaaStub, QbitStub]:
    """Nyaa and qBittorrent in memory, the catalogue faked, the waits shortened."""
    if LIBRARY is None:
        pytest.skip("no SubsPlease 1080p MKV in data/downloads, or no ffmpeg")
    nyaa = NyaaStub({"Sousou no Frieren - 07": FEED})
    qbit = QbitStub()

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep)
    monkeypatch.setattr(
        nyaa_module.NyaaClient,
        "__init__",
        force_transport(nyaa_module.NyaaClient, nyaa.transport()),
    )
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, qbit.transport()),
    )

    # The real matcher, over the AniList and MAL fakes: for the live remux's
    # pre-match gate and for the match job alike.
    anilist: FakeAniList = frieren_fake()
    anilist.search["sousou no frieren"] = anilist.search["frieren"]
    service = CatalogService(anilist.source(), mal_frieren_fake().source(), Breaker(300.0))

    class _Holder:
        async def __aenter__(self) -> CatalogService:
            return service

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(prematch_module, "catalog_for", lambda settings: _Holder())
    monkeypatch.setattr(library_jobs, "catalog_for", lambda settings: _Holder())

    # Plenty of disk: neither the demand's storage hold nor the floor acts.
    fake_free_space(monkeypatch, acquisition_rules, 90 * GB)
    fake_free_space(monkeypatch, retention_floor, 90 * GB)

    reset_semaphore()
    monkeypatch.setattr(live_module, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(live_module, "PAYLOAD_SECONDS", 0.0)
    monkeypatch.setattr(remux_module, "LIVE_ADOPT_POLL_SECONDS", 0.02)
    return nyaa, qbit


# --- Helpers ---------------------------------------------------------------------


async def run_next(
    factory: SessionFactory,
    settings: Settings,
    job_type: str,
    *,
    episode_id: int | None = None,
) -> Job:
    """Claim the oldest pending ``job_type`` (for ``episode_id``) and run it as the worker does."""
    async with factory() as session:
        statement = select(Job).where(Job.type == job_type, Job.status == JobStatus.PENDING)
        if episode_id is not None:
            statement = statement.where(Job.payload["episode_id"].as_integer() == episode_id)
        job = await session.scalar(statement.order_by(Job.id).limit(1).with_for_update())
        assert job is not None, f"no pending {job_type} job for episode {episode_id}"
        now = datetime.now(UTC)
        job.status = JobStatus.RUNNING
        job.locked_by = "m20-flow:1"
        job.locked_at = now
        job.started_at = now
        job.attempts += 1
        await session.commit()
    status = await run_job(job, factory, settings)
    async with factory() as session:
        row = await session.get(Job, job.id)
        assert row is not None
        assert status is JobStatus.DONE, row.last_error
        return row


async def enqueue_one(factory: SessionFactory, job_type: str) -> None:
    async with factory() as session:
        await enqueue(session, job_type, {})
        await session.commit()


async def mal_trace(factory: SessionFactory) -> tuple[list[MalWriteLog], list[str]]:
    """Every ``mal_write_log`` row and every ``mal_*`` job type queued so far."""
    async with factory() as session:
        rows = list((await session.scalars(select(MalWriteLog).order_by(MalWriteLog.id))).all())
        jobs = list((await session.scalars(select(Job.type).where(Job.type.like("mal%")))).all())
    return rows, jobs


async def episode_row(factory: SessionFactory, episode_id: int) -> Episode:
    async with factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        return episode


class Download:
    """The stub's view of the cloned file: pieces checked as the test allows."""

    def __init__(self, qbit: QbitStub, source: Path, episode_id: int) -> None:
        self.qbit = qbit
        self.size = source.stat().st_size
        self.pieces = -(-self.size // PIECE_SIZE)
        self.cap = int(self.pieces * FIRST_CUT)
        info_hash = SUBSPLEASE_1080
        self.states = [0] * self.pieces
        qbit.add_torrent(
            info_hash,
            name=FILE_NAME,
            progress=0.0,
            size=self.size,
            num_complete=40,
            content_path=f"/data/downloads/{episode_id}/{FILE_NAME}",
        )
        qbit.add_files(info_hash, [FILE_NAME], size=self.size, progress=0.0)
        qbit.piece_states[info_hash] = self.states
        qbit.piece_sizes[info_hash] = PIECE_SIZE
        qbit.on_piece_states = self._advance
        self.calls = 0

    def _row(self) -> dict[str, Any]:
        return next(row for row in self.qbit.torrents if row["hash"] == SUBSPLEASE_1080)

    def _advance(self, requested: str) -> None:
        if requested != SUBSPLEASE_1080:
            return
        if self.calls == 0:
            self.states[-1] = 2  # firstLastPiecePrio: the last piece comes early
        self.calls += 1
        done = 0
        for index in range(min(self.cap, self.pieces)):
            if self.states[index] != 2 and done < STEP_PIECES:
                self.states[index] = 2
                done += 1
        self._row()["progress"] = sum(1 for s in self.states if s == 2) / self.pieces

    def finish(self) -> None:
        """Every piece in: the client says the torrent is complete and seeding."""
        self.cap = self.pieces
        for index in range(self.pieces):
            self.states[index] = 2
        row = self._row()
        row["progress"] = 1.0
        row["state"] = "stalledUP"
        row["completion_on"] = int(datetime.now(UTC).timestamp())


def segment_names(playlist: str, prefix: str) -> list[str]:
    return [line for line in playlist.splitlines() if line.startswith(prefix)]


# --- The flow --------------------------------------------------------------------


async def test_the_m20_flow_end_to_end(
    api_factory: SessionFactory,
    settings: Settings,
    client: AsyncClient,
    stubs: tuple[NyaaStub, QbitStub],
    ffmpeg_runs: Path,
) -> None:
    started = monotonic()
    nyaa, qbit = stubs
    assert LIBRARY is not None

    # --- 0. A user watching the show, MyAnimeList linked ----------------------------
    user = await add_user(api_factory, EMAIL, PASSWORD)
    await link_user(api_factory, settings, user_id=user.id)
    async with api_factory() as session:
        anime = Anime(
            anilist_id=FRIEREN_ID,
            title_romaji="Sousou no Frieren",
            title_english="Frieren: Beyond Journey's End",
            status="RELEASING",
            format="TV",
            episodes=28,
            summary_source="anilist",
            detail_source="anilist",
            refreshed_at=datetime.now(UTC),
        )
        session.add(anime)
        await session.flush()
        episodes = await make_episodes(session, anime, 12)  # every one has aired
        owner = await session.get(type(user), user.id)
        assert owner is not None
        await make_entry(session, owner, anime, progress=6)
        await set_setting(session, "preferred_groups", ["SubsPlease"])
        await set_setting(session, "watched_grace_hours", 0)
        await session.commit()
        episode_id = episodes[6].id
        anime_id = anime.id
    await login(client, EMAIL, PASSWORD)

    # --- 1. The window wants the next episode, and only it (N = 1) ------------------
    await enqueue_one(api_factory, COMPUTE_WANTS)
    await run_next(api_factory, settings, COMPUTE_WANTS)
    async with api_factory() as session:
        wanted = (
            await session.scalars(select(Want.episode_id).where(Want.user_id == user.id))
        ).all()
    assert list(wanted) == [episode_id]
    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.WANTED

    # --- 2. Play on demand: searched once, added sequentially -----------------------
    response = await client.post(f"/api/episodes/{episode_id}/demand")
    assert response.status_code == 202, response.text
    await run_next(api_factory, settings, SEARCH_RELEASE, episode_id=episode_id)
    assert nyaa.queries, "the first fetch asked Nyaa"
    first_queries = list(nyaa.queries)
    first_fetched = list(nyaa.fetched)
    added = qbit.added[-1]
    assert SUBSPLEASE_1080 in added["urls"]
    assert added["sequentialDownload"] == "true" and added["firstLastPiecePrio"] == "true"
    assert added["savepath"] == f"/data/downloads/{episode_id}"
    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.DOWNLOADING
    async with api_factory() as session:
        torrent = await session.scalar(select(Torrent).where(Torrent.episode_id == episode_id))
        assert torrent is not None and torrent.kind is TorrentKind.SINGLE
        torrent_id = torrent.id

    # The client has the torrent now: the clone stands in for its pre-allocated file.
    source = settings.downloads_dir / str(episode_id) / FILE_NAME
    source.parent.mkdir(parents=True)
    cloned = subprocess.run(["cp", "-c", str(LIBRARY), str(source)], check=False)
    if cloned.returncode != 0:
        pytest.skip("cannot clone the library file (not APFS?)")
    source.chmod(0o444)
    download = Download(qbit, source, episode_id)

    # No MyAnimeList trace of anything so far.
    assert await mal_trace(api_factory) == ([], [])

    # --- 3. The live remux, served while the download advances -----------------------
    live_task = asyncio.create_task(
        run_next(api_factory, settings, REMUX_LIVE, episode_id=episode_id)
    )
    seen_counts: list[int] = []
    cue_text = ""
    deadline = monotonic() + 90
    while monotonic() < deadline:
        assert not live_task.done() or live_task.exception() is None
        playlist = await client.get(f"/media/{episode_id}/video.m3u8")
        if playlist.status_code == 200:
            assert "#EXT-X-PLAYLIST-TYPE:EVENT" in playlist.text
            assert "#EXT-X-ENDLIST" not in playlist.text
            count = len(segment_names(playlist.text, "seg_"))
            if not seen_counts or count != seen_counts[-1]:
                seen_counts.append(count)
            subs = await client.get(f"/media/{episode_id}/subs.m3u8")
            if subs.status_code == 200 and len(seen_counts) >= 3:
                for name in segment_names(subs.text, "sub_"):
                    vtt = await client.get(f"/media/{episode_id}/{name}")
                    assert vtt.status_code == 200
                    if "-->" in vtt.text:
                        cue_text = vtt.text
                        break
            if cue_text and download.calls * STEP_PIECES >= download.cap:
                break
        await asyncio.sleep(0.2)
    assert cue_text, f"no subtitle cue served while downloading; segments seen {seen_counts}"
    assert "WEBVTT" in cue_text and "X-TIMESTAMP-MAP=MPEGTS:" in cue_text
    assert len(seen_counts) >= 3 and seen_counts == sorted(seen_counts), seen_counts

    master = await client.get(f"/media/{episode_id}/index.m3u8")
    assert master.status_code == 200 and 'URI="subs.m3u8"' in master.text
    play = (await client.get(f"/api/episodes/{episode_id}/play")).json()
    live_url = play["playlist_url"]
    assert live_url == f"/media/{episode_id}/index.m3u8"
    assert play["live"] is not None and play["live"]["ended"] is False
    expected = play["live"]["expected_duration"]
    assert expected > 600
    first_segment = (await client.get(f"/media/{episode_id}/seg_00000.m4s")).content
    assert first_segment

    # Watching while it downloads completes nothing, and writes nothing to MAL:
    # neither a short report nor one that claims the file is 300 s long.
    for position, duration in ((120.0, expected), (290.0, 300.0)):
        report = await client.post(
            "/api/progress",
            json={"episode_id": episode_id, "position_s": position, "duration_s": duration},
        )
        assert report.status_code == 200, report.text
        assert report.json()["completed"] is False
    assert await mal_trace(api_factory) == ([], [])

    # --- 4. Complete → match → adopt -------------------------------------------------
    download.finish()
    live_job = await asyncio.wait_for(live_task, timeout=90)
    assert live_job.payload["stage"] == STAGE_LIVE_DONE
    assert live_job.payload["front_bytes"] == download.size
    live_segments = {
        path.name: path.read_bytes() for path in live_dir_for(settings, episode_id).glob("seg_*")
    }
    assert live_segments.get("seg_00000.m4s") == first_segment

    await enqueue_one(api_factory, POLL_QBIT)
    await run_next(api_factory, settings, POLL_QBIT)
    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.MATCHING
    await run_next(api_factory, settings, MATCH_FILE)
    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.MATCHED
    remux_job = await run_next(api_factory, settings, REMUX, episode_id=episode_id)
    assert remux_job.payload[KEY_ADOPTED] is True

    runs = ffmpeg_runs.read_text().splitlines()
    assert len(runs) == 1 and "pipe:0" in runs[0], runs  # the live one, and no other
    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.READY
    async with api_factory() as session:
        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == episode_id)
        )
        assert rendition is not None and rendition.kind == RENDITION_REMUX
        assert rendition.text_subtitles is True
    output = output_dir_for(settings, episode_id)
    assert {p.name: p.read_bytes() for p in output.glob("seg_*")} == live_segments
    assert not live_dir_for(settings, episode_id).exists()

    play = (await client.get(f"/api/episodes/{episode_id}/play")).json()
    assert play["playlist_url"] == live_url
    assert play["live"] is None and play["pending"] is None
    duration = play["duration"]
    assert duration > 600
    vod = await client.get(f"/media/{episode_id}/video.m3u8")
    assert vod.status_code == 200
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in vod.text and "EVENT" not in vod.text
    assert vod.text.rstrip().endswith("#EXT-X-ENDLIST")
    assert (await client.get(f"/media/{episode_id}/seg_00000.m4s")).content == first_segment
    assert (await client.get(f"/media/{episode_id}/subs.m3u8")).status_code == 200
    assert await mal_trace(api_factory) == ([], [])

    # --- 5. The real completion: the first MAL write, and only now ------------------
    report = await client.post(
        "/api/progress",
        json={"episode_id": episode_id, "position_s": 0.95 * duration, "duration_s": duration},
    )
    assert report.status_code == 200, report.text
    assert report.json()["newly_completed"] is True
    assert report.json()["list_progress"] == 7
    rows, mal_jobs = await mal_trace(api_factory)
    assert [(row.field, row.old_value, row.new_value, row.cause) for row in rows] == [
        ("progress", 6, 7, MalWriteCause.WATCH)
    ]
    assert mal_jobs, "the completion queued its push"

    # --- 6. Delete after watching ------------------------------------------------------
    await enqueue_one(api_factory, COMPUTE_WANTS)
    await run_next(api_factory, settings, COMPUTE_WANTS)
    async with api_factory() as session:
        live_want = await session.scalar(
            select(Want).where(Want.episode_id == episode_id, Want.dropped_at.is_(None))
        )
        assert live_want is None, "watched past: nobody wants it any more"
        # Hold (c) keeps a play for 30 minutes even at grace 0 (the last
        # minutes after a 90 % completion are still playing); the clock moves
        # instead of the test waiting.
        earlier = datetime.now(UTC) - timedelta(minutes=31)
        await session.execute(
            update(WatchProgress)
            .where(WatchProgress.episode_id == episode_id)
            .values(updated_at=earlier, completed_at=earlier)
        )
        await session.commit()
    await enqueue_one(api_factory, RETENTION_SWEEP)
    await run_next(api_factory, settings, RETENTION_SWEEP)

    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.NOT_WANTED
    assert not source.exists() and not source.parent.exists()
    assert not output.exists()
    assert {"hashes": SUBSPLEASE_1080, "deleteFiles": "true"} in qbit.deleted
    async with api_factory() as session:
        kept = await session.get(Torrent, torrent_id)
        assert kept is not None and kept.qbit_state == QBIT_EVICTED
        assert kept.evicted_at is not None
        assert (
            await session.scalar(select(Rendition).where(Rendition.episode_id == episode_id))
            is None
        )

    # --- 7. Asked for again: the remembered release, no Nyaa ---------------------------
    # The user takes the watched mark back (an explicit un-mark, which is theirs
    # to send to MAL), so the episode is in their window again.
    unmarked = await client.delete(f"/api/episodes/{episode_id}/watched")
    assert unmarked.status_code == 200, unmarked.text
    async with api_factory() as session:
        entry = await session.get(ListEntry, (user.id, anime_id))
        assert entry is not None and entry.progress == 6
    writes_before = len((await mal_trace(api_factory))[0])

    nyaa_module.reset_shared_client()  # no cached answer could hide a request
    adds = len(qbit.added)
    response = await client.post(f"/api/episodes/{episode_id}/demand")
    assert response.status_code == 202, response.text
    await run_next(api_factory, settings, SEARCH_RELEASE, episode_id=episode_id)

    assert nyaa.queries == first_queries, "zero Nyaa requests on the re-fetch"
    assert nyaa.fetched == first_fetched
    assert len(qbit.added) == adds + 1
    again = qbit.added[-1]
    assert SUBSPLEASE_1080 in again["urls"]
    assert again["sequentialDownload"] == "true"
    assert (await episode_row(api_factory, episode_id)).state is EpisodeState.DOWNLOADING
    async with api_factory() as session:
        rows_now = (
            await session.scalars(select(Torrent).where(Torrent.episode_id == episode_id))
        ).all()
        assert [row.id for row in rows_now] == [torrent_id], "the same row, not a new one"
        assert rows_now[0].qbit_state == "added"
        relive = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.type == REMUX_LIVE, Job.status == JobStatus.PENDING)
        )
        assert relive == 1, "the demanded re-fetch is played live again"
    # The re-fetch itself wrote nothing to MyAnimeList.
    assert len((await mal_trace(api_factory))[0]) == writes_before

    elapsed = monotonic() - started
    assert elapsed < BUDGET_SECONDS, f"the flow took {elapsed:.0f}s"
    print(f"\nM20 flow: {elapsed:.1f}s, {len(live_segments)} segments, {download.size} bytes")
