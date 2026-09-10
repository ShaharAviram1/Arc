"""What every model backend has in common (§5.6).

The protocols, the four exceptions the router branches on, the result types,
the timing constants, and the functions that turn a model's text into
something typed. Both backends import from here and neither imports the other,
so "Gemini and Anthropic must agree" is a fact about this module rather than a
convention two files are trusted to keep.

**Two layers, and the split is what lets a second feature use the chain.**
:class:`JsonModel` is the general one: a system prompt, a user message and a
JSON schema in, a parsed JSON object out (:class:`JsonResult`). It knows
nothing about recommendations. :class:`RecsModel` is the recommendation-shaped
view of the same thing — :func:`recommend_via` is the whole of the adapter,
and every implementation's ``recommend`` is one line calling it. M13's match
suggestions (:mod:`arc.services.library.suggest`) ask for ``complete`` with
their own schema and get the chain's rotation, cooldowns, retries and refusal
handling for nothing.

The exceptions are the interesting part, because they are the API's error
codes wearing different hats:

* :class:`RecsUnavailable` — the provider could not be reached, was rate
  limited, or returned a 5xx (→ 502). Retryable, and :func:`with_retry` does.
* :class:`RecsFailed` — an answer arrived and could not be used: cut off, or
  not the schema (→ 502). Only the cut-off half is retryable.
* :class:`RecsRefused` — the model declined (→ 503). Never retried; asking a
  second time is asking the same question.
* :class:`RecsError` — the base, which the router catches nothing by.

**Retry policy.** One retry, in Arc rather than in either SDK, with the SDK's
own retries turned off (``MAX_RETRIES``). Two reasons to own it here: the
retryable set includes things no SDK knows are failures (a stream that ended
mid-JSON came back HTTP 200), and the budget has to be spent where the DoD can
see it — two attempts at :data:`TIMEOUT_SECONDS` plus the pause is ~41 s worst
case, against a typical 4–6 s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from arc.services.recs.schema import Picks

log = logging.getLogger(__name__)

#: Seconds for **one attempt**. Deliberately shorter than the 30 s the DoD
#: gives the whole page: a provider that has not started answering in twenty
#: seconds is having a bad minute, and a second attempt is worth more than a
#: longer first one.
TIMEOUT_SECONDS = 20.0

#: Retries **inside the SDK**. Zero: Arc does its own (:func:`with_retry`), and
#: two layers of retry multiply into a wait nobody budgeted for.
MAX_RETRIES = 0

#: Attempts Arc makes, and the pause between them. One retry, because the
#: failures worth retrying are transient by definition and a second one is a
#: signal rather than noise.
ATTEMPTS = 2
RETRY_PAUSE_SECONDS = 1.0

#: Markers of a *daily* quota in a 429 body, as Gemini writes them. Either the
#: quota id names a per-day metric, or the free-tier request metric appears with
#: the twenty-a-day limit. A per-minute 429 carries neither.
_PER_DAY = re.compile(r"PerDay", re.IGNORECASE)
_RESOURCE_EXHAUSTED = re.compile(r"RESOURCE_EXHAUSTED", re.IGNORECASE)
_FREE_TIER_DAILY = re.compile(
    r"generate_content_free_tier_requests.*?limit:\s*20", re.IGNORECASE | re.DOTALL
)

#: The substring that marks the one :class:`RecsFailed` worth another attempt.
#: A cut-off or empty stream is the provider stumbling; a schema mismatch is a
#: real answer that was wrong, and asking again just spends the budget.
RETRYABLE_FAILURES = ("truncated", "empty")

#: What the picks schema is registered under in ``response_format``. Arbitrary,
#: but it is echoed in provider errors, so it should read as what it is. Lives
#: here rather than in one backend because :func:`recommend_via` is what sends
#: it and it must reach both.
PICKS_SCHEMA_NAME = "recs_picks"


class RecsError(RuntimeError):
    """Base class for everything that can go wrong producing a run."""


class RecsUnavailable(RecsError):
    """The provider could not be reached, was rate-limited, or errored (→ 502)."""


class RecsRefused(RecsError):
    """The model declined the request (→ 503).

    Carries ``stop_details`` when the provider sent one; it names the refusal
    category, which is the only thing that makes one of these debuggable.
    """

    def __init__(self, message: str, *, stop_details: Any = None) -> None:
        super().__init__(message)
        self.stop_details = stop_details


class RecsFailed(RecsError):
    """An answer arrived and could not be used — truncated, or not valid (→ 502)."""


@dataclass(frozen=True, slots=True)
class JsonResult:
    """What a model returned to any schema-constrained question.

    ``data`` is the answer as a JSON object — parsed, but not yet checked
    against whatever the caller's schema promised, because that is the
    caller's vocabulary rather than the transport's.

    ``model`` is the id the *server* reported, which is not always the one that
    was asked for — Anthropic's server-side fallbacks substitute one.
    ``provider`` is filled in by the backend that made the call, so an answer
    served by the fallback provider can be recognised after the fact.
    """

    data: dict[str, Any]
    model: str
    usage: dict[str, Any]
    provider: str = ""


@dataclass(frozen=True, slots=True)
class RecsResult:
    """A :class:`JsonResult` read as recommendations (:func:`recommend_via`)."""

    picks: Picks
    model: str
    usage: dict[str, Any]
    provider: str = ""


class JsonModel(Protocol):
    """Anything that can answer a prompt in a given JSON schema.

    Deliberately narrow — three strings and a schema in, one parsed object
    out. It exists so the tests, the eval fixtures and any future backend can
    stand in for a provider without either side knowing, and so that a feature
    which is not recommendations (M13's match suggestions) can ask the same
    chain its own question.

    ``name`` is what the schema is registered under where the provider wants a
    name for it (``response_format.json_schema.name``); Anthropic's
    ``output_config.format`` has nowhere to put one and ignores it.
    """

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        name: str = PICKS_SCHEMA_NAME,
    ) -> JsonResult:  # pragma: no cover - protocol
        ...


class RecsModel(Protocol):
    """Anything that can turn a prompt into picks.

    The recommendation-shaped view of :class:`JsonModel`. Kept as its own
    protocol because the router, the runs orchestrator and the eval speak
    picks, not JSON, and because a test fake that only answers
    recommendations should not have to implement anything else.
    """

    async def recommend(
        self, *, system: str, user: str, schema: dict[str, Any]
    ) -> RecsResult:  # pragma: no cover - protocol
        ...


def parse_json(raw: str) -> dict[str, Any]:
    """A model's text as a JSON **object**, or :class:`RecsFailed`.

    Shared so that "what counts as a well-formed answer" is one decision. Both
    providers constrain the output with a JSON schema, so this should never
    fire; when it does, the message has to say *how* it was wrong, because the
    fix differs — a truncation is a budget, a schema mismatch is a prompt or a
    model. A top-level array or scalar is a schema mismatch like any other:
    every schema Arc sends has ``"type": "object"`` at its root.
    """
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecsFailed(f"the model's answer did not match the schema: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RecsFailed(
            f"the model's answer did not match the schema: expected an object, "
            f"got {type(loaded).__name__}"
        )
    return loaded


def picks_from(data: dict[str, Any]) -> Picks:
    """A parsed answer as validated :class:`Picks`, or :class:`RecsFailed`."""
    try:
        return Picks.model_validate(data)
    except ValidationError as exc:
        raise RecsFailed(f"the model's answer did not match the schema: {exc}") from exc


def parse_picks(raw: str) -> Picks:
    """A model's text as validated :class:`Picks`, or :class:`RecsFailed`."""
    return picks_from(parse_json(raw))


async def recommend_via(
    model: JsonModel, *, system: str, user: str, schema: dict[str, Any]
) -> RecsResult:
    """The whole of the adapter from :class:`JsonModel` to :class:`RecsModel`.

    Every ``recommend`` in this package is one line calling this, so the two
    layers cannot drift: a backend, the chain and a future third thing all
    turn an answer into picks the same way.

    The validation is deliberately **outside** the retry
    (:func:`with_retry`, which lives under ``complete``): a well-formed JSON
    object that is not the picks schema is a real answer that was wrong, and
    asking again spends quota to be told the same thing.
    """
    result = await model.complete(system=system, user=user, schema=schema, name=PICKS_SCHEMA_NAME)
    return RecsResult(
        picks=picks_from(result.data),
        model=result.model,
        usage=result.usage,
        provider=result.provider,
    )


def is_daily_quota(exc: Exception) -> bool:
    """Whether ``exc`` means "no more of this model until tomorrow".

    Read out of the error text rather than from a status code, because the
    status code cannot tell the two kinds of 429 apart — "too fast" and "that
    is your lot for today" are both 429. Conservative on purpose: anything it
    does not recognise is treated as transient, which costs one more request
    rather than a day of a model nobody tried.

    Lives here rather than in :mod:`arc.services.recs.chain` because two
    decisions turn on it — whether the backend retries, and whether the chain
    puts the entry on cooldown — and they must not disagree.
    """
    text = str(exc)
    if _RESOURCE_EXHAUSTED.search(text) and _PER_DAY.search(text):
        return True
    return bool(_FREE_TIER_DAILY.search(text))


def is_retryable(exc: Exception) -> bool:
    """Whether another attempt at ``exc`` is worth the wait.

    Everything :class:`RecsUnavailable` covers (unreachable, rate limited,
    5xx, timed out), plus the cut-off and empty streams that arrive as HTTP
    200. Never a refusal, and never a schema mismatch: both are answers.

    The exception is a spent daily quota. It is a :class:`RecsUnavailable` like
    any other, but retrying it is certain to fail — observed live, costing a
    second and a round trip before the chain moved to the next model anyway.
    """
    if isinstance(exc, RecsUnavailable):
        return not is_daily_quota(exc)
    if isinstance(exc, RecsFailed):
        return any(marker in str(exc) for marker in RETRYABLE_FAILURES)
    return False


async def with_retry[T](attempt: Callable[[], Awaitable[T]], *, provider: str) -> T:
    """Run ``attempt``, once more after a pause if it failed retryably.

    The second failure is raised as it is: by then the provider has said the
    same thing twice, and the router's 502 is the honest answer.

    Generic in what an attempt returns because it wraps ``complete`` now, and
    what that produces depends on the question being asked.
    """
    last: Exception | None = None
    for number in range(ATTEMPTS):
        try:
            return await attempt()
        except (RecsUnavailable, RecsFailed) as exc:
            if not is_retryable(exc) or number == ATTEMPTS - 1:
                raise
            last = exc
            log.warning(
                "recommendation attempt failed; retrying",
                extra={"provider": provider, "attempt": number + 1, "error": str(exc)[:200]},
            )
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
    raise RecsFailed(str(last))  # pragma: no cover - the loop always returns or raises


__all__ = [
    "ATTEMPTS",
    "MAX_RETRIES",
    "PICKS_SCHEMA_NAME",
    "RETRYABLE_FAILURES",
    "RETRY_PAUSE_SECONDS",
    "TIMEOUT_SECONDS",
    "JsonModel",
    "JsonResult",
    "RecsError",
    "RecsFailed",
    "RecsModel",
    "RecsRefused",
    "RecsResult",
    "RecsUnavailable",
    "is_daily_quota",
    "is_retryable",
    "parse_json",
    "parse_picks",
    "picks_from",
    "recommend_via",
    "with_retry",
]
