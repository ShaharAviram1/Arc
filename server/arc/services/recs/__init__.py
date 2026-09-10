"""Recommendations: candidate pool, history summary, prompt, model, runs.

Spec §4.8 (FR-R1 … FR-R5), architecture.md §5.6. The shape of the feature is
four small pieces and one orchestrator:

* ``pool`` — the ≤ 40 candidates a run may choose from (FR-R2). Everything the
  model is allowed to recommend is in here, and nothing on the user's list is,
  except ``planned``.
* ``history`` — what the user has actually watched, summarised for the prompt
  (FR-R3). A pure function over the rows the caller already loaded.
* ``prompt`` / ``schema`` — the system prompt, the user message, and the JSON
  schema the answer is constrained to (FR-R4).
* ``base`` — the protocol, the exception hierarchy, the timing constants, the
  shared answer parser and the one-retry policy. Both backends import it and
  neither imports the other.
* ``claude`` / ``openai_compat`` — the two backends (Anthropic, and one for
  every OpenAI-compatible provider). Tests inject fakes and reach neither.
* ``chain`` — several ``(provider, model)`` entries tried in order, with a
  cooldown for the ones whose daily free-tier quota is spent. It is itself a
  ``RecsModel``, so nothing above it knows there is more than one.
* ``factory`` — reads the configuration into a chain, sharing one HTTP client
  per provider; ``None`` when no entry has a key.
* ``runs`` — rate limit, build, call, validate, persist (FR-R5).

The non-negotiable of this milestone lives in ``runs.validate_picks``: a pick
the model invented, or one for a show the user has already watched, is dropped
rather than shown. The model chooses from the pool; it does not extend it.
"""

from __future__ import annotations

from arc.services.recs.base import (
    RecsError,
    RecsFailed,
    RecsModel,
    RecsRefused,
    RecsResult,
    RecsUnavailable,
)
from arc.services.recs.chain import ChainEntry, RecsChain
from arc.services.recs.claude import ClaudeRecsModel
from arc.services.recs.factory import build_recs_model, chain_entries
from arc.services.recs.history import History, HistoryItem, summarise
from arc.services.recs.openai_compat import OpenAICompatRecsModel
from arc.services.recs.pool import Candidate, build_pool
from arc.services.recs.prompt import SYSTEM_PROMPT, build_user_message
from arc.services.recs.runs import (
    DAILY_LIMIT,
    MAX_PICKS,
    MIN_PICKS,
    RecsEmptyPool,
    RecsRateLimited,
    remaining_today,
    run_recommendations,
    validate_picks,
)
from arc.services.recs.schema import PICKS_SCHEMA, Pick, Picks

__all__ = [
    "DAILY_LIMIT",
    "MAX_PICKS",
    "MIN_PICKS",
    "PICKS_SCHEMA",
    "SYSTEM_PROMPT",
    "Candidate",
    "ChainEntry",
    "ClaudeRecsModel",
    "History",
    "HistoryItem",
    "OpenAICompatRecsModel",
    "Pick",
    "Picks",
    "RecsEmptyPool",
    "RecsChain",
    "RecsError",
    "RecsFailed",
    "RecsModel",
    "RecsRateLimited",
    "RecsRefused",
    "RecsResult",
    "RecsUnavailable",
    "build_pool",
    "build_recs_model",
    "chain_entries",
    "build_user_message",
    "remaining_today",
    "run_recommendations",
    "summarise",
    "validate_picks",
]
