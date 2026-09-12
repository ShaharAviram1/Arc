"""Background TMDB enrichment (M15.5 bullet 4, FR-C6).

Two handlers:

* ``tmdb_enrich`` — one show. Looks its TMDB id up in the offline cross-id map
  (``offline_ids``: AniList publishes no TMDB id, so that file is the only
  route there is), fetches the series or film, the season and the crew, and
  writes whatever :func:`~arc.services.tmdb.enrich.plan_enrichment` says is a
  hole. Idempotent by construction: a second run plans nothing, because every
  column it would fill is now full. With ``{"art_only": true}`` in the payload
  it fetches the show and nothing else — one request, the backdrop and the
  poster — which is what a show nobody follows is worth.
* ``tmdb_enrich_all`` — the nightly sweep (04:10 UTC, after the catalogue's
  own). Two passes, in this order: the shows somebody is **watching** that are
  still missing key art, credits or stills, in full; then the shows of the
  **current and next season** that are mapped and still have no backdrop or no
  key-art poster, art-only, most popular first. The second pass is why the
  Home hero has artwork at all — the hero offers shows the viewer does *not*
  follow (``client/src/pages/Home.tsx``), and under the old followed-only rule
  none of them could ever be reached (owner, 2026-09-12).

"Watching" is wider than "on a list" (:func:`_worth_enriching`): a list entry,
playback progress on an episode, or a ready episode in the library. Arc plays
what it holds whether or not the show was ever added to a list, and a show
watched off-list could otherwise never gain an episode still — which is what
put an AniList banner, cropped to a sliver, on the Continue watching card
(owner, 2026-09-12).

Both are no-ops without ``TMDB_API_KEY``, with one INFO line rather than a
failure: a deployment with no key is a complete Arc that renders AniList's art
(architecture.md §9), and a nightly job that failed instead would be a red row
in the queue view for ever.

**Cost.** A full enrichment is three requests — the show, its season, its crew
— and an art-only one is a single ``/tv/{id}``, paced by the client at four a
second and spaced by :data:`SPACING_SECONDS` between shows. :data:`SWEEP_LIMIT`
caps a night at three hundred shows, so a library that has just gained the
feature fills in over a few nights rather than in one burst.
"""

from __future__ import annotations

import logging
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, Select, Text, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    OfflineId,
    Rendition,
    WatchProgress,
)
from arc.services.catalog.cache import episodes_for
from arc.services.catalog.jobs import FOLLOWED_STATUSES
from arc.services.catalog.seasons import current_season, next_season
from arc.services.jobs.queue import ACTIVE_STATUSES, DEDUPE_FIELD, enqueue, find_active
from arc.services.jobs.registry import JobContext, register
from arc.services.tmdb.client import TmdbClient, TmdbError, TmdbNotFound, TmdbUnavailable
from arc.services.tmdb.enrich import (
    TmdbPayloads,
    apply_enrichment,
    plan_enrichment,
    resolve_season,
    summarise,
)
from arc.services.tmdb.names import TMDB_ENRICH, TMDB_ENRICH_ALL, TMDB_PRIORITY, dedupe_key

log = logging.getLogger(__name__)

#: Seconds between the children of a sweep. Generous for a source that allows
#: fifty requests a second: the point is not TMDB's limit but Arc's own queue —
#: an enrichment holds a worker slot for three round trips, and nothing should
#: ever be behind two hundred of them.
SPACING_SECONDS = 2.0

#: How many shows one sweep will queue, both passes together. A cap rather
#: than a limit: the sweep runs every night and the shows it skips are the same
#: shows tomorrow. Three hundred rather than two because a season is around two
#: hundred shows on its own and the followed pass must not be able to crowd it
#: out entirely — at one request each and four a second, a full night's worth
#: is still a few minutes of TMDB's time.
SWEEP_LIMIT = 300

#: How many shows the Home hero may queue art for in one request
#: (:func:`enqueue_hero_art`). The hero cycles six slides and picks them from
#: the most popular unfollowed shows of the season, so twice that is the pool
#: it chooses from with room for the ones already on the viewer's list.
HERO_ART_LIMIT = 12

#: How many shows the Home page may queue episode stills for in one request
#: (:func:`enqueue_episode_stills`). The client shows eight cards per shelf
#: (``SHELF_LIMIT`` in ``client/src/pages/Home.tsx``) and the shelves that
#: carry a 16:9 card are Continue watching and Ready to watch, so this is one
#: shelf's worth: the cards a viewer can actually see without scrolling past
#: the fold, and a bound on the work one GET can create.
STILL_LIMIT = 8

#: A ``credits`` list this short is the studio row and nothing else — what a
#: MAL detail fetch leaves behind — and counts as "no credits" for the sweep.
#: The precise test is :func:`arc.services.tmdb.enrich._has_person_credit`;
#: this is the cheap SQL approximation of it, and it only has to be right
#: about which rows are worth *looking* at.
CREDITS_MIN_ROWS = 2

#: The message logged, once, when there is no key. Named so the tests can
#: assert on the thing an operator would grep for rather than on a substring.
NO_KEY_MESSAGE = "tmdb enrichment skipped: TMDB_API_KEY is not set"


def _api_key(ctx: JobContext) -> str | None:
    """The key, or ``None`` after logging why there will be no enrichment."""
    key = (ctx.settings.tmdb_api_key or "").strip()
    if not key:
        ctx.log.info(NO_KEY_MESSAGE)
        return None
    return key


async def tmdb_ids_for(session: AsyncSession, anime: Anime) -> OfflineId | None:
    """The cross-id row for this show, if the offline import has one.

    Looked up by AniList id first and MAL id second, the same precedence the
    cache matches payloads to rows with. A row that carries neither TMDB id is
    no use and comes back as ``None``: it exists, it just does not go where
    this job needs to go.
    """
    clauses = []
    if anime.anilist_id is not None:
        clauses.append(OfflineId.anilist_id == anime.anilist_id)
    if anime.mal_id is not None:
        clauses.append(OfflineId.mal_id == anime.mal_id)
    if not clauses:
        return None

    row: OfflineId | None = await session.scalar(
        select(OfflineId)
        .where(
            or_(*clauses),
            or_(OfflineId.tmdb_tv_id.isnot(None), OfflineId.tmdb_movie_id.isnot(None)),
        )
        # An AniList match is the better one: the id map keys on AniDB and both
        # of Arc's ids are attached to it, so a MAL-only match is the row that
        # agreed about less.
        .order_by(OfflineId.anilist_id.is_(None), OfflineId.id)
        .limit(1)
    )
    return row


async def _fetch(
    client: TmdbClient, anime: Anime, ids: OfflineId, *, log: logging.Logger, art_only: bool = False
) -> TmdbPayloads:
    """The three payloads one enrichment reads, as far as TMDB will give them.

    A film has no season; a series whose season cannot be resolved gets its art
    and no stills (see :func:`resolve_season`). A failed *credits* call is not
    fatal either — the backdrop that already arrived is worth writing on its
    own — but a failed show call is: there is nothing to plan from.

    ``art_only`` stops after the show itself: one request, which is what the
    season pass spends on a show nobody follows. The stills and the crew are
    for a page somebody will actually open, and the show call alone carries
    both the backdrop and the poster the hero needs.
    """
    if ids.tmdb_tv_id is not None:
        show = await client.tv(ids.tmdb_tv_id)
        if art_only:
            return TmdbPayloads(show=show)
        season_number = resolve_season(anime, show, mapped=ids.tmdb_season)
        season = None
        if season_number is not None:
            try:
                season = await client.tv_season(ids.tmdb_tv_id, season_number)
            except TmdbNotFound:
                log.info(
                    "tmdb has no such season; art only",
                    extra={
                        "anime_id": anime.id,
                        "tmdb_tv_id": ids.tmdb_tv_id,
                        "season": season_number,
                    },
                )
        credits = await _credits(client.tv_credits(ids.tmdb_tv_id), log=log, anime_id=anime.id)
        return TmdbPayloads(show=show, season=season, credits=credits, season_number=season_number)

    assert ids.tmdb_movie_id is not None  # tmdb_ids_for guarantees one of the two
    show = await client.movie(ids.tmdb_movie_id)
    if art_only:
        return TmdbPayloads(show=show)
    credits = await _credits(client.movie_credits(ids.tmdb_movie_id), log=log, anime_id=anime.id)
    return TmdbPayloads(show=show, credits=credits)


async def _credits(
    call: Coroutine[Any, Any, dict[str, Any]], *, log: logging.Logger, anime_id: int
) -> dict[str, Any] | None:
    """Await a credits call, turning a "not found" into a missing block.

    Takes the coroutine rather than the client so the series and film paths
    share one rule. A :class:`TmdbUnavailable` is *not* caught: the breaker has
    opened and the rest of the sweep should stop, not grind.
    """
    try:
        return await call
    except TmdbNotFound:
        log.info("tmdb has no credits for this title", extra={"anime_id": anime_id})
        return None


@register(TMDB_ENRICH)
async def tmdb_enrich(ctx: JobContext) -> None:
    """Fill one show's missing key art, stills and credits from TMDB.

    ``{"art_only": true}`` narrows it to the backdrop and the poster — one
    request instead of three. See the module docstring for who gets which.
    """
    anime_id = int(ctx.payload["anime_id"])
    art_only = bool(ctx.payload.get("art_only", False))
    if _api_key(ctx) is None:
        return

    anime = await ctx.session.get(Anime, anime_id, populate_existing=True)
    if anime is None:
        ctx.log.info("tmdb enrichment skipped: no such show", extra={"anime_id": anime_id})
        return

    ids = await tmdb_ids_for(ctx.session, anime)
    if ids is None:
        ctx.log.info(
            "tmdb enrichment skipped: the id map has no tmdb id for this show",
            extra={"anime_id": anime_id},
        )
        return

    async with TmdbClient.from_settings(ctx.settings) as client:
        try:
            payloads = await _fetch(client, anime, ids, log=ctx.log, art_only=art_only)
        except TmdbNotFound as exc:
            # The id map is a weekly snapshot of somebody else's file and does
            # go stale. Nothing to retry and nothing to fix here.
            ctx.log.info(
                "tmdb enrichment skipped: the mapped id is gone",
                extra={"anime_id": anime_id, "error": str(exc)},
            )
            return

    # An art-only run has no season payload to match rows against, so the
    # lookup is skipped rather than made and thrown away.
    episodes = [] if art_only else await episodes_for(ctx.session, anime_id)
    plan = plan_enrichment(anime, episodes, payloads, art_only=art_only)
    touched = await apply_enrichment(ctx.session, anime, plan)
    ctx.log.info(
        "tmdb enrichment",
        extra={
            "anime_id": anime_id,
            "art_only": art_only,
            "tmdb_tv_id": ids.tmdb_tv_id,
            "tmdb_movie_id": ids.tmdb_movie_id,
            "season": payloads.season_number,
            "episode_fields": touched,
            **summarise(plan),
        },
    )


def _mapped() -> ColumnElement[bool]:
    """Whether the offline id map can reach this row at all.

    The clause that keeps every sweep honest: without it a deployment whose
    offline import has not run yet would queue a job per candidate, and every
    one of them would find no id and log a skip.
    """
    return exists().where(
        or_(
            and_(Anime.anilist_id.isnot(None), OfflineId.anilist_id == Anime.anilist_id),
            and_(Anime.mal_id.isnot(None), OfflineId.mal_id == Anime.mal_id),
        ),
        or_(OfflineId.tmdb_tv_id.isnot(None), OfflineId.tmdb_movie_id.isnot(None)),
    )


def _in_current_seasons(now: datetime | None = None) -> ColumnElement[bool]:
    """Whether the row is one of this season's shows, or next season's.

    Both, because the hero offers what is airing *and* what is about to: the
    season grid the client builds it from links the two (``schedule.next``),
    and a show whose art arrives the week it premieres is a week late.
    """
    year, season = current_season(now)
    upcoming = next_season(year, season)
    return or_(
        and_(Anime.season_year == year, Anime.season == season),
        and_(Anime.season_year == upcoming[0], Anime.season == upcoming[1]),
    )


def _missing_key_art() -> ColumnElement[bool]:
    """No backdrop, or no key-art poster. Either is a hole TMDB fills."""
    return or_(Anime.banner_url.is_(None), Anime.cover_large_url.is_(None))


def _already_queued() -> ColumnElement[bool]:
    """Whether an enrichment for this row is already pending or running.

    The same test :func:`~arc.services.jobs.queue.find_active` makes, written
    as a correlated EXISTS so a caller can leave those rows out of a SELECT
    rather than asking about them one at a time. :func:`enqueue_enrichment`
    still checks per show — this is the cheap filter, not the guarantee.
    """
    key = cast(ColumnElement[str], Job.payload[DEDUPE_FIELD].astext)
    return exists().where(
        Job.type == TMDB_ENRICH,
        Job.status.in_(ACTIVE_STATUSES),
        key == func.concat(f"{TMDB_ENRICH}:", Anime.id.cast(Text)),
    )


def _watched_by_anyone() -> ColumnElement[bool]:
    """Whether any user has playback progress on an episode of this show.

    Somebody is watching it, whatever their list says. A show can be watched
    without being on a list at all — Arc plays whatever it holds — and under
    the list-only rule that show could never gain a single episode still
    (owner, 2026-09-12: the Continue watching card had nothing but a banner to
    fall back on).
    """
    return exists().where(
        Episode.anime_id == Anime.id,
        WatchProgress.episode_id == Episode.id,
    )


def _in_the_library() -> ColumnElement[bool]:
    """Whether Arc holds an episode of this show that is ready to play.

    The other half of "somebody is actually watching it": the file is here and
    transcoded, so the show is one click from a player and its cards are worth
    filling in. Two EXISTS rather than one over a join — an episode in the
    ``ready`` state and a rendition that finished are the same fact recorded
    twice, and a single subquery naming both tables would cross-join them and
    answer "no" on a deployment whose ``renditions`` table is empty.
    """
    ready_state = exists().where(
        Episode.anime_id == Anime.id,
        Episode.state == EpisodeState.READY,
    )
    ready_rendition = exists().where(
        Episode.anime_id == Anime.id,
        Rendition.episode_id == Episode.id,
        Rendition.ready_at.isnot(None),
    )
    return or_(ready_state, ready_rendition)


def _worth_enriching() -> ColumnElement[bool]:
    """Whether a show is worth all three requests rather than the art alone.

    Three ways in, any one of which is enough: somebody follows it, somebody
    has playback progress on an episode of it, or Arc holds a ready episode of
    it. The last two are what widened the rule on 2026-09-12 — a show being
    watched off-list is the case the owner hit — and both are the same claim
    as "followed" made by a stronger kind of evidence.
    """
    followed = exists().where(
        ListEntry.anime_id == Anime.id,
        ListEntry.status.in_(FOLLOWED_STATUSES),
    )
    return or_(followed, _watched_by_anyone(), _in_the_library())


def _needs_enrichment() -> Select[tuple[int]]:
    """Shows somebody is watching that have a TMDB id and a hole TMDB could fill.

    Four holes, any one of which is enough: no backdrop, no key-art poster, no
    credits worth the name, or an episode that has aired with no still. The
    credits test is ``jsonb_array_length < 2`` — the cheap SQL stand-in for "a
    studio row and nobody else", which is what a MAL-filled row carries.

    "Somebody is watching it" is :func:`_worth_enriching`, which is wider than
    the list: see there.
    """
    wanted = _worth_enriching()
    mapped = _mapped()
    missing_still = exists().where(
        Episode.anime_id == Anime.id,
        Episode.still_url.is_(None),
        Episode.air_at.isnot(None),
        Episode.air_at <= func.now(),
    )
    # The same rule :func:`~arc.services.tmdb.enrich._may_write_credits`
    # applies, expressed in SQL: a column AniList filled is not a hole however
    # short its answer, and without that clause a row whose AniList credits
    # name only the studio would be swept, fetched and written-nothing every
    # night for ever.
    missing_credits = or_(
        Anime.credits.is_(None),
        and_(
            Anime.detail_source.is_distinct_from("anilist"),
            func.jsonb_array_length(Anime.credits) < CREDITS_MIN_ROWS,
        ),
    )
    incomplete = or_(_missing_key_art(), missing_credits, missing_still)
    return select(Anime.id).where(wanted, mapped, incomplete).order_by(Anime.id).limit(SWEEP_LIMIT)


def _needs_season_art(now: datetime | None = None) -> Select[tuple[int]]:
    """Mapped shows of this season and the next that still have no key art.

    The sweep's second pass, and the one the Home hero depends on: the hero
    offers the season's shows to somebody who follows *none* of them, so the
    followed-only rule above could never reach a single one of them (owner,
    2026-09-12 — "the hero posters are still bad").

    Most popular first, because that is the order the client ranks a season in
    once its own genre test has nothing to say, so the shows a hero is most
    likely to pick are the ones a capped sweep reaches. Art only: see
    :func:`_fetch`.
    """
    return (
        select(Anime.id)
        .where(_in_current_seasons(now), _mapped(), _missing_key_art())
        .order_by(Anime.popularity.desc().nullslast(), Anime.id)
        .limit(SWEEP_LIMIT)
    )


async def _next_free_slot(session: AsyncSession, now: datetime) -> datetime:
    """The first moment a new enrichment can be scheduled without doubling up.

    The same rule the catalogue sweeps follow: count from the last enrichment
    already on the queue rather than from this run's "now", so a sweep that
    meets last night's leftovers does not hand them the same instants.
    """
    latest = await session.scalar(
        select(func.max(Job.run_after)).where(
            Job.type == TMDB_ENRICH, Job.status.in_(ACTIVE_STATUSES)
        )
    )
    if latest is None:
        return now
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return max(now, latest + timedelta(seconds=SPACING_SECONDS))


async def enqueue_enrichment(
    session: AsyncSession,
    anime_id: int,
    *,
    run_after: datetime | None = None,
    art_only: bool = False,
) -> Job | None:
    """Queue one ``tmdb_enrich``, or ``None`` if one is already queued.

    The dedupe is checked before the enqueue rather than left to it so that a
    caller can tell "queued" from "already queued" — the sweep counts one and
    not the other, and a show whose job is already pending must not consume a
    spacing slot.

    One key per show whatever the mode, so the full enrichment a followed show
    gets is never queued twice because something also wanted its backdrop. The
    first caller wins, and the sweep runs the followed pass first for exactly
    that reason: the richer job is the one that should be standing.
    """
    if await find_active(session, TMDB_ENRICH, dedupe_key(anime_id)) is not None:
        return None
    payload: dict[str, Any] = {"anime_id": anime_id}
    if art_only:
        payload["art_only"] = True
    return await enqueue(
        session,
        TMDB_ENRICH,
        payload,
        priority=TMDB_PRIORITY,
        run_after=run_after,
        dedupe_key=dedupe_key(anime_id),
    )


async def enqueue_hero_art(
    session: AsyncSession, *, now: datetime | None = None, limit: int = HERO_ART_LIMIT
) -> int:
    """Art for the shows the Home hero is about to choose between. Returns how many.

    The hero is built on the client out of the season grid and the last
    recommendation run (``client/src/pages/Home.tsx``), so the server cannot
    name the six shows it will land on — but it knows the pool they come from,
    and it is the same one: the season's shows, most popular first. This
    queues art-only enrichments for the top :data:`HERO_ART_LIMIT` of them
    that have **no artwork at all** — neither a banner nor a key-art poster,
    which is the row that makes the hero look broken.

    Cheap enough to sit in a GET: one SELECT, which returns nothing at all once
    the pool is filled in, because a row with either kind of art is out of it
    and a row whose job is already queued is filtered in SQL
    (:func:`_already_queued`). The caller commits.
    """
    candidates = await session.scalars(
        select(Anime.id)
        .where(
            _in_current_seasons(now),
            _mapped(),
            Anime.banner_url.is_(None),
            Anime.cover_large_url.is_(None),
            ~_already_queued(),
        )
        .order_by(Anime.popularity.desc().nullslast(), Anime.id)
        .limit(limit)
    )
    queued = 0
    for anime_id in candidates.all():
        if await enqueue_enrichment(session, anime_id, art_only=True) is not None:
            queued += 1
    return queued


async def enqueue_episode_stills(
    session: AsyncSession, anime_ids: Sequence[int], *, limit: int = STILL_LIMIT
) -> int:
    """Full enrichments for the shows on the Home shelves with no episode still. Returns how many.

    The nightly sweep reaches these shows too (:func:`_needs_enrichment`), but
    "tonight" is the wrong answer for a card the viewer is looking at now: the
    shelves are eight cards each and the one at the front is the episode they
    are half-way through. So the page asks for them itself — a *full*
    enrichment, because a still is the whole point and an art-only run fetches
    none.

    Bounded like the hero's (:data:`STILL_LIMIT`), filtered in SQL to the rows
    the id map can reach and that have no enrichment standing already, and a
    no-op once the stills are in, because a card with a still is not passed in.
    If an art-only job for the same show is already queued this queues nothing:
    one dedupe key per show, first caller wins (:func:`enqueue_enrichment`),
    and the caller here runs before the hero's so the richer job is the one
    that stands. The caller commits.
    """
    # Order preserved, duplicates dropped: both shelves can carry the same
    # show, and the front of Continue watching is the card that matters most.
    wanted = list(dict.fromkeys(anime_ids))[:limit]
    if not wanted:
        return 0

    candidates = await session.scalars(
        select(Anime.id)
        .where(Anime.id.in_(wanted), _mapped(), ~_already_queued())
        .order_by(Anime.id)
    )
    queued = 0
    for anime_id in candidates.all():
        if await enqueue_enrichment(session, anime_id) is not None:
            queued += 1
    return queued


async def sweep_candidates(
    session: AsyncSession, *, now: datetime | None = None
) -> list[tuple[int, bool]]:
    """``(anime_id, art_only)`` for one night's sweep, best claim first.

    The watched shows in full, then the season's in art-only mode, each show
    once and the whole list capped at :data:`SWEEP_LIMIT`. Order is the policy:
    a show somebody is watching is worth three requests and a show nobody has
    heard of is worth one, and when the cap bites it is the second pass that
    loses — those shows are still there tomorrow, and the hero shows the most
    popular of them first anyway.
    """
    followed = list((await session.scalars(_needs_enrichment())).all())
    seen = set(followed)
    season = [
        anime_id
        for anime_id in (await session.scalars(_needs_season_art(now))).all()
        if anime_id not in seen
    ]
    ordered: list[tuple[int, bool]] = [(anime_id, False) for anime_id in followed]
    ordered += [(anime_id, True) for anime_id in season]
    return ordered[:SWEEP_LIMIT]


@register(TMDB_ENRICH_ALL)
async def tmdb_enrich_all(ctx: JobContext) -> None:
    """Nightly sweep: an enrichment for every followed show and every season show that needs one."""
    if _api_key(ctx) is None:
        return

    candidates = await sweep_candidates(ctx.session)
    start = await _next_free_slot(ctx.session, datetime.now(UTC))
    queued = 0
    for anime_id, art_only in candidates:
        job = await enqueue_enrichment(
            ctx.session,
            anime_id,
            run_after=start + timedelta(seconds=queued * SPACING_SECONDS),
            art_only=art_only,
        )
        if job is not None:
            queued += 1
    ctx.log.info(
        "tmdb enrichment sweep",
        extra={
            "candidates": len(candidates),
            "art_only": sum(1 for _, art_only in candidates if art_only),
            "queued": queued,
        },
    )


__all__ = [
    "CREDITS_MIN_ROWS",
    "HERO_ART_LIMIT",
    "NO_KEY_MESSAGE",
    "SPACING_SECONDS",
    "STILL_LIMIT",
    "SWEEP_LIMIT",
    "TMDB_ENRICH",
    "TMDB_ENRICH_ALL",
    "TmdbError",
    "TmdbUnavailable",
    "enqueue_enrichment",
    "enqueue_episode_stills",
    "enqueue_hero_art",
    "sweep_candidates",
    "tmdb_enrich",
    "tmdb_enrich_all",
    "tmdb_ids_for",
]
