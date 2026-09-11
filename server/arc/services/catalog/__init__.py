"""The catalogue: where titles come from, and what they mean for a user.

* ``source`` — :class:`CatalogSource`, :class:`CatalogMedia`, and the two
  errors every source speaks in
* ``breaker`` — per-source open/closed state, so an outage costs one timeout
  rather than one per request
* ``service`` — :class:`CatalogService`, AniList with MAL behind it (FR-C6)
* ``factory`` — building one of those from settings. Deliberately *not*
  re-exported here: it imports both source implementations, and those import
  this package's ``source`` module, so exporting it would make importing
  :mod:`arc.services.anilist` first a circular import. Import
  ``arc.services.catalog.factory`` directly.
* ``cache`` — ``anime`` / ``episodes`` upserts and :func:`ensure_anime`
* ``seasons`` — which season a date is in (FR-C7)
* ``lists`` — the list states (FR-C2, FR-W2)
* ``local`` — searching the cached rows, which a live search merges in front
  of its own page (FR-C1)
* ``jobs`` — the five ``catalog_*`` handlers
* ``names`` — the job type strings, importable without the handlers

Importing this package does **not** register the job handlers: the API has no
use for them and importing ``jobs`` from here would make every request path
drag the queue in. The worker imports :mod:`arc.services.catalog.jobs`
explicitly.
"""

from __future__ import annotations

from arc.services.catalog.breaker import Breaker, SourceState
from arc.services.catalog.cache import (
    DEFAULT_MAX_AGE,
    ensure_anime,
    episodes_for,
    preferred_title,
    sync_episodes,
    upsert_detail,
    upsert_summaries,
)
from arc.services.catalog.lists import (
    MAX_SCORE,
    MIN_SCORE,
    ListEntryError,
    StatusRequired,
    get_my_list,
    list_status_for,
    remove_list_entry,
    set_list_entry,
)
from arc.services.catalog.local import LOCAL_SEARCH_LIMIT, local_search
from arc.services.catalog.seasons import current_season, next_season, season_of
from arc.services.catalog.service import CATALOGUE_UNAVAILABLE, CatalogService
from arc.services.catalog.source import (
    AiringEntry,
    CatalogMedia,
    CatalogSource,
    MediaRelation,
    MediaTitle,
    SearchPage,
    SourceNotFound,
    SourceUnavailable,
)

__all__ = [
    "CATALOGUE_UNAVAILABLE",
    "DEFAULT_MAX_AGE",
    "LOCAL_SEARCH_LIMIT",
    "MAX_SCORE",
    "MIN_SCORE",
    "AiringEntry",
    "Breaker",
    "CatalogMedia",
    "CatalogService",
    "CatalogSource",
    "ListEntryError",
    "MediaRelation",
    "MediaTitle",
    "SearchPage",
    "SourceNotFound",
    "SourceState",
    "SourceUnavailable",
    "StatusRequired",
    "current_season",
    "ensure_anime",
    "episodes_for",
    "get_my_list",
    "list_status_for",
    "local_search",
    "next_season",
    "preferred_title",
    "remove_list_entry",
    "season_of",
    "set_list_entry",
    "sync_episodes",
    "upsert_detail",
    "upsert_summaries",
]
