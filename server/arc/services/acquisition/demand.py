"""Play on demand: pressing play on an episode that is not ready yet (FR-A14, M20).

M20 makes Arc a cache in front of the indexer rather than a collection (owner,
2026-10-13): one episode ready per watcher, one more at the halfway mark, and
nothing fetched that a device of the user already holds. The price is that an
episode the user wants *now* may not be on the server — they are on the Mac and
the copy is on the iPad, or they skipped the next one and want the one after.
Pressing play is then a request to fetch it, at the top of the queue.

**Only what the user's window covers** (owner Q5, 2026-10-13). An episode may
be demanded when it is in the user's window *before* the device skip and the
slot cap — window ∪ halfway prefetch ∪ device-held — or when they already have
a live want on it (a sample, a trip episode, an earlier demand). That is
:func:`~arc.services.acquisition.wants.coverable`, the reconciler's own
computation narrowed to one user, so the button and the rule cannot disagree.
Anything further ahead is refused with a sentence: a fetch outside the window
would be a new kind of acquisition, and CLAUDE.md's non-negotiable allows none.

What a demand does, in one transaction:

1. Upserts the (user, episode) want with ``demanded_at = now`` and clears any
   drop. The reconciler then keeps it wanted even though a device holds it and
   even though the show is waiting for a slot, and it never occupies a slot
   (FR-A10: "a demanded episode never counts and is never refused, like a
   sample").
2. Starts the search through the reconciler's own
   :func:`~arc.services.acquisition.wants.start_search`, at
   :data:`DEMAND_PRIORITY` and with ``retry_now`` (a person asking is entitled
   to one more look today, as with a sample). A search already queued behind
   the window's own priority is moved up to it.
3. If the episode is already downloading, queues ``qbit_top`` so the torrent
   goes to the top of the client's queue.

Refused while acquisition is paused or held for disk space (FR-T6) — but only
for an episode that still needs fetching. One already downloading or further
along is not held up by either, so its demand is accepted.

Nothing here touches a list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, JobStatus, Want
from arc.services.acquisition.names import (
    DEMAND_PRIORITY,
    QBIT_TOP,
    QBIT_TOP_PRIORITY,
    SEARCH_RELEASE,
    enqueue_compute_wants,
    search_dedupe_key,
    top_dedupe_key,
)
from arc.services.acquisition.rules import is_paused, is_storage_held
from arc.services.acquisition.wants import STARTABLE, coverable, lock_wants, start_search
from arc.services.jobs.queue import enqueue, find_active

log = logging.getLogger(__name__)

#: Episode states that still need acquisition to do something: a pause or a
#: storage hold would leave a demand for one of these waiting for nothing.
NEEDS_FETCH: frozenset[EpisodeState] = STARTABLE | frozenset(
    {EpisodeState.WANTED, EpisodeState.SEARCHING}
)

NOT_IN_WINDOW = "not_in_window"
NOT_IN_WINDOW_MESSAGE = (
    "Arc fetches only the next episode of a show you are watching or have planned, "
    "so this one cannot be fetched on demand."
)
ACQUISITION_PAUSED = "acquisition_paused"
ACQUISITION_PAUSED_MESSAGE = (
    "Fetching is paused on this server, so this episode cannot be fetched right now."
)
STORAGE_HELD = "storage_held"
STORAGE_HELD_MESSAGE = "The server is low on disk space, so it is not fetching anything right now."


class DemandRefused(Exception):
    """A demand Arc will not act on, with a code and a sentence for a person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DemandOutcome:
    """What one demand did."""

    episode: Episode
    #: The demanded want; ``None`` when the episode was already ready.
    want: Want | None
    #: Whether the episode moved into ``wanted`` (a search was started).
    started: bool
    #: Whether a ``qbit_top`` was queued (the episode was downloading).
    topped: bool

    @property
    def state(self) -> EpisodeState:
        return self.episode.state


async def demand(
    session: AsyncSession,
    *,
    user_id: int,
    episode: Episode,
    now: datetime,
    settings: Settings | None = None,
) -> DemandOutcome:
    """Want this episode now, at the top of the queue (FR-A14). Flushes; the caller commits.

    Raises :class:`DemandRefused` with :data:`NOT_IN_WINDOW` when the episode is
    not one the user's window covers, and with :data:`ACQUISITION_PAUSED` or
    :data:`STORAGE_HELD` when it still needs fetching and acquisition cannot
    fetch. ``settings`` is what makes the disk measurable; without one there
    is no measurement, which is never held (the reconciler's rule).
    """
    if episode.state is EpisodeState.READY:
        # Nothing to fetch: the player can open it. No stamp, no reconciliation.
        return DemandOutcome(episode=episode, want=None, started=False, topped=False)
    # One at a time with the reconciler (and with another demand): see
    # :func:`~arc.services.acquisition.wants.lock_wants`.
    await lock_wants(session)
    if episode.id not in await coverable(session, user_id, now=now):
        raise DemandRefused(NOT_IN_WINDOW, NOT_IN_WINDOW_MESSAGE)
    if episode.state in NEEDS_FETCH:
        if await is_paused(session):
            raise DemandRefused(ACQUISITION_PAUSED, ACQUISITION_PAUSED_MESSAGE)
        if settings is not None and await is_storage_held(session, settings):
            raise DemandRefused(STORAGE_HELD, STORAGE_HELD_MESSAGE)

    # An upsert, so two presses arriving together cannot collide on the key.
    statement = pg_insert(Want).values(user_id=user_id, episode_id=episode.id, demanded_at=now)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Want.user_id, Want.episode_id],
            set_={"demanded_at": now, "dropped_at": None, "drop_reason": None},
        )
    )
    want = await session.get(Want, (user_id, episode.id), populate_existing=True)

    started, _ = await start_search(
        session, episode, now=now, retry_now=True, priority=DEMAND_PRIORITY
    )
    await _raise_queued_search(session, episode.id)

    topped = False
    if episode.state is EpisodeState.DOWNLOADING:
        await enqueue(
            session,
            QBIT_TOP,
            {"episode_id": episode.id},
            priority=QBIT_TOP_PRIORITY,
            dedupe_key=top_dedupe_key(episode.id),
        )
        topped = True
    # The reconciler re-reads the flags (a trip-only episode stops being one)
    # and keeps the demanded row; deduplicated on the type.
    await enqueue_compute_wants(session)
    await session.flush()
    log.info(
        "episode demanded",
        extra={
            "user_id": user_id,
            "episode_id": episode.id,
            "state": episode.state.value,
            "started": started,
            "topped": topped,
        },
    )
    return DemandOutcome(episode=episode, want=want, started=started, topped=topped)


async def _raise_queued_search(session: AsyncSession, episode_id: int) -> None:
    """Move a search still waiting in the queue up to :data:`DEMAND_PRIORITY`.

    ``enqueue``'s dedupe hands back an existing row as it stands, so a search
    the reconciler queued at the window's 150 would otherwise keep its place
    behind every other search. Only a ``pending`` row: one already running
    needs no place in the queue.
    """
    job = await find_active(session, SEARCH_RELEASE, search_dedupe_key(episode_id))
    if job is not None and job.status is JobStatus.PENDING and job.priority > DEMAND_PRIORITY:
        job.priority = DEMAND_PRIORITY
        await session.flush()


__all__ = [
    "ACQUISITION_PAUSED",
    "DEMAND_PRIORITY",
    "NOT_IN_WINDOW",
    "STORAGE_HELD",
    "DemandOutcome",
    "DemandRefused",
    "demand",
]
