"""The questions about trips that the rest of Arc asks (FR-A12, §5.4e).

A leaf: it imports the models, the trip constants and nothing from
acquisition, media handlers or retention, because the reconciler, the linker,
the transcode sweep and the copy hook all ask these and all of them sit below
the modules that create and cancel trips.

**Trip-only** is the one new idea, and it is a predicate rather than a state.
An episode is trip-only when it has at least one live want and *every* live
want on it is a trip want (``wants.trip``). Such an episode is wanted for a
device and for nothing else, so it is given the small offline copy (FR-P6) and
never an HLS rendition: it stops at ``matched``, its copy is made, and its
source is deleted as soon as the copy is ready (owner, 2026-10-05). A normal
want arriving on it makes it an ordinary episode again — the reconciler's
promotion step transcodes it while the source is still there, and the ordinary
path fetches it again once it is not.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    DEFAULT_SETTINGS,
    Episode,
    Setting,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    Want,
)
from arc.services.trips.names import (
    TRIP_COPY_DAYS_KEY,
    TRIP_MAX_EPISODES_CEILING,
    TRIP_MAX_EPISODES_KEY,
    TRIP_PACK_MIN_SEEDERS_CEILING,
    TRIP_PACK_MIN_SEEDERS_KEY,
    copy_priority,
)

log = logging.getLogger(__name__)

#: Ceiling on ``trip_copy_days`` as read: a year. The panel accepts the same.
MAX_COPY_DAYS = 365


async def _int_setting(session: AsyncSession, key: str, *, low: int, high: int) -> int:
    """One integer out of ``settings``, clamped, falling back to the default.

    Lenient like every other reader: a hand-edited row of the wrong type is
    logged and ignored rather than allowed to stop a trip being made.
    """
    default = int(DEFAULT_SETTINGS[key])
    stored = await session.scalar(select(Setting.value).where(Setting.key == key))
    if stored is None:
        return default
    if isinstance(stored, bool) or not isinstance(stored, int):
        log.warning("setting is not an integer, using the default", extra={"key": key})
        return default
    return min(max(stored, low), high)


async def trip_max_episodes(session: AsyncSession) -> int:
    """The most episodes one trip may take, 1..50 (FR-D2)."""
    return await _int_setting(session, TRIP_MAX_EPISODES_KEY, low=1, high=TRIP_MAX_EPISODES_CEILING)


async def trip_copy_days(session: AsyncSession) -> int:
    """Days a trip's copy waits for the device, at least one (FR-D2)."""
    return await _int_setting(session, TRIP_COPY_DAYS_KEY, low=1, high=MAX_COPY_DAYS)


async def trip_pack_min_seeders(session: AsyncSession) -> int:
    """Fewest listed seeders a pack needs to be preferred for a trip, 1..500 (FR-D2)."""
    return await _int_setting(
        session, TRIP_PACK_MIN_SEEDERS_KEY, low=1, high=TRIP_PACK_MIN_SEEDERS_CEILING
    )


async def trip_only_episode_ids(
    session: AsyncSession, episode_ids: Collection[int] | None = None
) -> set[int]:
    """The episodes (of ``episode_ids``, or all) whose live wants are all trip wants.

    One grouped query. An episode with no live want at all is **not**
    trip-only: a manual drop, or a download whose want went away, is prepared
    for streaming as it always was.
    """
    if episode_ids is not None and not episode_ids:
        return set()
    statement = (
        select(Want.episode_id)
        .where(Want.dropped_at.is_(None))
        .group_by(Want.episode_id)
        .having(func.bool_and(Want.trip))
    )
    if episode_ids is not None:
        statement = statement.where(Want.episode_id.in_(set(episode_ids)))
    return set((await session.scalars(statement)).all())


async def needs_rendition(session: AsyncSession, episode_id: int) -> bool:
    """Whether a ``matched`` episode should be transcoded (FR-P1, FR-A12).

    The one predicate the linker and the transcode sweep ask. False exactly
    when the episode is trip-only: it gets an ``offline_encode`` instead.
    """
    return episode_id not in await trip_only_episode_ids(session, [episode_id])


async def delivered_to(
    session: AsyncSession, user_ids: Collection[int] | None = None
) -> dict[int, set[int]]:
    """``user → episodes a device of theirs holds from a trip`` (FR-A12).

    Delivered (``delivered_at`` set) and not released (``released_at`` null),
    whatever the trip's own state. A new trip passes over these (the device
    has them already). The user's streaming window does **not** (owner,
    2026-10-06): an episode on the iPad may still be watched on the Mac.
    ``delivered_at`` is set by the device's confirmation and ``released_at``
    by its "I deleted it" (:mod:`arc.services.trips.deliver`).
    """
    statement = (
        select(Trip.user_id, TripEpisode.episode_id)
        .join(Trip, Trip.id == TripEpisode.trip_id)
        .where(TripEpisode.delivered_at.is_not(None), TripEpisode.released_at.is_(None))
    )
    if user_ids is not None:
        if not user_ids:
            return {}
        statement = statement.where(Trip.user_id.in_(set(user_ids)))
    found: dict[int, set[int]] = defaultdict(set)
    for user_id, episode_id in (await session.execute(statement)).all():
        found[user_id].add(episode_id)
    return dict(found)


async def pending_episode_ids(
    session: AsyncSession, episode_ids: Collection[int] | None = None
) -> set[int]:
    """Episodes that some active trip is still waiting on (``pending``)."""
    statement = (
        select(TripEpisode.episode_id)
        .join(Trip, Trip.id == TripEpisode.trip_id)
        .where(Trip.state == TripState.ACTIVE, TripEpisode.state == TripEpisodeState.PENDING)
        .distinct()
    )
    if episode_ids is not None:
        if not episode_ids:
            return set()
        statement = statement.where(TripEpisode.episode_id.in_(set(episode_ids)))
    return set((await session.scalars(statement)).all())


async def holds_trip_episode(session: AsyncSession, user_id: int, episode_id: int) -> bool:
    """Whether ``user_id`` holds a ``pending`` or ``delivered`` trip row on the episode.

    The question the media route asks before it serves the copy of an episode
    that is not ``ready`` (FR-P6, FR-A12), and ``/play`` before it answers
    ``offline_only``. ``delivered`` counts so a second device of the same user
    can fetch the copy in the hour before the settle deletes it; ``expired``
    and ``cancelled`` do not. A ``pending`` row only ever sits on an active
    trip (cancel and the sweep end them), so the trip's own state is not read.
    """
    found = await session.scalar(
        select(TripEpisode.trip_id)
        .join(Trip, Trip.id == TripEpisode.trip_id)
        .where(
            Trip.user_id == user_id,
            TripEpisode.episode_id == episode_id,
            TripEpisode.state.in_((TripEpisodeState.PENDING, TripEpisodeState.DELIVERED)),
        )
        .limit(1)
    )
    return found is not None


async def latest_delivery(session: AsyncSession, episode_id: int) -> datetime | None:
    """The most recent device confirmation on the episode, by any user (M19 T4).

    Any row's ``delivered_at``, whatever its state and released or not (a
    confirmation on a cancelled or expired row stamps it without changing the
    state): the settle waits :data:`~arc.services.trips.names.
    TRIP_SETTLE_DELAY` from the latest of them, so every user's second device
    has the same hour.
    """
    found: datetime | None = await session.scalar(
        select(func.max(TripEpisode.delivered_at)).where(
            TripEpisode.episode_id == episode_id,
            TripEpisode.delivered_at.is_not(None),
        )
    )
    return found


async def trip_copy_priority(session: AsyncSession, episode_id: int) -> int:
    """The priority a trip copy of this episode is queued at (FR-P3).

    The earliest place it holds in any active trip that is still waiting on
    it, so two users' trips through the same show share the better of the two.
    An episode no trip is waiting on takes position 0.
    """
    position = await session.scalar(
        select(func.min(Episode.number - Trip.first_number))
        .select_from(TripEpisode)
        .join(Trip, Trip.id == TripEpisode.trip_id)
        .join(Episode, Episode.id == TripEpisode.episode_id)
        .where(
            TripEpisode.episode_id == episode_id,
            Trip.state == TripState.ACTIVE,
            TripEpisode.state == TripEpisodeState.PENDING,
        )
    )
    return copy_priority(int(position or 0))


async def ended_trip_leftover(session: AsyncSession, episode_id: int) -> bool:
    """Whether an episode is only here because of a trip that has ended.

    It was in some trip, no trip is waiting on it any more, and nobody wants
    it for anything else (a trip row left behind by a cancel, before the
    reconciliation ends it, does not count). The linker asks this so a file
    that lands after its trip was cancelled is deleted rather than transcoded
    for nobody.
    """
    in_a_trip = await session.scalar(
        select(TripEpisode.trip_id).where(TripEpisode.episode_id == episode_id).limit(1)
    )
    if in_a_trip is None or await pending_episode_ids(session, [episode_id]):
        return False
    other = await session.scalar(
        select(Want.user_id)
        .where(
            Want.episode_id == episode_id,
            Want.dropped_at.is_(None),
            Want.trip.is_(False),
        )
        .limit(1)
    )
    return other is None


__all__ = [
    "MAX_COPY_DAYS",
    "delivered_to",
    "ended_trip_leftover",
    "holds_trip_episode",
    "latest_delivery",
    "needs_rendition",
    "pending_episode_ids",
    "trip_copy_days",
    "trip_copy_priority",
    "trip_max_episodes",
    "trip_only_episode_ids",
    "trip_pack_min_seeders",
]
