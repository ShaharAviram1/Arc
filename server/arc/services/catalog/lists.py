"""List states: what happens when a user says "I'm watching this" (FR-W2).

Three functions, all pure business rules over a session; the routers in
:mod:`arc.api.list` do nothing but translate their exceptions into status
codes.

The rule that matters most is at the bottom of :func:`set_list_entry`: a
change made *in Arc* sets ``updated_by = arc`` and ``mal_dirty = true``,
records one queued ``mal_write_log`` row per field it actually changed, and
talks to MyAnimeList not at all. Those rows *are* the push queue
(:mod:`arc.services.mal.writelog`): they carry the cause — ``manual``, an
explicit edit — that the FR-M4 guards are decided from, and they are written
in this transaction so that a list change and the write it owes are one thing
or neither. The ``mal_push`` job then sends them, which is what keeps the "no
write without a user-originated event" guarantee (FR-M7) to one enforcement
point instead of one per endpoint. This module is one of the three places
allowed to queue such a write (:mod:`arc.services.mal.names` names the other
two), and it does so for the same reason it recomputes the acquisition window:
an explicit list edit is a user-originated event, which is precisely what
FR-M4 permits Arc to write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, ListEntry, ListStatus, MalWriteCause, UpdatedBy
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.catalog.cache import ensure_anime
from arc.services.catalog.service import CatalogService
from arc.services.mal.names import enqueue_mal_push, is_linked
from arc.services.mal.sync import record_removal
from arc.services.mal.writelog import (
    FIELD_PROGRESS,
    FIELD_SCORE,
    FIELD_STATUS,
    record_pending,
)

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

    # What the entry held before this request, so that the queued write log
    # rows below describe the change the user actually made. A create has no
    # "before" on MyAnimeList's side either, which is what ``None``/``0`` say.
    before = _snapshot(entry)

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
    # The acquisition window is a function of this row (FR-A1, FR-W4), so
    # every change to it queues a recompute — deduplicated, so a burst of list
    # edits costs one job. Enqueued rather than computed inline: reconciling
    # every user's window is not something a request should wait on, and the
    # fifteen-minute sweep would get there anyway. In the same transaction, so
    # a list change that is rolled back does not leave work behind.
    await enqueue_compute_wants(session)
    # An explicit list edit is one of the three events FR-M7 allows a MAL write
    # for. Recorded here as one queued row per changed field and *sent* by the
    # job: the push has to read MyAnimeList's current values first (FR-M5),
    # which is not something a request handler should wait on. A no-op for a
    # user with no MAL link — their edits still set ``mal_dirty``, and the
    # import decides who wins if they link later (FR-M2, FR-M3).
    if await is_linked(session, user_id):
        await _queue_changes(
            session, user_id=user_id, anime_id=anime_id, before=before, after=entry
        )
        await enqueue_mal_push(session, user_id=user_id, anime_id=anime_id)
    return entry, anime


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """The three MAL-visible fields of a list entry, before a change."""

    status: str | None
    progress: int
    score: int | None


def _snapshot(entry: ListEntry | None) -> _Snapshot:
    """What the entry held, or what "not on the list" looks like."""
    if entry is None:
        return _Snapshot(status=None, progress=0, score=None)
    return _Snapshot(status=entry.status.value, progress=entry.progress, score=entry.score)


async def _queue_changes(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int,
    before: _Snapshot,
    after: ListEntry,
) -> None:
    """One queued write log row per field this edit actually moved (FR-M5).

    *Actually* moved: a PUT that re-sends the status a show already has is a
    request, not a change, and queueing a write for it would put a row in the
    user's log for something they did not do. The comparison is against the
    entry's own previous values rather than against MyAnimeList's, because
    this runs in a request and MyAnimeList is a network away; the push reads
    the remote values and writes them onto the row it sends.
    """
    now = _Snapshot(status=after.status.value, progress=after.progress, score=after.score)
    for field, old, new in (
        (FIELD_STATUS, before.status, now.status),
        (FIELD_SCORE, before.score, now.score),
        (FIELD_PROGRESS, before.progress, now.progress),
    ):
        if old == new:
            continue
        await record_pending(
            session,
            user_id=user_id,
            anime_id=anime_id,
            field=field,
            old_value=old,
            new_value=new,
            cause=MalWriteCause.MANUAL,
        )


async def remove_list_entry(session: AsyncSession, *, user_id: int, anime_id: int) -> bool:
    """Delete the entry; ``False`` if there was nothing to delete.

    The cached ``anime`` row stays: it costs nothing, other users may be
    watching the show, and re-adding it should not mean another round trip to
    a catalogue source.
    """
    entry = await session.get(ListEntry, (user_id, anime_id))
    if entry is None:
        return False

    # The write log row is written *now*, before the entry is deleted, because
    # the entry is the only thing that knows what it held — by the time the
    # push job runs there is nothing left to read an ``old_value`` off (FR-M5).
    # That pending row is therefore the record of the removal, and the job only
    # closes it.
    linked = await is_linked(session, user_id)
    if linked:
        await record_removal(session, user_id=user_id, anime_id=anime_id, status=entry.status)

    await session.delete(entry)
    await session.flush()
    # A show that left the list wants nothing (FR-W4). Same reasoning as
    # :func:`set_list_entry`: queue it, do not compute it here.
    await enqueue_compute_wants(session)
    if linked:
        await enqueue_mal_push(session, user_id=user_id, anime_id=anime_id, delete=True)
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
