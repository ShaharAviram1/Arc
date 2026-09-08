"""Retention: deleting what nobody is going to watch again (spec §4.9).

* ``rules`` — G and D, read out of ``settings`` (FR-T5). Imports nothing from
  the rest of ``services``, because ``compute_wants`` reads D from it.
* ``sweep`` — which episodes may go, why, and how many bytes that is
  (FR-T1). Read-only, so the admin preview endpoint can call it directly.
* ``delete`` — removing one episode's files, rows and torrent (FR-T3).
* ``names`` — the two job types, importable without the handlers.
* ``jobs`` — the handlers themselves. Importing it registers them, so only
  the worker does.

FR-T2 (the stale-want drop) is **not** here: it is a rule about the wants
table, it has to run on every reconciliation rather than on the hour, and it
lives in :func:`arc.services.acquisition.wants.compute_wants` where the rest
of that table's rules are.
"""

from __future__ import annotations

from arc.services.retention.names import (
    DELETE_EPISODE_FILES,
    RETENTION_PRIORITY,
    RETENTION_SWEEP,
    delete_files_dedupe_key,
    enqueue_delete_files,
    enqueue_retention_sweep,
)
from arc.services.retention.rules import (
    GRACE_KEY,
    UNWATCHED_KEY,
    grace_days,
    grace_period,
    unwatched_days,
    unwatched_period,
)

__all__ = [
    "DELETE_EPISODE_FILES",
    "GRACE_KEY",
    "RETENTION_PRIORITY",
    "RETENTION_SWEEP",
    "UNWATCHED_KEY",
    "delete_files_dedupe_key",
    "enqueue_delete_files",
    "enqueue_retention_sweep",
    "grace_days",
    "grace_period",
    "unwatched_days",
    "unwatched_period",
]
