"""The job queue: enqueue, claim, run, retry, and the handler registry.

Arc runs background work as rows in the ``jobs`` table, claimed by the worker
with ``SELECT … FOR UPDATE SKIP LOCKED`` (architecture.md §1, §2). There is no
broker: the API enqueues by inserting a row, the worker polls.

Layout:

* ``registry`` — ``@register("type")`` and the :class:`JobContext` handlers get
* ``queue`` — :func:`enqueue`, with optional dedupe
* ``runner`` — :func:`claim_one`, :func:`run_job`, backoff, :func:`requeue_stale`
* ``loop`` — :func:`run_worker_loop`, the worker's claim loop
* ``builtin`` — ``noop`` and ``fail_once``

Importing this package registers the built-in handlers; feature packages
register theirs at import time the same way, so anything that dispatches jobs
must import them (the worker does).
"""

from __future__ import annotations

from arc.services.jobs import builtin as builtin  # noqa: F401  (registers handlers)
from arc.services.jobs.heartbeat import (
    HEARTBEAT_STALE_AFTER,
    check_heartbeat,
    heartbeat_at,
    heartbeat_path,
    touch_heartbeat,
)
from arc.services.jobs.loop import run_worker_loop
from arc.services.jobs.queue import (
    JobTransition,
    cancel_job,
    enqueue,
    find_active,
    retry_job,
)
from arc.services.jobs.registry import (
    JobContext,
    JobHandler,
    UnknownJobType,
    get_handler,
    register,
    registered_types,
)
from arc.services.jobs.runner import (
    backoff,
    claim_one,
    claim_statement,
    requeue_stale,
    run_job,
)

__all__ = [
    "HEARTBEAT_STALE_AFTER",
    "JobContext",
    "JobHandler",
    "JobTransition",
    "UnknownJobType",
    "backoff",
    "cancel_job",
    "check_heartbeat",
    "claim_one",
    "claim_statement",
    "enqueue",
    "find_active",
    "get_handler",
    "heartbeat_at",
    "heartbeat_path",
    "register",
    "registered_types",
    "requeue_stale",
    "retry_job",
    "run_job",
    "run_worker_loop",
    "touch_heartbeat",
]
