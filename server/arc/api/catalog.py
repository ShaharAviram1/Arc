"""Catalogue health for the admin (architecture.md §5b).

One endpoint, and it exists because the fallback is otherwise invisible. When
AniList is down the product keeps working — search returns MAL results, show
pages render, air dates come back estimated — which is the point, and also
means nobody would notice for a week. This is where an admin finds out which
source is answering, when the other one last failed, and what it said.

The numbers come straight off the circuit breaker; nothing here calls a source,
so hitting this endpoint during an outage costs no timeouts.

The season sweep can also be triggered from here (FR-C7). It is the same job
the worker schedules at 03:30 UTC, queued under the same dedupe key, so an
admin pressing the button while the nightly run is pending gets that run back
rather than a second one. Enqueued, never run inline: the sweep is two seasons
of upstream requests and must not happen inside a request handler.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from pydantic import BaseModel

from arc.api.deps import CatalogDep, SessionDep, get_admin_user
from arc.api.jobs import JobOut
from arc.models import Job
from arc.services.catalog.names import CATALOG_PRIORITY, SEASON_SWEEP
from arc.services.jobs import enqueue

router = APIRouter(prefix="/api/catalog", tags=["catalog"], dependencies=[Depends(get_admin_user)])


class SourceStatusOut(BaseModel):
    """One source's breaker state."""

    #: ``"open"`` while Arc is skipping this source, ``"closed"`` otherwise.
    state: str
    #: Last time it answered, and last time it failed. Either may be null on a
    #: freshly started process that has not needed the catalogue yet.
    healthy_at: datetime | None = None
    failed_at: datetime | None = None
    #: What it said when it failed — "api temporarily disabled upstream",
    #: "unconfigured", an HTTP status.
    reason: str | None = None
    #: Whether the source has what it needs to be asked anything. Always true
    #: for AniList, which takes no credentials; false for MAL until
    #: ``MAL_CLIENT_ID`` is set.
    configured: bool = True


class CatalogStatusOut(BaseModel):
    """``GET /api/catalog/status``."""

    sources: dict[str, SourceStatusOut]
    #: Which source the next read would go to: ``"anilist"``, ``"mal"``, or
    #: ``"none"`` when both are open or unconfigured.
    active: str


@router.get(
    "/status",
    response_model=CatalogStatusOut,
    summary="Catalogue source health and breaker state (admin)",
)
async def status(catalog: CatalogDep) -> CatalogStatusOut:
    return CatalogStatusOut.model_validate(catalog.status())


@router.post(
    "/season-sweep",
    response_model=JobOut,
    status_code=http_status.HTTP_202_ACCEPTED,
    summary="Queue the season pre-cache now (admin, FR-C7)",
)
async def season_sweep(session: SessionDep) -> Job:
    """Cache this season and the next from whichever source is up.

    The schedule renders from those rows, so this is the button to press after
    a season rolls over, after an outage, or on a fresh deployment that has not
    reached 03:30 yet.
    """
    job = await enqueue(session, SEASON_SWEEP, priority=CATALOG_PRIORITY, dedupe_key=SEASON_SWEEP)
    await session.commit()
    return job


__all__ = ["SEASON_SWEEP", "CatalogStatusOut", "SourceStatusOut", "router"]
