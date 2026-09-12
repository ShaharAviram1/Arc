"""Catalogue endpoints: search, read a show, force a refresh.

Search is **local, then offline, then live** (M15.5). It still goes to a
catalogue source on every call rather than only querying its own tables,
because a search that cannot find a show nobody has added yet is not a search
(FR-C1). But two local answers are matched first and put in front of the live
page:

1. the cached ``anime`` rows — what somebody here has already looked at, which
   is where the answer is for anything on a list or downloading. Upstream is
   not always up (AniList spent M15 disabled) and MyAnimeList — the fallback —
   matches whole words from the start of a title, so "jobless reincarnation"
   found nothing while the show sat cached, on the caller's list, with an
   episode downloading. See :mod:`arc.services.catalog.local`;
2. the **offline catalogue** — 41k titles and every synonym each was released
   under, imported weekly (:mod:`arc.services.catalog.offline.search`). Those
   hits are materialised as ``anime`` rows on the way past, which is what mints
   the internal id their cards link to, and they are written as the weakest
   source, so the first live payload for the same show overwrites all of it.

Two consequences, both deliberate. A search with local or offline hits answers
200 even when both live sources are down — it has something true to say — and
only an empty result from *both* local tables on a failed upstream is a 502.
And the merge happens on the first page only: neither set is paginated, so
repeating them under ``page=2`` would show the same cards twice.

What the cache gets out of the live half is the results: every hit is upserted
on the way past, which is also what gives each result the internal id the rest
of the API is addressed by (FR-C6). The response is built from the stored rows,
not from the payload, so a card and a show page can never disagree about a
title.

Reading a show is the opposite: cache first, upstream only when the row is
missing, older than a day, or filled from MAL while AniList is healthy again
(FR-C5, FR-C6).

The refresh endpoint takes the job type and its dedupe key from
:mod:`arc.services.catalog.names` rather than spelling them out: the worker's
sweeps queue the same work under the same key, and a second copy of that string
here would be a second chance for a manual refresh and a swept one to stop
deduplicating against each other. That module is handler-free, so importing it
does not drag the job registry into the request path the way importing
``catalog.jobs`` would.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.api.anime_schemas import (
    AnimeDetail,
    AnimeSummary,
    MalSyncOut,
    RelatedAnime,
    RelationOut,
    SearchPage,
)
from arc.api.deps import AdminUser, AnimeId, CatalogDep, CurrentUser, SessionDep
from arc.api.episode_extras import episode_extras
from arc.api.jobs import JobOut
from arc.models import Anime, Job, ListEntry
from arc.services.catalog import (
    CATALOGUE_UNAVAILABLE,
    SourceNotFound,
    SourceUnavailable,
    ensure_anime,
    episodes_for,
    list_status_for,
    local_search,
    upsert_summaries,
)
from arc.services.catalog.names import CATALOG_PRIORITY
from arc.services.catalog.names import REFRESH as REFRESH_JOB
from arc.services.catalog.names import dedupe_key as refresh_dedupe_key
from arc.services.catalog.offline.materialise import upsert_offline_summaries
from arc.services.catalog.offline.search import offline_search
from arc.services.jobs import enqueue
from arc.services.mal.names import is_linked
from arc.services.mal.writelog import SyncState, sync_state
from arc.services.playback.progress import completed_episode_ids

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/anime", tags=["anime"])

ANIME_NOT_FOUND = "anime not found"

#: Shortest query worth sending upstream. One character matches most of
#: AniList and costs a request to say so.
MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 100
MAX_PAGE = 50

#: How many cards the two local halves may fill between them before the live
#: page starts. ``local.py``'s twenty is one screen, and the offline catalogue
#: answers *every* query — it has every title there is — so an uncapped second
#: block would put forty cards in front of the results the user can only get
#: from upstream.
PRE_LIVE_LIMIT = 20


def now() -> datetime:
    """The clock the episode list's ``aired`` flags are rendered against.

    A function rather than an inline ``datetime.now`` so a test can pin it:
    "has episode 6 aired yet" is the one answer on the show page that changes
    on its own, and asserting it against a fixture's fixed air times needs a
    fixed present to compare them with.
    """
    return datetime.now(UTC)


async def _related_anime(session: SessionDep, anime: Anime) -> dict[tuple[str, int], RelatedAnime]:
    """``(source, external id) → the cached row`` for this show's relations.

    One query for the whole relation list, and named columns rather than whole
    ``Anime`` rows: the franchise rail (M15) renders a cover, a format, an
    episode count and a year, and a dozen full rows would carry a dozen
    synopses and relation blobs along for the ride.

    Most relations resolve to nothing — a sequel nobody has opened has no row —
    and the client renders those unlinked, with the placeholders the rail uses
    for artwork it does not have. Nothing is fetched to change that: see
    :class:`~arc.api.anime_schemas.RelationOut`.
    """
    anilist_ids, mal_ids = RelationOut.external_ids(anime)
    if not anilist_ids and not mal_ids:
        return {}
    clauses = []
    if anilist_ids:
        clauses.append(Anime.anilist_id.in_(anilist_ids))
    if mal_ids:
        clauses.append(Anime.mal_id.in_(mal_ids))
    rows = await session.execute(
        select(
            Anime.id,
            Anime.anilist_id,
            Anime.mal_id,
            Anime.cover_url,
            Anime.cover_large_url,
            Anime.format,
            Anime.episodes,
            Anime.season_year,
        ).where(or_(*clauses))
    )
    found: dict[tuple[str, int], RelatedAnime] = {}
    for row in rows.all():
        related = RelatedAnime(
            id=row.id,
            cover_url=row.cover_url,
            cover_large_url=row.cover_large_url,
            format=row.format,
            episodes=row.episodes,
            season_year=row.season_year,
        )
        if row.anilist_id is not None:
            found[("anilist", row.anilist_id)] = related
        if row.mal_id is not None:
            found[("mal", row.mal_id)] = related
    return found


async def _offline_results(session: AsyncSession, q: str, local: list[Anime]) -> list[Anime]:
    """The offline catalogue's hits for ``q``, as ``anime`` rows (M15.5).

    Narrowed twice before anything is written. The query asks for only as many
    rows as the cache left room for (:data:`PRE_LIVE_LIMIT`), and the shows the
    cache already returned are dropped by external id before the upsert rather
    than after it — the de-duplication downstream would catch them anyway, but
    only after writing a page of rows to say what Arc had already said.

    A deployment that has never run the import has an empty table and gets an
    empty list, which is precisely the behaviour search had before M15.5.
    """
    room = PRE_LIVE_LIMIT - len(local)
    if room <= 0:
        return []
    known = {row.anilist_id for row in local} | {row.mal_id for row in local}
    known.discard(None)
    rows = [
        row
        for row in await offline_search(session, q, limit=room)
        if row.anilist_id not in known and row.mal_id not in known
    ]
    if not rows:
        return []
    return await upsert_offline_summaries(session, rows)


@router.get(
    "/search",
    response_model=SearchPage,
    summary="Search the catalogue by title (FR-C1)",
    responses={502: {"description": CATALOGUE_UNAVAILABLE}},
)
async def search(
    user: CurrentUser,
    session: SessionDep,
    catalog: CatalogDep,
    q: Annotated[str, Query(min_length=MIN_QUERY_LENGTH, max_length=MAX_QUERY_LENGTH)],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
) -> SearchPage:
    # The cached rows first, then the offline catalogue (see the module
    # docstring), and only on the first page, because neither is paginated.
    local = await local_search(session, q, user_id=user.id) if page == 1 else []
    offline = await _offline_results(session, q, local) if page == 1 else []
    if offline:
        # The materialised rows are somebody's cards; they must survive the
        # request whether or not the live half goes on to fail.
        await session.commit()

    live: list[Anime] = []
    live_page, has_next = page, False
    try:
        found = await catalog.search(q, page=page)
    except SourceUnavailable as exc:
        log.warning("catalogue search failed", extra={"query": q, "error": str(exc)})
        # An outage is only an error when Arc has nothing of its own to say —
        # which, since M15.5, means neither the cache nor the offline import.
        if not local and not offline:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=CATALOGUE_UNAVAILABLE
            ) from exc
    else:
        # Cache what came back before answering. These are summary-only
        # upserts: they fill in the columns a card needs and deliberately leave
        # ``refreshed_at`` alone, so opening one of these results still
        # triggers a full fetch. They are also what mints the internal ids in
        # the response.
        live = await upsert_summaries(session, found.results)
        await session.commit()
        live_page, has_next = found.page, found.has_next

    # De-duplicated on the internal id, not on an external one: the whole point
    # of that id is that one show is one row whichever source found it, so a
    # cached hit, an offline hit and the live result for the same show are one
    # card. The offline rows have internal ids by now — materialising them is
    # what mints those — so the same one line covers all three.
    seen = {row.id for row in local}
    rows = list(local)
    for row in offline + live:
        if row.id in seen:
            continue
        seen.add(row.id)
        rows.append(row)

    statuses = await list_status_for(session, user_id=user.id, anime_ids=[row.id for row in rows])
    return SearchPage(
        results=[AnimeSummary.from_anime(row, statuses.get(row.id)) for row in rows],
        page=live_page,
        has_next=has_next,
    )


@router.get(
    "/{anime_id}",
    response_model=AnimeDetail,
    summary="One show, with its episode list",
    responses={404: {"description": ANIME_NOT_FOUND}, 502: {"description": CATALOGUE_UNAVAILABLE}},
)
async def detail(
    anime_id: AnimeId,
    user: CurrentUser,
    session: SessionDep,
    catalog: CatalogDep,
) -> AnimeDetail:
    """``anime_id`` is Arc's internal id, not AniList's (FR-C6)."""
    try:
        anime = await ensure_anime(session, catalog, anime_id=anime_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ANIME_NOT_FOUND) from exc
    except SourceUnavailable as exc:
        # ``ensure_anime`` already falls back to a cached row when there is
        # one, so reaching here means Arc has never seen this show.
        log.warning("catalogue fetch failed", extra={"anime_id": anime_id, "error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=CATALOGUE_UNAVAILABLE
        ) from exc
    await session.commit()

    episodes = await episodes_for(session, anime.id)
    entry = await session.get(ListEntry, (user.id, anime.id))
    # The download percentage, the preparing percentage, the failure sentence
    # and the rendition all live outside ``episodes`` (FR-A7, FR-P1, FR-P4);
    # three queries for the whole list rather than three per episode.
    episode_ids = [episode.id for episode in episodes]
    extras = await episode_extras(session, episode_ids)
    return AnimeDetail.build(
        anime,
        episodes=episodes,
        now=now(),
        list_entry=entry,
        # The MyAnimeList badge (FR-M6). One query per show page, and only
        # here: it is not on a card and not in the list, so nothing else pays
        # for it.
        mal_sync=_mal_sync(
            await sync_state(
                session,
                user_id=user.id,
                anime_id=anime.id,
                linked=await is_linked(session, user.id),
            )
        ),
        # One query for the whole list: which of these the caller has finished
        # (FR-S4). The tick on a show page is per user, so it cannot come from
        # the episode row.
        watched=await completed_episode_ids(session, user_id=user.id, episode_ids=episode_ids),
        related=await _related_anime(session, anime),
        torrents=extras.torrents,
        renditions=extras.renditions,
        transcode_jobs=extras.transcode_jobs,
    )


def _mal_sync(state: SyncState) -> MalSyncOut:
    """The service's answer as the API's shape."""
    return MalSyncOut(state=state.state, error=state.error, last_write_at=state.last_write_at)


@router.post(
    "/{anime_id}/refresh",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a catalogue refresh for one show (admin)",
)
async def refresh(
    anime_id: AnimeId,
    admin: AdminUser,
    session: SessionDep,
    response: Response,
) -> Job:
    """Enqueue, do not fetch.

    An admin pressing this must not wait on a catalogue source inside a
    request, and two admins pressing it twice must not produce two fetches —
    hence a job with a dedupe key rather than a call. The id is not validated
    here: the handler does that, and a bad id becomes a failed job with a
    readable error instead of a slow 404.
    """
    job = await enqueue(
        session,
        REFRESH_JOB,
        {"anime_id": anime_id},
        priority=CATALOG_PRIORITY,
        dedupe_key=refresh_dedupe_key(anime_id),
    )
    await session.commit()
    return job


__all__ = [
    "ANIME_NOT_FOUND",
    "CATALOGUE_UNAVAILABLE",
    "MIN_QUERY_LENGTH",
    "REFRESH_JOB",
    "now",
    "refresh_dedupe_key",
    "router",
]
