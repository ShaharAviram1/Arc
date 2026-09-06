"""Catalogue endpoints: search, read a show, force a refresh.

Search is live — it goes to a catalogue source on every call rather than
querying the local cache — because the cache only holds what somebody has
already looked at, and a search that cannot find a show nobody has added yet is
not a search (FR-C1). What the cache gets out of it is the results: every hit
is upserted on the way past, which is also what gives each result the internal
id the rest of the API is addressed by (FR-C6). The response is built from the
stored rows, not from the payload, so a card and a show page can never disagree
about a title.

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

from arc.api.anime_schemas import AnimeDetail, AnimeSummary, RelationOut, SearchPage
from arc.api.deps import AdminUser, CatalogDep, CurrentUser, SessionDep
from arc.api.jobs import JobOut
from arc.models import Anime, Job, ListEntry
from arc.services.catalog import (
    CATALOGUE_UNAVAILABLE,
    SourceNotFound,
    SourceUnavailable,
    ensure_anime,
    episodes_for,
    list_status_for,
    upsert_summaries,
)
from arc.services.catalog.names import REFRESH as REFRESH_JOB
from arc.services.catalog.names import dedupe_key as refresh_dedupe_key
from arc.services.jobs import enqueue

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/anime", tags=["anime"])

ANIME_NOT_FOUND = "anime not found"

#: Shortest query worth sending upstream. One character matches most of
#: AniList and costs a request to say so.
MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 100
MAX_PAGE = 50


def now() -> datetime:
    """The clock the episode list's ``aired`` flags are rendered against.

    A function rather than an inline ``datetime.now`` so a test can pin it:
    "has episode 6 aired yet" is the one answer on the show page that changes
    on its own, and asserting it against a fixture's fixed air times needs a
    fixed present to compare them with.
    """
    return datetime.now(UTC)


async def _relation_ids(session: SessionDep, anime: Anime) -> dict[tuple[str, int], int]:
    """``(source, external id) → internal id`` for this show's relations.

    One query for the whole relation list. Most relations resolve to nothing —
    a sequel nobody has opened has no row — and the client renders those
    unlinked rather than following an id that would 404.
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
        select(Anime.id, Anime.anilist_id, Anime.mal_id).where(or_(*clauses))
    )
    found: dict[tuple[str, int], int] = {}
    for internal, anilist_id, mal_id in rows.all():
        if anilist_id is not None:
            found[("anilist", anilist_id)] = internal
        if mal_id is not None:
            found[("mal", mal_id)] = internal
    return found


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
    try:
        found = await catalog.search(q, page=page)
    except SourceUnavailable as exc:
        log.warning("catalogue search failed", extra={"query": q, "error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=CATALOGUE_UNAVAILABLE
        ) from exc

    # Cache what came back before answering. These are summary-only upserts:
    # they fill in the columns a card needs and deliberately leave
    # ``refreshed_at`` alone, so opening one of these results still triggers a
    # full fetch. They are also what mints the internal ids in the response.
    rows = await upsert_summaries(session, found.results)
    await session.commit()

    statuses = await list_status_for(session, user_id=user.id, anime_ids=[row.id for row in rows])
    return SearchPage(
        results=[AnimeSummary.from_anime(row, statuses.get(row.id)) for row in rows],
        page=found.page,
        has_next=found.has_next,
    )


@router.get(
    "/{anime_id}",
    response_model=AnimeDetail,
    summary="One show, with its episode list",
    responses={404: {"description": ANIME_NOT_FOUND}, 502: {"description": CATALOGUE_UNAVAILABLE}},
)
async def detail(
    anime_id: int,
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
    # ``watched`` stays empty until M8 writes ``watch_progress``; the shape is
    # here so the client can render the mark from day one.
    return AnimeDetail.build(
        anime,
        episodes=episodes,
        now=now(),
        list_entry=entry,
        relation_ids=await _relation_ids(session, anime),
    )


@router.post(
    "/{anime_id}/refresh",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a catalogue refresh for one show (admin)",
)
async def refresh(
    anime_id: int,
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
