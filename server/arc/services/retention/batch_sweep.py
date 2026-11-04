"""Leftover pack files: what per-episode retention cannot see (FR-T1, M21 T8).

A want withdrawn while its file in a season pack was downloading leaves the
bytes that had arrived inside ``downloads/batch/<hash>/`` with no
``media_files`` row naming them and no episode to charge them to. The
per-episode sweep (:mod:`arc.services.retention.sweep`) works from rows, so it
never sees them, and the pack itself may never reach ``batch.disposition``'s
DELETE while somebody keeps the show on a list. This module finds such files
and, as one step of the hourly ``retention_sweep``, removes them.

**The rule is a conjunction, and every half of it says "keep" by default**
(:func:`leftover_reason`). A file under the batch root is a candidate only
when *all* of these hold:

* its directory is a pack Arc knows (a ``torrents`` row of kind ``batch``) —
  an unknown ``batch/<dir>`` is out of scope (the library fence already keeps
  it out of the library);
* the pack is not **held**: not ``awaiting_pick`` (or ``cancelled``, whose
  ``qbit_cancel`` job owns the deletion), no pending ``pack_picks`` row, and
  the client is not re-reading its files (a check, a move) — while any of
  these is true the whole directory is untouchable;
* it is not **in flight**: while the pack is in the client and still owes Arc
  a wanted file that is not complete, :meth:`~arc.services.library.ingest.InFlight.holds` is applied
  as it is (every incomplete file of the pack is kept); once the pack owes
  nothing, an incomplete file is no longer being ingested;
* no ``media_files.path`` names it, and no ``torrent_files`` row with
  ``wanted = true`` claims it;
* qBittorrent reports **priority 0** for its index, or the torrent is **gone
  from the client** altogether;
* it has not changed for an hour (:data:`MIN_AGE`), and
* it is under :func:`~arc.services.retention.sweep.allowed_roots` with no
  symlink on its path (:func:`~arc.services.retention.sweep.safe_path`, plus a
  resolve that must change nothing).

**qBittorrent unreachable means nothing is a candidate** (fail closed): the
priority half of the rule cannot be answered, and "the torrent is gone" must
never be inferred from a client that did not answer.

The deletion goes through :mod:`arc.services.retention.delete`'s own file
primitive (the confinement re-check right before ``unlink``) and prunes the
directories it leaves empty. While ``batch_sweep_dry_run`` is true (seeded on)
— or ``RETENTION_DRY_RUN`` is — each would-be deletion is logged at INFO with
its path, bytes and reason, and nothing is removed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import MediaFile, PackPick, Torrent, TorrentFile, TorrentKind
from arc.services.acquisition.qbit import (
    BATCH_DIR,
    COMPLETE_PROGRESS,
    FILE_OFF,
    QBIT_AWAITING_PICK,
    QBIT_CANCELLED,
    FileInfo,
    QbitClient,
    QbitError,
    QbitUnavailable,
)
from arc.services.library.ingest import InFlight, in_flight
from arc.services.retention.delete import prune_empty_dirs, remove_file
from arc.services.retention.sweep import safe_path

log = logging.getLogger(__name__)

#: A file must not have changed for this long before it is a leftover. An hour
#: is far longer than any write the client makes to a file it has just switched
#: off, and short enough that the bytes do not linger for a day.
MIN_AGE: Final[timedelta] = timedelta(hours=1)

#: ``torrents.qbit_state`` values under which a pack's directory is left whole:
#: held for a person's pick (FR-A4, M21 T7), or being deleted with its files by
#: the ``qbit_cancel`` job, which owns that deletion.
HELD_PACK_STATES: Final[frozenset[str]] = frozenset({QBIT_AWAITING_PICK, QBIT_CANCELLED})

#: Client states under which the client is reading or moving the pack's files
#: (a recheck, a resume check, a move). The same set as
#: :data:`arc.services.acquisition.jobs.CHECKING_STATES` — copied rather than
#: imported because that module registers job handlers on import, and the API
#: process imports this one; a test holds the two equal.
BUSY_CLIENT_STATES: Final[frozenset[str]] = frozenset(
    {"checkingDL", "checkingUP", "queuedForChecking", "checkingResumeData", "moving", "allocating"}
)

#: qBittorrent's optional suffix for an incomplete file ("Append .!qB extension
#: to incomplete files"). The file is the same torrent file either way.
INCOMPLETE_SUFFIX: Final[str] = ".!qB"

REASON_PRIORITY_ZERO: Final[str] = "qBittorrent has this file switched off and no episode claims it"
REASON_CLIENT_GONE: Final[str] = (
    "the pack is no longer in qBittorrent and no episode claims this file"
)


@dataclass(frozen=True, slots=True)
class Leftover:
    """One file the leftover sweep would delete."""

    path: Path
    #: Bytes actually allocated on the disk (a sparse file's apparent size is
    #: its finished size, which is not what deleting it frees).
    size: int
    torrent_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class FileFacts:
    """Everything :func:`leftover_reason` decides from, gathered elsewhere."""

    #: The pack is held: awaiting a pick, a pending ``pack_picks`` row, being
    #: cancelled, or the client is checking its files.
    held_pack: bool
    #: The pack still owes Arc a wanted file that is not complete.
    pack_downloading: bool
    #: :meth:`InFlight.holds` for the file.
    fenced: bool
    #: A ``media_files.path`` names the file.
    library: bool
    #: A ``torrent_files`` row with ``wanted = true`` claims the file.
    claimed: bool
    #: The torrent is in Arc's category in the client.
    in_client: bool
    #: The client's priority for the file's index; ``None`` when no row or no
    #: client listing names it.
    client_priority: int | None
    #: How long since the file last changed.
    age: timedelta
    #: Under the allowed roots, not a symlink, no symlink on its path.
    safe: bool


def leftover_reason(facts: FileFacts) -> str | None:
    """Why the file is a leftover, or ``None`` when it must be kept. Pure."""
    if facts.held_pack or not facts.safe:
        return None
    if facts.pack_downloading and facts.fenced:
        return None
    if facts.library or facts.claimed:
        return None
    if facts.age <= MIN_AGE:
        return None
    if not facts.in_client:
        return REASON_CLIENT_GONE
    if facts.client_priority == FILE_OFF:
        return REASON_PRIORITY_ZERO
    return None


# --- Gathering the facts ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pack:
    torrent_id: int
    directory: Path
    held: bool
    downloading: bool
    in_client: bool
    #: Relative path (as the torrent names it) → (wanted, client priority).
    files: Mapping[str, tuple[bool, int | None]]


def _allocated(info: os.stat_result) -> int:
    """Bytes a file holds on the disk, never more than its apparent size."""
    blocks: int | None = getattr(info, "st_blocks", None)
    if blocks is None:  # pragma: no cover - every POSIX host reports it
        return info.st_size
    return min(info.st_size, blocks * 512)


def _relative_name(directory: Path, path: Path) -> str:
    relative = path.relative_to(directory).as_posix()
    if relative.endswith(INCOMPLETE_SUFFIX):
        relative = relative[: -len(INCOMPLETE_SUFFIX)]
    return relative


def _walk_pack(
    pack: _Pack, fence: InFlight, library: frozenset[str], settings: Settings, now: datetime
) -> list[Leftover]:
    """Every leftover in one pack's directory. Blocking: run on a thread."""
    if pack.directory.is_symlink() or not pack.directory.is_dir():
        if pack.directory.is_symlink():
            log.warning(
                "refusing to sweep a pack directory that is a symlink",
                extra={"path": str(pack.directory)},
            )
        return []
    found: list[Leftover] = []
    for current, dirnames, filenames in os.walk(pack.directory, followlinks=False):
        base = Path(current)
        for name in dirnames:
            if (base / name).is_symlink():
                log.warning(
                    "refusing to sweep a symlinked directory", extra={"path": str(base / name)}
                )
        for name in filenames:
            path = base / name
            try:
                info = path.lstat()
            except OSError:  # pragma: no cover - vanished mid-walk
                continue
            if stat.S_ISLNK(info.st_mode):
                log.warning("refusing to sweep a symlink", extra={"path": str(path)})
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            checked = safe_path(path, settings)
            safe = checked is not None and checked == path
            relative = _relative_name(pack.directory, path)
            wanted, priority = pack.files.get(relative, (False, None))
            facts = FileFacts(
                held_pack=pack.held,
                pack_downloading=pack.downloading,
                fenced=fence.holds(str(path)),
                library=str(path) in library,
                claimed=wanted,
                in_client=pack.in_client,
                client_priority=priority,
                age=now - datetime.fromtimestamp(info.st_mtime, UTC),
                safe=safe,
            )
            reason = leftover_reason(facts)
            if reason is not None:
                found.append(
                    Leftover(
                        path=path, size=_allocated(info), torrent_id=pack.torrent_id, reason=reason
                    )
                )
    return found


async def _client_listing(qbit: QbitClient) -> dict[str, str] | None:
    """``hash → state`` for Arc's category, or ``None`` if the client did not answer."""
    try:
        return {info.hash.lower(): info.state for info in await qbit.torrents()}
    except QbitError as exc:  # QbitUnavailable included
        log.warning(
            "qBittorrent did not answer; no pack file is a leftover this time",
            extra={"error": str(exc)},
        )
        return None


async def leftover_files(
    session: AsyncSession,
    settings: Settings,
    qbit: QbitClient | None,
    *,
    now: datetime | None = None,
) -> list[Leftover]:
    """The files under ``downloads/batch/`` the rule calls leftovers. Read-only.

    ``qbit`` is the client to ask; ``None`` (not configured) or a client that
    does not answer gives an empty list — fail closed.
    """
    if qbit is None:
        return []
    moment = now or datetime.now(UTC)
    root = settings.downloads_dir.resolve() / BATCH_DIR

    packs = (
        await session.execute(
            select(Torrent.id, Torrent.info_hash, Torrent.qbit_state).where(
                Torrent.kind == TorrentKind.BATCH
            )
        )
    ).all()
    present = [
        (torrent_id, info_hash, state)
        for torrent_id, info_hash, state in packs
        # A hash is hex; anything else is no directory Arc made.
        if info_hash.isalnum() and (root / info_hash.lower()).is_dir()
    ]
    if not present:
        return []

    listing = await _client_listing(qbit)
    if listing is None:
        return []

    ids = [torrent_id for torrent_id, _, _ in present]
    picking = set(
        (
            await session.scalars(
                select(PackPick.torrent_id).where(
                    PackPick.torrent_id.in_(ids), PackPick.resolved_at.is_(None)
                )
            )
        ).all()
    )
    rows = (await session.scalars(select(TorrentFile).where(TorrentFile.torrent_id.in_(ids)))).all()
    by_torrent: dict[int, list[TorrentFile]] = {}
    for row in rows:
        by_torrent.setdefault(row.torrent_id, []).append(row)
    library = frozenset(
        (
            await session.scalars(
                select(MediaFile.path).where(MediaFile.path.startswith(str(root) + os.sep))
            )
        ).all()
    )
    fence = await in_flight(session, settings)

    built: list[_Pack] = []
    for torrent_id, info_hash, state in present:
        directory = root / info_hash.lower()
        client_state = listing.get(info_hash.lower())
        held = (
            state in HELD_PACK_STATES
            or torrent_id in picking
            or (client_state is not None and client_state in BUSY_CLIENT_STATES)
        )
        own = by_torrent.get(torrent_id, [])
        # Only a pack the client still has can be fetching anything.
        downloading = client_state is not None and any(
            row.wanted
            and row.completed_at is None
            and (row.progress is None or row.progress < COMPLETE_PROGRESS)
            for row in own
        )
        client_files: dict[int, FileInfo] = {}
        if client_state is not None and not held:
            try:
                client_files = {info.index: info for info in await qbit.files(info_hash)}
            except QbitUnavailable as exc:
                log.warning(
                    "qBittorrent stopped answering; no pack file is a leftover this time",
                    extra={"error": str(exc)},
                )
                return []
            except QbitError as exc:
                log.warning(
                    "could not read a pack's files; leaving it alone",
                    extra={"torrent_id": torrent_id, "error": str(exc)},
                )
                held = True
        files: dict[str, tuple[bool, int | None]] = {}
        for row in own:
            parts = PurePosixPath(row.path).parts
            if ".." in parts:
                continue
            listed = client_files.get(row.file_index)
            # The client's own row must name the same file, or its priority is
            # about something else.
            priority = listed.priority if listed is not None and listed.name == row.path else None
            files[PurePosixPath(*parts).as_posix()] = (row.wanted, priority)
        built.append(
            _Pack(
                torrent_id=torrent_id,
                directory=directory,
                held=held,
                downloading=downloading,
                in_client=client_state is not None,
                files=files,
            )
        )

    def walk() -> list[Leftover]:
        found: list[Leftover] = []
        for pack in built:
            found.extend(_walk_pack(pack, fence, library, settings, moment))
        return found

    return await asyncio.to_thread(walk)


# --- Acting on them ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeftoverSweep:
    """What one run of the step did (or, dry, would have done)."""

    candidates: int
    deleted: int
    freed_bytes: int
    dry_run: bool


def _still_leftover(item: Leftover, settings: Settings, now: datetime) -> bool:
    """The two facts that can change between the plan and the unlink."""
    try:
        info = item.path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    if now - datetime.fromtimestamp(info.st_mtime, UTC) <= MIN_AGE:
        return False
    return safe_path(item.path, settings) == item.path


async def _named_now(session: AsyncSession, item: Leftover, batch_root: Path) -> bool:
    """Whether a claim, a library row or a pending pick names the file *now*.

    Asked immediately before the unlink, as the episode deleter asks for a live
    want: the list was drawn up a moment ago, and a reconciliation running
    alongside can claim a file of the pack in between.
    """
    directory = batch_root / item.path.relative_to(batch_root).parts[0]
    relative = _relative_name(directory, item.path)
    claimed = await session.scalar(
        select(TorrentFile.id)
        .where(
            TorrentFile.torrent_id == item.torrent_id,
            TorrentFile.path == relative,
            TorrentFile.wanted.is_(True),
        )
        .limit(1)
    )
    library = await session.scalar(
        select(MediaFile.id).where(MediaFile.path == str(item.path)).limit(1)
    )
    picking = await session.scalar(
        select(PackPick.id)
        .where(PackPick.torrent_id == item.torrent_id, PackPick.resolved_at.is_(None))
        .limit(1)
    )
    state = await session.scalar(select(Torrent.qbit_state).where(Torrent.id == item.torrent_id))
    named = (
        claimed is not None
        or library is not None
        or picking is not None
        or state in HELD_PACK_STATES
    )
    if named:
        log.info("a leftover pack file is wanted again; keeping it", extra={"path": str(item.path)})
    return named


async def sweep_leftovers(
    session: AsyncSession,
    settings: Settings,
    qbit: QbitClient | None,
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> LeftoverSweep:
    """Delete (or, dry, log) every leftover, pruning directories left empty.

    One file's failure never stops the others. Nothing is written to the
    database, so there is nothing to commit; a re-run finds what is left.
    """
    moment = now or datetime.now(UTC)
    found = await leftover_files(session, settings, qbit, now=moment)
    # A pack the client no longer has may lose its directory too once empty.
    gone = {item.torrent_id for item in found if item.reason == REASON_CLIENT_GONE}
    batch_root = settings.downloads_dir.resolve() / BATCH_DIR

    deleted = 0
    freed = 0
    for item in found:
        if dry_run:
            log.info(
                "batch sweep dry run: would delete a leftover pack file",
                extra={
                    "path": str(item.path),
                    "bytes": item.size,
                    "reason": item.reason,
                    "torrent_id": item.torrent_id,
                },
            )
            continue
        try:
            if await _named_now(session, item, batch_root):
                continue
            if not await asyncio.to_thread(_still_leftover, item, settings, datetime.now(UTC)):
                continue
            if not await asyncio.to_thread(remove_file, item.path, settings):
                continue
            pack_dir = batch_root / item.path.relative_to(batch_root).parts[0]
            await asyncio.to_thread(
                prune_empty_dirs,
                item.path.parent,
                pack_dir,
                settings,
                include_stop=item.torrent_id in gone,
            )
        except Exception as exc:  # one file must never stop the rest
            log.warning(
                "could not delete a leftover pack file",
                extra={"path": str(item.path), "error": repr(exc)},
            )
            continue
        deleted += 1
        freed += item.size
        log.info(
            "deleted a leftover pack file",
            extra={
                "path": str(item.path),
                "bytes": item.size,
                "reason": item.reason,
                "torrent_id": item.torrent_id,
            },
        )
    return LeftoverSweep(
        candidates=len(found),
        deleted=deleted,
        freed_bytes=sum(item.size for item in found) if dry_run else freed,
        dry_run=dry_run,
    )


__all__ = [
    "BUSY_CLIENT_STATES",
    "HELD_PACK_STATES",
    "MIN_AGE",
    "REASON_CLIENT_GONE",
    "REASON_PRIORITY_ZERO",
    "FileFacts",
    "Leftover",
    "LeftoverSweep",
    "leftover_files",
    "leftover_reason",
    "sweep_leftovers",
]
