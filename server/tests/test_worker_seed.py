"""The worker's startup seed of the season pre-cache (FR-C7).

The scheduler runs the sweep at 03:30 UTC, which is no help to a deployment
that starts at 09:00 on the day a season rolls over: the schedule page reads
the cached rows and there are none for the new season yet. So the worker asks
one question at startup — is there anything cached for the season we are in? —
and queues the sweep if there is not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from arc.db import SessionFactory
from arc.models import Anime, Job, JobStatus
from arc.services.catalog.names import SEASON_SWEEP
from arc.services.catalog.seasons import current_season
from arc.services.jobs import enqueue
from arc.worker import _seed_season_sweep

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
