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
removed from a list entirely, produce no wants — and their rows are **dropped**
(:data:`REASON_NOT_WANTING`), not deleted. The row is what retention measures
FR-T1's grace period from: an episode whose only watcher put the show on hold
this morning has a moment attached to it ("this is when it stopped being
wanted"), and deleting the row would throw that moment away and leave the sweep
to judge the files by their own age — an episode dropped an hour ago would then
be deleted tonight because its bytes happen to be a month old, and the sweep
would say "nobody wants it and nobody ever did" about something somebody wanted
until this morning. Keeping the row is also what makes the return trip work:
the show going back to ``watching`` finds the row in the window again and
revives it.

Rows that fall out of the *window* are still deleted, and that difference is
the point. A user who watched episode 5 has moved past it: the completion is in
``watch_progress``, retention reads *that* as the anchor, and the want row has
nothing left to say. So the rule is one line — a want whose (user, show) still
has a ``watching``/``planned`` list entry left the window and is deleted;
anything else stopped being wanted and is dropped.

Going stale (FR-T2, M10). Every run, a want on an episode that has been
``ready`` for more than **D** days and that its user has not completed is
**dropped**: ``dropped_at`` and ``drop_reason`` are written and the row stops
counting as live, so a show somebody has quietly given up on stops pinning
files to the disk and retention's grace period (FR-T1) starts running from
that moment.

That drop has to survive the next reconciliation, fifteen minutes later, or
it would mean nothing at all — and the reconciler's ordinary behaviour is to
revive any dropped row whose (user, episode) is back in the window, which it
still is: the user has not watched anything, so the window has not moved. So
a stale drop is only cleared when **the user has done something about the show
in Arc since**: ``list_entries.updated_by == arc`` and an ``updated_at`` later
than the drop. Every path through ``PUT /api/list/{id}`` writes both, so a
status change, a progress edit, a score or a re-add after a delete all count.

Why the ``updated_by`` half (the revival rule, in full). ``updated_at`` alone
would also be moved by a MAL import, and an import's timestamp is MAL's own —
so a *score* set on MyAnimeList, which says nothing whatsoever about whether
the user intends to watch this episode, would silently start the download
again. Arc cannot tell a MAL-side status or progress change from a MAL-side
score change after the fact, because the previous remote values are not stored
anywhere; it would take a column per want to remember what the entry looked
like when the drop happened. It does not need one, because the two MAL-side
changes that *should* revive a want do it without going through this branch at
all:

* **progress** moves the window. A row the user's progress has passed leaves
  ``desired`` and is deleted; the episodes that come into the window instead
  are inserted live, which is a fetch either way.
* **status** takes the show out of ``watching``/``planned`` and back. Leaving
  re-labels the row :data:`REASON_NOT_WANTING` (below), and coming back revives
  it through the ordinary path, whichever side made the change.

What is left over — a MAL-side score, a MAL row touched with nothing changed —
is exactly the set of changes that must *not* revive a stale drop. So the rule
is "Arc-side action, later than the drop", and it is complete rather than
merely conservative. A row dropped for any other reason is revived as it always
was.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
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
    Rendition,
    UpdatedBy,
    Want,
    WatchProgress,
)
from arc.services.acquisition.names import (
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    enqueue_compute_wants,
    search_dedupe_key,
)
from arc.services.acquisition.rules import is_paused, look_ahead_n
from arc.services.acquisition.states import transition
from arc.services.catalog.airing import aired_through, is_aired
from arc.services.jobs.queue import enqueue
from arc.services.retention.rules import unwatched_period

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

#: What :func:`_reconcile` writes into ``wants.drop_reason`` when the show
#: stopped being ``watching``/``planned`` — a status change to ``on_hold``,
#: ``dropped`` or ``completed``, or the entry removed from the list entirely
#: (FR-W4). Unlike :data:`STALE_DROP_REASON` it puts no condition on the
#: revival: the show coming back is the user coming back.
REASON_NOT_WANTING = "show no longer watching or planned"

#: What FR-T2 writes into ``wants.drop_reason``, and the value
#: :func:`_reconcile` refuses to revive without a sign that the user came back
#: to the show. The spec's own words, with D left as D: the number is in
#: ``settings`` and can be changed on any afternoon, and a row that says "21"
#: when the setting says 30 would be a lie about a rule rather than a record
#: of one. The days themselves are in the log line.
STALE_DROP_REASON = "unwatched for D days"


@dataclass(frozen=True, slots=True)
class _Touch:
    """When a user last did something to one show, and which side did it.

    Read off ``list_entries``, carried through the reconciliation so that the
    revival rule is decided from the row rather than from a second query.
    """

    at: datetime
    by: UpdatedBy

    def came_back_after(self, dropped_at: datetime) -> bool:
        """Whether this is the "the user came back" signal FR-T2 waits for.

        An Arc-side change later than the drop, and nothing else. The module
        docstring has the argument for why MAL-side changes do not belong
        here — the ones that ought to revive a want never reach this branch.
        """
        return self.by is UpdatedBy.ARC and self.at > dropped_at


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
    #: Rows deleted because the user's progress moved past them.
    removed: int = 0
    #: Rows dropped because their show is no longer watching/planned (FR-W4).
    #: Kept rather than deleted, so retention's grace period has a moment to
    #: run from (:data:`REASON_NOT_WANTING`).
    shelved: int = 0
    #: Wants whose ``dropped_at`` was cleared because they are live again.
    revived: int = 0
    #: Wants dropped for going unwatched for D days (FR-T2).
    dropped: int = 0
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
            "shelved": self.shelved,
            "revived": self.revived,
            "dropped": self.dropped,
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
            # and one episode the rule cannot place must not hide the aired
            # episode after it. The bound above is what stops the loop.
            continue
        picked.append(episode)
    return picked


async def compute_wants(session: AsyncSession, *, now: datetime | None = None) -> WantsResult:
    """Reconcile ``wants`` with every user's list, then start what is missing.

    Flushes but does not commit; the caller owns the transaction, so the wants,
    the state changes and the ``search_release`` jobs land together or not at
    all.

    While acquisition is paused this does **nothing at all** — no rows read,
    none written, no episode moved, nothing queued — and answers an empty
    result. Not "reconcile but skip the searches": the reconciliation is what
    releases episodes back to ``not_wanted`` and drops rows, and a pause that
    quietly rewrote a table would be a strange thing for a button labelled
    *pause* to do. The whole point is that resuming finds the world exactly as
    the pause left it.
    """
    if await is_paused(session):
        log.info("acquisition paused; wants left untouched")
        return WantsResult()

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

    # The value is when the user last did something to this show, and which
    # side did it. That is what decides whether a want dropped for going stale
    # (FR-T2) is allowed to come back — see :func:`_reconcile`.
    desired: dict[tuple[int, int], _Touch] = {}
    #: Every (user, show) with a ``watching``/``planned`` entry, which is how a
    #: want that left the *window* is told from one whose show stopped being
    #: wanted at all.
    wanting: set[tuple[int, int]] = set()
    for entry, anime in entries:
        wanting.add((entry.user_id, entry.anime_id))
        touch = _Touch(at=entry.updated_at, by=entry.updated_by)
        progress = max(entry.progress, furthest.get((entry.user_id, entry.anime_id), 0))
        for episode in window(
            episodes.get(anime.id, []),
            progress=progress,
            look_ahead=look_ahead,
            now=moment,
            anime_status=anime.status,
            next_airing=anime.next_airing,
        ):
            desired[(entry.user_id, episode.id)] = touch

    result = await _reconcile(session, desired, wanting, now=moment)
    result = await _drop_stale(session, result, now=moment)
    return await _start_searches(session, result, now=moment)


async def _reconcile(
    session: AsyncSession,
    desired: Mapping[tuple[int, int], _Touch],
    wanting: Collection[tuple[int, int]],
    *,
    now: datetime,
) -> WantsResult:
    """Make ``wants`` equal ``desired``: insert, revive, drop, delete.

    **Leaving is two different things** (FR-W4). A row whose (user, show) still
    has a ``watching``/``planned`` entry left the *window* — the user watched
    past it, or N was turned down — and is deleted: there is nothing to
    remember, and a completion (which is what retention will measure the grace
    period from) is already in ``watch_progress``. A row whose show is not in
    ``wanting`` at all stopped being wanted, and is **dropped**
    (:data:`REASON_NOT_WANTING`) so that retention has the moment it stopped.

    A row that is *already* dropped keeps its ``dropped_at`` — restamping it
    would push the deletion date away every quarter of an hour for as long as
    the show sat on hold — but does take the new reason, which is what lets a
    stale drop come back when the show does (see the module docstring).

    **Reviving** is the subtle one. A row that is in the window and carries a
    ``dropped_at`` is ordinarily brought back — that is how a want dropped
    while the show was on hold returns when it goes back to watching. The one
    exception is FR-T2's stale drop (:data:`STALE_DROP_REASON`): the user has
    had the episode ready for D days and not watched it, and reviving that on
    the next tick would undo the rule fifteen minutes after it applied. It
    comes back when the user has acted on the show *in Arc* since the drop —
    ``updated_by == arc`` and a later ``updated_at`` — and not for a MAL import
    that merely moved a score.
    """
    rows = await session.execute(
        select(Want, Episode.anime_id).join(Episode, Episode.id == Want.episode_id)
    )
    existing = {(want.user_id, want.episode_id): (want, anime_id) for want, anime_id in rows.all()}

    added = revived = 0
    for key, touch in desired.items():
        found = existing.get(key)
        if found is None:
            session.add(Want(user_id=key[0], episode_id=key[1]))
            added += 1
            continue
        want = found[0]
        if want.dropped_at is None:
            continue
        if want.drop_reason == STALE_DROP_REASON and not touch.came_back_after(want.dropped_at):
            continue
        want.dropped_at = None
        want.drop_reason = None
        revived += 1

    removed: list[tuple[int, int]] = []
    shelved = 0
    for key, (want, anime_id) in existing.items():
        if key in desired:
            continue
        if (key[0], anime_id) in wanting:
            removed.append(key)
            continue
        if want.dropped_at is None:
            want.dropped_at = now
            shelved += 1
        want.drop_reason = REASON_NOT_WANTING
    for user_id, episode_id in removed:
        await session.execute(
            delete(Want).where(Want.user_id == user_id, Want.episode_id == episode_id)
        )
    await session.flush()

    if added or revived or removed or shelved:
        log.info(
            "wants reconciled",
            extra={
                "added": added,
                "revived": revived,
                "removed": len(removed),
                "shelved": shelved,
            },
        )
    return WantsResult(
        wanted=len(desired),
        added=added,
        removed=len(removed),
        shelved=shelved,
        revived=revived,
    )


async def _drop_stale(session: AsyncSession, result: WantsResult, *, now: datetime) -> WantsResult:
    """Drop wants on episodes that have sat ready and unwatched for D days (FR-T2).

    Only ``ready`` episodes are considered, because "unwatched" is only a
    statement about an episode the user *could* have watched: an episode still
    downloading has not been offered to anybody, and dropping the want for it
    would cancel the download rather than free anything.

    "Since it became ready" is ``renditions.ready_at`` when there is a
    rendition — the moment the episode became playable, written once — and the
    episode's ``state_changed_at`` otherwise. The two agree in the ordinary
    case; the fallback covers an episode marked ready by a path that left no
    rendition row, where the alternative would be a want that can never go
    stale.

    A user who has *completed* the episode is never dropped by this: their
    want is on its way out through the window instead (the reconciler deletes
    it as soon as their progress moves), and a drop would put a ``dropped_at``
    on a row that is about to disappear — which retention would then read as
    the moment to start counting the grace period from.

    Nor is a user who is **still doing something about the show**. The clock
    runs from the later of "when the episode became ready" and "when this user
    last touched this show" (``list_entries.updated_at``), for two reasons.
    The first is correctness: a user who set a show to watching this morning
    should not have this afternoon's reconciliation drop the episode that has
    been sitting ready since last month — they have plainly not given up on
    it, which is the only thing FR-T2 is trying to detect. The second is that
    it is what makes the revival above mean anything: without it, a want
    revived because the user came back would be dropped again by the very same
    run, and "the user acted, so fetch it again" would last for no time at
    all. ``updated_at`` moves on an Arc list change and on a MAL import that
    actually changed something (it carries MAL's own timestamp, so a
    six-hourly re-import of an untouched row does not move it) — which is
    exactly "the user did something".
    """
    window_d = await unwatched_period(session)
    cutoff = now - window_d
    ready_since = func.coalesce(Rendition.ready_at, Episode.state_changed_at)
    # ``greatest`` ignores nulls in Postgres, so a want whose list entry has
    # gone (the reconciler is about to delete the row) falls back to the
    # episode's own readiness rather than never going stale.
    quiet_since = func.greatest(ready_since, ListEntry.updated_at)

    rows = (
        await session.execute(
            select(Want)
            .join(Episode, Episode.id == Want.episode_id)
            .outerjoin(Rendition, Rendition.episode_id == Episode.id)
            .outerjoin(
                ListEntry,
                (ListEntry.user_id == Want.user_id) & (ListEntry.anime_id == Episode.anime_id),
            )
            .outerjoin(
                WatchProgress,
                (WatchProgress.episode_id == Want.episode_id)
                & (WatchProgress.user_id == Want.user_id)
                & (WatchProgress.completed.is_(True)),
            )
            .where(
                Want.dropped_at.is_(None),
                Episode.state == EpisodeState.READY,
                WatchProgress.user_id.is_(None),
                ready_since.is_not(None),
                quiet_since < cutoff,
            )
        )
    ).scalars()

    dropped = 0
    for want in rows.all():
        want.dropped_at = now
        want.drop_reason = STALE_DROP_REASON
        dropped += 1
        log.info(
            "want dropped for going unwatched",
            extra={
                "user_id": want.user_id,
                "episode_id": want.episode_id,
                "days": int(window_d.total_seconds() // 86400),
            },
        )
    if dropped:
        await session.flush()
    return replace(result, dropped=dropped)


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
            priority=SEARCH_RELEASE_PRIORITY,
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
    return replace(result, started=started, searches=searches, released=released)


__all__ = [
    "REASON_NOT_WANTING",
    "RELEASABLE",
    "STALE_DROP_REASON",
    "STARTABLE",
    "UNAVAILABLE_RETRY",
    "WANTING_STATUSES",
    "WantsResult",
    "compute_wants",
    "enqueue_compute_wants",
    "window",
]
