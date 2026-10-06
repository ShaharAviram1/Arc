"""Walking the library directories into ``media_files`` rows (FR-L1).

Against the real database, with a real temporary ``DATA_DIR``. The probe is
switched off (``probe=False``) rather than mocked: whether ffprobe exists on
the machine running the suite is not what these tests are about, and
``test_probe.py`` covers both answers.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    Job,
    JobStatus,
    MediaFile,
    ReviewState,
    Torrent,
    TorrentFile,
    TorrentKind,
)
from arc.services.library import ingest
from arc.services.library.names import MATCH_FILE

pytestmark = pytest.mark.pg

#: A tenth of the production window, so a test that has to *wait out* the
#: settle can do it in a moment. The rule is what is under test, not the value.
SETTLE = 60.0

FRIEREN = "[SubsPlease] Sousou no Frieren - 05 (1080p) [A1B2C3D4].mkv"
MUSHISHI = "Mushishi - 03.mkv"
NCOP = "[Group] Some Show - NCOP.mkv"
PARTIAL = "[SubsPlease] Dandadan - 07 (1080p) [DEADBEEF].mkv.!qB"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "downloads").mkdir()
    (tmp_path / "manual").mkdir()
    return tmp_path


@pytest.fixture
def library_settings(settings: Settings, data_dir: Path) -> Settings:
    return settings.model_copy(update={"data_dir": data_dir, "library_settle_seconds": SETTLE})


def make(path: Path, *, age: float = SETTLE * 2, size: int = 1024) -> Path:
    """A file of ``size`` bytes whose mtime is ``age`` seconds in the past."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    stamp = time.time() - age
    import os

    os.utime(path, (stamp, stamp))
    return path


async def rows(session: AsyncSession) -> list[MediaFile]:
    found = await session.scalars(select(MediaFile).order_by(MediaFile.path))
    return list(found.all())


async def match_jobs(session: AsyncSession) -> list[Job]:
    found = await session.scalars(select(Job).where(Job.type == MATCH_FILE).order_by(Job.id))
    return list(found.all())


class TestScan:
    async def test_both_directories_are_walked(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "downloads" / FRIEREN)
        make(data_dir / "manual" / MUSHISHI)

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.added == 2
        assert {Path(row.path).name for row in await rows(db_session)} == {FRIEREN, MUSHISHI}

    async def test_a_row_carries_the_parse_and_the_size(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "manual" / FRIEREN, size=4096)
        await ingest.scan(db_session, library_settings, probe=False)

        row = (await rows(db_session))[0]
        assert row.size == 4096
        assert row.review_state is ReviewState.PENDING
        assert row.episode_id is None
        assert row.parsed is not None
        assert row.parsed["title_key"] == "sousou no frieren"
        assert row.parsed["episode"] == 5
        assert row.parsed["kind"] == "episode"
        assert "probe" not in row.parsed

    async def test_the_stored_path_is_absolute(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "manual" / MUSHISHI)
        await ingest.scan(db_session, library_settings, probe=False)
        assert Path((await rows(db_session))[0].path).is_absolute()

    async def test_each_new_file_enqueues_one_match_job(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "manual" / FRIEREN)
        make(data_dir / "manual" / MUSHISHI)
        await ingest.scan(db_session, library_settings, probe=False)

        jobs = await match_jobs(db_session)
        assert len(jobs) == 2
        ids = {int(job.payload["media_file_id"]) for job in jobs}
        assert ids == {row.id for row in await rows(db_session)}

    async def test_nested_directories_are_walked(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "downloads" / "[Group] Frieren S1" / FRIEREN)
        result = await ingest.scan(db_session, library_settings, probe=False)
        assert result.added == 1

    async def test_a_missing_directory_is_not_an_error(
        self, db_session: AsyncSession, settings: Settings, tmp_path: Path
    ) -> None:
        """A fresh deployment has no ``data/`` at all until something writes one."""
        empty = settings.model_copy(update={"data_dir": tmp_path / "nothing-here"})
        result = await ingest.scan(db_session, empty, probe=False)
        assert result == ingest.ScanResult()


class TestSkips:
    async def test_a_partial_download_is_skipped(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "downloads" / PARTIAL)
        make(data_dir / "downloads" / FRIEREN)

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.added == 1
        assert result.skipped_partial == 1
        assert [Path(row.path).name for row in await rows(db_session)] == [FRIEREN]

    async def test_non_video_files_are_skipped(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        for name in ("release.nfo", "cover.jpg", "notes.txt", "Show - 01.srt"):
            make(data_dir / "downloads" / name)
        result = await ingest.scan(db_session, library_settings, probe=False)
        assert (result.seen, result.added) == (0, 0)

    async def test_hidden_files_and_directories_are_skipped(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "downloads" / f".{FRIEREN}")
        make(data_dir / "downloads" / ".Trash" / MUSHISHI)
        make(data_dir / "downloads" / "@eaDir" / MUSHISHI)
        result = await ingest.scan(db_session, library_settings, probe=False)
        assert result.added == 0

    async def test_a_file_still_being_written_waits_for_the_next_scan(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        """The settle window: a copy in progress has a size that is not final."""
        path = make(data_dir / "downloads" / FRIEREN, age=1.0)

        first = await ingest.scan(db_session, library_settings, probe=False)
        assert (first.added, first.skipped_recent) == (0, 1)
        assert await rows(db_session) == []

        # The next scan, once the file is older than the settle window. The
        # clock is passed in rather than slept through.
        later = path.stat().st_mtime + SETTLE + 1
        second = await ingest.scan(db_session, library_settings, now=later, probe=False)
        assert second.added == 1
        assert [Path(row.path).name for row in await rows(db_session)] == [FRIEREN]


class TestIdempotence:
    async def test_a_second_scan_adds_nothing(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "manual" / FRIEREN)
        make(data_dir / "manual" / MUSHISHI)
        make(data_dir / "manual" / NCOP)

        first = await ingest.scan(db_session, library_settings, probe=False)
        second = await ingest.scan(db_session, library_settings, probe=False)

        assert first.added == 3
        assert second.added == 0
        assert second.seen == 3
        assert len(await rows(db_session)) == 3
        assert len(await match_jobs(db_session)) == 3

    async def test_a_rescan_does_not_requeue_a_pending_match(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "manual" / FRIEREN)
        await ingest.scan(db_session, library_settings, probe=False)
        await ingest.scan(db_session, library_settings, probe=False)
        assert len(await match_jobs(db_session)) == 1

    async def test_ingest_file_returns_none_for_a_known_path(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        path = make(data_dir / "manual" / FRIEREN)
        assert await ingest.ingest_file(db_session, library_settings, path, probe=False) is not None
        assert await ingest.ingest_file(db_session, library_settings, path, probe=False) is None

    async def test_a_new_file_beside_an_indexed_one_is_picked_up(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        make(data_dir / "manual" / FRIEREN)
        await ingest.scan(db_session, library_settings, probe=False)

        make(data_dir / "manual" / MUSHISHI)
        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.added == 1
        assert len(await rows(db_session)) == 2


class TestBatching:
    """One pass is bounded, and it commits as it goes (FR-L1).

    The first scan of an existing library is thousands of files and one
    ffprobe each. Held in one transaction it outlives ``WORKER_STALE_AFTER``,
    the sweep decides the worker died, and the job is requeued while it is
    still running — two scans probing the same files.
    """

    def counted(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """Count ``commit()`` calls on ``session`` without stopping them."""
        commits: list[int] = []
        original = session.commit

        async def counting_commit() -> None:
            commits.append(1)
            await original()

        monkeypatch.setattr(session, "commit", counting_commit)
        return commits

    def thirty(self, data_dir: Path) -> None:
        for index in range(30):
            make(data_dir / "manual" / f"[Group] Some Show - {index + 1:02d} [1080p].mkv")

    async def test_a_batch_of_ten_commits_three_times_and_indexes_everything(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.thirty(data_dir)
        commits = self.counted(db_session, monkeypatch)

        result = await ingest.scan(
            db_session, library_settings, probe=False, batch=100, commit_every=10
        )

        assert result.added == 30
        assert result.remaining == 0
        assert len(commits) == 3
        assert len(await rows(db_session)) == 30
        assert len(await match_jobs(db_session)) == 30

    async def test_a_partial_final_batch_is_committed_too(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.thirty(data_dir)
        commits = self.counted(db_session, monkeypatch)

        result = await ingest.scan(
            db_session, library_settings, probe=False, batch=100, commit_every=7
        )

        assert result.added == 30
        assert len(commits) == 5  # 7, 14, 21, 28, and the last two
        assert len(await rows(db_session)) == 30

    async def test_the_cap_leaves_the_rest_for_the_next_pass(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
    ) -> None:
        self.thirty(data_dir)

        first = await ingest.scan(
            db_session, library_settings, probe=False, batch=10, commit_every=10
        )

        assert (first.added, first.remaining, first.seen) == (10, 20, 30)
        assert len(await rows(db_session)) == 10

        second = await ingest.scan(
            db_session, library_settings, probe=False, batch=10, commit_every=10
        )
        third = await ingest.scan(
            db_session, library_settings, probe=False, batch=10, commit_every=10
        )

        assert (second.added, second.remaining) == (10, 10)
        assert (third.added, third.remaining) == (10, 0)
        assert len(await rows(db_session)) == 30
        assert len({row.path for row in await rows(db_session)}) == 30

    async def test_the_defaults_come_from_settings(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
    ) -> None:
        self.thirty(data_dir)
        capped = library_settings.model_copy(update={"library_scan_batch": 12})

        result = await ingest.scan(db_session, capped, probe=False)

        assert (result.added, result.remaining) == (12, 18)

    async def test_an_empty_pass_commits_nothing(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        commits = self.counted(db_session, monkeypatch)
        await ingest.scan(db_session, library_settings, probe=False)
        assert commits == []


class TestProbe:
    async def test_a_probe_summary_is_stored_under_probe(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_probe(path: object) -> dict[str, object]:
            return {"duration": 1420.0, "format": "matroska,webm", "streams": []}

        monkeypatch.setattr("arc.services.library.ingest.probe_summary", fake_probe)
        make(data_dir / "manual" / FRIEREN)

        await ingest.scan(db_session, library_settings, probe=True)

        row = (await rows(db_session))[0]
        assert row.parsed is not None
        assert row.parsed["probe"]["duration"] == 1420.0
        # The parse itself is untouched by the probe.
        assert row.parsed["episode"] == 5

    async def test_no_probe_leaves_the_key_out(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def no_probe(path: object) -> None:
            return None

        monkeypatch.setattr("arc.services.library.ingest.probe_summary", no_probe)
        make(data_dir / "manual" / FRIEREN)

        await ingest.scan(db_session, library_settings, probe=True)

        row = (await rows(db_session))[0]
        assert row.parsed is not None
        assert "probe" not in row.parsed


class TestScanJob:
    async def test_the_handler_runs_a_scan(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The job is a thin wrapper; this proves the wiring, not the walk."""
        import logging

        from arc.services.jobs.registry import JobContext
        from arc.services.library.jobs import library_scan
        from arc.services.media import probe as probe_module

        monkeypatch.setattr(probe_module, "ffprobe_path", lambda *_: None)
        make(data_dir / "manual" / MUSHISHI)

        job = Job(type="library_scan", payload={}, status=JobStatus.RUNNING)
        db_session.add(job)
        await db_session.flush()

        await library_scan(
            JobContext(
                job=job,
                session=db_session,
                settings=library_settings,
                log=logging.getLogger("test"),
            )
        )
        assert len(await rows(db_session)) == 1


# --- Files a torrent is still downloading (owner incident, 2026-10-06) -------

PACK_HASH = "ab" * 20
SINGLE_HASH = "cd" * 20
IRUMA = "[SubsPlease] Mairimashita! Iruma-kun S2 - {:02d} (720p) [0F0F0F0F].mkv"


def sparse(path: Path, *, size: int = 760 * 1024 * 1024, age: float = SETTLE * 10) -> Path:
    """What qBittorrent leaves for a selected file at start: full size, no bytes, old mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)
    stamp = time.time() - age
    import os

    os.utime(path, (stamp, stamp))
    return path


async def _episode(session: AsyncSession, number: int = 1) -> Episode:
    anime = await session.scalar(select(Anime).where(Anime.anilist_id == 109_000))
    if anime is None:
        anime = Anime(anilist_id=109_000, title_romaji="Mairimashita! Iruma-kun 2", episodes=21)
        session.add(anime)
        await session.flush()
    episode = Episode(anime_id=anime.id, number=number)
    session.add(episode)
    await session.flush()
    return episode


async def _pack(
    session: AsyncSession, files: int, *, complete: frozenset[int] = frozenset()
) -> Torrent:
    """A batch with ``files`` wanted files, of which the indices in ``complete`` are in."""
    torrent = Torrent(
        info_hash=PACK_HASH,
        kind=TorrentKind.BATCH,
        title="[SubsPlease] Mairimashita! Iruma-kun S2 (01-21) (720p) [Batch]",
        save_path=f"/data/downloads/batch/{PACK_HASH}",
        qbit_state="stalledDL",
        progress=0.0,
    )
    session.add(torrent)
    await session.flush()
    for index in range(files):
        episode = await _episode(session, index + 1)
        done = index in complete
        session.add(
            TorrentFile(
                torrent_id=torrent.id,
                file_index=index,
                path=f"Iruma S2/{IRUMA.format(index + 1)}",
                size=760 * 1024 * 1024,
                episode_id=episode.id,
                wanted=True,
                progress=1.0 if done else 0.0,
                completed_at=datetime.now(UTC) if done else None,
            )
        )
    await session.flush()
    return torrent


def _pack_file(data_dir: Path, index: int) -> Path:
    return data_dir / "downloads" / "batch" / PACK_HASH / "Iruma S2" / IRUMA.format(index + 1)


class TestInFlight:
    async def test_a_sparse_file_under_a_downloading_pack_is_not_indexed(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        await _pack(db_session, 3)
        for index in range(3):
            sparse(_pack_file(data_dir, index))

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.added == 0
        assert result.skipped_unfinished == 3
        assert await rows(db_session) == []
        assert await match_jobs(db_session) == []

    async def test_the_same_file_is_indexed_once_it_is_complete(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        await _pack(db_session, 3, complete=frozenset({1}))
        for index in range(3):
            sparse(_pack_file(data_dir, index))

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.added == 1
        assert result.skipped_unfinished == 2
        assert [row.path for row in await rows(db_session)] == [
            str(_pack_file(data_dir, 1).resolve())
        ]

    async def test_a_file_under_a_pack_dir_with_no_row_for_it_is_not_indexed(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        """Only a file the rows say is complete leaves a pack's directory."""
        await _pack(db_session, 1, complete=frozenset({0}))
        make(data_dir / "downloads" / "batch" / PACK_HASH / "extra" / FRIEREN)

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.skipped_unfinished == 1
        assert {Path(row.path).name for row in await rows(db_session)} == set()

    async def test_a_singles_preallocated_file_is_not_indexed(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        episode = await _episode(db_session, 7)
        db_session.add(
            Torrent(
                episode_id=episode.id,
                info_hash=SINGLE_HASH,
                qbit_state="stalledDL",
                progress=0.0,
            )
        )
        await db_session.flush()
        directory = data_dir / "downloads" / str(episode.id)
        sparse(directory / IRUMA.format(7))
        make(directory / (IRUMA.format(8) + ".!qB"))

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.added == 0
        assert result.skipped_unfinished == 1
        assert result.skipped_partial == 1
        assert await rows(db_session) == []

    async def test_a_finished_single_flows_through(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        episode = await _episode(db_session, 7)
        db_session.add(
            Torrent(
                episode_id=episode.id,
                info_hash=SINGLE_HASH,
                qbit_state="stoppedUP",
                progress=1.0,
                completed_at=datetime.now(UTC),
            )
        )
        await db_session.flush()
        make(data_dir / "downloads" / str(episode.id) / IRUMA.format(7))

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.added == 1
        assert result.skipped_unfinished == 0

    async def test_a_stalled_singles_directory_is_the_librarys_again(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        """A decided single is not downloading; its directory is not fenced."""
        episode = await _episode(db_session, 7)
        db_session.add(Torrent(episode_id=episode.id, info_hash=SINGLE_HASH, qbit_state="stalled"))
        await db_session.flush()
        make(data_dir / "downloads" / str(episode.id) / IRUMA.format(7))

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.skipped_unfinished == 0
        assert result.added == 1

    async def test_files_outside_any_torrent_are_unaffected(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        await _pack(db_session, 1)
        make(data_dir / "manual" / MUSHISHI)
        make(data_dir / "downloads" / FRIEREN)

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.added == 2
        assert result.skipped_unfinished == 0


class TestPruneUnfinished:
    async def _bogus(self, session: AsyncSession, path: Path, **fields: object) -> MediaFile:
        row = MediaFile(
            path=str(path.resolve()),
            size=760 * 1024 * 1024,
            parsed={"title": "Iruma"},
            review_state=fields.pop("review_state", ReviewState.PENDING),
            llm_suggestion={"anime_id": 1, "episode": 1, "dismissed": True},
            **fields,
        )
        session.add(row)
        await session.flush()
        return row

    async def test_only_pending_unlinked_rows_under_unfinished_files_go(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        await _pack(db_session, 4, complete=frozenset({3}))
        linked_episode = await _episode(db_session, 20)
        bogus = [await self._bogus(db_session, sparse(_pack_file(data_dir, i))) for i in (0, 1)]
        # Under an unfinished file, but a person already decided about it.
        ignored = await self._bogus(
            db_session, sparse(_pack_file(data_dir, 2)), review_state=ReviewState.IGNORED
        )
        # Complete file: its row is the landing path's and stays.
        landed = await self._bogus(
            db_session, make(_pack_file(data_dir, 3)), episode_id=linked_episode.id
        )
        # Unrelated review item.
        elsewhere = await self._bogus(db_session, make(data_dir / "manual" / MUSHISHI))

        doomed = await ingest.prune_unfinished(db_session, library_settings)

        assert {row.id for row in doomed} == {row.id for row in bogus}
        left = {row.id for row in await rows(db_session)}
        assert left == {ignored.id, landed.id, elsewhere.id}

    async def test_a_linked_row_is_never_pruned(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        await _pack(db_session, 1)
        episode = await _episode(db_session, 9)
        kept = await self._bogus(db_session, sparse(_pack_file(data_dir, 0)), episode_id=episode.id)
        assert await ingest.prune_unfinished(db_session, library_settings) == []
        assert [row.id for row in await rows(db_session)] == [kept.id]

    async def test_the_scan_prunes_what_it_meets_and_says_so(
        self, db_session: AsyncSession, library_settings: Settings, data_dir: Path
    ) -> None:
        await _pack(db_session, 2)
        for index in range(2):
            await self._bogus(db_session, sparse(_pack_file(data_dir, index)))

        result = await ingest.scan(db_session, library_settings, probe=False)

        assert result.pruned == 2
        assert result.added == 0
        assert await rows(db_session) == []

    async def test_the_cli_step_is_idempotent_and_has_a_dry_run(
        self,
        db_session: AsyncSession,
        library_settings: Settings,
        data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from arc.cli import build_parser, cmd_prune_unfinished_media

        await _pack(db_session, 2)
        for index in range(2):
            await self._bogus(db_session, sparse(_pack_file(data_dir, index)))
        await db_session.commit()

        dry = build_parser().parse_args(["prune-unfinished-media", "--dry-run"])
        assert await cmd_prune_unfinished_media(db_session, library_settings, dry) == 0
        captured = capsys.readouterr()
        assert "2 row(s) would be removed" in captured.err
        assert len(captured.out.splitlines()) == 2
        assert len(await rows(db_session)) == 2

        real = build_parser().parse_args(["prune-unfinished-media"])
        assert await cmd_prune_unfinished_media(db_session, library_settings, real) == 0
        assert "2 row(s) removed" in capsys.readouterr().err
        assert await cmd_prune_unfinished_media(db_session, library_settings, real) == 0
        assert "0 row(s) removed" in capsys.readouterr().err
        assert await rows(db_session) == []
