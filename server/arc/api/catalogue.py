"""What the offline catalogue import has loaded (admin, M15.5).

One endpoint, and it exists for the same reason ``/api/catalog/status`` does:
the thing it reports on is invisible when it works. The offline tables are
consulted first by search and matching (later tasks in this milestone) and by
nothing a user can see directly, so an import that quietly stopped running
three months ago would look exactly like one that ran on Monday — until the
week AniList goes down again and the fallback turns out to be a stale copy of
the catalogue.

Hence ``stale``: true when manami has never been imported, or when the import
Arc has is older than ``OFFLINE_CATALOGUE_STALE_DAYS`` (14 by default, which is
two missed weekly runs). It is the flag the admin Storage tab and the config
check read; the counts beside it are the sanity check that the rows survived
whatever the last import did.

Nothing here downloads anything. The import is a job — ``import_offline_
catalogue``, weekly at 03:30 UTC on Monday, or ``python -m arc.cli
import-catalogue`` for an operator who does not want to wait — and a 62 MB
download inside a request handler would be a timeout, not a feature.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from arc.api.deps import SessionDep, SettingsDep, get_admin_user
from arc.models import OfflineAnime, OfflineId, OfflineImport
from arc.services.catalog.offline.names import MANAMI

router = APIRouter(
    prefix="/api/catalogue", tags=["catalogue"], dependencies=[Depends(get_admin_user)]
)


class OfflineSourceOut(BaseModel):
    """One row of ``offline_imports``."""

    #: ``"manami"`` or ``"fribb"``.
    source: str
    #: The release tag (manami) or the ``ETag``/``Last-Modified``/date the file
    #: was served with (Fribb). Null when neither could be determined.
    version: str | None = None
    imported_at: datetime
    #: How many rows that import wrote.
    rows: int
    #: sha256 of the file it came from — what the next run compares against to
    #: decide whether there is anything to do.
    checksum: str | None = None


class OfflineCatalogueOut(BaseModel):
    """``GET /api/catalogue/offline``."""

    #: One entry per source that has ever been imported, newest first. Empty
    #: on a deployment where the job has not run yet.
    sources: list[OfflineSourceOut]
    #: True when manami's import is missing or older than the configured
    #: staleness window. The one field worth putting in front of an admin.
    stale: bool
    anime_rows: int
    id_rows: int


@router.get(
    "/offline",
    response_model=OfflineCatalogueOut,
    summary="Offline catalogue import status (admin, M15.5)",
)
async def offline(session: SessionDep, settings: SettingsDep) -> OfflineCatalogueOut:
    rows = list(
        (
            await session.scalars(select(OfflineImport).order_by(OfflineImport.imported_at.desc()))
        ).all()
    )
    anime_rows = await session.scalar(select(func.count()).select_from(OfflineAnime)) or 0
    id_rows = await session.scalar(select(func.count()).select_from(OfflineId)) or 0

    cutoff = datetime.now(UTC) - timedelta(days=settings.offline_catalogue_stale_days)
    manami = next((row for row in rows if row.source == MANAMI), None)
    stale = manami is None or _as_utc(manami.imported_at) < cutoff

    return OfflineCatalogueOut(
        sources=[OfflineSourceOut.model_validate(row, from_attributes=True) for row in rows],
        stale=stale,
        anime_rows=int(anime_rows),
        id_rows=int(id_rows),
    )


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    The column is ``timestamptz`` and asyncpg always hands back an aware value,
    so this only ever matters to a test that inserted one by hand — but a
    comparison that raises ``TypeError`` on a naive datetime is not the way to
    find that out.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = ["OfflineCatalogueOut", "OfflineSourceOut", "router"]
