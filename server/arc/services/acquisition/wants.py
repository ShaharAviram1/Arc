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
"fetch the next N" rule must not make. That ``max`` is FR-W5's own boundary and
is taken from :func:`~arc.services.playback.watched.watched_through`, so the
window and the watched marks on a show page are one rule rather than two
copies of it.

"Aired" is :mod:`arc.services.catalog.airing`'s rule, read from there rather
than re-derived — a home page that says "behind by 2" while acquisition thinks
nothing has aired is worse than either answer alone. For an airing show the
newest aired episode is inside the window on its air day for free: it is
``p+1`` for anyone who is caught up.

Merging (FR-A2) is what the table shape does on its own: wants are keyed by
(user, episode), and the *episode* is what gets a state and a torrent. Three
users wanting episode 7 is three rows and one download.

Dormant imports (FR-A9, owner 2026-09-13). One thing comes *before* the window:
whether the entry counts at all. A ``watching``/``planned`` entry that arrived
in a MyAnimeList import and that nobody has touched in Arc since is **dormant**
— it contributes nothing to ``desired``, and its (user, show) does not join
``wanting`` either, so a want it left behind is shelved
(:data:`~arc.services.acquisition.dormancy.REASON_DORMANT`) and a download in
flight for it is cancelled like any other want that has gone away. A show that
is currently airing is the exception and is never dormant. The rule itself, and
the argument for it, are in :mod:`arc.services.acquisition.dormancy`; the only
thing decided here is what a dormant entry *does*, which is nothing.

The slot cap (FR-A10, owner 2026-09-13) is the gate after the window, and the
only one that is per *user*: at most **K** of one person's shows may be
fetching at once (default 5, 0 for no cap). Shows already fetching keep their
slot — the cap limits starting, never cancels — free slots go to currently
airing shows first and then to the most recently touched entries, and the rest
**wait**: they take no part in the reconciliation at all, so no want is created
for them and every row they already have is left exactly as it was found. An
episode that has *arrived* (``ready``), one FR-A6 has given up on
(``unavailable``) and one whose transcode broke (``failed``) hold no slot —
between them they are every way a slot could be held for ever by a show that is
not fetching. The rule and the argument for it are in
:mod:`arc.services.acquisition.slots`; :func:`slot_view` is the same
computation answered for one user, so the show page and the reconciler cannot
disagree about who is waiting.

The storage guard (FR-T6, owner 2026-09-13) is the other new gate, and it sits
at the opposite end. A hold does not stop the reconciliation — every ending
this module writes frees disk space — it stops :func:`_start_searches` putting
an episode into ``wanted``. Releases and cancellations still happen while held,
which is the point of not treating it like the pause.

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

Cancelling (2026-09-13). A want going away is also allowed to stop work that
has already started. An episode in ``wanted``/``searching``/``unavailable``
goes back to ``not_wanted`` (:func:`release_if_unwanted`); an episode
``downloading`` has its torrent removed from qBittorrent **with its files** and
goes back to ``not_wanted`` too (:func:`cancel_if_unwanted`). Before that, a
download whose last want vanished ran to completion and was transcoded for
nobody, holding one of the client's download slots the whole way — ``poll_qbit``
reacts to what the client does, not to what anybody wants. The line is drawn at
bytes that have *landed*: from ``downloaded`` onwards the file is retention's
to measure and delete (FR-T1), because it may be half-way through a transcode
and because the grace period exists precisely so that "nobody wants this" and
"throw it away" are not the same moment.

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

Samples (FR-A8). One kind of want is not derived from anybody's list: the first
episode of a show a user asked to try, so they can decide before adding it
(``wants.sample``). The reconciler cannot work that row out from the list —
there is no entry to read — so it reads it from the row itself: a **live**
sample want whose (user, show) is not watching/planned **and whose episode the
user has not completed** is added to ``desired`` as it stands, which is what
keeps it from being shelved as "no longer watching". A sample on a show that
*is* watching/planned is governed entirely by the window, like any other want:
episode 1 is either inside it or the user has watched past it, in which case
the row is deleted as ever.

The "not completed" half is the sample's ending on a show that is on the list
without being followed — ``completed``, ``dropped``, ``on_hold``. Nothing else
would close it: FR-S4 leaves an existing entry's status alone, so the show
never joins ``wanting``, and FR-T2 will not drop a want whose user has watched
the episode. So a watched sample simply stops being desired and is shelved
below, with retention counting FR-T1's grace from the completion.

A **dropped** sample outside ``wanting`` is left exactly as it is, reason and
all. That is the end of a sample's life and the one place the rules differ: the
D-day drop (FR-T2) is precisely what is meant to happen to a sample nobody
watched, and shelving it under :data:`REASON_NOT_WANTING` a quarter of an hour
later would overwrite that record — and, worse, hand it back the unconditional
revival that reason carries. Pressing the button again is the only way back,
and it clears the drop itself.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    Rendition,
    Torrent,
    UpdatedBy,
    Want,
    WatchProgress,
)
from arc.services.acquisition.dormancy import REASON_DORMANT, is_dormant
from arc.services.acquisition.names import (
    QBIT_CANCEL,
    QBIT_CANCEL_PRIORITY,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    cancel_dedupe_key,
    enqueue_compute_wants,
    search_dedupe_key,
)
from arc.services.acquisition.qbit import DECIDED_STATES, QBIT_CANCELLED
from arc.services.acquisition.rules import (
    is_paused,
    is_storage_held,
    look_ahead_and_cap,
)
from arc.services.acquisition.slots import SETTLED, SlotShow, assign_slots
from arc.services.acquisition.states import transition
from arc.services.catalog.airing import RELEASING, aired_through, is_aired
from arc.services.jobs.queue import enqueue
from arc.services.playback.watched import watched_through
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

#: And the states whose last want going away **cancels a download in flight**.
#: Only ``downloading``: bytes are arriving and nobody is waiting for them, so
#: the torrent is removed from the client with its files and the episode goes
#: back to ``not_wanted`` (:func:`cancel_if_unwanted`). Everything further on —
#: ``downloaded``, ``matching``, ``matched``, ``preparing``, ``ready`` — has
#: bytes that have *landed*, and those are retention's to measure and delete
#: (FR-T1): the file may be most of the way through a transcode, and the grace
#: period exists precisely so that a want disappearing is not the same event as
#: a file being thrown away.
CANCELLABLE: frozenset[EpisodeState] = frozenset({EpisodeState.DOWNLOADING})

#: The sentence every ending writes, here and in ``search_release``. One string
#: because they are one statement about the episode — FR-A2 merges wants, so it
#: is only ever said when the *last* user's want has gone.
NOBODY_WANTS = "nobody wants this episode"

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

    #: (user, episode) pairs the reconciliation asked for. Rows belonging to a
    #: show waiting for a slot (FR-A10) are not among them — they were left
    #: untouched rather than asked for — so this is what Arc *decided*, not a
    #: count of the table.
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
    #: Downloads in flight stopped because nothing wants them any more, each
    #: with a ``qbit_cancel`` job queued to remove it from the client.
    cancelled: int = 0
    #: Shows held back by the per-user slot cap this run (FR-A10): they had
    #: something to fetch and there was no slot free. Summed across users, so
    #: two users each waiting on one show is two.
    waiting: int = 0

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
            "cancelled": self.cancelled,
            "waiting": self.waiting,
        }


async def _episodes_by_anime(
    session: AsyncSession,
    anime_ids: set[int],
    floors: Mapping[int, int] | None = None,
) -> dict[int, list[Episode]]:
    """Every episode of these shows, grouped, in number order.

    ``floors`` is ``anime_id → the progress the window starts after``, and it
    is an optimisation with a proof rather than a heuristic. A show with 400
    entries behind it is thousands of rows, almost all of them episodes the
    user watched years ago, and the reconciler's ``window`` never asks about an
    episode at or below ``progress``.

    Why dropping them changes no answer: ``aired_through`` is a *max* over the
    dated episodes in the past, so leaving some out can only lower the boundary
    — and only when the highest such episode was itself at or below
    ``progress``. In that case the boundary was already ``<= progress``, and
    ``is_aired``'s ``number <= boundary`` is false for every episode the window
    considers either way; both lists then fall through to the episode's own air
    time, which is unchanged. The ``next_airing`` half of the boundary does not
    come from the list at all. So the narrowed read is equivalent, not merely
    cheaper.

    Only the one-user path passes it (:func:`slot_view`): the reconciler reads
    one show once for every user watching it, where the floor would have to be
    the lowest of their progresses and would save nothing worth the clause.
    """
    if not anime_ids:
        return {}
    stmt = select(Episode).where(Episode.anime_id.in_(anime_ids))
    started = {} if floors is None else {key: value for key, value in floors.items() if value > 0}
    if started:
        # Shows with no progress keep their whole list, which is why the
        # ``notin_`` arm is there: an OR of one clause per show would otherwise
        # have to name every show, started or not.
        stmt = stmt.where(
            or_(
                Episode.anime_id.notin_(started),
                *(
                    and_(Episode.anime_id == anime_id, Episode.number > number)
                    for anime_id, number in started.items()
                ),
            )
        )
    rows = await session.scalars(stmt.order_by(Episode.anime_id, Episode.number))
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


@dataclass(frozen=True, slots=True)
class _Plan:
    """What one pass over the lists says should be wanted, before the wants.

    Everything :func:`compute_wants` needs to reconcile, and everything the
    API needs to answer "why is this show not fetching?" — worked out by one
    function so the two can never disagree about it (FR-A10).
    """

    #: (user, episode) → the touch that decides a stale drop's revival.
    desired: dict[tuple[int, int], _Touch]
    #: Every (user, show) with a live, non-dormant watching/planned entry.
    wanting: set[tuple[int, int]]
    #: And the ones whose entry is dormant (FR-A9).
    dormant: set[tuple[int, int]]
    #: The (user, show) pairs the slot cap held back this run (FR-A10). Handed
    #: to :func:`_reconcile` as the set of pairs it must not touch at all.
    held: set[tuple[int, int]]
    #: The same thing keyed by user, which is what the API answers with.
    waiting: dict[int, set[int]]
    #: (user, show) → the episode number their window starts after. Read for
    #: the window, handed on because :func:`_reconcile` needs it to tell a
    #: held show's *watched-past* rows from the rest (FR-A10).
    progress: dict[tuple[int, int], int]
    #: user → how many of their shows are occupying a slot right now.
    fetching: dict[int, int]
    #: K as it stands; 0 is no cap.
    cap: int

    @property
    def held_back(self) -> int:
        """How many (user, show) pairs are waiting for a slot, in total."""
        return sum(len(shows) for shows in self.waiting.values())


@dataclass(frozen=True, slots=True)
class SlotView:
    """One user's standing under the slot cap, for the API (FR-A10).

    What a show page needs to say "waiting for a slot — 5 of your 5 shows are
    fetching": which of the user's shows are held back, how many are fetching,
    and what K is. Computed on demand and stored nowhere — the answer is a
    function of the wants, the windows and one setting, all of which move on
    their own.
    """

    #: Arc's anime ids, for the shows this user has waiting.
    waiting: frozenset[int]
    #: Shows of theirs occupying a slot: something is being searched for,
    #: downloaded or prepared. Samples do not count (FR-A8), and neither does
    #: an episode that has arrived or that FR-A6 has given up on.
    fetching: int
    #: K. 0 means there is no cap, in which case ``waiting`` is empty.
    cap: int
    #: Whether acquisition is paused, or holding itself for want of disk space
    #: (FR-T6). Either one makes "Arc starts this one when one of them
    #: finishes" false — nothing finishing will start anything, because
    #: ``compute_wants`` is not running or is not starting searches — so the
    #: page has to be able to say which of the three it is. They are carried
    #: rather than folded into ``waiting`` because the cap's answer is still
    #: the cap's answer: this is the show that waits when Arc runs again.
    paused: bool = False
    held: bool = False


async def _live_wants(
    session: AsyncSession, user_ids: Collection[int]
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """``(live (user, episode) pairs, (user, show) pairs occupying a slot)``.

    One query answers both halves of the slot cap's arithmetic. A pair
    "occupies a slot" when the user has a live, non-**sample** want on an
    episode that is actually in flight — one whose state is not
    :data:`~arc.services.acquisition.slots.SETTLED`. The three settled states
    are each a slot that would otherwise never come back: ``ready`` (arrived,
    waiting to be watched), ``unavailable`` (FR-A6 has given up and is retrying
    daily — five unfindable shows would freeze a whole list) and ``failed`` (a
    transcode that needs an admin, not a slot).

    ``live`` is every live want including the samples, and it is what decides
    whether a show is *hungry*: an episode somebody already has a row for is
    not something the show is waiting to ask for.
    """
    if not user_ids:
        return set(), set()
    rows = await session.execute(
        select(Want.user_id, Want.episode_id, Episode.anime_id, Episode.state, Want.sample)
        .join(Episode, Episode.id == Want.episode_id)
        .where(Want.dropped_at.is_(None), Want.user_id.in_(user_ids))
    )
    live: set[tuple[int, int]] = set()
    occupied: set[tuple[int, int]] = set()
    for user_id, episode_id, anime_id, state, sample in rows.all():
        live.add((user_id, episode_id))
        if not sample and state not in SETTLED:
            occupied.add((user_id, anime_id))
    return live, occupied


async def _plan_wants(
    session: AsyncSession,
    *,
    now: datetime,
    look_ahead: int,
    cap: int,
    user_ids: Collection[int] | None = None,
) -> _Plan:
    """Read every list and work out what ought to be wanted, in one pass.

    Three gates, in this order, and the order is the rule:

    1. **Dormancy** (FR-A9). An untouched import contributes nothing and does
       not join ``wanting``, so a want it left behind is shelved rather than
       deleted. It is not a candidate for a slot either — a show that is not
       fetching because nobody has picked it up is not a show waiting for
       room.
    2. **The window** (FR-A1). ``p+1 … p+N`` of the episodes that have aired.
    3. **The slot cap** (FR-A10). Per user, :func:`assign_slots` decides which
       shows may create wants this run. A show that is not admitted takes no
       part in the reconciliation at all: no want is created for it, and every
       row it already has is left exactly as it was found.

    That last sentence is deliberately stronger than "its live wants are
    kept", and the difference is a bug a review caught. A waiting show can be
    holding a want FR-T2 dropped for going unwatched — its episode is
    ``ready``, so the show is not an occupant, and the rest of its window is
    hungry enough to lose the slot race. Contributing only the *live* rows
    would leave that key out of ``desired`` while the show was still in
    ``wanting``, and :func:`_reconcile` **deletes** a want that has left the
    window of a wanting show: retention would lose the ``dropped_at`` it
    measures FR-T1's grace from (files deleted days early), and the row would
    come back live the moment a slot freed, skipping the Arc-side touch FR-T2's
    revival is supposed to need. So the pairs go to :func:`_reconcile` as a set
    of rows it must not touch, and the cap decides nothing about retention.

    ``user_ids`` narrows every query to one user, which is what the API's
    :func:`slot_view` asks for. ``None`` is the reconciler: all of them.
    """
    stmt = (
        select(ListEntry, Anime)
        .join(Anime, Anime.id == ListEntry.anime_id)
        .where(ListEntry.status.in_(WANTING_STATUSES))
    )
    if user_ids is not None:
        stmt = stmt.where(ListEntry.user_id.in_(user_ids))
    rows = await session.execute(stmt.order_by(ListEntry.user_id, ListEntry.anime_id))
    entries = list(rows.all())
    furthest = await _completed_through(
        session, {(entry.user_id, entry.anime_id) for entry, _ in entries}
    )
    # FR-W5's boundary, and the *same* function the watched marks are derived
    # from (:func:`~arc.services.playback.watched.watched_through`), so "how
    # far has this user got" cannot come to mean two things. Its docstring has
    # the one state in which it and the per-episode ``watched_source`` are
    # deliberately allowed to differ.
    progress_of = {
        (entry.user_id, entry.anime_id): watched_through(
            entry.progress, furthest.get((entry.user_id, entry.anime_id), 0)
        )
        for entry, _ in entries
    }
    episodes = await _episodes_by_anime(
        session,
        {anime.id for _, anime in entries},
        # One user, one progress per show, so the read can start where their
        # window does. See :func:`_episodes_by_anime` for why that is exact.
        floors=(
            {anime_id: number for (_, anime_id), number in progress_of.items()}
            if user_ids is not None
            else None
        ),
    )
    live, occupied = await _live_wants(session, {entry.user_id for entry, _ in entries})

    touches: dict[tuple[int, int], _Touch] = {}
    windows: dict[tuple[int, int], list[Episode]] = {}
    shows: dict[int, list[SlotShow]] = defaultdict(list)
    wanting: set[tuple[int, int]] = set()
    dormant: set[tuple[int, int]] = set()
    for entry, anime in entries:
        key = (entry.user_id, entry.anime_id)
        airing = anime.status == RELEASING
        if is_dormant(entry, airing=airing):
            # Not in ``wanting`` on purpose: a dormant entry's existing wants
            # are shelved rather than deleted, exactly like a show that has
            # left watching/planned, because retention still needs the moment
            # they stopped being wanted (FR-T1's grace anchor).
            dormant.add(key)
            continue
        wanting.add(key)
        touches[key] = _Touch(at=entry.updated_at, by=entry.updated_by)
        picked = window(
            episodes.get(anime.id, []),
            progress=progress_of[key],
            look_ahead=look_ahead,
            now=now,
            anime_status=anime.status,
            next_airing=anime.next_airing,
        )
        windows[key] = picked
        shows[entry.user_id].append(
            SlotShow(
                anime_id=entry.anime_id,
                airing=airing,
                updated_at=entry.updated_at,
                fetching=key in occupied,
                hungry=any((entry.user_id, episode.id) not in live for episode in picked),
            )
        )

    desired: dict[tuple[int, int], _Touch] = {}
    waiting: dict[int, set[int]] = {}
    fetching: dict[int, int] = {}
    held: set[tuple[int, int]] = set()
    for user_id, user_shows in shows.items():
        admitted, denied = assign_slots(user_shows, cap)
        if denied:
            waiting[user_id] = {show.anime_id for show in denied}
            held.update((user_id, show.anime_id) for show in denied)
        fetching[user_id] = sum(1 for show in user_shows if show.fetching)
        # Every show except the ones held back — which is *not* the same as
        # ``admitted``. A show with nothing to fetch is in neither of
        # ``assign_slots``'s lists (it is not competing and it is not waiting),
        # and it still has to contribute its window, or the live want on the
        # ``ready`` episode that made it settled would be deleted as "left the
        # window" on the next tick. Its window is exactly the rows it already
        # has, so this asks for nothing new.
        denied_ids = {show.anime_id for show in denied}
        for show in user_shows:
            if show.anime_id in denied_ids:
                continue
            key = (user_id, show.anime_id)
            for episode in windows[key]:
                desired[(user_id, episode.id)] = touches[key]

    if dormant:
        log.info("dormant list entries produced no wants", extra={"entries": len(dormant)})
    plan = _Plan(
        desired=desired,
        wanting=wanting,
        dormant=dormant,
        held=held,
        waiting=waiting,
        progress=progress_of,
        fetching=fetching,
        cap=cap,
    )
    if plan.held_back:
        log.info(
            "shows held back by the slot cap",
            extra={"shows": plan.held_back, "users": len(waiting), "cap": cap},
        )
    return plan


async def _current_plan(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    user_ids: Collection[int] | None = None,
) -> _Plan:
    """:func:`_plan_wants` with N and K read for you, in one query. Writes nothing."""
    look_ahead, cap = await look_ahead_and_cap(session)
    return await _plan_wants(
        session,
        now=now or datetime.now(UTC),
        look_ahead=look_ahead,
        cap=cap,
        user_ids=user_ids,
    )


async def slot_view(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> SlotView:
    """Where one user stands under the cap (FR-A10). Read-only.

    The same computation the reconciler runs, narrowed to one user, so "this
    show is waiting" on a show page and "this show creates no wants" in the
    next reconciliation are one predicate rather than two that agree most of
    the time. Nothing is persisted and nothing is cached: a slot frees itself
    when an episode becomes ready, which no request would be there to see.

    The pause and the storage hold are carried with it because the sentence
    the page writes depends on them. "Arc starts this one when one of them
    finishes" is a promise, and while acquisition is paused or held it is a
    false one — nothing finishing will start anything, because the reconciler
    is not running or is not starting searches. ``settings`` is what makes the
    disk measurable, exactly as in :func:`compute_wants`; without one there is
    no measurement and therefore no hold reported, which is the same answer a
    failed measurement gives.
    """
    plan = await _current_plan(session, now=now, user_ids=[user_id])
    return SlotView(
        waiting=frozenset(plan.waiting.get(user_id, set())),
        fetching=plan.fetching.get(user_id, 0),
        cap=plan.cap,
        paused=await is_paused(session),
        held=settings is not None and await is_storage_held(session, settings),
    )


async def slot_totals(session: AsyncSession, *, now: datetime | None = None) -> tuple[int, int]:
    """``(shows waiting for a slot across everybody, K)`` — the admin figure.

    With the pause, the storage hold and the dormant count, the fourth reason
    acquisition can look idle while nothing is wrong. **One** pass over every
    user's list, not one per user, and K comes back with it rather than through
    a second read of the same table: the panel asks for both together and a
    count without its cap says nothing.
    """
    plan = await _current_plan(session, now=now)
    return plan.held_back, plan.cap


async def compute_wants(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> WantsResult:
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

    A **storage hold** (FR-T6) is the opposite bargain, and deliberately so.
    Everything this function does when the disk is nearly full is something
    that frees space — a want dropped, a row shelved, a download nobody wants
    cancelled with its partial files — so the reconciliation runs in full and
    only the *starting* stops (:func:`_start_searches`). ``settings`` is what
    makes the disk measurable; without one there is no measurement, which is
    the same answer a failed measurement gives (never held). Every production
    caller is the ``compute_wants`` job handler, which passes its own.

    The **slot cap** (FR-A10) is the third brake and the only per-user one. It
    is applied inside :func:`_plan_wants`, between dormancy and ``desired``, so
    the whole computation stays one pass; ``result.waiting`` is how many shows
    it held back. Unlike the other two it never *undoes* anything: a show over
    the cap keeps the wants it has and gains none.
    """
    if await is_paused(session):
        log.info("acquisition paused; wants left untouched")
        return WantsResult()

    moment = now or datetime.now(UTC)
    held = settings is not None and await is_storage_held(session, settings)

    plan = await _current_plan(session, now=moment)
    # The samples are the one kind of want no list can account for, so they are
    # added to the plan rather than worked out inside it (FR-A8).
    desired = dict(plan.desired)
    desired.update(await _sample_wants(session, plan.wanting))

    result = await _reconcile(
        session,
        desired,
        plan.wanting,
        plan.dormant,
        plan.held,
        progress=plan.progress,
        now=moment,
    )
    result = replace(result, waiting=plan.held_back)
    result = await _drop_stale(session, result, now=moment)
    return await _start_searches(session, result, now=moment, held=held)


async def _sample_wants(
    session: AsyncSession, wanting: Collection[tuple[int, int]]
) -> dict[tuple[int, int], _Touch]:
    """The live "try episode 1" wants the list cannot account for (FR-A8).

    Only the ones whose (user, show) is **not** watching/planned: a sample on a
    followed show is the window's business, and adding it here would keep
    episode 1 wanted after the user had watched it.

    And only the ones the user has **not watched**. That second condition is
    what gives a sample an ending in the one case nothing else closes. A
    completed watch on an *unlisted* show makes the show ``watching`` (FR-S4),
    so the row leaves through ``wanting``; but a show already on the list as
    ``completed``, ``dropped`` or ``on_hold`` keeps that status — FR-S4 never
    overrides one — so (user, show) never joins ``wanting``, and FR-T2's stale
    drop excludes an episode its user has completed. Without this the row would
    be re-added to ``desired`` every quarter of an hour, retention would skip an
    episode with a live want, and a rewatched sample would pin its bytes for
    good. Watched and out of the window is the ordinary end of a want, so it
    ends the ordinary way: :func:`_reconcile` shelves it
    (:data:`REASON_NOT_WANTING`) and retention counts the grace period from the
    completion.

    The ``_Touch`` is built from the want's own ``created_at`` so the type is
    the one the rest of the reconciliation carries. It is never consulted: a
    live row in ``desired`` is already live, and only a *dropped* row's revival
    asks a touch anything — which is the one thing a dropped sample never gets
    (see the module docstring).
    """
    rows = await session.execute(
        select(Want, Episode.anime_id)
        .join(Episode, Episode.id == Want.episode_id)
        .outerjoin(
            WatchProgress,
            (WatchProgress.episode_id == Want.episode_id)
            & (WatchProgress.user_id == Want.user_id)
            & (WatchProgress.completed.is_(True)),
        )
        .where(
            Want.sample.is_(True),
            Want.dropped_at.is_(None),
            WatchProgress.user_id.is_(None),
        )
    )
    return {
        (want.user_id, want.episode_id): _Touch(at=want.created_at, by=UpdatedBy.ARC)
        for want, anime_id in rows.all()
        if (want.user_id, anime_id) not in wanting
    }


def _watched_past(want: Want, number: int, progress: int) -> bool:
    """Whether this row is one its user has finished with (FR-A10's exception).

    Live, not a sample, and on an episode at or below their progress. Only
    asked about a show the slot cap is holding back, where every other row is
    left untouched — :func:`_reconcile` has the argument for both halves.
    """
    return want.dropped_at is None and not want.sample and number <= progress


async def _reconcile(
    session: AsyncSession,
    desired: Mapping[tuple[int, int], _Touch],
    wanting: Collection[tuple[int, int]],
    dormant: Collection[tuple[int, int]] = (),
    held: Collection[tuple[int, int]] = (),
    *,
    progress: Mapping[tuple[int, int], int] | None = None,
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

    The second exception is a dropped **sample** want (FR-A8) on a show that is
    not watching/planned: it is left untouched, reason and all, because nothing
    here has anything to say about it. Rows inserted here are never samples —
    ``sample`` is a statement about how a want was created, and these were
    worked out from a list.

    ``held`` is FR-A10's cap, and it is very nearly the one argument that says
    **do nothing**. A (user, show) the cap kept out of ``desired`` this run is
    left alone: not shelved, not restamped, not revived, and not deleted. The
    rows such a show can be holding are precisely the ones with something to
    lose — a ``ready`` episode's want, a sample mid-download, and a want FR-T2
    dropped, whose ``dropped_at`` retention measures the grace period from and
    whose revival is supposed to require an Arc-side touch. A cap on how much
    is fetched at once has no business deciding any of that, and "the show has
    no slot today" is not "the user stopped wanting it".

    The one exception is the one ending that loses nothing: a **live,
    non-sample** want whose episode is at or below the user's ``progress``. The
    user has watched past it, so this is the ordinary "left the window" delete
    (see two paragraphs up) and the reason it is safe is the same — the
    completion is in ``watch_progress``, which is what retention measures
    FR-T1's grace from, so the row has nothing left to say. Without it a want
    the user finished would sit live until the show next won a slot, and a live
    want is enough to make the sweep skip the episode: a cap would end up
    pinning files to the disk, which is further from its job than anything else
    on this list. Dropped rows keep their ``dropped_at``, rows still inside the
    window are still wanted, and a sample is not the window's business — all
    three stay exactly as they are.

    ``dormant`` changes only the *sentence* a shelved row carries (FR-A9):
    :data:`~arc.services.acquisition.dormancy.REASON_DORMANT` rather than
    :data:`REASON_NOT_WANTING`, because "imported and never touched" and "you
    put this show on hold" are different facts about a row and an admin
    reading the wants table after an import needs to tell them apart. Both
    revive unconditionally, so nothing else about the row behaves differently
    — touching the show in Arc brings it straight back.
    """
    watched_through = progress or {}
    rows = await session.execute(
        select(Want, Episode.anime_id, Episode.number).join(Episode, Episode.id == Want.episode_id)
    )
    # The episode *number* is only read for the held-show exception below, and
    # it comes off this query rather than a second one: the join is already
    # here for the anime id.
    existing = {
        (want.user_id, want.episode_id): (want, anime_id, number)
        for want, anime_id, number in rows.all()
    }

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
    for key, (want, anime_id, number) in existing.items():
        if key in desired:
            continue
        pair = (key[0], anime_id)
        if pair in held:
            # FR-A10: waiting for a slot, so this row is none of our business —
            # unless the user has watched past it, which is the one ending that
            # takes nothing away (the docstring has the argument).
            if _watched_past(want, number, watched_through.get(pair, 0)):
                removed.append(key)
            continue
        if pair in wanting:
            removed.append(key)
            continue
        if want.sample and want.dropped_at is not None:
            # A dropped sample on a show nobody follows has finished (FR-A8):
            # its reason is the record of how, and re-labelling it
            # ``REASON_NOT_WANTING`` would both lose that and grant it the
            # unconditional revival that reason carries. A *live* sample never
            # reaches here — ``_sample_wants`` put it in ``desired``.
            continue
        if want.dropped_at is None:
            want.dropped_at = now
            shelved += 1
        want.drop_reason = REASON_DORMANT if pair in dormant else REASON_NOT_WANTING
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


async def start_search(
    session: AsyncSession,
    episode: Episode,
    *,
    now: datetime,
    newest_job: int = 0,
    retry_now: bool = False,
) -> tuple[bool, bool]:
    """Take one wanted episode from resting to ``wanted`` + a queued search.

    Returns ``(started, enqueued)``: whether the episode moved, and whether a
    *new* ``search_release`` row was written (a dedupe hit is not one —
    ``newest_job`` is the largest job id that existed before the caller began,
    which is how a fresh row is told from the one ``enqueue`` hands back).

    Does nothing to an episode that is not :data:`STARTABLE`: anything from
    ``searching`` onwards has work or bytes behind it, and telling it that
    somebody wants it would achieve nothing but an illegal transition.

    ``retry_now`` skips the :data:`UNAVAILABLE_RETRY` gate. The gate exists so
    that the fifteen-minute reconciliation does not re-open a search for an
    episode Nyaa has not had for a fortnight; a user pressing "try episode 1"
    (FR-A8) is not that sweep, and a person who has just asked for something is
    entitled to one more look today.

    This is the per-episode half of :func:`_start_searches`, extracted so that
    the sample routes act on the episode through the reconciler's own rule
    rather than through a second copy of it: two writers of ``episodes.state``
    that disagree about when an ``unavailable`` episode may be retried is a bug
    nobody would find until a fortnight had passed.
    """
    if episode.state not in STARTABLE:
        return False, False
    if (
        not retry_now
        and episode.state is EpisodeState.UNAVAILABLE
        and episode.state_changed_at is not None
        and now - episode.state_changed_at < UNAVAILABLE_RETRY
    ):
        return False, False
    transition(episode, EpisodeState.WANTED, reason="a user wants this episode")
    # ``enqueue`` does the dedupe itself and returns the row that already
    # holds the key, so asking first would be the same query twice.
    job = await enqueue(
        session,
        SEARCH_RELEASE,
        {"episode_id": episode.id},
        priority=SEARCH_RELEASE_PRIORITY,
        dedupe_key=search_dedupe_key(episode.id),
    )
    return True, job.id > newest_job


async def release_if_unwanted(
    session: AsyncSession, episode: Episode, *, wanted_ids: Collection[int] | None = None
) -> bool:
    """Put an episode nobody wants any more back to ``not_wanted``.

    Only from :data:`RELEASABLE` — the states with no bytes behind them — and
    only when no live want of *any* user is left on it (FR-A2 merges wants, so
    one user backing out is not the last word). An episode that is already
    downloading or further along is untouched: a cancel is not a reason to
    throw away work somebody else may still be waiting for.

    ``wanted_ids`` is the caller's own answer to "which episodes have live
    wants", for a caller that has just read it for a whole sweep; without it
    this asks about the one episode. Either way the *rule* is here and only
    here — the other half extracted from :func:`_start_searches`, for the same
    reason as :func:`start_search`.
    """
    if episode.state not in RELEASABLE:
        return False
    if await _still_wanted(session, episode, wanted_ids):
        return False
    transition(episode, EpisodeState.NOT_WANTED, reason=NOBODY_WANTS)
    return True


async def _still_wanted(
    session: AsyncSession, episode: Episode, wanted_ids: Collection[int] | None
) -> bool:
    """Whether any user still has a live want on this episode (FR-A2).

    ``wanted_ids`` is a sweep's own answer for every episode at once; without
    one this asks about the single episode. Shared by the two endings so that
    "nobody wants it" means the same thing in both.
    """
    if wanted_ids is not None:
        return episode.id in wanted_ids
    found = await session.scalar(
        select(Want.user_id)
        .where(Want.episode_id == episode.id, Want.dropped_at.is_(None))
        .limit(1)
    )
    return found is not None


async def cancel_if_unwanted(
    session: AsyncSession, episode: Episode, *, wanted_ids: Collection[int] | None = None
) -> bool:
    """Stop downloading an episode nobody wants any more. ``True`` if it did.

    The counterpart of :func:`release_if_unwanted` one state further on. An
    episode in ``downloading`` has a torrent in the client pulling bytes onto
    the disk for a want that no longer exists — the show went on hold, the user
    watched past it, a sample was cancelled — and before this it kept pulling
    them: ``poll_qbit`` only reacts to what the *client* does, and the
    reconciler only touched the states with nothing behind them. So the episode
    would finish downloading, transcode, and become ready for nobody, having
    held a download slot the whole way.

    Two writes here, and neither of them talks to qBittorrent. The episode goes
    back to ``not_wanted`` and the episode's **live** ``torrents`` row is marked
    :data:`~arc.services.acquisition.qbit.QBIT_CANCELLED`; the removal itself is
    a ``qbit_cancel`` job the caller queues, because this runs inside the
    reconciler's transaction and a transaction must not be held open across a
    request to another process. The mark is also the job's mandate: it deletes
    the hashes whose row says ``cancelled`` and nothing else, so an episode
    wanted again in the seconds in between keeps whatever release it has just
    chosen.

    **A row that already carries a decision is left alone**
    (:data:`~arc.services.acquisition.qbit.DECIDED_STATES`). An episode can
    reach ``downloading`` with older rows beside the live one — a release that
    stalled, or one whose delivered file a person rejected in review — and
    those are not this download. Re-labelling them ``cancelled`` would hand
    them to ``qbit_cancel``, which deletes with files: the rejected torrent is
    still holding the file sitting in somebody's review queue, and deleting it
    from under them is not what "nobody wants the next episode" should mean.

    Only from :data:`CANCELLABLE`. Anything with bytes already landed is
    retention's (FR-T1), and anything before ``downloading`` has nothing in the
    client to remove — :func:`release_if_unwanted` is that half.
    """
    if episode.state not in CANCELLABLE:
        return False
    if await _still_wanted(session, episode, wanted_ids):
        return False

    torrents = [
        torrent
        for torrent in (
            await session.scalars(select(Torrent).where(Torrent.episode_id == episode.id))
        ).all()
        if torrent.qbit_state not in DECIDED_STATES
    ]
    for torrent in torrents:
        torrent.qbit_state = QBIT_CANCELLED
    transition(episode, EpisodeState.NOT_WANTED, reason=NOBODY_WANTS)
    log.info(
        "download cancelled, nobody wants the episode",
        extra={
            "episode_id": episode.id,
            "hashes": [torrent.info_hash for torrent in torrents],
        },
    )
    return True


async def enqueue_cancel(session: AsyncSession, episode_id: int) -> None:
    """Queue the qBittorrent side of one cancellation, deduplicated."""
    await enqueue(
        session,
        QBIT_CANCEL,
        {"episode_id": episode_id},
        priority=QBIT_CANCEL_PRIORITY,
        dedupe_key=cancel_dedupe_key(episode_id),
    )


async def _start_searches(
    session: AsyncSession, result: WantsResult, *, now: datetime, held: bool = False
) -> WantsResult:
    """Move episodes into and out of ``wanted``, and queue the searches.

    An ``unavailable`` episode is only restarted once :data:`UNAVAILABLE_RETRY`
    has passed since it landed there, so a want that survives a fortnight of
    fruitless searching does not re-open one every quarter of an hour.

    ``held`` is FR-T6's storage guard, and it gates **only the starting half**.
    An episode with a live want is skipped outright while the disk is under the
    floor — skipped rather than released, because it is still wanted and saying
    otherwise would be a lie the show page would repeat — while the releases
    and the cancellations below run exactly as they always do. Those are what
    free space; refusing to do them while short of space would be the wrong way
    round.

    The per-episode decisions are :func:`start_search`,
    :func:`release_if_unwanted` and :func:`cancel_if_unwanted`; what is left
    here is the part that is only true of a *sweep* — which episodes are worth
    looking at at all, and the one read of the live wants that answers for all
    of them.

    The cancellations are collected and their jobs queued **after** the flush,
    which is the one piece of ordering that matters: the episode's state and
    the ``cancelled`` mark on its torrents are what the job reads, and a job
    row visible to a worker before the rows it is about would be a job that
    finds nothing to do. Nothing here talks to qBittorrent, so a client that is
    down cannot fail the reconciliation — the job carries that risk, with the
    runner's backoff behind it.
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
            select(Episode).where(
                or_(Episode.id.in_(live), Episode.state.in_(RELEASABLE | CANCELLABLE))
            )
        )
    ).all()

    started = searches = released = 0
    cancelled: list[int] = []
    for episode in interesting:
        if episode.id in with_wants:
            if held:
                continue
            moved, enqueued = await start_search(session, episode, now=now, newest_job=newest_job)
            started += int(moved)
            searches += int(enqueued)
        elif await release_if_unwanted(session, episode, wanted_ids=with_wants):
            released += 1
        elif await cancel_if_unwanted(session, episode, wanted_ids=with_wants):
            cancelled.append(episode.id)

    await session.flush()
    for episode_id in cancelled:
        await enqueue_cancel(session, episode_id)
    if cancelled:
        await session.flush()
    if started or released or cancelled:
        log.info(
            "acquisition window applied",
            extra={
                "started": started,
                "searches": searches,
                "released": released,
                "cancelled": len(cancelled),
                "held": held,
            },
        )
    elif held:
        log.info("acquisition held; no search started")
    return replace(
        result,
        started=started,
        searches=searches,
        released=released,
        cancelled=len(cancelled),
    )


__all__ = [
    "CANCELLABLE",
    "NOBODY_WANTS",
    "QBIT_CANCELLED",
    "REASON_NOT_WANTING",
    "RELEASABLE",
    "STALE_DROP_REASON",
    "STARTABLE",
    "UNAVAILABLE_RETRY",
    "WANTING_STATUSES",
    "SlotView",
    "WantsResult",
    "cancel_if_unwanted",
    "compute_wants",
    "enqueue_cancel",
    "enqueue_compute_wants",
    "release_if_unwanted",
    "slot_totals",
    "slot_view",
    "start_search",
    "window",
]
