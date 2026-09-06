"""Acquisition: from "somebody wants this" to "the bytes are on disk" (§5.1).

* ``states`` — the episode state machine (spec §6). Every write to
  ``episodes.state`` in the whole codebase goes through it.
* ``wants`` — :func:`~arc.services.acquisition.wants.compute_wants`, the
  reconciler behind FR-A1, FR-A2 and FR-W4.
* ``rules`` — the admin-editable ranking rules, read out of ``settings``.
* ``nyaa`` — the RSS search feed, the query builder, the filter and the
  ranker (FR-A3, FR-A4).
* ``qbit`` — the qBittorrent Web API client and the container↔host path
  mapping (FR-A5).
* ``reject`` — what an ignored download does to the episode it was for. Light
  enough for the review API to import, which is the point of it being here
  rather than in ``jobs``.
* ``jobs`` — the three handlers.
* ``names`` — the job type strings, importable without the handlers.

Importing this package does **not** register the job handlers, for the same
reason :mod:`arc.services.catalog` does not: the API has no use for them and
importing ``jobs`` from a request path would drag the Nyaa and qBittorrent
clients into it. The worker imports :mod:`arc.services.acquisition.jobs`
explicitly.
"""

from __future__ import annotations

from arc.services.acquisition.names import (
    COMPUTE_WANTS,
    POLL_QBIT,
    SEARCH_RELEASE,
    search_dedupe_key,
)
from arc.services.acquisition.rules import Rules, load_rules, look_ahead_n, override_key
from arc.services.acquisition.states import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    advance_to_matched,
    can_transition,
    transition,
)
from arc.services.acquisition.wants import (
    WantsResult,
    compute_wants,
    enqueue_compute_wants,
    window,
)

__all__ = [
    "COMPUTE_WANTS",
    "POLL_QBIT",
    "SEARCH_RELEASE",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "IllegalTransition",
    "Rules",
    "WantsResult",
    "advance_to_matched",
    "can_transition",
    "compute_wants",
    "enqueue_compute_wants",
    "load_rules",
    "look_ahead_n",
    "override_key",
    "search_dedupe_key",
    "transition",
    "window",
]
