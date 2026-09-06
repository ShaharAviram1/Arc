"""Finding video files and turning them into ``media_files`` rows (FR-L1).

The scan walks ``DATA_DIR/downloads`` and ``DATA_DIR/manual`` and, for each
video file it has not seen before, writes one row and enqueues one
``match_file`` job. Everything here is written to be run again: the scan is on
a two-minute timer, and running it twice must not produce two rows, two jobs,
or two matches.

Four rules decide what is skipped, and each of them is a bug that would
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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import MediaFile, ReviewState
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

    parsed = parse(path.name)
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

    # One membership query for the whole scan, then one insert per new file.
    resolved = {path: str(path.resolve()) for path in candidates}
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
    if len(added) % every != 0:
        await session.commit()

    result = ScanResult(
        seen=seen,
        added=len(added),
        skipped_partial=partial,
        skipped_recent=recent,
        remaining=remaining,
        paths=tuple(added),
    )
    if result.added or result.skipped_recent or result.skipped_partial or result.remaining:
        log.info("library scan finished", extra=result.as_dict())
    return result


__all__ = [
    "PARTIAL_SUFFIXES",
    "ScanResult",
    "ingest_file",
    "is_partial",
    "is_video",
    "known_paths",
    "scan",
    "walk",
]
