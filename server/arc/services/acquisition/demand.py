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
4. And (M20 T6, FR-P8), for a downloading **single**, queues
   ``qbit_sequential`` (sequential download, first and last pieces first, top
   of the queue) and the live remux, which decides on the file itself whether
   it may be played early (MKV, the pre-match gate, copyable, a duration).
   Nothing here asks the matcher or the catalogue: this runs under the want
   lock, in a request. The answer is the live remux's own when it has given
   one — a refusal says the episode "will play when its download finishes" —
   and optimistic otherwise. A live remux that is running or has finished is
   not queued again (a restart would only redo it). A demand on an episode
   that is not downloading yet gets its live remux from ``search_release``
   once it is.

Refused while acquisition is paused or held for disk space (FR-T6) — but only
for an episode that still needs fetching. One already downloading or further
along is not held up by either, so its demand is accepted.

Nothing here touches a list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, JobStatus, Torrent, TorrentKind, Want
from arc.services.acquisition.names import (
    DEMAND_PRIORITY,
    QBIT_SEQUENTIAL,
    QBIT_SEQUENTIAL_PRIORITY,
    QBIT_TOP,
    QBIT_TOP_PRIORITY,
    SEARCH_RELEASE,
    enqueue_compute_wants,
    search_dedupe_key,
    sequential_dedupe_key,
    top_dedupe_key,
)
from arc.services.acquisition.qbit import DECIDED_STATES, QBIT_MISSING
from arc.services.acquisition.rules import is_paused, is_storage_held
from arc.services.acquisition.wants import STARTABLE, coverable, lock_wants, start_search
from arc.services.jobs.queue import enqueue, find_active
from arc.services.media.names import (
    STAGE_LIVE_DONE,
    STAGE_LIVE_REFUSED,
    enqueue_remux_live,
    latest_live_job,
)

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
#: Said when a downloading episode cannot be played before its download ends
#: (a batch, an MP4, a release name that does not surely name this episode).
PLAYS_WHEN_DOWNLOADED_MESSAGE = "This episode will play when its download finishes."


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
    #: Whether a live remux was queued: the episode can be played while it
    #: downloads (FR-P8). False with :attr:`live_note` set when it cannot.
    live: bool = False
    #: Why not, for a person ("will play when its download finishes").
    live_note: str | None = None

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
    live, live_note = False, None
    if episode.state is EpisodeState.DOWNLOADING:
        live, live_note = await _start_live(session, episode, settings)
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
            "live": live,
            "live_note": live_note,
        },
    )
    return DemandOutcome(
        episode=episode,
        want=want,
        started=started,
        topped=topped,
        live=live,
        live_note=live_note,
    )


async def _start_live(
    session: AsyncSession, episode: Episode, settings: Settings | None
) -> tuple[bool, str | None]:
    """Make a downloading single playable while it downloads, if it may be (FR-P8).

    Only a single (a batch's other files are other episodes). ``(live, note)``:
    the live remux's refusal for this download when it has already given one,
    else an optimistic ``(True, None)``.
    """
    torrent = await session.scalar(
        select(Torrent)
        .where(
            Torrent.episode_id == episode.id,
            Torrent.kind == TorrentKind.SINGLE,
            or_(
                Torrent.qbit_state.is_(None),
                Torrent.qbit_state.not_in(sorted(DECIDED_STATES | {QBIT_MISSING})),
            ),
        )
        .order_by(Torrent.id.desc())
        .limit(1)
    )
    if torrent is None:
        return False, PLAYS_WHEN_DOWNLOADED_MESSAGE
    await enqueue(
        session,
        QBIT_SEQUENTIAL,
        {"episode_id": episode.id},
        priority=QBIT_SEQUENTIAL_PRIORITY,
        dedupe_key=sequential_dedupe_key(episode.id),
    )
    latest = await latest_live_job(session, episode.id)
    if latest is not None and latest.created_at >= torrent.added_at:
        stage = str((latest.payload or {}).get("stage") or "")
        if latest.status is JobStatus.RUNNING:
            return True, None
        if latest.status is JobStatus.DONE and stage == STAGE_LIVE_DONE:
            return True, None
        if latest.status is JobStatus.DONE and stage == STAGE_LIVE_REFUSED:
            log.info(
                "a demanded download will not be played before it finishes",
                extra={"episode_id": episode.id, "reason": latest.payload.get("reason")},
            )
            return False, PLAYS_WHEN_DOWNLOADED_MESSAGE
    await enqueue_remux_live(session, episode.id)
    return True, None


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
    "PLAYS_WHEN_DOWNLOADED_MESSAGE",
    "STORAGE_HELD",
    "DemandOutcome",
    "DemandRefused",
    "demand",
]
