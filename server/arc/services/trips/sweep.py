"""The hourly trip sweep: expiry and the end of a trip (FR-A12, FR-T7).

The body of the :data:`~arc.services.trips.names.TRIP_SWEEP` job. Three
passes, each a question about rows rather than files — the deletions are the
settle's (:mod:`arc.services.trips.settle`):

1. **Expiry.** A ``pending`` row of an active trip expires when its copy has
   waited ``trip_copy_days`` (14) for the device since ``available_at``, or
   — no copy ever made (``available_at`` null) — when its trip passed
   ``deadline_at``. The row goes ``expired`` and an ``offline_settle`` is
   queued at once: an expired row's copy may no longer be fetched (the media
   route serves only ``pending`` and ``delivered`` rows), so there is no hour
   to wait for — a download still running at that moment ends with a 404, by
   design.
2. **The end of a trip.** An active trip with no ``pending`` row left ends
   once its ``deadline_at`` has passed (2026-10-30: until then it holds its
   delivered copies, :data:`~arc.services.trips.names.FINISH_ON_LAST_CONFIRM`
   off): ``finished`` when every row was delivered (released or not), else
   ``expired`` (:func:`ending_state`); ``ended_at`` is stamped. With the
   switch on, it ends as soon as nothing is pending, ``finished`` when at
   least one episode reached a device (``delivered_at`` set). Its rows stay as
   they are, as history. The trips are locked first and each one's rows read
   after, so an "again" committed meanwhile keeps its trip active.
3. **The safety net.** A copy of an episode that is not ``ready`` (nor
   ``preparing``), that no trip is waiting on and that no active trip's
   device holds unwatched gets an ``offline_settle`` (deduplicated). That is
   how a held copy goes once its user has watched the episode (within the
   hour) and how a trip ended in pass 2 lets its copies go (owner,
   2026-10-30). That is what a lost settle job, a cancelled trip's
   leftover, a deleted user's trip or a confirmation that never arrived comes
   down to; the settle re-checks
   everything and defers itself while a confirmation is under an hour old.

The idle rule for the copies of ``ready`` episodes is retention's
(:func:`~arc.services.retention.sweep.idle_copies`) and is not repeated here.
A reconciliation is queued whenever a row or a trip changed, so the trip
wants it held end in minutes rather than at the next tick. Nothing here
touches a list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    Episode,
    EpisodeState,
    OfflineCopy,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
)
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.trips import names
from arc.services.trips.names import enqueue_offline_settle
from arc.services.trips.rules import held_episode_ids, pending_episode_ids, trip_copy_days

log = logging.getLogger(__name__)


def expiry_due(
    *,
    available_at: datetime | None,
    deadline_at: datetime,
    copy_days: int,
    now: datetime,
) -> bool:
    """Whether a ``pending`` trip row has waited long enough to expire. Pure.

    ``trip_copy_days`` from the moment its copy was first ready; with no copy
    yet, the trip's own ``deadline_at`` (``created_at + trip_copy_days``).
    """
    if available_at is not None:
        return available_at + timedelta(days=copy_days) <= now
    return deadline_at <= now


def ending_state(
    *, any_delivered: bool, all_delivered: bool, finish_on_last_confirm: bool
) -> TripState:
    """How a trip with nothing pending ends. Pure.

    Since 2026-10-30 (``finish_on_last_confirm`` off): ``finished`` when every
    row is delivered, else ``expired``. With it on, as before: ``finished``
    when some episode reached a device, ``expired`` when none did.
    """
    done = any_delivered if finish_on_last_confirm else all_delivered
    return TripState.FINISHED if done else TripState.EXPIRED


def may_end(*, deadline_at: datetime, now: datetime, finish_on_last_confirm: bool) -> bool:
    """Whether a trip with nothing pending may end now. Pure.

    At once with the switch on; otherwise only once its deadline has passed.
    """
    return finish_on_last_confirm or deadline_at <= now


@dataclass(slots=True)
class TripSweep:
    """What one sweep did. Logged, and returned for the tests."""

    expired_rows: list[tuple[int, int]] = field(default_factory=list)
    ended: dict[int, TripState] = field(default_factory=dict)
    settles: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.expired_rows or self.ended)


async def _expire(session: AsyncSession, sweep: TripSweep, *, now: datetime) -> None:
    copy_days = await trip_copy_days(session)
    rows = (
        await session.execute(
            select(TripEpisode, Trip.deadline_at)
            .join(Trip, Trip.id == TripEpisode.trip_id)
            .where(Trip.state == TripState.ACTIVE, TripEpisode.state == TripEpisodeState.PENDING)
            .order_by(TripEpisode.trip_id, TripEpisode.episode_id)
            # The trip **and** its rows: a cancel, a confirmation, a release
            # and an "again" all take the trip ``FOR UPDATE`` and then the row,
            # so a confirmation committing while this waits is re-read here
            # (the row is no longer pending) rather than overwritten.
            .with_for_update(of=(Trip, TripEpisode))
        )
    ).all()
    for row, deadline_at in rows:
        if expiry_due(
            available_at=row.available_at, deadline_at=deadline_at, copy_days=copy_days, now=now
        ):
            row.state = TripEpisodeState.EXPIRED
            sweep.expired_rows.append((row.trip_id, row.episode_id))
    await session.flush()


async def end_if_nothing_pending(
    session: AsyncSession, trip: Trip, *, now: datetime, any_time: bool = False
) -> TripState | None:
    """End an active trip, already locked ``FOR UPDATE``, if no row is pending.

    Asked in a statement of its own **after** the lock is held, so an "again"
    that re-pended the last row and committed while this waited is seen. Rows
    are left as they are. Returns the new state, or ``None`` when it stays.
    Before its deadline only with the switch on (:func:`may_end`) or
    ``any_time`` (the next trip on the show is starting).
    """
    if trip.state is not TripState.ACTIVE:
        return None
    switch = names.FINISH_ON_LAST_CONFIRM
    if not any_time and not may_end(
        deadline_at=trip.deadline_at, now=now, finish_on_last_confirm=switch
    ):
        return None
    pending = await session.scalar(
        select(TripEpisode.episode_id)
        .where(TripEpisode.trip_id == trip.id, TripEpisode.state == TripEpisodeState.PENDING)
        .limit(1)
    )
    if pending is not None:
        return None
    delivered = await session.scalar(
        select(TripEpisode.episode_id)
        .where(TripEpisode.trip_id == trip.id, TripEpisode.delivered_at.is_not(None))
        .limit(1)
    )
    undelivered = await session.scalar(
        select(TripEpisode.episode_id)
        .where(
            TripEpisode.trip_id == trip.id,
            TripEpisode.state != TripEpisodeState.DELIVERED,
        )
        .limit(1)
    )
    trip.state = ending_state(
        any_delivered=delivered is not None,
        all_delivered=undelivered is None,
        finish_on_last_confirm=switch,
    )
    trip.ended_at = now
    await session.flush()
    return trip.state


async def _end_trips(session: AsyncSession, sweep: TripSweep, *, now: datetime) -> None:
    # Lock every active trip first, then look at each one's rows: a snapshot
    # "no pending rows" read alongside the lock could miss an "again" that
    # committed while the lock was awaited.
    trips = (
        await session.scalars(
            select(Trip)
            .where(Trip.state == TripState.ACTIVE)
            .order_by(Trip.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    for trip in trips:
        ended = await end_if_nothing_pending(session, trip, now=now)
        if ended is not None:
            sweep.ended[trip.id] = ended


async def _orphan_copies(session: AsyncSession) -> list[int]:
    """Copies of non-ready episodes that no trip is waiting on or holds unwatched.

    Whether or not a trip row still exists: a deleted user's trips go with
    them (CASCADE) and their copies would otherwise stay on the disk for
    ever. A ``preparing`` episode is left out — it is on its way to ``ready``,
    and its copy becomes an ordinary one. The settle re-checks everything.
    """
    candidates = list(
        (
            await session.scalars(
                select(OfflineCopy.episode_id)
                .join(Episode, Episode.id == OfflineCopy.episode_id)
                .where(Episode.state.notin_((EpisodeState.READY, EpisodeState.PREPARING)))
                .order_by(OfflineCopy.episode_id)
            )
        ).all()
    )
    waiting = await pending_episode_ids(session, candidates)
    waiting |= await held_episode_ids(session, candidates)
    return [episode_id for episode_id in candidates if episode_id not in waiting]


async def sweep_trips(session: AsyncSession, *, now: datetime) -> TripSweep:
    """Apply the module docstring's three passes. Flushed, not committed."""
    sweep = TripSweep()
    await _expire(session, sweep, now=now)
    await _end_trips(session, sweep, now=now)

    settle = {episode_id for _, episode_id in sweep.expired_rows}
    settle.update(await _orphan_copies(session))
    for episode_id in sorted(settle):
        await enqueue_offline_settle(session, episode_id)
    sweep.settles = sorted(settle)
    if sweep.changed:
        await enqueue_compute_wants(session)
    await session.flush()
    if sweep.changed or sweep.settles:
        log.info(
            "trip sweep",
            extra={
                "expired_rows": len(sweep.expired_rows),
                "ended": {str(trip_id): state.value for trip_id, state in sweep.ended.items()},
                "settles": sweep.settles,
            },
        )
    return sweep


__all__ = [
    "TripSweep",
    "end_if_nothing_pending",
    "ending_state",
    "expiry_due",
    "may_end",
    "sweep_trips",
]
