"""The caller's own list: set a state, drop a show, read it back (FR-C2, FR-W2).

``PUT`` rather than ``POST`` because the request describes the state a
(user, anime) pair should be in, not an event: sending the same body twice
leaves the same row. Whether that row already existed is the only thing the
method has to work out, and it changes exactly one rule — ``status`` is
required on a create.

``anime_id`` in these paths is Arc's internal id (FR-C6). It is not AniList's,
and it is not MAL's; the client gets it from a search result or a show page,
both of which upsert before they answer.

Nothing here writes to MyAnimeList. The change is marked ``mal_dirty`` and
M9's ``mal_push`` job is what carries it upstream, so there is one place that
can write to MAL rather than one per endpoint (FR-M7).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from arc.api.anime_schemas import AnimeSummary, ListEntryOut, ListEntryPatch, ListRow
from arc.api.deps import AnimeId, CatalogDep, CurrentUser, SessionDep
from arc.models import ListStatus
from arc.services.catalog import (
    CATALOGUE_UNAVAILABLE,
    ListEntryError,
    SourceNotFound,
    SourceUnavailable,
    get_my_list,
    remove_list_entry,
    set_list_entry,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/list", tags=["list"])

ANIME_NOT_FOUND = "anime not found"
ENTRY_NOT_FOUND = "not on your list"


@router.get("", response_model=list[ListRow], summary="The caller's list, newest change first")
async def index(
    user: CurrentUser,
    session: SessionDep,
    status_filter: Annotated[ListStatus | None, Query(alias="status")] = None,
) -> list[ListRow]:
    rows = await get_my_list(session, user_id=user.id, status=status_filter)
    return [
        ListRow(
            anime=AnimeSummary.from_anime(anime, entry.status),
            entry=ListEntryOut.model_validate(entry),
        )
        for anime, entry in rows
    ]


@router.put(
    "/{anime_id}",
    response_model=ListEntryOut,
    summary="Add or update a show on the caller's list (FR-C2, FR-W2)",
    responses={
        404: {"description": ANIME_NOT_FOUND},
        422: {"description": "status is required when adding a show"},
        502: {"description": CATALOGUE_UNAVAILABLE},
    },
)
async def put(
    anime_id: AnimeId,
    body: ListEntryPatch,
    user: CurrentUser,
    session: SessionDep,
    catalog: CatalogDep,
) -> ListEntryOut:
    """``anime_id`` is Arc's internal id, not AniList's (FR-C6)."""
    try:
        entry, _anime = await set_list_entry(
            session,
            catalog,
            user_id=user.id,
            anime_id=anime_id,
            status=body.status,
            progress=body.progress,
            score=body.score,
            # ``score: null`` clears the score; an omitted ``score`` leaves it.
            # Pydantic gives both the value ``None``, so the distinction has
            # to come from which keys the caller actually sent.
            score_given="score" in body.model_fields_set,
        )
    except SourceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ANIME_NOT_FOUND) from exc
    except SourceUnavailable as exc:
        log.warning("catalogue fetch failed", extra={"anime_id": anime_id, "error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=CATALOGUE_UNAVAILABLE
        ) from exc
    except ListEntryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    await session.commit()
    return ListEntryOut.model_validate(entry)


@router.delete(
    "/{anime_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a show from the caller's list",
    responses={404: {"description": ENTRY_NOT_FOUND}},
)
async def remove(anime_id: AnimeId, user: CurrentUser, session: SessionDep) -> Response:
    removed = await remove_list_entry(session, user_id=user.id, anime_id=anime_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ENTRY_NOT_FOUND)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["ANIME_NOT_FOUND", "CATALOGUE_UNAVAILABLE", "ENTRY_NOT_FOUND", "router"]
