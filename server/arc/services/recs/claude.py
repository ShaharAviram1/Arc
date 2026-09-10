"""The Anthropic backend (§5.6).

One of two implementations of :class:`~arc.services.recs.base.RecsModel`;
selected by ``RECS_PROVIDER=anthropic``. Production ships on Gemini
(:mod:`arc.services.recs.openai_compat`), so this is the switchable
alternative rather than the default — but it is the one whose call shape is
most easily got wrong from memory, so every part of it is spelled out:

* ``thinking={"type": "adaptive"}`` — the current form. The older
  ``{"type": "enabled", "budget_tokens": N}`` is **rejected with a 400** on
  Claude 5 models.
* ``output_config`` carries **both** the JSON schema and ``effort: "low"``.
  The effort matters as much as the schema: adaptive thinking draws from the
  same ``max_tokens`` the answer does, and at a small budget it eats the
  answer outright — the failure Gemini showed first and this backend shares.
  Low effort plus :data:`MAX_TOKENS` of 16000 leaves room for both.
* ``fallbacks="default"`` with the ``server-side-fallback-2026-07-01`` beta —
  a policy decline is re-run on a fallback model inside the same call, and the
  category routing is Anthropic's to maintain rather than ours. Whether a
  fallback served the answer is recorded in the usage we log.
* **streaming** — a long think over a forty-title prompt is a long-lived
  request, and a non-streaming one meets an idle timeout somewhere along the
  way.

Retries, timeouts and the exception hierarchy are
:mod:`arc.services.recs.base`'s, shared with the other backend.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import replace
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.resources.beta.messages.messages import AsyncMessages as AsyncBetaMessages

from arc.services.recs.base import (
    MAX_RETRIES,
    PICKS_SCHEMA_NAME,
    TIMEOUT_SECONDS,
    JsonResult,
    RecsFailed,
    RecsRefused,
    RecsResult,
    RecsUnavailable,
    parse_json,
    recommend_via,
    with_retry,
)

log = logging.getLogger(__name__)

#: The beta that enables the scalar ``fallbacks="default"`` form. Pairing this
#: header with the older array form (or that header with this form) is a 400.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: Output budget. Shared with adaptive thinking, which is why it is not the
#: 4000 an answer of five paragraphs would suggest: at that size the reasoning
#: consumes the budget and the answer is truncated or absent.
MAX_TOKENS = 16000

#: Thinking depth. The hard part of this task is in the prompt — forty
#: candidates, each already annotated with why it is there — not in the
#: deliberation, and every thinking token is one the answer does not get.
EFFORT = "low"

#: The keyword arguments the call depends on. Checked against the installed
#: SDK by :func:`stream_parameters` rather than assumed — every one of them is
#: newer than most of what has been written about this API.
REQUIRED_STREAM_PARAMS = frozenset({"output_config", "fallbacks", "betas", "thinking"})


def stream_parameters() -> frozenset[str]:
    """The keyword arguments the installed SDK's beta stream helper accepts.

    Read from the SDK rather than trusted, and asserted in the tests: if a
    version bump drops ``output_config`` or ``fallbacks``, that is a failing
    test here rather than a 400 on a user's first recommendation. (Verified
    against ``anthropic`` 1.4.0, which accepts all four of
    :data:`REQUIRED_STREAM_PARAMS`, so the non-streaming
    ``create(..., stream=True)`` fallback is not needed.)
    """
    return frozenset(inspect.signature(AsyncBetaMessages.stream).parameters)


def _text_block(message: Any) -> str:
    """The first text block of a response.

    ``output_config.format`` guarantees there is exactly one and that it holds
    valid JSON — but a thinking block precedes it, so this looks rather than
    indexes.
    """
    for block in message.content or ():
        if getattr(block, "type", None) == "text":
            return str(block.text)
    raise RecsFailed("empty")


def served_by_fallback(message: Any) -> bool:
    """Whether a fallback model answered instead of the one we asked for.

    Guarded at every step: ``usage.iterations`` is only present when something
    interesting happened, and a sticky follow-up turn carries no fallback
    block at all.
    """
    usage = getattr(message, "usage", None)
    iterations = getattr(usage, "iterations", None) or ()
    return any(getattr(item, "type", None) == "fallback_message" for item in iterations)


def _usage(message: Any) -> dict[str, Any]:
    usage = getattr(message, "usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "fallback": served_by_fallback(message),
    }


def parse_message(message: Any) -> JsonResult:
    """A finished message as a :class:`JsonResult`, or the right exception.

    Split out from the request so it can be tested against a hand-built
    message object, which is most of what can go wrong here.
    """
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        raise RecsRefused(
            "the model declined this request", stop_details=getattr(message, "stop_details", None)
        )
    if stop_reason == "max_tokens":
        raise RecsFailed("truncated")

    data = parse_json(_text_block(message))
    return JsonResult(
        data=data, model=str(getattr(message, "model", "") or ""), usage=_usage(message)
    )


class ClaudeRecsModel:
    """:class:`~arc.services.recs.base.RecsModel` over the Anthropic API.

    ``client`` is injectable so the tests can hand in a fake with the same
    surface; in production it is built here from the key and never leaves.
    """

    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: Any | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client or AsyncAnthropic(
            api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES
        )

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        name: str = PICKS_SCHEMA_NAME,
    ) -> JsonResult:
        """One streamed, schema-constrained call, with one retry (base).

        ``name`` is accepted and ignored: ``output_config.format`` carries the
        schema itself and has nowhere to put a name for it.
        """

        async def attempt() -> JsonResult:
            return await self._attempt(system=system, user=user, schema=schema)

        # Stamped here rather than in the parser: the parsers are tested
        # against hand-built payloads and have no idea who sent them.
        result = replace(await with_retry(attempt, provider=self.provider), provider=self.provider)
        log.info(
            "model answered",
            extra={
                "provider": self.provider,
                "model": result.model,
                "schema": name,
                **result.usage,
            },
        )
        return result

    async def recommend(self, *, system: str, user: str, schema: dict[str, Any]) -> RecsResult:
        """:meth:`complete`, read as picks (:func:`recommend_via`)."""
        return await recommend_via(self, system=system, user=user, schema=schema)

    async def _attempt(self, *, system: str, user: str, schema: dict[str, Any]) -> JsonResult:
        # Annotated ``Any`` because the SDK types this as a TypedDict and a
        # dict literal mixing a nested mapping with a string infers as
        # ``dict[str, Collection[str]]``, which does not match it.
        output_config: Any = {
            "format": {"type": "json_schema", "schema": schema},
            "effort": EFFORT,
        }
        try:
            async with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config=output_config,
                fallbacks="default",
                betas=[FALLBACK_BETA],
            ) as stream:
                message = await stream.get_final_message()
        # The base class, not the four leaves: it also covers
        # APIResponseValidationError, and a new subclass in a future SDK
        # version should reach the router as a 502 rather than a 500.
        except anthropic.APIError as exc:
            raise RecsUnavailable(str(exc)) from exc
        return parse_message(message)

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if it has one."""
        closer = getattr(self._client, "close", None)
        if callable(closer):
            await closer()


__all__ = [
    "EFFORT",
    "FALLBACK_BETA",
    "MAX_TOKENS",
    "REQUIRED_STREAM_PARAMS",
    "ClaudeRecsModel",
    "parse_message",
    "served_by_fallback",
    "stream_parameters",
]
