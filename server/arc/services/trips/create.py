"""Starting a trip (FR-A12, owner 2026-10-05).

"Prepare for a trip" on a show: the next X **aired** episodes after the
user's progress, each made into a small offline copy for the device. Five
refusals, each a sentence the show page can say:

* the demo account (403) — FR-D5: no trips;
* an active trip already (409) — one per user, also a database index;
* the disk under its floor (409) — FR-T6: a trip starts fetching at once,
  which is exactly what a hold forbids;
* a count outside 1..``trip_max_episodes`` (422);
* nothing aired after the user's progress (422).

What it writes is ordinary acquisition, done at once rather than at the next
tick — the same bargain the sample makes (:mod:`arc.services.acquisition.
samples`): a ``trips`` row and one ``trip_episodes`` row per episode, a want
per episode (``trip`` set where nothing else wants it), a search started
through the reconciler's own :func:`~arc.services.acquisition.wants.
start_search` for every episode resting in a startable state (priority 160,
behind the window's 150), the small copy queued for every episode whose source
is already here (a ``ready`` episode, or one a transcode is preparing), and a
reconciliation queued for the rest.

**No list entry is read for writing and none is touched.** A trip is not an
activation (FR-A9): a dormant import stays dormant. And nothing here, or
anything it queues, can write to MyAnimeList.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    ListEntry,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    Trip,
    TripEpisode,
    TripState,
    User,
    Want,
    WatchProgress,
)
from arc.services.acquisition.names import SEARCH_RELEASE_PRIORITY, enqueue_compute_wants
from arc.services.acquisition.rules import is_storage_held
from arc.services.acquisition.wants import start_search
from arc.services.catalog.airing import aired_through, is_aired
from arc.services.media.copies import file_matches
from arc.services.media.names import OFFLINE_WHY_TRIP, enqueue_offline_encode, offline_path_for
from arc.services.playback.watched import watched_through
from arc.services.trips.hooks import stamp_available
from arc.services.trips.names import TRIP_SEARCH_PRIORITY, copy_priority
from arc.services.trips.rules import (
    delivered_to,
    trip_copy_days,
    trip_max_episodes,
    trip_only_episode_ids,
)

log = logging.getLogger(__name__)

#: The refusals' details: short codes the client maps to its own sentences,
#: like ``source_gone`` on the copy route.
DEMO_REFUSED = "demo_account"
TRIP_ACTIVE = "trip_active"
STORAGE_HELD = "storage_held"
COUNT_OUT_OF_RANGE = "count_out_of_range"
NOTHING_AIRED = "nothing_aired"
TRIP_NOT_FOUND = "trip not found"
TRIP_NOT_ACTIVE = "trip_not_active"


class TripError(Exception):
    """Why a trip cannot be made or changed. ``str()`` is the API's detail."""


class TripForbidden(TripError):
    """403: this account may not have trips."""


class TripConflict(TripError):
    """409: the request clashes with what is already true."""


class TripInvalid(TripError):
    """422: the request itself is out of range."""


class TripNotFound(TripError):
    """404: no such trip, or not the caller's."""


@dataclass(frozen=True, slots=True)
class TripCreated:
    """The new trip, the episodes it took, and how many were asked for."""

    trip: Trip
    episodes: list[Episode]
    requested: int


async def active_trip(session: AsyncSession, user_id: int) -> Trip | None:
    """The user's active trip, if they have one (there is at most one)."""
    found: Trip | None = await session.scalar(
        select(Trip).where(Trip.user_id == user_id, Trip.state == TripState.ACTIVE).limit(1)
    )
    return found


async def _progress(session: AsyncSession, user_id: int, anime_id: int) -> int:
    """FR-W5's boundary for one user and show: the reconciler's own rule."""
    entry = await session.get(ListEntry, (user_id, anime_id))
    furthest = await session.scalar(
        select(func.max(Episode.number))
        .join(WatchProgress, WatchProgress.episode_id == Episode.id)
        .where(
            WatchProgress.user_id == user_id,
            WatchProgress.completed.is_(True),
            Episode.anime_id == anime_id,
        )
    )
    return watched_through(entry.progress if entry is not None else 0, int(furthest or 0))


def pick_episodes(
    episodes: list[Episode],
    *,
    progress: int,
    count: int,
    now: datetime,
    anime_status: str | None,
    next_airing: dict[str, object] | None,
    skip: set[int] | frozenset[int] = frozenset(),
) -> list[Episode]:
    """Up to ``count`` aired episodes after ``progress``, in number order. Pure.

    ``episodes`` must be the show's whole list in number order (the aired
    boundary is read from it). ``skip`` is what a device of the user already
    holds from an earlier trip; those are passed over and the trip reaches one
    further for each, because a trip is a count of episodes to take along.
    """
    boundary = aired_through(episodes, now=now, anime_status=anime_status, next_airing=next_airing)
    picked: list[Episode] = []
    for episode in episodes:
        if len(picked) >= count:
            break
        if episode.number <= progress or episode.id in skip:
            continue
        if not is_aired(episode, now=now, anime_status=anime_status, boundary=boundary):
            continue
        picked.append(episode)
    return picked


def _want(wants: dict[int, Want], session: AsyncSession, user_id: int, episode_id: int) -> None:
    """Make sure this user wants this episode; a new or revived row is a trip's.

    A live row is left exactly as it is — the window or a sample already
    wants it, and the reconciler keeps ``trip`` false on it. A dropped row is
    revived as a trip row: asking for a trip is the user acting on the show.
    ``wants`` is the user's rows on the trip's episodes, read in one query.
    """
    want = wants.get(episode_id)
    if want is None:
        session.add(Want(user_id=user_id, episode_id=episode_id, trip=True))
        return
    if want.dropped_at is None:
        return
    want.dropped_at = None
    want.drop_reason = None
    want.sample = False
    want.trip = True


#: Episode states with a linked source a copy can be made from.
_COPYABLE: frozenset[EpisodeState] = frozenset(
    {EpisodeState.READY, EpisodeState.MATCHED, EpisodeState.PREPARING, EpisodeState.FAILED}
)


async def _newest_sources(session: AsyncSession, episode_ids: list[int]) -> dict[int, MediaFile]:
    """``episode → newest linked media file``, the transcode's own choice, in one query."""
    if not episode_ids:
        return {}
    found: dict[int, MediaFile] = {}
    for media in (
        await session.scalars(
            select(MediaFile)
            .where(MediaFile.episode_id.in_(episode_ids))
            .order_by(MediaFile.episode_id, MediaFile.id.desc())
        )
    ).all():
        if media.episode_id is not None:
            found.setdefault(media.episode_id, media)
    return found


async def queue_trip_copy(
    session: AsyncSession,
    settings: Settings,
    episode: Episode,
    *,
    position: int,
    now: datetime,
    copy: OfflineCopy | None,
    source: MediaFile | None,
) -> bool:
    """Make sure a trip's copy of ``episode`` exists or is on its way.

    A ready copy whose file is on disk is stamped available on the spot; one
    that can be made — the source is on disk — is queued (``why="trip"``,
    priority 600 + 10 × position). Returns whether there is, or will be, a
    copy. Episodes whose file has not arrived yet are the linker's: it queues
    the copy when the file is matched (:mod:`arc.services.library.link`).
    ``copy`` and ``source`` are the caller's batched lookups.
    """
    if (
        copy is not None
        and copy.state is OfflineCopyState.READY
        and file_matches(offline_path_for(settings, episode.id), copy)
    ):
        await stamp_available(session, episode.id, now=now)
        return True
    if episode.state not in _COPYABLE:
        return False
    if source is None or not Path(source.path).is_file():
        return False
    await enqueue_offline_encode(
        session, episode.id, why=OFFLINE_WHY_TRIP, priority=copy_priority(position)
    )
    if copy is None:
        session.add(
            OfflineCopy(
                episode_id=episode.id, state=OfflineCopyState.QUEUED, media_file_id=source.id
            )
        )
    elif copy.state in (OfflineCopyState.READY, OfflineCopyState.FAILED):
        # A ready row whose file has gone, or a failed one: asked for again.
        copy.state = OfflineCopyState.QUEUED
        copy.error = None
        copy.media_file_id = source.id
    return True


async def create_trip(
    session: AsyncSession,
    settings: Settings,
    *,
    user: User,
    anime_id: int,
    count: int,
    now: datetime,
) -> TripCreated:
    """Start a trip; raise a :class:`TripError` for each refusal. Flushed, not committed."""
    if user.is_demo:
        raise TripForbidden(DEMO_REFUSED)
    anime = await session.get(Anime, anime_id)
    if anime is None:
        raise TripNotFound("anime not found")
    if await active_trip(session, user.id) is not None:
        raise TripConflict(TRIP_ACTIVE)
    if await is_storage_held(session, settings):
        raise TripConflict(STORAGE_HELD)
    cap = await trip_max_episodes(session)
    if isinstance(count, bool) or not 1 <= count <= cap:
        raise TripInvalid(COUNT_OUT_OF_RANGE)
    episodes = list(
        (
            await session.scalars(
                select(Episode).where(Episode.anime_id == anime_id).order_by(Episode.number)
            )
        ).all()
    )
    progress = await _progress(session, user.id, anime_id)
    held = (await delivered_to(session, [user.id])).get(user.id, set())
    taken = pick_episodes(
        episodes,
        progress=progress,
        count=count,
        now=now,
        anime_status=anime.status,
        next_airing=anime.next_airing,
        skip=held,
    )
    if not taken:
        raise TripInvalid(NOTHING_AIRED)

    trip = Trip(
        user_id=user.id,
        anime_id=anime_id,
        first_number=taken[0].number,
        last_number=taken[-1].number,
        count=len(taken),
        state=TripState.ACTIVE,
        created_at=now,
        deadline_at=now + timedelta(days=await trip_copy_days(session)),
    )
    try:
        # A savepoint, so a request racing this one onto the one-active-trip
        # index is a 409 rather than a poisoned session.
        async with session.begin_nested():
            session.add(trip)
            await session.flush()
    except IntegrityError as exc:
        raise TripConflict(TRIP_ACTIVE) from exc

    ids = [episode.id for episode in taken]
    wants = {
        want.episode_id: want
        for want in (
            await session.scalars(
                select(Want).where(Want.user_id == user.id, Want.episode_id.in_(ids))
            )
        ).all()
    }
    for episode in taken:
        session.add(TripEpisode(trip_id=trip.id, episode_id=episode.id))
        _want(wants, session, user.id, episode.id)
    await session.flush()

    trip_only = await trip_only_episode_ids(session, ids)
    copies_of = {
        copy.episode_id: copy
        for copy in (
            await session.scalars(select(OfflineCopy).where(OfflineCopy.episode_id.in_(ids)))
        ).all()
    }
    sources = await _newest_sources(session, ids)
    started = copies = 0
    for position, episode in enumerate(taken):
        if await queue_trip_copy(
            session,
            settings,
            episode,
            position=position,
            now=now,
            copy=copies_of.get(episode.id),
            source=sources.get(episode.id),
        ):
            copies += 1
            if episode.state is not EpisodeState.READY and episode.id in trip_only:
                # A trip-only episode whose copy already exists or is on its
                # way needs nothing fetched (its source may already be gone).
                continue
        moved, _ = await start_search(
            session,
            episode,
            now=now,
            retry_now=True,
            priority=TRIP_SEARCH_PRIORITY if episode.id in trip_only else SEARCH_RELEASE_PRIORITY,
        )
        started += int(moved)
    await enqueue_compute_wants(session)
    await session.flush()
    log.info(
        "trip created",
        extra={
            "user_id": user.id,
            "anime_id": anime_id,
            "trip_id": trip.id,
            "requested": count,
            "taken": len(taken),
            "first": trip.first_number,
            "last": trip.last_number,
            "searches_started": started,
            "copies": copies,
        },
    )
    return TripCreated(trip=trip, episodes=taken, requested=count)


__all__ = [
    "COUNT_OUT_OF_RANGE",
    "DEMO_REFUSED",
    "NOTHING_AIRED",
    "STORAGE_HELD",
    "TRIP_ACTIVE",
    "TRIP_NOT_ACTIVE",
    "TRIP_NOT_FOUND",
    "TripConflict",
    "TripCreated",
    "TripError",
    "TripForbidden",
    "TripInvalid",
    "TripNotFound",
    "active_trip",
    "create_trip",
    "pick_episodes",
    "queue_trip_copy",
]
