"""Leftover pack files: what per-episode retention cannot see (FR-T1, M21 T8).

A want withdrawn while its file in a season pack was downloading leaves the
bytes that had arrived inside ``downloads/batch/<hash>/`` with no
``media_files`` row naming them and no episode to charge them to. The
per-episode sweep (:mod:`arc.services.retention.sweep`) works from rows, so it
never sees them, and the pack itself may never reach ``batch.disposition``'s
DELETE while somebody keeps the show on a list. This module finds such files
and, as one step of the hourly ``retention_sweep``, removes them.

**Only a finished or a gone pack is swept.** A pack the client still has and
that still owes Arc a wanted file is *downloading*, and its whole directory is
kept; so is a **held** pack (awaiting a pick, being cancelled, being checked by
the client) and one whose presence in the client cannot be settled.

**The rule is a conjunction, and every half of it says "keep" by default**
(:func:`leftover_reason`). A file under the batch root is a candidate only
when *all* of these hold:

* its directory is a pack Arc knows (a ``torrents`` row of kind ``batch``) —
  an unknown ``batch/<dir>`` is out of scope (the library fence already keeps
  it out of the library);
* the pack is neither held nor downloading (above), and
  :meth:`~arc.services.library.ingest.InFlight.holds` does not fence it (for a
  pack that is not downloading this is the fence's "no pack row" half);
* no ``media_files.path`` names it, and no ``torrent_files`` row with
  ``wanted = true`` claims it;
* qBittorrent reports **priority 0** for its index, or the pack is **gone from
  the client** — which takes three agreeing answers: a non-empty listing of
  Arc's category without it, ``has(hash)`` (any category) saying no, and the
  row's own ``qbit_state`` already one the poll writes for a torrent the
  client no longer has (:data:`GONE_STATES`);
* it has not changed for an hour (:data:`MIN_AGE`), and
* it is under :func:`~arc.services.retention.sweep.allowed_roots` with no
  symlink on its path (:func:`~arc.services.retention.sweep.safe_path`, plus a
  resolve that must change nothing).

**qBittorrent unreachable, or answering an empty listing, means nothing is a
candidate** (fail closed): the priority half cannot be answered, and "the
torrent is gone" must never be inferred from a client that did not answer.

The deletion re-asks the database and the client about each pack right before
its first unlink, goes through :mod:`arc.services.retention.delete`'s own file
primitive (the confinement re-check right before ``unlink``), prunes the
directories it leaves empty, and asks the client to **recheck** each pack it
still holds that lost a file — otherwise libtorrent goes on believing it has
the deleted pieces, and a later claim on that file would come out with holes.
While ``batch_sweep_dry_run`` is true (seeded on) — or ``RETENTION_DRY_RUN``
is — each would-be deletion is logged at INFO with its path, bytes and reason,
and nothing is removed or rechecked.

File names are matched **byte for byte** (after the ``.!qB`` suffix is
stripped) against the names the torrent gives them, with no Unicode
normalisation: libtorrent writes the names it was given, and a name the
filesystem stored differently (an NFD-normalising volume) simply matches no row
and is kept while the pack is in the client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from collections.abc import Mapping, Sequence
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
    CHECKING_STATES,
    COMPLETE_PROGRESS,
    FILE_OFF,
    QBIT_AWAITING_PICK,
    QBIT_CANCELLED,
    QBIT_MISSING,
    QBIT_STALLED,
    QBIT_UNREADABLE,
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

#: The ``qbit_state`` values the poll (or Arc's own deletion) writes for a
#: torrent the client no longer has. A pack counts as gone only when its row
#: already says so: one listing that lacks it is never enough on its own.
GONE_STATES: Final[frozenset[str]] = frozenset({QBIT_MISSING, QBIT_STALLED, QBIT_UNREADABLE})

#: A v1 (SHA-1) or v2 (SHA-256) info hash, lower-cased. Anything else is no
#: directory Arc made.
_HASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

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
    #: cancelled, the client checking its files, or its presence unsettled.
    held_pack: bool
    #: The client still has the pack and it still owes Arc a wanted file.
    pack_downloading: bool
    #: :meth:`InFlight.holds` for the file.
    fenced: bool
    #: A ``media_files.path`` names the file.
    library: bool
    #: A ``torrent_files`` row with ``wanted = true`` claims the file.
    claimed: bool
    #: The pack is in the client; false only once it is confirmed gone.
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
    if facts.held_pack or facts.pack_downloading or facts.fenced or not facts.safe:
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


def _downloading(rows: Sequence[TorrentFile]) -> bool:
    """Whether any wanted file of the pack is still incomplete."""
    return any(
        row.wanted
        and row.completed_at is None
        and (row.progress is None or row.progress < COMPLETE_PROGRESS)
        for row in rows
    )


def _priorities(
    rows: Sequence[TorrentFile], client_files: Mapping[int, FileInfo]
) -> dict[str, tuple[bool, int | None]]:
    """``relative path → (wanted, the client's priority)`` for one pack."""
    files: dict[str, tuple[bool, int | None]] = {}
    for row in rows:
        parts = PurePosixPath(row.path).parts
        if ".." in parts or not parts:
            continue
        listed = client_files.get(row.file_index)
        # The client's own row must name the same file (byte for byte), or its
        # priority is about something else.
        priority = listed.priority if listed is not None and listed.name == row.path else None
        files[PurePosixPath(*parts).as_posix()] = (row.wanted, priority)
    return files


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
                fenced=fence.holds(str(path))
                if pack.downloading
                else (fence.unknown_pack(str(path)) is not None),
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


async def client_listing(qbit: QbitClient) -> dict[str, str] | None:
    """``hash → state`` for Arc's category, or ``None`` when that is unknown.

    ``None`` when the client did not answer, and also when it answered an
    **empty** listing (the rule T7's pick reconciliation follows): a client
    that has just restarted, or a category emptied by hand, is no evidence
    that any pack is gone.
    """
    try:
        listing = {info.hash.lower(): info.state for info in await qbit.torrents()}
    except QbitError as exc:  # QbitUnavailable included
        log.warning(
            "qBittorrent did not answer; no pack file is a leftover this time",
            extra={"error": str(exc)},
        )
        return None
    if not listing:
        log.warning("qBittorrent listed no torrents; no pack file is a leftover this time")
        return None
    return listing


async def _confirmed_gone(qbit: QbitClient, info_hash: str, state: str | None) -> bool | None:
    """Whether a pack missing from the listing is really gone from the client.

    ``True`` only when the row already says so (:data:`GONE_STATES`) **and**
    ``has`` (any category) says no; ``False`` when the client holds it
    somewhere; ``None`` when that cannot be settled (the row disagrees, or
    ``has`` failed) — the caller then holds the pack whole.
    """
    try:
        held_anywhere = await qbit.has(info_hash)
    except QbitError as exc:
        log.warning(
            "could not ask qBittorrent whether it holds a pack; leaving it alone",
            extra={"hash": info_hash, "error": str(exc)},
        )
        return None
    if held_anywhere:
        return False
    if state not in GONE_STATES:
        return None
    return True


async def leftover_files(
    session: AsyncSession,
    settings: Settings,
    qbit: QbitClient | None,
    *,
    now: datetime | None = None,
    listing: Mapping[str, str] | None = None,
) -> list[Leftover]:
    """The files under ``downloads/batch/`` the rule calls leftovers. Read-only.

    ``qbit`` is the client to ask; ``None`` (not configured) or a client that
    does not answer gives an empty list — fail closed. ``listing`` is
    :func:`client_listing`'s answer when the caller already has it.
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
        (torrent_id, info_hash.lower(), state)
        for torrent_id, info_hash, state in packs
        if _HASH.fullmatch(info_hash.lower()) and (root / info_hash.lower()).is_dir()
    ]
    if not present:
        return []

    if listing is None:
        listing = await client_listing(qbit)
    if not listing:
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
                select(MediaFile.path).where(
                    MediaFile.path.startswith(str(root) + os.sep, autoescape=True)
                )
            )
        ).all()
    )
    fence = await in_flight(session, settings)

    built: list[_Pack] = []
    for torrent_id, info_hash, state in present:
        client_state = listing.get(info_hash)
        own = by_torrent.get(torrent_id, [])
        held = state in HELD_PACK_STATES or torrent_id in picking
        in_client = client_state is not None
        if client_state is None and not held:
            gone = await _confirmed_gone(qbit, info_hash, state)
            # Held anywhere, or unsettled: the whole directory stays.
            held = gone is not True
            in_client = gone is not True
        if client_state is not None and client_state in CHECKING_STATES:
            held = True
        downloading = in_client and _downloading(own)
        client_files: dict[int, FileInfo] = {}
        if client_state is not None and not held and not downloading:
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
        built.append(
            _Pack(
                torrent_id=torrent_id,
                directory=root / info_hash,
                held=held,
                downloading=downloading,
                in_client=in_client,
                files=_priorities(own, client_files),
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
    #: Hashes of the packs the client was asked to recheck.
    rechecked: tuple[str, ...] = ()


def _still_leftover(item: Leftover, settings: Settings, now: datetime) -> bool:
    """The filesystem facts that can change between the plan and the unlink."""
    try:
        info = item.path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    if now - datetime.fromtimestamp(info.st_mtime, UTC) <= MIN_AGE:
        return False
    return safe_path(item.path, settings) == item.path


async def _named_now(session: AsyncSession, item: Leftover, directory: Path) -> bool:
    """Whether a claim, a library row or a pending pick names the file *now*.

    Asked immediately before the unlink, as the episode deleter asks for a live
    want: the list was drawn up a moment ago, and a reconciliation running
    alongside can claim a file of the pack in between.
    """
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


async def _client_agrees(
    session: AsyncSession,
    qbit: QbitClient,
    info_hash: str,
    torrent_id: int,
    items: Sequence[Leftover],
    directory: Path,
) -> list[Leftover]:
    """One pack's items whose client facts still hold, asked right before the
    first unlink. Any change, or any failure to ask, keeps the whole pack."""
    gone = all(item.reason == REASON_CLIENT_GONE for item in items)
    try:
        info = await qbit.info(info_hash)
        if gone:
            # Still nowhere in the client, in any category.
            return list(items) if info is None else []
        if info is None or info.state in CHECKING_STATES:
            return []
        listed = {row.index: row for row in await qbit.files(info_hash)}
    except QbitError as exc:
        log.warning(
            "could not re-ask qBittorrent about a pack; keeping its files",
            extra={"hash": info_hash, "error": str(exc)},
        )
        return []
    rows = (
        await session.scalars(
            select(TorrentFile)
            .where(TorrentFile.torrent_id == torrent_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    if _downloading(rows):
        return []
    files = _priorities(rows, listed)
    kept = [
        item
        for item in items
        if files.get(_relative_name(directory, item.path), (True, None)) == (False, FILE_OFF)
    ]
    if len(kept) != len(items):
        log.info(
            "qBittorrent changed its mind about a pack's files; keeping those",
            extra={"hash": info_hash, "kept": len(items) - len(kept)},
        )
    return kept


async def sweep_leftovers(
    session: AsyncSession,
    settings: Settings,
    qbit: QbitClient | None,
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> LeftoverSweep:
    """Delete (or, dry, log) every leftover, pruning directories left empty.

    Each pack is re-asked about (database and client) right before its first
    unlink; a pack the client still holds that lost a file is rechecked once
    afterwards. One file's failure never stops the others. Nothing is written
    to the database, so there is nothing to commit; a re-run finds what is left.
    """
    moment = now or datetime.now(UTC)
    found = await leftover_files(session, settings, qbit, now=moment)
    batch_root = settings.downloads_dir.resolve() / BATCH_DIR

    if dry_run or qbit is None:
        for item in found:
            log.info(
                "batch sweep dry run: would delete a leftover pack file",
                extra={
                    "path": str(item.path),
                    "bytes": item.size,
                    "reason": item.reason,
                    "torrent_id": item.torrent_id,
                },
            )
        return LeftoverSweep(
            candidates=len(found),
            deleted=0,
            freed_bytes=sum(item.size for item in found) if dry_run else 0,
            dry_run=dry_run,
        )

    by_pack: dict[int, list[Leftover]] = {}
    for item in found:
        by_pack.setdefault(item.torrent_id, []).append(item)

    deleted = 0
    freed = 0
    rechecks: list[str] = []
    for torrent_id, items in by_pack.items():
        info_hash = items[0].path.relative_to(batch_root).parts[0]
        directory = batch_root / info_hash
        gone = all(item.reason == REASON_CLIENT_GONE for item in items)
        survivors = await _client_agrees(session, qbit, info_hash, torrent_id, items, directory)
        lost = 0
        for item in survivors:
            try:
                if await _named_now(session, item, directory):
                    continue
                if not await asyncio.to_thread(_still_leftover, item, settings, datetime.now(UTC)):
                    continue
                if not await asyncio.to_thread(remove_file, item.path, settings):
                    continue
                lost += 1
                await asyncio.to_thread(
                    prune_empty_dirs, item.path.parent, directory, settings, include_stop=gone
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
        if lost and not gone:
            rechecks.append(info_hash)

    # The client still believes it has the deleted pieces: once per pack per
    # run, so a later claim on one of those files is fetched again rather than
    # trusted. Never for a gone pack (nothing to ask) or in a dry run.
    for info_hash in rechecks:
        try:
            await qbit.recheck([info_hash])
        except QbitError as exc:
            log.warning(
                "could not ask qBittorrent to recheck a swept pack",
                extra={"hash": info_hash, "error": str(exc)},
            )
            continue
        log.info("asked qBittorrent to recheck a swept pack", extra={"hash": info_hash})
    return LeftoverSweep(
        candidates=len(found),
        deleted=deleted,
        freed_bytes=freed,
        dry_run=False,
        rechecked=tuple(rechecks),
    )


__all__ = [
    "GONE_STATES",
    "HELD_PACK_STATES",
    "MIN_AGE",
    "REASON_CLIENT_GONE",
    "REASON_PRIORITY_ZERO",
    "FileFacts",
    "Leftover",
    "LeftoverSweep",
    "client_listing",
    "leftover_files",
    "leftover_reason",
    "sweep_leftovers",
]
