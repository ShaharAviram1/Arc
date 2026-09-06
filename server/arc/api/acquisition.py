"""Admin controls over acquisition: force a search, recompute, look at wants.

Three endpoints, all admin-only, and none of them does any work in the
request. Searching Nyaa takes four paced requests and adding a magnet talks to
another process; both belong on the queue where they can be retried and
watched (FR-D3). So the two POSTs answer **202** with the job row and the
client polls ``/api/jobs/{id}`` if it cares.

``GET /api/acquisition/wants`` is the diagnostic the other two exist for: it
answers "why is Arc downloading that?" and "why is it *not* downloading this?"
by showing the live wants with the episode's state beside them. It is not the
show page's data — that comes through ``GET /api/anime/{id}`` per episode
(FR-A7) — it is the whole table at once, which is an admin's question.

Both POST routes deduplicate, so pressing a button twice queues one job and
returns the same row the second time. The 202 is unconditional: "the work is
accepted" is true of a dedupe hit as well, and a client that had to tell 201
from 200 to know whether to poll would be doing arithmetic on a status code.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from arc.api.deps import SessionDep, get_admin_user
from arc.api.jobs import JobOut
from arc.models import Anime, Episode, EpisodeState, Job, User, Want
from arc.services.acquisition.names import POLL_QBIT, SEARCH_RELEASE, search_dedupe_key
from arc.services.acquisition.names import enqueue_compute_wants as queue_wants
from arc.services.catalog import preferred_title
from arc.services.jobs import enqueue

log = logging.getLogger(__name__)

# Admin only, at the router: these are operational controls (FR-D2, FR-D3),
# and a route added here later must not be able to forget the check.
router = APIRouter(tags=["acquisition"], dependencies=[Depends(get_admin_user)])

MAX_LIMIT = 500


class WantOut(BaseModel):
    """One live want, with enough context to read the row without a join."""

    user_id: int
    user_email: str | None = None
    episode_id: int
    episode_number: int
    anime_id: int
    anime_title: str
    #: The episode's acquisition state — the thing the want is *for*.
    state: EpisodeState
    unavailable_reason: str | None = None


@router.post(
    "/api/episodes/{episode_id}/search",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a Nyaa search for one episode (admin)",
)
async def search_episode(episode_id: int, session: SessionDep) -> Job:
    """Enqueue, do not search.

    The id is not validated here, deliberately: the handler checks it, and a
    bad id becomes a job row whose ``last_error`` says so rather than a 404
    that first cost a database round trip. It is also the only honest answer —
    an episode that exists but is already ``ready`` is not a 404 either, and
    the handler is where that judgement lives.
    """
    job = await enqueue(
        session,
        SEARCH_RELEASE,
        {"episode_id": episode_id},
        dedupe_key=search_dedupe_key(episode_id),
    )
    await session.commit()
    return job


@router.post(
    "/api/acquisition/compute-wants",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Recompute every user's acquisition window (admin)",
)
async def compute_wants(session: SessionDep) -> Job:
    """The same job the scheduler queues every fifteen minutes (FR-A1)."""
    job = await queue_wants(session)
    await session.commit()
    return job


@router.post(
    "/api/acquisition/poll",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Poll qBittorrent now instead of on the minute (admin)",
)
async def poll_now(session: SessionDep) -> Job:
    """Useful when watching a download by hand; the timer does it anyway."""
    job = await enqueue(session, POLL_QBIT, dedupe_key=POLL_QBIT)
    await session.commit()
    return job


@router.get(
    "/api/acquisition/wants",
    response_model=list[WantOut],
    summary="Every live acquisition want (admin)",
)
async def list_wants(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 200,
) -> list[WantOut]:
    """Wants that have not been dropped, newest first.

    ``dropped_at IS NULL`` is the partial index on the table, so this is the
    query the schema was shaped for.
    """
    rows = await session.execute(
        select(Want, Episode, Anime, User)
        .join(Episode, Episode.id == Want.episode_id)
        .join(Anime, Anime.id == Episode.anime_id)
        .join(User, User.id == Want.user_id)
        .where(Want.dropped_at.is_(None))
        .order_by(Want.created_at.desc(), Want.episode_id.desc())
        .limit(limit)
    )
    return [
        WantOut(
            user_id=want.user_id,
            user_email=user.email,
            episode_id=episode.id,
            episode_number=episode.number,
            anime_id=anime.id,
            anime_title=preferred_title(anime),
            state=episode.state,
            unavailable_reason=episode.unavailable_reason,
        )
        for want, episode, anime, user in rows.all()
    ]


__all__ = ["MAX_LIMIT", "WantOut", "router"]
