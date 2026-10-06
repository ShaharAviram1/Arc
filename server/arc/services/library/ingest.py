"""Finding video files and turning them into ``media_files`` rows (FR-L1).

The scan walks ``DATA_DIR/downloads`` and ``DATA_DIR/manual`` and, for each
video file it has not seen before, writes one row and enqueues one
``match_file`` job. Everything here is written to be run again: the scan is on
a two-minute timer, and running it twice must not produce two rows, two jobs,
or two matches.

Five rules decide what is skipped, and each of them is a bug that would
otherwise reach a user.

* **Not a video.** The extension has to be in ``VIDEO_EXTENSIONS``, so the
  ``.nfo``, ``.txt`` and cover art that come with a release are ignored.
* **Not finished.** ``.part``, ``.!qB``, ``.crdownload`` and friends are what
  an in-progress download is called. So is a file whose mtime is inside
  :attr:`Settings.library_settle_seconds`: a copy that is still being written
  has a size and a duration that are both wrong, and the next scan is two
  minutes away.
* **Not visible.** Anything whose name starts with a dot, and anything inside
  a dot-directory — ``.Trash``, ``@eaDir``, a resource fork.
* **Not a torrent Arc is still downloading** (owner incident, 2026-10-06).
  Decided by the ``torrents`` / ``torrent_files`` rows, never by the file
  (:func:`in_flight`): qBittorrent creates a selected file at its **full
  size**, sparse, the moment the torrent starts, and a torrent stalled at 0 %
  never writes to it again — so the size is the finished size and the mtime is
  as old as the add, and the two tests above both pass on a file that holds no
  bytes at all. Such a file belongs to the poll's hand-off
  (``acquisition.jobs._complete`` / ``_complete_file``), which indexes it with
  the matcher's prior once the client says it is complete; a row the scan
  wrote earlier for one is deleted when the scan meets it again
  (:func:`prune_unfinished`).
* **Not new.** ``media_files.path`` is unique, so a path Arc already has a row
  for is left alone. That is what makes a rescan free and what makes this safe
  to run beside a qBittorrent hand-off that inserts the same row (M6).

Ingest deliberately does **not** match. It writes the row, enqueues the job,
and returns; matching talks to the catalogue and belongs on the queue where it
can be retried, spaced and watched (architecture.md §5.2).

One pass is **bounded**, which is what makes the first one survivable. An
existing library is thousands of files and one ffprobe each, and a job that
runs longer than ``WORKER_STALE_AFTER`` is assumed dead and requeued
underneath itself — so a pass indexes at most ``LIBRARY_SCAN_BATCH`` new
files, commits every ``LIBRARY_SCAN_COMMIT_EVERY`` of them, and logs how many
it left behind. The next pass is two minutes away and starts where this one
stopped, because a path that already has a row is skipped.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import MediaFile, ReviewState, Torrent, TorrentFile, TorrentKind
from arc.services.acquisition.qbit import (
    BATCH_DIR,
    COMPLETE_PROGRESS,
    DECIDED_STATES,
    QBIT_MISSING,
)
from arc.services.jobs.queue import enqueue
from arc.services.library.names import MATCH_FILE, match_dedupe_key
from arc.services.library.parser import parse
from arc.services.media.probe import probe_summary

log = logging.getLogger(__name__)

#: Suffixes a partial download carries while it is still being written.
#: qBittorrent uses ``.!qB``, aria2 ``.aria2``, browsers ``.crdownload``, and
#: everything else ``.part`` or ``.tmp``.
PARTIAL_SUFFIXES: frozenset[str] = frozenset(
    {".part", ".!qb", ".crdownload", ".aria2", ".tmp", ".partial", ".downloading"}
)


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What one pass over the library directories did."""

    seen: int = 0
    added: int = 0
    skipped_partial: int = 0
    skipped_recent: int = 0
    #: Files under a torrent Arc is still downloading (:func:`in_flight`).
    skipped_unfinished: int = 0
    #: Rows an earlier pass wrote for such a file, deleted by this one
    #: (:func:`prune_unfinished`).
    pruned: int = 0
    #: New files this pass ran out of budget for. They are not lost — the next
    #: pass finds them exactly as this one did — and a number that stays high
    #: over several passes is the signal that ``LIBRARY_SCAN_BATCH`` is too
    #: small for how fast files are arriving.
    remaining: int = 0
    #: Paths of the rows created, for the log and for the tests.
    paths: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "added": self.added,
            "skipped_partial": self.skipped_partial,
            "skipped_recent": self.skipped_recent,
            "skipped_unfinished": self.skipped_unfinished,
            "pruned": self.pruned,
            "remaining": self.remaining,
        }


def is_partial(path: Path) -> bool:
    """Whether the name says this download has not finished."""
    return path.suffix.lower() in PARTIAL_SUFFIXES


def is_video(path: Path, extensions: frozenset[str]) -> bool:
    return path.suffix.lower().lstrip(".") in extensions


def walk(root: Path) -> Iterator[Path]:
    """Every visible file under ``root``, in a stable order.

    ``os.walk`` rather than ``Path.rglob`` for one reason: it lets a hidden or
    system directory be pruned from ``dirnames`` so its contents are never
    stat-ed at all. A ``.Trash`` full of deleted episodes is common on a NAS
    and is not a small thing to walk.

    Classification is the caller's job — :func:`scan` has to *count* the
    partials it skipped, and a walker that had already dropped them could not
    tell it how many there were.
    """
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames if not name.startswith(".") and not name.startswith("@")
        ]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            yield Path(dirpath) / name


async def known_paths(session: AsyncSession, paths: list[str]) -> set[str]:
    """Which of ``paths`` already have a ``media_files`` row.

    One query for the whole scan. Asking per file would be a round trip per
    episode in the library, every two minutes, forever.
    """
    if not paths:
        return set()
    rows = await session.scalars(select(MediaFile.path).where(MediaFile.path.in_(paths)))
    return set(rows.all())


#: ``torrents.qbit_state`` values under which a **single** is not being
#: downloaded any more, whatever its progress says: Arc decided about it
#: (stalled, cancelled, rejected — the first two are deleted with their files)
#: or the client no longer has it. Its directory is the library's again.
_SINGLE_NOT_LIVE: frozenset[str] = DECIDED_STATES | {QBIT_MISSING}


@dataclass(frozen=True, slots=True)
class InFlight:
    """The paths a torrent Arc tracks is still downloading (owner incident 2026-10-06).

    Built from the rows, never from the files: a sparse pre-allocated file
    reports its finished size and an mtime as old as the add, so nothing about
    the file itself says it is empty.

    ``singles`` are the episode directories (``<downloads>/<episode id>``) of
    every single whose download is live and not complete — every file under one
    is unfinished. ``packs`` maps each batch's directory
    (``<downloads>/batch/<hash>``) to the files in it that **are** complete
    (their ``torrent_files`` row has ``completed_at``, or progress at
    :data:`~arc.services.acquisition.qbit.COMPLETE_PROGRESS`); anything else
    under that directory is unfinished, whatever state the pack is in.
    """

    singles: frozenset[str] = frozenset()
    packs: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def holds(self, absolute: str) -> bool:
        """Whether ``absolute`` (a resolved path) is a file still being downloaded."""
        for directory in self.singles:
            if absolute.startswith(directory + os.sep):
                return True
        for directory, complete in self.packs.items():
            if absolute.startswith(directory + os.sep):
                return absolute not in complete
        return False


async def in_flight(session: AsyncSession, settings: Settings) -> InFlight:
    """What :class:`InFlight` says, for the torrents in the database now.

    Two queries. The directories are derived from the ids the way the client was
    told them (``qbit.save_path_for`` / ``qbit.batch_save_path_for``), on the
    worker's side of the mount, so no path reported by another process is
    trusted here.
    """
    root = settings.downloads_dir
    singles = await session.scalars(
        select(Torrent.episode_id).where(
            Torrent.kind == TorrentKind.SINGLE,
            Torrent.episode_id.is_not(None),
            Torrent.completed_at.is_(None),
            or_(Torrent.progress.is_(None), Torrent.progress < COMPLETE_PROGRESS),
            or_(Torrent.qbit_state.is_(None), Torrent.qbit_state.not_in(sorted(_SINGLE_NOT_LIVE))),
        )
    )
    single_dirs = frozenset(str(root / str(episode_id)) for episode_id in singles.all())

    packs: dict[str, set[str]] = {}
    hashes = await session.execute(
        select(Torrent.id, Torrent.info_hash).where(Torrent.kind == TorrentKind.BATCH)
    )
    directory_of: dict[int, Path] = {}
    for torrent_id, info_hash in hashes.all():
        directory = root / BATCH_DIR / info_hash.lower()
        directory_of[torrent_id] = directory
        packs[str(directory)] = set()
    if directory_of:
        done = await session.execute(
            select(TorrentFile.torrent_id, TorrentFile.path).where(
                TorrentFile.torrent_id.in_(sorted(directory_of)),
                or_(
                    TorrentFile.completed_at.is_not(None),
                    TorrentFile.progress >= COMPLETE_PROGRESS,
                ),
            )
        )
        for torrent_id, relative in done.all():
            directory = directory_of[torrent_id]
            parts = PurePosixPath(relative).parts
            if ".." in parts:
                continue
            packs[str(directory)].add(str(directory.joinpath(*parts)))
    return InFlight(
        singles=single_dirs,
        packs={directory: frozenset(files) for directory, files in packs.items()},
    )


async def prune_unfinished(
    session: AsyncSession, settings: Settings, fence: InFlight | None = None
) -> list[MediaFile]:
    """Delete the rows an earlier scan wrote for files still being downloaded.

    Only a row that is still ``pending`` and linked to no episode — what the
    scan writes and the matcher sends to review — and whose path
    :meth:`InFlight.holds`. Anything a person confirmed or ignored, anything
    the matcher linked, and anything outside a tracked torrent's directory is
    left exactly as it is. The model's suggestion for the row lives on the row
    (``llm_suggestion``), dismissed or not, so it goes with it; a
    ``match_file`` / ``llm_suggest_match`` job still queued for it finds the row
    gone and does nothing.

    Flushed, not committed: the scan and ``arc.cli prune-unfinished-media`` own
    their transactions. Returns the deleted rows, for the log and the report.
    """
    fence = fence if fence is not None else await in_flight(session, settings)
    if not fence.singles and not fence.packs:
        return []
    prefix = str(settings.downloads_dir) + os.sep
    rows = await session.scalars(
        select(MediaFile).where(
            MediaFile.review_state == ReviewState.PENDING,
            MediaFile.episode_id.is_(None),
            MediaFile.path.startswith(prefix, autoescape=True),
        )
    )
    doomed = [row for row in rows.all() if fence.holds(row.path)]
    for row in doomed:
        await session.delete(row)
    if doomed:
        await session.flush()
        log.info(
            "rows for files still downloading were removed",
            extra={"count": len(doomed), "paths": [row.path for row in doomed][:20]},
        )
    return doomed


async def ingest_file(
    session: AsyncSession,
    settings: Settings,
    path: Path,
    *,
    probe: bool = True,
    expected: list[int] | None = None,
) -> MediaFile | None:
    """Create the ``media_files`` row for ``path`` and queue its match.

    Returns ``None`` when a row already exists, which is what makes the whole
    scan idempotent. The row is flushed, not committed: the caller owns the
    transaction, so the row and its job appear together or not at all.

    ``expected`` is ``[anime_id, episode_number]`` and is passed straight into
    the ``match_file`` payload as the matcher's prior (FR-L3). The *scan* never
    has one — a file that appeared on disk on its own says nothing about what
    Arc meant to download — but the qBittorrent hand-off always does, because
    Arc chose that release for that episode
    (:mod:`arc.services.acquisition.jobs`).
    """
    absolute = str(path.resolve())
    existing = await session.scalar(select(MediaFile).where(MediaFile.path == absolute))
    if existing is not None:
        return None

    parsed = parse(path.name, path=True)
    payload = parsed.as_dict()
    if probe:
        summary = await probe_summary(path)
        if summary is not None:
            payload["probe"] = summary

    try:
        size: int | None = path.stat().st_size
    except OSError:
        size = None

    media_file = MediaFile(
        path=absolute,
        size=size,
        parsed=payload,
        review_state=ReviewState.PENDING,
    )
    session.add(media_file)
    await session.flush()

    job_payload: dict[str, Any] = {"media_file_id": media_file.id}
    if expected is not None:
        job_payload["expected"] = list(expected)
    await enqueue(
        session,
        MATCH_FILE,
        job_payload,
        dedupe_key=match_dedupe_key(media_file.id),
    )
    log.info(
        "media file indexed",
        extra={
            "media_file_id": media_file.id,
            "path": absolute,
            "title": parsed.title,
            "episode": parsed.episode,
            "kind": parsed.kind,
        },
    )
    return media_file


async def scan(
    session: AsyncSession,
    settings: Settings,
    *,
    roots: tuple[Path, ...] | None = None,
    now: float | None = None,
    probe: bool = True,
    batch: int | None = None,
    commit_every: int | None = None,
) -> ScanResult:
    """One pass over the library directories.

    ``now`` is injectable so the settle window can be tested without sleeping.

    ``batch`` caps how many *new* files this pass indexes and ``commit_every``
    how many it indexes between commits; both default to their settings. The
    commits are the point: a pass that held one transaction open for a
    thousand ffprobes would outlive the worker's stale-job window and be
    requeued while it was still running. A pass that dies half way therefore
    keeps the batches it finished, which is exactly what should happen — those
    rows and their match jobs are correct, and the rest are picked up next
    time.
    """
    extensions = settings.video_extensions_set
    clock = time.time() if now is None else now
    settle = settings.library_settle_seconds

    seen = 0
    recent = 0
    partial = 0
    candidates: list[Path] = []
    for root in roots if roots is not None else settings.library_dirs:
        for path in walk(Path(root)):
            if is_partial(path):
                partial += 1
                continue
            if not is_video(path, extensions):
                continue
            seen += 1
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if clock - modified < settle:
                recent += 1
                continue
            candidates.append(path)

    # What the client is still writing, from the rows (owner incident,
    # 2026-10-06): skipped here, and any row an earlier pass wrote for one of
    # them removed, before anything is indexed.
    fence = await in_flight(session, settings)
    pruned = await prune_unfinished(session, settings, fence)
    resolved: dict[Path, str] = {}
    unfinished = 0
    for path in candidates:
        absolute = str(path.resolve())
        if fence.holds(absolute):
            unfinished += 1
            continue
        resolved[path] = absolute

    # One membership query for the whole scan, then one insert per new file.
    already = await known_paths(session, sorted(resolved.values()))
    fresh = [(path, absolute) for path, absolute in resolved.items() if absolute not in already]

    limit = settings.library_scan_batch if batch is None else batch
    every = settings.library_scan_commit_every if commit_every is None else commit_every
    remaining = max(0, len(fresh) - limit)

    added: list[str] = []
    for path, absolute in fresh[:limit]:
        media_file = await ingest_file(session, settings, path, probe=probe)
        if media_file is not None:
            added.append(absolute)
        if len(added) % every == 0 and added:
            await session.commit()
    if len(added) % every != 0 or (pruned and not added):
        await session.commit()

    result = ScanResult(
        seen=seen,
        added=len(added),
        skipped_partial=partial,
        skipped_recent=recent,
        skipped_unfinished=unfinished,
        pruned=len(pruned),
        remaining=remaining,
        paths=tuple(added),
    )
    if (
        result.added
        or result.skipped_recent
        or result.skipped_partial
        or result.skipped_unfinished
        or result.pruned
        or result.remaining
    ):
        log.info("library scan finished", extra=result.as_dict())
    return result


__all__ = [
    "PARTIAL_SUFFIXES",
    "InFlight",
    "ScanResult",
    "in_flight",
    "ingest_file",
    "is_partial",
    "is_video",
    "known_paths",
    "prune_unfinished",
    "scan",
    "walk",
]
