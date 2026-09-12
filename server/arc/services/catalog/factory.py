"""Building a :class:`CatalogService` from settings.

Two callers with different lifetimes. The API builds one service per app and
keeps it on ``app.state`` for the life of the process, so its pacing and its
breaker reflect the real request rate — that is the case the breaker exists
for, where an outage would otherwise cost one timeout per page view. A job
builds one per run and closes it: a worker slot has no ``app.state``, and
holding two HTTP clients open between hourly sweeps buys nothing.

A per-job service means a per-job breaker, so every sweep re-probes a source
that was down an hour ago. That is the behaviour worth having: a scheduled job
is exactly where a probe belongs, and one 15-second timeout an hour is not a
cost worth engineering around. The breaker still earns its keep *within* a run
— the reconciliation stops after the first failure instead of timing out fifty
times.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arc.config import Settings
from arc.services.anilist.source import AniListSource
from arc.services.catalog.breaker import Breaker
from arc.services.catalog.service import CatalogService
from arc.services.mal.catalog import MalSource


def create_catalog(
    settings: Settings,
    *,
    breaker: Breaker | None = None,
    wait_on_rate_limit: bool = True,
) -> CatalogService:
    """AniList primary, MAL fallback, with a breaker in front of both.

    ``wait_on_rate_limit`` is the other difference between the two lifetimes
    above: a job may sleep off AniList's 429 and an API request may not (§6).
    The default is the job's, so that only the one caller with a user waiting
    on it has to say so.
    """
    return CatalogService(
        AniListSource.from_settings(settings, wait_on_rate_limit=wait_on_rate_limit),
        MalSource.from_settings(settings),
        breaker if breaker is not None else Breaker(settings.catalog_breaker_seconds),
    )


@asynccontextmanager
async def catalog_for(settings: Settings) -> AsyncIterator[CatalogService]:
    """A catalogue for the duration of one job, closed on the way out."""
    catalog = create_catalog(settings)
    try:
        yield catalog
    finally:
        await catalog.aclose()


__all__ = ["catalog_for", "create_catalog"]
