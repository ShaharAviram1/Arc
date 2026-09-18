"""The worker's claim loop.

One coroutine claims jobs one at a time and hands each to a task; a semaphore
caps how many run at once. Claiming is deliberately serial — the claim is a
single short transaction, and running two of them concurrently would buy
nothing but contention.

**A job type whose in-process cap is full is not claimed at all.** Some work is
capped below ``WORKER_CONCURRENCY`` by something outside the queue — one
``MAX_TRANSCODES`` ffmpeg on a two-vCPU host — and a claim the executing side
cannot serve is worse than no claim: the job takes a concurrency slot, parks on
the media semaphore and holds the slot for the length of the encode ahead of
it. So the loop counts what it is running by type and asks
:func:`~arc.services.jobs.runner.claim_one` to skip the types that are at their
cap (:func:`process_caps`), which hands the free slot to the best *other* job.
Ordering is untouched: nothing is reprioritised, and a skipped type is claimed
in its usual place the moment a slot frees.

Shutdown is cooperative: setting ``stop`` stops the claiming, in-flight jobs
are given ``WORKER_DRAIN_TIMEOUT`` seconds to finish, and anything still
running after that is cancelled. A cancelled job is put straight back to
``pending`` on the way out, so it is picked up by the next worker rather than
waiting for a sweep.

That timeout is therefore a deployment figure as much as a runtime one: it
must fit inside the ``stop_grace_period`` Compose gives the container
(deploy/docker-compose.yml), or Docker's ``SIGKILL`` arrives first and none of
this runs. What covers the case none of it can cover — a worker killed
outright — is
:func:`arc.services.jobs.runner.requeue_orphans`, which the next worker runs
at start-up and which does not wait for a lock to age.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import update

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Job, JobStatus
from arc.services.jobs.runner import claim_one, run_job
from arc.services.media.names import TRANSCODE

log = logging.getLogger("arc.worker")


def process_caps(settings: Settings) -> dict[str, int]:
    """Job types capped *inside one worker process*, and by how much.

    ``WORKER_CONCURRENCY`` is how many jobs the queue side runs at once;
    ``MAX_TRANSCODES`` is how many ffmpegs the host's cores can stand, and it is
    normally the smaller number. On 2026-09-18 production had them at 2 and 1:
    the two lowest priorities in the queue were both transcodes, both were
    claimed, one encoded and the other sat in the second slot for 25 minutes
    parked on the media semaphore — so ``compute_wants``, ``mal_push``,
    ``poll_qbit`` and ``search_release`` waited out an encode. The queue was
    behaving exactly as designed; the mistake was claiming work the encoder
    could not start.

    Named from :mod:`arc.services.media.names` rather than spelled out here, and
    returned as a mapping rather than handled as a special case, so that a
    second capped type would be one more entry and no new plumbing. Transcodes
    are the only one today.
    """
    return {TRANSCODE: settings.max_transcodes}


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
    running: Counter[str],
) -> None:
    """Run one job and give the slot — and its type's cap — back, whatever happens."""
    try:
        await run_job(job, factory, settings)
    except Exception:  # pragma: no cover - run_job is written not to raise
        log.exception("job runner crashed", extra={"job_id": job.id, "type": job.type})
    finally:
        # Before the release, so that the next claim — which cannot run until
        # it has a slot — reads a count this job is no longer in.
        running[job.type] -= 1
        semaphore.release()


def _log_gate(full: frozenset[str], caps: dict[str, int]) -> None:
    """Say once that a type has stopped being claimable, and once when it is again.

    Called only when the set changes, not on every poll: the gate can hold for
    the length of an encode, and a line a second for twenty minutes would bury
    everything else the worker has to say.
    """
    if full:
        log.info(
            "at the per-process cap; claiming other work until a slot frees",
            extra={"types": sorted(full), "caps": {name: caps[name] for name in sorted(full)}},
        )
    else:
        log.info("below the per-process cap; every job type is claimable again")


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
    caps = process_caps(settings)
    # What this process is running, by type, and which types that already puts
    # at their cap. ``at_cap`` is remembered only so the gate is logged when it
    # closes and when it opens, rather than on every poll.
    running: Counter[str] = Counter()
    at_cap: frozenset[str] = frozenset()

    while not stop.is_set():
        if not await _acquire_unless_stopped(semaphore, stop, poll_interval):
            continue

        # Asked again with the slot in hand: SIGTERM may have arrived while
        # this was waiting for one, and claiming a job after the signal means
        # starting work the drain is about to requeue. The shutdown budget is
        # small (``WORKER_DRAIN_TIMEOUT``, and Compose's `stop_grace_period`
        # behind it), so every job not started is one the deploy need not
        # interrupt.
        if stop.is_set():
            semaphore.release()
            break

        full = frozenset(job_type for job_type, cap in caps.items() if running[job_type] >= cap)
        if full != at_cap:
            _log_gate(full, caps)
            at_cap = full

        try:
            async with factory() as session:
                job = await claim_one(session, worker_id, exclude_types=full)
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

        running[job.type] += 1
        task = asyncio.create_task(
            _run_one(job, factory, settings, semaphore, running), name=f"job-{job.id}"
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


__all__ = ["process_caps", "run_worker_loop"]
