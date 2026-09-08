"""The job names shared by the catalogue jobs and the API that enqueues them.

A handler-free module on purpose. :mod:`arc.services.catalog.jobs` imports
:mod:`arc.services.jobs.registry` and registers five handlers as a side effect
of being imported, which the API must not do (see the note in
``arc/services/catalog/__init__.py``). So the API cannot import a job type or
its dedupe key from there — and before this module existed it kept its own
copies, which is exactly how an endpoint and a worker end up disagreeing about
what "already queued" means.

Importing this costs nothing and registers nothing.
"""

from __future__ import annotations

#: The job type a refresh — manual or swept — is queued under.
REFRESH = "catalog_refresh"
#: Daily: refresh everything anybody follows, plus everything airing (FR-C5).
REFRESH_ALL = "catalog_refresh_all"
#: Hourly: refresh whatever is about to air or has just aired (FR-C5).
PRE_AIR = "catalog_pre_air"
#: Hourly: attach AniList ids to rows that arrived through MAL (FR-C6).
RECONCILE = "catalog_reconcile"
#: Daily: cache this season and the next so the schedule survives an outage
#: of both sources (FR-C7).
SEASON_SWEEP = "catalog_season_sweep"

#: Queue priority for the catalogue jobs that actually talk to AniList (lower
#: runs first; the default is 100). Well behind the default: every one of these
#: is a sweep or a cache refill on a timer, each holds a slot for several paced
#: upstream requests, and none of them is the reason anybody has the app open.
#: A show somebody opens is fetched inside the request through the cache, not
#: through this queue, so nothing a user is looking at waits on it.
#:
#: :data:`REFRESH_ALL` and :data:`PRE_AIR` deliberately keep the default: they
#: make no upstream call at all — they run one ``SELECT`` and enqueue the
#: spaced-out :data:`REFRESH` jobs that do — so holding them behind the work
#: they schedule would only delay the scheduling.
CATALOG_PRIORITY = 200


def dedupe_key(anime_id: int) -> str:
    """One queued refresh per show, however many people ask for it."""
    return f"{REFRESH}:{anime_id}"


__all__ = [
    "CATALOG_PRIORITY",
    "PRE_AIR",
    "RECONCILE",
    "REFRESH",
    "REFRESH_ALL",
    "SEASON_SWEEP",
    "dedupe_key",
]
