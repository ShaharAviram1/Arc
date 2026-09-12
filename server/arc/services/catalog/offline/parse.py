"""Turning the two downloaded files into rows. Pure functions only.

Nothing here touches the network, the filesystem or a session: every function
takes text (or already-decoded JSON) and returns rows. That is what makes the
awkward half of this feature — the leniency — testable. Both files are
somebody else's, published weekly, and every field in them is optional in
practice: a missing ``animeSeason.year``, a ``score`` that is simply absent, an
``imdb_id`` that is a string this week and a list the next, a ``themoviedb_id``
that is an integer in old entries and ``{"tv": N}`` in new ones. A parser that
raised on any of those would turn a dataset change into a week with no
catalogue, which is the opposite of what M15.5 is for.

So the rule is: **skip what cannot be read, keep what can**. An entry with no
title is dropped (it could never be searched); everything else degrades to
``None``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any, NamedTuple, TypedDict

#: The ``sources`` hosts Arc knows how to read an id out of, and the column
#: each one fills. manami lists ten hosts per entry; the other six (Anime-Planet
#: and anisearch use slugs, animecountdown/simkl/ANN/livechart are not ids Arc
#: maps anything by) are ignored rather than stored, because a column nothing
#: queries is a column that is wrong without anyone noticing.
#:
#: Kitsu moved from ``kitsu.io`` to ``kitsu.app``; both are accepted so an
#: older release still parses.
SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anilist_id", re.compile(r"^https?://(?:www\.)?anilist\.co/anime/(\d+)", re.IGNORECASE)),
    ("mal_id", re.compile(r"^https?://(?:www\.)?myanimelist\.net/anime/(\d+)", re.IGNORECASE)),
    ("kitsu_id", re.compile(r"^https?://(?:www\.)?kitsu\.(?:app|io)/anime/(\d+)", re.IGNORECASE)),
    ("anidb_id", re.compile(r"^https?://(?:www\.)?anidb\.net/anime/(\d+)", re.IGNORECASE)),
)

#: The release tag inside the header's ``$schema`` URL:
#: ``…/refs/tags/2026-27/schemas/…`` → ``2026-27``.
TAG_PATTERN = re.compile(r"/refs/tags/([^/]+)/")

#: ``duration.unit`` → seconds. The live file only ever says ``SECONDS``, but
#: the schema allows the others and a unit Arc does not know must mean "no
#: duration" rather than "120 of something".
DURATION_UNITS: dict[str, int] = {
    "SECONDS": 1,
    "MINUTES": 60,
    "HOURS": 3600,
    "DAYS": 86400,
}

#: What ``search_text`` joins the title and its synonyms with. A separator that
#: cannot occur inside a title, so a search for one title can never match
#: across the boundary between two.
SEARCH_SEPARATOR = " | "


class ManamiHeader(NamedTuple):
    """The first line of the JSONL file.

    ``tag`` is the release ("2026-27"), read out of the ``$schema`` URL because
    the header does not state it anywhere else; ``last_update`` is the date the
    dataset itself claims. Both may be ``None`` on a file whose header has
    changed shape — the import stores what it got and carries on.
    """

    tag: str | None
    last_update: str | None


class OfflineAnimeRow(TypedDict):
    """One ``offline_anime`` row, shaped for ``insert()``'s executemany."""

    anilist_id: int | None
    mal_id: int | None
    kitsu_id: int | None
    anidb_id: int | None
    title: str
    synonyms: list[str]
    type: str | None
    episodes: int | None
    status: str | None
    season: str | None
    season_year: int | None
    picture: str | None
    thumbnail: str | None
    studios: list[str]
    tags: list[str]
    score: float | None
    duration_seconds: int | None
    related: list[str]
    search_text: str


class OfflineIdRow(TypedDict):
    """One ``offline_ids`` row."""

    anidb_id: int | None
    anilist_id: int | None
    mal_id: int | None
    kitsu_id: int | None
    tmdb_tv_id: int | None
    tmdb_movie_id: int | None
    tmdb_season: int | None
    tvdb_id: int | None
    tvdb_season: int | None
    imdb_id: str | None
    type: str | None


# --- small coercions --------------------------------------------------------


def _as_int(value: object) -> int | None:
    """An integer, or ``None`` for anything that is not one.

    Accepts a numeric string because both files have carried them: Fribb's
    ``tvdb_id`` is occasionally ``"unknown"`` and occasionally ``"81797"``.
    """
    if isinstance(value, bool):  # bool is an int; a flag is not an id
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.lstrip("-").isdigit() else None
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_str(value: object) -> str | None:
    """A non-empty string, or ``None``. Never a coerced number."""
    return value if isinstance(value, str) and value.strip() else None


def _as_str_list(value: object) -> list[str]:
    """A list of non-empty strings; anything else is an empty list."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


# --- manami -----------------------------------------------------------------


def ids_from_sources(sources: object) -> dict[str, int | None]:
    """Pull the four ids Arc maps by out of an entry's ``sources`` URLs.

    Always returns all four keys so the row shape never depends on the data.
    The first match for a host wins: a duplicate would be a bug in the file,
    and taking the first is at least deterministic.
    """
    found: dict[str, int | None] = {column: None for column, _ in SOURCE_PATTERNS}
    if not isinstance(sources, list):
        return found
    for url in sources:
        if not isinstance(url, str):
            continue
        for column, pattern in SOURCE_PATTERNS:
            if found[column] is not None:
                continue
            match = pattern.match(url)
            if match is not None:
                found[column] = int(match.group(1))
                break
    return found


def build_search_text(title: str, synonyms: Iterable[str]) -> str:
    """``title`` and its synonyms, lowercased, de-duplicated, in order.

    De-duplicated because the dataset frequently repeats the title inside the
    synonyms, and order is preserved because the title should be the first
    thing a human reading the column sees. Lowercased here rather than in the
    query so an ``ILIKE`` can use the trigram index without a functional one.
    """
    seen: dict[str, None] = {}
    for value in (title, *synonyms):
        lowered = value.strip().lower()
        if lowered:
            seen.setdefault(lowered, None)
    return SEARCH_SEPARATOR.join(seen)


def duration_seconds(duration: object) -> int | None:
    """``{"value": 120, "unit": "SECONDS"}`` → ``120``; unknown unit → ``None``."""
    if not isinstance(duration, dict):
        return None
    value = _as_int(duration.get("value"))
    if value is None:
        return None
    factor = DURATION_UNITS.get(str(duration.get("unit", "")).upper())
    return None if factor is None else value * factor


def parse_manami_header(line: str) -> ManamiHeader:
    """Read the release tag and ``lastUpdate`` out of the first line."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return ManamiHeader(None, None)
    if not isinstance(payload, dict):
        return ManamiHeader(None, None)
    schema = _as_str(payload.get("$schema")) or ""
    match = TAG_PATTERN.search(schema)
    return ManamiHeader(
        tag=match.group(1) if match else None,
        last_update=_as_str(payload.get("lastUpdate")),
    )


def manami_row(entry: dict[str, Any]) -> OfflineAnimeRow | None:
    """One dataset entry as a row, or ``None`` if it cannot be one.

    The only thing that disqualifies an entry is having no title: such a row
    could never be searched, matched or displayed, so storing it would only
    make the counts lie.
    """
    title = _as_str(entry.get("title"))
    if title is None:
        return None
    ids = ids_from_sources(entry.get("sources"))
    synonyms = _as_str_list(entry.get("synonyms"))
    season = entry.get("animeSeason")
    season = season if isinstance(season, dict) else {}
    score = entry.get("score")
    score = score if isinstance(score, dict) else {}
    return OfflineAnimeRow(
        anilist_id=ids["anilist_id"],
        mal_id=ids["mal_id"],
        kitsu_id=ids["kitsu_id"],
        anidb_id=ids["anidb_id"],
        title=title,
        synonyms=synonyms,
        type=_as_str(entry.get("type")),
        episodes=_as_int(entry.get("episodes")),
        status=_as_str(entry.get("status")),
        season=_as_str(season.get("season")),
        season_year=_as_int(season.get("year")),
        picture=_as_str(entry.get("picture")),
        thumbnail=_as_str(entry.get("thumbnail")),
        studios=_as_str_list(entry.get("studios")),
        tags=_as_str_list(entry.get("tags")),
        score=_as_float(score.get("arithmeticMean")),
        duration_seconds=duration_seconds(entry.get("duration")),
        related=_as_str_list(entry.get("relatedAnime")),
        search_text=build_search_text(title, synonyms),
    )


def parse_manami(lines: Iterable[str]) -> tuple[ManamiHeader, Iterator[OfflineAnimeRow]]:
    """``(header, rows)`` for a manami JSONL file.

    The header is read eagerly (it is the first line and the caller needs the
    version before it decides anything); the rows are a **generator**, because
    the file is 62 MB of JSON and materialising 41k dictionaries before the
    first one reaches the database would be the peak memory of the whole
    worker. The importer chunks straight off this iterator.

    A line that is not JSON, or not an object, is skipped rather than fatal:
    one corrupt line must not cost the other forty thousand.
    """
    iterator = iter(lines)
    header = ManamiHeader(None, None)
    for line in iterator:
        if line.strip():
            header = parse_manami_header(line)
            break

    def rows() -> Iterator[OfflineAnimeRow]:
        for line in iterator:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            row = manami_row(entry)
            if row is not None:
                yield row

    return header, rows()


# --- Fribb ------------------------------------------------------------------


def _first_int(value: object) -> int | None:
    """An id that may have been written as a list of them. First one wins.

    Fribb's ``themoviedb_id.movie`` is a **list** in every entry of the current
    file (``{"movie": [129]}``) while ``tv`` is a bare integer, and an anime
    film that TMDB split into several releases has several. Arc wants one id to
    enrich from, and the first is the one the entry leads with.
    """
    if isinstance(value, list):
        for item in value:
            found = _as_int(item)
            if found is not None:
                return found
        return None
    return _as_int(value)


def _tmdb(value: object) -> tuple[int | None, int | None]:
    """``themoviedb_id`` → ``(series id, film id)``.

    Four shapes have been seen: ``{"tv": N}``, ``{"movie": [N, …]}``, ``null``,
    and — in older releases — a bare integer, which is treated as a *series*
    id because that is what it meant when the field had one namespace.
    """
    if isinstance(value, dict):
        return _first_int(value.get("tv")), _first_int(value.get("movie"))
    return _first_int(value), None


def _first_imdb(value: object) -> str | None:
    """The first IMDb id, whether the field is a string or a list of them."""
    if isinstance(value, str):
        return _as_str(value)
    if isinstance(value, list):
        for item in value:
            found = _as_str(item)
            if found is not None:
                return found
    return None


def fribb_row(entry: dict[str, Any]) -> OfflineIdRow | None:
    """One Fribb entry as a row, or ``None`` when it maps nothing.

    "Maps nothing" means it carries neither of the two ids Arc looks up by
    (AniList, MAL) — such a row could never be reached from an Arc show, so it
    is 41k rows of storage for no lookup.
    """
    anilist_id = _as_int(entry.get("anilist_id"))
    mal_id = _as_int(entry.get("mal_id"))
    if anilist_id is None and mal_id is None:
        return None
    season = entry.get("season")
    season = season if isinstance(season, dict) else {}
    tmdb_tv, tmdb_movie = _tmdb(entry.get("themoviedb_id"))
    return OfflineIdRow(
        anidb_id=_as_int(entry.get("anidb_id")),
        anilist_id=anilist_id,
        mal_id=mal_id,
        kitsu_id=_as_int(entry.get("kitsu_id")),
        tmdb_tv_id=tmdb_tv,
        tmdb_movie_id=tmdb_movie,
        tmdb_season=_as_int(season.get("tmdb")),
        tvdb_id=_as_int(entry.get("tvdb_id")),
        tvdb_season=_as_int(season.get("tvdb")),
        imdb_id=_first_imdb(entry.get("imdb_id")),
        type=_as_str(entry.get("type")),
    )


def parse_fribb(data: object) -> list[OfflineIdRow]:
    """Every mappable entry of Fribb's ``anime-list-full.json``.

    A list rather than an iterator, unlike manami's: the file is one JSON array
    that has to be decoded whole anyway, so there is nothing to stream.
    Anything that is not a list of objects yields no rows rather than raising —
    an HTML error page saved as JSON is a plausible download, and it must leave
    the existing table alone.
    """
    if not isinstance(data, list):
        return []
    rows: list[OfflineIdRow] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        row = fribb_row(entry)
        if row is not None:
            rows.append(row)
    return rows


__all__ = [
    "DURATION_UNITS",
    "SEARCH_SEPARATOR",
    "SOURCE_PATTERNS",
    "TAG_PATTERN",
    "ManamiHeader",
    "OfflineAnimeRow",
    "OfflineIdRow",
    "build_search_text",
    "duration_seconds",
    "fribb_row",
    "ids_from_sources",
    "manami_row",
    "parse_fribb",
    "parse_manami",
    "parse_manami_header",
]
