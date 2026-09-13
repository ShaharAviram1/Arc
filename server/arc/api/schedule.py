"""The seasonal schedule (FR-C3).

``GET /api/schedule?year=&season=`` — one season, grouped by weekday in the
caller's timezone, with prev/next links.

**The cache is the only source.** Nothing here calls AniList or MyAnimeList,
and that is the point of FR-C7: the season pre-cache writes the rows overnight
so that a day when both sources are down costs the freshness of the airing
times and not the page. It also keeps the endpoint fast — a season is a handful
of queries and no network — and means an open schedule tab cannot pace a
hundred requests at AniList.

The router is thin, as everything in ``arc/api`` is: it turns query parameters
into a season, runs the statements
:mod:`arc.services.catalog.schedule` builds, and hands the rows to
:func:`~arc.services.catalog.schedule.place_entries`. Which shows are in the
week and where each lands are both decided there.

One asymmetry is worth naming here, because it is the only thing this module
does with the season it was asked for: the **current** season's grid takes a
second set of rows — everything on air this week, whatever season it carries
(:func:`~arc.services.catalog.schedule.airing_this_week`) — and the prev/next
views do not. The current week is a calendar and the others are a catalogue
browse; see the service's module docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from arc.api.deps import CurrentUser, SessionDep
from arc.api.schedule_schemas import SchedulePage, SeasonName
from arc.models import Anime, Episode
from arc.services.catalog import list_status_for
from arc.services.catalog.schedule import (
    ScheduleRow,
    adjacent_seasons,
    airing_this_week,
    current_season,
    place_entries,
    season_members,
    user_timezone,
)

router = APIRouter(prefix="/api/schedule", tags=["schedule"])

#: Bounds on the year. Not policy — the oldest season anyone has cached is
#: 1960-something and the newest AniList announces is a year out — but a guard
#: so that ``?year=99999999`` is a 422 rather than a query.
MIN_YEAR = 1900
MAX_YEAR = 2200


def now() -> datetime:
    """The clock the schedule is rendered against.

    A function so a test can pin it: which season is "current", and whether a
    row's cached ``next_airing`` is still describing the future, both move on
    their own otherwise.
    """
    return datetime.now(UTC)


@router.get(
    "",
    response_model=SchedulePage,
    summary="One season, grouped by weekday in the caller's timezone (FR-C3)",
)
async def schedule(
    user: CurrentUser,
    session: SessionDep,
    year: Annotated[int | None, Query(ge=MIN_YEAR, le=MAX_YEAR)] = None,
    season: SeasonName | None = None,
) -> SchedulePage:
    """Both parameters are optional; either one missing falls back to today's.

    Filling them independently rather than insisting on both means
    ``?season=WINTER`` is "this year's winter", which is what a client that
    only changed one half of a link is asking for.
    """
    at = now()
    default_year, default_season = current_season(at)
    target_year = year if year is not None else default_year
    target_season = season if season is not None else SeasonName(default_season)

    rows = list((await session.scalars(season_members(target_year, target_season.value))).all())
    # The current week is a calendar, so it also holds every show that is on
    # air now, whatever season it was tagged with: a two-cour show that started
    # in spring is still on a Friday in summer, and a long-runner is tagged
    # with no season at all (owner, 2026-09-13). Merged by id, the season's own
    # row winning — it is the same row either way, and the one already in hand
    # is not marked as carried in.
    carried: list[Anime] = []
    if (target_year, target_season.value) == (default_year, default_season):
        seen = {row.id for row in rows}
        carried = [
            row
            for row in (await session.scalars(airing_this_week(now=at))).all()
            if row.id not in seen
        ]
    anime_ids = [row.id for row in rows] + [row.id for row in carried]

    # The air time of the *highest-numbered* episode that has one, per show:
    # what places a season that has finished airing on the weekday it used to
    # air. One query rather than the episode lists themselves — placement only
    # needs the weekday, and a season is two hundred shows.
    #
    # Deliberately the last episode rather than ``max(air_at)``, which is the
    # same answer on every well-formed list and the wrong one on a source that
    # dates an early episode after a later one: the stray date would win the
    # max and move a whole show to another weekday. It is the query form of
    # :func:`~arc.services.catalog.airing.effective_air_at`, whose corrected
    # times always peak at the last episode's.
    latest: dict[int, datetime] = {}
    if anime_ids:
        aired = await session.execute(
            select(Episode.anime_id, Episode.air_at)
            .where(Episode.anime_id.in_(anime_ids), Episode.air_at.isnot(None))
            .distinct(Episode.anime_id)
            .order_by(Episode.anime_id, Episode.number.desc())
        )
        latest = {anime_id: at_ for anime_id, at_ in aired.all() if at_ is not None}

    statuses = await list_status_for(session, user_id=user.id, anime_ids=anime_ids)
    timezone = user_timezone(user.timezone)
    placement = place_entries(
        [
            ScheduleRow(
                anime=row,
                latest_air_at=latest.get(row.id),
                list_status=statuses.get(row.id),
                carried_over=carried_over,
            )
            for source, carried_over in ((rows, False), (carried, True))
            for row in source
        ],
        tz=timezone,
        now=at,
    )

    previous, upcoming = adjacent_seasons(target_year, target_season.value)
    return SchedulePage.build(
        placement,
        year=target_year,
        season=target_season,
        prev=previous,
        upcoming=upcoming,
        timezone=timezone.key,
    )


__all__ = ["MAX_YEAR", "MIN_YEAR", "now", "router"]
