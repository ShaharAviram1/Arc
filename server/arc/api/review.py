"""The match-review queue over HTTP (FR-L4, FR-L6, FR-D4).

Phase 1 is API only; the page lands in M13. The contract is already the one the
page needs: list, a cheap count for the sidebar badge, confirm, ignore, reopen,
and a search so somebody can pick a title the matcher never proposed.

**Who may use it.** Any signed-in user, not just an admin. spec §2 gives an
ordinary user "resolve match-review items for their own requested shows", and
in a single-library server every file is potentially somebody's — there is no
per-user ownership on ``media_files`` to filter by. Admin-only would mean the
owner resolves everybody's queue, which is the opposite of the intent. The
admin-wide view (FR-D4) is this same endpoint.

**What it never returns.** An absolute path. See
:mod:`arc.api.review_schemas`.

**What confirming does.** Exactly what an automatic link does, through the same
function (:func:`arc.services.library.link.link`), with ``review_state`` set to
``confirmed`` instead of ``auto``, and with ``match_confidence`` set back to
null: the number described how sure the *matcher* was, and once a person has
chosen the show there is nothing left for it to describe. The candidate list
is kept — it is what was on screen when the choice was made.

**What ignoring does.** Takes the file out of the queue — and, if Arc
downloaded it, tells the *episode* so: the release Arc chose was not this
episode after all, so the episode leaves ``matching`` for ``unavailable`` and
the daily retry looks again (:mod:`arc.services.acquisition.reject`). A file
somebody dropped in by hand carries no such claim and changes nothing.

A file that is already linked is a 409, not a silent relink: the client should
reopen it first, so that "I changed my mind" is a deliberate two-step rather
than a double-click. Since every linked state (``auto``, ``confirmed``) carries
an ``episode_id`` and no unlinked one does, that check is also, exactly, "only
a ``pending`` or ``ignored`` file may be confirmed" — which is the rule as a
person would state it. Confirming an ``ignored`` file is deliberate and
allowed: "not anime after all — no, wait, it is *this*" needs no detour
through ``reopen``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.api.anime_schemas import AnimeSummary
from arc.api.anime_schemas import SearchPage as AnimeSearchPage
from arc.api.deps import CatalogDep, CurrentUser, SessionDep, SettingsDep
from arc.api.review_schemas import (
    CandidateOut,
    ConfirmRequest,
    ReviewItem,
    ReviewPage,
    ReviewSummary,
)
from arc.config import Settings
from arc.models import Anime, MediaFile, ReviewState
from arc.services.acquisition.reject import reject_download
from arc.services.catalog import (
    CATALOGUE_UNAVAILABLE,
    SourceUnavailable,
    list_status_for,
    upsert_summaries,
)
from arc.services.library.link import LinkError, link

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["review"])

ITEM_NOT_FOUND = "review item not found"
ALREADY_LINKED = "this file is already linked to an episode"
NOT_IGNORED = "only an ignored file can be reopened"

#: How many rows one page of the queue returns. The queue is meant to be
#: emptied, not paged through; a library that has hundreds pending has a
#: matcher problem, and the cap keeps a runaway one from being a 40 MB
#: response.
PAGE_LIMIT = 200

#: Same bounds as ``/api/anime/search``, so the review UI's search box and the
#: main one behave identically.
MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 100


async def _pending_count(session: AsyncSession) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(MediaFile)
        .where(MediaFile.review_state == ReviewState.PENDING)
    )
    return int(total or 0)


async def _shows_for(session: AsyncSession, rows: list[MediaFile]) -> dict[int, Anime]:
    """Every anime named by every candidate of every row, in one query.

    Resolving them per candidate would be a round trip each, and a queue of
    fifty files with five candidates apiece is two hundred and fifty of them.
    """
    wanted: set[int] = set()
    for row in rows:
        wanted |= CandidateOut.anime_ids(row.match_candidates)
    if not wanted:
        return {}
    found = await session.scalars(select(Anime).where(Anime.id.in_(wanted)))
    return {anime.id: anime for anime in found.all()}


async def _item(session: AsyncSession, media_file: MediaFile, settings: Settings) -> ReviewItem:
    shows = await _shows_for(session, [media_file])
    return ReviewItem.build(media_file, data_dir=settings.data_dir, shows=shows)


async def _load(session: AsyncSession, media_file_id: int) -> MediaFile:
    media_file = await session.get(MediaFile, media_file_id)
    if media_file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ITEM_NOT_FOUND)
    return media_file


@router.get(
    "",
    response_model=ReviewPage,
    summary="The match-review queue (FR-L4, FR-L6)",
)
async def list_queue(
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    state: ReviewState = ReviewState.PENDING,
    limit: Annotated[int, Query(ge=1, le=PAGE_LIMIT)] = PAGE_LIMIT,
) -> ReviewPage:
    """Files in ``state``, newest first, plus the always-pending count."""
    rows = list(
        (
            await session.scalars(
                select(MediaFile)
                .where(MediaFile.review_state == state)
                .order_by(MediaFile.created_at.desc(), MediaFile.id.desc())
                .limit(limit)
            )
        ).all()
    )
    shows = await _shows_for(session, rows)
    return ReviewPage(
        items=[ReviewItem.build(row, data_dir=settings.data_dir, shows=shows) for row in rows],
        pending=await _pending_count(session),
    )


@router.get(
    "/summary",
    response_model=ReviewSummary,
    summary="How many files are waiting for review",
)
async def summary(user: CurrentUser, session: SessionDep) -> ReviewSummary:
    """One ``COUNT``. The client sidebar polls this, so it stays this small."""
    return ReviewSummary(pending=await _pending_count(session))


@router.get(
    "/{media_file_id}/search",
    response_model=AnimeSearchPage,
    summary="Search the catalogue for a title to confirm against (FR-L6)",
    responses={
        404: {"description": ITEM_NOT_FOUND},
        502: {"description": CATALOGUE_UNAVAILABLE},
    },
)
async def search_for_item(
    media_file_id: int,
    user: CurrentUser,
    session: SessionDep,
    catalog: CatalogDep,
    q: Annotated[str, Query(min_length=MIN_QUERY_LENGTH, max_length=MAX_QUERY_LENGTH)],
) -> AnimeSearchPage:
    """The same live search ``/api/anime/search`` does, hung off an item.

    Under the item's id rather than free-standing so the client does not have
    to hold two ideas at once, and so that a future audit line ("who searched
    what while resolving this file") has somewhere to go. The results are
    upserted on the way past exactly as they are in the main search, which is
    what gives each one the internal id ``confirm`` then takes.
    """
    await _load(session, media_file_id)
    try:
        found = await catalog.search(q)
    except SourceUnavailable as exc:
        log.warning("review search failed", extra={"query": q, "error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=CATALOGUE_UNAVAILABLE
        ) from exc

    rows = await upsert_summaries(session, found.results)
    await session.commit()
    statuses = await list_status_for(session, user_id=user.id, anime_ids=[row.id for row in rows])
    return AnimeSearchPage(
        results=[AnimeSummary.from_anime(row, statuses.get(row.id)) for row in rows],
        page=found.page,
        has_next=found.has_next,
    )


@router.post(
    "/{media_file_id}/confirm",
    response_model=ReviewItem,
    summary="Link a file to an episode by hand (FR-L6)",
    responses={404: {"description": ITEM_NOT_FOUND}, 409: {"description": ALREADY_LINKED}},
)
async def confirm(
    media_file_id: int,
    body: ConfirmRequest,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ReviewItem:
    """Set the show and the episode number, and move the episode to ``matched``.

    Only from a file that is not linked — see the module docstring for why
    that is the same rule as "only ``pending`` or ``ignored``".
    """
    media_file = await _load(session, media_file_id)
    if media_file.episode_id is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ALREADY_LINKED)

    try:
        await link(
            session,
            media_file,
            anime_id=body.anime_id,
            episode_number=body.episode_number,
            review_state=ReviewState.CONFIRMED,
        )
    except LinkError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # A person decided, so there is no longer a machine's confidence to report.
    # The candidates stay: they are the evidence that was on screen.
    media_file.match_confidence = None
    await session.commit()
    log.info(
        "review item confirmed",
        extra={
            "media_file_id": media_file.id,
            "user_id": user.id,
            "anime_id": body.anime_id,
            "episode_number": body.episode_number,
        },
    )
    return await _item(session, media_file, settings)


@router.post(
    "/{media_file_id}/ignore",
    response_model=ReviewItem,
    summary='Mark a file "not anime / ignore" (FR-L6)',
    responses={404: {"description": ITEM_NOT_FOUND}},
)
async def ignore(
    media_file_id: int,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ReviewItem:
    """Take the file out of the queue without linking it.

    The row stays — the file is still on disk, and re-creating it on the next
    scan only to ignore it again would be a queue that never empties.

    If the file is one **Arc downloaded**, ignoring it also says something
    about the *episode*: the release Arc picked was not it, and the episode has
    been sitting in ``matching`` waiting for this answer. It goes to
    ``unavailable``, which is both the sentence the show page shows and the
    state the daily retry looks for — see
    :mod:`arc.services.acquisition.reject`.
    """
    media_file = await _load(session, media_file_id)
    media_file.review_state = ReviewState.IGNORED
    episode = await reject_download(session, media_file, downloads_dir=settings.downloads_dir)
    await session.commit()
    log.info(
        "review item ignored",
        extra={
            "media_file_id": media_file.id,
            "user_id": user.id,
            "episode_unavailable": episode.id if episode is not None else None,
        },
    )
    return await _item(session, media_file, settings)


@router.post(
    "/{media_file_id}/reopen",
    response_model=ReviewItem,
    summary="Put an ignored file back in the queue (FR-L6)",
    responses={404: {"description": ITEM_NOT_FOUND}, 409: {"description": NOT_IGNORED}},
)
async def reopen(
    media_file_id: int,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ReviewItem:
    """Only from ``ignored``, and deliberately so.

    Reopening a *linked* file would have to decide what happens to the episode
    it is linked to — which may by then be transcoded, half-watched, and the
    reason somebody's MAL progress moved. That is an unlink operation with
    real consequences, and it belongs with the admin delete/re-fetch tools
    (FR-T4), not behind the same button as "I ignored this by mistake".
    """
    media_file = await _load(session, media_file_id)
    if media_file.review_state is not ReviewState.IGNORED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_IGNORED)
    media_file.review_state = ReviewState.PENDING
    await session.commit()
    log.info("review item reopened", extra={"media_file_id": media_file.id, "user_id": user.id})
    return await _item(session, media_file, settings)


__all__ = [
    "ALREADY_LINKED",
    "ITEM_NOT_FOUND",
    "NOT_IGNORED",
    "PAGE_LIMIT",
    "router",
]
