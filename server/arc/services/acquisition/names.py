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

# --- Queue priorities (lower runs first; the default is 100) ----------------
#
# Acquisition is the greediest thing Arc does and the least urgent. A single
# MAL link can turn into thirty wants and thirty ``search_release`` jobs, each
# of which holds a worker slot for the length of up to five paced Nyaa queries
# — and with everything at the default priority those thirty sit in front of
# the user's own MyAnimeList write. So the three job types here sort *behind*
# the default, in the order they matter to somebody sitting in front of the
# app: watch what is already downloading, then work out what is wanted, then go
# looking for it.

#: Below the default: the poll is a single request to a service on the same
#: host, and it is the step that turns a finished torrent into a playable
#: episode. Delaying it delays a file that is already on the disk.
POLL_QBIT_PRIORITY = 50

#: Above the default: reconciling the whole wants table is a handful of
#: queries, but nothing is waiting on the answer within the minute.
COMPUTE_WANTS_PRIORITY = 120

#: And behind that. One search is up to five paced Nyaa requests plus a
#: qBittorrent call, so a burst of them is what actually starves the queue.
SEARCH_RELEASE_PRIORITY = 150


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
    return await enqueue(
        session, COMPUTE_WANTS, priority=COMPUTE_WANTS_PRIORITY, dedupe_key=COMPUTE_WANTS
    )


__all__ = [
    "COMPUTE_WANTS",
    "COMPUTE_WANTS_PRIORITY",
    "POLL_QBIT",
    "POLL_QBIT_PRIORITY",
    "SEARCH_RELEASE",
    "SEARCH_RELEASE_PRIORITY",
    "enqueue_compute_wants",
    "search_dedupe_key",
]
