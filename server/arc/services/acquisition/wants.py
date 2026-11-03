"""What Arc should be fetching, recomputed from scratch (FR-A1, FR-A2, FR-A14, FR-W4, FR-W7).

:func:`compute_wants` is a **reconciler**, not an event handler. It reads every
list entry, walk, rewatch pass and demand, works out the whole set of (user,
episode) pairs that ought to exist, and makes the ``wants`` table equal to it:
rows that should be there are inserted, rows that should not are deleted or
shelved. That is why it can be run on a timer, after a list change and after a
watch completion without any of those paths having to know what the others did,
and why running it twice changes nothing the second time.

M21 (owner 2026-11-03: "every episode on every show is playable when clicked
on") gives a want four sources, and they are the whole of CLAUDE.md's
acquisition non-negotiable — nothing outside them is ever fetched.

**(a) The list window** (FR-A1). For each non-dormant ``watching`` or
``planned`` entry with no rewatch pass open, take ``p`` — the larger of the
user's furthest completed episode and ``list_entries.progress``
(:func:`~arc.services.playback.watched.watched_through`, FR-W5's own boundary,
so the window and the watched marks are one rule) — and want ``p+1 … p+N`` of
the episodes that have **aired**. N is ``look_ahead_n``, deepened to
``look_ahead_slow`` for a thinly seeded show (:func:`show_look_ahead`), one
further while the user is half-way through ``p+1`` (the **halfway prefetch**,
:func:`_halfway`, outside the slot cap), minus what a device of the user holds
unwatched (the **device skip**, :func:`_device_held`: a delivered trip copy or a
Keep-offline download reported within :data:`DEVICE_HOLDING_DAYS`; the window
does not slide past it).

**(b) Walks and rewatch passes** (FR-A1, FR-W7). Once a user plays episode k
of *any* show, the same window runs from their **walk** (``walks.after_number``:
k−1 while k plays, k once it is finished) — with the show's own N, its own
prefetch and its own skip — for as long as the walk is live: touched within
**D** days (FR-T2's period), and not overtaken by an Arc-side status change to
dropped, on hold or completed made at or after its last move (an FR-W5
auto-complete included; :func:`walk_counts`). An open **rewatch pass** replaces
the list window with one counted from the pass's position (the highest number
finished since it began, 0 before the first) — the list window is *suspended*
while the pass is open — and only a walk that moved since the pass began
counts. A show's window is the **union** of the windows it has
(:func:`_plan_wants`), so a walk behind the list position adds its own few
episodes. Windows only ever want aired episodes (:func:`window`), whatever a
walk row says.

**(c) Demands** (FR-A14). Pressing play on any aired episode is a demand
(:mod:`arc.services.acquisition.demand`), and an open demand is wanted
**unconditionally** (:func:`_demanded_wants`): any list status, progress,
dormancy, slot cap, device holding, N = 0. It ends when the user finishes the
episode after the press (``finished_at >= demanded_at`` — the row is
**deleted**, so FR-T1's watched grace runs from the fresh finish), when it has
been untouched for D days (``greatest(demanded_at, watch_progress.updated_at)``,
dropped with :data:`STALE_DROP_REASON` in any episode state, its download
cancelled; only a new click revives it), or when the user withdraws it. A
finished or stale demand that a window, walk, pass or trip still asks for is
not ended that way: it loses ``demanded_at`` and lives on as an ordinary want
under that source's own rules. An open
demand is always a live want, so nothing that cancels on "nobody wants this"
(:func:`cancel_if_unwanted`, :func:`release_if_unwanted`, a trip's cancel or
release, a sample's cancel) ever stops one.

**(d) Trips** (FR-A12, :func:`_trip_wants`), the one source read from its own
rows. A trip episode a user also demanded is wanted for streaming too (no
longer trip-only).

Merging (FR-A2) is what the table shape does on its own: wants are keyed by
(user, episode), and the *episode* is what gets a state and a torrent. Three
users wanting episode 7 is three rows and one download.

Dormant imports (FR-A9). A ``watching``/``planned`` entry that arrived in a
MyAnimeList import and that nobody has touched in Arc since is **dormant**: it
gives no list window (:mod:`arc.services.acquisition.dormancy`). A walk or a
pass on the same show still counts — they are things the user did in Arc.

The slot cap (FR-A10) is the gate after the windows, and the only one that is
per *user*: at most **K** of one person's shows may be fetching at once
(default 5, 0 for no cap). A walk-only or rewatch show is a slot candidate
exactly like a listed one, its recency the later of the walk's last move and
the pass's start. Shows already fetching keep their slot; free slots go to
airing shows first and then to the most recently touched; the rest **wait** and
every row they already have is left as it was found. ``ready``, ``unavailable``
and ``failed`` episodes hold no slot; neither do the halfway prefetch, a demand
or a trip. :func:`slot_view` is the same computation answered for one user.

The storage guard (FR-T6) stops only :func:`_start_searches` putting episodes
into ``wanted``; every ending this module writes frees space and still runs.
The pause stops everything.

Following, deleting and shelving (FR-W4). A (user, show) is **followed** while
it has a list window (watching/planned, not dormant), a live walk or an open
rewatch pass. A row of a followed show that left its window — watched past, N
turned down — is **deleted**: the completion is retention's anchor. A row whose
show is no longer followed (on hold, dropped, completed, off the list, dormant,
a lapsed walk) is **dropped** (:data:`REASON_NOT_WANTING` /
:data:`~arc.services.acquisition.dormancy.REASON_DORMANT`) rather than deleted,
so retention keeps the moment it stopped (FR-T1's grace) and the show coming
back revives it. A row whose episode is still ``downloading`` and above the
user's position is not deleted merely because the window shrank (owner
2026-10-20), except at N = 0, the hard stop.

Cancelling (2026-09-13). A want going away also stops work already started: an
episode in ``wanted``/``searching``/``unavailable`` goes back to
``not_wanted`` (:func:`release_if_unwanted`), and one ``downloading`` has its
torrent removed with its files (:func:`cancel_if_unwanted`). From
``downloaded`` onwards the bytes are retention's.

Going stale (FR-T2). A window want on an episode that has been ``ready`` for
**D** days, unwatched, counted from the later of its readiness, the user's last
list change, their walk's last move and their open pass's start, is
**dropped** (:data:`STALE_DROP_REASON`). It is revived only when the user has
done something about the show **in Arc** since (an Arc-side list change, a
walk move, a pass start — :class:`_Touch`); a MyAnimeList import that moved a
score must not restart a download, and the MAL-side changes that should (a
progress or a status) revive it through the window or ``wanting`` without this
branch.

Samples (FR-A8) are retired (M21: a sample is a click on episode 1); the
``POST`` route answers 410. :func:`_sample_wants` stays only until the rows made
before M21 have ended: a live sample on a show nobody follows is kept as it is
until its user watches it, a dropped one is left with its reason.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    DeviceHolding,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    OfflineCopy,
    OfflineCopyState,
    Rendition,
    Rewatch,
    Torrent,
    TorrentFile,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    UpdatedBy,
    Walk,
    Want,
    WatchProgress,
)
from arc.services.acquisition.claims import live_claim, release_files
from arc.services.acquisition.dormancy import REASON_DORMANT, is_dormant
from arc.services.acquisition.names import (
    DEMAND_PRIORITY,
    QBIT_CANCEL,
    QBIT_CANCEL_PRIORITY,
    QBIT_TOP,
    QBIT_TOP_PRIORITY,
    SEARCH_RELEASE,
    SEARCH_RELEASE_PRIORITY,
    cancel_dedupe_key,
    enqueue_compute_wants,
    search_dedupe_key,
    top_dedupe_key,
)
from arc.services.acquisition.qbit import DECIDED_STATES, QBIT_CANCELLED
from arc.services.acquisition.rules import (
    is_paused,
    is_storage_held,
    window_rules,
)
from arc.services.acquisition.slots import SETTLED, SlotShow, assign_slots
from arc.services.acquisition.states import transition
from arc.services.catalog.airing import RELEASING, aired_through, is_aired
from arc.services.jobs.queue import ACTIVE_STATUSES, enqueue
from arc.services.media.copies import episodes_with_sources, file_matches
from arc.services.media.names import (
    OFFLINE_WHY_TRIP,
    enqueue_offline_encode,
    enqueue_remux,
    latest_offline_jobs,
    latest_transcode_jobs,
    offline_path_for,
)
from arc.services.playback.progress import COMPLETION_FRACTION, PREFETCH_FRACTION
from arc.services.playback.watched import watched_through
from arc.services.retention.rules import unwatched_period
from arc.services.trips.names import TRIP_SEARCH_PRIORITY, enqueue_trip_release
from arc.services.trips.rules import delivered_to, pending_episode_ids, trip_copy_priority

log = logging.getLogger(__name__)

#: How many thinly seeded show ids the reconciliation names in its log line;
#: past this it gives only the count.
THIN_IDS_LOGGED: int = 10

#: Episode states whose want is **in flight** (owner 2026-10-20): deleting it
#: would cancel a download with its files. Such a want is not deleted merely
#: because the window shrank — a thin show that stopped counting as thin, or N
#: turned down to anything but 0 — and goes on the first run after the episode
#: leaves this state (:func:`_reconcile`). Not ``searching``: releasing a
#: search loses no bytes, and a kept one would keep FR-A6's retry loop alive
#: for an episode outside the window.
IN_FLIGHT: frozenset[EpisodeState] = frozenset({EpisodeState.DOWNLOADING})

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

#: What :func:`_reconcile` writes into ``wants.drop_reason`` when a row a trip
#: brought in stops being asked for and its show is not an admitted,
#: followed one (FR-A12). It revives unconditionally, like
#: :data:`REASON_NOT_WANTING`; the trip release ignores these rows when it
#: decides whether a cancelled trip's bytes may go at once.
REASON_TRIP_ENDED = "trip ended"

#: How long a device's report of what it holds keeps an episode out of the
#: user's window (FR-A15, owner 2026-10-13). Devices re-report on every launch
#: and reconnect; a report older than this is from a device that has been
#: wiped, lost or left in a drawer, and must not suppress fetches for ever.
DEVICE_HOLDING_DAYS = 14

#: The advisory lock :func:`compute_wants` and a demand (FR-A14) share
#: (:func:`lock_wants`). Folded into a signed 64-bit key from a fixed string,
#: like the other advisory keys in Arc.
WANTS_LOCK_KEY: int = int.from_bytes(b"ARCWANTS", "big") - (1 << 63)


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
    #: Trip-only episodes a normal want has arrived on while their source was
    #: still here: transcodes queued for them (FR-A12's promotion).
    promoted: int = 0
    #: Copies queued for pending trip episodes that had none (FR-A12).
    copies: int = 0
    #: Episodes that were trip-only before this run and are not any more: a
    #: torrent sent to the back of the client's queue for them is moved back to
    #: the top (FR-A12). Not a count, so not in :meth:`as_dict`.
    untripped: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, int]:
        return {
            "promoted": self.promoted,
            "copies": self.copies,
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


def show_look_ahead(look_ahead: int, look_ahead_slow: int, *, thin: bool) -> int:
    """The N one show's window uses (FR-A1, owner 2026-10-20).

    ``look_ahead_n`` for every show, deepened to ``look_ahead_slow`` for a
    thinly seeded one (:func:`_thin_shows`). N = 0 stays "fetch nothing", and a
    slow figure below N never shrinks anything.
    """
    if look_ahead <= 0:
        return 0
    return max(look_ahead, look_ahead_slow) if thin else look_ahead


def _thin_threshold(look_ahead: int, look_ahead_slow: int, slow_seeders: int) -> int:
    """The seeder figure :func:`_thin_shows` compares against, or 0 for "skip it".

    0 whenever thinness could not change any window: the setting is off, N is
    0, or the slow look-ahead is no deeper than N.
    """
    if slow_seeders <= 0 or look_ahead <= 0 or look_ahead_slow <= look_ahead:
        return 0
    return slow_seeders


async def _thin_shows(
    session: AsyncSession, anime_ids: Collection[int], *, below: int
) -> frozenset[int]:
    """The shows among ``anime_ids`` that are thinly seeded (FR-A1). Read-only.

    A show is thin when its newest ``torrents`` row **with a seeder figure**
    (``added_at`` then ``id``, both descending) recorded fewer than ``below``.
    A single counts through ``torrents.episode_id``, a batch through its
    ``torrent_files``; a re-fetch row whose figure was cleared (``seeders``
    null) is skipped rather than allowed to hide the last real one, and an
    unreadable batch tombstone has no files and so no show. Derived on every
    call and stored nowhere, like the halfway prefetch.

    One statement; none at all when ``below`` is 0 or there is nothing to ask
    about.
    """
    if below <= 0 or not anime_ids:
        return frozenset()
    ids = list(set(anime_ids))
    singles = (
        select(
            Episode.anime_id.label("anime_id"),
            Torrent.added_at.label("added_at"),
            Torrent.id.label("torrent_id"),
            Torrent.seeders.label("seeders"),
        )
        .join(Episode, Episode.id == Torrent.episode_id)
        .where(Torrent.seeders.is_not(None), Episode.anime_id.in_(ids))
    )
    batches = (
        select(
            Episode.anime_id.label("anime_id"),
            Torrent.added_at.label("added_at"),
            Torrent.id.label("torrent_id"),
            Torrent.seeders.label("seeders"),
        )
        .join(TorrentFile, TorrentFile.torrent_id == Torrent.id)
        .join(Episode, Episode.id == TorrentFile.episode_id)
        .where(Torrent.seeders.is_not(None), Episode.anime_id.in_(ids))
    )
    rows = union_all(singles, batches).subquery()
    newest = (
        select(rows.c.anime_id, rows.c.seeders)
        .distinct(rows.c.anime_id)
        .order_by(rows.c.anime_id, rows.c.added_at.desc(), rows.c.torrent_id.desc())
    )
    return frozenset(
        anime_id for anime_id, seeders in (await session.execute(newest)).all() if seeders < below
    )


def window(
    episodes: list[Episode],
    *,
    progress: int,
    look_ahead: int,
    now: datetime,
    anime_status: str | None,
    next_airing: dict[str, object] | None,
    held: frozenset[int] = frozenset(),
    prefetch: bool = False,
) -> list[Episode]:
    """The next ``look_ahead`` aired episodes after ``progress`` (FR-A1).

    Pure, so the table of progress/N/aired combinations in the tests is the
    specification of the rule rather than a description of it.

    ``prefetch`` is the halfway rule (M20): the user is at least half-way
    through the first episode of the window, so the window reaches one
    further, ``p+1 … p+N+1``. With N = 0 there is no window to extend.

    ``held`` is the device skip (FR-A15, owner 2026-10-13, reversing the
    2026-10-06 "not left out"): episode **ids** a device of the user holds
    unwatched — a delivered trip copy or a reported Keep-offline download. They
    are taken out of the candidates and the window is **not** extended past
    them: a binge on an iPad that holds 16–20 must not make the server fetch
    21. Pressing play on one of them elsewhere is a demanded want
    (FR-A14), which the reconciler adds on its own.
    """
    if look_ahead <= 0:
        return []
    reach = progress + look_ahead + (1 if prefetch else 0)
    boundary = aired_through(episodes, now=now, anime_status=anime_status, next_airing=next_airing)
    picked: list[Episode] = []
    for episode in episodes:
        if episode.number <= progress:
            continue
        if episode.number > reach:
            break
        if episode.id is not None and episode.id in held:
            # Skipped, and still counted against the reach above: the window
            # does not slide past what the device already has.
            continue
        if not is_aired(episode, now=now, anime_status=anime_status, boundary=boundary):
            # Not `break`: episode lists are ordered but not always complete,
            # and one episode the rule cannot place must not hide the aired
            # episode after it. The bound above is what stops the loop.
            continue
        picked.append(episode)
    return picked


@dataclass(frozen=True, slots=True)
class _Plan:
    """What one pass over the lists, walks and passes says should be wanted.

    Everything :func:`compute_wants` needs to reconcile, and everything the
    API needs to answer "why is this show not fetching?" — worked out by one
    function so the two can never disagree about it (FR-A10).
    """

    #: (user, episode) → the touch that decides a stale drop's revival.
    desired: dict[tuple[int, int], _Touch]
    #: Every (user, show) with a live, non-dormant watching/planned entry.
    wanting: set[tuple[int, int]]
    #: And the ones whose entry is dormant (FR-A9) and that nothing else
    #: follows: a walk or a pass on a dormant show makes it followed.
    dormant: set[tuple[int, int]]
    #: The (user, show) pairs the slot cap held back this run (FR-A10). Handed
    #: to :func:`_reconcile` as the set of pairs it must not touch at all.
    held: set[tuple[int, int]]
    #: The same thing keyed by user, which is what the API answers with.
    waiting: dict[int, set[int]]
    #: (user, show) → the lowest position a window of theirs starts after
    #: (list or pass, and walk). Handed on because :func:`_reconcile` needs it
    #: to tell a *watched-past* row from the rest (FR-A10, the in-flight rule).
    progress: dict[tuple[int, int], int]
    #: user → how many of their shows are occupying a slot right now.
    fetching: dict[int, int]
    #: K as it stands; 0 is no cap.
    cap: int
    #: user → the episodes their windows cover **before** the device skip and
    #: the slot cap. Read only by :func:`coverable` for the API's
    #: ``fetchable`` until M21 T4 retires both; never used to create a want.
    covers: Mapping[int, frozenset[int]] = field(default_factory=dict)
    #: The shows whose window this run was deepened to ``look_ahead_slow``
    #: because they are thinly seeded (:func:`_thin_shows`, FR-A1).
    thin: frozenset[int] = frozenset()
    #: N as it stands (``look_ahead_n``). 0 is the hard stop: the in-flight
    #: rule in :func:`_reconcile` does not apply.
    look_ahead: int = 0
    #: Every (user, show) that is **followed** (M21): ``wanting`` ∪ a live walk
    #: ∪ an open rewatch pass. What :func:`_reconcile` asks when it chooses
    #: between deleting a row that left the window and shelving it.
    following: frozenset[tuple[int, int]] = frozenset()
    #: (user, episode) for every episode a held pair's window would want if the
    #: cap admitted it (its pre-cap window, prefetch included). What tells a
    #: held show's window rows from rows nothing asks for any more (a withdrawn
    #: demand), and what a stale demand on a held show counts as still wanted.
    held_windows: frozenset[tuple[int, int]] = frozenset()

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
) -> tuple[set[tuple[int, int]], dict[tuple[int, int], set[int]]]:
    """``(live (user, episode) pairs, (user, show) → the episodes occupying a slot)``.

    The occupants come back per episode rather than as a flag, because the
    halfway prefetch's episode (M20) never counts either: :func:`_plan_wants`
    takes it out once it knows which episode that is.

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

    **Trip wants are in neither** (FR-A12). Not in ``occupied``, because a
    trip never counts against K; and not in ``live`` either, because an
    episode wanted only for a device is still one the show's *window* is
    waiting to ask for — leaving it out of "hungry" would let the window take
    it over without competing for a slot, which is the cap bypassed by the back
    door. ``wants.trip`` is the previous run's answer, which is the one there
    is at planning time.

    **A demanded want** (FR-A14) is treated like a sample: it is ``live`` (the
    show is not waiting to ask for that episode — the user already asked) and
    it never makes the show an occupant. "A demanded episode never counts"
    against K is the rule, and it is what lets play on demand bypass the cap
    without the cap then counting the bypass against the user's other shows.
    """
    if not user_ids:
        return set(), {}
    rows = await session.execute(
        select(
            Want.user_id,
            Want.episode_id,
            Episode.anime_id,
            Episode.state,
            Want.sample,
            Want.trip,
            Want.demanded_at,
        )
        .join(Episode, Episode.id == Want.episode_id)
        .where(Want.dropped_at.is_(None), Want.user_id.in_(user_ids))
    )
    live: set[tuple[int, int]] = set()
    occupied: dict[tuple[int, int], set[int]] = defaultdict(set)
    for user_id, episode_id, anime_id, state, sample, trip, demanded_at in rows.all():
        if trip:
            continue
        live.add((user_id, episode_id))
        if not sample and demanded_at is None and state not in SETTLED:
            occupied[(user_id, anime_id)].add(episode_id)
    return live, dict(occupied)


async def _device_held(
    session: AsyncSession, user_ids: Collection[int], *, now: datetime
) -> dict[int, frozenset[int]]:
    """``user → episode ids a device of theirs holds unwatched`` (FR-A15).

    Two sources, unioned: a trip copy the device confirmed and has not
    released (:func:`~arc.services.trips.rules.delivered_to`), and a
    Keep-offline download a device reported within :data:`DEVICE_HOLDING_DAYS`
    (``device_holdings``). Watched episodes need no special case: the window
    starts after the user's progress.
    """
    if not user_ids:
        return {}
    held: dict[int, set[int]] = defaultdict(set)
    for user_id, episode_ids in (await delivered_to(session, user_ids)).items():
        held[user_id].update(episode_ids)
    cutoff = now - timedelta(days=DEVICE_HOLDING_DAYS)
    rows = await session.execute(
        select(DeviceHolding.user_id, DeviceHolding.episode_id)
        .where(DeviceHolding.user_id.in_(set(user_ids)), DeviceHolding.reported_at >= cutoff)
        .distinct()
    )
    for user_id, episode_id in rows.all():
        held[user_id].add(episode_id)
    return {user_id: frozenset(ids) for user_id, ids in held.items()}


def is_halfway(position_s: float, duration_s: float | None) -> bool:
    """The halfway prefetch's band: ``0.5 ≤ position/duration < 0.9`` (M20).

    Below it the user has not committed to the episode; at or above the
    completion fraction the episode is watched and the window has moved on its
    own. A non-positive duration (a manual mark's ``0``/``0``) is never
    halfway.
    """
    if duration_s is None or duration_s <= 0:
        return False
    fraction = position_s / duration_s
    return PREFETCH_FRACTION <= fraction < COMPLETION_FRACTION


async def _halfway(
    session: AsyncSession, pairs: Collection[tuple[int, int]]
) -> set[tuple[int, int]]:
    """Which ``(user, episode)`` pairs are in the halfway band (:func:`is_halfway`).

    ``pairs`` is each (user, show)'s *first* window episode — the one the
    prefetch is measured on. One query for the whole reconciliation.
    """
    if not pairs:
        return set()
    rows = await session.execute(
        select(
            WatchProgress.user_id,
            WatchProgress.episode_id,
            WatchProgress.position_s,
            WatchProgress.duration_s,
        ).where(tuple_(WatchProgress.user_id, WatchProgress.episode_id).in_(list(pairs)))
    )
    return {
        (user_id, episode_id)
        for user_id, episode_id, position_s, duration_s in rows.all()
        if is_halfway(position_s, duration_s)
    }


def walk_counts(touched_at: datetime, *, pass_started_at: datetime | None) -> bool:
    """Whether a walk touched within D days opens a window (FR-A1, M21). Pure.

    With a rewatch pass open, only a walk that moved since the pass began
    counts (FR-W7): the pass has its own position, and a walk left from before
    it is about another watch. Nothing about the list entry is read here: the
    user setting the show dropped, on hold or completed, FR-W5's auto-complete
    and a pass ending **delete** the walk where they happen
    (:mod:`arc.services.acquisition.walks`), because FR-S4's list advance
    stamps the entry in the very instant a finishing report moves the walk.
    """
    if pass_started_at is not None:
        return touched_at >= pass_started_at
    return True


async def _live_walks(
    session: AsyncSession, *, since: datetime, user_ids: Collection[int] | None
) -> dict[tuple[int, int], Walk]:
    """``(user, show) → walk`` for every walk touched at or after ``since`` (D days)."""
    stmt = select(Walk).where(Walk.touched_at >= since)
    if user_ids is not None:
        stmt = stmt.where(Walk.user_id.in_(user_ids))
    return {(walk.user_id, walk.anime_id): walk for walk in (await session.scalars(stmt)).all()}


async def _open_passes(
    session: AsyncSession, user_ids: Collection[int] | None
) -> dict[tuple[int, int], tuple[datetime, int]]:
    """``(user, show) → (pass start, pass position)`` for every open rewatch pass.

    The position is :func:`~arc.services.playback.rewatch.pass_position`'s rule
    — the highest number whose latest finish is at or after the pass's start,
    0 before the first — answered for every pass in two queries.
    """
    stmt = select(Rewatch).where(Rewatch.ended_at.is_(None))
    if user_ids is not None:
        stmt = stmt.where(Rewatch.user_id.in_(user_ids))
    passes = list((await session.scalars(stmt)).all())
    if not passes:
        return {}
    rows = await session.execute(
        select(Rewatch.id, func.max(Episode.number))
        .join(Episode, Episode.anime_id == Rewatch.anime_id)
        .join(
            WatchProgress,
            (WatchProgress.episode_id == Episode.id)
            & (WatchProgress.user_id == Rewatch.user_id)
            & (WatchProgress.finished_at >= Rewatch.started_at),
        )
        .where(Rewatch.id.in_([rewatch.id for rewatch in passes]))
        .group_by(Rewatch.id)
    )
    positions = {rewatch_id: int(number or 0) for rewatch_id, number in rows.all()}
    return {
        (rewatch.user_id, rewatch.anime_id): (rewatch.started_at, positions.get(rewatch.id, 0))
        for rewatch in passes
    }


def _pair_touch(
    entry: ListEntry | None, *, walk_at: datetime | None, pass_at: datetime | None
) -> _Touch:
    """The touch a (user, show)'s window rows carry: its latest Arc-side moment.

    A walk's move and a pass's start are the user acting on the show in Arc,
    exactly as an Arc-side list change is (FR-T2's revival). The entry's own
    touch stands when it is the only one, whichever side made it.
    """
    arc_side = [moment for moment in (walk_at, pass_at) if moment is not None]
    if entry is not None and entry.updated_by is UpdatedBy.ARC:
        arc_side.append(entry.updated_at)
    if arc_side:
        return _Touch(at=max(arc_side), by=UpdatedBy.ARC)
    assert entry is not None, "a pair with no walk and no pass has a list entry"
    return _Touch(at=entry.updated_at, by=entry.updated_by)


async def _plan_wants(
    session: AsyncSession,
    *,
    now: datetime,
    look_ahead: int,
    cap: int,
    stale: timedelta,
    look_ahead_slow: int = 0,
    slow_seeders: int = 0,
    user_ids: Collection[int] | None = None,
) -> _Plan:
    """Read every list, walk and pass and work out what ought to be wanted, in one pass.

    Three gates, in this order, and the order is the rule:

    1. **Who follows what** (FR-A1, FR-A9, FR-W7). Per (user, show), the base
       position is the open rewatch pass's (the list window **suspended**), else
       ``watched_through(list progress, highest completed)`` for a non-dormant
       watching/planned entry, else none; the walk position is
       ``walks.after_number`` while :func:`walk_counts` says the walk is live
       and it was touched within ``stale`` (D). A pair with neither is not
       followed and contributes nothing — a dormant entry with no walk and no
       pass is exactly that, so a want it left behind is shelved.
    2. **The windows** (FR-A1). For each position present, ``p+1 … p+N`` of
       the episodes that have aired — N being ``look_ahead_slow`` for a thinly
       seeded show (:func:`_thin_shows`) — one further while the user is
       half-way through that window's first episode (:func:`_halfway`), minus
       what a device of theirs holds (:func:`_device_held`), without sliding
       past it. The show's window is the union.
    3. **The slot cap** (FR-A10). Per user, :func:`assign_slots` decides which
       followed shows may create wants this run. A show that is not admitted
       takes no part in the reconciliation at all except for its halfway
       prefetch: no want is created for it, and every row it already has is
       left exactly as it was found (the pairs go to :func:`_reconcile` as
       ``held``). That is stronger than "its live wants are kept" on purpose:
       a waiting show can be holding a want FR-T2 dropped, whose ``dropped_at``
       retention measures FR-T1's grace from.

    Demands (FR-A14) and trips (FR-A12) are not planned here: they are added
    by :func:`compute_wants` after the cap has had its say.

    ``user_ids`` narrows every query to one user, which is what the API's
    :func:`slot_view` asks for. ``None`` is the reconciler: all of them.
    """
    stmt = select(ListEntry).where(ListEntry.status.in_(WANTING_STATUSES))
    if user_ids is not None:
        stmt = stmt.where(ListEntry.user_id.in_(user_ids))
    listed = {
        (entry.user_id, entry.anime_id): entry for entry in (await session.scalars(stmt)).all()
    }
    walks = await _live_walks(session, since=now - stale, user_ids=user_ids)
    passes = await _open_passes(session, user_ids)

    anime_ids = {anime_id for _, anime_id in (*listed, *walks, *passes)}
    shows_by_id: dict[int, Anime] = (
        {
            anime.id: anime
            for anime in (await session.scalars(select(Anime).where(Anime.id.in_(anime_ids)))).all()
        }
        if anime_ids
        else {}
    )
    furthest = await _completed_through(session, set(listed))

    # Gate 1: the positions, per (user, show).
    wanting: set[tuple[int, int]] = set()
    dormant_listed: set[tuple[int, int]] = set()
    base: dict[tuple[int, int], int] = {}
    for key, entry in listed.items():
        anime = shows_by_id.get(key[1])
        if anime is None:
            continue
        if is_dormant(entry, airing=anime.status == RELEASING):
            dormant_listed.add(key)
            continue
        wanting.add(key)
        if key not in passes:
            # FR-W5's boundary, and the *same* function the watched marks are
            # derived from, so "how far has this user got" means one thing.
            base[key] = watched_through(entry.progress, furthest.get(key, 0))
    for key, (_, position) in passes.items():
        if key[1] in shows_by_id:
            base[key] = position
    walking: dict[tuple[int, int], int] = {}
    for key, walk in walks.items():
        if key[1] not in shows_by_id:
            continue
        pass_started = passes[key][0] if key in passes else None
        if walk_counts(walk.touched_at, pass_started_at=pass_started):
            walking[key] = walk.after_number
    positions: dict[tuple[int, int], tuple[int, ...]] = {}
    for key in {*base, *walking}:
        found = {value for value in (base.get(key), walking.get(key)) if value is not None}
        positions[key] = tuple(sorted(found))
    following = frozenset(positions)
    progress_of = {key: found[0] for key, found in positions.items()}

    thin = await _thin_shows(
        session,
        {anime_id for _, anime_id in following},
        below=_thin_threshold(look_ahead, look_ahead_slow, slow_seeders),
    )
    floors: dict[int, int] = {}
    for (_, anime_id), lowest in progress_of.items():
        floors[anime_id] = min(lowest, floors.get(anime_id, lowest))
    episodes = await _episodes_by_anime(
        session,
        {anime_id for _, anime_id in following},
        # One user, so the read can start where their lowest window does. See
        # :func:`_episodes_by_anime` for why that is exact.
        floors=floors if user_ids is not None else None,
    )
    users = {user_id for user_id, _ in following}
    live, occupied = await _live_wants(session, users)
    device_held = await _device_held(session, users, now=now)
    # The first episode after each window's position: the one that window's
    # halfway prefetch is measured on. Read before the loop so the progress
    # rows for every window come back in one query.
    firsts: dict[tuple[int, int, int], int] = {}
    for key, starts in positions.items():
        rows = episodes.get(key[1], [])
        for position in starts:
            first = next((row for row in rows if row.number > position), None)
            if first is not None:
                firsts[(*key, position)] = first.id
    halfway = await _halfway(session, {(key[0], first) for key, first in firsts.items()})

    # Gate 2: the windows.
    touches: dict[tuple[int, int], _Touch] = {}
    windows: dict[tuple[int, int], list[Episode]] = {}
    extras: dict[tuple[int, int], list[Episode]] = {}
    covers: dict[int, set[int]] = defaultdict(set)
    shows: dict[int, list[SlotShow]] = defaultdict(list)
    for key in sorted(following):
        user_id, anime_id = key
        anime = shows_by_id[anime_id]
        rows = episodes.get(anime_id, [])
        walked_at = walks[key].touched_at if key in walking else None
        started = passes[key][0] if key in passes else None
        listed_entry = listed.get(key) if key in wanting else None
        touches[key] = _pair_touch(listed_entry, walk_at=walked_at, pass_at=started)
        n_show = show_look_ahead(look_ahead, look_ahead_slow, thin=anime_id in thin)
        picked: dict[int, Episode] = {}
        regular: set[int] = set()
        extra_ids: set[int] = set()
        # The regular window before the device skip: what an occupant has to
        # be fetching for the show to hold a slot (below).
        reach: set[int] = set()
        for position in positions[key]:
            first_id = firsts.get((user_id, anime_id, position))
            prefetch = first_id is not None and (user_id, first_id) in halfway
            unskipped = window(
                rows,
                progress=position,
                look_ahead=n_show,
                now=now,
                anime_status=anime.status,
                next_airing=anime.next_airing,
                prefetch=prefetch,
            )
            covers[user_id].update(episode.id for episode in unskipped)
            reach.update(episode.id for episode in unskipped if episode.number <= position + n_show)
            # The same window with the device skip applied (FR-A15): what is
            # actually fetched.
            for episode in window(
                rows,
                progress=position,
                look_ahead=n_show,
                now=now,
                anime_status=anime.status,
                next_airing=anime.next_airing,
                held=device_held.get(user_id, frozenset()),
                prefetch=prefetch,
            ):
                picked[episode.id] = episode
                # The halfway prefetch's episode (``p+N+1``) is outside the
                # slot cap: it never makes the show hungry, never makes it an
                # occupant, and is wanted even while the show waits — unless
                # another of the show's windows asks for it as a regular one.
                if episode.number > position + n_show:
                    extra_ids.add(episode.id)
                else:
                    regular.add(episode.id)
        extra_ids -= regular
        chosen = sorted(picked.values(), key=lambda episode: episode.number)
        windows[key] = chosen
        extras[key] = [episode for episode in chosen if episode.id in extra_ids]
        # The slot's recency (FR-A10): the list entry's last change for a list
        # window, the walk's last move, the pass's start — the latest present.
        recency = [moment for moment in (started, walked_at) if moment is not None]
        if listed_entry is not None and key not in passes:
            recency.append(listed_entry.updated_at)
        shows[user_id].append(
            SlotShow(
                anime_id=anime_id,
                airing=anime.status == RELEASING,
                updated_at=max(recency),
                # An occupant only for its window's episodes (M21): a download
                # nothing asks for any more — a withdrawn demand — must not
                # hold the show a slot, or the held-show rule could never see it.
                fetching=bool((occupied.get(key, set()) & reach) - extra_ids),
                hungry=any(
                    (user_id, episode.id) not in live
                    for episode in chosen
                    if episode.id not in extra_ids
                ),
            )
        )

    # Gate 3: the slot cap.
    desired: dict[tuple[int, int], _Touch] = {}
    waiting: dict[int, set[int]] = {}
    fetching: dict[int, int] = {}
    held: set[tuple[int, int]] = set()
    held_windows: set[tuple[int, int]] = set()
    for user_id, user_shows in shows.items():
        admitted, denied = assign_slots(user_shows, cap)
        if denied:
            waiting[user_id] = {show.anime_id for show in denied}
            held.update((user_id, show.anime_id) for show in denied)
        fetching[user_id] = sum(1 for show in user_shows if show.fetching)
        # Every show except the ones held back — which is *not* the same as
        # ``admitted``. A show with nothing to fetch is in neither of
        # ``assign_slots``'s lists, and it still has to contribute its window,
        # or the live want on the ``ready`` episode that made it settled would
        # be deleted as "left the window" on the next tick.
        denied_ids = {show.anime_id for show in denied}
        for show in user_shows:
            key = (user_id, show.anime_id)
            # A waiting show still gets its halfway prefetch (never refused).
            if show.anime_id in denied_ids:
                held_windows.update((user_id, episode.id) for episode in windows[key])
            chosen = extras[key] if show.anime_id in denied_ids else windows[key]
            for episode in chosen:
                desired[(user_id, episode.id)] = touches[key]

    dormant = dormant_listed - following
    if dormant:
        log.info("dormant list entries produced no wants", extra={"entries": len(dormant)})
    plan = _Plan(
        desired=desired,
        wanting=wanting,
        dormant=set(dormant),
        held=held,
        waiting=waiting,
        progress=progress_of,
        fetching=fetching,
        cap=cap,
        covers={user_id: frozenset(ids) for user_id, ids in covers.items()},
        thin=thin,
        look_ahead=look_ahead,
        following=following,
        held_windows=frozenset(held_windows),
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
    stale: timedelta | None = None,
    user_ids: Collection[int] | None = None,
) -> _Plan:
    """:func:`_plan_wants` with N, K and the thin-swarm pair read in one query.

    ``stale`` is FR-T2's D, the walk's lifetime; read when not given. Writes
    nothing.
    """
    rules = await window_rules(session)
    return await _plan_wants(
        session,
        now=now or datetime.now(UTC),
        look_ahead=rules.n,
        cap=rules.cap,
        stale=stale if stale is not None else await unwatched_period(session),
        look_ahead_slow=rules.slow_n,
        slow_seeders=rules.slow_seeders,
        user_ids=user_ids,
    )


async def coverable(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
) -> frozenset[int]:
    """Episode ids the M20 rule let this user fetch on demand (FR-A14). Read-only.

    **M21:** a demand no longer consults this — any aired episode may be
    demanded (:mod:`arc.services.acquisition.demand`). It stays only because
    the API's ``fetchable`` flags still read it; M21 T4 replaces them with
    "aired and not ready" and deletes this, :func:`coverable_once_active` and
    :attr:`_Plan.covers`. Since M21 the windows it reads include walks and
    rewatch passes.

    Their windows before the device skip and the slot cap — window ∪ halfway
    prefetch ∪ device-held, on every non-dormant watching/planned show — plus
    every episode they have a live want on **for another reason** (a sample, a
    trip episode, a window row). A demanded row does not justify itself: an
    earlier demand outside today's window is not a licence to demand it again.
    Nothing further ahead: a fetch outside the window would be a new kind of
    acquisition, and the non-negotiable allows none. Measured on the user's
    real progress: the halfway prefetch counts only once they are half-way.
    """
    plan = await _current_plan(session, now=now, user_ids=[user_id])
    wanted = (
        await session.scalars(
            select(Want.episode_id).where(
                Want.user_id == user_id,
                Want.dropped_at.is_(None),
                or_(Want.demanded_at.is_(None), Want.sample.is_(True), Want.trip.is_(True)),
            )
        )
    ).all()
    return plan.covers.get(user_id, frozenset()) | frozenset(wanted)


async def coverable_once_active(
    session: AsyncSession,
    user_id: int,
    anime: Anime,
    episodes: list[Episode],
    entry: ListEntry | None,
    *,
    now: datetime | None = None,
) -> frozenset[int]:
    """What :func:`coverable` will say about ``anime`` once the user touches it. Read-only.

    **M21:** kept only for ``next_season``'s ``fetchable`` until M21 T4
    deletes it with :func:`coverable` (see there).

    For the player's "Next season" (FR-S5 item 3): pressing it on a sequel
    whose entry is dormant (FR-A9) activates it, and on one that is not on the
    list adds it as Watching — and then demands its first episode (FR-A14). So
    the offer needs the answer the demand will get **after** that touch: this
    show's window, on the entry as the press leaves it (the status it has, or
    Watching for none), before the device skip and the slot cap, exactly as
    :func:`_plan_wants` builds ``covers`` — a thinly seeded show's deeper N
    included. The halfway prefetch is left out: it
    only ever reaches one further, and a season nobody has started is not half
    watched. A status that is neither watching nor planned covers nothing — a
    touch does not change it.

    ``episodes`` is the show's rows in number order (:func:`episodes_for`).
    Nothing is written and no window is acted on: this predicts a demand, it
    does not make one.
    """
    status = entry.status if entry is not None else ListStatus.WATCHING
    if status not in WANTING_STATUSES:
        return frozenset()
    rules = await window_rules(session)
    thin = await _thin_shows(
        session, {anime.id}, below=_thin_threshold(rules.n, rules.slow_n, rules.slow_seeders)
    )
    look_ahead = show_look_ahead(rules.n, rules.slow_n, thin=anime.id in thin)
    furthest = await _completed_through(session, {(user_id, anime.id)})
    progress = watched_through(
        entry.progress if entry is not None else 0, furthest.get((user_id, anime.id), 0)
    )
    covered = window(
        episodes,
        progress=progress,
        look_ahead=look_ahead,
        now=now or datetime.now(UTC),
        anime_status=anime.status,
        next_airing=anime.next_airing,
    )
    return frozenset(episode.id for episode in covered)


async def lock_wants(session: AsyncSession) -> None:
    """Serialise a reconciliation and a demand (FR-A14) for this transaction.

    A transaction-level advisory lock both take before they read: a demand
    committed between the reconciler's read of the demanded rows and its
    delete pass would otherwise be deleted as "left the window", and the
    demand's own ``compute_wants`` would dedupe against the very job doing it.
    Released at commit or rollback.
    """
    await session.execute(select(func.pg_advisory_xact_lock(WANTS_LOCK_KEY)))


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

    await lock_wants(session)
    # Wall-clock, not ``moment`` (which a caller may pin): what the demand
    # guard in :func:`_reconcile` compares ``demanded_at`` against.
    planned_at = datetime.now(UTC)
    stale = await unwatched_period(session)
    plan = await _current_plan(session, now=moment, stale=stale)
    if plan.thin:
        log.info(
            "thinly seeded shows get the deeper look-ahead",
            extra={
                "shows": len(plan.thin),
                "anime_ids": sorted(plan.thin) if len(plan.thin) <= THIN_IDS_LOGGED else None,
            },
        )
    # The samples are the one kind of want no list can account for, so they are
    # added to the plan rather than worked out inside it (FR-A8).
    desired = dict(plan.desired)
    desired.update(await _sample_wants(session, plan.following))
    # What a window, a walk, a pass or a sample asks for, before the demands.
    elsewhere = set(desired)
    # And the demanded ones (FR-A14, M21): an open demand is wanted whatever
    # the list, the windows, the device skip and the slot cap say, so it is
    # added after all of them have had their say.
    demands = await _demanded_wants(session, now=moment, stale=stale)
    desired.update(demands.open)
    # And the trips (FR-A12), the other kind no list accounts for. Passed
    # beside ``desired`` rather than merged into it, because a trip key that
    # nothing else asked for is a *trip-only* row and has to be marked so.
    trips = await _trip_wants(session)
    # A stale demand something else still asks for is not dropped (orchestrator
    # 2026-11-03): it loses ``demanded_at``, exactly like a finished one, and
    # lives on as an ordinary want under that source's own rules. Only a stale
    # demand nobody else wants is dropped and its download cancelled.
    elsewhere.update(trips)
    # The window a held show waits to be admitted with counts too: the slot
    # cap decides when a window episode starts, not whether it is wanted.
    elsewhere.update(plan.held_windows)
    stale_kept = demands.stale & elsewhere

    result = await _reconcile(
        session,
        desired,
        plan.following,
        plan.dormant,
        plan.held,
        progress=plan.progress,
        trips=trips,
        now=moment,
        planned_at=planned_at,
        keep_in_flight=plan.look_ahead > 0,
        finished=demands.finished,
        released=stale_kept,
        held_windows=plan.held_windows,
    )
    result = replace(result, waiting=plan.held_back)
    result = await _drop_stale(
        session,
        result,
        now=moment,
        period=stale,
        exclude=trips.keys(),
        stale_demands=demands.stale - stale_kept,
    )
    return await _start_searches(session, result, now=moment, held=held, settings=settings)


async def _trip_wants(session: AsyncSession) -> dict[tuple[int, int], _Touch]:
    """``(user, episode)`` for every episode an active trip is waiting on (FR-A12).

    ``pending`` rows of ``active`` trips, and only those: a delivered episode
    is on the device, an expired or cancelled one is not wanted any more, and
    a trip that has ended wants nothing. Like a sample, a trip is not derived
    from any list, so it is read off its own rows — and it **touches no list
    entry**: a dormant import stays dormant (FR-A9) and nothing here can lead
    to a MyAnimeList write.

    The ``_Touch`` is the trip's own creation, Arc-side: asking for a trip is
    the user acting on the show, so it may revive a want FR-T2 dropped before
    the trip was made, exactly as a list edit would.
    """
    # ``FOR SHARE OF trips``: a cancel takes the trip ``FOR UPDATE`` before it
    # writes, so a reconciliation that has read an active trip finishes with it
    # before the cancel can commit, and one that starts after the cancel reads
    # it cancelled. Without it a reconciliation that read the trip just before
    # the cancel committed would write trip wants for a trip that is over (they
    # would only be undone by the next run).
    rows = await session.execute(
        select(Trip.user_id, TripEpisode.episode_id, Trip.created_at)
        .join(Trip, Trip.id == TripEpisode.trip_id)
        .where(Trip.state == TripState.ACTIVE, TripEpisode.state == TripEpisodeState.PENDING)
        .with_for_update(of=Trip, read=True)
    )
    return {
        (user_id, episode_id): _Touch(at=created, by=UpdatedBy.ARC)
        for user_id, episode_id, created in rows.all()
    }


async def _sample_wants(
    session: AsyncSession, following: Collection[tuple[int, int]]
) -> dict[tuple[int, int], _Touch]:
    """The live "try episode 1" wants the list cannot account for (FR-A8).

    Only the ones whose (user, show) is **not followed** (M21: a list window,
    a live walk or an open pass, :attr:`_Plan.following`): a sample on a
    followed show is the window's business, and adding it here would keep
    episode 1 wanted after the user had watched it. So a pre-M21 live sample on
    a show the user has since walked is left to the walk's window, and once
    that window has moved past episode 1 the row is deleted like any
    watched-past window row.

    And only the ones the user has **not watched**. That second condition is
    what gives a sample an ending in the one case nothing else closes. A
    completed watch on an *unlisted* show makes the show ``watching`` (FR-S4),
    so the row leaves through ``following``; but a show already on the list as
    ``completed``, ``dropped`` or ``on_hold`` keeps that status — FR-S4 never
    overrides one — so (user, show) need not be followed, and FR-T2's stale
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
        if (want.user_id, anime_id) not in following
    }


@dataclass(frozen=True, slots=True)
class _Demands:
    """Every live demand, sorted by how it stands (FR-A14, M21)."""

    #: The open ones: wanted unconditionally, stale ones included (they are
    #: kept live by :func:`_reconcile` so that :func:`_drop_stale` can drop
    #: them with FR-T2's reason rather than have them deleted or shelved).
    open: dict[tuple[int, int], _Touch]
    #: Finished since the press (``finished_at >= demanded_at``): the demand is
    #: over, and :func:`_reconcile` deletes the row unless something else
    #: still wants the episode, in which case it lives on as an ordinary want.
    finished: frozenset[tuple[int, int]]
    #: Open and untouched for D days: ``greatest(demanded_at,
    #: watch_progress.updated_at) < now − D``, in any episode state. Dropped by
    #: :func:`_drop_stale` only when nothing else asks for the episode; one a
    #: window, walk, pass, sample or trip still wants is handed to
    #: :func:`_reconcile` with the finished ones and loses ``demanded_at``.
    stale: frozenset[tuple[int, int]]


async def _demanded_wants(session: AsyncSession, *, now: datetime, stale: timedelta) -> _Demands:
    """The live demanded wants, as :class:`_Demands` (FR-A14, M21).

    A demand is in ``desired`` **unconditionally** — whatever the list status,
    progress, dormancy, slot cap, device holding or N — so no list-derived rule
    can shelve, delete or cancel it. It ends in one of three ways: the user
    finishes the episode after pressing play (``finished``: deleted, not
    shelved — a drop anchor would keep the file G days, where the fresh
    ``finished_at`` gives retention's watched grace), it goes untouched for D
    days (``stale``: dropped by :func:`_drop_stale`, and only a new click
    revives it), or the user withdraws it (``demanded_at`` cleared by
    :func:`~arc.services.acquisition.demand.withdraw`, after which it is not
    read here at all).

    A demanded trip episode is an ordinary open demand: it enters ``desired``,
    so its row is not trip-only and it gets a streaming rendition.

    Only live rows: a dropped demand is not a demand until it is pressed again.
    """
    rows = await session.execute(
        select(
            Want.user_id,
            Want.episode_id,
            Want.demanded_at,
            WatchProgress.finished_at,
            WatchProgress.updated_at,
        )
        .outerjoin(
            WatchProgress,
            (WatchProgress.user_id == Want.user_id) & (WatchProgress.episode_id == Want.episode_id),
        )
        .where(Want.demanded_at.is_not(None), Want.dropped_at.is_(None))
    )
    cutoff = now - stale
    found: dict[tuple[int, int], _Touch] = {}
    finished: set[tuple[int, int]] = set()
    quiet: set[tuple[int, int]] = set()
    for user_id, episode_id, demanded_at, finished_at, played_at in rows.all():
        key = (user_id, episode_id)
        if finished_at is not None and finished_at >= demanded_at:
            finished.add(key)
            continue
        found[key] = _Touch(at=demanded_at, by=UpdatedBy.ARC)
        touched = demanded_at if played_at is None else max(demanded_at, played_at)
        if touched < cutoff:
            quiet.add(key)
    return _Demands(open=found, finished=frozenset(finished), stale=frozenset(quiet))


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
    following: Collection[tuple[int, int]],
    dormant: Collection[tuple[int, int]] = (),
    held: Collection[tuple[int, int]] = (),
    *,
    progress: Mapping[tuple[int, int], int] | None = None,
    trips: Mapping[tuple[int, int], _Touch] | None = None,
    now: datetime,
    planned_at: datetime | None = None,
    keep_in_flight: bool = False,
    finished: Collection[tuple[int, int]] = (),
    released: Collection[tuple[int, int]] = (),
    held_windows: Collection[tuple[int, int]] = (),
) -> WantsResult:
    """Make ``wants`` equal ``desired``: insert, revive, drop, delete.

    ``following`` (M21) is every (user, show) that is followed — a list
    window, a live walk or an open rewatch pass (:attr:`_Plan.following`). It
    is what the paragraphs below mean by "wanting": a row of a followed show
    that left its window is deleted, a row of any other show is shelved, so a
    lapsed walk's leftovers are shelved :data:`REASON_NOT_WANTING`.

    ``held_windows`` (M21) is the pre-cap windows of the held pairs
    (:attr:`_Plan.held_windows`). On a held pair a live, non-sample row with
    no demand that not even that window asks for — a withdrawn demand — is
    shelved :data:`REASON_NOT_WANTING`, so its download is cancelled rather
    than left holding the show a slot; every other held row is left as found.

    ``released`` is the stale demands something else still asks for
    (:func:`compute_wants`): they lose ``demanded_at`` wherever they are, held
    show included, and are never deleted here.

    ``finished`` (M21, FR-A14) is the demands finished since the press
    (:attr:`_Demands.finished`). A row
    nothing else asks for is **deleted** — whatever the show's status, held or
    not — so retention's watched grace runs from the fresh ``finished_at``
    rather than a drop anchor keeping the file G days; one something else
    still asks for loses its ``demanded_at`` and lives on as an ordinary want.
    A dropped row brought back by the window or a trip loses its
    ``demanded_at`` too: only a new click revives a demand.

    ``planned_at`` is when the plan was read (wall-clock). A **live** row whose
    ``demanded_at`` is later than that was demanded after the plan was made
    (FR-A14): it is left exactly as found rather than deleted or shelved on
    the strength of a plan that never saw it. The advisory lock
    (:func:`lock_wants`) is the first guard; this one is the second.

    ``trips`` is FR-A12's half (:func:`_trip_wants`). Every trip key is wanted
    too, and the ones nothing in ``desired`` asked for are **trip-only**: the
    row's ``trip`` flag is written to say so on every run, which is what makes
    the flag a fact about the current reconciliation rather than about how the
    row was first made — with one exception: a row that **already existed** on
    a show the slot cap is holding is never flagged (the held-show rule below:
    such rows are left exactly as they were found, and a flag would later make
    the trip's ending take the row with it). A trip-flagged row is therefore
    always one the trip itself brought in. When it leaves (the trip ended, or
    the episode was delivered) it is **deleted** if its show is still
    followed and not held — the ordinary "left the window" ending — and
    otherwise **shelved** with :data:`REASON_TRIP_ENDED`, held show included,
    so retention keeps the moment it stopped being wanted (FR-T1's anchor)
    and a held show does not keep an episode wanted for a device that no
    longer asks for it.

    **Leaving is two different things** (FR-W4). A row whose (user, show) is
    still followed (``following``: a list window, a live walk or an open pass)
    left the *window* — the user watched past it, or N was turned down — and
    is deleted: there is nothing to remember, and a completion (which is what
    retention will measure the grace period from) is already in
    ``watch_progress``. A row whose show is not followed at all stopped being
    wanted, and is **dropped**
    (:data:`REASON_NOT_WANTING`) so that retention has the moment it stopped.

    **An in-flight row is not deleted for leaving the window** (owner
    2026-10-20), when ``keep_in_flight`` is set — :func:`compute_wants` sets
    it whenever N is not 0, because N = 0 is the hard stop and cancels what is
    running as it always did. A live want on a followed show whose episode is
    :data:`IN_FLIGHT` (``downloading``) and **above** the user's progress is
    kept when the window no longer reaches it — the show stopped counting as
    thinly seeded, N was turned down, a device now holds it, a demand was
    withdrawn — because deleting it would cancel the download with its
    files, and a thin-swarm figure that moves with every tracker scrape would
    cancel and restart the same fetch. The first run after the episode is no
    longer ``downloading`` (``downloaded``/``matching``, before it is ready)
    deletes it as above, and from then on its files are retention's, as for
    any ready episode outside the window. A ``searching`` episode is not
    protected: releasing a search loses no bytes. Every other ending is
    unchanged: watched past is still deleted, and a show no longer followed
    (dormant, dropped, on hold, off the list, a lapsed walk, a pass ended) is
    still shelved and cancelled.

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
    trip_touches = trips or {}
    wanting = following
    over = set(finished)
    lapsed = set(released)
    waiting_for = set(held_windows)
    # Trip-only: a trip asked for it and nothing else did (FR-A12).
    trip_only = {key for key in trip_touches if key not in desired}
    wanted_now: dict[tuple[int, int], _Touch] = dict(desired)
    for key in trip_only:
        wanted_now[key] = trip_touches[key]
    rows = await session.execute(
        select(Want, Episode.anime_id, Episode.number, Episode.state).join(
            Episode, Episode.id == Want.episode_id
        )
    )
    # The episode *number* is read for the held-show exception and the
    # in-flight rule below, and the *state* for the latter; both come off this
    # query rather than a second one: the join is already here for the anime id.
    found_rows = rows.all()
    existing = {
        (want.user_id, want.episode_id): (want, anime_id, number)
        for want, anime_id, number, _ in found_rows
    }
    in_flight = {
        (want.user_id, want.episode_id) for want, _, _, state in found_rows if state in IN_FLIGHT
    }
    # Trip-only before this run (FR-A12): read before the loop rewrites the
    # flags, so the end of the run can say which stopped being so.
    live_trip: dict[int, bool] = {}
    for (_, episode_id), (want, _, _) in existing.items():
        if want.dropped_at is None:
            live_trip[episode_id] = live_trip.get(episode_id, True) and want.trip
    was_trip_only = {episode_id for episode_id, only in live_trip.items() if only}

    added = revived = 0
    for key, touch in wanted_now.items():
        is_trip = key in trip_only
        found = existing.get(key)
        if found is None:
            session.add(Want(user_id=key[0], episode_id=key[1], trip=is_trip))
            added += 1
            continue
        want = found[0]
        if is_trip and not want.trip and (key[0], found[1]) in held:
            # A row the held show already had: none of the trip's business
            # (FR-A10's "left exactly as found"; see the docstring).
            continue
        if want.trip != is_trip:
            want.trip = is_trip
        if (key in over or key in lapsed) and want.demanded_at is not None:
            # A finished demand something else still asks for: an ordinary
            # want from here on (it occupies a slot, searches at the window's
            # priority, and stops counting toward ``demand_max_open``).
            want.demanded_at = None
        if want.dropped_at is None:
            continue
        trip_touch = trip_touches.get(key)
        if (
            want.drop_reason == STALE_DROP_REASON
            and not touch.came_back_after(want.dropped_at)
            and not (trip_touch is not None and trip_touch.came_back_after(want.dropped_at))
        ):
            continue
        want.dropped_at = None
        want.drop_reason = None
        # A dropped demand comes back as an ordinary want: only a new click
        # makes it a demand again (FR-A14, FR-T2).
        want.demanded_at = None
        if is_trip and want.sample:
            # A cancelled sample brought back by a trip is the trip's row now:
            # left a sample, it would come back live on its own when the trip
            # ended, which is the one thing cancelling it ruled out (FR-A8).
            want.sample = False
        revived += 1

    removed: list[tuple[int, int]] = []
    shelved = 0
    for key, (want, anime_id, number) in existing.items():
        if key in wanted_now:
            continue
        if (
            planned_at is not None
            and want.dropped_at is None
            and want.demanded_at is not None
            and want.demanded_at > planned_at
        ):
            continue
        if key in lapsed:
            # A stale demand a held show's window still asks for: an ordinary
            # want from here on, and the held-show rule below keeps it.
            want.demanded_at = None
        if key in over and want.dropped_at is None:
            # Finished since the press (FR-A14, M21): deleted, never shelved —
            # on a held show, an unfollowed one or a trip's alike.
            removed.append(key)
            continue
        pair = (key[0], anime_id)
        if want.trip:
            # A row the trip brought in, which it no longer asks for (see
            # above): the window's ordinary delete on a followed, admitted
            # show; shelved everywhere else, held shows included.
            if pair in wanting and pair not in held:
                removed.append(key)
                continue
            if want.dropped_at is None:
                want.dropped_at = now
                shelved += 1
            want.drop_reason = REASON_TRIP_ENDED
            continue
        if pair in held:
            # FR-A10: waiting for a slot, so this row is none of our business —
            # unless the user has watched past it, which is the one ending that
            # takes nothing away (the docstring has the argument).
            if _watched_past(want, number, watched_through.get(pair, 0)):
                removed.append(key)
            elif (
                want.dropped_at is None
                and want.demanded_at is None
                and not want.sample
                and key not in waiting_for
            ):
                # Nothing asks for it, not even the window the show waits to
                # be admitted with — a withdrawn demand, say (M21). Shelved,
                # so its download stops rather than running on and holding
                # the show a slot.
                want.dropped_at = now
                want.drop_reason = REASON_NOT_WANTING
                shelved += 1
            continue
        if pair in wanting:
            if (
                keep_in_flight
                and key in in_flight
                and want.dropped_at is None
                and number > watched_through.get(pair, 0)
            ):
                # Downloading and not watched past (owner 2026-10-20): the
                # window shrank under it — the show stopped counting as thinly
                # seeded, or N was turned down (not to 0). Deleting the row
                # would cancel the download with its files; it is kept, and the
                # first run after the episode stops downloading deletes it.
                continue
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
    untripped: tuple[int, ...] = ()
    if was_trip_only:
        untripped = tuple(
            sorted(
                (
                    await session.scalars(
                        select(Want.episode_id)
                        .where(Want.episode_id.in_(was_trip_only), Want.dropped_at.is_(None))
                        .group_by(Want.episode_id)
                        .having(~func.bool_and(Want.trip))
                    )
                ).all()
            )
        )

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
        wanted=len(wanted_now),
        added=added,
        removed=len(removed),
        shelved=shelved,
        revived=revived,
        untripped=untripped,
    )


async def _drop_stale(
    session: AsyncSession,
    result: WantsResult,
    *,
    now: datetime,
    period: timedelta | None = None,
    exclude: Collection[tuple[int, int]] = (),
    stale_demands: Collection[tuple[int, int]] = (),
) -> WantsResult:
    """Drop wants on episodes that have sat ready and unwatched for D days (FR-T2).

    Two kinds of row, two clocks (M21). An **open demand** (FR-A14) is in
    ``stale_demands`` when :func:`_demanded_wants` found it untouched for D
    days — neither pressed again nor played — and nothing else asks for it;
    in **any** episode state it ends here, so a download nobody came back for
    is cancelled by the sweep that follows: dropped when the episode is
    ``ready`` (retention's anchor), **deleted** otherwise (nothing to anchor,
    and a dropped row would stop a later window fetching the episode). The
    rule below does not look at demands at all. Every other row is a
    window's, and the rest of this docstring is about those.
    ``period`` is D, read when not given.

    Never a trip's (FR-A12): neither a trip-only row nor any (user, episode)
    an active trip is waiting on (``exclude``). A trip is the user saying they
    mean to watch it later, somewhere else — the opposite of what FR-T2 is
    trying to detect — and dropping a key the trip has just revived would make
    every reconciliation revive and drop it again.

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
    window_d = period if period is not None else await unwatched_period(session)
    cutoff = now - window_d
    ready_since = func.coalesce(Rendition.ready_at, Episode.state_changed_at)
    # ``greatest`` ignores nulls in Postgres, so a want whose list entry has
    # gone (the reconciler is about to delete the row) falls back to the
    # episode's own readiness rather than never going stale. Since M21 the
    # walk's last move and the open pass's start restart the clock too: a
    # user playing the show is plainly still at it.
    quiet_since = func.greatest(
        ready_since, ListEntry.updated_at, Walk.touched_at, Rewatch.started_at
    )

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
                Walk,
                (Walk.user_id == Want.user_id) & (Walk.anime_id == Episode.anime_id),
            )
            .outerjoin(
                Rewatch,
                (Rewatch.user_id == Want.user_id)
                & (Rewatch.anime_id == Episode.anime_id)
                & Rewatch.ended_at.is_(None),
            )
            .outerjoin(
                WatchProgress,
                (WatchProgress.episode_id == Want.episode_id)
                & (WatchProgress.user_id == Want.user_id)
                & (WatchProgress.completed.is_(True)),
            )
            .where(
                Want.dropped_at.is_(None),
                Want.trip.is_(False),
                Want.demanded_at.is_(None),
                Episode.state == EpisodeState.READY,
                WatchProgress.user_id.is_(None),
                ready_since.is_not(None),
                quiet_since < cutoff,
            )
        )
    ).scalars()

    skip = set(exclude)
    found = list(rows.all())
    quiet = [key for key in stale_demands if key not in skip]
    gone: list[Want] = []
    if quiet:
        for want, state in (
            await session.execute(
                select(Want, Episode.state)
                .join(Episode, Episode.id == Want.episode_id)
                .where(
                    tuple_(Want.user_id, Want.episode_id).in_(quiet),
                    Want.dropped_at.is_(None),
                    Want.demanded_at.is_not(None),
                )
            )
        ).all():
            # A stale demand on an episode that is not ``ready`` has no bytes
            # for retention to anchor, and a dropped row would keep a window
            # from fetching it later: deleted. A ready one is dropped.
            (found if state is EpisodeState.READY else gone).append(want)
    for want in gone:
        await session.delete(want)
        log.info(
            "stale demand deleted",
            extra={"user_id": want.user_id, "episode_id": want.episode_id},
        )
    dropped = 0
    for want in found:
        if (want.user_id, want.episode_id) in skip:
            continue
        want.dropped_at = now
        want.drop_reason = STALE_DROP_REASON
        dropped += 1
        log.info(
            "want dropped for going unwatched",
            extra={
                "user_id": want.user_id,
                "episode_id": want.episode_id,
                "demand": want.demanded_at is not None,
                "days": int(window_d.total_seconds() // 86400),
            },
        )
    if dropped or gone:
        await session.flush()
    return replace(result, dropped=dropped, removed=result.removed + len(gone))


async def start_search(
    session: AsyncSession,
    episode: Episode,
    *,
    now: datetime,
    newest_job: int = 0,
    retry_now: bool = False,
    priority: int = SEARCH_RELEASE_PRIORITY,
) -> tuple[bool, bool]:
    """Take one wanted episode from resting to ``wanted`` + a queued search.

    ``priority`` is the search's place in the queue: the window's own 150, a
    trip's 160 (FR-A12) for an episode only a trip wants, or a demand's 100
    (FR-A14) for an episode somebody pressed play on.

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
        priority=priority,
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

    An open demand (FR-A14) is a live want, so its episode is never released.
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

    **An episode downloading out of a batch takes the branch above the two
    writes** (FR-A11). Its bytes are arriving inside a pack several episodes
    share, and it has no ``torrents`` row of its own at all — a batch's
    ``episode_id`` is null, which is precisely what keeps the query below from
    reaching one. So the file is given back instead
    (:func:`~arc.services.acquisition.claims.release_files`): its
    ``torrent_files`` row stops being wanted, the ``qbit_reselect`` job writes
    priority 0 for it, and ``batch.disposition`` decides whether the torrent is
    then worth keeping. The torrent is **never** marked ``cancelled`` — that
    value is ``qbit_cancel``'s mandate and ``qbit_cancel`` deletes with files,
    which for a pack means the other episodes' bytes. The episode takes the same
    ``downloading → not_wanted`` edge either way, so nothing above this function
    can tell the two apart.

    ``episode_id`` stays on the row. Changing your mind a minute later is then
    free: the next search finds the pack through ``batch.claim_existing`` and
    asks Nyaa nothing, which is the batch's version of "the release is not
    barred from being chosen again".

    The caller's ``qbit_cancel`` job is queued for this episode as it is for any
    other, and finds nothing marked ``cancelled`` to delete — one query and a
    log line. Left that way on purpose: making the return value carry *which*
    kind of cancellation it was would put the batch distinction into every
    caller, to save a job that is already written to find nothing.

    Only from :data:`CANCELLABLE`. Anything with bytes already landed is
    retention's (FR-T1), and anything before ``downloading`` has nothing in the
    client to remove — :func:`release_if_unwanted` is that half.

    **An open demand is never cancelled here** (FR-A14, M21): it is a live
    want for as long as it is open, so :func:`_still_wanted` finds it and
    every caller — the reconciler, a sample's cancel, a trip's cancel — leaves
    the download running. A demand stops its download only by ending first
    (finished, stale or withdrawn).
    """
    if episode.state not in CANCELLABLE:
        return False
    if await _still_wanted(session, episode, wanted_ids):
        return False

    claim = await live_claim(session, episode.id)
    if claim is not None:
        transition(episode, EpisodeState.NOT_WANTED, reason=NOBODY_WANTS)
        torrent_ids = await release_files(session, (claim,))
        log.info(
            "batch file given back, nobody wants the episode",
            extra={
                "episode_id": episode.id,
                "torrent_ids": list(torrent_ids),
                "file_index": claim.file_index,
                "path": claim.path,
            },
        )
        return True

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
    session: AsyncSession,
    result: WantsResult,
    *,
    now: datetime,
    held: bool = False,
    settings: Settings | None = None,
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
    # ``episode → whether every live want on it is a trip's`` (FR-A12), read
    # after ``_reconcile`` has rewritten the flags, so it is this run's answer.
    # And which of them somebody pressed play on (FR-A14), so a search the
    # reconciler has to (re)start for one keeps the demand's place in the queue.
    all_trip: dict[int, bool] = {}
    demanded: set[int] = set()
    for episode_id, only, pressed in (
        await session.execute(
            select(
                Want.episode_id,
                func.bool_and(Want.trip),
                func.bool_or(Want.demanded_at.is_not(None)),
            )
            .where(Want.dropped_at.is_(None))
            .group_by(Want.episode_id)
        )
    ).all():
        all_trip[episode_id] = bool(only)
        if pressed:
            demanded.add(episode_id)
    with_wants = set(all_trip)
    trip_only = {episode_id for episode_id, only in all_trip.items() if only}
    # A trip-only episode whose copy is made needs nothing more: its source was
    # deleted once the copy was ready (owner, 2026-10-05) and it sits in
    # ``not_wanted`` with the copy waiting for the device. Searching for it
    # again would fetch the very bytes that were just thrown away.
    # With ``settings`` the file itself is checked (size and ETag), so a copy
    # whose file has gone does not keep the episode from being fetched again;
    # without them (a caller with no data directory) the row's word is taken.
    copied: set[int] = set()
    if trip_only:
        for copy in (
            await session.scalars(
                select(OfflineCopy).where(
                    OfflineCopy.episode_id.in_(trip_only),
                    OfflineCopy.state == OfflineCopyState.READY,
                )
            )
        ).all():
            if settings is None or file_matches(offline_path_for(settings, copy.episode_id), copy):
                copied.add(copy.episode_id)
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
    matched: list[Episode] = []
    for episode in interesting:
        if episode.id in with_wants:
            if episode.state is EpisodeState.MATCHED:
                matched.append(episode)
            if held:
                continue
            if episode.id in copied:
                continue
            moved, enqueued = await start_search(
                session,
                episode,
                now=now,
                newest_job=newest_job,
                priority=(
                    DEMAND_PRIORITY
                    if episode.id in demanded
                    else TRIP_SEARCH_PRIORITY
                    if episode.id in trip_only
                    else SEARCH_RELEASE_PRIORITY
                ),
            )
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
    await _enqueue_tops(session, result.untripped)
    promoted = await _promote(session, matched, trip_only=trip_only, copied=copied)
    copies = 0 if held else await _requeue_trip_copies(session)
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
        promoted=promoted,
        copies=copies,
    )


async def _enqueue_tops(session: AsyncSession, episode_ids: Collection[int]) -> None:
    """Queue a move to the top for each in-flight episode no longer trip-only.

    A torrent taken only for a trip went to the back of the client's queue
    (FR-A12); once somebody wants the episode for streaming that is the wrong
    place for it. Only ``downloading`` episodes have a torrent to move. The
    job talks to the client; this does not.
    """
    if not episode_ids:
        return
    downloading = (
        await session.scalars(
            select(Episode.id).where(
                Episode.id.in_(set(episode_ids)), Episode.state == EpisodeState.DOWNLOADING
            )
        )
    ).all()
    for episode_id in downloading:
        await enqueue(
            session,
            QBIT_TOP,
            {"episode_id": episode_id},
            priority=QBIT_TOP_PRIORITY,
            dedupe_key=top_dedupe_key(episode_id),
        )
    if downloading:
        await session.flush()


async def _promote(
    session: AsyncSession,
    matched: Collection[Episode],
    *,
    trip_only: Collection[int],
    copied: Collection[int],
) -> int:
    """Settle the wanted ``matched`` episodes the encode paths left behind (FR-A12).

    **Promotion.** A ``matched`` episode that is *not* trip-only, has no
    rendition and no prepare job queued or running is one a trip held back from
    streaming — the link gave it an offline encode rather than a rendition —
    and a normal want has since arrived on it. Its source is still here, so it
    is remuxed now (M20; the remux hands over to the burn-in encode itself
    when the source cannot be copied). (Once the source is gone the episode is ``not_wanted``
    and the ordinary search above fetches it again.) A transcode that has used
    up its attempts is left alone, as the startup sweep leaves it: that is a
    job for a person — and so is one an admin cancelled.

    **The safety net.** A trip-only ``matched`` episode whose copy is ready
    should have had its source deleted by the copy hook's ``trip_release``;
    if that job was lost, it is queued again here (deduplicated).

    Returns how many transcodes were queued.
    """
    if not matched:
        return 0
    trip_only_ids = set(trip_only)
    streaming = [episode.id for episode in matched if episode.id not in trip_only_ids]
    promoted = 0
    if streaming:
        with_rendition = set(
            (
                await session.scalars(
                    select(Rendition.episode_id).where(Rendition.episode_id.in_(streaming))
                )
            ).all()
        )
        jobs = await latest_transcode_jobs(session, streaming)
        for episode_id in streaming:
            if episode_id in with_rendition:
                continue
            job = jobs.get(episode_id)
            if job is not None and (
                job.status in ACTIVE_STATUSES
                # Cancelled by an admin: deliberate, and only the startup
                # sweep second-guesses it (as it always has).
                or job.status is JobStatus.CANCELLED
                or (job.status is JobStatus.FAILED and job.attempts >= job.max_attempts)
            ):
                continue
            await enqueue_remux(session, episode_id)
            promoted += 1
            log.info("trip episode promoted to streaming", extra={"episode_id": episode_id})
    for episode in matched:
        if episode.id in trip_only_ids and episode.id in copied:
            await enqueue_trip_release(session, episode.id)
    return promoted


#: Episode states whose linked source a trip copy can be made from.
_COPYABLE: frozenset[EpisodeState] = frozenset(
    {EpisodeState.READY, EpisodeState.MATCHED, EpisodeState.PREPARING, EpisodeState.FAILED}
)


async def _requeue_trip_copies(session: AsyncSession) -> int:
    """Queue the copy a pending trip episode is missing (FR-A12, FR-P6).

    An episode some active trip still waits on, whose source is linked, that
    has **no** copy row and no live ``offline_encode``: its copy was never
    queued (the episode arrived by a path that queued none) or was deleted
    since (a ``ready`` episode's copy can be removed by retention). A copy row
    in any state is left to its own path — ``failed`` is not retried
    automatically (FR-P6). Skipped while storage is held, like the searches.
    Deduplicated, so a second run queues nothing. Returns how many were queued.
    """
    pending = await pending_episode_ids(session)
    if not pending:
        return 0
    candidates = set(
        (
            await session.scalars(
                select(Episode.id).where(Episode.id.in_(pending), Episode.state.in_(_COPYABLE))
            )
        ).all()
    )
    candidates &= await episodes_with_sources(session, list(candidates))
    if not candidates:
        return 0
    candidates -= set(
        (
            await session.scalars(
                select(OfflineCopy.episode_id).where(OfflineCopy.episode_id.in_(candidates))
            )
        ).all()
    )
    candidates -= set(await latest_offline_jobs(session, sorted(candidates)))
    for episode_id in sorted(candidates):
        await enqueue_offline_encode(
            session,
            episode_id,
            why=OFFLINE_WHY_TRIP,
            priority=await trip_copy_priority(session, episode_id),
        )
    if candidates:
        await session.flush()
        log.info("trip copies queued", extra={"episode_ids": sorted(candidates)})
    return len(candidates)


__all__ = [
    "CANCELLABLE",
    "DEVICE_HOLDING_DAYS",
    "NOBODY_WANTS",
    "QBIT_CANCELLED",
    "REASON_NOT_WANTING",
    "REASON_TRIP_ENDED",
    "RELEASABLE",
    "STALE_DROP_REASON",
    "STARTABLE",
    "UNAVAILABLE_RETRY",
    "WANTING_STATUSES",
    "SlotView",
    "WantsResult",
    "cancel_if_unwanted",
    "compute_wants",
    "coverable",
    "coverable_once_active",
    "enqueue_cancel",
    "enqueue_compute_wants",
    "is_halfway",
    "lock_wants",
    "release_if_unwanted",
    "slot_totals",
    "slot_view",
    "start_search",
    "walk_counts",
    "window",
]
