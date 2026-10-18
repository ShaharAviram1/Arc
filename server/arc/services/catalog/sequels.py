"""A show's sequel, as Arc's own catalogue knows it (FR-S5 item 3).

The player offers "Next season" at the last episode of a finished season
(owner, 2026-10-18, after the real-iPad check): the same show's episodes run
out, and the catalogue knows what comes next — ``anime.relations`` names the
``SEQUEL`` by AniList and MAL id, and the sequel is often a row of its own.

Only a sequel **with a row in Arc** is offered: "not in the catalogue" means
no offer. And only a **series** sequel is a next season — TV, TV short or
ONA (:data:`SEASON_FORMATS`); a film, an OVA or a special that follows a
season is not what "Next season" promises, so it is never offered.

Nothing here fetches from a source to change any of that — a player that
asked AniList about every last episode would spend the rate budget on a
question the show page answers when somebody opens the sequel.

The rules are pure functions over rows the caller already has, plus one query
(:func:`find_sequel`) that resolves the relation ids, matching the same two
sources the show page's franchise rail does
(:func:`arc.api.anime._related_anime`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, Episode
from arc.services.catalog.airing import FINISHED

#: The relation type the offer follows. A prequel or a side story is not
#: "the next season".
SEQUEL = "SEQUEL"

#: The formats a "Next season" may be, in the order they are preferred when
#: several sequels resolve (AniList's vocabulary, which MAL rows are mapped to).
#: Only a TV-like series is a *season*: a ``MOVIE``, ``SPECIAL``, ``OVA`` or
#: ``MUSIC`` sequel — or one whose format nobody stated — is never offered, even
#: when it is the only sequel Arc has (orchestrator, 2026-10-18).
SEASON_FORMATS: tuple[str, ...] = ("TV", "TV_SHORT", "ONA")


@dataclass(frozen=True, slots=True)
class SequelCandidate:
    """A resolved sequel relation: Arc's row id and the two facts the choice reads."""

    anime_id: int
    format: str | None
    season_year: int | None


def season_ends_here(anime: Anime, episode: Episode, *, has_following: bool) -> bool:
    """Whether ``episode`` is the end of its show, so a sequel may be offered.

    No following row, **and** the show is finished at this point. When the
    source states a count, the episode's number must reach it — whatever the
    status says, so a FINISHED show whose rows stop at 10 of 12 (a stale or
    grown count, a row not listed yet) offers no sequel at 10. With no stated
    count, the status must say ``FINISHED``. An airing show that simply has no
    next row yet is not finished — the next episode is coming, and offering
    the sequel there would skip it.
    """
    if has_following:
        return False
    if anime.episodes is not None and anime.episodes > 0:
        return episode.number >= anime.episodes
    return anime.status == FINISHED


def sequel_ids(relations: Sequence[Any] | None) -> tuple[set[int], set[int]]:
    """The AniList and MAL ids of the ``SEQUEL`` relations in a stored blob."""
    anilist_ids: set[int] = set()
    mal_ids: set[int] = set()
    for raw in relations or []:
        if not isinstance(raw, dict) or raw.get("relation_type") != SEQUEL:
            continue
        if raw.get("anilist_id") is not None:
            anilist_ids.add(int(raw["anilist_id"]))
        if raw.get("mal_id") is not None:
            mal_ids.add(int(raw["mal_id"]))
    return anilist_ids, mal_ids


def pick_sequel(
    relations: Sequence[Any] | None,
    rows: Mapping[tuple[str, int], SequelCandidate],
) -> int | None:
    """Arc's id for the sequel to offer, or ``None``.

    ``rows`` is ``(source, external id) → candidate``, as :func:`find_sequel`
    reads it. Each ``SEQUEL`` relation resolves by its AniList id first and
    its MAL id second (the show page's rule); one with no row is ignored, and
    so is one whose format (the relation's, else the row's) is not in
    :data:`SEASON_FORMATS`. Several resolved: by :data:`SEASON_FORMATS`' order
    (TV first), then the earliest ``season_year`` (an unknown year last), then
    the lowest id so the answer is stable.
    """
    found: dict[int, SequelCandidate] = {}
    for raw in relations or []:
        if not isinstance(raw, dict) or raw.get("relation_type") != SEQUEL:
            continue
        row: SequelCandidate | None = None
        if raw.get("anilist_id") is not None:
            row = rows.get(("anilist", int(raw["anilist_id"])))
        if row is None and raw.get("mal_id") is not None:
            row = rows.get(("mal", int(raw["mal_id"])))
        if row is None:
            continue
        stated = raw.get("format")
        fmt = stated if isinstance(stated, str) and stated else row.format
        if fmt not in SEASON_FORMATS:
            continue
        found[row.anime_id] = SequelCandidate(
            anime_id=row.anime_id, format=fmt, season_year=row.season_year
        )
    if not found:
        return None
    best = min(
        found.values(),
        key=lambda candidate: (
            SEASON_FORMATS.index(candidate.format or ""),
            candidate.season_year is None,
            candidate.season_year or 0,
            candidate.anime_id,
        ),
    )
    return best.anime_id


def first_regular_episode(episodes: Sequence[Episode]) -> Episode | None:
    """The lowest-numbered episode numbered 1 or more (0 is a special or a prologue)."""
    regular = [row for row in episodes if row.number >= 1]
    return min(regular, key=lambda row: row.number) if regular else None


async def find_sequel(session: AsyncSession, anime: Anime) -> Anime | None:
    """The sequel row :func:`pick_sequel` chooses for ``anime``, or ``None``. One query, or two."""
    anilist_ids, mal_ids = sequel_ids(anime.relations)
    if not anilist_ids and not mal_ids:
        return None
    clauses = []
    if anilist_ids:
        clauses.append(Anime.anilist_id.in_(anilist_ids))
    if mal_ids:
        clauses.append(Anime.mal_id.in_(mal_ids))
    result = await session.execute(
        select(Anime.id, Anime.anilist_id, Anime.mal_id, Anime.format, Anime.season_year).where(
            or_(*clauses), Anime.id != anime.id
        )
    )
    rows: dict[tuple[str, int], SequelCandidate] = {}
    for row in result.all():
        candidate = SequelCandidate(anime_id=row.id, format=row.format, season_year=row.season_year)
        if row.anilist_id is not None:
            rows[("anilist", row.anilist_id)] = candidate
        if row.mal_id is not None:
            rows[("mal", row.mal_id)] = candidate
    chosen = pick_sequel(anime.relations, rows)
    return await session.get(Anime, chosen) if chosen is not None else None


__all__ = [
    "SEASON_FORMATS",
    "SEQUEL",
    "SequelCandidate",
    "find_sequel",
    "first_regular_episode",
    "pick_sequel",
    "season_ends_here",
    "sequel_ids",
]
