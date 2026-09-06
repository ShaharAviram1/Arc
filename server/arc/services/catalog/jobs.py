"""Background catalogue work (FR-C5, FR-C6, FR-C7).

Five handlers:

* ``catalog_refresh`` — re-fetch one show, unconditionally.
* ``catalog_refresh_all`` — the daily sweep (04:00 UTC). Picks the shows worth
  refreshing and enqueues one ``catalog_refresh`` each, **spaced out**: a
  hundred of them arriving at once would be a hundred upstream requests in a
  minute, which is over AniList's real limit however politely the client paces
  itself. ``run_after`` is what keeps the sweep inside the budget.
* ``catalog_pre_air`` — the hourly sweep. Refreshes anything airing in the next
  90 minutes or aired in the last six hours, which is the half of FR-C5 the
  daily run cannot satisfy ("within one hour of a followed show's scheduled air
  time").
* ``catalog_reconcile`` — the hourly repair pass. Rows that arrived through MAL
  during an outage have no AniList id; this asks AniList for each MAL id it can
  and attaches what comes back, so the next refresh upgrades the row and its
  estimated air dates (FR-C6).
* ``catalog_season_sweep`` — the daily pre-cache (03:30 UTC). Writes this
  season's and next season's summaries so the schedule renders even when both
  sources are down (FR-C7), then queues a spaced-out ``catalog_refresh`` for
  each current-season row the summaries left with no airing information at
  all — the only query that carries a schedule is the detail one.

Every handler is idempotent: a refresh is an upsert, and an enqueue with a
dedupe key is a no-op when the work is already queued.

**Where the catalogue comes from.** Handlers build their own service from
settings rather than sharing the API's. A job runs in the worker, where there
is no ``app.state``, and :class:`JobContext` carries no service bag; adding one
for a single consumer would be speculative. The breaker underneath is still
process-wide (see :mod:`arc.services.catalog.factory`), which is what makes
"only reconcile while AniList is healthy" mean anything.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, exists, func, or_, select

from arc.models import Anime, Episode, Job, ListEntry, ListStatus
from arc.services.catalog.cache import ensure_anime, upsert_summaries
from arc.services.catalog.factory import catalog_for
from arc.services.catalog.names import (
    PRE_AIR,
    RECONCILE,
    REFRESH,
    REFRESH_ALL,
    SEASON_SWEEP,
    dedupe_key,
)
from arc.services.catalog.schedule import SCHEDULED_FORMATS
from arc.services.catalog.seasons import current_season, next_season
from arc.services.catalog.source import SourceNotFound, SourceUnavailable
from arc.services.jobs.queue import ACTIVE_STATUSES, enqueue, find_active
from arc.services.jobs.registry import JobContext, register

#: List states that mean somebody still cares what this show does. Dropped and
#: completed shows are deliberately absent: they generate no wants (FR-W4), so
#: refreshing them buys nothing.
FOLLOWED_STATUSES = (ListStatus.WATCHING, ListStatus.PLANNED, ListStatus.ON_HOLD)

#: Seconds between the children of a sweep — 12 refreshes a minute at most.
#: A refresh is no longer one request: a long-running show pages its aired
#: schedule (:data:`arc.services.anilist.client.MAX_SCHEDULE_PAGES`), so the
#: gap has to cover a job that costs several requests rather than one, and
#: AniList's real ceiling is 30/min, not the documented 90.
SPACING_SECONDS = 5.0

#: How far ahead of an episode's air time to refresh (FR-C5 asks for "within
#: one hour"; 90 minutes leaves room for the sweep's own hourly period).
PRE_AIR_LEAD = timedelta(minutes=90)

#: And how long after, so that an episode which has just aired gets its data
#: (and the next ``nextAiringEpisode``) promptly.
PRE_AIR_TRAIL = timedelta(hours=6)

#: AniList's "currently airing" status.
RELEASING = "RELEASING"

#: How many MAL-only rows one reconciliation run will look up, and how long it
#: waits between them. Fifty at five seconds is a little over four minutes of
#: an hourly job — slow enough to leave the budget for anything a user is
#: waiting on, fast enough to clear a day's outage in an afternoon.
RECONCILE_LIMIT = 50
RECONCILE_SPACING_SECONDS = 5.0

#: How many unplaced rows one season sweep will follow up with a detail fetch
#: (:func:`_enqueue_season_details`). Sixty at five seconds is five minutes of
#: queue for a job that runs once a day, so a fresh season fills in over a few
#: nights rather than in one burst that AniList would throttle.
SEASON_DETAIL_LIMIT = 60


async def _sleep(seconds: float) -> None:
    """Indirection so a test can skip the reconciliation's pacing."""
    await asyncio.sleep(seconds)


@register(REFRESH)
async def catalog_refresh(ctx: JobContext) -> None:
    """Re-fetch one show and rewrite its cache rows."""
    anime_id = int(ctx.payload["anime_id"])
    async with catalog_for(ctx.settings) as catalog:
        # max_age=0 forces the fetch: the point of this job is that whatever
        # is cached is not to be trusted.
        anime = await ensure_anime(ctx.session, catalog, anime_id=anime_id, max_age=timedelta(0))
    ctx.log.info(
        "catalogue refresh",
        extra={
            "anime_id": anime.id,
            "source": anime.detail_source,
            "status": anime.status,
            "episodes": anime.episodes,
        },
    )


async def _next_free_slot(ctx: JobContext, now: datetime) -> datetime:
    """The first moment a new refresh can be scheduled without doubling up.

    The sweeps run on separate schedules and meet every few hours; each one
    counting its slots from its own "now" would hand them all the same
    instants, and two dozen refreshes in a minute is over AniList's real limit
    whatever the client's pacing does about it. So a sweep starts one spacing
    slot after the last refresh *already* on the queue, and only falls back to
    now when the queue is empty.
    """
    latest = await ctx.session.scalar(
        select(func.max(Job.run_after)).where(Job.type == REFRESH, Job.status.in_(ACTIVE_STATUSES))
    )
    if latest is None:
        return now
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return max(now, latest + timedelta(seconds=SPACING_SECONDS))


async def _enqueue_refreshes(ctx: JobContext, anime_ids: list[int]) -> int:
    """Queue one spaced-out ``catalog_refresh`` per show; return how many were new.

    The dedupe is checked here rather than left to ``enqueue`` so that a show
    whose refresh is already queued does not consume a spacing slot — twenty
    already-queued shows would otherwise push the twenty-first a minute into
    the future for no reason.
    """
    now = datetime.now(UTC)
    start = await _next_free_slot(ctx, now)
    queued = 0
    for anime_id in anime_ids:
        key = dedupe_key(anime_id)
        if await find_active(ctx.session, REFRESH, key) is not None:
            continue
        await enqueue(
            ctx.session,
            REFRESH,
            {"anime_id": anime_id},
            run_after=start + timedelta(seconds=queued * SPACING_SECONDS),
            dedupe_key=key,
        )
        queued += 1
    return queued


@register(REFRESH_ALL)
async def catalog_refresh_all(ctx: JobContext) -> None:
    """Daily sweep: refresh every show anybody follows, plus everything airing.

    The second half matters even when nobody follows the show: the seasonal
    schedule (M4) renders from the same ``anime`` rows, so a releasing title
    with a stale ``nextAiringEpisode`` is a wrong weekday on the schedule page.
    """
    followed = exists().where(
        ListEntry.anime_id == Anime.id,
        ListEntry.status.in_(FOLLOWED_STATUSES),
    )
    statement = select(Anime.id).where(or_(followed, Anime.status == RELEASING)).order_by(Anime.id)
    anime_ids = list((await ctx.session.scalars(statement)).all())
    queued = await _enqueue_refreshes(ctx, anime_ids)
    ctx.log.info("catalogue daily sweep", extra={"candidates": len(anime_ids), "queued": queued})


@register(PRE_AIR)
async def catalog_pre_air(ctx: JobContext) -> None:
    """Hourly sweep: refresh anything airing very soon or just aired (FR-C5)."""
    now = datetime.now(UTC)
    lower = int((now - PRE_AIR_TRAIL).timestamp())
    upper = int((now + PRE_AIR_LEAD).timestamp())

    # ``next_airing`` is AniList's blob; ``airingAt`` inside it is epoch
    # seconds, so the comparison is done in the same units rather than by
    # converting a thousand rows in Python.
    airing_at = Anime.next_airing["airingAt"].astext.cast(Integer)
    statement = (
        select(Anime.id)
        .where(Anime.next_airing.isnot(None), airing_at >= lower, airing_at <= upper)
        .order_by(Anime.id)
    )
    anime_ids = list((await ctx.session.scalars(statement)).all())
    queued = await _enqueue_refreshes(ctx, anime_ids)
    ctx.log.info("catalogue pre-air sweep", extra={"candidates": len(anime_ids), "queued": queued})


@register(RECONCILE)
async def catalog_reconcile(ctx: JobContext) -> None:
    """Attach AniList ids to rows that arrived through MAL (FR-C6).

    Asks AniList *specifically* — not the service — because the whole question
    is "does AniList know this show", and letting the fallback answer it would
    return the MAL record Arc already has and learn nothing.

    Skipped entirely while AniList's breaker is open: fifty lookups against a
    source that is down is fifty timeouts and no ids.
    """
    async with catalog_for(ctx.settings) as catalog:
        if not catalog.healthy("anilist"):
            ctx.log.info("catalogue reconcile skipped; anilist is not healthy")
            return

        statement = (
            select(Anime)
            .where(Anime.mal_id.isnot(None), Anime.anilist_id.is_(None))
            .order_by(Anime.id)
            .limit(RECONCILE_LIMIT)
        )
        rows = list((await ctx.session.scalars(statement)).all())
        attached = 0
        for index, row in enumerate(rows):
            if index:
                await _sleep(RECONCILE_SPACING_SECONDS)
            assert row.mal_id is not None  # the WHERE clause said so
            try:
                media = await catalog.primary.by_mal_id(row.mal_id)
            except SourceNotFound:
                # "No such title" is AniList answering, not failing.
                media = None
            except SourceUnavailable as exc:
                # Stop rather than grind: the rest of this batch would fail the
                # same way, and the next hourly run will pick up where this one
                # left off.
                catalog.breaker.record_failure("anilist", exc.reason)
                ctx.log.warning(
                    "catalogue reconcile stopped; anilist became unavailable",
                    extra={"checked": index, "attached": attached, "error": str(exc)},
                )
                break

            # This job talks to the source directly rather than through
            # ``CatalogService._first``, so nothing else would tell the breaker
            # that AniList is answering — and an hourly job that only ever
            # reports failures leaves a stale ``failed_at`` on the admin view.
            catalog.breaker.record_success("anilist")
            if media is None or media.anilist_id is None:
                continue
            clash = await ctx.session.scalar(
                select(Anime.id).where(Anime.anilist_id == media.anilist_id)
            )
            if clash is not None:
                ctx.log.warning(
                    "catalogue reconcile found an anilist id already in use",
                    extra={"anime_id": row.id, "anilist_id": media.anilist_id, "holder": clash},
                )
                continue
            row.anilist_id = media.anilist_id
            attached += 1

        await ctx.session.flush()
        ctx.log.info("catalogue reconcile", extra={"candidates": len(rows), "attached": attached})


async def _enqueue_season_details(ctx: JobContext, year: int, season: str) -> int:
    """Queue a detail fetch for every current-season row the schedule cannot place.

    The season query carries ``nextAiringEpisode`` and nothing else about
    airing, so a show that is between broadcasts — finished, on a break, or not
    yet started — comes out of the sweep with no slot and no episodes, and the
    schedule has nothing to put it on a weekday with (FR-C3). Only the *detail*
    query asks for ``airingSchedule``, which is where the last episode's air
    time comes from, so those rows have to be fetched one at a time.

    Deliberately narrow: only the weekly formats (a film has no weekday to
    recover), only rows with neither a slot nor a single episode row, only the
    current season, and only :data:`SEASON_DETAIL_LIMIT` of them per run. The
    enqueue goes through :func:`_enqueue_refreshes`, so these share the sweeps'
    spacing and dedupe key rather than inventing a second budget.
    """
    statement = (
        select(Anime.id)
        .where(
            Anime.season == season,
            Anime.season_year == year,
            Anime.format.in_(sorted(SCHEDULED_FORMATS)),
            Anime.next_airing.is_(None),
            ~exists().where(Episode.anime_id == Anime.id),
        )
        .order_by(Anime.id)
        .limit(SEASON_DETAIL_LIMIT)
    )
    anime_ids = list((await ctx.session.scalars(statement)).all())
    queued = await _enqueue_refreshes(ctx, anime_ids)
    ctx.log.info(
        "catalogue season sweep queued detail fetches for unplaced rows",
        extra={"year": year, "season": season, "candidates": len(anime_ids), "queued": queued},
    )
    return queued


@register(SEASON_SWEEP)
async def catalog_season_sweep(ctx: JobContext) -> None:
    """Cache this season and the next one (FR-C7).

    Summaries only, so a title somebody opens still gets a full fetch. The
    point is that the *schedule* renders from local rows: a day when neither
    source answers should cost the airing times, not the season itself.

    The summaries alone leave a hole, though: a season row between broadcasts
    has no ``nextAiringEpisode`` and no episodes, and the schedule can place
    neither. So the sweep finishes by queueing a spaced-out detail fetch for
    those (:func:`_enqueue_season_details`), which is what brings back the
    ``airingSchedule`` the weekday is recovered from.
    """
    year, season = current_season()
    upcoming = next_season(year, season)
    cached = 0
    async with catalog_for(ctx.settings) as catalog:
        for target_year, target_season in ((year, season), upcoming):
            try:
                media = await catalog.season(target_year, target_season)
            except SourceUnavailable as exc:
                ctx.log.warning(
                    "catalogue season sweep skipped a season",
                    extra={"year": target_year, "season": target_season, "error": str(exc)},
                )
                continue
            rows = await upsert_summaries(ctx.session, media)
            cached += len(rows)
            ctx.log.info(
                "catalogue season cached",
                extra={"year": target_year, "season": target_season, "titles": len(rows)},
            )
    queued = await _enqueue_season_details(ctx, year, season)
    ctx.log.info("catalogue season sweep", extra={"titles": cached, "queued": queued})


__all__ = [
    "FOLLOWED_STATUSES",
    "PRE_AIR",
    "PRE_AIR_LEAD",
    "PRE_AIR_TRAIL",
    "RECONCILE",
    "RECONCILE_LIMIT",
    "RECONCILE_SPACING_SECONDS",
    "REFRESH",
    "REFRESH_ALL",
    "SEASON_DETAIL_LIMIT",
    "SEASON_SWEEP",
    "SPACING_SECONDS",
    "catalog_pre_air",
    "catalog_reconcile",
    "catalog_refresh",
    "catalog_refresh_all",
    "catalog_season_sweep",
    "dedupe_key",
]
