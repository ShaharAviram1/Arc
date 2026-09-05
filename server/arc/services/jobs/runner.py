"""Claiming a job, running it, and deciding what happens when it fails.

The claim is the whole reason Arc needs no Redis (architecture.md §1)::

    SELECT ... FROM jobs
     WHERE status = 'pending' AND run_after <= now()
     ORDER BY priority, run_after, id
     LIMIT 1
       FOR UPDATE SKIP LOCKED

``FOR UPDATE`` locks the row for the transaction; ``SKIP LOCKED`` makes a
second worker step over it instead of blocking on it. That is what lets the
worker be scaled to more than one process without a lock server, and it is
why the claim must be committed promptly — the lock lives only as long as the
transaction.

Sessions are deliberately short and separate:

* the claim runs in its own transaction and commits at once;
* the handler gets a fresh session, committed on success and rolled back on
  failure, so a half-done handler leaves nothing behind;
* the resulting status update is a third, short transaction — a job that
  fails must still be *recorded* as failed, which cannot happen inside the
  transaction that just blew up.
"""

from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Job, JobStatus
from arc.services.jobs.registry import JobContext, UnknownJobType, get_handler

log = logging.getLogger("arc.jobs")

#: Wait after the 1st, 2nd, 3rd failed attempt. Beyond that the delay keeps
#: growing (×5) until it hits :data:`MAX_BACKOFF`.
BACKOFF_SECONDS: tuple[int, ...] = (10, 60, 300)
MAX_BACKOFF = timedelta(hours=1)

#: How many ×5 steps are ever actually computed. The cap is reached after two
#: of them (300 × 5² = 7500s > 1h), so anything past this bound is
#: :data:`MAX_BACKOFF` by definition — and computing it anyway would overflow
#: ``timedelta`` for a job with a large ``max_attempts``, which is not allowed
#: to happen: :func:`backoff` feeds :func:`run_job`, which never raises.
MAX_BACKOFF_STEPS = 8

#: ``last_error`` is read by a human in the admin queue view, not parsed. The
#: repr plus the tail of the traceback is what identifies a failure; the rest
#: is framework frames.
MAX_ERROR_CHARS = 2000

#: ``locked_by`` is ``String(64)``.
WORKER_ID_MAX = 64


def claim_statement() -> Select[tuple[Job]]:
    """The claim SELECT, exactly as run by :func:`claim_one`.

    Exposed so that tests can prove the ``SKIP LOCKED`` behaviour on raw
    connections against the same statement the worker uses.
    """
    return (
        select(Job)
        .where(Job.status == JobStatus.PENDING, Job.run_after <= func.now())
        .order_by(Job.priority, Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def backoff(attempts: int) -> timedelta:
    """How long to wait before retrying a job that has failed ``attempts`` times.

    Never raises, for any ``attempts``. The growth factor is clamped *before*
    it is exponentiated rather than after: ``5 ** n`` overflows ``timedelta``
    long before ``min(..., MAX_BACKOFF)`` gets a chance to apply it, and a job
    row with a big ``max_attempts`` would then strand itself in ``running``.
    """
    index = max(attempts, 1) - 1
    if index < len(BACKOFF_SECONDS):
        return timedelta(seconds=BACKOFF_SECONDS[index])
    steps = index - len(BACKOFF_SECONDS) + 1
    if steps > MAX_BACKOFF_STEPS:
        return MAX_BACKOFF
    return min(timedelta(seconds=BACKOFF_SECONDS[-1] * 5**steps), MAX_BACKOFF)


def format_error(exc: BaseException) -> str:
    """``repr`` of the exception plus the tail of its traceback, capped."""
    head = repr(exc)[:MAX_ERROR_CHARS]
    room = MAX_ERROR_CHARS - len(head) - 1
    if room <= 0:
        return head
    tail = "".join(traceback.format_exception(exc)).strip()
    return f"{head}\n{tail[-room:]}"


async def claim_one(session: AsyncSession, worker_id: str) -> Job | None:
    """Take the next due job, or return ``None`` if there is nothing to do.

    Commits before returning: the row lock is only held for the length of the
    claim, and the job is marked ``running`` so that no one else picks it up.
    ``attempts`` is incremented here rather than on failure, so a worker that
    dies mid-job still burns an attempt and a poison job cannot loop forever.
    """
    job = await session.scalar(claim_statement())
    if job is None:
        # Release the (empty) read transaction rather than leaving it idle.
        await session.rollback()
        return None

    now = datetime.now(UTC)
    job.status = JobStatus.RUNNING
    job.locked_by = worker_id[:WORKER_ID_MAX]
    job.locked_at = now
    job.started_at = now
    job.attempts += 1
    await session.commit()
    return job


async def run_job(job: Job, factory: SessionFactory, settings: Settings) -> JobStatus:
    """Run a claimed job and write down how it went. Never raises.

    Returns the status the job ended in: ``done``, ``pending`` (a retry is
    scheduled) or ``failed``.
    """
    try:
        handler = get_handler(job.type)
    except UnknownJobType as exc:
        # Not transient: no retry, however many attempts are left.
        log.error("job type unknown", extra={"job_id": job.id, "type": job.type})
        await _finish(job, factory, JobStatus.FAILED, last_error=format_error(exc))
        return JobStatus.FAILED

    started = datetime.now(UTC)
    try:
        async with factory() as session:
            context = JobContext(job=job, session=session, settings=settings, log=log)
            try:
                await handler(context)
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
    except Exception as exc:
        return await _handle_failure(job, factory, exc)

    log.info(
        "job done",
        extra={
            "job_id": job.id,
            "type": job.type,
            "attempts": job.attempts,
            "ms": round((datetime.now(UTC) - started).total_seconds() * 1000),
        },
    )
    await _finish(job, factory, JobStatus.DONE, last_error=None)
    return JobStatus.DONE


async def _handle_failure(job: Job, factory: SessionFactory, exc: Exception) -> JobStatus:
    """Retry with backoff while attempts remain; otherwise fail the row."""
    error = format_error(exc)
    if job.attempts < job.max_attempts:
        try:
            delay = backoff(job.attempts)
        except Exception:
            # Belt and braces around the arithmetic. Whatever the delay turns
            # out to be, the status update below must still happen: an
            # exception escaping here would break run_job's promise never to
            # raise and leave the row locked in ``running`` until the sweep.
            log.exception("backoff computation failed", extra={"job_id": job.id})
            delay = MAX_BACKOFF
        run_after = datetime.now(UTC) + delay
        log.warning(
            "job failed, retrying",
            extra={
                "job_id": job.id,
                "type": job.type,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "retry_in_s": int(delay.total_seconds()),
                "error": repr(exc),
            },
        )
        await _finish(job, factory, JobStatus.PENDING, last_error=error, run_after=run_after)
        return JobStatus.PENDING

    log.error(
        "job failed permanently",
        extra={
            "job_id": job.id,
            "type": job.type,
            "attempts": job.attempts,
            "error": repr(exc),
        },
    )
    await _finish(job, factory, JobStatus.FAILED, last_error=error)
    return JobStatus.FAILED


async def _finish(
    job: Job,
    factory: SessionFactory,
    status: JobStatus,
    *,
    last_error: str | None,
    run_after: datetime | None = None,
) -> None:
    """Write the outcome in a transaction of its own and clear the lock.

    Written as an UPDATE by id rather than through the ORM object: ``job``
    was loaded by the claim session, which is long closed, and the row must
    be updated even when the handler's session is in an unusable state.
    """
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "status": status,
        "locked_by": None,
        "locked_at": None,
        "last_error": last_error,
        "finished_at": None if status is JobStatus.PENDING else now,
    }
    if run_after is not None:
        values["run_after"] = run_after

    async with factory() as session:
        await session.execute(update(Job).where(Job.id == job.id).values(**values))
        await session.commit()

    # Keep the in-memory row in step, so callers (and tests) can read the
    # outcome off the object they passed in.
    for field, value in values.items():
        setattr(job, field, value)


async def requeue_stale(
    session: AsyncSession, older_than: timedelta = timedelta(minutes=10)
) -> int:
    """Recover jobs left ``running`` by a worker that died. Returns the count.

    A crashed worker leaves its rows locked by a process that no longer
    exists; nothing else will ever touch them. Anything claimed longer ago
    than ``older_than`` goes back to ``pending`` — unless its attempts are
    already spent, in which case it is failed, so that a job which kills the
    worker every time cannot cycle forever.

    ``older_than`` must comfortably exceed the longest a real job takes, or a
    slow transcode will be requeued underneath itself. Handlers are
    idempotent, so the worst case is duplicated work, not corruption.
    """
    cutoff = datetime.now(UTC) - older_than
    stale = (Job.status == JobStatus.RUNNING, Job.locked_at.is_not(None), Job.locked_at < cutoff)
    now = datetime.now(UTC)
    note = f"worker lock expired: no result within {older_than}"

    exhausted = cast(
        CursorResult[Any],
        await session.execute(
            update(Job)
            .where(*stale, Job.attempts >= Job.max_attempts)
            .values(
                status=JobStatus.FAILED,
                locked_by=None,
                locked_at=None,
                finished_at=now,
                last_error=note,
            )
        ),
    )
    requeued = cast(
        CursorResult[Any],
        await session.execute(
            update(Job)
            .where(*stale, Job.attempts < Job.max_attempts)
            .values(
                status=JobStatus.PENDING,
                locked_by=None,
                locked_at=None,
                run_after=now,
                last_error=note,
            )
        ),
    )
    await session.commit()

    total = max(requeued.rowcount, 0) + max(exhausted.rowcount, 0)
    if total:
        log.warning(
            "requeued stale jobs",
            extra={"requeued": requeued.rowcount, "failed": exhausted.rowcount},
        )
    return total


__all__ = [
    "BACKOFF_SECONDS",
    "MAX_BACKOFF",
    "MAX_BACKOFF_STEPS",
    "MAX_ERROR_CHARS",
    "backoff",
    "claim_one",
    "claim_statement",
    "format_error",
    "requeue_stale",
    "run_job",
]
