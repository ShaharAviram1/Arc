"""The candidate pool a recommendation run chooses from (FR-R2, §5.6).

Everything the model may recommend is in this list and nothing else is; a pick
outside it is dropped by :func:`arc.services.recs.runs.validate_picks`. So the
pool is where the product decision lives, and it is a small one: **forty
titles, from three sources, none of them already on the user's list** — except
``planned``, which is on the list precisely because the user has not decided
when to start it and is exactly the kind of nudge this page exists to give.

The three sources, in order, de-duplicated by ``anime_id``:

1. **This season and the next.** Rows the season pre-cache already wrote
   (FR-C7), so this costs one query and no catalogue call.
2. **Relations of what the user rated highly.** A sequel to a 9 is the single
   most reliable recommendation there is, and it is the one thing a seasonal
   list cannot produce. Relations are stored on the show the user watched, so
   this is a read of ``anime.relations`` plus — for at most ten of them — a
   catalogue fetch for shows Arc has never cached.
3. **Shows sharing at least two of the user's top-three genres**, where "top"
   is weighted by score.

Each source has a share of the forty rather than filling it in order, because
a season alone is bigger than the whole pool and would leave no room for the
other two. Whatever a source does not use is given back at the end, so a user
with no scored history still gets a full pool of seasonal titles.

Everything here is deliberately cheap: three queries, at most ten optional
catalogue fetches, no ranking model, no embedding. The judgement is the
model's; this is the shortlist it judges.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any

from sqlalchemy import Select, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, ListEntry, ListStatus
from arc.services.anilist.client import strip_html
from arc.services.catalog import (
    CatalogMedia,
    CatalogService,
    SourceNotFound,
    SourceUnavailable,
    current_season,
    next_season,
    preferred_title,
    upsert_detail,
)

log = logging.getLogger(__name__)

#: How many candidates a run may consider (§5.6: "≤ 40"). The whole pool goes
#: into the prompt with a synopsis each, so this is a token budget as much as
#: a quality one.
POOL_CAP = 40

#: What each source may contribute before the leftovers are shared out. The
#: season is capped hardest because it is the least personal of the three and
#: the only one that could fill the pool on its own.
SEASON_SHARE = 20
RELATION_SHARE = 12

#: Tag names per candidate. AniList ranks them, so the first few are the ones
#: that actually describe the show ("Time Manipulation", "Iyashikei") rather
#: than the long tail nobody voted on.
TAG_LIMIT = 6

#: Synopsis characters per candidate. Enough for the premise, short enough
#: that forty of them are a prompt rather than a book.
SYNOPSIS_CHARS = 400

#: The score at or above which a completed show's relations are worth chasing.
#: A 10-point scale, so 8 is "I would recommend this to someone".
HIGH_SCORE = 8

#: Genres considered "the user's", and how many of them a row must share to
#: qualify. Two, because one genre in common is nearly every anime ever made.
TOP_GENRES = 3
GENRE_OVERLAP = 2

#: Weight given to a watched show with no score. Mid-scale: an unscored show
#: says something about taste, just less than a 9 does.
UNSCORED_WEIGHT = 5

#: How many rows the genre query loads before the overlap filter runs in
#: Python. Postgres can answer "shares *any* of these genres" from the array;
#: "shares two" is a count, and counting 400 rows in Python is cheaper than
#: teaching the query to do it.
GENRE_SCAN = 400

#: Relations Arc has never cached that **one run** may fetch, in total. Every
#: one is a catalogue round trip on a page a user is waiting for, and each is
#: optional: a failure skips the title rather than failing the run.
RELATION_FETCH_LIMIT = 10

#: How long the whole relation-resolving phase may take. A wall clock rather
#: than a count, because the count is already bounded and the thing that
#: actually hurts is ten sequential fetches against a slow-but-not-failing
#: catalogue: ten times four seconds is a page nobody waits for. On expiry the
#: loop keeps whatever resolved and moves on — the pool has two other sources.
RELATION_DEADLINE_SECONDS = 5.0

#: Completed shows whose relations are walked. A user with a thousand finished
#: shows has tens of thousands of relation rows, and flattening all of them to
#: pick the first twelve is work thrown away. Seeds are sorted best-first, so
#: the cut is at the uninteresting end.
RELATION_SEED_LIMIT = 50

#: Rows the season query may return. The pool takes at most
#: :data:`SEASON_SHARE` of them and then tops up from the same list, so a few
#: multiples of the cap is plenty — and it bounds a query that would otherwise
#: load every cached row of two seasons to use forty.
SEASON_SCAN = POOL_CAP * 4

#: Formats that can be a recommendation. An allow-list rather than the old
#: "everything but MUSIC" deny-list: the tail of AniList's vocabulary is
#: OVA/SPECIAL/MUSIC/MANGA/NOVEL, and none of those is a show somebody starts
#: watching on a recommendation. Movies stay — a film is a perfectly good
#: Friday night — and ``TV_SHORT``/``ONA`` stay because a good deal of modern
#: television is one or the other.
ALLOWED_FORMATS = frozenset({"TV", "TV_SHORT", "ONA", "MOVIE"})

#: Title fragments that mark a recap, a special, or a five-minute theatrical
#: short. AniList files these as full entries beside the show they belong to,
#: so without this the pool offers "Frieren Recap" to somebody who has just
#: watched Frieren. Matched case-insensitively on **word** boundaries, which is
#: the whole reason this is a regex rather than a substring test: "mini" as a
#: substring catches *Administrator* and *Terminir*, and "pv" catches nothing
#: useful at all inside a longer word.
RECAP_WORDS = (
    "recap",
    "theater",
    "theatre",
    "mini",
    "special",
    "picture drama",
    "omake",
    "daze",
    "pv",
)
RECAP_PATTERN = re.compile(r"\b(?:" + "|".join(RECAP_WORDS) + r")\b", re.IGNORECASE)

#: Relation types that make one show a *continuation* of another rather than a
#: separate thing to recommend. Anything in this set, pointing at a show on the
#: user's list, is excluded from the main pool and offered by
#: :mod:`arc.services.recs.continuations` instead — where it can say which show
#: it follows, which is the only useful thing to say about a sequel.
CONTINUATION_RELATIONS = frozenset(
    {"SEQUEL", "PREQUEL", "SIDE_STORY", "SPIN_OFF", "ALTERNATIVE", "PARENT", "SUMMARY"}
)

#: The list states that put a show out of reach. ``planned`` is deliberately
#: absent (FR-R2).
EXCLUDED_STATUSES = frozenset(
    {ListStatus.WATCHING, ListStatus.COMPLETED, ListStatus.ON_HOLD, ListStatus.DROPPED}
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One title the model may pick, and why it is on the shortlist.

    ``why`` is not shown to the user: it goes into the prompt so the model can
    tell "this is simply airing now" from "this is the sequel to the show you
    gave a 9", and it is stored on the run so a pick can be explained later.
    """

    anime_id: int
    anilist_id: int | None
    mal_id: int | None
    title: str
    genres: tuple[str, ...]
    tags: tuple[str, ...]
    synopsis: str | None
    season: str | None
    season_year: int | None
    format: str | None
    episodes: int | None
    why: str

    def as_dict(self) -> dict[str, Any]:
        """The compact form stored in ``rec_runs.candidates``."""
        return {
            "anime_id": self.anime_id,
            "anilist_id": self.anilist_id,
            "mal_id": self.mal_id,
            "title": self.title,
            "genres": list(self.genres),
            "tags": list(self.tags),
            "synopsis": self.synopsis,
            "season": self.season,
            "season_year": self.season_year,
            "format": self.format,
            "episodes": self.episodes,
            "why": self.why,
        }


# --- Turning a row into a candidate -----------------------------------------


def _tag_names(tags: list[dict[str, Any]] | None) -> tuple[str, ...]:
    """The first :data:`TAG_LIMIT` tag names, in the order the source ranked."""
    if not tags:
        return ()
    names = [str(tag["name"]) for tag in tags if isinstance(tag, dict) and tag.get("name")]
    return tuple(names[:TAG_LIMIT])


def _synopsis(description: str | None) -> str | None:
    """The description as one short paragraph of plain text.

    AniList's HTML is already stripped on the way into the cache
    (:func:`arc.services.anilist.client.strip_html`) and MAL's is plain, so
    this is belt and braces plus the two things the prompt actually needs:
    newlines collapsed, and a length that forty of these can share.
    """
    text = strip_html(description)
    if text is None:
        return None
    flat = " ".join(text.split())
    if not flat:
        return None
    if len(flat) <= SYNOPSIS_CHARS:
        return flat
    return flat[:SYNOPSIS_CHARS].rstrip() + "…"


def is_recap(title: str) -> bool:
    """Whether a title reads as a recap, special or short rather than a show."""
    return bool(RECAP_PATTERN.search(title))


def candidate_of(anime: Anime, why: str) -> Candidate | None:
    """``anime`` as a candidate, or ``None`` if it is not recommendable.

    Three rejections: a format nobody starts watching on a recommendation
    (:data:`ALLOWED_FORMATS`), a title that reads as a recap or a special
    (:func:`is_recap`), and a row with no title at all — a cache entry created
    by an id lookup that never got its detail fill.
    """
    if not anime.format or anime.format.upper() not in ALLOWED_FORMATS:
        return None
    if not (anime.title_english or anime.title_romaji or anime.title_native):
        return None
    if is_recap(preferred_title(anime)):
        return None
    return Candidate(
        anime_id=anime.id,
        anilist_id=anime.anilist_id,
        mal_id=anime.mal_id,
        title=preferred_title(anime),
        genres=tuple(anime.genres or ()),
        tags=_tag_names(anime.tags),
        synopsis=_synopsis(anime.description),
        season=anime.season,
        season_year=anime.season_year,
        format=anime.format,
        episodes=anime.episodes,
        why=why,
    )


# --- Taste ------------------------------------------------------------------


def top_genres(
    rows: Sequence[tuple[Anime, ListEntry]], *, limit: int = TOP_GENRES
) -> tuple[str, ...]:
    """The user's genres, weighted by score, most-liked first.

    Only ``completed`` and ``watching`` entries count: a planned show says what
    somebody means to watch, and a dropped one says the opposite of what this
    is measuring. An unscored entry still counts, at :data:`UNSCORED_WEIGHT` —
    most people score nothing, and a pool built only from scored history would
    be empty for them.

    Ties break alphabetically so the same list always produces the same three;
    the pool is stored on the run and a non-deterministic one would make two
    runs incomparable for no gain.
    """
    weights: Counter[str] = Counter()
    for anime, entry in rows:
        if entry.status not in (ListStatus.COMPLETED, ListStatus.WATCHING):
            continue
        weight = entry.score if entry.score else UNSCORED_WEIGHT
        for genre in anime.genres or ():
            weights[genre] += weight
    ranked = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
    return tuple(genre for genre, _ in ranked[:limit])


def _relation_seeds(rows: Sequence[tuple[Anime, ListEntry]]) -> list[tuple[Anime, ListEntry]]:
    """The completed shows whose relations are worth following.

    Scored 8 or better, or — for a user who scores nothing, which is most of
    them — everything completed. Highest score first, then most recently
    touched, so the ten fetches a run can afford are spent on the shows the
    user liked most.
    """
    completed = [(anime, entry) for anime, entry in rows if entry.status is ListStatus.COMPLETED]
    high = [(anime, entry) for anime, entry in completed if (entry.score or 0) >= HIGH_SCORE]
    seeds = high or completed
    return sorted(seeds, key=lambda pair: (-(pair[1].score or 0), -pair[1].updated_at.timestamp()))


# --- The sources ------------------------------------------------------------


def _excluded_ids(rows: Iterable[tuple[Anime, ListEntry]]) -> set[int]:
    """Anime the user has already made a decision about (FR-R2)."""
    return {anime.id for anime, entry in rows if entry.status in EXCLUDED_STATUSES}


def continuation_refs(rows: Iterable[tuple[Anime, ListEntry]]) -> set[tuple[str, int]]:
    """``(kind, external id)`` of every direct continuation of a listed show.

    A sequel to something on the user's list is a real recommendation, but not
    *this* kind: the main pool asks the model to argue why a stranger's taste
    fits a show it has never seen, and "it is season two of the thing you are
    watching" needs no argument at all. Those go to
    :mod:`arc.services.recs.continuations`, which can name the show they follow.

    Keyed by external id rather than by row, because the relation blob is all
    that is known about a title Arc has never cached — and the pool has to be
    able to exclude one it has.
    """
    refs: set[tuple[str, int]] = set()
    for anime, _entry in rows:
        for relation in anime.relations or ():
            if not isinstance(relation, dict):
                continue
            if str(relation.get("relation_type") or "").upper() not in CONTINUATION_RELATIONS:
                continue
            anilist_id = relation.get("anilist_id")
            mal_id = relation.get("mal_id")
            if isinstance(anilist_id, int):
                refs.add(("anilist", anilist_id))
            if isinstance(mal_id, int):
                refs.add(("mal", mal_id))
    return refs


def _is_continuation(anime: Anime, refs: set[tuple[str, int]]) -> bool:
    """Whether ``anime`` is a continuation of something on the user's list."""
    if anime.anilist_id is not None and ("anilist", anime.anilist_id) in refs:
        return True
    return anime.mal_id is not None and ("mal", anime.mal_id) in refs


def _visible(statement: Select[tuple[Anime]], excluded: set[int]) -> Select[tuple[Anime]]:
    return statement.where(Anime.id.notin_(excluded)) if excluded else statement


async def _season_rows(session: AsyncSession, *, now: datetime, excluded: set[int]) -> list[Anime]:
    """This season's and next season's cached rows.

    Ordered by ``popularity`` — the number of people with the show on a list —
    because a season is forty to sixty titles and the pool takes twenty, so
    something has to choose, and "what everybody else is watching this season"
    is the honest answer for a source with no personal signal in it. ``NULLS
    LAST`` puts rows written before the column existed (or by a source that
    does not publish it) at the bottom rather than the top, and ``id`` breaks
    ties so two runs a minute apart see the same pool.
    """
    year, season = current_season(now)
    next_year, next_name = next_season(year, season)
    pairs = [(season, year), (next_name, next_year)]
    statement = (
        _visible(
            select(Anime).where(tuple_(Anime.season, Anime.season_year).in_(pairs)),
            excluded,
        )
        .order_by(Anime.popularity.desc().nullslast(), Anime.id)
        .limit(SEASON_SCAN)
    )
    return list((await session.execute(statement)).scalars().all())


async def _rows_by_external_id(
    session: AsyncSession, keys: Sequence[tuple[str, int]]
) -> dict[tuple[str, int], Anime]:
    """The locally cached rows for a batch of relation keys, in one query."""
    anilist_ids = [value for kind, value in keys if kind == "anilist" and value]
    mal_ids = [value for kind, value in keys if kind == "mal" and value]
    if not anilist_ids and not mal_ids:
        return {}
    clauses = []
    if anilist_ids:
        clauses.append(Anime.anilist_id.in_(anilist_ids))
    if mal_ids:
        clauses.append(Anime.mal_id.in_(mal_ids))
    rows = (await session.execute(select(Anime).where(or_(*clauses)))).scalars().all()
    found: dict[tuple[str, int], Anime] = {}
    for row in rows:
        if row.anilist_id is not None:
            found[("anilist", row.anilist_id)] = row
        if row.mal_id is not None:
            found[("mal", row.mal_id)] = row
    return found


async def _fetch_relation(catalog: CatalogService, kind: str, value: int) -> CatalogMedia | None:
    """One relation Arc has never cached, or ``None``.

    Every failure is a skip. This runs while a person waits for a page, the
    title is one of forty, and a recommendation page that 502s because a
    sequel's AniList entry could not be reached would be a worse product than
    one that quietly has thirty-nine candidates.
    """
    if kind == "anilist":
        return await catalog.by_anilist_id(value)
    return await catalog.by_mal_id(value)


async def resolve_relations(
    session: AsyncSession,
    catalog: CatalogService,
    relations: Sequence[dict[str, Any]],
    *,
    budget: RelationBudget | None = None,
) -> dict[tuple[str, int], Anime]:
    """``(kind, external id) -> anime row`` for as many relations as affordable.

    The bounded half of the recommendation page, shared by the pool and by
    :mod:`arc.services.recs.continuations`. One query for everything already
    cached, then as many catalogue fetches as ``budget`` still allows.

    ``budget`` is the run's, and passing the *same* one to both callers is what
    keeps the limit a limit — see :class:`RelationBudget`. Omitting it gives a
    fresh one, which is right for a caller that is the only thing resolving
    (and for a test).

    Every fetch is optional. A relation that cannot be resolved is simply
    absent from the result; the caller shows one fewer title, which is always
    better than failing a page over a sequel's metadata.
    """
    budget = budget if budget is not None else RelationBudget()
    keys = [relation_key(relation) for relation in relations]
    resolved = await _rows_by_external_id(session, keys)

    for kind, value in keys:
        if (kind, value) in resolved or not value:
            continue
        if not budget.spend():
            break
        try:
            media = await _fetch_relation(catalog, kind, value)
            if media is None:
                continue
            resolved[(kind, value)] = await upsert_detail(session, media)
        except (SourceNotFound, SourceUnavailable) as exc:
            log.info(
                "relation skipped", extra={"kind": kind, "external_id": value, "error": str(exc)}
            )
    return resolved


@dataclass
class RelationBudget:
    """What one *run* may spend resolving relations it has never cached.

    Mutable and passed by reference on purpose. The pool and the continuations
    section both resolve relations, and each used to start its own count and
    its own clock — so the "at most ten fetches and five seconds" the docs
    promised was really twenty and ten. One of these, created once in
    :func:`arc.services.recs.runs.run_recommendations` and handed to both, is
    what makes the promise true.

    The pool is served first, deliberately: forty candidates the model chooses
    from are worth more than the ninth sequel in a list capped at eight.
    """

    fetches_left: int = RELATION_FETCH_LIMIT
    deadline: float = field(default_factory=lambda: monotonic() + RELATION_DEADLINE_SECONDS)

    def spend(self) -> bool:
        """Take one fetch from the budget, or report that there is none left.

        Two brakes rather than one: the count stops a user with a thousand
        relations from making a thousand calls, and the wall clock is what
        saves a page from a catalogue answering *slowly* rather than failing —
        the circuit breaker never trips in that case, so nothing else would.
        """
        if self.fetches_left <= 0 or monotonic() >= self.deadline:
            return False
        self.fetches_left -= 1
        return True


def relation_key(relation: dict[str, Any]) -> tuple[str, int]:
    """The one id a relation is looked up by: AniList's if it has one.

    One key per relation rather than both, because the two lookups would find
    the same row and the second would be a wasted round trip.
    """
    anilist_id = relation.get("anilist_id")
    if isinstance(anilist_id, int):
        return ("anilist", anilist_id)
    mal_id = relation.get("mal_id")
    return ("mal", mal_id if isinstance(mal_id, int) else 0)


async def _relation_candidates(
    session: AsyncSession,
    catalog: CatalogService,
    *,
    rows: Sequence[tuple[Anime, ListEntry]],
    excluded: set[int],
    seen: set[int],
    limit: int,
    continuations: set[tuple[str, int]],
    budget: RelationBudget,
) -> list[Candidate]:
    """Relations of the user's best completed shows, resolved to local rows.

    A relation of a *listed* show is a continuation and belongs to the other
    section, so those types are dropped **before** anything is resolved: the
    seeds here are completed shows, which are on the list by definition, so
    every sequel among their relations is one this source would discard after
    paying for it. What survives is a shared-universe entry or a different
    adaptation of the same source — a real discovery.
    """
    if limit <= 0:
        return []
    seeds = _relation_seeds(rows)[:RELATION_SEED_LIMIT]
    blobs = [
        (relation, preferred_title(anime))
        for anime, _entry in seeds
        for relation in (anime.relations or ())
        if isinstance(relation, dict)
        and str(relation.get("relation_type") or "").upper() not in CONTINUATION_RELATIONS
    ]
    if not blobs:
        return []

    resolved = await resolve_relations(
        session, catalog, [relation for relation, _ in blobs], budget=budget
    )

    found: list[Candidate] = []
    for relation, seed_title in blobs:
        if len(found) >= limit:
            break
        row = resolved.get(relation_key(relation))
        if row is None or row.id in excluded or row.id in seen:
            continue
        if _is_continuation(row, continuations):
            continue
        candidate = candidate_of(row, f"related to {seed_title}, which you completed")
        if candidate is None:
            continue
        seen.add(row.id)
        found.append(candidate)
    return found


async def _genre_candidates(
    session: AsyncSession,
    *,
    genres: Sequence[str],
    excluded: set[int],
    seen: set[int],
    limit: int,
    continuations: set[tuple[str, int]],
) -> list[Candidate]:
    """Local rows sharing at least :data:`GENRE_OVERLAP` of ``genres``."""
    if limit <= 0 or len(genres) < GENRE_OVERLAP:
        return []
    # Ordered by score rather than popularity, deliberately, and it is the one
    # place the two differ: this source has already matched on the user's own
    # taste, so the remaining question is "is it any good" rather than "is it
    # well known". Popularity here would just re-list the season.
    statement = (
        _visible(select(Anime).where(Anime.genres.overlap(list(genres))), excluded)
        .order_by(Anime.average_score.desc().nullslast(), Anime.id)
        .limit(GENRE_SCAN)
    )
    wanted = set(genres)
    found: list[Candidate] = []
    for row in (await session.execute(statement)).scalars().all():
        if len(found) >= limit:
            break
        if row.id in seen or _is_continuation(row, continuations):
            continue
        shared = sorted(wanted.intersection(row.genres or ()))
        if len(shared) < GENRE_OVERLAP:
            continue
        candidate = candidate_of(row, f"shares your genres: {', '.join(shared)}")
        if candidate is None:
            continue
        seen.add(row.id)
        found.append(candidate)
    return found


def _take(candidates: Iterable[Candidate], *, limit: int, seen: set[int]) -> list[Candidate]:
    taken: list[Candidate] = []
    for candidate in candidates:
        if len(taken) >= limit:
            break
        if candidate.anime_id in seen:
            continue
        seen.add(candidate.anime_id)
        taken.append(candidate)
    return taken


# --- The pool ---------------------------------------------------------------


async def build_pool(
    session: AsyncSession,
    catalog: CatalogService,
    *,
    rows: Sequence[tuple[Anime, ListEntry]],
    now: datetime,
    budget: RelationBudget | None = None,
) -> list[Candidate]:
    """Up to :data:`POOL_CAP` candidates for one user (FR-R2).

    ``rows`` is the user's whole list — ``(anime, entry)`` pairs, as
    :func:`arc.services.catalog.lists.get_my_list` returns them — passed in
    rather than queried here because the caller needs the same rows for the
    history summary and one query is enough for both.

    ``budget`` is the run's relation budget, shared with the continuations
    section so the two together stay inside one limit.
    """
    budget = budget if budget is not None else RelationBudget()
    excluded = _excluded_ids(rows)
    # Continuations of listed shows are excluded from the *main* pool and
    # offered by their own section instead (FR-R2, §5.6).
    continuations = continuation_refs(rows)
    seen: set[int] = set()

    season_rows = await _season_rows(session, now=now, excluded=excluded)
    year, season = current_season(now)
    next_year, next_name = next_season(year, season)

    def season_why(row: Anime) -> str:
        if row.season == season and row.season_year == year:
            return f"airing this season ({season.title()} {year})"
        return f"starts next season ({next_name.title()} {next_year})"

    seasonal = [
        candidate
        for candidate in (
            candidate_of(row, season_why(row))
            for row in season_rows
            if not _is_continuation(row, continuations)
        )
        if candidate is not None
    ]
    pool = _take(seasonal, limit=SEASON_SHARE, seen=seen)

    pool += await _relation_candidates(
        session,
        catalog,
        rows=rows,
        excluded=excluded,
        seen=seen,
        limit=min(RELATION_SHARE, POOL_CAP - len(pool)),
        continuations=continuations,
        budget=budget,
    )

    genres = top_genres(rows)
    pool += await _genre_candidates(
        session,
        genres=genres,
        excluded=excluded,
        seen=seen,
        limit=POOL_CAP - len(pool),
        continuations=continuations,
    )

    # Whatever the personal sources did not use goes back to the season, so a
    # brand-new account with no history still gets a full forty.
    if len(pool) < POOL_CAP:
        pool += _take(seasonal, limit=POOL_CAP - len(pool), seen=seen)

    log.info(
        "recommendation pool built",
        extra={"candidates": len(pool), "excluded": len(excluded), "genres": list(genres)},
    )
    return pool[:POOL_CAP]


__all__ = [
    "ALLOWED_FORMATS",
    "CONTINUATION_RELATIONS",
    "EXCLUDED_STATUSES",
    "GENRE_OVERLAP",
    "HIGH_SCORE",
    "POOL_CAP",
    "RELATION_DEADLINE_SECONDS",
    "RELATION_FETCH_LIMIT",
    "RELATION_SEED_LIMIT",
    "RELATION_SHARE",
    "SEASON_SCAN",
    "RelationBudget",
    "SEASON_SHARE",
    "SYNOPSIS_CHARS",
    "TAG_LIMIT",
    "TOP_GENRES",
    "Candidate",
    "build_pool",
    "candidate_of",
    "continuation_refs",
    "resolve_relations",
    "is_recap",
    "top_genres",
]
