"""Play on demand: ``POST /api/episodes/{id}/demand`` (FR-A14, M20).

What the player calls when the user presses play on an episode that is not
ready. Thin over :mod:`arc.services.acquisition.demand`, which has the rule.

* **202** ``{state}`` — the episode is wanted at the top of the queue; ``state``
  is the episode's state after the demand (``wanted`` once a search started,
  ``downloading`` …, or ``ready`` if it already was). The player then polls
  ``GET /api/episodes/{id}/play`` for the pending progress.
* **409** ``{detail: {code, message}}``, ``message`` a sentence a person can be
  shown: ``not_in_window`` (the episode is not one the caller's window
  covers), ``acquisition_paused`` or ``storage_held`` (the episode still
  needs fetching and acquisition cannot fetch).
* **404** for an unknown episode.

Open to every signed-in account, the demo one included: it streams, and the
demo is allowed to stream (owner, M20 plan). Unsafe, so the Origin check
(:mod:`arc.api.csrf`) applies. Nothing here touches a list entry or
MyAnimeList.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from arc.api.deps import CurrentUser, EpisodeId, SessionDep, SettingsDep
from arc.models import Episode, EpisodeState
from arc.services.acquisition.demand import DemandRefused, demand

log = logging.getLogger(__name__)

router = APIRouter(tags=["playback"])

EPISODE_NOT_FOUND = "episode not found"


def now() -> datetime:
    """The clock a demand is stamped with; a function so a test can pin it."""
    return datetime.now(UTC)


class DemandOut(BaseModel):
    """``POST /api/episodes/{id}/demand`` — where the episode stands now."""

    state: EpisodeState


@router.post(
    "/api/episodes/{episode_id}/demand",
    response_model=DemandOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fetch an episode the caller pressed play on, at the top of the queue (FR-A14)",
    responses={
        404: {"description": EPISODE_NOT_FOUND},
        409: {"description": "not_in_window | acquisition_paused | storage_held"},
    },
)
async def demand_episode(
    episode_id: EpisodeId, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> DemandOut:
    """Want the episode now; 202 with its state, or 409 with a sentence."""
    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EPISODE_NOT_FOUND)
    try:
        outcome = await demand(
            session, user_id=user.id, episode=episode, now=now(), settings=settings
        )
    except DemandRefused as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    await session.commit()
    return DemandOut(state=outcome.state)


__all__ = ["EPISODE_NOT_FOUND", "DemandOut", "now", "router"]
