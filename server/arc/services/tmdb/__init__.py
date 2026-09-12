"""TMDB: key art, episode stills and credits for what AniList has not filled.

* ``client`` — :class:`TmdbClient`, paced and breakered like the other two
  upstreams, reached by id only (Arc never searches TMDB)
* ``enrich`` — :func:`plan_enrichment`, the pure statement of what TMDB is
  allowed to write, and :func:`apply_enrichment`, which writes it
* ``jobs`` — the ``tmdb_enrich`` / ``tmdb_enrich_all`` handlers
* ``names`` — the job type strings, importable without the handlers

Importing this package does **not** register the job handlers, for the same
reason :mod:`arc.services.catalog` does not: the API has no use for them and
would drag the queue into every request path. The worker imports
:mod:`arc.services.tmdb.jobs` explicitly.
"""

from __future__ import annotations

from arc.services.tmdb.client import (
    TMDB_API_URL,
    TmdbClient,
    TmdbError,
    TmdbNotFound,
    TmdbUnavailable,
    image_url,
)
from arc.services.tmdb.enrich import (
    Enrichment,
    TmdbPayloads,
    apply_enrichment,
    plan_enrichment,
    resolve_season,
)
from arc.services.tmdb.names import TMDB_ENRICH, TMDB_ENRICH_ALL, TMDB_PRIORITY, dedupe_key

__all__ = [
    "TMDB_API_URL",
    "TMDB_ENRICH",
    "TMDB_ENRICH_ALL",
    "TMDB_PRIORITY",
    "Enrichment",
    "TmdbClient",
    "TmdbError",
    "TmdbNotFound",
    "TmdbPayloads",
    "TmdbUnavailable",
    "apply_enrichment",
    "dedupe_key",
    "image_url",
    "plan_enrichment",
    "resolve_season",
]
