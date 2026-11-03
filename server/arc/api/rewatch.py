"""Rewatch mode's switch (FR-W7, M21).

Two routes, both idempotent:

* ``PUT /api/anime/{id}/rewatch`` — open a pass on the show, or answer the one
  already open: **200** with :class:`~arc.api.playback_schemas.RewatchOut`.
  **409** with a sentence when the show has no aired episode — the status the
  sample route (FR-A8) gives the same refusal.
* ``DELETE /api/anime/{id}/rewatch`` — end the open pass (reason ``off``):
  **204**, also when there was none — the state asked for is the state there
  is.

**404** for an unknown show on both. Mutating, so both go through
:class:`arc.api.csrf.OriginCheckMiddleware` like every unsafe ``/api/``
request. The rules live in :mod:`arc.services.playback.rewatch`; nothing here
writes to a list entry or to MyAnimeList, and neither does anything it calls.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status

from arc.api.anime import ANIME_NOT_FOUND
from arc.api.deps import AnimeId, CurrentUser, SessionDep
from arc.api.playback_schemas import RewatchOut
from arc.models import Anime
from arc.services.playback.rewatch import NOTHING_AIRED_DETAIL, NothingAired, start, stop, summary

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/anime", tags=["anime"])


def now() -> datetime:
    """The clock a pass starts and ends by; a function so a test can pin it."""
    return datetime.now(UTC)


async def _check(anime_id: int, session: SessionDep) -> None:
    if await session.get(Anime, anime_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ANIME_NOT_FOUND)


@router.put(
    "/{anime_id}/rewatch",
    response_model=RewatchOut,
    summary="Turn rewatch mode on for this show (FR-W7)",
    responses={404: {"description": ANIME_NOT_FOUND}, 409: {"description": NOTHING_AIRED_DETAIL}},
)
async def rewatch_on(anime_id: AnimeId, user: CurrentUser, session: SessionDep) -> RewatchOut:
    """Open a pass, or answer the open one. Idempotent."""
    await _check(anime_id, session)
    at = now()
    try:
        opened = await start(session, user_id=user.id, anime_id=anime_id, now=at)
    except NothingAired as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    out = await summary(session, opened, now=at)
    await session.commit()
    log.info("rewatch on", extra={"user_id": user.id, "anime_id": anime_id})
    return RewatchOut(
        started_at=out.started_at,
        position=out.position,
        finished_in_pass=out.finished_in_pass,
        total=out.total,
    )


@router.delete(
    "/{anime_id}/rewatch",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Turn rewatch mode off for this show (FR-W7)",
    responses={404: {"description": ANIME_NOT_FOUND}},
)
async def rewatch_off(anime_id: AnimeId, user: CurrentUser, session: SessionDep) -> Response:
    """End the open pass, if any. Idempotent."""
    await _check(anime_id, session)
    ended = await stop(session, user_id=user.id, anime_id=anime_id, now=now())
    await session.commit()
    if ended:
        log.info("rewatch off", extra={"user_id": user.id, "anime_id": anime_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
