"""Which anime season a date falls in.

Anime seasons are three calendar months each and are named the same way by
both sources — AniList in capitals, MAL in lower case — so the only real
content here is the quarter arithmetic and the wrap from FALL to the next
year's WINTER.

Pure functions with an explicit date argument: the season pre-cache (FR-C7)
runs at 03:30 UTC and a test has to be able to say what day it is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

#: In calendar order, one per quarter. AniList's ``MediaSeason`` vocabulary,
#: which is what ``anime.season`` stores; MAL's lower-case names are mapped to
#: it on the way in.
SEASONS = ("WINTER", "SPRING", "SUMMER", "FALL")


def season_of(when: date) -> tuple[int, str]:
    """``(year, season)`` for a date. Jan–Mar WINTER … Oct–Dec FALL."""
    return (when.year, SEASONS[(when.month - 1) // 3])


def next_season(year: int, season: str) -> tuple[int, str]:
    """The season after ``(year, season)``, rolling FALL into the next WINTER."""
    index = SEASONS.index(season.upper())
    if index == len(SEASONS) - 1:
        return (year + 1, SEASONS[0])
    return (year, SEASONS[index + 1])


def current_season(now: datetime | None = None) -> tuple[int, str]:
    """The season Arc is in, measured in UTC."""
    return season_of((now or datetime.now(UTC)).date())


__all__ = ["SEASONS", "current_season", "next_season", "season_of"]
