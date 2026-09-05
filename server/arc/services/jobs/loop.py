"""The worker's claim loop.

One coroutine claims jobs one at a time and hands each to a task; a semaphore
caps how many run at once. Claiming is deliberately serial — the claim is a
single short transaction, and running two of them concurrently would buy
nothing but contention.

Shutdown is cooperative: setting ``stop`` stops the claiming, in-flight jobs
are given ``WORKER_DRAIN_TIMEOUT`` seconds to finish, and anything still
running after that is cancelled. A cancelled job is put straight back to
``pending`` on the way out, so it is picked up by the next worker rather than
waiting out ``WORKER_STALE_AFTER`` for
:func:`arc.services.jobs.runner.requeue_stale` — which stays the backstop for
the case this cannot cover, a worker that is killed outright.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import update

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Job, JobStatus
from arc.services.jobs.runner import claim_one, run_job

log = logging.getLogger("arc.worker")


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, returning early if ``stop`` is set meanwhile."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def _acquire_unless_stopped(
    semaphore: asyncio.Semaphore, stop: asyncio.Event, timeout: float
) -> bool:
    """Take a slot, giving up after ``timeout`` so ``stop`` is rechecked."""
    if stop.is_set():
        return False
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def _run_one(
    job: Job,
    factory: SessionFactory,
    settings: Settings,
    semaphore: asyncio.Semaphore,
) -> None:
    """Run one job and give the slot back, whatever happens."""
    try:
        await run_job(job, factory, settings)
    except Exception:  # pragma: no cover - run_job is written not to raise
        log.exception("job runner crashed", extra={"job_id": job.id, "type": job.type})
    finally:
        semaphore.release()


async def run_worker_loop(
    factory: SessionFactory,
    settings: Settings,
    stop: asyncio.Event,
    *,
    worker_id: str,
    concurrency: int,
    poll_interval: float = 1.0,
) -> None:
    """Claim and run jobs until ``stop`` is set, then drain and return."""
    semaphore = asyncio.Semaphore(concurrency)
    # Task → job id, so that the drain can name (and requeue) whatever it has
    # to cancel; a task alone does not say which row it was holding.
    in_flight: dict[asyncio.Task[None], int] = {}

    while not stop.is_set():
        if not await _acquire_unless_stopped(semaphore, stop, poll_interval):
            continue

        try:
            async with factory() as session:
                job = await claim_one(session, worker_id)
        except Exception:  # pragma: no cover - transient database trouble
            semaphore.release()
            log.exception("claim failed", extra={"worker_id": worker_id})
            await _sleep_unless_stopped(stop, poll_interval)
            continue

        if job is None:
            semaphore.release()
            await _sleep_unless_stopped(stop, poll_interval)
            continue

        log.info(
            "job claimed",
            extra={"job_id": job.id, "type": job.type, "attempt": job.attempts},
        )

        task = asyncio.create_task(
            _run_one(job, factory, settings, semaphore), name=f"job-{job.id}"
        )
        in_flight[task] = job.id
        task.add_done_callback(lambda finished: in_flight.pop(finished, None))

    await _drain(in_flight, factory, settings.worker_drain_timeout)


async def _drain(
    in_flight: dict[asyncio.Task[None], int], factory: SessionFactory, timeout: float
) -> None:
    """Let running jobs finish, then cancel and requeue whatever is left."""
    pending = {task: job_id for task, job_id in in_flight.items() if not task.done()}
    if not pending:
        log.info("worker loop stopped", extra={"drained": 0})
        return

    log.info("draining jobs", extra={"in_flight": len(pending), "timeout_s": timeout})
    _, still_running = await asyncio.wait(set(pending), timeout=timeout)
    if still_running:
        cancelled = sorted(pending[task] for task in still_running)
        log.warning(
            "drain timed out, cancelling jobs",
            extra={"cancelled": len(still_running), "job_ids": cancelled},
        )
        for task in still_running:
            task.cancel()
        await asyncio.gather(*still_running, return_exceptions=True)
        await _requeue_cancelled(factory, cancelled)
    log.info("worker loop stopped", extra={"drained": len(pending)})


async def _requeue_cancelled(factory: SessionFactory, job_ids: list[int]) -> None:
    """Put jobs the drain had to cancel back on the queue, now rather than later.

    A cancelled handler never reaches the runner's status update, so its row
    would otherwise stay ``running``, locked by a process that has exited,
    until the stale sweep notices — ``WORKER_STALE_AFTER`` seconds of a job
    nobody is working on. Only rows still ``running`` are touched: one that
    finished between the ``cancel()`` and this update has already recorded its
    own outcome and must not be resurrected.

    The attempt spent on the interrupted run is not given back; that is the
    same bargain :func:`~arc.services.jobs.runner.requeue_stale` makes, and it
    is what stops a job that kills its worker from cycling forever.

    A short transaction of its own, and never fatal: the worker is on its way
    out, and the sweep remains the backstop if this cannot be written.
    """
    try:
        async with factory() as session:
            await session.execute(
                update(Job)
                .where(Job.id.in_(job_ids), Job.status == JobStatus.RUNNING)
                .values(
                    status=JobStatus.PENDING,
                    locked_by=None,
                    locked_at=None,
                    run_after=datetime.now(UTC),
                    last_error="worker shut down before this job finished",
                )
            )
            await session.commit()
    except Exception:  # pragma: no cover - database trouble during shutdown
        log.exception("could not requeue cancelled jobs", extra={"job_ids": job_ids})
        return
    log.info("cancelled jobs returned to the queue", extra={"job_ids": job_ids})


__all__ = ["run_worker_loop"]
