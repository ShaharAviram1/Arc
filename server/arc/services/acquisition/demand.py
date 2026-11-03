"""Play on demand: pressing play on an episode that is not ready yet (FR-A14, M20, M21).

M20 made Arc a cache in front of the indexer rather than a collection (owner,
2026-10-13): one episode ready per watcher, one more at the halfway mark, and
nothing fetched that a device of the user already holds. Pressing play on an
episode that is not on the server is a request to fetch it, at the top of the
queue.

**Any aired episode of any show** (M21, owner 2026-11-03: "every episode on
every show is playable when clicked on"). M20's "only what the window covers"
is retired. :func:`demand` takes the want lock and then, in this order:

1. a ``ready`` episode writes nothing and counts for nothing (before the lock);
2. an episode that has not aired is refused :data:`NOT_AIRED`;
3. an episode the caller already has an **open** demand on is restamped and
   skips the cap;
4. a new demand beyond ``demand_max_open`` open ones (0 = no cap) is refused
   :data:`DEMAND_CAP`, with the open ones listed (:func:`open_demands`) — the
   count is read under the lock, so two clicks cannot both take the last place;
5. while paused or held for disk space, an episode that still needs fetching is
   refused (:data:`ACQUISITION_PAUSED`, :data:`STORAGE_HELD`).

A demand is **open** while its row is live and the user has not finished the
episode since pressing play (``watch_progress.finished_at >= demanded_at``).
The reconciler wants an open demand unconditionally and ends it when it is
finished, stale (D days untouched) or withdrawn
(:func:`~arc.services.acquisition.wants._demanded_wants`); :func:`withdraw` is
the last of those.

What an accepted demand does, in one transaction:

1. Upserts the (user, episode) want with ``demanded_at = now`` and clears any
   drop. A row that was a trip's or a sample's becomes a plain demand
   (``trip``/``sample`` cleared), so the trip's and the sample's own cancels —
   which skip such rows as theirs — count it as the live want it is (FR-A14:
   an open demand is never cancelled). It never occupies a slot (FR-A10).
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
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    JobStatus,
    Torrent,
    TorrentKind,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    Want,
    WatchProgress,
)
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
from arc.services.acquisition.rules import demand_max_open, is_paused, is_storage_held
from arc.services.acquisition.wants import (
    REASON_WITHDRAWN,
    STARTABLE,
    cancel_if_unwanted,
    enqueue_cancel,
    lock_wants,
    release_if_unwanted,
    start_search,
    user_plan,
)
from arc.services.catalog.cache import preferred_title
from arc.services.jobs.queue import enqueue, find_active
from arc.services.media.names import (
    STAGE_LIVE_DONE,
    STAGE_LIVE_REFUSED,
    enqueue_remux_live,
    latest_live_job,
)
from arc.services.playback.next_season import has_aired

log = logging.getLogger(__name__)

#: Episode states that still need acquisition to do something: a pause or a
#: storage hold would leave a demand for one of these waiting for nothing.
NEEDS_FETCH: frozenset[EpisodeState] = STARTABLE | frozenset(
    {EpisodeState.WANTED, EpisodeState.SEARCHING}
)

NOT_AIRED = "not_aired"
NOT_AIRED_MESSAGE = "This episode hasn't aired yet."
DEMAND_CAP = "demand_cap"
ACQUISITION_PAUSED = "acquisition_paused"
ACQUISITION_PAUSED_MESSAGE = (
    "Fetching is paused on this server, so this episode cannot be fetched right now."
)
STORAGE_HELD = "storage_held"
STORAGE_HELD_MESSAGE = "The server is low on disk space, so it is not fetching anything right now."
#: Said when a downloading episode cannot be played before its download ends
#: (a batch, an MP4, a release name that does not surely name this episode).
PLAYS_WHEN_DOWNLOADED_MESSAGE = "This episode will play when its download finishes."


def demand_cap_message(cap: int) -> str:
    """The cap refusal's sentence (FR-A14): "You already have 3 episodes on request. …".

    Not "fetching": the count includes demands whose episode is ready but not
    yet watched, and ones Arc has found no release for.
    """
    episodes = "episode" if cap == 1 else "episodes"
    return f"You already have {cap} {episodes} on request. Finish or cancel one first."


@dataclass(frozen=True, slots=True)
class OpenDemand:
    """One of the caller's open demands, as the cap refusal lists it."""

    episode_id: int
    anime_id: int
    #: The show's title (``preferred_title``), for the link's text.
    title: str
    number: int


class DemandRefused(Exception):
    """A demand Arc will not act on, with a code and a sentence for a person.

    ``open`` is the caller's open demands, set only on :data:`DEMAND_CAP`.
    """

    def __init__(self, code: str, message: str, open: tuple[OpenDemand, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.open = open


async def open_demands(session: AsyncSession, user_id: int) -> list[OpenDemand]:
    """The user's open demands, oldest press first (FR-A14, M21). Read-only.

    Open: a live want (``dropped_at`` null) with ``demanded_at`` set and no
    ``watch_progress.finished_at`` at or after it. Read under the want lock by
    :func:`demand`, so the count the cap is checked against is the one the
    write lands on.
    """
    rows = await session.execute(
        select(Want.episode_id, Episode.anime_id, Episode.number, Anime)
        .join(Episode, Episode.id == Want.episode_id)
        .join(Anime, Anime.id == Episode.anime_id)
        .outerjoin(
            WatchProgress,
            (WatchProgress.user_id == Want.user_id) & (WatchProgress.episode_id == Want.episode_id),
        )
        .where(
            Want.user_id == user_id,
            Want.dropped_at.is_(None),
            Want.demanded_at.is_not(None),
            or_(
                WatchProgress.finished_at.is_(None),
                WatchProgress.finished_at < Want.demanded_at,
            ),
        )
        .order_by(Want.demanded_at, Want.episode_id)
    )
    return [
        OpenDemand(
            episode_id=episode_id, anime_id=anime_id, title=preferred_title(anime), number=number
        )
        for episode_id, anime_id, number, anime in rows.all()
    ]


async def _aired(session: AsyncSession, episode: Episode, *, now: datetime) -> bool:
    """Whether ``episode`` has aired, by the catalogue's rule (an announced show never has)."""
    anime = await session.get(Anime, episode.anime_id)
    if anime is None:
        return False
    rows = list(
        (
            await session.scalars(
                select(Episode).where(Episode.anime_id == anime.id).order_by(Episode.number)
            )
        ).all()
    )
    return has_aired(anime, rows, episode, now=now)


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

    Raises :class:`DemandRefused` with :data:`NOT_AIRED` for an episode that
    has not aired, :data:`DEMAND_CAP` (with ``open``) for a new demand beyond
    ``demand_max_open``, and :data:`ACQUISITION_PAUSED` or
    :data:`STORAGE_HELD` when it still needs fetching and acquisition cannot
    fetch — in that order (the module docstring). ``settings`` is what makes
    the disk measurable; without one there is no measurement, which is never
    held (the reconciler's rule).
    """
    if episode.state is EpisodeState.READY:
        # Nothing to fetch: the player can open it. No stamp, no reconciliation.
        return DemandOutcome(episode=episode, want=None, started=False, topped=False)
    # One at a time with the reconciler (and with another demand): see
    # :func:`~arc.services.acquisition.wants.lock_wants`.
    await lock_wants(session)
    if not await _aired(session, episode, now=now):
        raise DemandRefused(NOT_AIRED, NOT_AIRED_MESSAGE)
    opened = await open_demands(session, user_id)
    if all(found.episode_id != episode.id for found in opened):
        # A new demand: the cap applies. Pressing play again on an open one
        # only restamps it and never counts twice.
        cap = await demand_max_open(session)
        if cap and len(opened) >= cap:
            raise DemandRefused(DEMAND_CAP, demand_cap_message(cap), tuple(opened))
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
            set_={
                "demanded_at": now,
                "dropped_at": None,
                "drop_reason": None,
                # A demand, whatever the row was before (the module docstring).
                "trip": False,
                "sample": False,
            },
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


async def withdraw(session: AsyncSession, *, user_id: int, episode_id: int, now: datetime) -> bool:
    """Withdraw the caller's demand on an episode (FR-A14, M21). Flushes; the caller commits.

    ``demanded_at`` goes back to null. Then one question, asked of the caller's
    own reconciliation (their plan, narrowed to them as :func:`slot_view` does
    it, plus an active trip of theirs waiting on the episode): **does anything
    the user follows still want this episode now?**

    * Yes — an admitted window, walk or pass, a halfway prefetch, a pending
      trip episode: the row stays live and becomes an ordinary want in place.
      Nothing is released or cancelled, so a download the window wants runs
      on.
    * No — an unfollowed show, an episode outside every window, a show the
      slot cap holds: the row is **shelved** at once
      (:data:`~arc.services.acquisition.wants.REASON_WITHDRAWN`) and the
      episode is released or its download cancelled through the reconciler's
      own :func:`~arc.services.acquisition.wants.release_if_unwanted` /
      :func:`~arc.services.acquisition.wants.cancel_if_unwanted`, which keep it
      while another user or a trip still wants it. On a held show it stays
      shelved until the show is admitted and its window revives it (M20's
      held-show rule), so a withdrawn demand never holds a slot. This happens
      even while acquisition is paused: the user asked, and it frees space.

    A reconciliation is queued either way. ``True`` when there was a demand to
    withdraw; a second call changes nothing and queues nothing. Taken under
    the want lock like a demand. The plan costs one user's lists — fine for a
    button press.
    """
    await lock_wants(session)
    want = await session.get(Want, (user_id, episode_id), populate_existing=True)
    if want is None or want.demanded_at is None:
        return False
    want.demanded_at = None
    await session.flush()
    on_trip = await session.scalar(
        select(TripEpisode.episode_id)
        .join(Trip, Trip.id == TripEpisode.trip_id)
        .where(
            Trip.user_id == user_id,
            Trip.state == TripState.ACTIVE,
            TripEpisode.episode_id == episode_id,
            TripEpisode.state == TripEpisodeState.PENDING,
        )
        .limit(1)
    )
    followed = on_trip is not None or (
        (user_id, episode_id) in (await user_plan(session, user_id, now=now)).desired
    )
    cancelled = False
    if not followed and want.dropped_at is None:
        want.dropped_at = now
        want.drop_reason = REASON_WITHDRAWN
        await session.flush()
        episode = await session.get(Episode, episode_id)
        if episode is not None and not await release_if_unwanted(session, episode):
            cancelled = await cancel_if_unwanted(session, episode)
            if cancelled:
                await session.flush()
                await enqueue_cancel(session, episode_id)
    await enqueue_compute_wants(session)
    await session.flush()
    log.info(
        "demand withdrawn",
        extra={
            "user_id": user_id,
            "episode_id": episode_id,
            "kept": followed,
            "cancelled": cancelled,
        },
    )
    return True


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
    "DEMAND_CAP",
    "DEMAND_PRIORITY",
    "NOT_AIRED",
    "NOT_AIRED_MESSAGE",
    "PLAYS_WHEN_DOWNLOADED_MESSAGE",
    "STORAGE_HELD",
    "DemandOutcome",
    "DemandRefused",
    "OpenDemand",
    "demand",
    "demand_cap_message",
    "open_demands",
    "withdraw",
]
