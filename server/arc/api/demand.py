"""Play on demand: ``POST`` and ``DELETE /api/episodes/{id}/demand`` (FR-A14, M20, M21).

What the player and the show page call when the user presses play on an
episode that is not ready, and when they withdraw that press. Thin over
:mod:`arc.services.acquisition.demand`, which has the rules.

* **202** ``{state, live, message}`` — the episode is wanted at the top of the
  queue; ``state`` is the episode's state after the demand (``wanted`` once a
  search started, ``downloading`` …, or ``ready`` if it already was). The
  player then polls ``GET /api/episodes/{id}/play`` for the pending progress.
  ``live`` (M20 T6, FR-P8) is true when a downloading episode is being made
  playable while it downloads; ``message`` says when it is not ("will play
  when its download finishes").
* **409** ``{detail: {code, message}}``, ``message`` a sentence a person can be
  shown: ``not_aired``, ``demand_cap`` (the detail then also carries ``open``:
  ``[{episode_id, anime_id, title, number}]``, the caller's open demands, so
  the refusal can link to them), ``acquisition_paused`` or ``storage_held``
  (the episode still needs fetching and acquisition cannot fetch). M20's
  refusal of an episode outside the window is retired: any aired episode may
  be demanded (M21).
* **404** for an unknown episode.

``DELETE`` withdraws the caller's demand (M21): **204** whether or not there
was one (idempotent), 404 for an unknown episode.

Open to every signed-in account, the demo one included: it streams, and the
demo is allowed to stream (owner, M20 plan). Unsafe, so the Origin check
(:mod:`arc.api.csrf`) applies. Nothing here touches a list entry or
MyAnimeList.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from arc.api.deps import CurrentUser, EpisodeId, SessionDep, SettingsDep
from arc.models import Episode, EpisodeState
from arc.services.acquisition.demand import DemandRefused, demand, withdraw

log = logging.getLogger(__name__)

router = APIRouter(tags=["playback"])

EPISODE_NOT_FOUND = "episode not found"


def now() -> datetime:
    """The clock a demand is stamped with; a function so a test can pin it."""
    return datetime.now(UTC)


class OpenDemandOut(BaseModel):
    """One open demand, as the ``demand_cap`` refusal lists it (M21)."""

    model_config = ConfigDict(from_attributes=True)

    episode_id: int
    anime_id: int
    #: The show's title.
    title: str
    number: int


class DemandOut(BaseModel):
    """``POST /api/episodes/{id}/demand`` — where the episode stands now."""

    state: EpisodeState
    #: A live remux was queued: the episode plays while it downloads (FR-P8).
    live: bool = False
    #: Why a downloading episode will only play once its download finishes.
    message: str | None = None


@router.post(
    "/api/episodes/{episode_id}/demand",
    response_model=DemandOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fetch an episode the caller pressed play on, at the top of the queue (FR-A14)",
    responses={
        404: {"description": EPISODE_NOT_FOUND},
        409: {
            "description": "not_aired | demand_cap (with open[]) | acquisition_paused "
            "| storage_held"
        },
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
        detail: dict[str, object] = {"code": exc.code, "message": exc.message}
        if exc.open:
            detail["open"] = [
                OpenDemandOut.model_validate(found).model_dump() for found in exc.open
            ]
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc
    await session.commit()
    return DemandOut(state=outcome.state, live=outcome.live, message=outcome.live_note)


@router.delete(
    "/api/episodes/{episode_id}/demand",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw the caller's demand on an episode (FR-A14, M21)",
    responses={404: {"description": EPISODE_NOT_FOUND}},
)
async def withdraw_demand(
    episode_id: EpisodeId, user: CurrentUser, session: SessionDep
) -> Response:
    """204 whether or not there was a demand: withdrawing twice is not an error."""
    if await session.get(Episode, episode_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EPISODE_NOT_FOUND)
    await withdraw(session, user_id=user.id, episode_id=episode_id, now=now())
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["EPISODE_NOT_FOUND", "DemandOut", "OpenDemandOut", "now", "router"]
