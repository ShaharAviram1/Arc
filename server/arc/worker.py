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

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from arc import __version__
from arc.config import Settings, get_settings
from arc.core import config_check
from arc.core.logging import setup_logging
from arc.db import SessionFactory, create_engine, create_session_factory
from arc.models import DEFAULT_PRIORITY, Anime, Job
from arc.services.acquisition import jobs as acquisition_jobs  # noqa: F401  (registers handlers)
from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    COMPUTE_WANTS_PRIORITY,
    POLL_QBIT,
    POLL_QBIT_PRIORITY,
    QBIT_POLICY,
    QBIT_POLICY_PRIORITY,
)
from arc.services.acquisition.nyaa import close_shared_client
from arc.services.auth import purge_expired
from arc.services.catalog import jobs as catalog_jobs  # noqa: F401  (registers handlers)
from arc.services.catalog.names import CATALOG_PRIORITY
from arc.services.catalog.seasons import current_season
from arc.services.jobs import enqueue, requeue_stale, run_worker_loop
from arc.services.library import jobs as library_jobs  # noqa: F401  (registers handlers)
from arc.services.library.names import LIBRARY_SCAN, LIBRARY_SCAN_PRIORITY
from arc.services.mal import jobs as mal_jobs  # noqa: F401  (registers handlers)
from arc.services.mal.names import IMPORT_ALL as MAL_IMPORT_ALL
from arc.services.mal.names import IMPORT_PRIORITY as MAL_IMPORT_PRIORITY
from arc.services.media.jobs import sweep_transcodes
from arc.services.recs.factory import close_shared_model
from arc.services.retention import jobs as retention_jobs  # noqa: F401  (registers handlers)
from arc.services.retention.names import RETENTION_PRIORITY, RETENTION_SWEEP

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

#: How often to recompute every user's acquisition window (§5.1 step 1). The
#: window also moves on every list change and every watch completion, both of
#: which enqueue the job themselves; this is the tick that catches what
#: neither can see — an episode airing.
COMPUTE_WANTS_SECONDS = 900

#: How often to ask qBittorrent what it is doing (§5.1 step 4). A minute is
#: the resolution of the download percentage on the show page (FR-A7) and the
#: worst-case delay between a torrent finishing and the transcode starting.
POLL_QBIT_SECONDS = 60

#: How often Arc rewrites qBittorrent's seeding policy (spec §9). Daily, and
#: also at start-up: a container that has just been recreated comes up with
#: qBittorrent's own defaults — seed for ever, upload unlimited — and a
#: long-lived one can be changed by hand in the Web UI. Neither may leave Arc
#: seeding, and once a day is often enough to catch the second.
QBIT_POLICY_SECONDS = 86400

#: How long after start-up the first MyAnimeList import sweep runs. Not
#: immediately: a worker restart is not a reason to re-read everybody's list,
#: and the sweep's own six-hourly period gets there soon enough. Five minutes
#: also keeps it clear of the catalogue work every boot already queues.
MAL_SWEEP_DELAY_SECONDS = 300

#: How often the retention sweep runs (FR-T1: "hourly", architecture.md §5.7).
#: The rule it applies is measured in days, so the period only decides how
#: promptly a file goes once its grace period has run out — an hour is soon
#: enough to be tidy and rare enough that a deployment which has nothing to
#: delete does nothing at all, sixty times a day.
RETENTION_SWEEP_SECONDS = 3600

#: How long after start-up the first sweep runs. Not immediately: a worker
#: that has just come up is the least informed it will ever be — a
#: ``compute_wants`` has not run, so a want dropped as stale during the
#: outage has not been revived yet — and retention is the one job in Arc
#: whose mistakes are not recoverable. Ten minutes costs nothing.
RETENTION_SWEEP_DELAY_SECONDS = 600

#: How often to look for MAL-only rows that AniList could now identify
#: (FR-C6). Hourly: the ids only change when an outage has just ended, and the
#: job is a no-op the rest of the time.
RECONCILE_SECONDS = 3600


#: File under ``DATA_DIR`` whose modification time is the worker's liveness
#: signal. Written on start-up and re-written by every heartbeat tick.
HEARTBEAT_FILENAME = "worker.heartbeat"

#: How stale that file may be before ``--check`` calls the worker dead. Three
#: beats: one missed tick is a busy event loop, three is a process that has
#: stopped running its scheduler. Deliberately generous, because the cost of a
#: false negative is Docker killing a healthy worker mid-transcode.
HEARTBEAT_STALE_AFTER = HEARTBEAT_SECONDS * 3


def worker_id() -> str:
    """Identity written to ``jobs.locked_by`` — ``host:pid``, ≤ 64 chars."""
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


def heartbeat_path(settings: Settings) -> Path:
    """Where the liveness file lives.

    Under ``DATA_DIR`` rather than ``/tmp`` because that is the one directory
    the deployment already guarantees is writable by the worker's user, and
    because ``docker compose exec`` runs the check inside the same container
    and therefore sees the same path.
    """
    return settings.data_dir / HEARTBEAT_FILENAME


def touch_heartbeat(settings: Settings) -> None:
    """Record that the worker is alive, now. Never raises.

    A failure here must not kill the worker: it would turn "the log directory
    is full" into "no episodes are transcoded". The healthcheck will notice
    soon enough, which is exactly its job.
    """
    path = heartbeat_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - depends on the filesystem
        log.warning(
            "could not write the heartbeat file", extra={"path": str(path), "error": str(exc)}
        )


def check_heartbeat(settings: Settings, *, now: float | None = None) -> bool:
    """Whether the heartbeat file is fresh enough to call the worker healthy.

    ``False`` for a missing file too: a worker that has not started has not
    written one, and "no evidence of life" is the same answer as "last seen an
    hour ago" as far as a container healthcheck is concerned.
    """
    path = heartbeat_path(settings)
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age <= HEARTBEAT_STALE_AFTER


def _heartbeat(settings: Settings) -> None:
    touch_heartbeat(settings)
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


async def _enqueue_sweep(factory: SessionFactory, job_type: str, priority: int) -> None:
    """Put a periodic sweep on the queue rather than running it here.

    The scheduler fires in one process; the queue is shared. Going through a
    job row means the sweep is retried if it fails, is visible in the admin
    queue view (FR-D3), and — because of the dedupe key — does not pile up
    when a worker is behind. The key is the type itself: dedupe only matches
    pending/running rows, so yesterday's finished sweep does not block today's.

    ``priority`` is passed rather than defaulted because a scheduled job is
    exactly the kind that must not be allowed to sit at the front of the queue
    by accident: these all fire on timers nobody is watching, and the number
    lives with the job type in its service's ``names`` module so the worker and
    every other caller queue it the same way.
    """
    try:
        async with factory() as session:
            await enqueue(session, job_type, priority=priority, dedupe_key=job_type)
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
            await enqueue(
                session,
                catalog_jobs.SEASON_SWEEP,
                priority=CATALOG_PRIORITY,
                dedupe_key=catalog_jobs.SEASON_SWEEP,
            )
            await session.commit()
        log.info(
            "season pre-cache queued at startup",
            extra={"year": year, "season": season, "swept_before": seen is not None},
        )
    except Exception:  # pragma: no cover - a scheduling failure must not kill the worker
        log.exception("could not seed the season sweep")


async def _sweep_transcodes(factory: SessionFactory) -> None:
    """Queue transcodes for episodes the queue has lost track of (M7, FR-P1).

    Run once at start-up rather than on a timer. Every ordinary transcode is
    enqueued by the link that caused it, so this only ever finds work when the
    worker was *not* running at the moment it should have been: a file matched
    during an outage, an encode killed mid-flight, a retry that came due while
    the process was down. On a healthy deployment it queues nothing, which is
    what makes it safe to run on every boot.
    """
    try:
        async with factory() as session:
            await sweep_transcodes(session)
    except Exception:  # pragma: no cover - a scheduling failure must not kill the worker
        log.exception("could not sweep for unfinished transcodes")


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

    # One ERROR line per production key that is missing or still holds an
    # example value (arc/core/config_check.py). The worker checks the same
    # list as the API: a `docker compose logs worker` is as likely to be where
    # somebody looks first, and the worker is the half that needs QBIT_PASS.
    config_check.log_warnings(settings, component="worker")

    identity = worker_id()
    # Before anything that can block: the healthcheck's grace period starts
    # when the container does, and a first beat that waited on a slow database
    # connection would be a restart loop on a cold host.
    touch_heartbeat(settings)
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
        args=[settings],
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
        args=[factory, catalog_jobs.REFRESH_ALL, DEFAULT_PRIORITY],
    )
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=PRE_AIR_SWEEP_SECONDS,
        id=catalog_jobs.PRE_AIR,
        args=[factory, catalog_jobs.PRE_AIR, DEFAULT_PRIORITY],
    )
    # Catalogue fallback upkeep (FR-C6, FR-C7): attach AniList ids to rows that
    # arrived through MAL, and pre-cache the season so the schedule survives an
    # outage of both sources.
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=RECONCILE_SECONDS,
        id=catalog_jobs.RECONCILE,
        args=[factory, catalog_jobs.RECONCILE, CATALOG_PRIORITY],
    )
    scheduler.add_job(
        _enqueue_sweep,
        "cron",
        hour=SEASON_SWEEP_HOUR,
        minute=SEASON_SWEEP_MINUTE,
        id=catalog_jobs.SEASON_SWEEP,
        args=[factory, catalog_jobs.SEASON_SWEEP, CATALOG_PRIORITY],
    )
    # Acquisition (FR-A1, FR-A5): recompute the wants, and watch the client.
    # Both start immediately rather than one interval in: a worker that has
    # just come up is exactly when a download that finished while it was down
    # needs noticing, and when a list change made during the outage needs
    # acting on.
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=COMPUTE_WANTS_SECONDS,
        id=COMPUTE_WANTS,
        args=[factory, COMPUTE_WANTS, COMPUTE_WANTS_PRIORITY],
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=POLL_QBIT_SECONDS,
        id=POLL_QBIT,
        args=[factory, POLL_QBIT, POLL_QBIT_PRIORITY],
        next_run_time=datetime.now(UTC),
    )
    # Seeding policy (spec §9): tell the client not to seed and to cap its
    # upload. Immediately, because a torrent added before the policy is
    # written is a torrent that seeds until ``poll_qbit`` notices.
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=QBIT_POLICY_SECONDS,
        id=QBIT_POLICY,
        args=[factory, QBIT_POLICY, QBIT_POLICY_PRIORITY],
        next_run_time=datetime.now(UTC),
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
        args=[factory, LIBRARY_SCAN, LIBRARY_SCAN_PRIORITY],
        next_run_time=datetime.now(UTC),
    )
    # MyAnimeList re-import (FR-M3): pull every linked account's list on the
    # configured period, default six hours. One sweep job that queues one
    # import per user, spaced, rather than a job per user on a timer — the
    # number of linked users is not something the scheduler should know.
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        hours=settings.mal_import_interval_hours,
        id=MAL_IMPORT_ALL,
        args=[factory, MAL_IMPORT_ALL, MAL_IMPORT_PRIORITY],
        next_run_time=datetime.now(UTC) + timedelta(seconds=MAL_SWEEP_DELAY_SECONDS),
    )
    # Retention (FR-T1, FR-T2): delete what nobody is going to watch again.
    # Deliberately not started immediately — see the constant above.
    scheduler.add_job(
        _enqueue_sweep,
        "interval",
        seconds=RETENTION_SWEEP_SECONDS,
        id=RETENTION_SWEEP,
        args=[factory, RETENTION_SWEEP, RETENTION_PRIORITY],
        next_run_time=datetime.now(UTC) + timedelta(seconds=RETENTION_SWEEP_DELAY_SECONDS),
    )
    scheduler.start()

    # …and once now if it has never run. A fresh deployment would otherwise
    # have an empty schedule until 03:30 tomorrow, which is exactly the state
    # FR-C7 exists to avoid.
    await _seed_season_sweep(factory)
    # …and catch up on anything matched, half-encoded or awaiting a retry
    # while this worker was not running (FR-P1, FR-P4).
    await _sweep_transcodes(factory)
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
            "compute_wants_s": COMPUTE_WANTS_SECONDS,
            "mal_import_interval_h": settings.mal_import_interval_hours,
            "poll_qbit_s": POLL_QBIT_SECONDS,
            "qbit_policy_s": QBIT_POLICY_SECONDS,
            "qbit_seeding": settings.qbit_seeding,
            "qbit_upload_limit_kib": settings.qbit_upload_limit_kib,
            "retention_sweep_s": RETENTION_SWEEP_SECONDS,
            "retention_dry_run": settings.retention_dry_run,
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
        # The Nyaa client is process-wide and outlives every search job, so
        # this is the only place that closes it (acquisition/nyaa.py).
        await close_shared_client()
        # Likewise the model chain: it is per process precisely so that the
        # daily-quota cooldowns outlive one suggestion job (recs/factory.py),
        # which makes this the only place that can close it. Usually a no-op —
        # a deployment with no provider never builds one.
        await close_shared_model()
        await engine.dispose()
        # A worker that has stopped on purpose is not "recently alive", and
        # leaving the file behind would keep ``--check`` green for another
        # ninety seconds after the process is gone.
        heartbeat_path(settings).unlink(missing_ok=True)
        log.info("worker stopped")


def main(argv: list[str] | None = None) -> int:
    """``python -m arc.worker`` — run the worker, or ``--check`` its health.

    ``--check`` is what the container healthcheck runs (deploy/docker-compose.yml).
    It is a *file* check rather than a database or HTTP one on purpose: the
    worker has no port to probe, and asking it about the queue would report
    Postgres's health rather than the worker's — a worker wedged with a dead
    event loop and a healthy database would pass.
    """
    parser = argparse.ArgumentParser(prog="arc.worker", description="Arc background worker")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 if this container's worker heartbeat is fresh, 1 otherwise",
    )
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.check:
        path = heartbeat_path(settings)
        if check_heartbeat(settings):
            print(f"worker heartbeat is fresh ({path})")
            return 0
        print(f"worker heartbeat missing or stale ({path})", file=sys.stderr)
        return 1

    setup_logging(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # pragma: no cover - signal race on some platforms
        log.info("worker interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
