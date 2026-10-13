"""Pinning a show: "Keep files" (FR-T8, M20 T8).

Two routes, both answering **204** and both idempotent:

* ``PUT /api/anime/{id}/pin`` — keep this show's files after watching. A
  second pin is the same row.
* ``DELETE /api/anime/{id}/pin`` — stop keeping them. Unpinning a show that
  was not pinned is not an error: the state asked for is the state there is.

**404** for an unknown show, and **403** to the demo account before anything
is looked up — a pin holds files on the disk, like the other actions the demo
account is refused (owner 2026-10-13). Whether the caller has pinned a show is
read off the show payload (``pinned`` on ``GET /api/anime/{id}``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status

from arc.api.anime import ANIME_NOT_FOUND
from arc.api.deps import AnimeId, CurrentUser, SessionDep
from arc.models import Anime
from arc.services.pins import pin_show, unpin_show
from arc.services.trips.create import DEMO_REFUSED

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/anime", tags=["anime"])


async def _check(anime_id: int, user_is_demo: bool, session: SessionDep) -> None:
    if user_is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DEMO_REFUSED)
    if await session.get(Anime, anime_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ANIME_NOT_FOUND)


@router.put(
    "/{anime_id}/pin",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Keep this show's files after watching (FR-T8)",
    responses={403: {"description": DEMO_REFUSED}, 404: {"description": ANIME_NOT_FOUND}},
)
async def pin(anime_id: AnimeId, user: CurrentUser, session: SessionDep) -> Response:
    """Pin the show for the caller. Idempotent."""
    await _check(anime_id, user.is_demo, session)
    await pin_show(session, user_id=user.id, anime_id=anime_id)
    await session.commit()
    log.info("show pinned", extra={"user_id": user.id, "anime_id": anime_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{anime_id}/pin",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop keeping this show's files after watching (FR-T8)",
    responses={403: {"description": DEMO_REFUSED}, 404: {"description": ANIME_NOT_FOUND}},
)
async def unpin(anime_id: AnimeId, user: CurrentUser, session: SessionDep) -> Response:
    """Unpin the show for the caller. Idempotent."""
    await _check(anime_id, user.is_demo, session)
    await unpin_show(session, user_id=user.id, anime_id=anime_id)
    await session.commit()
    log.info("show unpinned", extra={"user_id": user.id, "anime_id": anime_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
