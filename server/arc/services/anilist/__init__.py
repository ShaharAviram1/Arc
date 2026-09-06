"""AniList: the GraphQL client and the source adapter around it.

* ``queries`` — the five GraphQL documents
* ``client`` — :class:`AniListClient`, paced and retrying, returning the
  source-neutral dataclasses of :mod:`arc.services.catalog.source`
* ``source`` — :class:`AniListSource`, the same client behind the
  :class:`~arc.services.catalog.source.CatalogSource` interface

The cache, the refresh jobs and the fallback logic used to live here and now
live in :mod:`arc.services.catalog`: they stopped being about AniList the day
a second source could answer the same questions (FR-C6).
"""

from __future__ import annotations

from arc.services.anilist.client import (
    ANILIST_URL,
    SEARCH_PER_PAGE,
    AniListClient,
    AniListDisabled,
    AniListError,
    AniListNotFound,
    parse_media,
    strip_html,
)
from arc.services.anilist.source import AniListSource

__all__ = [
    "ANILIST_URL",
    "SEARCH_PER_PAGE",
    "AniListClient",
    "AniListDisabled",
    "AniListError",
    "AniListNotFound",
    "AniListSource",
    "parse_media",
    "strip_html",
]
