"""The TMDB job types, importable without their handlers.

Same reason :mod:`arc.services.catalog.names` exists: importing
:mod:`arc.services.tmdb.jobs` registers two handlers as a side effect, and the
one place that most wants the *name* is :mod:`arc.services.catalog.jobs`, which
enqueues an enrichment when a refresh leaves a followed show without key art.
A handler-free module is what keeps that from being a circular import.
"""

from __future__ import annotations

#: Enrich one show from TMDB. Payload ``{"anime_id": N}``, or
#: ``{"anime_id": N, "art_only": true}`` for the backdrop and the poster alone
#: — one request instead of three, which is what the season sweep and the Home
#: hero queue for a show nobody follows.
TMDB_ENRICH = "tmdb_enrich"

#: Nightly: find the followed shows that are still missing art, stills or
#: credits, then the current and next season's shows that are missing key art,
#: and queue one :data:`TMDB_ENRICH` each, spaced out.
TMDB_ENRICH_ALL = "tmdb_enrich_all"

#: Queue priority (lower runs first; the default is 100). Behind the catalogue
#: sweeps, which are themselves behind everything else: nothing in Arc waits on
#: a backdrop, and a night's worth of enrichment must never sit in front of the
#: refresh that keeps the schedule right.
TMDB_PRIORITY = 260


def dedupe_key(anime_id: int) -> str:
    """One queued enrichment per show, however many things ask for it."""
    return f"{TMDB_ENRICH}:{anime_id}"


__all__ = ["TMDB_ENRICH", "TMDB_ENRICH_ALL", "TMDB_PRIORITY", "dedupe_key"]
