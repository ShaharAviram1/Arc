"""Where a season's shows sit in the user's week (FR-C3).

The schedule reads the **local cache only**. No catalogue source is called
while a schedule page renders: the season pre-cache (FR-C7) is what fills the
rows, and a day when both sources are down must cost the freshness of the
airing times, not the page. That is also why every function here is pure —
rows in, placement out, statements built and never executed — with the session
left to the router.

There are two layouts, and the difference between them is the first thing to
know about this module.

* A season being **browsed** — the prev/next views — is a catalogue listing
  that happens to be laid out as a week: "the shows of Spring 2026", each on
  the weekday it airs or aired. :func:`place_entries` builds it.
* The **current** view is a calendar of concrete dates (owner, 2026-10-04: "the
  schedule should only show what's really playing that date"). Each column is
  one local date, and lists exactly the shows with an episode airing on it.
  :func:`place_dated` builds it, over :func:`displayed_dates`.

Four decisions live here.

**Which shows are candidates.** The catalogue's own answer is
``anime.season``, and for a browse that is the whole rule. The current view
takes a second source as well — every weekly-format show of *any* season and
any status with an air time inside the displayed dates (:func:`airing_between`)
— merged into the season's own rows by id, because a calendar is about what is
on, not about what started when: a two-cour show that began in spring is still
on air on a Friday in summer, a long-runner belongs to no season at all, and in
the week a season turns over next season's premiere airs before the UTC
quarter says so. The season tag on the row is untouched; the entry says it was
carried in (:attr:`ScheduleEntry.carried_over` over the wire) so the card can
name the season the show started in. Found on production: That Time I Got
Reincarnated as a Slime Season 4 airs every Friday, is tagged ``SPRING 2026``,
and was absent from the Summer 2026 grid (owner, 2026-09-13). The Home hero's
pool keeps its own, narrower rule (:func:`on_air_this_week`).

**Which candidates are on a date** (current view only). Being a candidate is
not being on: the season's own rows include shows that finished long ago and
shows whose premiere is a month away, and the weekday layout used to put both
on today's column. A dated column takes a show only on evidence that an
episode airs that local date, in this order — the cached ``next_airing`` slot;
an ``Episode`` row whose ``air_at`` falls on it (what keeps Tuesday's show on
Tuesday after Tuesday's broadcast, when ``next_airing`` has rolled to next
week); and, for a ``RELEASING`` show with no dated episode rows at all, the
weekly slot inferred one week before ``next_airing`` — only once that instant
has passed, never for a premiere, and for a slot that names no episode only
when the show demonstrably started before it (:func:`_started_by`). A
``FINISHED`` show is placed by its dated episode rows alone — a finale that
aired on Wednesday was on on Wednesday — and otherwise goes to ``ended``, which
the page does not draw on any date but which keeps the season's listing whole
for Search and Home. A season show with no evidence in range is listed as
unscheduled — with its premiere date when it has not started — and a carried-in
one with none is dropped, because it is only in the candidates for being on.

**Which instant places a show** (browse only). A row that is still airing
carries ``next_airing`` (AniList's ``nextAiringEpisode``, or the slot Arc
synthesises from a MAL broadcast time), and that is the sharpest statement
anyone makes about when the show is on. A row without one falls back to the
last episode that has an air time, which is what puts a finished season on the
weekday it used to air. A row with neither is not on a weekday at all.

**Which shows get a weekday.** Only the weekly formats. A movie has a release
date, not a slot, and putting it on the Friday of its premiere would say it is
on every Friday; the same goes for OVAs, specials and music videos. They are
listed as unscheduled, which the client renders beside the grid.

Weekdays and dates are the user's, not UTC's: a show that airs 16:00 Saturday
in Tokyo is a Sunday show in Japan and a Saturday morning one in California,
and the whole point of FR-C3 is that each user sees their own week.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, Integer, Select, and_, exists, or_, select

from arc.models import Anime, Episode, ListStatus
from arc.services.catalog.airing import (
    FINISHED,
    RELEASING,
    next_airing_at,
    next_airing_episode,
    next_airing_estimated,
)
from arc.services.catalog.cache import preferred_title
from arc.services.catalog.seasons import (
    SEASONS,
    adjacent_seasons,
    current_season,
    next_season,
    prev_season,
)

log = logging.getLogger(__name__)

#: Monday is 0, matching :meth:`datetime.date.weekday`.
DAYS_IN_WEEK = 7

#: The formats that have a weekly slot. Everything else — MOVIE, OVA, SPECIAL,
#: MUSIC, and a row whose format nobody has filled in yet — is listed as
#: unscheduled rather than pinned to the weekday of its release.
SCHEDULED_FORMATS = frozenset({"TV", "TV_SHORT", "ONA"})

#: List states that mean "I follow this", which is what the schedule
#: highlights (FR-C3). Dropped and completed are deliberately absent: they are
#: on the list as history, not as something to watch for on Friday.
FOLLOWING_STATUSES = frozenset({ListStatus.WATCHING, ListStatus.PLANNED, ListStatus.ON_HOLD})

#: How stale a ``next_airing`` may be before it stops counting as a statement
#: about the future. The blob is a cache of a moving value: an episode airs,
#: and until the next refresh the row still points at the broadcast that has
#: just happened. A few days of that is harmless — it is the same weekday
#: either way — but a row nobody has refreshed for a fortnight is describing an
#: episode that never came, and the last episode with a real air time is better
#: evidence.
STALE_NEXT_AIRING = timedelta(days=7)

#: How far either side of the present an air time may fall and still count as
#: "on this week" for :func:`airing_this_week`. A week each way, because a week
#: is the grid's own period: a weekly show refreshed just after a broadcast
#: points up to seven days ahead, and one refreshed just before points up to
#: seven days back. The trailing edge is deliberately the same seven days as
#: :data:`STALE_NEXT_AIRING`, so a row can never be pulled into the week for a
#: slot that :func:`place_entries` then throws away as stale.
AIRING_WINDOW = timedelta(days=7)

#: The timezone anything unparseable falls back to. Arc works in UTC
#: throughout, so a broken ``users.timezone`` costs an offset, not a page.
DEFAULT_TIMEZONE = "UTC"

#: How many days past today the current view always reaches: a week of
#: upcoming dates, today included. The client opens on today with three
#: columns, so on a Saturday or a Sunday the next columns are next week's —
#: Sunday's "tomorrow" is next Monday, not the Monday six days ago — and Home's
#: "This week" shelf reads the same seven upcoming dates as its appointments.
DAYS_AHEAD = 6

#: A weekly slot's period, for the one inference :func:`place_dated` makes.
WEEK = timedelta(days=7)

#: The status of a show that has been announced and has not started.
NOT_YET_RELEASED = "NOT_YET_RELEASED"


def user_timezone(name: str | None) -> ZoneInfo:
    """The user's IANA zone, or UTC with a warning.

    ``users.timezone`` is free text as far as the database is concerned, and a
    schedule that 500s because somebody's profile says ``"GMT+2"`` would be a
    worse answer than a schedule in UTC.
    """
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError, ValueError:
            log.warning("unknown user timezone; falling back to UTC", extra={"timezone": name})
    return ZoneInfo(DEFAULT_TIMEZONE)


@dataclass(frozen=True, slots=True)
class DatedEpisode:
    """One ``Episode`` row with an air time, as the dated layout reads it."""

    number: int
    air_at: datetime
    #: ``episodes.air_at_estimated`` (FR-C6), carried to the entry's badge.
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    """One cached show, plus the facts placement needs about it.

    ``latest_air_at`` is the air time of the highest-numbered episode that has
    one — a single row per show in the router rather than the whole episode
    list, because the browse layout only ever asks for the weekday it fell on.
    The last episode rather than the latest date, so that a source which dates
    an early episode after a later one cannot move the show to another weekday;
    see :mod:`arc.services.catalog.airing`. The dated layout reads it for one
    thing only: whether the show has any dated episode at all.

    ``dated_episodes`` and ``first_air_at`` are filled for the current view
    alone: the episodes dated inside the displayed range, and the air time of
    the lowest-numbered dated episode (a premiere date for a show that has not
    started and has no ``next_airing``).
    """

    anime: Anime
    latest_air_at: datetime | None = None
    list_status: ListStatus | None = None
    #: True when the row is in this week because it is on air, not because it
    #: carries the season being shown — a two-cour show from an earlier season,
    #: or a long-runner tagged with none. Carried through to the entry so the
    #: card can say which season the show started in.
    carried_over: bool = False
    dated_episodes: tuple[DatedEpisode, ...] = ()
    first_air_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PlacedEntry:
    """One show as the schedule renders it."""

    anime: Anime
    list_status: ListStatus | None = None
    #: Local time of day the show airs, or ``None`` for an unscheduled entry.
    air_time_local: time | None = None
    #: The episode this slot names and when it airs. On a browse that is the
    #: next one, when the row knows; on a dated column it is the episode airing
    #: that date, which may already have aired. The number is null for a
    #: MAL-synthesised slot, which knows the time and not the episode (FR-C6).
    next_episode: int | None = None
    next_at: datetime | None = None
    #: Whether ``next_at`` is Arc's own arithmetic over a MAL broadcast slot
    #: rather than a published time (FR-C6). Always false when there is no
    #: ``next_at`` to qualify: an absent time is not an estimated one.
    next_at_estimated: bool = False
    #: Whether the show was pulled into this week by being on air rather than
    #: by its season tag (see the module docstring).
    carried_over: bool = False
    #: The local date a show that has not started premieres on, when anything
    #: says; set only on the current view's unscheduled entries (owner,
    #: 2026-10-04: an upcoming show stays reachable, with its date).
    starts_on: date | None = None
    #: The highest episode airing on the same date as ``next_episode`` when a
    #: dated column holds more than one ("Ep 3–4"); null otherwise.
    last_episode: int | None = None

    @property
    def following(self) -> bool:
        """Whether the caller follows this show (FR-C3 highlights these)."""
        return self.list_status in FOLLOWING_STATUSES

    @property
    def sort_key(self) -> tuple[int, int, str]:
        """By time of day, then by title, so a day's order is deterministic."""
        at = self.air_time_local
        minutes = at.hour * 60 + at.minute if at is not None else 0
        seconds = at.second if at is not None else 0
        return (minutes, seconds, preferred_title(self.anime).casefold())


@dataclass(slots=True)
class WeekPlacement:
    """A whole season, sorted into the user's week.

    Deliberately not ``frozen``: :func:`place_entries` appends to these lists
    and sorts them in place, and a frozen dataclass would only have promised
    something it does not deliver — it stops the attribute being rebound, not
    the list behind it being rewritten.
    """

    #: One list per column, each sorted by local air time. On a browse, seven,
    #: index 0 = Monday; on the current view, one per entry of ``dates``.
    days: tuple[list[PlacedEntry], ...] = field(
        default_factory=lambda: tuple([] for _ in range(DAYS_IN_WEEK))
    )
    #: Movies, OVAs, specials, and anything with no air time at all — plus, on
    #: the current view, the season's shows with nothing airing in range.
    unscheduled: list[PlacedEntry] = field(default_factory=list)
    #: The local date of each column, on the current view; empty on a browse,
    #: whose columns are weekdays rather than dates.
    dates: tuple[date, ...] = ()
    #: The current season's own weekly shows that have finished and have no
    #: episode on a displayed date. On no day of the calendar, but still shows
    #: of the season: Search's and Home's season listings read them from here.
    #: Always empty on a browse, which places finished shows on a weekday.
    ended: list[PlacedEntry] = field(default_factory=list)


def season_members(year: int, season: str) -> Select[tuple[Anime]]:
    """Every show tagged with one season — the catalogue's own answer.

    The whole membership rule for a season being browsed, and the first half of
    it for the current one.
    """
    return select(Anime).where(Anime.season == season, Anime.season_year == year).order_by(Anime.id)


def on_air_this_week(*, now: datetime) -> ColumnElement[bool]:
    """The ``WHERE`` clause for "on air within :data:`AIRING_WINDOW` of ``now``".

    The Home hero's pool: what is on air this week. It was the second half of
    the current schedule's membership until the schedule became a calendar of
    dates (owner, 2026-10-04), which takes the wider :func:`airing_between`;
    this rule did not move with it. Three conditions, and each of them earns
    its place:

    * ``status = RELEASING``. A finished show from last season is not on this
      week whatever dates it carries, and an announced one is not on yet.
    * A known air time in the window: the cached ``next_airing`` slot, whose
      ``airingAt`` is compared as epoch seconds in SQL rather than by
      converting every row in Python (the same arithmetic
      :func:`~arc.services.catalog.jobs.catalog_pre_air` does), or — only where
      the row has no slot at all — an episode dated inside the window. A
      ``RELEASING`` row with no air time anywhere is not carried in: it has
      nothing to place it on a weekday, and its own season's grid already lists
      it as unscheduled.
    * A weekly format. :func:`place_entries` would put anything else in
      ``unscheduled``, and the current season's unscheduled list is its own
      films and OVAs — a releasing ONA from two seasons ago belongs on a
      weekday or nowhere.

    Separated from the statement below because more than one thing asks the
    question. The TMDB art passes target the pool the Home hero picks from — so
    :func:`~arc.services.tmdb.jobs.hero_pool_members` composes this clause into
    a query of its own rather than restating the rule (owner, 2026-09-17: One
    Piece, carried into the grid and reached by neither art pass). One
    definition, two callers; a second version would be a rule that could drift.
    """
    lower = int((now - AIRING_WINDOW).timestamp())
    upper = int((now + AIRING_WINDOW).timestamp())
    airing_at = Anime.next_airing["airingAt"].astext.cast(Integer)
    published = and_(Anime.next_airing.isnot(None), airing_at >= lower, airing_at <= upper)
    dated_episode = and_(
        Anime.next_airing.is_(None),
        exists().where(
            Episode.anime_id == Anime.id,
            Episode.air_at >= now - AIRING_WINDOW,
            Episode.air_at <= now + AIRING_WINDOW,
        ),
    )
    return and_(
        Anime.status == RELEASING,
        Anime.format.in_(sorted(SCHEDULED_FORMATS)),
        or_(published, dated_episode),
    )


def airing_this_week(*, now: datetime) -> Select[tuple[Anime]]:
    """Every show on air within :data:`AIRING_WINDOW` of ``now``, any season.

    The rows behind :func:`on_air_this_week`, which is where the rule itself
    is written down. The schedule no longer reads it; the dated view's
    candidates are :func:`airing_between`.
    """
    return select(Anime).where(on_air_this_week(now=now)).order_by(Anime.id)


def airing_between(start: datetime, end: datetime) -> Select[tuple[Anime]]:
    """Every weekly show, any season and any status, with an air time in
    ``[start, end)`` — the current view's carry-in candidates.

    Wider than :func:`on_air_this_week` on purpose, and kept apart from it so
    the Home hero's pool does not move: the calendar must also hold next
    season's premiere in the week the quarter turns over, before the UTC
    season says it is current, and a finished show whose finale aired on a
    displayed date. :func:`place_dated` then decides which dates, if any, each
    of these is on. Either a ``next_airing`` slot in range (compared as epoch
    seconds in SQL) or an episode dated in range qualifies.
    """
    lower = int(start.timestamp())
    upper = int(end.timestamp())
    airing_at = Anime.next_airing["airingAt"].astext.cast(Integer)
    slot = and_(Anime.next_airing.isnot(None), airing_at >= lower, airing_at < upper)
    dated = exists().where(
        Episode.anime_id == Anime.id, Episode.air_at >= start, Episode.air_at < end
    )
    return (
        select(Anime)
        .where(Anime.format.in_(sorted(SCHEDULED_FORMATS)), or_(slot, dated))
        .order_by(Anime.id)
    )


def _next_airing(row: ScheduleRow, *, now: datetime) -> tuple[int | None, datetime | None, bool]:
    """``(episode, at, estimated)`` from ``next_airing``, if it is still fresh.

    A stale blob is dropped whole rather than reported with its time: "episode
    6 aired three weeks ago and is next" is worse than saying nothing. Dropping
    it clears the estimated flag too — there is no time left for it to qualify.
    """
    blob = row.anime.next_airing
    at = next_airing_at(blob)
    if at is None or at < now - STALE_NEXT_AIRING:
        return (None, None, False)
    return (next_airing_episode(blob), at, next_airing_estimated(blob))


def place_entries(rows: list[ScheduleRow], *, tz: tzinfo, now: datetime) -> WeekPlacement:
    """Sort a browsed season's rows into seven local weekdays plus the leftovers.

    The prev/next layout; the current view is :func:`place_dated`. ``now`` is
    not a filter — a season page shows the whole season, past episodes
    included — it is only what decides whether a row's cached ``next_airing``
    is still describing the future (:data:`STALE_NEXT_AIRING`).
    """
    placement = WeekPlacement()
    for row in rows:
        next_episode, next_at, next_estimated = _next_airing(row, now=now)
        at = next_at if next_at is not None else row.latest_air_at
        placeable = at is not None and (row.anime.format or "") in SCHEDULED_FORMATS
        local = at.astimezone(tz) if at is not None and placeable else None
        entry = PlacedEntry(
            anime=row.anime,
            list_status=row.list_status,
            air_time_local=local.time() if local is not None else None,
            next_episode=next_episode,
            next_at=next_at,
            next_at_estimated=next_estimated,
            carried_over=row.carried_over,
        )
        if local is None:
            placement.unscheduled.append(entry)
        else:
            placement.days[local.weekday()].append(entry)

    for day in placement.days:
        day.sort(key=lambda entry: entry.sort_key)
    placement.unscheduled.sort(key=lambda entry: preferred_title(entry.anime).casefold())
    return placement


def displayed_dates(*, now: datetime, tz: tzinfo) -> tuple[date, ...]:
    """The local dates the current view covers, in order.

    Monday of the user's current week — so the days already gone this week
    stay an arrow away — through :data:`DAYS_AHEAD` past today, a full week of
    upcoming dates: the three columns the client opens on are today, tomorrow
    and the day after, each a real date, and Home's shelf takes the seven from
    today. Seven dates on a Monday, up to thirteen on a Sunday.

    The carry-in candidates are queried over exactly these dates
    (:func:`airing_between` over :func:`range_bounds`).
    """
    today = now.astimezone(tz).date()
    monday = today - timedelta(days=today.weekday())
    last = today + timedelta(days=DAYS_AHEAD)
    return tuple(monday + timedelta(days=offset) for offset in range((last - monday).days + 1))


def range_bounds(dates: tuple[date, ...], *, tz: tzinfo) -> tuple[datetime, datetime]:
    """``[start, end)`` as instants: local midnight of the first date to local
    midnight after the last, which is the episode query's ``air_at`` window."""
    start = datetime.combine(dates[0], time(), tzinfo=tz)
    end = datetime.combine(dates[-1] + timedelta(days=1), time(), tzinfo=tz)
    return start, end


def _season_end(season: str | None, year: int | None) -> date | None:
    """The last day of a tagged season, or ``None`` for an untagged show."""
    if season not in SEASONS or year is None:
        return None
    following_year, following = next_season(year, season)
    return date(following_year, SEASONS.index(following) * 3 + 1, 1) - timedelta(days=1)


def _started_by(row: ScheduleRow, at: datetime) -> bool:
    """Whether the show is known to have started on or before ``at``.

    Arc keeps no start date, so the bound is the season tag: a show tagged
    Summer 2026 premiered by 30 September, whenever in the summer it did. A
    show with no season (a long-runner) has no known start and gets ``False``
    — the inference this guards is not made on a guess.
    """
    end = _season_end(row.anime.season, row.anime.season_year)
    return end is not None and end < at.date()


def _dated_slots(
    row: ScheduleRow, *, tz: tzinfo, now: datetime, index: dict[date, int]
) -> dict[date, PlacedEntry]:
    """Every displayed date ``row`` has an episode airing on, with its entry.

    One entry per date: the first piece of evidence to name a date places the
    show there, and a second episode on the same date widens the entry to
    ``last_episode`` rather than hiding it. Evidence is asked in the module
    docstring's order. Episode rows at or past the episode ``next_airing``
    names are left to it — the slot is the fresher statement about when that
    episode airs, and a row dated for a broadcast that has since moved would
    otherwise put the show on two dates. A finished show has no slot and
    nothing is inferred for it: its dated rows are the whole of its evidence.
    """
    found: dict[date, PlacedEntry] = {}

    def put(at: datetime, number: int | None, estimated: bool) -> None:
        local = at.astimezone(tz)
        day = local.date()
        if day not in index:
            return
        held = found.get(day)
        if held is not None:
            if (
                held.next_episode is not None
                and number is not None
                and number > max(held.next_episode, held.last_episode or 0)
            ):
                found[day] = replace(held, last_episode=number)
            return
        found[day] = PlacedEntry(
            anime=row.anime,
            list_status=row.list_status,
            air_time_local=local.time(),
            next_episode=number,
            next_at=at,
            next_at_estimated=estimated,
            carried_over=row.carried_over,
        )

    finished = row.anime.status == FINISHED
    next_episode, next_at, next_estimated = (
        (None, None, False) if finished else _next_airing(row, now=now)
    )
    if next_at is not None:
        put(next_at, next_episode, next_estimated)

    for episode in sorted(row.dated_episodes, key=lambda episode: episode.number):
        if next_episode is not None and episode.number >= next_episode:
            continue
        if next_episode is None and next_at is not None and episode.air_at >= next_at:
            continue
        put(episode.air_at, episode.number, episode.estimated)

    # A weekly show whose last broadcast left no dated row still aired a week
    # before its next one. Only where nothing is dated at all — a show with
    # episode rows has said when it aired, and a gap in them is a break, not a
    # broadcast to invent — only for an instant already past, never for a
    # premiere, and for a slot that names no episode only when the show had
    # demonstrably started by then.
    inferred = next_at - WEEK if next_at is not None else None
    if (
        inferred is not None
        and row.anime.status == RELEASING
        and inferred <= now
        and row.latest_air_at is None
        and not row.dated_episodes
        and (
            (next_episode is not None and next_episode > 1)
            or (next_episode is None and _started_by(row, inferred))
        )
    ):
        put(inferred, None if next_episode is None else next_episode - 1, next_estimated)
    return found


def _starts_on(row: ScheduleRow, *, tz: tzinfo, now: datetime) -> date | None:
    """The local premiere date of a show that has not started, if known."""
    if row.anime.status != NOT_YET_RELEASED:
        return None
    _, next_at, _ = _next_airing(row, now=now)
    at = next_at if next_at is not None else row.first_air_at
    return at.astimezone(tz).date() if at is not None else None


def place_dated(
    rows: list[ScheduleRow], *, tz: tzinfo, now: datetime, dates: tuple[date, ...]
) -> WeekPlacement:
    """Sort the current view's rows onto the dates they air (FR-C3).

    ``dates`` is :func:`displayed_dates`. A weekly show lands on each date it
    has evidence for and nowhere else (module docstring). The season's own
    finished shows with nothing in range go to ``ended``; its other shows with
    nothing in range are unscheduled, carrying their premiere date when they
    have not started; a carried-in row with nothing in range is dropped.
    Movies, OVAs and specials are listed as unscheduled exactly as
    :func:`place_entries` lists them.
    """
    index = {day: position for position, day in enumerate(dates)}
    placement = WeekPlacement(days=tuple([] for _ in dates), dates=dates)
    for row in rows:
        weekly = (row.anime.format or "") in SCHEDULED_FORMATS
        slots = _dated_slots(row, tz=tz, now=now, index=index) if weekly else {}
        for day, entry in slots.items():
            placement.days[index[day]].append(entry)
        if slots or row.carried_over:
            continue
        next_episode, next_at, next_estimated = _next_airing(row, now=now)
        entry = PlacedEntry(
            anime=row.anime,
            list_status=row.list_status,
            next_episode=next_episode,
            next_at=next_at,
            next_at_estimated=next_estimated,
            carried_over=row.carried_over,
            starts_on=_starts_on(row, tz=tz, now=now) if weekly else None,
        )
        if weekly and row.anime.status == FINISHED:
            placement.ended.append(entry)
        else:
            placement.unscheduled.append(entry)

    for day_entries in placement.days:
        day_entries.sort(key=lambda entry: entry.sort_key)
    placement.unscheduled.sort(key=lambda entry: preferred_title(entry.anime).casefold())
    placement.ended.sort(key=lambda entry: preferred_title(entry.anime).casefold())
    return placement


__all__ = [
    "AIRING_WINDOW",
    "DAYS_AHEAD",
    "DAYS_IN_WEEK",
    "DEFAULT_TIMEZONE",
    "FOLLOWING_STATUSES",
    "SCHEDULED_FORMATS",
    "STALE_NEXT_AIRING",
    "DatedEpisode",
    "PlacedEntry",
    "ScheduleRow",
    "WeekPlacement",
    "adjacent_seasons",
    "airing_between",
    "airing_this_week",
    "current_season",
    "displayed_dates",
    "next_season",
    "on_air_this_week",
    "place_dated",
    "place_entries",
    "prev_season",
    "range_bounds",
    "season_members",
    "user_timezone",
]
