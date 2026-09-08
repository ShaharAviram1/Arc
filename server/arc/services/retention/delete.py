"""Deleting one episode's files, rows and torrent (FR-T3).

:mod:`arc.services.retention.sweep` decides *whether*; this does it. Six
things go, in an order chosen so that a failure half-way leaves the least
awkward state behind:

1. **qBittorrent first**, with ``deleteFiles``. The client is the only party
   here that is not Arc: if it cannot be reached, nothing else should have
   happened yet, because a ``torrents`` row deleted while the client is down
   is a torrent that seeds for ever with nothing left to say why. The client
   only ever deletes hashes in Arc's own category and skips the rest with a
   warning (:meth:`~arc.services.acquisition.qbit.QbitClient.delete`), which
   is what makes "the torrent is not in the client any more" a no-op rather
   than a failure.
2. the rendition directory, 3. the source directory, 4. any loose source file
   (a manual drop lives in ``DATA_DIR/manual`` and has no directory of its
   own), 5. the ``renditions``, ``media_files`` and ``torrents`` rows, and the
   tombstoned ``wants`` rows that were the reason this episode could go
   (:func:`_delete_leftover_wants` — with one exception, which is the whole of
   its docstring), and
6. the state change back to ``not_wanted`` (spec §6's ``ready → (retention) →
   not_wanted``), which is what lets a later want re-acquire the episode
   through the ordinary path.

**Everything is tolerant of already being gone.** A sweep that crashed after
deleting the directories runs again an hour later and finds them missing;
that must be an ordinary run, not an error. The paths are re-checked against
``DATA_DIR``'s roots immediately before the deletion even though the planner
already checked them, because this is the function that actually calls
``rmtree`` and a check that lives anywhere else is a check somebody can route
around.

**Dry run** (``RETENTION_DRY_RUN``) stops after the plan: the log line says
what would have gone, and nothing — not a file, not a row, not the state — is
touched. It is the switch to turn on for the first night on a new deployment.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete as sql_delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, MediaFile, Rendition, Torrent, Want
from arc.services.acquisition.qbit import QbitClient
from arc.services.acquisition.states import transition
from arc.services.acquisition.wants import STALE_DROP_REASON
from arc.services.retention.sweep import (
    PROTECTED_STATES,
    RETENTION_REASON,
    Targets,
    safe_path,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Removed:
    """What one deletion actually did. Logged, and returned for the tests."""

    episode_id: int
    dry_run: bool = False
    #: False when the episode was in a state retention must not touch.
    acted: bool = True
    rendition_dir: str | None = None
    source_dir: str | None = None
    loose_files: int = 0
    media_files: int = 0
    torrents: int = 0
    #: Tombstoned ``wants`` rows cleared with the files. Never the stale ones
    #: (:func:`_delete_leftover_wants`), and never a live want.
    wants: int = 0
    hashes: tuple[str, ...] = ()
    freed_bytes: int = 0
    state_changed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "dry_run": self.dry_run,
            "rendition_dir": self.rendition_dir,
            "source_dir": self.source_dir,
            "loose_files": self.loose_files,
            "media_files": self.media_files,
            "torrents": self.torrents,
            "wants": self.wants,
            "hashes": list(self.hashes),
            "freed_bytes": self.freed_bytes,
            "state_changed": self.state_changed,
        }


def _remove_tree(path: Path, settings: Settings) -> bool:
    """Delete a directory, re-checking that it is one Arc may delete."""
    checked = safe_path(path, settings)
    if checked is None:
        return False
    if not checked.exists():
        return False
    try:
        shutil.rmtree(checked)
    except OSError as exc:  # pragma: no cover - a permission problem on the host
        log.warning("could not remove a directory", extra={"path": str(checked), "error": str(exc)})
        return False
    return True


def _remove_file(path: Path, settings: Settings) -> bool:
    """Delete one file, re-checking that it is one Arc may delete."""
    checked = safe_path(path, settings)
    if checked is None:
        return False
    try:
        checked.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover - a permission problem on the host
        log.warning("could not remove a file", extra={"path": str(checked), "error": str(exc)})
        return False
    return True


async def _delete_leftover_wants(session: AsyncSession, episode_id: int) -> int:
    """Drop the tombstoned ``wants`` rows of an episode that has just been swept.

    The rows were the anchor: the sweep measured G from the last
    ``dropped_at``, and once the files are gone they are a tombstone for
    something that no longer exists — the episode is ``not_wanted`` and holds
    nothing. Leaving them is not harmless either, because
    :func:`~arc.services.acquisition.wants._reconcile` reads a dropped row as
    "this user already had their answer" and would go on refusing to re-fetch
    an episode nobody has any record of.

    **Except the stale ones** (FR-T2). That drop *is* the record — "you had
    this ready for D days and did not watch it" — and it is the only thing
    standing between the user and a download of the same episode on the next
    quarter-hour's reconciliation: the show is still ``watching``, the episode
    is still in the window, and the user's progress has not moved, so a
    reconciler that found no row would make a live want, fetch the episode
    again, drop it again D days later and delete it again G days after that,
    for ever. Those rows go when the user comes back to the show, and not
    before.

    Live wants are never touched. The manual delete (FR-T4) is allowed to run
    on an episode somebody wants, and re-acquiring it afterwards is the
    documented behaviour of that button.
    """
    result = await session.execute(
        sql_delete(Want).where(
            Want.episode_id == episode_id,
            Want.dropped_at.is_not(None),
            Want.drop_reason.is_distinct_from(STALE_DROP_REASON),
        )
    )
    return cast("CursorResult[Any]", result).rowcount or 0


async def _delete_rows(session: AsyncSession, targets: Targets) -> tuple[int, int]:
    """The ``renditions``, ``media_files`` and ``torrents`` rows (FR-T3).

    The counts are what the database actually removed, not what was asked for:
    a re-run of a sweep that already deleted these rows should say it deleted
    nothing rather than claim the work twice.
    """
    if targets.rendition_id is not None:
        await session.execute(sql_delete(Rendition).where(Rendition.id == targets.rendition_id))
    files = 0
    if targets.media_file_ids:
        result = await session.execute(
            sql_delete(MediaFile).where(MediaFile.id.in_(targets.media_file_ids))
        )
        files = cast("CursorResult[Any]", result).rowcount or 0
    torrents = 0
    if targets.torrent_ids:
        result = await session.execute(
            sql_delete(Torrent).where(Torrent.id.in_(targets.torrent_ids))
        )
        torrents = cast("CursorResult[Any]", result).rowcount or 0
    return files, torrents


async def delete_episode_files(
    session: AsyncSession,
    settings: Settings,
    episode: Episode,
    targets: Targets,
    *,
    reason: str = RETENTION_REASON,
    dry_run: bool = False,
) -> Removed:
    """Remove everything ``targets`` names and reset the episode (FR-T3).

    Flushes but does not commit: the caller owns the transaction, so the rows,
    the state change and the job's own bookkeeping land together. The *files*
    are of course gone either way, which is why the row deletions come last
    and why a re-run of the whole thing is harmless.
    """
    if episode.state in PROTECTED_STATES:
        log.warning(
            "refusing to delete the files of an episode that is still in flight",
            extra={"episode_id": episode.id, "state": episode.state.value},
        )
        return Removed(episode_id=episode.id, acted=False)

    if dry_run:
        removed = Removed(
            episode_id=episode.id,
            dry_run=True,
            rendition_dir=str(targets.rendition_dir) if targets.rendition_dir else None,
            source_dir=str(targets.source_dir) if targets.source_dir else None,
            loose_files=len(targets.loose_files),
            media_files=len(targets.media_file_ids),
            torrents=len(targets.torrent_ids),
            hashes=targets.torrent_hashes,
            freed_bytes=targets.bytes,
        )
        log.info("retention dry run: would delete", extra={**removed.as_dict(), "reason": reason})
        return removed

    if targets.torrent_hashes:
        async with QbitClient.from_settings(settings) as qbit:
            await qbit.delete(list(targets.torrent_hashes), delete_files=True)

    rendition_dir = (
        str(targets.rendition_dir)
        if targets.rendition_dir is not None and _remove_tree(targets.rendition_dir, settings)
        else None
    )
    source_dir = (
        str(targets.source_dir)
        if targets.source_dir is not None and _remove_tree(targets.source_dir, settings)
        else None
    )
    loose = sum(1 for path in targets.loose_files if _remove_file(path, settings))

    files, torrents = await _delete_rows(session, targets)
    wants = await _delete_leftover_wants(session, episode.id)
    state_changed = transition(episode, EpisodeState.NOT_WANTED, reason=reason)
    await session.flush()

    removed = Removed(
        episode_id=episode.id,
        rendition_dir=rendition_dir,
        source_dir=source_dir,
        loose_files=loose,
        media_files=files,
        torrents=torrents,
        wants=wants,
        hashes=targets.torrent_hashes,
        freed_bytes=targets.bytes,
        state_changed=state_changed,
    )
    log.info("retention deleted an episode", extra={**removed.as_dict(), "reason": reason})
    return removed


__all__ = ["Removed", "delete_episode_files"]
