"""What a device tells Arc about a trip episode (FR-A12, M19 T4).

Three facts, each idempotent, each about one ``trip_episodes`` row of the
caller's own trip (anyone else's is :class:`~arc.services.trips.create.
TripNotFound`, so a trip id is never confirmed to exist):

* **delivered** — the device holds the copy, whole (it checked the size
  against the total). A ``pending`` row (or an ``expired`` one of a trip still
  active) goes ``delivered`` with ``delivered_at`` stamped, and an
  ``offline_settle`` is queued ``TRIP_SETTLE_DELAY`` later, long enough for a
  second device of the same user to fetch the same copy. A ``cancelled`` row,
  or an ``expired`` one of an ended trip, only gets ``delivered_at``: no right
  to fetch the copy comes back. A repeat changes nothing. The ETag the device
  sends is compared with the copy's and a mismatch is only logged. When the
  confirmation leaves nothing of the trip pending, the trip ends
  ``finished`` there and then (:data:`FINISH_ON_LAST_CONFIRM`) so the next
  one can start at once.
* **released** — the device deleted its copy. ``released_at`` is stamped on a
  delivered row: trip bookkeeping (the phase, the "again" offer) and nothing
  else. A delivered episode is **not** kept out of the user's window (owner,
  2026-10-06): inside it, it is fetched and prepared for streaming like any
  other episode, whatever the device holds.
* **again** — while the trip is active, a ``delivered`` or ``expired`` row
  goes back to ``pending``. Its ``delivered_at`` and ``released_at`` are
  **cleared**: the row describes the copy the device is about to get, not the
  one it lost (the confirmation is in the log). Its ``available_at`` restarts
  the ``trip_copy_days`` clock now when a copy is still on disk, and is
  cleared otherwise so the copy hook stamps it when the copy is made again.
  The reconciliation queued here does the rest by the paths that already
  exist: a trip-only want returns, and the episode is fetched again, or its
  copy made again, or left alone because the copy is still there.

Nothing here touches a list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    OfflineCopy,
    OfflineCopyState,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
)
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.media.copies import file_matches
from arc.services.media.names import offline_path_for
from arc.services.trips.cancel import drop_queued_copies
from arc.services.trips.create import TRIP_NOT_ACTIVE, TRIP_NOT_FOUND, TripConflict, TripNotFound
from arc.services.trips.names import TRIP_SETTLE_DELAY, enqueue_offline_settle
from arc.services.trips.rules import pending_episode_ids
from arc.services.trips.sweep import end_if_nothing_pending

#: On (owner, 2026-10-06; kept a constant so it can be turned off): end the
#: trip ``finished`` inside the confirmation that leaves nothing pending, so
#: the user can start the next one at once. The hourly sweep stays the safety
#: net for every other ending.
FINISH_ON_LAST_CONFIRM: bool = True

log = logging.getLogger(__name__)

#: The row states ``again`` re-pends.
_AGAIN_FROM: frozenset[TripEpisodeState] = frozenset(
    {TripEpisodeState.DELIVERED, TripEpisodeState.EXPIRED}
)


async def _row(
    session: AsyncSession, user: User, trip_id: int, episode_id: int
) -> tuple[Trip, TripEpisode]:
    """The caller's trip and its row for ``episode_id``, or :class:`TripNotFound`.

    Locks, in the order every trip writer takes them — the trip, its row, the
    episode: the trip ``FOR UPDATE`` (what a cancel and the sweep take, so a
    confirmation, a release or an "again" cannot cross an expiry or a trip
    ending), the ``trip_episodes`` row, and the episode row (what the settle
    and the release hold while they decide to delete a copy).
    """
    trip = await session.scalar(select(Trip).where(Trip.id == trip_id).with_for_update())
    if trip is None or trip.user_id != user.id:
        raise TripNotFound(TRIP_NOT_FOUND)
    row = await session.get(TripEpisode, (trip_id, episode_id), with_for_update=True)
    if row is None:
        raise TripNotFound(TRIP_NOT_FOUND)
    await session.scalar(select(Episode.id).where(Episode.id == episode_id).with_for_update())
    return trip, row


def confirmed_state(state: TripEpisodeState, *, trip_active: bool) -> TripEpisodeState:
    """The row state a confirmation leaves. Pure.

    ``pending`` → ``delivered``; ``expired`` → ``delivered`` only while the trip
    is still active (the device finished as the clock ran out). A ``cancelled``
    row, or an ``expired`` one of a trip that has ended, keeps its state: the
    confirmation stamps ``delivered_at`` (the record that the device has it)
    but restores no right to fetch the copy.
    """
    if state is TripEpisodeState.PENDING:
        return TripEpisodeState.DELIVERED
    if state is TripEpisodeState.EXPIRED and trip_active:
        return TripEpisodeState.DELIVERED
    return state


async def confirm_delivered(
    session: AsyncSession,
    *,
    user: User,
    trip_id: int,
    episode_id: int,
    etag: str | None,
    now: datetime,
) -> TripEpisode:
    """The device holds the copy. Flushed, not committed. Idempotent."""
    trip, row = await _row(session, user, trip_id, episode_id)
    copy = await session.get(OfflineCopy, episode_id)
    if etag is not None and (copy is None or copy.etag != etag):
        log.info(
            "trip copy confirmed with a different ETag; accepted",
            extra={"trip_id": trip.id, "episode_id": episode_id, "user_id": user.id},
        )
    changed = False
    state = confirmed_state(row.state, trip_active=trip.state is TripState.ACTIVE)
    if state is not row.state:
        row.state = state
        changed = True
    if row.delivered_at is None or row.released_at is not None:
        # First confirmation, or held again after the device deleted it: the
        # settle's hour runs from now.
        row.delivered_at = now
        row.released_at = None
        changed = True
    if changed:
        dropped = await _drop_unneeded_encode(session, episode_id)
        await enqueue_offline_settle(session, episode_id, run_after=now + TRIP_SETTLE_DELAY)
        await enqueue_compute_wants(session)
        if FINISH_ON_LAST_CONFIRM:
            await end_if_nothing_pending(session, trip, now=now)
        log.info(
            "trip episode delivered",
            extra={
                "trip_id": trip.id,
                "episode_id": episode_id,
                "user_id": user.id,
                "state": row.state.value,
                "encodes_cancelled": dropped,
            },
        )
    await session.flush()
    return row


async def _drop_unneeded_encode(session: AsyncSession, episode_id: int) -> int:
    """Cancel a queued copy of a delivered episode nothing else waits for.

    The device already holds a copy, so a re-make queued meanwhile (an
    "again" whose copy was being made again) is for nobody — unless the
    episode is ``ready`` (its copy is anyone's) or another trip still waits on
    it. Pending jobs only, as :func:`~arc.services.trips.cancel.
    drop_queued_copies` does: a running encode is not stopped, and the settle
    defers until it has finished, then deletes its result.
    """
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.state is EpisodeState.READY:
        return 0
    if await pending_episode_ids(session, [episode_id]):
        return 0
    return await drop_queued_copies(
        session, [episode_id], reason="a device already holds this trip copy"
    )


async def release_delivered(
    session: AsyncSession, *, user: User, trip_id: int, episode_id: int, now: datetime
) -> TripEpisode:
    """The device deleted its copy. Flushed, not committed. Idempotent."""
    trip, row = await _row(session, user, trip_id, episode_id)
    if row.delivered_at is not None and row.released_at is None:
        row.released_at = now
        log.info(
            "trip episode released by the device",
            extra={"trip_id": trip.id, "episode_id": episode_id, "user_id": user.id},
        )
    await session.flush()
    return row


async def ask_again(
    session: AsyncSession,
    settings: Settings,
    *,
    user: User,
    trip_id: int,
    episode_id: int,
    now: datetime,
) -> Trip:
    """Re-pend a delivered or expired row of an active trip. Flushed, not committed.

    :class:`TripConflict` (``trip_not_active``) once the trip has ended. A
    ``pending`` row is left as it is.
    """
    trip, row = await _row(session, user, trip_id, episode_id)
    if trip.state is not TripState.ACTIVE:
        raise TripConflict(TRIP_NOT_ACTIVE)
    if row.state in _AGAIN_FROM:
        previous = row.state
        copy = await session.get(OfflineCopy, episode_id)
        on_disk = (
            copy is not None
            and copy.state is OfflineCopyState.READY
            and file_matches(offline_path_for(settings, episode_id), copy)
        )
        row.state = TripEpisodeState.PENDING
        row.delivered_at = None
        row.released_at = None
        row.available_at = now if on_disk else None
        await enqueue_compute_wants(session)
        log.info(
            "trip episode asked for again",
            extra={
                "trip_id": trip.id,
                "episode_id": episode_id,
                "user_id": user.id,
                "was": previous.value,
                "copy_on_disk": on_disk,
            },
        )
    await session.flush()
    return trip


__all__ = [
    "FINISH_ON_LAST_CONFIRM",
    "ask_again",
    "confirm_delivered",
    "confirmed_state",
    "release_delivered",
]
