"""Admin control over retention: preview it, run it, delete one episode (FR-T4).

Three endpoints, and the first is the point of the other two. ``GET
/api/retention/preview`` answers the only question an admin ever really has
about a process that deletes files — *what is it about to delete, and why?* —
by calling the sweep's own rule with the deletion left out
(:func:`arc.services.retention.sweep.candidates`). It is not a second
implementation of the policy; a preview that could disagree with the sweep
would be worse than no preview.

The two POSTs **enqueue and answer 202**, like every other operational button
in Arc. Deleting an episode talks to qBittorrent and to the filesystem, and
neither belongs in a request. Both deduplicate — the sweep on its type, the
manual delete on its episode — and the 202 is unconditional, because "the work
is accepted" is true of a dedupe hit too.

The manual delete does check two things in the request, because both are
better said to a person than left in a failed job row a minute later: the
episode has to exist, and it must not be one retention may never touch (a
download in flight, an encode running). Everything else — the files being
gone already, the torrent having been removed by hand — is the handler's, and
is not an error there either.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from arc.api.deps import EpisodeId, SessionDep, SettingsDep, get_admin_user
from arc.api.jobs import JobOut
from arc.models import Anime, Episode, EpisodeState, Job
from arc.services.catalog import preferred_title
from arc.services.retention.names import enqueue_delete_files, enqueue_retention_sweep
from arc.services.retention.sweep import PROTECTED_STATES, candidates

log = logging.getLogger(__name__)

# Admin only, at the router: this is the surface that removes files (FR-T4),
# and a route added here later must not be able to forget the check.
router = APIRouter(tags=["retention"], dependencies=[Depends(get_admin_user)])

EPISODE_NOT_FOUND = "episode not found"

IN_FLIGHT = (
    "this episode is {state}; its files are being written or read right now. "
    "Wait for it to finish, or cancel it first."
)


class RetentionItemOut(BaseModel):
    """One episode the next sweep would delete."""

    episode_id: int
    anime_id: int
    anime_title: str
    number: int
    state: EpisodeState
    #: The sentence the sweep will log: which rule applied and when the grace
    #: period ran out.
    reason: str
    #: What the deletion would free, rendition plus source, measured on disk.
    bytes: int
    #: The paths, so an admin can look before pressing the button. Null when
    #: that half is not on the disk (a rendition row whose directory is gone).
    rendition_dir: str | None = None
    source_dir: str | None = None
    #: qBittorrent hashes that would be removed with their data.
    torrents: list[str] = []


class RetentionPreviewOut(BaseModel):
    """``GET /api/retention/preview``."""

    #: Whether ``RETENTION_DRY_RUN`` is on, in which case the sweep will
    #: report exactly this list and delete none of it.
    dry_run: bool
    episodes: list[RetentionItemOut]
    #: The sum of the list, so the client does not have to add it up.
    bytes: int


@router.get(
    "/api/retention/preview",
    response_model=RetentionPreviewOut,
    summary="What the next retention sweep would delete (admin)",
)
async def preview(session: SessionDep, settings: SettingsDep) -> RetentionPreviewOut:
    """The sweep's own rule, read-only.

    Nothing is written and no upstream service is called, so this is safe to
    poll and safe to look at while a sweep is running.
    """
    found = await candidates(session, settings)
    titles: dict[int, str] = {}
    for target in found:
        if target.anime_id not in titles:
            anime = await session.get(Anime, target.anime_id)
            titles[target.anime_id] = preferred_title(anime) if anime is not None else ""
    return RetentionPreviewOut(
        dry_run=settings.retention_dry_run,
        bytes=sum(target.bytes for target in found),
        episodes=[
            RetentionItemOut(
                episode_id=target.episode_id,
                anime_id=target.anime_id,
                anime_title=titles[target.anime_id],
                number=target.number,
                state=target.state,
                reason=target.reason,
                bytes=target.bytes,
                rendition_dir=(
                    str(target.targets.rendition_dir) if target.targets.rendition_dir else None
                ),
                source_dir=(str(target.targets.source_dir) if target.targets.source_dir else None),
                torrents=list(target.targets.torrent_hashes),
            )
            for target in found
        ],
    )


@router.post(
    "/api/retention/sweep",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run the retention sweep now instead of on the hour (admin)",
)
async def sweep_now(session: SessionDep) -> Job:
    """The same job the scheduler queues hourly, under the same dedupe key."""
    job = await enqueue_retention_sweep(session)
    await session.commit()
    log.info("retention sweep queued by an admin", extra={"job_id": job.id})
    return job


@router.post(
    "/api/episodes/{episode_id}/delete-files",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Delete one episode's files now (admin, FR-T4)",
    responses={
        404: {"description": EPISODE_NOT_FOUND},
        409: {"description": "the episode's files are in use"},
    },
)
async def delete_files(episode_id: EpisodeId, session: SessionDep) -> Job:
    """Delete now, grace period or not.

    The episode goes back to ``not_wanted``, so anybody who still wants it has
    it re-acquired by the next ``compute_wants`` — which is FR-T4's "or
    re-fetch", without a second button.
    """
    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EPISODE_NOT_FOUND)
    if episode.state in PROTECTED_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=IN_FLIGHT.format(state=episode.state.value),
        )
    job = await enqueue_delete_files(session, episode_id)
    await session.commit()
    log.info(
        "manual file deletion queued by an admin",
        extra={"episode_id": episode_id, "job_id": job.id},
    )
    return job


__all__ = [
    "EPISODE_NOT_FOUND",
    "IN_FLIGHT",
    "RetentionItemOut",
    "RetentionPreviewOut",
    "router",
]
