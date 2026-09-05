"""Background worker entrypoint: ``python -m arc.worker``.

One process, two things running side by side (architecture.md §2):

* the **claim loop** — takes due rows out of ``jobs`` with ``SELECT … FOR
  UPDATE SKIP LOCKED`` and runs their handlers, up to ``WORKER_CONCURRENCY``
  at a time;
* the **scheduler** (APScheduler) — periodic work: the heartbeat, the sweep
  that recovers jobs a crashed worker left locked, and the hourly purge of
  expired sessions (M2). M3+ hangs the real periodic jobs (AniList refresh,
  Nyaa polling, MAL re-import, retention) off the same scheduler.

More than one worker may run at once; ``SKIP LOCKED`` is what makes that safe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from arc import __version__
from arc.config import Settings, get_settings
from arc.core.logging import setup_logging
from arc.db import SessionFactory, create_engine, create_session_factory
from arc.services.auth import purge_expired
from arc.services.jobs import requeue_stale, run_worker_loop

log = logging.getLogger("arc.worker")

HEARTBEAT_SECONDS = 30
#: How often to look for jobs abandoned by a dead worker.
STALE_SWEEP_SECONDS = 300
#: How often to delete expired session rows. A scheduler task rather than a
#: queued job: it needs no payload, no retry and no history, and a job row per
#: hour forever would be noise in the queue view (FR-D3). Expired sessions are
#: already refused by ``resolve_session``, so this is only housekeeping and
#: missing an hour costs nothing.
SESSION_PURGE_SECONDS = 3600


def worker_id() -> str:
    """Identity written to ``jobs.locked_by`` — ``host:pid``, ≤ 64 chars."""
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


def _heartbeat() -> None:
    log.info("worker heartbeat", extra={"at": datetime.now(UTC).isoformat()})


async def _sweep_stale(factory: SessionFactory, older_than: timedelta) -> None:
    """Return jobs locked by a worker that is no longer running to the queue."""
    try:
        async with factory() as session:
            await requeue_stale(session, older_than)
    except Exception:  # pragma: no cover - a sweep failure must not kill the worker
        log.exception("stale job sweep failed")


async def _purge_sessions(factory: SessionFactory) -> None:
    """Delete expired rows from ``sessions`` (architecture.md §7)."""
    try:
        async with factory() as session:
            removed = await purge_expired(session)
        if removed:
            log.info("expired sessions purged", extra={"count": removed})
    except Exception:  # pragma: no cover - housekeeping must not kill the worker
        log.exception("session purge failed")


async def run(settings: Settings) -> None:
    """Run the scheduler and the claim loop until SIGINT/SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            # Runs on the main thread, outside the loop: hand the set() back.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    identity = worker_id()
    engine = create_engine(settings)
    factory = create_session_factory(engine)

    stale_after = timedelta(seconds=settings.worker_stale_after)

    # Before claiming anything, take back whatever a previous run of this
    # worker (or another one) died holding.
    await _sweep_stale(factory, stale_after)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _heartbeat,
        "interval",
        seconds=HEARTBEAT_SECONDS,
        id="heartbeat",
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        _sweep_stale,
        "interval",
        seconds=STALE_SWEEP_SECONDS,
        id="requeue_stale",
        args=[factory, stale_after],
    )
    scheduler.add_job(
        _purge_sessions,
        "interval",
        seconds=SESSION_PURGE_SECONDS,
        id="purge_expired_sessions",
        args=[factory],
    )
    scheduler.start()
    log.info(
        "worker started",
        extra={
            "env": settings.env,
            "version": __version__,
            "worker_id": identity,
            "concurrency": settings.worker_concurrency,
            "poll_interval_s": settings.worker_poll_interval,
            "heartbeat_s": HEARTBEAT_SECONDS,
            "stale_after_s": settings.worker_stale_after,
            "stale_sweep_s": STALE_SWEEP_SECONDS,
            "session_purge_s": SESSION_PURGE_SECONDS,
        },
    )

    try:
        await run_worker_loop(
            factory,
            settings,
            stop,
            worker_id=identity,
            concurrency=settings.worker_concurrency,
            poll_interval=settings.worker_poll_interval,
        )
    finally:
        log.info("worker stopping")
        scheduler.shutdown(wait=False)
        await engine.dispose()
        log.info("worker stopped")


def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # pragma: no cover - signal race on some platforms
        log.info("worker interrupted")


if __name__ == "__main__":
    main()
