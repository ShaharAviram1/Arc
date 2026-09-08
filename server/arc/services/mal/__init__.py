"""MyAnimeList.

Two halves that share a name and nothing else.

* ``catalog`` — the read-only catalogue fallback (FR-C6): :class:`MalSource`
  over MAL API v2, authenticated with nothing but the ``X-MAL-CLIENT-ID``
  header. It cannot write and takes no user's credentials.
* ``oauth``, ``client``, ``sync``, ``writelog``, ``jobs``, ``names`` — the
  per-user half (M9, spec §4.7): the PKCE handshake, the authenticated list
  client, the import and push rules, and the audit log.

Keeping the two apart matters more than it looks: the non-negotiable in
CLAUDE.md is that no code path writes to MAL except from a user-originated
event, and a module that physically cannot write is the cheapest way to keep
the catalogue fallback out of that argument. ``names`` is deliberately thin
and handler-free for the same reason — it is what the list service, the
playback service and the API import, and it is where the one function allowed
to queue the *job* lives. Its other half, the one allowed to write the queued
``mal_write_log`` row that job sends, is
:func:`arc.services.mal.writelog.record_pending`; both are pinned to the same
three callers by ``tests/test_mal_guard.py``.

Only ``catalog`` and ``names`` are re-exported here. The rest are imported by
module path (``from arc.services.mal import sync``): they pull in the
catalogue package and an HTTP client, which is not a cost a request path that
merely wants to name a job should pay.
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
from arc.services.mal.names import (
    IMPORT,
    IMPORT_ALL,
    PUSH,
    PUSH_ALL,
    enqueue_mal_import,
    enqueue_mal_push,
    enqueue_mal_push_all,
    is_linked,
)

__all__ = [
    "IMPORT",
    "IMPORT_ALL",
    "MAL_API_URL",
    "PUSH",
    "PUSH_ALL",
    "MalSource",
    "enqueue_mal_import",
    "enqueue_mal_push",
    "enqueue_mal_push_all",
    "is_linked",
    "parse_anime",
    "parse_broadcast",
    "parse_date",
    "synthesise_airing",
]
