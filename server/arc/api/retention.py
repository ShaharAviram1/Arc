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

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from arc.api.deps import EpisodeId, SessionDep, SettingsDep, get_admin_user
from arc.api.jobs import JobOut
from arc.models import Anime, Episode, EpisodeState, Job
from arc.services.catalog import preferred_title
from arc.services.pins import pinned_usage
from arc.services.retention.floor import floor_plan
from arc.services.retention.names import enqueue_delete_files, enqueue_retention_sweep
from arc.services.retention.rules import grace_days, offline_idle_period, watched_grace_hours
from arc.services.retention.sweep import (
    PROTECTED_STATES,
    Deletable,
    idle_copies,
    retained_usage,
    retention_plan,
)
from arc.services.storage import disk_usage

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
    #: qBittorrent hashes that would be removed with their data. **Empty for a
    #: batch-backed episode** (FR-A11): a pack's row belongs to no episode, so
    #: the query behind this cannot reach one, and deleting it by hash with its
    #: files would take several other episodes' bytes.
    torrents: list[str] = []
    #: How many ``torrent_files`` claims the deletion would give back (FR-A11).
    #: 1 for an episode being held by a pack, 0 for everything else. The row is
    #: not deleted and the bytes above are the file's own; what this counts is
    #: the claim, which is un-wanted *before* the file is unlinked so that a
    #: start for another episode cannot re-fetch what retention just removed.
    torrent_files: int = 0
    #: In ``held`` only: the hold that keeps this episode past its grace
    #: (pinned, part-way through, played recently, copy being downloaded).
    hold: str | None = None


class RetentionPreviewOut(BaseModel):
    """``GET /api/retention/preview``."""

    #: Whether ``RETENTION_DRY_RUN`` is on, in which case the sweep will
    #: report exactly this list and delete none of it.
    dry_run: bool
    episodes: list[RetentionItemOut]
    #: The sum of the list, so the client does not have to add it up.
    bytes: int
    #: Past their grace but kept by a hold (M20 T8). Not deleted by the next
    #: sweep; listed so that "why is this still on the disk?" has an answer.
    held: list[RetentionItemOut] = []
    #: What the floor eviction would then take (FR-T6, M20 T8): non-empty only
    #: while free space, after the normal pass, is under the floor. The same
    #: plan and reason strings as the sweep (``retention/floor.floor_plan``);
    #: the sweep stops early if the disk, measured again after each eviction,
    #: is already a GiB above the floor.
    floor: list[RetentionItemOut] = []
    #: The two graces the reasons are written in (FR-T1, FR-T5): hours after
    #: watching, G days after a drop or for a file nobody ever wanted.
    watched_grace_hours: int = 6
    grace_days: int = 7


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
    now = datetime.now(UTC)
    plan = await retention_plan(session, settings, now=now)
    # The floor half, exactly as the sweep plans it after its normal pass:
    # what that pass and the idle-copy rule free counts as freed already.
    deleting = {target.episode_id for target in plan.deletable}
    idle = await idle_copies(session, settings, now=now, idle=await offline_idle_period(session))
    pending = sum(target.bytes for target in plan.deletable) + sum(
        copy.bytes for copy in idle if copy.episode_id not in deleting
    )
    floor = await floor_plan(
        session, settings, now=now, already=plan.deletable, pending_bytes=pending
    )
    titles = await _titles(session, {t.anime_id for t in (*plan.deletable, *plan.held, *floor)})
    return RetentionPreviewOut(
        dry_run=settings.retention_dry_run,
        bytes=sum(target.bytes for target in plan.deletable),
        episodes=[_item(target, titles) for target in plan.deletable],
        held=[_item(target, titles) for target in plan.held],
        floor=[_item(target, titles) for target in floor],
        watched_grace_hours=await watched_grace_hours(session),
        grace_days=await grace_days(session),
    )


async def _titles(session: SessionDep, anime_ids: set[int]) -> dict[int, str]:
    titles: dict[int, str] = {}
    for anime_id in sorted(anime_ids):
        anime = await session.get(Anime, anime_id)
        titles[anime_id] = preferred_title(anime) if anime is not None else ""
    return titles


def _item(target: Deletable, titles: dict[int, str]) -> RetentionItemOut:
    return RetentionItemOut(
        episode_id=target.episode_id,
        anime_id=target.anime_id,
        anime_title=titles.get(target.anime_id, ""),
        number=target.number,
        state=target.state,
        reason=target.reason,
        bytes=target.bytes,
        rendition_dir=(str(target.targets.rendition_dir) if target.targets.rendition_dir else None),
        source_dir=(str(target.targets.source_dir) if target.targets.source_dir else None),
        torrents=list(target.targets.torrent_hashes),
        torrent_files=len(target.targets.torrent_file_ids),
        hold=target.hold,
    )


class DiskUsageOut(BaseModel):
    """``shutil.disk_usage`` on ``DATA_DIR``, in bytes."""

    total: int
    used: int
    free: int


class RetainedOut(BaseModel):
    """What Arc is holding, split into the two things it holds."""

    #: Torrent payloads, from ``media_files.size``.
    sources: int
    #: HLS output, measured on disk.
    renditions: int
    #: The small offline copies (FR-P6), from ``offline_copies.size``.
    offline_bytes: int = 0
    total: int


class PinnedShowOut(BaseModel):
    """One pinned show on ``GET /api/retention/disk`` (FR-T8)."""

    anime_id: int
    anime_title: str
    #: How many users have pinned it.
    users: int
    #: Retained episodes of the show.
    episodes: int
    #: Source, rendition and offline copy bytes of those episodes.
    bytes: int


class DiskOut(BaseModel):
    """``GET /api/retention/disk`` (FR-T4, FR-D3).

    Two different measurements side by side, deliberately. ``data_dir`` is the
    *filesystem* — what the host says is left, including everything Arc did not
    put there — and ``retained`` is Arc's own share of it. The gap between them
    is the answer to "I deleted 40 GB and the disk is still full".
    """

    data_dir: DiskUsageOut
    retained: RetainedOut
    episodes_retained: int
    #: Every pinned show and what its retained episodes hold (FR-T8), largest
    #: first. Delete after watching skips these; only the floor may evict them.
    pinned: list[PinnedShowOut] = []


def _disk_usage(path: Path) -> DiskUsageOut:
    """:func:`arc.services.storage.disk_usage`, in the shape this GET answers.

    The walk-up rule and the "``DATA_DIR`` may not exist yet" argument live in
    that module now, because acquisition's storage guard (FR-T6) needs the same
    measurement and a service cannot import a router's private helper.
    Unmeasurable stays zeros here rather than an error, because that is what
    this page has always shown and a 500 is not an answer to "how full is the
    disk?".
    """
    usage = disk_usage(path)
    if usage is None:
        return DiskUsageOut(total=0, used=0, free=0)
    return DiskUsageOut(total=usage.total, used=usage.used, free=usage.free)


@router.get(
    "/api/retention/disk",
    response_model=DiskOut,
    summary="Disk usage: the filesystem and Arc's share of it (admin, FR-T4)",
)
async def disk(session: SessionDep, settings: SettingsDep) -> DiskOut:
    """Read-only, and safe to poll.

    The rendition half walks directories, which is why it goes through
    :func:`~arc.services.retention.sweep.retained_usage` — the same function
    the acquisition page's ``retained_bytes`` uses, on a worker thread.
    """
    usage = await retained_usage(session, settings)
    data_dir = await asyncio.to_thread(_disk_usage, settings.data_dir)
    pinned = await pinned_usage(session, settings)
    titles = await _titles(session, {show.anime_id for show in pinned})
    return DiskOut(
        pinned=[
            PinnedShowOut(
                anime_id=show.anime_id,
                anime_title=titles.get(show.anime_id, ""),
                users=show.users,
                episodes=show.episodes,
                bytes=show.bytes,
            )
            for show in pinned
        ],
        data_dir=data_dir,
        retained=RetainedOut(
            sources=usage.sources,
            renditions=usage.renditions,
            offline_bytes=usage.offline_bytes,
            total=usage.total,
        ),
        episodes_retained=usage.episodes,
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
    "DiskOut",
    "DiskUsageOut",
    "PinnedShowOut",
    "RetainedOut",
    "RetentionItemOut",
    "RetentionPreviewOut",
    "router",
]
