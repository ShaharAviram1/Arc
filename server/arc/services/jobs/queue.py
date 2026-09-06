"""Putting work on the queue.

Enqueuing is a plain insert into ``jobs``; the worker finds the row on its
next poll. :func:`enqueue` *flushes* but does not commit, so that a job can
be enqueued in the same transaction as the change that caused it — a want is
written and its ``search_release`` job appears together, or neither does.
The caller commits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DEFAULT_MAX_ATTEMPTS, DEFAULT_PRIORITY, Job, JobStatus

#: Statuses that still count as "this work is already queued" for dedupe.
ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)

#: Where the dedupe key is kept. It lives inside the payload rather than in a
#: column of its own: it is only ever read by this function, and a column
#: would mean a migration and an index for something a JSONB expression index
#: can do later if the queue ever gets big enough to need one.
DEDUPE_FIELD = "dedupe_key"


async def find_active(
    session: AsyncSession,
    type: str,
    dedupe_key: str,
    *,
    exclude_job_id: int | None = None,
) -> Job | None:
    """The pending/running job for ``type`` carrying ``dedupe_key``, if any.

    ``exclude_job_id`` is for a handler that requeues **itself** — the retry
    schedule in :mod:`arc.services.acquisition.jobs` is the case. Such a job is
    ``running`` under the very key it is about to queue under, so without this
    the dedupe finds the caller and the retry is silently dropped.
    """
    key_expr = cast(ColumnElement[str], Job.payload[DEDUPE_FIELD].astext)
    statement = (
        select(Job)
        .where(
            Job.type == type,
            Job.status.in_(ACTIVE_STATUSES),
            key_expr == dedupe_key,
        )
        .order_by(Job.id)
        .limit(1)
    )
    if exclude_job_id is not None:
        statement = statement.where(Job.id != exclude_job_id)
    existing: Job | None = await session.scalar(statement)
    return existing


async def enqueue(
    session: AsyncSession,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = DEFAULT_PRIORITY,
    run_after: datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    dedupe_key: str | None = None,
    exclude_job_id: int | None = None,
) -> Job:
    """Insert a job row and return it (flushed, not committed).

    ``priority`` is claimed lowest-first; ``run_after`` delays a job (leave it
    ``None`` for "as soon as a worker is free"). A *naive* ``run_after`` is
    read as UTC, not as the host's local time: Arc works in UTC throughout,
    and silently shifting a delay by the server's offset is the kind of bug
    that only appears once the host is moved.

    ``dedupe_key`` makes the call idempotent: if a job of the same type with
    the same key is already pending or running, that job is returned and
    nothing is inserted. Use it for work that is pointless to queue twice —
    "recompute wants for user 3", "poll qBittorrent". On such a hit the
    existing row is returned **as it stands**: this call's ``priority``,
    ``run_after`` and ``max_attempts`` are ignored, not merged in. It is a
    best-effort check, not a unique constraint: two workers racing on the same
    key can still produce two rows, which is why handlers are idempotent
    anyway.
    """
    body: dict[str, Any] = dict(payload or {})
    if dedupe_key is not None:
        existing = await find_active(session, type, dedupe_key, exclude_job_id=exclude_job_id)
        if existing is not None:
            return existing
        body[DEDUPE_FIELD] = dedupe_key

    if run_after is not None and run_after.tzinfo is None:
        run_after = run_after.replace(tzinfo=UTC)

    job = Job(
        type=type,
        payload=body,
        status=JobStatus.PENDING,
        priority=priority,
        attempts=0,
        max_attempts=max_attempts,
        # Set explicitly rather than leaning on the column's server default,
        # so the returned object is complete without a second round trip.
        run_after=run_after or datetime.now(UTC),
    )
    session.add(job)
    await session.flush()
    return job


__all__ = ["ACTIVE_STATUSES", "DEDUPE_FIELD", "enqueue", "find_active"]
