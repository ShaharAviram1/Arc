"""Admin control over playback preparation (FR-P4, FR-P5).

One endpoint for now: queue a remux or a transcode for an episode. M8 hangs the
authenticated ``/media/{episode_id}/…`` streaming routes off the same module,
which is why it is called this rather than ``transcode``.

Like the acquisition controls beside it, this **enqueues and returns 202**
rather than doing the work: an encode is twenty minutes long, and no request
should be. The 202 is unconditional, including on a dedupe hit — "the work is
accepted" is true either way, and a client that had to tell 200 from 202 to
know whether to poll would be doing arithmetic on a status code.

Three things *are* checked in the request, because all three are questions a
person is better answered on directly than through a failed job row a minute
later: the episode has to exist, it has to be in a state a transcode makes
sense from, and re-encoding an episode that is already playable has to be
asked for explicitly (``?force=true``). Everything else — the file being
readable, the container having a video stream, ffmpeg being installed — is the
handler's, and becomes a ``failed`` episode with the tail of the error on it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from arc.api.deps import EpisodeId, SessionDep, get_admin_user
from arc.api.jobs import JobOut
from arc.models import Episode, EpisodeState, Job
from arc.services.media.names import REMUX, TRANSCODE, enqueue_remux, enqueue_transcode

log = logging.getLogger(__name__)

# Admin only, at the router: preparation is operational surface (FR-D2), and a
# route added here later must not be able to forget the check.
router = APIRouter(tags=["media"], dependencies=[Depends(get_admin_user)])

EPISODE_NOT_FOUND = "episode not found"

#: States a transcode can be asked for from. Everything before ``matched`` has
#: no file yet, and ``ready`` needs ``force`` (below).
TRANSCODABLE: frozenset[EpisodeState] = frozenset(
    {EpisodeState.MATCHED, EpisodeState.PREPARING, EpisodeState.FAILED, EpisodeState.READY}
)

NOT_TRANSCODABLE = (
    "this episode has no matched file to prepare (it is {state}); acquire and match one first"
)
ALREADY_READY = (
    "this episode is already prepared; pass force=true to encode it again "
    "(the existing rendition is deleted first)"
)


@router.post(
    "/api/episodes/{episode_id}/transcode",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a remux or a transcode for one episode (admin)",
    responses={
        404: {"description": EPISODE_NOT_FOUND},
        409: {"description": "the episode is not in a state that can be prepared"},
    },
)
async def transcode_episode(
    episode_id: EpisodeId,
    session: SessionDep,
    force: Annotated[bool, Query(description="Re-encode an episode that is already ready")] = False,
    mode: Annotated[
        Literal["remux", "transcode"] | None,
        Query(
            description=(
                "``remux`` copies the video and carries the subtitles as a text track "
                "(the default for new episodes since M20; it hands over to the burn-in "
                "encode itself when the source cannot be copied). ``transcode`` is the "
                "burn-in encode. Omitted: ``transcode`` with ``force``, else ``remux``."
            )
        ),
    ] = None,
) -> Job:
    """Enqueue, do not encode.

    ``mode`` picks the job (M20, architecture.md §5.3c). ``force`` without a
    mode stays what it always was, the burn-in re-encode (FR-P5), so an admin
    who relied on it to get burned-in subtitles back still gets them; without
    ``force`` the default is the remux, as for a newly linked file.

    ``force`` is what FR-P5 keeps the source file for: it deletes the existing
    rendition — row and directory — and encodes again, which is how a change
    of subtitle language or encoder settings reaches an episode that was
    prepared under the old ones. The deletion happens in the handler rather
    than here, so that "the old rendition is gone" and "a new one is being
    made" are one step and cannot half-happen.
    """
    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EPISODE_NOT_FOUND)
    if episode.state not in TRANSCODABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=NOT_TRANSCODABLE.format(state=episode.state.value),
        )
    if episode.state is EpisodeState.READY and not force:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ALREADY_READY)

    chosen = mode or (TRANSCODE if force else REMUX)
    if chosen == REMUX:
        job = await enqueue_remux(session, episode_id, force=force)
    else:
        job = await enqueue_transcode(session, episode_id, force=force)
    await session.commit()
    log.info(
        "preparation queued by an admin",
        extra={"episode_id": episode_id, "job_id": job.id, "force": force, "mode": chosen},
    )
    return job


__all__ = [
    "ALREADY_READY",
    "EPISODE_NOT_FOUND",
    "NOT_TRANSCODABLE",
    "TRANSCODABLE",
    "router",
]
