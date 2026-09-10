"""Putting work on the queue, and the two buttons above it.

Enqueuing is a plain insert into ``jobs``; the worker finds the row on its
next poll. :func:`enqueue` *flushes* but does not commit, so that a job can
be enqueued in the same transaction as the change that caused it — a want is
written and its ``search_release`` job appears together, or neither does.
The caller commits.

:func:`retry_job` and :func:`cancel_job` are the admin queue view's two
controls (FR-D3). Both are state transitions on a row rather than anything the
worker has to be told about, which is why they live here and not in the runner:
the worker's next poll picks up whatever the table now says.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DEFAULT_MAX_ATTEMPTS, DEFAULT_PRIORITY, Job, JobStatus

#: Statuses that still count as "this work is already queued" for dedupe.
ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)

#: Where the dedupe key is kept. It lives inside the payload rather than in a
#: column of its own: it is only ever read by this function, and a column
#: would mean a migration and an index for something a JSONB expression index
#: can do later if the queue ever gets big enough to need one.
DEDUPE_FIELD = "dedupe_key"


#: Statuses a job may be sent back to the queue from. ``cancelled`` is in the
#: list for the same reason ``failed`` is: both mean the row has stopped and
#: nothing is holding it, so "actually, run it" is a decision an admin is
#: allowed to change their mind about.
RETRYABLE: Final[tuple[JobStatus, ...]] = (JobStatus.FAILED, JobStatus.CANCELLED)

#: And the one status a job may be cancelled from. Not ``running``: the handler
#: is executing inside a worker right now, and there is no safe way to stop it
#: from a different process — an ffmpeg encode or a torrent add would be left
#: half-done with a row saying it never happened.
CANCELLABLE: Final[tuple[JobStatus, ...]] = (JobStatus.PENDING,)

NOT_RETRYABLE = (
    "a {status} job cannot be retried; only a failed or cancelled one can. "
    "Queue the work again instead."
)
NOT_CANCELLABLE = "a {status} job cannot be cancelled; only a pending one can."
RUNNING_NOT_CANCELLABLE = (
    "this job is running: a worker is executing it right now, and Arc cannot "
    "stop it safely mid-flight. Wait for it to finish or fail."
)


class JobTransition(RuntimeError):
    """A job is not in a status the requested transition is allowed from.

    Carries the sentence a person should read; the router turns it into a 409.
    """


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


async def _guarded(
    session: AsyncSession, job: Job, allowed: tuple[JobStatus, ...], values: dict[str, Any]
) -> bool:
    """Apply ``values`` to ``job`` only while its status is still in ``allowed``.

    The status goes in the ``WHERE`` clause rather than being read into Python
    first, because between a ``SELECT`` and an ``UPDATE`` a worker can claim the
    very row being cancelled: the read says ``pending``, the write lands on a
    job that is now ``running``, and Arc has marked work cancelled that is
    executing. Postgres closes that on its own — the ``UPDATE`` takes a row
    lock, so it either wins (and the claim's ``FOR UPDATE SKIP LOCKED`` steps
    over the row) or waits for the claim to commit and then re-checks the
    qualification against the new status and matches nothing.

    ``job`` is refreshed either way, so the caller's error message names the
    status the row *actually* holds rather than the one it held a moment ago.
    Returns whether the update applied.
    """
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status.in_(allowed))
            .values(**values)
            .execution_options(synchronize_session=False)
        ),
    )
    await session.refresh(job)
    return bool(result.rowcount)


async def retry_job(session: AsyncSession, job: Job) -> Job:
    """Put a stopped job back on the queue, due now (flushed, not committed).

    ``attempts`` goes back to zero, so a job that exhausted its budget gets a
    full one rather than failing again on the first exception; ``last_error``
    is **kept**, because it is the reason somebody is pressing retry and
    clearing it would erase the diagnosis the moment the fix is tried. The
    lock and the finish timestamps are cleared: the row is pending, and a
    pending row that claims to have started an hour ago is a lie the queue view
    would show.

    Raises :class:`JobTransition` for a job that is not failed or cancelled.
    """
    applied = await _guarded(
        session,
        job,
        RETRYABLE,
        {
            "status": JobStatus.PENDING,
            "attempts": 0,
            "run_after": datetime.now(UTC),
            "locked_by": None,
            "locked_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    if not applied:
        raise JobTransition(NOT_RETRYABLE.format(status=job.status.value))
    return job


async def cancel_job(session: AsyncSession, job: Job) -> Job:
    """Take a pending job off the queue (flushed, not committed).

    ``cancelled`` is a terminal status the claim never selects, so this is the
    whole of the operation — there is nobody to notify.

    Raises :class:`JobTransition` for anything that is not pending, with a
    different sentence for ``running`` because that is the case an admin will
    actually hit and "only a pending one can" does not explain why. That case
    includes losing the race to a worker by a millisecond, which is why the
    status is re-read from the row rather than trusted from before the write.
    """
    applied = await _guarded(
        session,
        job,
        CANCELLABLE,
        {"status": JobStatus.CANCELLED, "finished_at": datetime.now(UTC)},
    )
    if not applied:
        if job.status is JobStatus.RUNNING:
            raise JobTransition(RUNNING_NOT_CANCELLABLE)
        raise JobTransition(NOT_CANCELLABLE.format(status=job.status.value))
    return job


__all__ = [
    "ACTIVE_STATUSES",
    "CANCELLABLE",
    "DEDUPE_FIELD",
    "NOT_CANCELLABLE",
    "NOT_RETRYABLE",
    "RETRYABLE",
    "RUNNING_NOT_CANCELLABLE",
    "JobTransition",
    "cancel_job",
    "enqueue",
    "find_active",
    "retry_job",
]
