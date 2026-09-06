"""The acquisition job type strings, importable without the handlers.

The same split as :mod:`arc.services.catalog.names` and
:mod:`arc.services.library.names`, for the same reason: the API and the list
service need to *name* a job, the worker needs to *run* it, and importing the
handler module from a request path would drag the whole job registry — and the
Nyaa and qBittorrent clients it builds — into the API.

Importing this registers nothing, and — just as importantly — it reaches
nothing. :func:`enqueue_compute_wants` lives here rather than beside the
reconciler it queues for exactly that reason: its callers are
:mod:`arc.services.catalog.lists` and (from M8) the watch handler, and
:mod:`arc.services.acquisition.wants` imports the catalogue's airing rule, so
putting the one-line enqueue there would make the catalogue and acquisition
packages import each other in a cycle that fails on whichever is imported
second.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Job
from arc.services.jobs.queue import enqueue

#: Recompute every user's acquisition window (FR-A1, FR-A2, FR-W4). Scheduled
#: every 15 minutes and enqueued after any list change or watch completion.
#: Deduplicated on the type itself: two of these queued at once would walk the
#: same rows and reach the same answer.
COMPUTE_WANTS = "compute_wants"

#: Find and start a release for one episode (FR-A3, FR-A4, FR-A5). Per
#: episode, so two episodes still search concurrently.
SEARCH_RELEASE = "search_release"

#: Sync download progress with qBittorrent and hand finished files to the
#: library (FR-A5). Queue-wide work, deduplicated on the type.
POLL_QBIT = "poll_qbit"


def search_dedupe_key(episode_id: int) -> str:
    """One queued search per episode, however many people want it (FR-A2)."""
    return f"{SEARCH_RELEASE}:{episode_id}"


async def enqueue_compute_wants(session: AsyncSession) -> Job:
    """Queue a reconciliation, deduplicated on the job type.

    Called from every path that can change the answer — a list change
    (:mod:`arc.services.catalog.lists`), a watch completion (M8), an admin
    pressing the button, the scheduler's fifteen-minute tick — so that none of
    them has to know what a want is. Flushed, not committed: the caller's
    transaction is what makes the list change and its consequence atomic, or
    neither.
    """
    return await enqueue(session, COMPUTE_WANTS, dedupe_key=COMPUTE_WANTS)


__all__ = [
    "COMPUTE_WANTS",
    "POLL_QBIT",
    "SEARCH_RELEASE",
    "enqueue_compute_wants",
    "search_dedupe_key",
]
