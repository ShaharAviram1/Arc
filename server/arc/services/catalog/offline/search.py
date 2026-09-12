"""Searching the offline catalogue (FR-C1, FR-C6, FR-C7).

41,537 titles and every name each of them was ever released under, in a table
that cannot be unreachable. This is the *second* stop of a search — behind the
rows Arc already has and in front of the live page — and the first stop of
filename matching, which is the half of M15.5 that matters most: a release
group writes "Mushoku Tensei S3", and that exact string is one of manami's
synonyms while no live search finds it at all.

The matching rules are :mod:`arc.services.catalog.local`'s, deliberately, so
that one search does not mean two things depending on which table answered:
every word of the query must appear as a case-insensitive substring, ``%`` and
``_`` are escaped, and the query is capped at the same eight words. What is
different is only where it looks. ``offline_anime.search_text`` is the title
and every synonym, lowercased and joined by ``" | "`` at import time, with a
trigram GIN index over it — so an unanchored ``ILIKE '%word%'`` over 41k rows
is an index scan rather than a sequential one, which is the entire reason the
column is denormalised.

**Ranking.** Exact name first, then prefix, then the dataset's own score, and
only then the type. "Exact" and "prefix" are judged against the *whole* name
list rather than against ``title`` alone, unlike ``local.py``: manami publishes
one title per entry and it is the romaji one, so *Frieren: Beyond Journey's
End* is a synonym and a rule that read titles only would rank the show nowhere
for its English name. The separator makes that a single ``LIKE`` — a name is
exact when ``" | " + search_text + " | "`` contains ``" | " + query + " | "``.

Type is the tie-break rather than a filter. Nothing is excluded — an OVA is a
real answer to a search for an OVA — but where two entries score the same (and
the thousands with no score at all *all* score the same), the series and the
films come before the specials, because a franchise's three-minute short is
almost never what somebody typing its name meant.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from arc.models import OfflineAnime
from arc.services.catalog.local import ESCAPE, escape_like, terms_of
from arc.services.catalog.offline.materialise import media_from
from arc.services.catalog.offline.parse import SEARCH_SEPARATOR
from arc.services.catalog.source import CatalogMedia

#: How many offline hits a search may put in front of the live page. The same
#: twenty as the local cache, and the API narrows it further so that the two of
#: them together are still one screen of cards.
OFFLINE_SEARCH_LIMIT: Final[int] = 20

#: How many rows a season seed will take. Bigger than any real season — 2026's
#: busiest has about 350 entries once OVAs and shorts are counted — and small
#: enough that a bad ``year`` cannot ask Postgres for the whole table.
OFFLINE_SEASON_LIMIT: Final[int] = 1000

#: Types that get the tie-break, in the order they win it. Everything else —
#: OVA, SPECIAL, UNKNOWN, and whatever next week's release invents — sorts
#: after them.
RANKED_TYPES: Final[tuple[str, ...]] = ("TV", "MOVIE", "ONA")


def _padded() -> ColumnElement[str]:
    """``search_text`` with a separator at each end.

    So that the first and last names in the list are surrounded by exactly what
    the ones in the middle are, and one ``LIKE`` pattern can ask about all of
    them without three special cases.
    """
    return literal(SEARCH_SEPARATOR) + OfflineAnime.search_text + literal(SEARCH_SEPARATOR)


def _type_rank() -> ColumnElement[int]:
    """0, 1, 2 for TV/MOVIE/ONA; 3 for everything else."""
    return case(
        {name: index for index, name in enumerate(RANKED_TYPES)},
        value=OfflineAnime.type,
        else_=len(RANKED_TYPES),
    )


async def offline_search(
    session: AsyncSession, term: str, *, limit: int = OFFLINE_SEARCH_LIMIT
) -> list[OfflineAnime]:
    """The offline titles matching ``term``, best first.

    Returns ``[]`` for a term that is nothing but whitespace, and for an empty
    table — a deployment that has never run the import must degrade to the
    behaviour it had before M15.5, not to an error.
    """
    terms = terms_of(term)
    if not terms:
        return []

    # ``search_text`` is lowercased at import time precisely so this can be a
    # plain ``LIKE`` against the trigram index rather than an ``ILIKE`` or a
    # functional index.
    whole = escape_like(term.strip().lower())
    exact = _padded().like(f"%{SEARCH_SEPARATOR}{whole}{SEARCH_SEPARATOR}%", escape=ESCAPE)
    prefix = _padded().like(f"%{SEARCH_SEPARATOR}{whole}%", escape=ESCAPE)
    rank = case((exact, 0), (prefix, 1), else_=2)

    rows = await session.scalars(
        select(OfflineAnime)
        .where(
            *[
                OfflineAnime.search_text.like(f"%{escape_like(word.lower())}%", escape=ESCAPE)
                for word in terms
            ]
        )
        .order_by(
            rank,
            OfflineAnime.score.desc().nullslast(),
            _type_rank(),
            OfflineAnime.episodes.desc().nullslast(),
            OfflineAnime.id,
        )
        .limit(max(int(limit), 0))
    )
    return list(rows.all())


async def offline_season(
    session: AsyncSession, year: int, season: str, *, limit: int = OFFLINE_SEASON_LIMIT
) -> list[CatalogMedia]:
    """One season's titles as catalogue summaries, for the seed (FR-C7).

    Every type, not only the weekly ones: the season page lists films and OVAs
    beside the series (``catalog/schedule.py`` puts them under "unscheduled"),
    and a seed that dropped them would make the page smaller during an outage
    than it is the rest of the time.

    Carries no airing information at all — the dataset has none — so a row
    seeded from here has no ``next_airing`` and gets no episodes. That is the
    documented shape of the fallback: the season still lists, the air times are
    what the outage costs (FR-C6, FR-C7).
    """
    rows = await session.scalars(
        select(OfflineAnime)
        .where(
            OfflineAnime.season_year == year,
            func.upper(OfflineAnime.season) == season.upper(),
        )
        .order_by(OfflineAnime.score.desc().nullslast(), _type_rank(), OfflineAnime.id)
        .limit(max(int(limit), 0))
    )
    return [media_from(row) for row in rows.all()]


__all__ = [
    "OFFLINE_SEARCH_LIMIT",
    "OFFLINE_SEASON_LIMIT",
    "RANKED_TYPES",
    "offline_search",
    "offline_season",
]
