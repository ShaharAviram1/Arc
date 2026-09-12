"""Writing a source's answers into ``anime`` and ``episodes``.

The local catalogue is a cache, not a copy: :func:`ensure_anime` is the only
thing that decides whether a source needs asking at all, and everything else in
Arc reads the tables. Five rules shape the code here.

1. **One show is one row.** ``anime.id`` is internal; a payload is matched to a
   row by ``anilist_id`` first and ``mal_id`` second. An AniList payload
   carrying ``idMal`` therefore lands on the row a MAL search created rather
   than beside it, and gains the AniList id on the way (FR-C6).
2. **A search result must not overwrite a detail fetch.** The search query
   carries the summary fields only, so an upsert from one writes only those
   columns and leaves ``refreshed_at`` alone — otherwise a search for
   "Frieren" would blank its relations and then claim the row was fresh.
3. **Weaker data never overwrites stronger data.** A source is as strong as its
   position in :data:`~arc.services.catalog.source.SOURCE_NAMES` — the order
   :class:`CatalogService` tries them, AniList before MAL. A weaker source
   fills columns that are null and touches nothing else; a stronger one
   overwrites; a source always gets to correct its own earlier answer. That
   applies to the *summary* columns as much as the detail ones, judged against
   ``summary_source`` and ``detail_source`` respectively: MAL's 230 px cover
   replacing AniList's key art is the same mistake as MAL's synopsis replacing
   AniList's, and it is the more visible one. The same rule one level down: an
   estimated air time never replaces a published one, and a published one
   always replaces an estimate and clears the flag; an episode title or still
   is only ever written where there is none at all.
4. **Episodes are Arc's, not a source's.** ``episodes`` rows carry the local
   state machine (spec §6) and, from M5 on, links to files. So a sync creates
   and back-fills rows, and never deletes one or touches its ``state``.
5. **Stale beats absent.** If every source is unreachable and there is a cached
   row, callers get the cached row with a warning in the log rather than a 502.

``refreshed_at`` records the last successful detail fill from *any* source;
``detail_source`` records which one. That split is what lets
:func:`ensure_anime` treat a MAL-filled row as stale the moment AniList is
healthy again, so an outage's worth of estimates is upgraded promptly rather
than a day later.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, Episode
from arc.services.catalog.airing import next_airing_estimated
from arc.services.catalog.offline.ids import fill_missing_ids
from arc.services.catalog.service import CatalogService
from arc.services.catalog.source import (
    SOURCE_STRENGTH,
    AiringEntry,
    CatalogMedia,
    EpisodeArt,
    SourceName,
    SourceNotFound,
    SourceUnavailable,
)

log = logging.getLogger(__name__)

#: How old a cached row may be before a detail read refreshes it (FR-C5).
DEFAULT_MAX_AGE = timedelta(hours=24)

#: The gap assumed between two episodes when a date has to be worked out rather
#: than read (:func:`_backfill_before_the_schedule`). Almost every TV anime is
#: weekly, and the estimate is badged as one.
WEEKLY = timedelta(days=7)

#: Columns a *search* result is allowed to write. Everything outside this set
#: is only ever written by a detail fetch.
#:
#: Documentation of the split rather than the thing that enforces it: the two
#: ``_*_values`` functions below are what the writes iterate, and since the
#: racing insert stopped writing from ``excluded`` nothing reads this tuple.
#: It stays because "which half of the row is this column in" is a question
#: asked of this module often enough to be worth an answer in one place.
SUMMARY_COLUMNS = (
    "title_romaji",
    "title_english",
    "title_native",
    "format",
    "episodes",
    "status",
    "season",
    "season_year",
    "cover_url",
    "cover_large_url",
    "popularity",
    "average_score",
    "summary_source",
)

#: Columns only a detail fetch knows about. ``refreshed_at`` and
#: ``detail_source`` are written alongside them but are not in the list: they
#: are bookkeeping about the fill, not part of it, and the MAL "fill the nulls"
#: rule must not apply to them.
DETAIL_COLUMNS = (
    "synonyms",
    "description",
    "banner_url",
    "genres",
    "tags",
    "studio",
    "credits",
    "relations",
    "next_airing",
)


def _rank(source: str | None) -> int:
    """How much a source's word is worth: lower is stronger.

    :data:`SOURCE_STRENGTH` is the order "who is believed": AniList first
    because it is the better answer, MAL as the live fallback, and the offline
    import (M15.5) last — it knows one title, a cover and an episode count, and
    nothing at all about a synopsis. Reading the rank off that tuple rather
    than off two ``== "mal"`` comparisons means a fourth source would slot in
    without a branch here.

    A source name nothing recognises — including ``None``, which is a row no
    source has filled in yet — ranks below every real one, so anything may
    write over it.
    """
    if source is None:
        return len(SOURCE_STRENGTH)
    try:
        return SOURCE_STRENGTH.index(source)
    except ValueError:
        return len(SOURCE_STRENGTH)


def _outranked(incoming: SourceName, wrote_it: str | None) -> bool:
    """Whether the source that wrote these columns beats the one offering new ones.

    When it does, the incoming payload may fill nulls and nothing else (rule
    3). MAL's cover is 230 px where AniList's is 1900, its synopsis is a
    different translation, and it has no banner or tags at all — so letting a
    five-minute outage rewrite a good row would cost a day of worse artwork on
    every page that shows it, which on the redesigned Show and Watch Now pages
    is the whole point of the row.

    Equal ranks are *not* outranked: a source always gets to correct its own
    earlier answer, so a MAL row refreshed from MAL updates in full.
    """
    return _rank(incoming) > _rank(wrote_it)


def preferred_title(anime: Anime) -> str:
    """English if the source had one, else romaji (FR-C1).

    The same rule as :attr:`MediaTitle.preferred`, applied to a cached row so
    that nothing has to reconstruct a media object to render a card.
    """
    return anime.title_english or anime.title_romaji or anime.title_native or f"anime {anime.id}"


def _relation_rows(media: CatalogMedia) -> list[dict[str, Any]]:
    """``anime.relations`` as stored: JSON-ready, with both external ids.

    Neither id is guaranteed — AniList knows its own and sometimes MAL's, MAL
    knows only its own — so both are stored and the API resolves whichever it
    can against the local rows.
    """
    return [
        {
            "anilist_id": relation.anilist_id,
            "mal_id": relation.mal_id,
            "relation_type": relation.relation_type,
            "format": relation.format,
            "title": {
                "romaji": relation.title.romaji,
                "english": relation.title.english,
                "native": relation.title.native,
                "preferred": relation.title.preferred,
            },
        }
        for relation in media.relations
    ]


def _summary_values(media: CatalogMedia) -> dict[str, Any]:
    return {
        "title_romaji": media.title.romaji,
        "title_english": media.title.english,
        "title_native": media.title.native,
        "format": media.format,
        "episodes": media.episodes,
        "status": media.status,
        "season": media.season,
        "season_year": media.season_year,
        "cover_url": media.cover_url,
        "cover_large_url": media.cover_large_url,
        "popularity": media.popularity,
        "average_score": media.average_score,
        "summary_source": media.source,
    }


def _detail_values(media: CatalogMedia) -> dict[str, Any]:
    return {
        "synonyms": media.synonyms,
        "description": media.description,
        "banner_url": media.banner_url,
        "genres": media.genres,
        "tags": media.tags,
        "studio": media.studio,
        "credits": media.credits,
        "relations": _relation_rows(media),
        "next_airing": media.next_airing,
    }


def _key(media: CatalogMedia) -> tuple[int | None, int | None]:
    return (media.anilist_id, media.mal_id)


async def _find_rows(
    session: AsyncSession, media: list[CatalogMedia]
) -> tuple[dict[int, Anime], dict[int, Anime]]:
    """Existing rows for these payloads, indexed by each external id.

    One query for a whole search page rather than one per result: search is on
    the keystroke path, and twenty round trips to Postgres is twenty times the
    latency for no extra safety.
    """
    anilist_ids = {item.anilist_id for item in media if item.anilist_id is not None}
    mal_ids = {item.mal_id for item in media if item.mal_id is not None}
    clauses = []
    if anilist_ids:
        clauses.append(Anime.anilist_id.in_(anilist_ids))
    if mal_ids:
        clauses.append(Anime.mal_id.in_(mal_ids))
    if not clauses:
        return {}, {}

    # ``populate_existing`` because ``sync_episodes`` and the ON CONFLICT
    # inserts below write with Core statements the session knows nothing about.
    rows = await session.scalars(
        select(Anime).where(or_(*clauses)).execution_options(populate_existing=True)
    )
    by_anilist: dict[int, Anime] = {}
    by_mal: dict[int, Anime] = {}
    for row in rows.all():
        if row.anilist_id is not None:
            by_anilist[row.anilist_id] = row
        if row.mal_id is not None:
            by_mal[row.mal_id] = row
    return by_anilist, by_mal


def _match(
    media: CatalogMedia, by_anilist: dict[int, Anime], by_mal: dict[int, Anime]
) -> Anime | None:
    """The row this payload belongs to: AniList id first, then MAL id."""
    if media.anilist_id is not None:
        found = by_anilist.get(media.anilist_id)
        if found is not None:
            return found
    if media.mal_id is not None:
        return by_mal.get(media.mal_id)
    return None


def _attach_ids(
    row: Anime, media: CatalogMedia, by_anilist: dict[int, Anime], by_mal: dict[int, Anime]
) -> None:
    """Fill in an external id the row did not have.

    Only ever fills a null. Changing an id that is already set would silently
    re-point every list entry and episode of one show at another, and a
    disagreement between the two sources is a thing to log, not to act on.
    The other row holding that id is checked first: writing it anyway would
    violate the unique index and fail the whole request.
    """
    for column, incoming, index in (
        ("anilist_id", media.anilist_id, by_anilist),
        ("mal_id", media.mal_id, by_mal),
    ):
        if incoming is None or getattr(row, column) is not None:
            continue
        holder = index.get(incoming)
        if holder is not None and holder is not row:
            log.warning(
                "catalogue id already belongs to another row; not attaching",
                extra={"column": column, "value": incoming, "anime_id": row.id},
            )
            continue
        setattr(row, column, incoming)
        index[incoming] = row


def _may_write_next_airing(row: Anime, media: CatalogMedia, *, summary_source: str | None) -> bool:
    """Whether ``media`` outranks the slot ``row`` already holds (rule 3).

    ``next_airing`` needs its own precedence check because it is the one column
    a *summary* writes as well as a detail fetch, so ``detail_source`` does not
    describe who put it there. A season sweep leaves a row with
    ``summary_source = "anilist"``, ``detail_source = NULL`` and AniList's
    published ``nextAiringEpisode``; the MAL sweep of the same season during an
    outage would then find no ``detail_source`` to be stopped by and overwrite
    a published time with an estimate — the show would move to whatever weekday
    MAL's broadcast slot implies, and stay there until somebody opened it.

    So the rule is read off both source columns and off the blob itself:
    AniList always wins, and MAL may write only where AniList has left nothing
    — no slot at all, or a slot MAL itself estimated. ``summary_source`` is
    passed in rather than read here because :func:`_apply` has already
    overwritten it by the time this matters.

    Deliberately not expressed through :func:`_outranked`, which asks about one
    source column at a time: this is the stricter rule, because the column is
    written from both halves of a payload and a published slot must survive a
    weaker write whichever half last touched the row.
    """
    if media.source != "mal":
        return True
    if row.next_airing is None:
        return True
    if "anilist" in (row.detail_source, summary_source):
        return False
    return next_airing_estimated(row.next_airing)


def _apply(row: Anime, media: CatalogMedia, *, now: datetime) -> None:
    """Write ``media`` onto ``row`` under the precedence rules (rules 2 and 3).

    The two halves of the row are governed separately, because two different
    columns record who wrote them: ``summary_source`` for the card fields and
    ``detail_source`` for the rest. A search page and a detail fetch can come
    from different sources — that is the ordinary state of a row during an
    outage — and one shared decision would let a MAL search rewrite an
    AniList synopsis, or stop a MAL detail fetch from filling a row whose only
    previous contact was an AniList search.
    """
    # A fallback source completes a row the primary wrote; it never rewrites
    # it. Both are decided before the loops below, which overwrite the very
    # columns the decisions are read from.
    summary_fill_only = _outranked(media.source, row.summary_source)
    detail_fill_only = _outranked(media.source, row.detail_source)
    may_write_next_airing = _may_write_next_airing(row, media, summary_source=row.summary_source)

    for name, value in _summary_values(media).items():
        # Within one source the summary columns are overwritten rather than
        # filled, because a renamed show or a corrected episode count has to
        # land. From a weaker one they are filled only where there is nothing:
        # MAL's 230 px cover must not replace the key art the redesigned cards
        # render, and MAL leaving ``num_episodes`` at 0 must not blank the 28
        # AniList published — a blanked count is 28 missing episode rows on
        # the next sync.
        #
        # ``summary_source`` is in this loop and is therefore skipped along
        # with the rest, which is what keeps the rule stable: a fill-only pass
        # does not make MAL the author of columns AniList still owns, so the
        # *next* MAL refresh is held to the same rule.
        if summary_fill_only and getattr(row, name) is not None:
            continue
        setattr(row, name, value)

    # ``next_airing`` is a detail column that a *season* payload also carries,
    # because the schedule is built from season rows and a show with no next
    # broadcast has no weekday (FR-C3, FR-C7). It is therefore written from a
    # summary too — but only when the payload actually has one, since a search
    # result does not ask AniList for the field and writing its null would
    # blank the schedule every time somebody searched.
    if media.next_airing is not None and may_write_next_airing:
        row.next_airing = media.next_airing

    if not media.full:
        return

    for name, value in _detail_values(media).items():
        if detail_fill_only and getattr(row, name) is not None:
            continue
        # A *full* fetch is also allowed to clear the slot — a show that has
        # finished airing has no next episode, and leaving the last one there
        # would keep it on the schedule for ever — but only from the source
        # that outranks whatever wrote it.
        if name == "next_airing" and not may_write_next_airing:
            continue
        setattr(row, name, value)
    # ``refreshed_at`` records the last successful detail fill from *any*
    # source, fill-only ones included: the row was asked about and answered,
    # which is what "fresh" means here. ``detail_source`` is the other half of
    # that bookkeeping and records who the columns actually belong to, so a
    # fill-only pass leaves it alone — otherwise ``ensure_anime`` would stop
    # treating the row as one AniList could improve on.
    row.refreshed_at = now
    if not detail_fill_only:
        row.detail_source = media.source


async def _insert_new(
    session: AsyncSession, media: list[CatalogMedia], *, now: datetime
) -> dict[int, Anime]:
    """Insert rows for payloads that matched nothing; return them by list index.

    ``INSERT … ON CONFLICT DO UPDATE`` rather than a plain insert: the same
    title can arrive from a search in one request and a refresh job in another,
    and without it the loser of that race gets an integrity error instead of a
    row.

    The conflict clause writes **nothing**: it assigns the arbiter column to
    the value it already has, which is a no-op that still makes the statement a
    ``DO UPDATE`` and therefore still ``RETURNING`` the row (``DO NOTHING``
    returns no row for a conflict, and the caller needs one). On the ordinary
    path there is no conflict and the insert carries everything; on the racing
    path the row that already exists may hold better data than this payload
    does, and the caller re-applies :func:`_apply` afterwards, which knows the
    precedence rules.

    It used to write the summary columns from ``excluded``, on the argument
    that the summary half was overwritten by the next ``_apply`` anyway. That
    stopped being true when the fallback source was barred from overwriting the
    primary's columns (rule 3): a MAL search racing an AniList row would have
    clobbered its key art here, and ``_apply`` would then have found MAL's
    values in place and left them, having no way to tell them from AniList's.
    Expressing the rules a second time in SQL is the alternative, and one rule
    written twice is how two paths come to disagree.

    Rows are grouped by conflict target (whichever unique id the payload
    carries) and by whether they are a detail fetch, because every row of one
    multi-row insert must name the same columns. In practice that is one group:
    a page of results all comes from one source and is all one kind.
    """
    groups: dict[tuple[str, bool], list[tuple[int, CatalogMedia]]] = {}
    for index, item in enumerate(media):
        column = "anilist_id" if item.anilist_id is not None else "mal_id"
        groups.setdefault((column, item.full), []).append((index, item))

    inserted: dict[int, Anime] = {}
    for (column, full), entries in groups.items():
        rows: list[dict[str, Any]] = []
        for _, item in entries:
            values: dict[str, Any] = {
                "anilist_id": item.anilist_id,
                "mal_id": item.mal_id,
                **_summary_values(item),
            }
            if full:
                values |= _detail_values(item)
                values["refreshed_at"] = now
                values["detail_source"] = item.source
            rows.append(values)

        insert = pg_insert(Anime).values(rows)
        statement = insert.on_conflict_do_update(
            index_elements=[getattr(Anime, column)],
            # The conflict target, assigned the value it conflicted on: a
            # write that changes nothing, which is the point.
            set_={column: insert.excluded[column]},
        ).returning(Anime)
        result = await session.execute(statement, execution_options={"populate_existing": True})
        by_id = {getattr(row, column): row for row in result.scalars().all()}
        for index, item in entries:
            found = by_id.get(getattr(item, column))
            if found is not None:
                inserted[index] = found
    return inserted


async def _insert_or_retry(
    session: AsyncSession, media: list[CatalogMedia], *, now: datetime
) -> dict[int, Anime]:
    """:func:`_insert_new`, once more through the lookup path if it lost a race.

    ``ON CONFLICT`` takes a single arbiter column, so an insert carrying *both*
    external ids is only protected against a clash on the one it was grouped
    by. A payload with a new AniList id and a known MAL id still violates the
    ``mal_id`` index when a MAL-first row appeared between :func:`_find_rows`
    and this insert — which is exactly what a search and the reconciliation job
    racing on one show produce.

    So the insert runs inside a SAVEPOINT: an :class:`IntegrityError` rolls
    back only the failed statement rather than the caller's whole transaction,
    the rows are looked up again (the competitor has committed by now, and this
    is a fresh statement, so READ COMMITTED sees it), and whatever is found
    takes the ordinary update path. Anything still missing is inserted
    normally, and a second failure is a real conflict rather than a race: it
    propagates.
    """
    try:
        async with session.begin_nested():
            return await _insert_new(session, media, now=now)
    except IntegrityError as exc:
        log.info(
            "catalogue row was created concurrently; re-reading and updating",
            extra={"error": str(exc.orig)},
        )

    by_anilist, by_mal = await _find_rows(session, media)
    resolved: dict[int, Anime] = {}
    missing: list[CatalogMedia] = []
    missing_at: list[int] = []
    for index, item in enumerate(media):
        row = _match(item, by_anilist, by_mal)
        if row is None:
            missing.append(item)
            missing_at.append(index)
            continue
        _attach_ids(row, item, by_anilist, by_mal)
        resolved[index] = row

    if missing:
        for offset, row in (await _insert_new(session, missing, now=now)).items():
            resolved[missing_at[offset]] = row
    return resolved


async def _with_mapped_ids(
    session: AsyncSession, media: Sequence[CatalogMedia]
) -> list[CatalogMedia]:
    """``media`` with each payload's missing external id filled from the map.

    Rule 1 says one show is one row, matched by ``anilist_id`` then ``mal_id``
    — which works as long as *something* tells Arc the two ids belong together.
    AniList's payloads say so themselves (they carry ``idMal``); MAL's never
    do, so before M15.5 a show first seen through MAL got a second row the
    moment AniList came back and answered with an id nothing had attached yet.
    ``catalog_reconcile`` repaired that afterwards, over the network, from the
    source that was down.

    The offline id map answers it up front and for free
    (:func:`~arc.services.catalog.offline.ids.fill_missing_ids`): a MAL-only
    payload arrives here already carrying the AniList id, so the lookup below
    finds the row AniList created and updates it instead of inserting beside
    it. Nothing is overwritten — only a null id is filled — and an id the map
    is not sure about is left null, which is the state Arc already handles.
    """
    pairs = await fill_missing_ids(session, [_key(item) for item in media])
    return [
        item if (anilist, mal) == _key(item) else replace(item, anilist_id=anilist, mal_id=mal)
        for item, (anilist, mal) in zip(media, pairs, strict=True)
    ]


async def _upsert(session: AsyncSession, media: list[CatalogMedia]) -> list[Anime]:
    """Upsert every payload and return the rows in the order they were given.

    Duplicates inside one call collapse onto one row: a search page can list a
    show twice, and Postgres refuses to touch the same row twice in a single
    statement anyway.
    """
    usable: list[CatalogMedia] = []
    seen: set[tuple[int | None, int | None]] = set()
    for item in await _with_mapped_ids(session, media):
        if item.anilist_id is None and item.mal_id is None:
            continue
        if _key(item) in seen:
            continue
        seen.add(_key(item))
        usable.append(item)
    if not usable:
        return []

    now = datetime.now(UTC)
    by_anilist, by_mal = await _find_rows(session, usable)

    resolved: list[Anime | None] = [None] * len(usable)
    pending: list[CatalogMedia] = []
    pending_at: list[int] = []
    for index, item in enumerate(usable):
        row = _match(item, by_anilist, by_mal)
        if row is None:
            pending.append(item)
            pending_at.append(index)
            continue
        _attach_ids(row, item, by_anilist, by_mal)
        _apply(row, item, now=now)
        resolved[index] = row

    if pending:
        # Flush the updates above first: the insert runs inside a SAVEPOINT
        # that may be rolled back, and an autoflush caught inside it would take
        # those writes down with it.
        await session.flush()
        inserted = await _insert_or_retry(session, pending, now=now)
        for offset, row in inserted.items():
            _apply(row, pending[offset], now=now)
            resolved[pending_at[offset]] = row

    await session.flush()
    return [row for row in resolved if row is not None]


async def upsert_summaries(session: AsyncSession, media: list[CatalogMedia]) -> list[Anime]:
    """Upsert a page of *search* or *season* results.

    Every payload is forced to summary shape rather than trusted to be summary
    shaped: a detail record slipping into this list would otherwise widen the
    insert and write ``refreshed_at`` for the whole page, and every one of
    those rows would then claim to be fresh without ever having been fetched.
    """
    return await _upsert(session, [replace(item, full=False) for item in media])


async def upsert_detail(session: AsyncSession, media: CatalogMedia) -> Anime:
    """Upsert one detail fetch and return the row."""
    rows = await _upsert(session, [media])
    if not rows:
        raise SourceNotFound("catalogue payload carried neither an anilist id nor a mal id")
    return rows[0]


def _episode_count(anime: Anime, airing: list[AiringEntry]) -> int:
    """How many episode rows this show should have.

    The source's episode count is authoritative when it knows the number. It is
    null for anything still airing without an announced count, and then the
    schedule is the best evidence there is: the highest number that has aired
    or is scheduled — which includes ``nextAiringEpisode``, so an episode due
    on Sunday already has a row (and a ``want``) before it airs.
    """
    if anime.episodes:
        return int(anime.episodes)
    highest = max((entry.episode for entry in airing), default=0)
    next_airing = anime.next_airing or {}
    upcoming = next_airing.get("episode")
    if isinstance(upcoming, int):
        highest = max(highest, upcoming)
    return highest


async def sync_episodes(
    session: AsyncSession,
    anime: Anime,
    airing: list[AiringEntry],
    *,
    extras: Sequence[EpisodeArt] = (),
) -> int:
    """Create/refresh ``episodes`` 1..N for ``anime``; return how many rows exist.

    Rows are only ever added or given an ``air_at``, a ``title`` or a
    ``still_url``. Nothing is deleted (a row may already own a media file or
    somebody's watch progress) and ``state`` is never written here, so
    re-running this over a show whose episode 3 is ``ready`` leaves it
    ``ready``.

    The air-time rule is the interesting half. A published time (AniList) is
    written over anything; a synthesised one (MAL, FR-C6) is written only where
    there is no time yet or the existing one is itself an estimate. So an
    outage fills the gaps, AniList's return corrects them, and a later MAL
    refresh cannot undo the correction.

    Episodes numbered *below* the published schedule are then estimated from
    it; see :func:`_backfill_before_the_schedule`.

    ``extras`` carries the per-episode titles and stills a detail fetch found
    (M15). They are written by :func:`_fill_episode_art`, which is a separate
    statement rather than more columns on the upsert below precisely because
    the two obey opposite rules: an air time is corrected on every refresh,
    while a title that is already there was either confirmed by hand in the
    match queue or written by an earlier fetch, and is never overwritten.
    """
    count = _episode_count(anime, airing)
    if count <= 0:
        return 0

    schedule = {entry.episode: entry for entry in airing}
    rows = [
        {
            "anime_id": anime.id,
            "number": number,
            "air_at": schedule[number].at if number in schedule else None,
            "air_at_estimated": schedule[number].estimated if number in schedule else False,
        }
        for number in range(1, count + 1)
    ]

    statement = pg_insert(Episode).values(rows)
    excluded = statement.excluded
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Episode.anime_id, Episode.number],
            set_={
                "air_at": excluded.air_at,
                "air_at_estimated": excluded.air_at_estimated,
            },
            # A second sync whose schedule did not reach episode 4 must not
            # blank the air date the first one found, and an estimate must not
            # displace a published time.
            where=excluded.air_at.isnot(None)
            & (
                ~excluded.air_at_estimated
                | Episode.__table__.c.air_at.is_(None)
                | Episode.__table__.c.air_at_estimated
            ),
        )
    )
    await session.flush()
    await _fill_episode_art(session, anime, extras, count=count)
    await _backfill_before_the_schedule(session, anime, airing)
    return count


async def _fill_episode_art(
    session: AsyncSession,
    anime: Anime,
    extras: Sequence[EpisodeArt],
    *,
    count: int,
) -> int:
    """Write episode titles and stills, but only where there are none (M15).

    ``coalesce(episodes.<column>, excluded.<column>)`` rather than a plain
    assignment, in both directions: the existing value wins when there is one,
    so a title somebody confirmed in the match queue survives every future
    refresh, and a null in *this* payload cannot blank a title an earlier one
    found. That is the whole rule, expressed once per column in SQL so the
    whole list is one statement instead of one per episode.

    Entries numbered outside 1..``count`` are dropped: ``streamingEpisodes``
    occasionally lists a recap or a "season 2 episode 1" under a number this
    show does not have, and an ON CONFLICT insert would otherwise *create* that
    episode row — inventing an episode out of a thumbnail.
    """
    rows = [
        {
            "anime_id": anime.id,
            "number": art.number,
            "title": art.title,
            "still_url": art.still_url,
        }
        for art in extras
        if 1 <= art.number <= count and (art.title or art.still_url)
    ]
    if not rows:
        return 0

    statement = pg_insert(Episode).values(rows)
    excluded = statement.excluded
    columns = Episode.__table__.c
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Episode.anime_id, Episode.number],
            set_={
                "title": func.coalesce(columns.title, excluded.title),
                "still_url": func.coalesce(columns.still_url, excluded.still_url),
            },
        )
    )
    await session.flush()
    return len(rows)


async def _backfill_before_the_schedule(
    session: AsyncSession, anime: Anime, airing: list[AiringEntry]
) -> None:
    """Estimate the episodes that fall before AniList's first published one.

    AniList's ``airingSchedule`` starts at the first episode that had a slot of
    its own, which is not always episode 1: Frieren's four-episode premiere
    aired as a single two-hour broadcast and the published schedule begins at
    episode 5. Left alone, episodes 1–4 keep a null ``air_at``, which the
    client and the acquisition window both read as "has not aired yet" — for a
    show that finished two years ago.

    So every episode below the lowest published number is dated by walking
    backwards a week at a time from that episode: number ``k`` gets
    ``first_at - (first_num - k) * 7 days``. **This is an approximation and is
    flagged as one** — ``air_at_estimated`` stays true, and the client badges
    it. Frieren really premiered with four episodes on 2023-09-29, so the dates
    this writes for 1–4 (weekly back from 10-06) are three weeks wrong at the
    far end. A wrong-but-ordered past date is still much better than a null
    that reads as "upcoming", and the badge says not to trust the day.

    Only rows that have no time at all or are already estimates are touched: a
    published time never loses to something worked out here, which is the same
    rule the upsert above follows. Nothing happens when the schedule is empty
    or is itself all estimates — there is nothing firmer to anchor to.
    """
    published = [entry for entry in airing if not entry.estimated]
    if not published:
        return
    first = min(published, key=lambda entry: entry.episode)
    if first.episode <= 1:
        return

    rows = await session.scalars(
        select(Episode)
        .where(
            Episode.anime_id == anime.id,
            Episode.number < first.episode,
            or_(Episode.air_at.is_(None), Episode.air_at_estimated),
        )
        .execution_options(populate_existing=True)
    )
    touched = 0
    for row in rows.all():
        row.air_at = first.at - (first.episode - row.number) * WEEKLY
        row.air_at_estimated = True
        touched += 1
    if touched:
        await session.flush()
        log.info(
            "estimated the episodes before the published schedule",
            extra={"anime_id": anime.id, "first_published": first.episode, "episodes": touched},
        )


async def _row_for(
    session: AsyncSession,
    *,
    anime_id: int | None,
    anilist_id: int | None,
    mal_id: int | None,
) -> Anime | None:
    """The cached row named by whichever id the caller had."""
    if anime_id is not None:
        return await session.get(Anime, anime_id, populate_existing=True)
    if anilist_id is not None:
        row = await session.scalar(select(Anime).where(Anime.anilist_id == anilist_id))
        if row is not None:
            return row
    if mal_id is not None:
        found: Anime | None = await session.scalar(select(Anime).where(Anime.mal_id == mal_id))
        return found
    return None


def _is_stale(row: Anime, *, max_age: timedelta, now: datetime, anilist_healthy: bool) -> bool:
    """Whether ``row`` needs re-fetching before it is served.

    Two reasons, and the second is the whole point of FR-C6: a row whose detail
    came from MAL is stale as soon as AniList is answering again, however
    recently it was filled. Otherwise a five-minute outage would leave a show
    page reading "estimated" for the rest of the day.
    """
    if row.refreshed_at is None or max_age <= timedelta(0):
        return True
    if now - row.refreshed_at >= max_age:
        return True
    return row.detail_source == "mal" and anilist_healthy


async def _fetch(
    catalog: CatalogService, *, anilist_id: int | None, mal_id: int | None
) -> CatalogMedia:
    """One detail record, by whichever id gets an answer.

    Both ids are tried because they fail differently: AniList maps only some
    MAL ids, and MAL cannot answer an AniList id at all. A ``SourceNotFound``
    from the AniList id is only final when there is no MAL id to fall back on —
    within the service it *is* final, and this is the one place that knows the
    two ids belong to the same show.
    """
    failure: SourceUnavailable | None = None
    if anilist_id is not None:
        try:
            media = await catalog.by_anilist_id(anilist_id)
            if media is not None:
                return media
        except SourceNotFound:
            if mal_id is None:
                raise
            log.info(
                "anilist has no title for this id; trying mal",
                extra={"anilist_id": anilist_id},
            )
        except SourceUnavailable as exc:
            failure = exc
    if mal_id is not None:
        try:
            media = await catalog.by_mal_id(mal_id)
            if media is not None:
                return media
        except SourceUnavailable as exc:
            failure = exc
    if failure is not None:
        raise failure
    raise SourceNotFound(f"no catalogue source has anilist_id={anilist_id} mal_id={mal_id}")


async def ensure_anime(
    session: AsyncSession,
    catalog: CatalogService,
    *,
    anime_id: int | None = None,
    anilist_id: int | None = None,
    mal_id: int | None = None,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> Anime:
    """The cached row for one show, fetched from a source if it is not fresh.

    Exactly one of the three ids identifies the row: ``anime_id`` is Arc's own
    and is what the API routes carry, the other two are for the jobs that work
    from a source's ids.

    "Fresh" means present, previously filled in by a detail fetch, refreshed
    within ``max_age``, and not a MAL fill that AniList could now improve on.
    Pass ``max_age=timedelta(0)`` to force a fetch — that is what the
    ``catalog_refresh`` job does.

    Raises :class:`SourceNotFound` when no source has the show and nothing is
    cached, and :class:`SourceUnavailable` when every source is down and
    nothing is cached.
    """
    cached = await _row_for(session, anime_id=anime_id, anilist_id=anilist_id, mal_id=mal_id)
    if cached is not None and not _is_stale(
        cached,
        max_age=max_age,
        now=datetime.now(UTC),
        anilist_healthy=catalog.healthy("anilist"),
    ):
        return cached

    if cached is not None:
        anilist_id, mal_id = cached.anilist_id, cached.mal_id
    if anilist_id is None and mal_id is None:
        raise SourceNotFound(f"no anime with id {anime_id}")

    try:
        media = await _fetch(catalog, anilist_id=anilist_id, mal_id=mal_id)
    except SourceNotFound:
        if cached is not None:
            # The row exists and somebody may have it on their list; a source
            # that has since deleted its entry is not a reason to 404 them.
            log.warning(
                "catalogue no longer has this title; serving cached row",
                extra={"anime_id": cached.id, "anilist_id": anilist_id, "mal_id": mal_id},
            )
            return cached
        raise
    except SourceUnavailable as exc:
        if cached is not None:
            log.warning(
                "catalogue refresh failed; serving cached row",
                extra={"anime_id": cached.id, "error": str(exc)},
            )
            return cached
        raise

    anime = await upsert_detail(session, media)
    await sync_episodes(session, anime, media.airing, extras=media.episode_extras)
    return anime


async def episodes_for(session: AsyncSession, anime_id: int) -> list[Episode]:
    """Every episode row of a show, in number order.

    ``populate_existing`` because :func:`sync_episodes` writes with a Core
    statement the session knows nothing about: without it, an ``Episode``
    already in the identity map would come back with the attributes it had
    before the sync rather than the ones now in the table.
    """
    rows = await session.scalars(
        select(Episode)
        .where(Episode.anime_id == anime_id)
        .order_by(Episode.number)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


__all__ = [
    "DEFAULT_MAX_AGE",
    "DETAIL_COLUMNS",
    "SUMMARY_COLUMNS",
    "ensure_anime",
    "episodes_for",
    "preferred_title",
    "sync_episodes",
    "upsert_detail",
    "upsert_summaries",
]
