"""Background worker entrypoint: ``python -m arc.worker``.

M0 runs the scheduler with a single heartbeat job so the process shape,
logging and shutdown path are proven. M1 adds the real loop: claim jobs from
the ``jobs`` table with ``SELECT … FOR UPDATE SKIP LOCKED``, dispatch through
the handler registry in ``arc.services.jobs``, and retry with backoff
(architecture.md §2, roadmap M1).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from arc import __version__
from arc.config import Settings, get_settings
from arc.core.logging import setup_logging

log = logging.getLogger("arc.worker")

HEARTBEAT_SECONDS = 30


def _heartbeat() -> None:
    log.info("worker heartbeat", extra={"at": datetime.now(UTC).isoformat()})


async def run(settings: Settings) -> None:
    """Run the scheduler until SIGINT/SIGTERM, then shut down cleanly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            # Runs on the main thread, outside the loop: hand the set() back.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _heartbeat,
        "interval",
        seconds=HEARTBEAT_SECONDS,
        id="heartbeat",
        next_run_time=datetime.now(UTC),
    )
    scheduler.start()
    log.info(
        "worker started",
        extra={"env": settings.env, "version": __version__, "heartbeat_s": HEARTBEAT_SECONDS},
    )

    # TODO(M1): start the job-claim loop alongside the scheduler and await
    # both here; stop it on `stop` the same way.
    try:
        await stop.wait()
    finally:
        log.info("worker stopping")
        scheduler.shutdown(wait=False)
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
