"""Where a season's shows sit in the user's week (FR-C3).

The schedule reads the **local cache only**. No catalogue source is called
while a schedule page renders: the season pre-cache (FR-C7) is what fills the
rows, and a day when both sources are down must cost the freshness of the
airing times, not the page. That is also why every function here is pure —
rows in, placement out, statements built and never executed — with the session
left to the router.

Three decisions live here.

**Which shows are in the week.** The catalogue's own answer is
``anime.season``, and for a season being *browsed* — the prev/next views — that
is the whole rule: those pages are "the shows of Spring 2026", a catalogue
listing that happens to be laid out as a week. The **current** season's grid is
a calendar instead, and a calendar is about what is on, not about what started
when: a two-cour show that began in spring is still on air on a Friday in
summer, and so is a long-runner that belongs to no season at all. So the
current week takes a second source — every ``RELEASING`` show with a known air
time inside :data:`AIRING_WINDOW` of now (:func:`airing_this_week`) — merged
into the season's own rows by id. The season tag on the row is untouched; the
entry says it was carried in (:attr:`ScheduleEntry.carried_over` over the wire)
so the card can name the season the show started in. Found on production: That
Time I Got Reincarnated as a Slime Season 4 airs every Friday, is tagged
``SPRING 2026``, and was absent from the Summer 2026 grid (owner, 2026-09-13).

**Which instant places a show.** A row that is still airing carries
``next_airing`` (AniList's ``nextAiringEpisode``, or the slot Arc synthesises
from a MAL broadcast time), and that is the sharpest statement anyone makes
about when the show is on. A row without one falls back to the last episode
that has an air time, which is what puts a finished season on the weekday it
used to air. A row with neither is not on a weekday at all.

**Which shows get a weekday.** Only the weekly formats. A movie has a release
date, not a slot, and putting it on the Friday of its premiere would say it is
on every Friday; the same goes for OVAs, specials and music videos. They are
listed as unscheduled, which the client renders beside the grid.

Weekdays are the user's, not UTC's: a show that airs 16:00 Saturday in Tokyo
is a Sunday show in Japan and a Saturday morning one in California, and the
whole point of FR-C3 is that each user sees their own week.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Integer, Select, and_, exists, or_, select

from arc.models import Anime, Episode, ListStatus
from arc.services.catalog.airing import (
    RELEASING,
    next_airing_at,
    next_airing_episode,
    next_airing_estimated,
)
from arc.services.catalog.cache import preferred_title
from arc.services.catalog.seasons import adjacent_seasons, current_season, next_season, prev_season

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
class ScheduleRow:
    """One cached show, plus the two facts placement needs about it.

    ``latest_air_at`` is the air time of the highest-numbered episode that has
    one — a single row per show in the router rather than the whole episode
    list, because placement only ever asks for the weekday it fell on. The last
    episode rather than the latest date, so that a source which dates an early
    episode after a later one cannot move the show to another weekday; see
    :mod:`arc.services.catalog.airing`.
    """

    anime: Anime
    latest_air_at: datetime | None = None
    list_status: ListStatus | None = None
    #: True when the row is in this week because it is on air, not because it
    #: carries the season being shown — a two-cour show from an earlier season,
    #: or a long-runner tagged with none. Carried through to the entry so the
    #: card can say which season the show started in.
    carried_over: bool = False


@dataclass(frozen=True, slots=True)
class PlacedEntry:
    """One show as the schedule renders it."""

    anime: Anime
    list_status: ListStatus | None = None
    #: Local time of day the show airs, or ``None`` for an unscheduled entry.
    air_time_local: time | None = None
    #: The next episode and when it airs, when the row knows. The number is
    #: null for a MAL-synthesised slot, which knows the time and not the
    #: episode (FR-C6).
    next_episode: int | None = None
    next_at: datetime | None = None
    #: Whether ``next_at`` is Arc's own arithmetic over a MAL broadcast slot
    #: rather than a published time (FR-C6). Always false when there is no
    #: ``next_at`` to qualify: an absent time is not an estimated one.
    next_at_estimated: bool = False
    #: Whether the show was pulled into this week by being on air rather than
    #: by its season tag (see the module docstring).
    carried_over: bool = False

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

    #: Seven lists, index 0 = Monday, each sorted by local air time.
    days: tuple[list[PlacedEntry], ...] = field(
        default_factory=lambda: tuple([] for _ in range(DAYS_IN_WEEK))
    )
    #: Movies, OVAs, specials, and anything with no air time at all.
    unscheduled: list[PlacedEntry] = field(default_factory=list)


def season_members(year: int, season: str) -> Select[tuple[Anime]]:
    """Every show tagged with one season — the catalogue's own answer.

    The whole membership rule for a season being browsed, and the first half of
    it for the current one.
    """
    return select(Anime).where(Anime.season == season, Anime.season_year == year).order_by(Anime.id)


def airing_this_week(*, now: datetime) -> Select[tuple[Anime]]:
    """Every show on air within :data:`AIRING_WINDOW` of ``now``, any season.

    The second half of the current week's membership (see the module
    docstring). Three conditions, and each of them earns its place:

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
    return (
        select(Anime)
        .where(
            Anime.status == RELEASING,
            Anime.format.in_(sorted(SCHEDULED_FORMATS)),
            or_(published, dated_episode),
        )
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
    """Sort a season's rows into seven local weekdays plus the leftovers.

    ``now`` is not a filter — a season page shows the whole season, past
    episodes included — it is only what decides whether a row's cached
    ``next_airing`` is still describing the future (:data:`STALE_NEXT_AIRING`).
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


__all__ = [
    "AIRING_WINDOW",
    "DAYS_IN_WEEK",
    "DEFAULT_TIMEZONE",
    "FOLLOWING_STATUSES",
    "SCHEDULED_FORMATS",
    "STALE_NEXT_AIRING",
    "PlacedEntry",
    "ScheduleRow",
    "WeekPlacement",
    "adjacent_seasons",
    "airing_this_week",
    "current_season",
    "next_season",
    "place_entries",
    "prev_season",
    "season_members",
    "user_timezone",
]
