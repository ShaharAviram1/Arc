"""What Arc should be fetching, recomputed from scratch (FR-A1, FR-A2, FR-W4).

:func:`compute_wants` is a **reconciler**, not an event handler. It reads every
list entry, works out the whole set of (user, episode) pairs that ought to
exist, and makes the ``wants`` table equal to it: rows that should be there are
inserted, rows that should not are deleted. That is why it can be run on a
timer, after a list change and after a watch completion without any of those
paths having to know what the others did, and why running it twice changes
nothing the second time.

The window (FR-A1). For each list entry in ``watching`` or ``planned``, take
``p`` — the user's furthest completed episode — and want ``p+1 … p+N`` of the
episodes that have **aired**. ``p`` is the larger of the user's completed
``watch_progress`` rows and ``list_entries.progress``, and the two disagree
more often than you would think: progress imported from MAL knows about
episodes watched before Arc existed, and Arc's own completions know about
episodes MAL has not been told about yet. Taking the smaller of the two would
re-fetch something the user has already seen, which is the one mistake a
"fetch the next N" rule must not make.

"Aired" is :mod:`arc.services.catalog.airing`'s rule, read from there rather
than re-derived — a home page that says "behind by 2" while acquisition thinks
nothing has aired is worse than either answer alone. For an airing show the
newest aired episode is inside the window on its air day for free: it is
``p+1`` for anyone who is caught up.

Merging (FR-A2) is what the table shape does on its own: wants are keyed by
(user, episode), and the *episode* is what gets a state and a torrent. Three
users wanting episode 7 is three rows and one download.

Dropping (FR-W4). ``dropped``, ``completed`` and ``on_hold`` shows, and shows
removed from a list entirely, produce no wants, so their rows are deleted — as
are rows that fell out of the window because the user watched ahead. Deleting
rather than tombstoning is deliberate here: ``dropped_at`` exists for FR-T2's
*stale want* rule (M10), which is a statement about one user and one episode
("you have had this for D days and not watched it") that must survive a
recompute. A row deleted for being out of the window carries no such
information — the window is recomputed from the list every fifteen minutes,
so the row would be recreated the moment it applied again.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    Want,
    WatchProgress,
)
from arc.services.acquisition.names import (
    SEARCH_RELEASE,
    enqueue_compute_wants,
    search_dedupe_key,
)
from arc.services.acquisition.rules import look_ahead_n
from arc.services.acquisition.states import transition
from arc.services.catalog.airing import aired_through, is_aired
from arc.services.jobs.queue import enqueue

log = logging.getLogger(__name__)

#: The list states that generate wants (FR-A1). Everything else is FR-W4's
#: "no acquisition wants": ``on_hold`` is in that group deliberately — a show
#: nobody is watching should not keep pulling episodes onto the disk, and
#: setting it back to ``watching`` recomputes the window immediately.
WANTING_STATUSES: tuple[ListStatus, ...] = (ListStatus.WATCHING, ListStatus.PLANNED)

#: Episode states a want may start a search from. Anything further along has
#: work or bytes behind it and is left exactly as it is: an episode that is
#: already ``downloading`` does not need to be told it is wanted.
STARTABLE: frozenset[EpisodeState] = frozenset({EpisodeState.NOT_WANTED, EpisodeState.UNAVAILABLE})

#: And the states that go back to ``not_wanted`` when the last want on them
#: goes away: the ones with no bytes behind them. ``searching`` is one of them
#: — a search job in flight releases the episode itself when it finds the want
#: gone, but the job may not run for hours, may have been dropped, or may never
#: have been queued, and until something writes that row the show page says
#: "looking for a release" on behalf of nobody. Both writers reach the same
#: state, which is what makes either of them sufficient.
RELEASABLE: frozenset[EpisodeState] = frozenset(
    {EpisodeState.WANTED, EpisodeState.SEARCHING, EpisodeState.UNAVAILABLE}
)

#: How long an ``unavailable`` episode is left alone before a want is allowed
#: to start the search over (FR-A6 gives up "after 14 days"; it does not say
#: never again). A day: releases for a show that had none yesterday appear on
#: their own schedule, and asking Nyaa hourly for something that has not
#: existed for a fortnight is rude for no gain.
UNAVAILABLE_RETRY = timedelta(days=1)

#: Why a row was removed, for the log.
REASON_OUT_OF_WINDOW = "outside the look-ahead window"
REASON_NOT_WANTING = "the show is no longer watching/planned"


@dataclass(frozen=True, slots=True)
class WantsResult:
    """What one reconciliation did."""

    #: (user, episode) pairs that should exist afterwards.
    wanted: int = 0
    #: Rows inserted. Named ``added`` rather than ``created`` because these
    #: counts are logged through ``extra=`` and ``created`` is one of
    #: ``LogRecord``'s own attributes — ``logging`` raises rather than let a
    #: caller shadow it, which turns a log line into a failed job.
    added: int = 0
    #: Rows deleted because the window or the list moved.
    removed: int = 0
    #: Wants whose ``dropped_at`` was cleared because they are live again.
    revived: int = 0
    #: Episodes moved ``not_wanted``/``unavailable`` → ``wanted``.
    started: int = 0
    #: ``search_release`` jobs enqueued (a dedupe hit is not counted).
    searches: int = 0
    #: Episodes returned to ``not_wanted`` because nothing wants them.
    released: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "wanted": self.wanted,
            "added": self.added,
            "removed": self.removed,
            "revived": self.revived,
            "started": self.started,
            "searches": self.searches,
            "released": self.released,
        }


async def _episodes_by_anime(
    session: AsyncSession, anime_ids: set[int]
) -> dict[int, list[Episode]]:
    """Every episode of these shows, grouped, in number order."""
    if not anime_ids:
        return {}
    rows = await session.scalars(
        select(Episode)
        .where(Episode.anime_id.in_(anime_ids))
        .order_by(Episode.anime_id, Episode.number)
    )
    grouped: dict[int, list[Episode]] = defaultdict(list)
    for episode in rows.all():
        grouped[episode.anime_id].append(episode)
    return grouped


async def _completed_through(
    session: AsyncSession, pairs: set[tuple[int, int]]
) -> dict[tuple[int, int], int]:
    """``(user, anime) → highest completed episode number`` (FR-S4).

    One query for the whole reconciliation. Only ``completed`` rows count: a
    user who is eight minutes into episode 7 has not watched it, and wanting
    episode 8 because they pressed play would fetch ahead of the rule.
    """
    if not pairs:
        return {}
    user_ids = {user_id for user_id, _ in pairs}
    anime_ids = {anime_id for _, anime_id in pairs}
    rows = await session.execute(
        select(WatchProgress.user_id, Episode.anime_id, Episode.number)
        .join(Episode, Episode.id == WatchProgress.episode_id)
        .where(
            WatchProgress.user_id.in_(user_ids),
            WatchProgress.completed.is_(True),
            Episode.anime_id.in_(anime_ids),
        )
    )
    furthest: dict[tuple[int, int], int] = {}
    for user_id, anime_id, number in rows.all():
        key = (user_id, anime_id)
        if number > furthest.get(key, 0):
            furthest[key] = number
    return furthest


def window(
    episodes: list[Episode],
    *,
    progress: int,
    look_ahead: int,
    now: datetime,
    anime_status: str | None,
    next_airing: dict[str, object] | None,
) -> list[Episode]:
    """The next ``look_ahead`` aired episodes after ``progress`` (FR-A1).

    Pure, so the table of progress/N/aired combinations in the tests is the
    specification of the rule rather than a description of it.
    """
    if look_ahead <= 0:
        return []
    boundary = aired_through(episodes, now=now, anime_status=anime_status, next_airing=next_airing)
    picked: list[Episode] = []
    for episode in episodes:
        if episode.number <= progress:
            continue
        if episode.number > progress + look_ahead:
            break
        if not is_aired(episode, now=now, anime_status=anime_status, boundary=boundary):
            # Not `break`: episode lists are ordered but not always complete,
            # and a gap with no air date must not hide the aired episode after
            # it. The bound above is what stops the loop.
            continue
        picked.append(episode)
    return picked


async def compute_wants(session: AsyncSession, *, now: datetime | None = None) -> WantsResult:
    """Reconcile ``wants`` with every user's list, then start what is missing.

    Flushes but does not commit; the caller owns the transaction, so the wants,
    the state changes and the ``search_release`` jobs land together or not at
    all.
    """
    moment = now or datetime.now(UTC)
    look_ahead = await look_ahead_n(session)

    rows = await session.execute(
        select(ListEntry, Anime)
        .join(Anime, Anime.id == ListEntry.anime_id)
        .where(ListEntry.status.in_(WANTING_STATUSES))
        .order_by(ListEntry.user_id, ListEntry.anime_id)
    )
    entries = list(rows.all())
    episodes = await _episodes_by_anime(session, {anime.id for _, anime in entries})
    furthest = await _completed_through(
        session, {(entry.user_id, entry.anime_id) for entry, _ in entries}
    )

    desired: set[tuple[int, int]] = set()
    for entry, anime in entries:
        progress = max(entry.progress, furthest.get((entry.user_id, entry.anime_id), 0))
        for episode in window(
            episodes.get(anime.id, []),
            progress=progress,
            look_ahead=look_ahead,
            now=moment,
            anime_status=anime.status,
            next_airing=anime.next_airing,
        ):
            desired.add((entry.user_id, episode.id))

    result = await _reconcile(session, desired, now=moment)
    return await _start_searches(session, result, now=moment)


async def _reconcile(
    session: AsyncSession, desired: set[tuple[int, int]], *, now: datetime
) -> WantsResult:
    """Make ``wants`` equal ``desired``: insert, revive, delete."""
    existing = {
        (want.user_id, want.episode_id): want
        for want in (await session.scalars(select(Want))).all()
    }

    added = revived = 0
    for key in desired:
        want = existing.get(key)
        if want is None:
            session.add(Want(user_id=key[0], episode_id=key[1]))
            added += 1
            continue
        if want.dropped_at is not None:
            # TODO(M10/FR-T2): this is also how a want dropped for going
            # unwatched for D days comes back to life fifteen minutes later.
            # When the stale-want sweep lands it must record *why* the row was
            # dropped and this must leave a ``drop_reason`` of that kind alone
            # until the user's progress or the window actually moves.
            want.dropped_at = None
            want.drop_reason = None
            revived += 1

    stale = [key for key in existing if key not in desired]
    for user_id, episode_id in stale:
        await session.execute(
            delete(Want).where(Want.user_id == user_id, Want.episode_id == episode_id)
        )
    await session.flush()

    if added or revived or stale:
        log.info(
            "wants reconciled",
            extra={"added": added, "revived": revived, "removed": len(stale)},
        )
    return WantsResult(wanted=len(desired), added=added, removed=len(stale), revived=revived)


async def _start_searches(
    session: AsyncSession, result: WantsResult, *, now: datetime
) -> WantsResult:
    """Move episodes into and out of ``wanted``, and queue the searches.

    An ``unavailable`` episode is only restarted once :data:`UNAVAILABLE_RETRY`
    has passed since it landed there, so a want that survives a fortnight of
    fruitless searching does not re-open one every quarter of an hour.
    """
    live = select(Want.episode_id).where(Want.dropped_at.is_(None))
    with_wants = set((await session.scalars(live.distinct())).all())
    # Ids are handed out in order, so "queued by this run" is "newer than
    # anything that existed before it". Read once, because the only thing it is
    # used for is telling a fresh row from the one ``enqueue`` returns on a
    # dedupe hit — which is the count in the log line and nothing else.
    newest_job = await session.scalar(select(func.max(Job.id))) or 0

    # Bounded on purpose: every episode of every cached show is
    # ``not_wanted``, and a sweep over all of them every fifteen minutes would
    # grow with the catalogue rather than with the queue. Only episodes
    # somebody wants, and episodes sitting in a state a *missing* want has to
    # undo, can possibly change here.
    interesting = (
        await session.scalars(
            select(Episode).where(or_(Episode.id.in_(live), Episode.state.in_(RELEASABLE)))
        )
    ).all()

    started = searches = released = 0
    for episode in interesting:
        if episode.id not in with_wants:
            if episode.state in RELEASABLE:
                transition(episode, EpisodeState.NOT_WANTED, reason="nobody wants this episode")
                released += 1
            continue
        if episode.state not in STARTABLE:
            continue
        if (
            episode.state is EpisodeState.UNAVAILABLE
            and episode.state_changed_at is not None
            and now - episode.state_changed_at < UNAVAILABLE_RETRY
        ):
            continue
        transition(episode, EpisodeState.WANTED, reason="a user wants this episode")
        started += 1
        # ``enqueue`` does the dedupe itself and returns the row that already
        # holds the key, so asking first would be the same query twice.
        job = await enqueue(
            session,
            SEARCH_RELEASE,
            {"episode_id": episode.id},
            dedupe_key=search_dedupe_key(episode.id),
        )
        if job.id > newest_job:
            searches += 1

    await session.flush()
    if started or released:
        log.info(
            "acquisition window applied",
            extra={"started": started, "searches": searches, "released": released},
        )
    return WantsResult(
        wanted=result.wanted,
        added=result.added,
        removed=result.removed,
        revived=result.revived,
        started=started,
        searches=searches,
        released=released,
    )


__all__ = [
    "RELEASABLE",
    "STARTABLE",
    "UNAVAILABLE_RETRY",
    "WANTING_STATUSES",
    "WantsResult",
    "compute_wants",
    "enqueue_compute_wants",
    "window",
]
