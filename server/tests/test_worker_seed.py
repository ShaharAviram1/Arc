"""What the worker does at startup that its timers would otherwise do later.

The scheduler runs the sweep at 03:30 UTC, which is no help to a deployment
that starts at 09:00 on the day a season rolls over: the schedule page reads
the cached rows and there are none for the new season yet. So the worker asks
one question at startup — is there anything cached for the season we are in? —
and queues the sweep if there is not.

The offline catalogue import (M15.5) has the same shape and a longer timer:
weekly, Monday 03:30. A deployment that has never imported it would have no
offline catalogue for up to seven days, which is precisely the window the
offline catalogue exists to cover, so the worker asks whether it has ever run
and schedules the first one immediately if it has not.

The last section is start-up of a different kind: what the worker takes back
from the container it replaced, before it claims anything of its own.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from arc import worker as worker_module
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Anime, Job, JobStatus, OfflineImport
from arc.services.catalog.names import SEASON_SWEEP
from arc.services.catalog.seasons import current_season
from arc.services.jobs import enqueue
from arc.worker import _offline_never_imported, _seed_season_sweep, run, worker_id

pytestmark = pytest.mark.pg


async def sweeps(factory: SessionFactory) -> list[Job]:
    async with factory() as session:
        rows = await session.scalars(select(Job).where(Job.type == SEASON_SWEEP).order_by(Job.id))
        return list(rows.all())


async def cache_a_show(factory: SessionFactory, *, season: str, year: int) -> None:
    async with factory() as session:
        session.add(
            Anime(
                anilist_id=920001,
                title_romaji="Cached Show",
                season=season,
                season_year=year,
                summary_source="anilist",
            )
        )
        await session.commit()


async def test_a_fresh_deployment_queues_the_sweep(api_factory: SessionFactory) -> None:
    await _seed_season_sweep(api_factory)

    queued = await sweeps(api_factory)
    assert len(queued) == 1
    assert queued[0].payload["dedupe_key"] == SEASON_SWEEP


async def test_a_cached_current_season_needs_no_sweep(api_factory: SessionFactory) -> None:
    year, season = current_season()
    await cache_a_show(api_factory, season=season, year=year)
    async with api_factory() as session:
        await enqueue(session, SEASON_SWEEP, dedupe_key=SEASON_SWEEP)
        job = await session.scalar(select(Job).where(Job.type == SEASON_SWEEP))
        assert job is not None
        job.status = JobStatus.DONE
        await session.commit()

    await _seed_season_sweep(api_factory)

    assert len(await sweeps(api_factory)) == 1


async def test_a_season_rollover_queues_another_sweep(api_factory: SessionFactory) -> None:
    """The sweep has run before, but not for the season Arc is in now."""
    year, season = current_season()
    # Last season's rows are cached; this season's are not.
    await cache_a_show(api_factory, season=season, year=year - 1)
    async with api_factory() as session:
        await enqueue(session, SEASON_SWEEP, dedupe_key=SEASON_SWEEP)
        job = await session.scalar(select(Job).where(Job.type == SEASON_SWEEP))
        assert job is not None
        job.status = JobStatus.DONE
        await session.commit()

    await _seed_season_sweep(api_factory)

    assert len(await sweeps(api_factory)) == 2


async def test_a_pending_sweep_is_not_doubled(api_factory: SessionFactory) -> None:
    """Two workers starting at once must not queue two sweeps."""
    await _seed_season_sweep(api_factory)
    await _seed_season_sweep(api_factory)

    assert len(await sweeps(api_factory)) == 1


# --- the offline catalogue import (M15.5) -----------------------------------


async def test_a_deployment_with_no_offline_catalogue_imports_it_at_once(
    api_factory: SessionFactory,
) -> None:
    assert await _offline_never_imported(api_factory) is True


async def test_an_imported_catalogue_waits_for_monday(api_factory: SessionFactory) -> None:
    """Even a *stale* import waits: a worker restart is not a reason to pull
    62 MB, and the cron entry is a few days away at most."""
    async with api_factory() as session:
        session.add(
            OfflineImport(
                source="manami",
                version="2026-01",
                rows=41_000,
                imported_at=datetime.now(UTC),
            )
        )
        await session.commit()

    assert await _offline_never_imported(api_factory) is False


# --- Taking back the previous container's work (M16) -------------------------
#
# A deploy replaces the worker container mid-transcode. Its ffmpeg dies with
# it and the job row stays `running`, locked by an identity that no longer
# exists — seconds old, so the age-based sweep leaves it for two hours and the
# episode sits in `preparing` (job 927, 2026-09-13). The next worker's first
# act must be to take it back, and it must happen before the claim loop starts.


class _StubScheduler:
    """APScheduler's shape, without the timers.

    The real scheduler would fire half a dozen `_enqueue_sweep` jobs the
    instant it started — several are registered with ``next_run_time`` of
    now — and write them to the test database from tasks that outlive the
    test. What is under test here is the order of two awaits, so the
    scheduler is replaced rather than tolerated.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.started = False

    def add_job(self, *args: Any, **kwargs: Any) -> None:
        return None

    def start(self) -> None:
        self.started = True

    def get_jobs(self) -> list[Any]:
        return []

    def shutdown(self, wait: bool = True) -> None:
        return None


async def a_job_the_previous_worker_was_running(factory: SessionFactory) -> int:
    """A `running` row under a dead container's lock, taken a moment ago."""
    async with factory() as session:
        job = await enqueue(session, "noop")
        await session.commit()
        job_id = job.id

    async with factory() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        row.status = JobStatus.RUNNING
        row.locked_by = "old-container:1"
        row.locked_at = datetime.now(UTC)
        row.attempts = 1
        await session.commit()
    return job_id


async def test_the_startup_reclaim_runs_before_the_first_claim(
    api_factory: SessionFactory,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run()`` itself, with the claim loop standing in for the first claim."""
    job_id = await a_job_the_previous_worker_was_running(api_factory)
    seen: dict[str, Any] = {}

    async def fake_loop(
        factory: SessionFactory,
        loop_settings: Settings,
        stop: asyncio.Event,
        *,
        worker_id: str,
        concurrency: int,
        poll_interval: float,
    ) -> None:
        # Whatever the queue looks like here is what the first claim would see.
        async with factory() as session:
            row = await session.get(Job, job_id)
            assert row is not None
            seen["status"] = row.status
            seen["locked_by"] = row.locked_by
            seen["last_error"] = row.last_error
        seen["worker_id"] = worker_id

    monkeypatch.setattr(worker_module, "AsyncIOScheduler", _StubScheduler)
    monkeypatch.setattr(worker_module, "run_worker_loop", fake_loop)

    await run(settings.model_copy(update={"data_dir": tmp_path}))

    assert seen["worker_id"] == worker_id(), "the loop claims under this process's identity"
    assert seen["status"] is JobStatus.PENDING, "the orphan was still running at the first claim"
    assert seen["locked_by"] is None
    assert "worker restarted" in (seen["last_error"] or "")


async def test_the_startup_reclaim_leaves_the_worker_running_if_it_fails(
    api_factory: SessionFactory,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery is housekeeping: a database hiccup must not be a boot loop."""
    reached = False

    async def boom(session: Any, identity: str) -> int:
        raise RuntimeError("the database said no")

    async def fake_loop(*args: Any, **kwargs: Any) -> None:
        nonlocal reached
        reached = True

    monkeypatch.setattr(worker_module, "AsyncIOScheduler", _StubScheduler)
    monkeypatch.setattr(worker_module, "requeue_orphans", boom)
    monkeypatch.setattr(worker_module, "run_worker_loop", fake_loop)

    await run(settings.model_copy(update={"data_dir": tmp_path}))

    assert reached, "a failed reclaim must not stop the worker from starting"
