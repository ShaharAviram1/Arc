"""Settling a trip episode's copy once nothing is waiting for it (FR-A12, FR-T7).

The body of the :data:`~arc.services.trips.names.OFFLINE_SETTLE` job, queued
by a device's confirmation (``TRIP_SETTLE_DELAY`` later) and by the trip sweep
when a row expires. By then the source of a trip-only episode is long gone
(owner decision 4: :mod:`arc.services.trips.release` deleted it the moment the
copy was made), so what a settle deletes is the **copy** — file and row — and
nothing else: no torrent, no pack file, no rendition.

Every question is asked again here, under a row lock on the episode, because
the job runs an hour after whatever queued it:

1. a ``ready`` episode's copy is an ordinary copy and follows the idle rule
   (``offline_idle_days``, retention's): **kept**; so is a ``preparing``
   episode's, which is on its way to ``ready``;
2. a ``pending`` row of any active trip on the episode — another user's trip,
   or the same one asked "again" — still needs it: **kept** (that row's own
   ending queues the next settle);
3. a confirmation less than :data:`~arc.services.trips.names.
   TRIP_SETTLE_DELAY` old, by anyone, is a second device's hour still
   running: **deferred** to the end of it;
4. an ``offline_encode`` pending or running for the episode: **deferred** by
   :data:`~arc.services.trips.names.TRIP_SETTLE_RETRY`;
5. otherwise the copy is deleted. The ``trip_episodes`` rows are left as
   they are — ``delivered`` and ``expired`` are the history of the trip — and
   an episode still holding landed bytes (a source whose release never ran)
   is handed to :func:`~arc.services.trips.release.release_episode` by a
   ``trip_release`` job, whose own rules decide whether they go.

Nothing here touches a want, a list entry or MyAnimeList.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, OfflineCopy
from arc.services.media.copies import encoding_episode_ids
from arc.services.media.names import offline_path_for
from arc.services.retention.delete import delete_copy
from arc.services.trips.names import (
    TRIP_SETTLE_DELAY,
    TRIP_SETTLE_RETRY,
    enqueue_trip_release,
)
from arc.services.trips.rules import latest_delivery, pending_episode_ids

log = logging.getLogger(__name__)

#: What a settled copy's deletion is logged as.
REASON_SETTLED = "trip copy settled; no device is waiting for it"

#: States with landed bytes a settle hands to the release job.
_LEFTOVER_STATES: frozenset[EpisodeState] = frozenset(
    {EpisodeState.DOWNLOADED, EpisodeState.MATCHED, EpisodeState.FAILED}
)

type SettleOutcome = Literal["copy_deleted", "settled", "kept", "deferred"]


@dataclass(frozen=True, slots=True)
class Settle:
    """What :func:`settle_episode` did, and when to look again if it deferred."""

    episode_id: int
    outcome: SettleOutcome
    why: str
    retry_at: datetime | None = None


def settle_due(latest: datetime | None, *, now: datetime) -> datetime | None:
    """When a settle may delete, given the latest confirmation; ``None`` for now. Pure."""
    if latest is None:
        return None
    due = latest + TRIP_SETTLE_DELAY
    return due if due > now else None


#: On (owner, 2026-10-06; kept a constant so it can be turned off): a copy
#: still being fetched — ``last_served_at`` within :data:`FETCH_QUIET` — is
#: not deleted under a running download, up to :data:`FETCH_CEILING` after the
#: latest confirmation. Pairs with the media route touching a non-ready copy's
#: ``last_served_at`` every five minutes
#: (:data:`~arc.services.media.copies.TRIP_TOUCH_ENABLED`).
SETTLE_WAITS_FOR_FETCH: bool = True
FETCH_QUIET: timedelta = timedelta(minutes=10)
FETCH_CEILING: timedelta = timedelta(hours=6)


def fetch_hold(
    last_served_at: datetime | None, latest: datetime | None, *, now: datetime
) -> datetime | None:
    """When a settle may delete a copy a device is still fetching; ``None`` for now. Pure.

    Nothing while :data:`SETTLE_WAITS_FOR_FETCH` is false. Otherwise: served within
    :data:`FETCH_QUIET` → wait until it has been quiet that long, but never past
    ``latest + FETCH_CEILING`` (a device stuck mid-download does not pin the
    copy for ever; without a confirmation the ceiling counts from ``now``).
    """
    if not SETTLE_WAITS_FOR_FETCH or last_served_at is None:
        return None
    quiet = last_served_at + FETCH_QUIET
    if quiet <= now:
        return None
    ceiling = (latest or now) + FETCH_CEILING
    hold = min(quiet, ceiling)
    return hold if hold > now else None


async def settle_episode(
    session: AsyncSession, settings: Settings, episode_id: int, *, now: datetime
) -> Settle:
    """Apply the module docstring's rules to one episode. Flushed, not committed."""
    episode = await session.scalar(
        select(Episode).where(Episode.id == episode_id).with_for_update()
    )
    if episode is None:
        return Settle(episode_id, "kept", "the episode is gone")
    if episode.state is EpisodeState.READY:
        return Settle(episode_id, "kept", "a ready episode's copy follows the idle rule")
    if episode.state is EpisodeState.PREPARING:
        return Settle(episode_id, "kept", "on its way to ready; its copy becomes an ordinary one")
    if await pending_episode_ids(session, [episode_id]):
        return Settle(episode_id, "kept", "a trip is still waiting on it")
    latest = await latest_delivery(session, episode_id)
    due = settle_due(latest, now=now)
    if due is not None:
        return Settle(episode_id, "deferred", "a device confirmed it within the hour", due)
    if await encoding_episode_ids(session, [episode_id]):
        return Settle(episode_id, "deferred", "an offline encode holds it", now + TRIP_SETTLE_RETRY)

    copy = await session.get(OfflineCopy, episode_id)
    hold = fetch_hold(copy.last_served_at if copy is not None else None, latest, now=now)
    if hold is not None:
        return Settle(episode_id, "deferred", "a device is still fetching it", hold)
    outcome: SettleOutcome = "settled"
    if copy is not None or os.path.lexists(offline_path_for(settings, episode_id)):
        if not await delete_copy(session, settings, episode_id, reason=REASON_SETTLED):
            # An encode was queued between the check above and the deleter's.
            return Settle(
                episode_id, "deferred", "an offline encode holds it", now + TRIP_SETTLE_RETRY
            )
        outcome = "copy_deleted"
    if episode.state in _LEFTOVER_STATES:
        await enqueue_trip_release(session, episode_id)
    await session.flush()
    why = REASON_SETTLED if outcome == "copy_deleted" else "no copy left to delete"
    return Settle(episode_id, outcome, why)


__all__ = [
    "FETCH_CEILING",
    "FETCH_QUIET",
    "REASON_SETTLED",
    "SETTLE_WAITS_FOR_FETCH",
    "Settle",
    "SettleOutcome",
    "fetch_hold",
    "settle_due",
    "settle_episode",
]
