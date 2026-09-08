"""The retention job types, importable without the handlers (FR-T1, FR-T4).

The same split as :mod:`arc.services.acquisition.names` and
:mod:`arc.services.media.names`, for the same reason: the admin API needs to
*name* the sweep and the manual delete, the worker needs to *run* them, and
importing the handlers from a request path would drag the qBittorrent client
into it.

**Both types sort behind everything else.** Retention is the only job in Arc
whose work nobody is waiting for: deleting a file an hour later than planned
costs a gigabyte of disk for an hour, while sitting in front of a transcode or
a MAL write costs somebody their evening. 200 is twice the default and well
behind acquisition's own 120/150.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Job
from arc.services.jobs.queue import enqueue

#: Delete what the retention rules say may go (FR-T1, FR-T2's leftovers).
#: Queue-wide work, deduplicated on the type itself: two sweeps at once would
#: walk the same episodes and reach the same answer, and the second would find
#: the first one's directories already gone.
RETENTION_SWEEP = "retention_sweep"

#: Delete one episode's files now, whatever the grace period says (FR-T4).
#: Per episode, so two manual deletions do not deduplicate onto each other.
DELETE_EPISODE_FILES = "delete_episode_files"

#: Behind the default, and behind acquisition (see the module docstring).
RETENTION_PRIORITY = 200


def delete_files_dedupe_key(episode_id: int) -> str:
    """One queued manual deletion per episode, however often the button is hit."""
    return f"{DELETE_EPISODE_FILES}:{episode_id}"


async def enqueue_retention_sweep(session: AsyncSession) -> Job:
    """Queue a sweep, deduplicated on the job type.

    Flushed, not committed: the caller's transaction owns it, like every other
    enqueue in Arc.
    """
    return await enqueue(
        session, RETENTION_SWEEP, priority=RETENTION_PRIORITY, dedupe_key=RETENTION_SWEEP
    )


async def enqueue_delete_files(session: AsyncSession, episode_id: int) -> Job:
    """Queue a manual deletion of one episode's files (FR-T4)."""
    return await enqueue(
        session,
        DELETE_EPISODE_FILES,
        {"episode_id": episode_id},
        priority=RETENTION_PRIORITY,
        dedupe_key=delete_files_dedupe_key(episode_id),
    )


__all__ = [
    "DELETE_EPISODE_FILES",
    "RETENTION_PRIORITY",
    "RETENTION_SWEEP",
    "delete_files_dedupe_key",
    "enqueue_delete_files",
    "enqueue_retention_sweep",
]
