"""Background worker entrypoint: ``python -m arc.worker``.

One process, two things running side by side (architecture.md §2):

* the **claim loop** — takes due rows out of ``jobs`` with ``SELECT … FOR
  UPDATE SKIP LOCKED`` and runs their handlers, up to ``WORKER_CONCURRENCY``
  at a time;
* the **scheduler** (APScheduler) — periodic work: the heartbeat, the sweep
  that recovers jobs a crashed worker left locked, the hourly purge of expired
  sessions (M2), the catalogue's five periodic jobs (M3, M3b), and the library
  scan that finds new files on disk (M5). M6+ hangs the rest (Nyaa polling,
  MAL re-import, retention) off the same scheduler.

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
from sqlalchemy import select

from arc import __version__
from arc.config import Settings, get_settings
from arc.core.logging import setup_logging
from arc.db import SessionFactory, create_engine, create_session_factory
from arc.models import Anime, Job
from arc.services.auth import purge_expired
from arc.services.catalog import jobs as catalog_jobs  # noqa: F401  (registers handlers)
from arc.services.catalog.seasons import current_season
from arc.services.jobs import enqueue, requeue_stale, run_worker_loop
from arc.services.library import jobs as library_jobs  # noqa: F401  (registers handlers)
from arc.services.library.names import LIBRARY_SCAN

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

#: When the daily catalogue sweep runs (UTC). 04:00 is the quiet end of every
#: timezone Arc cares about and well clear of the evening airing block, so a
#: hundred refreshes never compete with somebody opening a show page.
CATALOGUE_SWEEP_HOUR = 4

#: When the season pre-cache runs (UTC). Half an hour before the refresh sweep,
#: so a season that has just rolled over has its rows before the sweep decides
#: which of them are worth a full fetch (FR-C7).
SEASON_SWEEP_HOUR = 3
SEASON_SWEEP_MINUTE = 30

#: How often to look for shows about to air (FR-C5: "within one hour of a
#: followed show's scheduled air time"). The job itself looks 90 minutes
#: ahead, so an hourly period covers every air time with margin.
PRE_AIR_SWEEP_SECONDS = 3600

#: How often to look for MAL-only rows that AniList could now identify
#: (FR-C6). Hourly: the ids only change when an outage has just ended, and the
#: job is a no-op the rest of the time.
RECONCILE_SECONDS = 3600


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


async def _enqueue_sweep(factory: SessionFactory, job_type: str) -> None:
    """Put a periodic sweep on the queue rather than running it here.

    The scheduler fires in one process; the queue is shared. Going through a
    job row means the sweep is retried if it fails, is visible in the admin
    queue view (FR-D3), and — because of the dedupe key — does not pile up
    when a worker is behind. The key is the type itself: dedupe only matches
    pending/running rows, so yesterday's finished sweep does not block today's.
    """
    try:
        async with factory() as session:
            await enqueue(session, job_type, dedupe_key=job_type)
            await session.commit()
    except Exception:  # pragma: no cover - a scheduling failure must not kill the worker
        log.exception("could not enqueue sweep", extra={"job_type": job_type})


async def _seed_season_sweep(factory: SessionFactory) -> None:
    """Enqueue the season pre-cache if the schedule would otherwise be empty.

    Two conditions, either of which is enough. The first is that this
    deployment has never run the sweep — asked of the job table rather than of
    the ``anime`` rows, because "has the sweep happened" is the question and a
    season with no titles is also what a season nobody has searched looks like.

    The second is that the sweep *has* run but the cache holds nothing for the
    season Arc is in now. That is what a worker restarting the morning after a
    season rolls over looks like, and what a run that failed against both
    sources leaves behind: the schedule page (FR-C3) would render seven empty
    days until 03:30 tomorrow, which is precisely the state FR-C7 exists to
    avoid.

    The enqueue is deduplicated on the job type, so a sweep already pending
    from the scheduler is returned rather than doubled.
    """
    try:
        year, season = current_season()
        async with factory() as session:
            seen = await session.scalar(
                select(Job.id).where(Job.type == catalog_jobs.SEASON_SWEEP).limit(1)
            )
            cached = await session.scalar(
                select(Anime.id).where(Anime.season == season, Anime.season_year == year).limit(1)
            )
            if seen is not None and cached is not None:
                return
            await enqueue(session, catalog_jobs.SEASON_SWEEP, dedupe_key=catalog_jobs.SEASON_SWEEP)
            await session.commit()
        log.info(
            "season pre-cache queued at startup",
            extra={"year": year, "season": season, "swept_before": seen is not None},
        )
    except Exception:  # pragma: no cover - a scheduling failure must not kill the worker
        log.exception("could not seed the season sweep")


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
    # Catalogue refresh (FR-C5): a daily sweep of everything followed or
    # airing, and an hourly pass over whatever is about to air.
    scheduler.add_job(
        _enqueue_sweep,
        "cron",
        hour=CATALOGUE_SWEEP_HOUR,
        minute=0,
        id=catalog_jobs.REFRESH_ALL,
        args=[factory, catalog_jobs.REFRESH_ALL],
    )
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=PRE_AIR_SWEEP_SECONDS,
        id=catalog_jobs.PRE_AIR,
        args=[factory, catalog_jobs.PRE_AIR],
    )
    # Catalogue fallback upkeep (FR-C6, FR-C7): attach AniList ids to rows that
    # arrived through MAL, and pre-cache the season so the schedule survives an
    # outage of both sources.
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=RECONCILE_SECONDS,
        id=catalog_jobs.RECONCILE,
        args=[factory, catalog_jobs.RECONCILE],
    )
    scheduler.add_job(
        _enqueue_sweep,
        "cron",
        hour=SEASON_SWEEP_HOUR,
        minute=SEASON_SWEEP_MINUTE,
        id=catalog_jobs.SEASON_SWEEP,
        args=[factory, catalog_jobs.SEASON_SWEEP],
    )
    # Library ingest (FR-L1): walk the download and manual-drop directories.
    # ``next_run_time`` is now, not one interval from now — a worker that has
    # just started is exactly when a file dropped in while it was down needs
    # picking up, and waiting two minutes to notice would be the first thing
    # anyone complained about.
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=settings.library_scan_interval_seconds,
        id=LIBRARY_SCAN,
        args=[factory, LIBRARY_SCAN],
        next_run_time=datetime.now(UTC),
    )
    scheduler.start()

    # …and once now if it has never run. A fresh deployment would otherwise
    # have an empty schedule until 03:30 tomorrow, which is exactly the state
    # FR-C7 exists to avoid.
    await _seed_season_sweep(factory)
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
            "library_scan_s": settings.library_scan_interval_seconds,
            "scheduled": sorted(job.id for job in scheduler.get_jobs()),
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
