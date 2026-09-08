"""Admin controls over acquisition: pause it, force a search, look at wants.

None of these does any work in the request. Searching Nyaa takes four paced
requests and adding a magnet talks to another process; both belong on the queue
where they can be retried and watched (FR-D3). So the work-queueing POSTs
answer **202** with the job row and the client polls ``/api/jobs/{id}`` if it
cares. They deduplicate, so pressing a button twice queues one job and returns
the same row the second time; the 202 is unconditional, because "the work is
accepted" is true of a dedupe hit as well and a client that had to tell 201
from 200 to know whether to poll would be doing arithmetic on a status code.

``GET /api/acquisition/wants`` is the diagnostic they exist for: it answers
"why is Arc downloading that?" and "why is it *not* downloading this?" by
showing the live wants with the episode's state beside them. It is not the show
page's data — that comes through ``GET /api/anime/{id}`` per episode (FR-A7) —
it is the whole table at once, which is an admin's question.

**Pause, resume and status are different in kind**, and answer 200 rather than
202. Pausing is not work to be queued: it is a row in ``settings`` that has
either been written or has not, and the caller needs to know *which* before it
can say anything truthful to the person who pressed the button. Queueing that
would mean an admin pressing "pause" and being told "accepted" while the jobs
already in flight carried on adding torrents.

Resume queues a ``compute_wants`` in the same transaction, so the switch and
the reconciliation that acts on it land together: a resume that wrote the flag
and lost the job would leave Arc unpaused and idle until the fifteen-minute
tick came round.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from arc.api.deps import EpisodeId, SessionDep, get_admin_user
from arc.api.jobs import JobOut
from arc.models import Anime, Episode, EpisodeState, Job, User, Want
from arc.services.acquisition.names import (
    POLL_QBIT,
    POLL_QBIT_PRIORITY,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    search_dedupe_key,
)
from arc.services.acquisition.names import enqueue_compute_wants as queue_wants
from arc.services.acquisition.rules import is_paused, set_paused
from arc.services.catalog import preferred_title
from arc.services.jobs import enqueue

log = logging.getLogger(__name__)

# Admin only, at the router: these are operational controls (FR-D2, FR-D3),
# and a route added here later must not be able to forget the check.
router = APIRouter(tags=["acquisition"], dependencies=[Depends(get_admin_user)])

MAX_LIMIT = 500


class PauseOut(BaseModel):
    """What ``POST /api/acquisition/{pause,resume}`` answers.

    The flag as it now stands, not "ok": the button is a toggle, and the caller
    should render what the server believes rather than what it asked for.
    """

    paused: bool


class AcquisitionStatusOut(BaseModel):
    """``GET /api/acquisition/status`` — the switch and what it is holding.

    The three counts are the answer to "is the pause doing anything?". They are
    deliberately the *episode* states rather than job counts: a queue full of
    ``search_release`` rows that are all requeueing themselves is what a pause
    looks like from the job table, and it says nothing about how much of the
    library is mid-flight.
    """

    #: The ``acquisition_paused`` setting.
    paused: bool
    #: Wants that have not been dropped, across every user (FR-A2 merges them;
    #: this is the row count, so an episode three people want counts three).
    active_wants: int
    #: Episodes in the ``searching`` state — a search job has claimed them and
    #: is looking, or was looking when it last ran.
    searching: int
    #: Episodes qBittorrent is downloading. Unaffected by the pause: these
    #: finish, and ``poll_qbit`` hands them on (FR-A5).
    downloading: int


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
async def search_episode(episode_id: EpisodeId, session: SessionDep) -> Job:
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
        priority=SEARCH_RELEASE_PRIORITY,
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
    job = await enqueue(session, POLL_QBIT, priority=POLL_QBIT_PRIORITY, dedupe_key=POLL_QBIT)
    await session.commit()
    return job


@router.post(
    "/api/acquisition/pause",
    response_model=PauseOut,
    summary="Stop acquiring anything new (admin)",
)
async def pause(session: SessionDep) -> PauseOut:
    """Set the kill switch.

    Nothing is cancelled: searches already queued stay queued and requeue
    themselves every quarter of an hour, and downloads already running finish
    and are ingested. What stops is *starting* things — no new want is
    computed, no new query goes to Nyaa, no new magnet reaches qBittorrent.
    """
    paused = await set_paused(session, True)
    await session.commit()
    log.info("acquisition paused by an admin")
    return PauseOut(paused=paused)


@router.post(
    "/api/acquisition/resume",
    response_model=PauseOut,
    summary="Start acquiring again (admin)",
)
async def resume(session: SessionDep) -> PauseOut:
    """Clear the kill switch and recompute the window immediately.

    The recompute is queued in the same transaction as the flag, so resuming
    cannot half-happen. Deduplicated like every other ``compute_wants``, so a
    resume while the fifteen-minute tick is already pending adds nothing.
    """
    paused = await set_paused(session, False)
    job = await queue_wants(session)
    await session.commit()
    log.info("acquisition resumed by an admin", extra={"job_id": job.id})
    return PauseOut(paused=paused)


@router.get(
    "/api/acquisition/status",
    response_model=AcquisitionStatusOut,
    summary="Whether acquisition is paused, and what it is holding (admin)",
)
async def acquisition_status(session: SessionDep) -> AcquisitionStatusOut:
    """Three counts and a flag; no upstream call, so it is cheap to poll."""
    active_wants = await session.scalar(
        select(func.count()).select_from(Want).where(Want.dropped_at.is_(None))
    )
    rows = await session.execute(
        select(Episode.state, func.count())
        .where(Episode.state.in_((EpisodeState.SEARCHING, EpisodeState.DOWNLOADING)))
        .group_by(Episode.state)
    )
    states: dict[EpisodeState, int] = {state: count for state, count in rows.all()}
    return AcquisitionStatusOut(
        paused=await is_paused(session),
        active_wants=active_wants or 0,
        searching=states.get(EpisodeState.SEARCHING, 0),
        downloading=states.get(EpisodeState.DOWNLOADING, 0),
    )


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


__all__ = ["MAX_LIMIT", "AcquisitionStatusOut", "PauseOut", "WantOut", "router"]
