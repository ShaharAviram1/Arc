"""MyAnimeList.

Today this package holds only the read-only catalogue fallback (FR-C6):
:class:`MalSource` over MAL API v2, authenticated with nothing but the
``X-MAL-CLIENT-ID`` header. M9 adds the OAuth half — list import, the write
log, and the sync rules — beside it.

Keeping the two apart matters more than it looks: the non-negotiable in
CLAUDE.md is that no code path writes to MAL except from a user-originated
event, and a module that physically cannot write is the cheapest way to keep
the catalogue fallback out of that argument.
"""

from __future__ import annotations

from arc.services.mal.catalog import (
    MAL_API_URL,
    MalSource,
    parse_anime,
    parse_broadcast,
    parse_date,
    synthesise_airing,
)

__all__ = [
    "MAL_API_URL",
    "MalSource",
    "parse_anime",
    "parse_broadcast",
    "parse_date",
    "synthesise_airing",
]
