"""M21 T8: the leftover pack files sweep (FR-T1, architecture §5.7).

Files under ``downloads/batch/<hash>/`` that no episode claims any more, which
per-episode retention cannot see. Every test builds a pack in a throwaway
``DATA_DIR`` (``tmp_path``) and a qBittorrent stub; nothing outside the test's
temporary directory is created or deleted.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from arc.config import Settings
from arc.db import SessionFactory
from arc.main import create_app
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    PackPick,
    Torrent,
    TorrentFile,
    TorrentKind,
    UserRole,
)
from arc.services.acquisition import qbit as qbit_module
from arc.services.acquisition.qbit import QbitClient
from arc.services.jobs.registry import JobContext
from arc.services.retention import batch_sweep, sweep
from arc.services.retention import jobs as retention_jobs
from arc.services.retention.batch_sweep import (
    MIN_AGE,
    REASON_CLIENT_GONE,
    REASON_PRIORITY_ZERO,
    FileFacts,
    Leftover,
    leftover_files,
    leftover_reason,
    sweep_leftovers,
)
from arc.services.retention.names import RETENTION_SWEEP
from arc.services.retention.rules import BATCH_SWEEP_DRY_RUN_KEY, batch_sweep_dry_run
from tests.acquisition_helpers import QbitStub, acquisition_settings, force_transport, set_setting
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login

pytestmark = pytest.mark.pg

#: Bytes actually written into each test file, so its allocation is real.
WRITTEN = 3 * 4096
#: The size the torrent says each file has: the rest of it is sparse.
APPARENT = 4 * 1024 * 1024
OLD = 2 * 3600  # seconds: older than the one-hour floor
#: A torrent in the client that is none of the test packs.
OTHER = "ee" * 20


# --- Builders ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackFile:
    """One file of a test pack: its ``torrent_files`` row and its client row."""

    name: str
    wanted: bool = False
    priority: int = 0
    complete: bool = False
    #: Whether the file exists on disk at all.
    on_disk: bool = True
    #: Seconds since the file last changed.
    age: float = OLD


def write_file(path: Path, *, age: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(b"\x01" * WRITTEN)
        handle.truncate(APPARENT)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def pack_dir(settings: Settings, info_hash: str) -> Path:
    return settings.downloads_dir.resolve() / "batch" / info_hash


async def make_episode(session: AsyncSession, anilist_id: int) -> Episode:
    anime = Anime(anilist_id=anilist_id, title_romaji=f"Batch Sweep {anilist_id}", episodes=12)
    session.add(anime)
    await session.flush()
    episode = Episode(anime_id=anime.id, number=1, state=EpisodeState.UNAVAILABLE)
    session.add(episode)
    await session.flush()
    return episode


async def make_pack(
    session: AsyncSession,
    settings: Settings,
    stub: QbitStub | None,
    info_hash: str,
    files: Sequence[PackFile],
    *,
    qbit_state: str | None = "stoppedDL",
    client_state: str = "stoppedDL",
    in_client: bool = True,
) -> Torrent:
    """A pack row, its file rows, its files on disk and (optionally) the client's view."""
    torrent = Torrent(
        info_hash=info_hash,
        title=f"[Group] Batch Sweep {info_hash[:6]} (1080p)",
        kind=TorrentKind.BATCH,
        qbit_state=qbit_state,
        progress=0.3,
    )
    session.add(torrent)
    await session.flush()
    for index, item in enumerate(files):
        session.add(
            TorrentFile(
                torrent_id=torrent.id,
                file_index=index,
                path=item.name,
                size=APPARENT,
                wanted=item.wanted,
                priority=item.priority,
                progress=1.0 if item.complete else 0.2,
                completed_at=datetime.now(UTC) if item.complete else None,
            )
        )
        if item.on_disk:
            write_file(pack_dir(settings, info_hash) / item.name, age=item.age)
    await session.flush()
    if stub is not None and in_client:
        stub.add_torrent(info_hash, name=f"pack {info_hash[:6]}", state=client_state)
        stub.file_lists[info_hash] = [
            {
                "index": index,
                "name": item.name,
                "size": APPARENT,
                "priority": item.priority,
                "progress": 1.0 if item.complete else 0.2,
            }
            for index, item in enumerate(files)
        ]
    return torrent


@pytest.fixture
def data_settings(tmp_path: Path) -> Settings:
    return acquisition_settings(tmp_path)


@pytest.fixture
def stub() -> QbitStub:
    """A client holding one unrelated torrent: an empty listing is "unknown",
    so a test of "gone from the client" needs the listing to say something."""
    client = QbitStub()
    client.add_torrent(OTHER, name="something else", state="stalledUP")
    return client


@pytest.fixture
async def qbit(data_settings: Settings, stub: QbitStub) -> AsyncIterator[QbitClient]:
    async with QbitClient.from_settings(data_settings, transport=stub.transport()) as client:
        yield client


def names(found: Sequence[Leftover]) -> list[str]:
    return sorted(item.path.name for item in found)


# --- The pure rule -------------------------------------------------------------


def facts(**overrides: object) -> FileFacts:
    values: dict[str, object] = {
        "held_pack": False,
        "pack_downloading": False,
        "fenced": False,
        "library": False,
        "claimed": False,
        "in_client": True,
        "client_priority": 0,
        "age": timedelta(hours=2),
        "safe": True,
    }
    values.update(overrides)
    return FileFacts(**values)  # type: ignore[arg-type]


def test_the_pure_rule_keeps_by_default_and_names_both_reasons() -> None:
    assert leftover_reason(facts()) == REASON_PRIORITY_ZERO
    assert leftover_reason(facts(in_client=False, client_priority=None)) == REASON_CLIENT_GONE
    for keep in (
        facts(held_pack=True),
        facts(safe=False),
        facts(pack_downloading=True),
        facts(fenced=True),
        facts(library=True),
        facts(claimed=True),
        facts(age=MIN_AGE),
        facts(client_priority=1),
        facts(client_priority=None),
        facts(in_client=False, claimed=True),
    ):
        assert leftover_reason(keep) is None, keep


async def test_the_dry_run_switch_defaults_on(db_session: AsyncSession) -> None:
    assert await batch_sweep_dry_run(db_session) is True
    await set_setting(db_session, BATCH_SWEEP_DRY_RUN_KEY, "no")
    assert await batch_sweep_dry_run(db_session) is True, "a non-bool never turns deletion on"
    await set_setting(db_session, BATCH_SWEEP_DRY_RUN_KEY, False)
    assert await batch_sweep_dry_run(db_session) is False


# --- leftover_files: the acceptance cases --------------------------------------


async def test_a_claimed_file_in_a_sparse_pack_is_kept(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "a1" * 20,
        [
            PackFile("Show - 01.mkv", wanted=True, priority=1, complete=True),
            PackFile("Show - 02.mkv"),
        ],
    )

    found = await leftover_files(db_session, data_settings, qbit)

    assert names(found) == ["Show - 02.mkv"]


async def test_an_unclaimed_priority_zero_file_is_a_candidate(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    torrent = await make_pack(
        db_session, data_settings, stub, "a2" * 20, [PackFile("Show - 03.mkv")]
    )

    (item,) = await leftover_files(db_session, data_settings, qbit)

    assert item.path == pack_dir(data_settings, "a2" * 20) / "Show - 03.mkv"
    assert item.torrent_id == torrent.id
    assert item.reason == REASON_PRIORITY_ZERO
    # The bytes on the disk, not the sparse file's finished size (the two agree
    # on a filesystem that does not keep the file sparse, APFS for one).
    info = item.path.stat()
    assert item.size == min(info.st_size, info.st_blocks * 512) > 0


async def test_an_in_flight_path_is_kept(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """While the pack still owes a wanted file it is downloading, and ingest's
    fence holds every incomplete file of it: nothing is a candidate."""
    await make_pack(
        db_session,
        data_settings,
        stub,
        "a3" * 20,
        [
            PackFile("Show - 01.mkv", wanted=True, priority=1),
            PackFile("Show - 02.mkv"),
        ],
        client_state="stalledDL",
    )

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_a_file_younger_than_an_hour_is_kept(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "a4" * 20,
        [PackFile("Show - 04.mkv", age=50 * 60), PackFile("Show - 05.mkv")],
    )

    found = await leftover_files(db_session, data_settings, qbit)

    assert names(found) == ["Show - 05.mkv"]


async def test_a_media_file_row_alone_keeps_the_file(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session, data_settings, stub, "a5" * 20, [PackFile("Show - 06.mkv"), PackFile("x.mkv")]
    )
    path = pack_dir(data_settings, "a5" * 20) / "Show - 06.mkv"
    db_session.add(MediaFile(path=str(path), size=WRITTEN))
    await db_session.flush()

    found = await leftover_files(db_session, data_settings, qbit)

    assert names(found) == ["x.mkv"]


async def test_a_pack_awaiting_a_pick_is_kept_whole(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "a6" * 20,
        [PackFile("Show - 01.mkv"), PackFile("Show - 02.mkv", complete=True)],
        qbit_state="awaiting_pick",
    )

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_a_pending_pick_keeps_the_whole_dir(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """Whatever the pack's state says, a pending ``pack_picks`` row holds it."""
    torrent = await make_pack(
        db_session, data_settings, stub, "a7" * 20, [PackFile("Show - 01.mkv")]
    )
    episode = await make_episode(db_session, 981_007)
    db_session.add(
        PackPick(torrent_id=torrent.id, episode_id=episode.id, reason="unsure", candidates=[])
    )
    await db_session.flush()

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_a_pack_gone_from_the_client_leaves_candidates(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "a8" * 20,
        [PackFile("Show - 01.mkv", wanted=True, priority=1), PackFile("Show - 02.mkv")],
        qbit_state="missing",
        in_client=False,
    )
    # A stray file the torrent never named goes too: the client no longer has
    # the pack, so nothing of the directory is being fetched.
    write_file(pack_dir(data_settings, "a8" * 20) / "Extras" / "notes.txt")

    found = await leftover_files(db_session, data_settings, qbit)

    assert names(found) == ["Show - 02.mkv", "notes.txt"], "the wanted claim is still kept"
    assert {item.reason for item in found} == {REASON_CLIENT_GONE}


async def test_an_unreachable_qbittorrent_gives_no_candidates(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(db_session, data_settings, stub, "a9" * 20, [PackFile("Show - 01.mkv")])
    stub.down = True

    assert await leftover_files(db_session, data_settings, qbit) == []
    assert await leftover_files(db_session, data_settings, None) == [], "not configured"


async def test_a_file_the_client_still_fetches_is_kept(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """Priority above 0 in the client, even with no claim in Arc's rows."""
    await make_pack(
        db_session,
        data_settings,
        stub,
        "b1" * 20,
        # On but complete: the client is fetching nothing, the file stays.
        [PackFile("Show - 01.mkv", priority=1, complete=True), PackFile("Show - 02.mkv")],
    )

    found = await leftover_files(db_session, data_settings, qbit)

    assert names(found) == ["Show - 02.mkv"]


async def test_a_file_the_client_does_not_list_is_kept(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """No row, or the client's row names something else: no priority to read."""
    await make_pack(db_session, data_settings, stub, "b2" * 20, [PackFile("Show - 01.mkv")])
    write_file(pack_dir(data_settings, "b2" * 20) / ".resume.parts")
    stub.file_lists["b2" * 20][0]["name"] = "Something Else - 01.mkv"

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_a_symlink_is_refused(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    tmp_path: Path,
) -> None:
    await make_pack(db_session, data_settings, stub, "b3" * 20, [PackFile("Show - 01.mkv")])
    directory = pack_dir(data_settings, "b3" * 20)
    outside = write_file(tmp_path / "elsewhere" / "precious.mkv")
    other = write_file(tmp_path / "elsewhere" / "nested" / "also.mkv")
    (directory / "Show - 01.mkv").unlink()
    (directory / "Show - 01.mkv").symlink_to(outside)
    (directory / "Linked").symlink_to(other.parent, target_is_directory=True)

    assert await leftover_files(db_session, data_settings, qbit) == []
    await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)
    assert outside.exists() and other.exists()


async def test_a_symlinked_pack_directory_is_refused(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    tmp_path: Path,
) -> None:
    real = tmp_path / "real-pack"
    write_file(real / "Show - 01.mkv")
    batch = data_settings.downloads_dir.resolve() / "batch"
    batch.mkdir(parents=True)
    (batch / ("b4" * 20)).symlink_to(real, target_is_directory=True)
    await make_pack(
        db_session, data_settings, stub, "b4" * 20, [PackFile("Show - 01.mkv", on_disk=False)]
    )

    assert await leftover_files(db_session, data_settings, qbit) == []
    assert (real / "Show - 01.mkv").exists()


async def test_a_path_outside_the_allowed_roots_is_refused(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_pack(db_session, data_settings, stub, "b5" * 20, [PackFile("Show - 01.mkv")])
    monkeypatch.setattr(
        sweep, "allowed_roots", lambda settings: (settings.renditions_dir, settings.manual_dir)
    )

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_an_unknown_batch_directory_is_out_of_scope(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(db_session, data_settings, stub, "b6" * 20, [PackFile("Show - 01.mkv")])
    stray = write_file(pack_dir(data_settings, "ff" * 20) / "Unknown - 01.mkv")

    found = await leftover_files(db_session, data_settings, qbit)

    assert names(found) == ["Show - 01.mkv"]
    assert stray not in [item.path for item in found]


async def test_a_pack_the_client_is_checking_is_kept_whole(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "b7" * 20,
        [PackFile("Show - 01.mkv")],
        client_state="checkingDL",
    )

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_an_incomplete_file_suffix_is_the_same_file(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """qBittorrent's optional ``.!qB`` suffix names the torrent's own file."""
    await make_pack(
        db_session,
        data_settings,
        stub,
        "b8" * 20,
        [
            PackFile("Show - 01.mkv", wanted=True, priority=1, complete=True),
            PackFile("Show - 02.mkv", on_disk=False),
            # Claimed: its suffixed name must still find the claim.
            PackFile("Show - 03.mkv", wanted=True, priority=1, complete=True, on_disk=False),
        ],
    )
    write_file(pack_dir(data_settings, "b8" * 20) / "Show - 02.mkv.!qB")
    write_file(pack_dir(data_settings, "b8" * 20) / "Show - 03.mkv.!qB")

    found = await leftover_files(db_session, data_settings, qbit)

    assert names(found) == ["Show - 02.mkv.!qB"]


# --- Deleting -----------------------------------------------------------------


async def test_a_dry_run_deletes_nothing_and_logs(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await make_pack(db_session, data_settings, stub, "c1" * 20, [PackFile("Season/Show - 01.mkv")])
    path = pack_dir(data_settings, "c1" * 20) / "Season" / "Show - 01.mkv"

    with caplog.at_level(logging.INFO, logger=batch_sweep.__name__):
        result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=True)

    assert path.exists()
    assert result.candidates == 1 and result.deleted == 0 and result.dry_run
    assert result.freed_bytes > 0, "a dry run reports what it would free"
    (record,) = [r for r in caplog.records if "would delete" in r.getMessage()]
    assert record.levelno == logging.INFO
    assert record.__dict__["path"] == str(path)
    assert record.__dict__["bytes"] > 0
    assert record.__dict__["reason"] == REASON_PRIORITY_ZERO


async def test_a_real_run_deletes_the_candidates_prunes_the_empty_dir_and_leaves_the_rest(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "c2" * 20,
        [
            PackFile("Show - 01.mkv", wanted=True, priority=1, complete=True),
            PackFile("Season 2/Show - 13.mkv"),
            PackFile("Season 2/Deep/Show - 14.mkv"),
            PackFile("Show - 02.mkv", age=10 * 60),
        ],
    )
    directory = pack_dir(data_settings, "c2" * 20)

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.deleted == 2 and result.candidates == 2 and not result.dry_run
    assert not (directory / "Season 2").exists(), "the emptied directories are pruned"
    assert (directory / "Show - 01.mkv").exists()
    assert (directory / "Show - 02.mkv").exists()
    assert directory.is_dir(), "the client still has the pack: its directory stays"
    # Idempotent: a second run finds nothing more to do.
    again = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)
    assert again.candidates == 0 and again.deleted == 0


async def test_a_gone_pack_loses_its_emptied_directory(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "c3" * 20,
        [PackFile("Show - 01.mkv")],
        qbit_state="missing",
        in_client=False,
    )

    await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert not pack_dir(data_settings, "c3" * 20).exists()
    assert (data_settings.downloads_dir / "batch").is_dir(), "never above the pack"


async def test_one_failing_file_does_not_stop_the_others(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "c4" * 20,
        [PackFile("Show - 01.mkv"), PackFile("Show - 02.mkv")],
    )
    directory = pack_dir(data_settings, "c4" * 20)
    real = batch_sweep.remove_file

    def flaky(path: Path, settings: Settings) -> bool:
        if path.name == "Show - 01.mkv":
            raise PermissionError("not today")
        return real(path, settings)

    monkeypatch.setattr(batch_sweep, "remove_file", flaky)

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.deleted == 1
    assert (directory / "Show - 01.mkv").exists()
    assert not (directory / "Show - 02.mkv").exists()


async def test_a_file_claimed_after_the_plan_is_kept(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim is asked again right before the unlink."""
    torrent = await make_pack(
        db_session, data_settings, stub, "c6" * 20, [PackFile("Show - 01.mkv")]
    )
    real = batch_sweep.leftover_files

    async def then_claimed(*args: object, **kwargs: object) -> list[Leftover]:
        found = await real(*args, **kwargs)  # type: ignore[arg-type]
        row = await db_session.scalar(
            select(TorrentFile).where(TorrentFile.torrent_id == torrent.id)
        )
        assert row is not None
        row.wanted = True
        await db_session.flush()
        return found

    monkeypatch.setattr(batch_sweep, "leftover_files", then_claimed)

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.candidates == 1 and result.deleted == 0
    assert (pack_dir(data_settings, "c6" * 20) / "Show - 01.mkv").exists()


async def test_a_downloading_pack_is_untouched(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "c5" * 20,
        [
            PackFile("Show - 01.mkv", wanted=True, priority=1),
            PackFile("Show - 02.mkv"),
            # Complete, unclaimed, switched off: a leftover anywhere else, but
            # a downloading pack is kept whole (orchestrator, 2026-11-04).
            PackFile("Show - 03.mkv", complete=True),
        ],
        client_state="downloading",
    )
    directory = pack_dir(data_settings, "c5" * 20)
    before = sorted(path.name for path in directory.iterdir())

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.candidates == 0
    assert sorted(path.name for path in directory.iterdir()) == before


# --- The hourly job ---------------------------------------------------------------


def context(session: AsyncSession, settings: Settings) -> JobContext:
    job = Job(type=RETENTION_SWEEP, payload={}, status=JobStatus.RUNNING, attempts=1)
    job.id = 1
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("arc.jobs.test")
    )


def recorded(order: list[str], label: str, real: Any) -> Any:
    async def wrapper(*args: object, **kwargs: object) -> object:
        order.append(label)
        return await real(*args, **kwargs)

    return wrapper


async def test_the_hourly_job_runs_the_step_after_the_sweep(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, stub.transport()),
    )
    await make_pack(db_session, data_settings, stub, "d1" * 20, [PackFile("Show - 01.mkv")])
    path = pack_dir(data_settings, "d1" * 20) / "Show - 01.mkv"
    order: list[str] = []
    for name, label in (
        ("candidates", "sweep"),
        ("idle_copies", "idle"),
        ("_evict_to_floor", "floor"),
        ("sweep_leftovers", "leftovers"),
        ("prune_remembered", "remembered"),
    ):
        monkeypatch.setattr(
            retention_jobs, name, recorded(order, label, getattr(retention_jobs, name))
        )

    # Seeded dry: logs only.
    await retention_jobs.retention_sweep(context(db_session, data_settings))
    assert order == ["sweep", "idle", "floor", "leftovers", "remembered"]
    assert path.exists()

    # Switched off from the panel: the file goes.
    await set_setting(db_session, BATCH_SWEEP_DRY_RUN_KEY, False)
    await retention_jobs.retention_sweep(context(db_session, data_settings))
    assert not path.exists()


async def test_the_environment_dry_run_also_keeps_the_leftovers(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, stub.transport()),
    )
    await make_pack(db_session, data_settings, stub, "d2" * 20, [PackFile("Show - 01.mkv")])
    await set_setting(db_session, BATCH_SWEEP_DRY_RUN_KEY, False)
    dry = data_settings.model_copy(update={"retention_dry_run": True})

    await retention_jobs.retention_sweep(context(db_session, dry))

    assert (pack_dir(data_settings, "d2" * 20) / "Show - 01.mkv").exists()


async def test_the_hourly_job_survives_an_unconfigured_qbittorrent(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    bare = acquisition_settings(tmp_path, qbit_user=None)
    write_file(bare.downloads_dir / "batch" / ("d3" * 20) / "Show - 01.mkv")

    await retention_jobs.retention_sweep(context(db_session, bare))

    assert (bare.downloads_dir / "batch" / ("d3" * 20) / "Show - 01.mkv").exists()


# --- GET /api/retention/preview -----------------------------------------------------


@pytest.fixture
def preview_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "data_dir": tmp_path,
            "qbit_url": "http://qbit.test",
            "qbit_user": "admin",
            "qbit_pass": SecretStr("adminadmin"),
            "qbit_category": "arc",
        }
    )


@pytest.fixture
def preview_app(
    preview_settings: Settings, pg_engine: AsyncEngine, api_factory: SessionFactory
) -> FastAPI:
    app = create_app(preview_settings)
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    return app


@pytest.fixture
def patched_qbit(stub: QbitStub, monkeypatch: pytest.MonkeyPatch) -> QbitStub:
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, stub.transport()),
    )
    return stub


async def test_the_preview_reports_leftover_files_and_bytes(
    preview_app: FastAPI,
    preview_settings: Settings,
    api_factory: SessionFactory,
    patched_qbit: QbitStub,
) -> None:
    async with api_factory() as session:
        torrent = await make_pack(
            session,
            preview_settings,
            patched_qbit,
            "e1" * 20,
            [
                PackFile("Show - 01.mkv", wanted=True, priority=1, complete=True),
                PackFile("Show - 02.mkv"),
            ],
        )
        await session.commit()
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(preview_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        response = await client.get("/api/retention/preview")

    assert response.status_code == 200, response.text
    body = response.json()
    (row,) = body["leftover_files"]
    assert row["path"] == str(pack_dir(preview_settings, "e1" * 20) / "Show - 02.mkv")
    assert row["torrent_id"] == torrent.id
    assert row["reason"] == REASON_PRIORITY_ZERO
    assert body["leftover_bytes"] == row["bytes"] > 0
    assert body["batch_sweep_dry_run"] is True
    assert body["leftover_checked"] is True
    assert (pack_dir(preview_settings, "e1" * 20) / "Show - 02.mkv").exists(), "read-only"


async def test_the_preview_says_when_qbittorrent_could_not_be_asked(
    preview_app: FastAPI,
    preview_settings: Settings,
    api_factory: SessionFactory,
    patched_qbit: QbitStub,
) -> None:
    async with api_factory() as session:
        await make_pack(session, preview_settings, patched_qbit, "e2" * 20, [PackFile("x.mkv")])
        await session.commit()
    patched_qbit.down = True
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(preview_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = (await client.get("/api/retention/preview")).json()

    assert body["leftover_files"] == [] and body["leftover_bytes"] == 0
    assert body["leftover_checked"] is False


async def test_a_non_admin_gets_403_on_the_preview(
    preview_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, "member-batch-sweep@arc.test", "memberpassword")
    async with api_transport(preview_app) as client:
        await login(client, "member-batch-sweep@arc.test", "memberpassword")
        assert (await client.get("/api/retention/preview")).status_code == 403


# --- Fix round 1 (Validator / Reviewer, 2026-11-04) ----------------------------------


async def test_an_empty_listing_gives_no_candidates(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """An empty listing is unknown, not "every pack is gone"."""
    await make_pack(
        db_session,
        data_settings,
        stub,
        "f1" * 20,
        [PackFile("Show - 01.mkv")],
        qbit_state="missing",
        in_client=False,
    )
    stub.torrents = []

    assert await leftover_files(db_session, data_settings, qbit) == []
    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)
    assert result.candidates == 0
    assert (pack_dir(data_settings, "f1" * 20) / "Show - 01.mkv").exists()


async def test_a_pack_moved_to_another_category_is_kept_whole(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """Absent from Arc's category is not gone: ``has`` finds it elsewhere."""
    await make_pack(
        db_session,
        data_settings,
        stub,
        "f2" * 20,
        [PackFile("Show - 01.mkv", priority=1), PackFile("Show - 02.mkv")],
        qbit_state="missing",
    )
    for row in stub.torrents:
        if row["hash"] == "f2" * 20:
            row["category"] = "mine"

    assert await leftover_files(db_session, data_settings, qbit) == []
    await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)
    assert sorted(p.name for p in pack_dir(data_settings, "f2" * 20).iterdir()) == [
        "Show - 01.mkv",
        "Show - 02.mkv",
    ]


async def test_a_pack_missing_from_a_partial_listing_is_kept(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """A row the client sent without a hash is dropped from the listing; the pack
    looks absent, but its row does not say it is gone, so nothing is swept."""
    await make_pack(
        db_session,
        data_settings,
        stub,
        "f3" * 20,
        [PackFile("Show - 01.mkv")],
        qbit_state="stoppedDL",
        in_client=False,
    )
    stub.torrents.append({"hash": "", "name": "unreadable row", "category": "arc"})

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_a_gone_pack_needs_its_row_to_agree(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "f4" * 20,
        [PackFile("Show - 01.mkv")],
        qbit_state="stoppedDL",
        in_client=False,
    )

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_a_real_run_rechecks_a_pack_the_client_still_holds_once(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "f5" * 20,
        [PackFile("Show - 01.mkv"), PackFile("Show - 02.mkv"), PackFile("Show - 03.mkv")],
    )

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.deleted == 3
    assert stub.rechecked == ["f5" * 20]
    assert result.rechecked == ("f5" * 20,)


async def test_a_gone_pack_and_a_dry_run_are_never_rechecked(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "f6" * 20,
        [PackFile("Show - 01.mkv")],
        qbit_state="missing",
        in_client=False,
    )
    await make_pack(db_session, data_settings, stub, "f7" * 20, [PackFile("Show - 01.mkv")])

    dry = await sweep_leftovers(db_session, data_settings, qbit, dry_run=True)
    assert dry.candidates == 2 and stub.rechecked == []

    real = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)
    assert real.deleted == 2
    assert stub.rechecked == ["f7" * 20], "only the pack the client still holds"


async def test_a_reselect_between_planning_and_unlinking_keeps_the_file(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client is asked again right before the pack's first unlink."""
    torrent = await make_pack(
        db_session, data_settings, stub, "f8" * 20, [PackFile("Show - 01.mkv")]
    )
    real = batch_sweep.leftover_files

    async def then_reselected(*args: object, **kwargs: object) -> list[Leftover]:
        found = await real(*args, **kwargs)  # type: ignore[arg-type]
        row = await db_session.scalar(
            select(TorrentFile).where(TorrentFile.torrent_id == torrent.id)
        )
        assert row is not None
        row.wanted = True
        await db_session.flush()
        stub.file_lists["f8" * 20][0]["priority"] = 1
        return found

    monkeypatch.setattr(batch_sweep, "leftover_files", then_reselected)

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.candidates == 1 and result.deleted == 0
    assert stub.rechecked == []
    assert (pack_dir(data_settings, "f8" * 20) / "Show - 01.mkv").exists()


async def test_a_client_side_reselect_alone_keeps_the_file(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_pack(db_session, data_settings, stub, "f9" * 20, [PackFile("Show - 01.mkv")])
    real = batch_sweep.leftover_files

    async def then_reselected(*args: object, **kwargs: object) -> list[Leftover]:
        found = await real(*args, **kwargs)  # type: ignore[arg-type]
        stub.file_lists["f9" * 20][0]["priority"] = 1
        return found

    monkeypatch.setattr(batch_sweep, "leftover_files", then_reselected)

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.deleted == 0
    assert (pack_dir(data_settings, "f9" * 20) / "Show - 01.mkv").exists()


async def test_a_db_error_in_the_step_leaves_the_following_steps_running(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, stub.transport()),
    )
    await set_setting(db_session, BATCH_SWEEP_DRY_RUN_KEY, False)
    await db_session.commit()

    async def broken(session: AsyncSession, *args: object, **kwargs: object) -> object:
        await session.execute(text("SELECT 1 / 0"))
        raise AssertionError("unreachable")

    order: list[str] = []

    async def live(*args: object, **kwargs: object) -> None:
        order.append("live")

    monkeypatch.setattr(retention_jobs, "sweep_leftovers", broken)
    monkeypatch.setattr(retention_jobs, "sweep_live_leftovers", live)
    monkeypatch.setattr(
        retention_jobs,
        "prune_remembered",
        recorded(order, "remembered", retention_jobs.prune_remembered),
    )

    await retention_jobs.retention_sweep(context(db_session, data_settings))

    assert order == ["live", "remembered"], "the real prune ran on a usable session"


async def test_a_files_error_holds_that_pack_and_the_others_are_still_evaluated(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(db_session, data_settings, stub, "1a" * 20, [PackFile("Show - 01.mkv")])
    await make_pack(db_session, data_settings, stub, "1b" * 20, [PackFile("Show - 01.mkv")])
    # A row with no usable name: ``files()`` refuses the whole listing.
    stub.file_lists["1a" * 20] = [{"index": 0, "size": APPARENT, "priority": 0}]

    found = await leftover_files(db_session, data_settings, qbit)

    assert [item.path.parent.name for item in found] == ["1b" * 20]


async def test_the_clients_parts_file_is_kept_while_it_holds_the_pack(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "1c" * 20,
        [PackFile("Show - 01.mkv", wanted=True, priority=1, complete=True)],
    )
    parts = write_file(pack_dir(data_settings, "1c" * 20) / f".{'1c' * 20}.parts")

    assert await leftover_files(db_session, data_settings, qbit) == []
    await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)
    assert parts.exists()


async def test_nested_pruning_stops_at_the_pack_and_never_takes_batch(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(db_session, data_settings, stub, "1d" * 20, [PackFile("A/B/C/Show - 01.mkv")])
    await make_pack(
        db_session,
        data_settings,
        stub,
        "1e" * 20,
        [PackFile("A/B/Show - 01.mkv")],
        qbit_state="missing",
        in_client=False,
    )

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.deleted == 2
    held = pack_dir(data_settings, "1d" * 20)
    assert held.is_dir() and list(held.iterdir()) == [], "still in the client: kept, emptied"
    assert not pack_dir(data_settings, "1e" * 20).exists(), "gone: the pack dir goes too"
    assert (data_settings.downloads_dir / "batch").is_dir()


async def test_a_unicode_nested_pack_is_swept_and_pruned_to_the_pack(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "1f" * 20,
        [
            PackFile("進撃の巨人 S1/Disc 1/進撃 - 01.mkv"),
            PackFile("進撃の巨人 S1/Disc 1/進撃 - 02.mkv", wanted=True, priority=1, complete=True),
            PackFile("進撃の巨人 S1/Extras/Ñandú – OP.mkv"),
        ],
    )
    directory = pack_dir(data_settings, "1f" * 20)

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.deleted == 2
    assert not (directory / "進撃の巨人 S1" / "Extras").exists()
    assert (directory / "進撃の巨人 S1" / "Disc 1" / "進撃 - 02.mkv").exists()
    assert not (directory / "進撃の巨人 S1" / "Disc 1" / "進撃 - 01.mkv").exists()
    assert directory.is_dir()


async def test_the_preview_reports_the_effective_dry_run(
    preview_app: FastAPI,
    preview_settings: Settings,
    api_factory: SessionFactory,
    patched_qbit: QbitStub,
) -> None:
    """``RETENTION_DRY_RUN`` keeps the sweep dry whatever the setting says."""
    async with api_factory() as session:
        await set_setting(session, BATCH_SWEEP_DRY_RUN_KEY, False)
        await session.commit()
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    app = create_app(preview_settings.model_copy(update={"retention_dry_run": True}))
    app.state.engine = preview_app.state.engine
    app.state.session_factory = api_factory
    async with api_transport(app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        dry = (await client.get("/api/retention/preview")).json()
    async with api_transport(preview_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        live = (await client.get("/api/retention/preview")).json()

    assert dry["batch_sweep_dry_run"] is True
    assert live["batch_sweep_dry_run"] is False


async def test_the_preview_calls_an_empty_listing_unchecked(
    preview_app: FastAPI,
    preview_settings: Settings,
    api_factory: SessionFactory,
    patched_qbit: QbitStub,
) -> None:
    async with api_factory() as session:
        await make_pack(
            session,
            preview_settings,
            patched_qbit,
            "2a" * 20,
            [PackFile("x.mkv")],
            qbit_state="missing",
            in_client=False,
        )
        await session.commit()
    patched_qbit.torrents = []
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(preview_app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = (await client.get("/api/retention/preview")).json()

    assert body["leftover_files"] == [] and body["leftover_checked"] is False


# --- Fix round 2: the client's own word on downloading ----------------------------


async def test_a_pack_the_client_is_still_fetching_is_kept_whole(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """No row is wanted (released, the re-selection not yet run), but the client
    still has file 01 on and incomplete: the pack is downloading, kept whole."""
    await make_pack(
        db_session,
        data_settings,
        stub,
        "3a" * 20,
        [PackFile("Show - 01.mkv", priority=1), PackFile("Show - 02.mkv", complete=True)],
        client_state="stoppedDL",
    )

    assert await leftover_files(db_session, data_settings, qbit) == []
    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)
    assert result.candidates == 0
    assert (pack_dir(data_settings, "3a" * 20) / "Show - 02.mkv").exists()


async def test_an_active_download_state_keeps_the_pack_whole(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "3b" * 20,
        [PackFile("Show - 01.mkv"), PackFile("Show - 02.mkv", complete=True)],
        client_state="downloading",
    )

    assert await leftover_files(db_session, data_settings, qbit) == []


async def test_a_pack_the_client_fetches_nothing_in_still_yields_its_candidate(
    db_session: AsyncSession, data_settings: Settings, stub: QbitStub, qbit: QbitClient
) -> None:
    """The same pack once the client's files are all complete or off."""
    await make_pack(
        db_session,
        data_settings,
        stub,
        "3c" * 20,
        [
            PackFile("Show - 01.mkv", priority=1, complete=True),
            PackFile("Show - 02.mkv", complete=True),
        ],
        client_state="stoppedUP",
    )

    found = await leftover_files(db_session, data_settings, qbit)

    assert names(found) == ["Show - 02.mkv"]


async def test_the_client_starting_to_fetch_before_the_unlink_keeps_the_pack(
    db_session: AsyncSession,
    data_settings: Settings,
    stub: QbitStub,
    qbit: QbitClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_pack(
        db_session,
        data_settings,
        stub,
        "3d" * 20,
        [PackFile("Show - 01.mkv", priority=1, complete=True), PackFile("Show - 02.mkv")],
    )
    real = batch_sweep.leftover_files

    async def then_fetching(*args: object, **kwargs: object) -> list[Leftover]:
        found = await real(*args, **kwargs)  # type: ignore[arg-type]
        for row in stub.torrents:
            if row["hash"] == "3d" * 20:
                row["state"] = "downloading"
        return found

    monkeypatch.setattr(batch_sweep, "leftover_files", then_fetching)

    result = await sweep_leftovers(db_session, data_settings, qbit, dry_run=False)

    assert result.candidates == 1 and result.deleted == 0
    assert (pack_dir(data_settings, "3d" * 20) / "Show - 02.mkv").exists()
