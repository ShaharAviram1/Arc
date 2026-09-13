"""How much room is left on the data volume (FR-T4, FR-T6).

One question, two callers that must not answer it differently: the admin
panel's disk figures (``GET /api/retention/disk``) and acquisition's storage
guard (:func:`arc.services.acquisition.rules.is_storage_held`). It used to live
as a private helper inside :mod:`arc.api.retention`, which meant the only way
for a *service* to know how full the disk was would have been a second copy of
the walk-up rule below.

A deliberate leaf: this module imports nothing from ``arc`` at all. Acquisition
reads it, retention reads it, and neither can drag the other into a cycle.

The walk-up is the whole subtlety. ``DATA_DIR`` may not exist yet on a fresh
install and reading a figure is the wrong moment to create it, so the nearest
parent that *does* exist is measured instead — which is the filesystem the
directory will be created on, and therefore the number the caller is actually
asking for. Only when even the root cannot be read is there no answer, and that
is reported as ``None`` rather than as zeros: "no free space" and "I could not
look" are different facts, and the storage guard must never hold acquisition on
the second one (:func:`arc.services.acquisition.rules.is_storage_held`).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DiskUsage:
    """``shutil.disk_usage`` for one path, in bytes."""

    total: int
    used: int
    free: int


def disk_usage(path: Path) -> DiskUsage | None:
    """Filesystem figures for ``path``, or for the nearest parent that exists.

    ``None`` when nothing in the chain up to the root could be read at all —
    which is a failure to measure, not a measurement of zero.

    Synchronous and blocking (a ``statvfs``), so callers inside the event loop
    hand it to :func:`asyncio.to_thread`.
    """
    for candidate in (path, *path.parents):
        try:
            usage = shutil.disk_usage(candidate)
        except OSError:
            continue
        return DiskUsage(total=usage.total, used=usage.used, free=usage.free)
    return None


__all__ = ["DiskUsage", "disk_usage"]
