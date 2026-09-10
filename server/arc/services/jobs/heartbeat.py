"""The worker's liveness file: written by the worker, read by everyone else.

A file under ``DATA_DIR`` whose modification time is the last moment the
worker's scheduler ran. ``python -m arc.worker --check`` is the container
healthcheck that reads it (architecture.md §8), and M14's ``GET
/api/jobs/summary`` reads the same file to tell an admin whether the queue has
anything running it (FR-D3).

It lives here rather than in :mod:`arc.worker` because of that second reader.
Importing ``arc.worker`` from the API would drag in APScheduler and every job
handler package — registering handlers in a process that never runs them — for
the sake of one ``stat``. ``arc.worker`` imports these names back and
re-exports them, so ``--check`` and its tests are unchanged.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from arc.config import Settings

log = logging.getLogger(__name__)

#: How often the worker's scheduler rewrites the file.
HEARTBEAT_SECONDS: Final[int] = 30

#: File under ``DATA_DIR`` whose modification time is the worker's liveness
#: signal. Written on start-up and re-written by every heartbeat tick.
HEARTBEAT_FILENAME: Final[str] = "worker.heartbeat"

#: How stale that file may be before the worker is called dead. Three beats:
#: one missed tick is a busy event loop, three is a process that has stopped
#: running its scheduler. Deliberately generous, because the cost of a false
#: negative is Docker killing a healthy worker mid-transcode.
HEARTBEAT_STALE_AFTER: Final[int] = HEARTBEAT_SECONDS * 3


def heartbeat_path(settings: Settings) -> Path:
    """Where the liveness file lives.

    Under ``DATA_DIR`` rather than ``/tmp`` because that is the one directory
    the deployment already guarantees is writable by the worker's user, and
    because ``docker compose exec`` runs the check inside the same container
    and therefore sees the same path.
    """
    return settings.data_dir / HEARTBEAT_FILENAME


def touch_heartbeat(settings: Settings) -> None:
    """Record that the worker is alive, now. Never raises.

    A failure here must not kill the worker: it would turn "the log directory
    is full" into "no episodes are transcoded". The healthcheck will notice
    soon enough, which is exactly its job.
    """
    path = heartbeat_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - depends on the filesystem
        log.warning(
            "could not write the heartbeat file", extra={"path": str(path), "error": str(exc)}
        )


def heartbeat_at(settings: Settings) -> datetime | None:
    """When the worker last beat, or ``None`` if it never has.

    The file's mtime rather than its contents: the contents are a convenience
    for whoever opens it by hand, and a half-written file (the worker died
    between ``open`` and ``write``) must not be able to make this raise.
    """
    try:
        stamp = heartbeat_path(settings).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, UTC)


def check_heartbeat(settings: Settings, *, now: float | None = None) -> bool:
    """Whether the heartbeat file is fresh enough to call the worker healthy.

    ``False`` for a missing file too: a worker that has not started has not
    written one, and "no evidence of life" is the same answer as "last seen an
    hour ago" as far as a container healthcheck is concerned.
    """
    path = heartbeat_path(settings)
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age <= HEARTBEAT_STALE_AFTER


__all__ = [
    "HEARTBEAT_FILENAME",
    "HEARTBEAT_SECONDS",
    "HEARTBEAT_STALE_AFTER",
    "check_heartbeat",
    "heartbeat_at",
    "heartbeat_path",
    "touch_heartbeat",
]
