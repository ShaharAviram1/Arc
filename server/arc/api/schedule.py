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

The one thing here that is about the *caller* rather than the season is
:func:`watched_marks`: FR-W5's answer for the episode each slot names, and only
where that episode has already aired. It is what Watch Now's "✓ Watched" is
drawn from, and confining it to aired slots is the whole of the rule (owner,
2026-09-17).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from arc.api.deps import CurrentUser, SessionDep
from arc.api.schedule_schemas import SchedulePage, SeasonName
from arc.models import Anime, Episode
from arc.services.catalog import list_progress_for, list_status_for
from arc.services.catalog.schedule import (
    ScheduleRow,
    WeekPlacement,
    adjacent_seasons,
    airing_this_week,
    current_season,
    place_entries,
    season_members,
    user_timezone,
)
from arc.services.playback.progress import completed_episode_ids
from arc.services.playback.watched import watched_source

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
        watched=await watched_marks(session, user_id=user.id, placement=placement, now=at),
    )


async def watched_marks(
    session: AsyncSession,
    *,
    user_id: int,
    placement: WeekPlacement,
    now: datetime,
) -> dict[int, bool]:
    """FR-W5's answer for every slot whose named episode has already aired.

    **Only for those slots**, which is the whole rule (owner, 2026-09-17). A
    broadcast that has not happened cannot have been watched, so an upcoming
    appointment gets no entry here and its ``watched`` goes over the wire as
    null — Watch Now used to draw "Episode 23 ✓ Watched" beside Friday's slot,
    having read the tick off the show's latest *aired* episode instead of the
    one the card names.

    Almost always empty, and free when it is: a week's slots point at the next
    broadcast, so the only ones in the past are the show that aired earlier
    today and the row a refresh has not caught up with yet. Three queries when
    there is one of those — the episode ids behind the ``(anime, number)``
    pairs, then FR-W5's two halves through the same helpers every other page
    uses — and none at all when there is not.
    """
    entries = [*placement.unscheduled, *(entry for day in placement.days for entry in day)]
    pairs = [
        (entry.anime.id, entry.next_episode)
        for entry in entries
        if entry.next_episode is not None and entry.next_at is not None and entry.next_at <= now
    ]
    if not pairs:
        return {}

    rows = await session.execute(
        select(Episode.id, Episode.anime_id, Episode.number).where(
            tuple_(Episode.anime_id, Episode.number).in_(pairs)
        )
    )
    found = list(rows.all())
    completed = await completed_episode_ids(
        session, user_id=user_id, episode_ids=[episode_id for episode_id, _, _ in found]
    )
    progress = await list_progress_for(
        session, user_id=user_id, anime_ids=[anime_id for _, anime_id, _ in found]
    )
    return {
        anime_id: watched_source(
            number,
            completed=episode_id in completed,
            list_progress=progress.get(anime_id, 0),
        )
        is not None
        for episode_id, anime_id, number in found
    }


__all__ = ["MAX_YEAR", "MIN_YEAR", "now", "router", "watched_marks"]
