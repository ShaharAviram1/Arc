"""Searching the rows Arc already has (FR-C1, architecture.md §5.0).

``/api/anime/search`` is a *live* search, and that is still the point of it: the
cache only holds what somebody has already looked at, so a catalogue that
answered only from it could never find a show nobody has added yet. But the
converse bit an owner: searching "jobless reincarnation" for a show that is
cached, on their list, and half-downloaded returned nothing at all, because
AniList was disabled upstream and MyAnimeList's search does not match on a
partial title. A catalogue that cannot find the thing it is currently
downloading is not a catalogue.

So the local rows come first and the live page is merged in behind them
(:func:`arc.api.anime.search`). Three rules shape what "matches" means here.

1. **Every word must appear somewhere in the title.** The query is split on
   whitespace and each word is matched as a case-insensitive substring against
   ``title_romaji``, ``title_english`` and the synonyms — not necessarily the
   same field for each word, because "mushoku jobless" is a perfectly
   reasonable thing for somebody to type at a row whose romaji and English
   titles each hold half of it. ``ILIKE`` rather than a full-text index: the
   whole table is a few thousand rows, the pattern is unanchored (which is the
   entire feature — MAL's search is what a prefix match already gets you), and
   ``to_tsquery`` would not match "reincarn" either.

2. **Synonyms are searched as the stored JSON text.** ``anime.synonyms`` is a
   JSONB array of strings and is cast to text for the comparison, so the
   pattern is run over ``["Mushoku Tensei", "Jobless Reincarnation"]`` rather
   than over each element. That can only produce a false positive for a term
   containing the ``", "`` that separates two elements, and a term never can:
   the query is split on whitespace, so no term holds a space.

3. **Order is "what you meant" first.** An exact or prefix title match leads —
   typing "frieren" must not put a side story above the show — then the shows
   the caller follows, then popularity, so that a title everybody watches
   outranks an obscure OVA that happens to share a word. ``NULLS LAST`` on
   popularity because it is null on any row no detail fetch has reached.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import Text, and_, case, cast, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.expression import select

from arc.models import Anime, ListEntry

#: How many cached rows a search may put in front of the live page. Twenty is
#: one screen of cards, and the live results still follow it.
LOCAL_SEARCH_LIMIT: Final[int] = 20

#: Words beyond this are ignored. A query is capped at 100 characters, so this
#: only ever bites on someone pasting a release name, where the first eight
#: words have already decided the answer and the rest are another eight
#: ``ILIKE`` scans.
MAX_TERMS: Final[int] = 8

#: The escape character for ``ILIKE``. A title can contain a literal ``%`` or
#: ``_`` and a user can certainly type one; without this, "100%" would match
#: every row in the table.
ESCAPE: Final[str] = "\\"


def escape_like(term: str) -> str:
    """``term`` with the ``LIKE`` metacharacters neutralised."""
    return term.replace(ESCAPE, ESCAPE * 2).replace("%", f"{ESCAPE}%").replace("_", f"{ESCAPE}_")


def terms_of(query: str) -> list[str]:
    """The query as the words that must each match, at most :data:`MAX_TERMS`."""
    return query.split()[:MAX_TERMS]


def _matches(pattern: str) -> ColumnElement[bool]:
    """One word against the two titles and the synonyms (rules 1 and 2)."""
    return or_(
        Anime.title_romaji.ilike(pattern, escape=ESCAPE),
        Anime.title_english.ilike(pattern, escape=ESCAPE),
        cast(Anime.synonyms, Text).ilike(pattern, escape=ESCAPE),
    )


def _title_is(pattern: str) -> ColumnElement[bool]:
    """Whether either rendered title matches ``pattern`` as a whole.

    Titles only, deliberately: the ranking answers "did they type this show's
    name?", and a synonym buried in a list of fifteen is not that.
    """
    return or_(
        Anime.title_romaji.ilike(pattern, escape=ESCAPE),
        Anime.title_english.ilike(pattern, escape=ESCAPE),
    )


async def local_search(
    session: AsyncSession,
    query: str,
    *,
    user_id: int,
    limit: int = LOCAL_SEARCH_LIMIT,
) -> list[Anime]:
    """The cached shows matching ``query``, best first.

    Returns ``[]`` for a query that is nothing but whitespace — the route's
    minimum length is measured on the raw string, so "  " arrives here.
    """
    terms = terms_of(query)
    if not terms:
        return []

    whole = escape_like(query.strip())
    rank = case(
        (_title_is(whole), 0),
        (_title_is(f"{whole}%"), 1),
        else_=2,
    )
    # 0 sorts first, so "the caller has this on their list" is 0. The outer
    # join is on the composite primary key of ``list_entries``, so it can
    # never turn one anime row into two.
    followed = case((ListEntry.user_id.is_not(None), 0), else_=1)

    rows = await session.scalars(
        select(Anime)
        .outerjoin(ListEntry, and_(ListEntry.anime_id == Anime.id, ListEntry.user_id == user_id))
        .where(*[_matches(f"%{escape_like(term)}%") for term in terms])
        .order_by(rank, followed, Anime.popularity.desc().nullslast(), Anime.id)
        .limit(max(int(limit), 0))
    )
    return list(rows.all())


__all__ = ["ESCAPE", "LOCAL_SEARCH_LIMIT", "MAX_TERMS", "escape_like", "local_search", "terms_of"]
