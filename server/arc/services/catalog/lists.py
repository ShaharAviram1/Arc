"""List states: what happens when a user says "I'm watching this" (FR-W2).

Three functions, all pure business rules over a session; the routers in
:mod:`arc.api.list` do nothing but translate their exceptions into status
codes.

The rule that matters most is at the bottom of :func:`set_list_entry`: a
change made *in Arc* sets ``updated_by = arc`` and ``mal_dirty = true``, and
nothing here talks to MyAnimeList. M9's ``mal_push`` is what reads the dirty
flag and writes, so that the "no write without a user-originated event"
guarantee (FR-M7) has exactly one enforcement point instead of one per
endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, ListEntry, ListStatus, UpdatedBy
from arc.services.catalog.cache import ensure_anime
from arc.services.catalog.service import CatalogService

#: A score outside this range is not a list state, it is a typo.
MIN_SCORE = 1
MAX_SCORE = 10


class ListEntryError(ValueError):
    """The requested list change does not make sense."""


class StatusRequired(ListEntryError):
    """A new entry was asked for without saying what state it is in.

    An update may omit ``status`` — "just set my score" is a real request —
    but a create cannot: there is no sensible default, and guessing
    ``watching`` would put shows into acquisition that nobody asked for
    (FR-A1).
    """


async def set_list_entry(
    session: AsyncSession,
    catalog: CatalogService,
    *,
    user_id: int,
    anime_id: int,
    status: ListStatus | None = None,
    progress: int | None = None,
    score: int | None = None,
    score_given: bool = False,
) -> tuple[ListEntry, Anime]:
    """Create or update (user, anime) → status/progress/score (FR-C2, FR-W2).

    ``anime_id`` is Arc's internal id, so the show must already have a row —
    which it does, because the only ways to reach this endpoint are a search
    result and a show page, and both upsert on the way past. Refreshing it
    first is still worth a call: a list entry is where acquisition starts, and
    starting it from an episode count somebody cached a week ago is how a show
    ends up permanently "behind by 0". :class:`SourceNotFound` propagates for
    an id Arc has never heard of.

    ``score_given`` distinguishes "clear my score" (``score=None`` and
    ``score_given=True``) from "leave my score alone" (the field was omitted);
    ``None`` is a meaningful value here, so a sentinel is unavoidable.
    """
    # Validate before fetching. Every check below is answerable from the
    # request and the local row, so a request that cannot succeed must not
    # first cost a round trip to a catalogue source.
    if score_given and score is not None and not MIN_SCORE <= score <= MAX_SCORE:
        raise ListEntryError(f"score must be between {MIN_SCORE} and {MAX_SCORE}")
    if progress is not None and progress < 0:
        raise ListEntryError("progress must not be negative")

    entry = await session.get(ListEntry, (user_id, anime_id))
    if entry is None and status is None:
        raise StatusRequired("status is required when adding a show to your list")

    anime = await ensure_anime(session, catalog, anime_id=anime_id)

    if entry is None:
        # ``status`` is not None here: a create without one raised above.
        entry = ListEntry(user_id=user_id, anime_id=anime_id, status=status, progress=0)
        session.add(entry)

    if status is not None:
        entry.status = status
    if progress is not None:
        entry.progress = progress
    if score_given:
        entry.score = score

    # "Completed" means all of it, so the progress bar and MAL agree with the
    # badge. Only when the catalogue knows the count, and never downwards: a
    # user who set progress past a stale episode count keeps their number.
    if entry.status is ListStatus.COMPLETED and anime.episodes:
        entry.progress = max(entry.progress, int(anime.episodes))

    entry.updated_by = UpdatedBy.ARC
    entry.mal_dirty = True
    # Set explicitly rather than left to ``onupdate``: a create has no update
    # to fire it, and an entry whose only change was ``mal_dirty`` must still
    # move, because §5.5 step 4 resolves MAL conflicts by comparing this
    # timestamp against MAL's.
    entry.updated_at = datetime.now(UTC)

    await session.flush()
    return entry, anime


async def remove_list_entry(session: AsyncSession, *, user_id: int, anime_id: int) -> bool:
    """Delete the entry; ``False`` if there was nothing to delete.

    The cached ``anime`` row stays: it costs nothing, other users may be
    watching the show, and re-adding it should not mean another round trip to
    a catalogue source.
    """
    entry = await session.get(ListEntry, (user_id, anime_id))
    if entry is None:
        return False
    await session.delete(entry)
    await session.flush()
    return True


async def get_my_list(
    session: AsyncSession,
    *,
    user_id: int,
    status: ListStatus | None = None,
) -> list[tuple[Anime, ListEntry]]:
    """One user's list, most recently changed first."""
    statement = (
        select(Anime, ListEntry)
        .join(ListEntry, ListEntry.anime_id == Anime.id)
        .where(ListEntry.user_id == user_id)
        .order_by(ListEntry.updated_at.desc(), Anime.id)
    )
    if status is not None:
        statement = statement.where(ListEntry.status == status)
    rows = await session.execute(statement)
    return [(anime, entry) for anime, entry in rows.all()]


async def list_status_for(
    session: AsyncSession, *, user_id: int, anime_ids: list[int]
) -> dict[int, ListStatus]:
    """``anime_id → status`` for the ids this user has on their list.

    One query for a whole page of search results, so that rendering twenty
    cards with their list badges is not twenty round trips.
    """
    if not anime_ids:
        return {}
    rows = await session.execute(
        select(ListEntry.anime_id, ListEntry.status).where(
            ListEntry.user_id == user_id, ListEntry.anime_id.in_(anime_ids)
        )
    )
    return {anime_id: status for anime_id, status in rows.all()}


__all__ = [
    "MAX_SCORE",
    "MIN_SCORE",
    "ListEntryError",
    "StatusRequired",
    "get_my_list",
    "list_status_for",
    "remove_list_entry",
    "set_list_entry",
]
