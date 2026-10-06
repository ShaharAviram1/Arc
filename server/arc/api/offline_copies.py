"""Asking for an episode's small offline copy (FR-P6).

Two endpoints, for any signed-in account except the demo one:

* ``POST /api/episodes/{id}/offline`` — "Keep offline" on a ready episode.
  Queues the copy if there is not one and answers **202** with
  :class:`~arc.api.offline_schemas.OfflineOut` either way: ``available`` with
  a ``url`` when the copy is already made, ``queued``/``preparing`` while it
  is being made. **409** ``{"detail": "source_gone"}`` when the episode is
  ready but the source a copy would be made from is no longer on disk — the
  device downloads the full-size ``episode.mp4`` instead (FR-S7). The same
  409 with ``storage_held`` while the data volume is under its floor (FR-T6),
  and **429** ``copy_queue_full`` when the caller already has 10 copies queued
  or being made, or the host has 30 — both only when the request would start
  a *new* encode, and both answered on the device with the full-size file.
* ``GET /api/episodes/{id}/offline`` — the same answer without asking for
  anything: what a device polls while ``preparing``.

Both answer **404** for an unknown episode and for one that is not ``ready``,
and **403** to the demo account before anything is looked up. Like the
transcode control beside them, nothing here runs ffmpeg: the encode is a job
(:mod:`arc.services.media.offline`), twenty-odd seconds a minute of episode
behind every transcode in the queue.
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import APIRouter, HTTPException, status

from arc.api.deps import CurrentUser, EpisodeId, SessionDep, SettingsDep
from arc.api.media_stream import DEMO_REFUSED
from arc.api.offline_schemas import OfflineOut
from arc.models import Episode, EpisodeState, OfflineCopy
from arc.services.acquisition.rules import is_storage_held
from arc.services.media.copies import (
    COPY_QUEUE_FULL,
    SOURCE_GONE,
    STORAGE_HELD,
    CopyQueueFull,
    CopyUnavailable,
    SourceGone,
    StorageHeld,
    request_copy,
    source_on_disk,
)
from arc.services.media.names import latest_offline_jobs

log = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

#: The one 404 both routes give: unknown and not-ready are the same answer, as
#: on ``/play`` — the show page is where a user learns *why* an episode is not
#: ready, and this is only ever asked about one that a page showed as ready.
NOT_READY: Final[str] = "episode not found or not ready"


def _refuse_demo(user_is_demo: bool) -> None:
    if user_is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DEMO_REFUSED)


async def _answer(
    session: SessionDep, settings: SettingsDep, episode_id: int, copy: OfflineCopy | None
) -> OfflineOut:
    """:class:`OfflineOut` for a ready episode, checking the source on disk."""
    jobs = await latest_offline_jobs(session, [episode_id]) if copy is not None else {}
    has_source = await source_on_disk(session, episode_id) is not None
    return OfflineOut.build(
        episode_id,
        copy=copy,
        job=jobs.get(episode_id),
        has_source=has_source,
        codec=settings.offline_codec,
    )


@router.post(
    "/api/episodes/{episode_id}/offline",
    response_model=OfflineOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for the small offline copy of one ready episode (FR-P6)",
    responses={
        403: {"description": DEMO_REFUSED},
        404: {"description": NOT_READY},
        409: {
            "description": (
                "no usable copy can be made now: source_gone (the source is gone) or "
                "storage_held (the data volume is under its floor); download the "
                "full-size file"
            )
        },
        429: {
            "description": "copy_queue_full: too many copies queued; download the full-size file"
        },
    },
)
async def request_offline_copy(
    episode_id: EpisodeId, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> OfflineOut:
    """Queue the copy unless it exists; 202 with where it stands either way."""
    _refuse_demo(user.is_demo)
    try:
        asked = await request_copy(
            session,
            settings,
            episode_id,
            user_id=user.id,
            storage_held=lambda: is_storage_held(session, settings),
        )
    except CopyUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_READY) from exc
    except SourceGone as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=SOURCE_GONE) from exc
    except StorageHeld as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=STORAGE_HELD) from exc
    except CopyQueueFull as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=COPY_QUEUE_FULL
        ) from exc
    await session.commit()
    log.info(
        "offline copy asked for",
        extra={
            "user_id": user.id,
            "episode_id": episode_id,
            "job_id": asked.job.id if asked.job is not None else None,
            "state": asked.copy.state.value if asked.copy is not None else None,
        },
    )
    return OfflineOut.build(
        episode_id,
        copy=asked.copy,
        job=asked.job,
        has_source=asked.has_source,
        codec=settings.offline_codec,
    )


@router.get(
    "/api/episodes/{episode_id}/offline",
    response_model=OfflineOut,
    summary="Where one ready episode's small offline copy stands (FR-P6)",
    responses={403: {"description": DEMO_REFUSED}, 404: {"description": NOT_READY}},
)
async def get_offline_copy(
    episode_id: EpisodeId, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> OfflineOut:
    """Read-only, and safe to poll."""
    _refuse_demo(user.is_demo)
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.state is not EpisodeState.READY:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_READY)
    copy = await session.get(OfflineCopy, episode_id)
    return await _answer(session, settings, episode_id, copy)


__all__ = ["NOT_READY", "router"]
