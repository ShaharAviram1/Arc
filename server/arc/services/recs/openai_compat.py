"""Gemini and OpenRouter, over one OpenAI-compatible client (§5.6).

Both speak the OpenAI chat-completions dialect, so both are one class. What
differs between them is two values — the base URL and whether to send
OpenRouter's attribution headers — and each is a constructor argument rather
than a branch inside the request.

Three things about this endpoint were established against the live API rather
than recalled, and each cost a debugging session:

1. **Reasoning tokens are drawn from ``max_tokens``.** A request with
   ``max_tokens: 50`` came back ``finish_reason: "length"``, ``content: None``,
   ``completion_tokens: 0`` — it spent the whole budget reasoning and had
   nothing left to say. At 6000 against the real forty-candidate prompt it did
   the same thing more subtly: 828 characters of JSON ending mid-string, and
   **without** reporting ``length``. Hence :data:`MAX_TOKENS` of 16000,
   ``reasoning_effort="low"``, and the rule in :func:`parse_stream` that
   unparseable JSON on a stream which never said ``stop`` is a truncation
   rather than a schema violation.
2. **``reasoning_effort`` is the portable spelling, and the only one that
   works across models.** Google's own ``thinking_config`` passthrough is both
   awkward (it goes under a body field itself named ``extra_body``; the
   obvious ``{"google": …}`` is a 400) and narrow: ``gemini-2.5-flash``
   rejects it with *"Thinking level is not supported for this model"*. The
   top-level ``reasoning_effort="low"`` is accepted on 2.5, 3.5 and 3.8, and
   an OpenAI-compatible provider that does not understand it ignores it — so
   it is sent for both providers and there is no Google-specific branch left.
3. **Usage arrives on a final, choice-less chunk** and only when
   ``stream_options.include_usage`` is set. It is requested, and its absence is
   tolerated: not every gateway sends it.

A fourth thing is not about Gemini: an OpenAI-compatible gateway can report a
mid-stream failure as an ``error`` field on a chunk, at HTTP 200, having
already sent a ``[DONE]``-shaped envelope. OpenRouter does this when an
upstream model dies mid-generation. :class:`Accumulated` watches for it, so
that surfaces as a 502 rather than as "the model wrote half a sentence".
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Literal

import openai
from openai import AsyncOpenAI

from arc.services.recs.base import (
    MAX_RETRIES,
    TIMEOUT_SECONDS,
    RecsFailed,
    RecsRefused,
    RecsResult,
    RecsUnavailable,
    parse_picks,
    with_retry,
)

log = logging.getLogger(__name__)

#: Output budget, shared with the reasoning that precedes the answer (see the
#: module docstring). Running out of it does not shorten the answer, it
#: destroys it, so this is generous and streaming makes it free until used.
MAX_TOKENS = 16000

#: Thinking depth, in the portable OpenAI-compatible spelling. ``low`` because
#: the hard part of this task is in the prompt — forty candidates, each
#: annotated with why it is there — not in the deliberation. Typed as a
#: ``Literal`` because the SDK's parameter is one, and a bare ``str`` makes
#: every overload of ``create`` fail to match.
REASONING_EFFORT: Literal["low"] = "low"

#: The name the schema is registered under in ``response_format``. Arbitrary,
#: but it is echoed in errors, so it should read as what it is.
SCHEMA_NAME = "recs_picks"

#: OpenRouter's documented attribution headers. Only sent where they mean
#: something.
OPENROUTER_TITLE = "Arc"

#: ``finish_reason`` values that are a policy decline rather than an answer.
REFUSAL_REASONS = frozenset({"content_filter"})


def _headers(provider: str, *, public_url: str) -> dict[str, str] | None:
    """The provider-specific headers, if it has any."""
    if provider == "openrouter":
        return {"HTTP-Referer": public_url, "X-Title": OPENROUTER_TITLE}
    return None


class Accumulated:
    """What one streamed completion adds up to.

    A small mutable object rather than a tuple of locals because the chunk loop
    sets each field at a different moment: content arrives in pieces,
    ``finish_reason`` on the last chunk that carries one, ``usage`` on a final
    chunk with no choices at all, a refusal on either a delta or the assembled
    message, and an ``error`` whenever a gateway gives up mid-stream.
    """

    def __init__(self) -> None:
        self.text: list[str] = []
        self.finish_reason: str | None = None
        self.refusal: str | None = None
        self.error: str | None = None
        self.model: str = ""
        self.usage: dict[str, Any] = {}

    def add(self, chunk: Any) -> None:
        if getattr(chunk, "model", None):
            self.model = str(chunk.model)
        # A gateway reporting failure at HTTP 200, mid-stream. The shape is not
        # in the OpenAI schema, so it is read defensively: a dict with a
        # ``message``, or anything else stringified.
        error = getattr(chunk, "error", None)
        if error:
            self.error = str(error.get("message", error) if isinstance(error, dict) else error)
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self.usage = {
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
            }
        for choice in getattr(chunk, "choices", None) or ():
            reason = getattr(choice, "finish_reason", None)
            if reason:
                self.finish_reason = str(reason)
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if content:
                self.text.append(str(content))
            refusal = getattr(delta, "refusal", None)
            if refusal:
                self.refusal = str(refusal)

    @property
    def content(self) -> str:
        return "".join(self.text).strip()


def parse_stream(state: Accumulated) -> RecsResult:
    """A finished stream as a :class:`RecsResult`, or the right exception.

    The outcomes, in the order they have to be checked: a gateway that failed
    mid-stream, a refusal (either signal), a budget that ran out, an answer
    that is not there, and an answer that is not the schema.
    """
    if state.error:
        raise RecsUnavailable(state.error)
    if state.refusal or state.finish_reason in REFUSAL_REASONS:
        raise RecsRefused(
            "the model declined this request",
            stop_details=state.refusal or state.finish_reason,
        )
    if state.finish_reason == "length":
        # Usually the reasoning ate the whole budget rather than the answer
        # being long. Either way it is unusable, and it is worth one retry.
        raise RecsFailed("truncated")
    if not state.content:
        raise RecsFailed("empty")

    try:
        picks = parse_picks(state.content)
    except RecsFailed:
        # A stream that never said "stop" and left JSON half-written was cut
        # off, whatever it blamed. Reclassified because the two need different
        # answers: one is a budget to raise (and is retryable), the other is a
        # prompt or a model to change. Gemini has been observed ending a
        # truncated answer without reporting ``length``, which is why this
        # cannot rely on ``finish_reason`` alone.
        if state.finish_reason != "stop":
            raise RecsFailed("truncated") from None
        raise

    return RecsResult(picks=picks, model=state.model, usage={**state.usage, "fallback": False})


class OpenAICompatRecsModel:
    """:class:`~arc.services.recs.base.RecsModel` over an OpenAI-compatible API.

    ``client`` is injectable so the tests can hand in a fake with the same
    surface; in production it is built here and never leaves.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        provider: str,
        base_url: str | None = None,
        public_url: str = "",
        client: Any | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self.model = model
        self.provider = provider
        self.max_tokens = max_tokens
        self.extra_headers = _headers(provider, public_url=public_url)
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )

    async def recommend(self, *, system: str, user: str, schema: dict[str, Any]) -> RecsResult:
        """One streamed, schema-constrained completion, with one retry (base)."""

        async def attempt() -> RecsResult:
            return await self._attempt(system=system, user=user, schema=schema)

        # Stamped here rather than in the parser: the parsers are tested
        # against hand-built payloads and have no idea who sent them.
        result = replace(await with_retry(attempt, provider=self.provider), provider=self.provider)
        log.info(
            "recommendation model answered",
            extra={
                "provider": self.provider,
                "model": result.model,
                "picks": len(result.picks.picks),
                **result.usage,
            },
        )
        return result

    async def _attempt(self, *, system: str, user: str, schema: dict[str, Any]) -> RecsResult:
        state = Accumulated()
        try:
            # ``async with`` so the response is released on every path out,
            # including an exception raised while iterating: an abandoned
            # streaming response holds its connection until the pool notices.
            async with await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=self.max_tokens,
                reasoning_effort=REASONING_EFFORT,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": SCHEMA_NAME, "schema": schema, "strict": True},
                },
                stream=True,
                stream_options={"include_usage": True},
                extra_headers=self.extra_headers,
            ) as stream:
                async for chunk in stream:
                    state.add(chunk)
        # The base class, not the four leaves: it also covers
        # APIResponseValidationError, and a new subclass in a future SDK
        # version should reach the router as a 502 rather than a 500.
        except openai.APIError as exc:
            raise RecsUnavailable(str(exc)) from exc
        except RuntimeError as exc:
            # httpx's async iteration has been seen raising "generator didn't
            # stop after athrow()" while *closing* a stream whose answer had
            # already arrived in full. It is a teardown artefact, not a failed
            # request — but it reaches us at the same place, so it is logged
            # and reclassified rather than escaping as a 500.
            log.debug(
                "stream teardown raised", extra={"provider": self.provider, "error": str(exc)}
            )
            if not state.content:
                raise RecsUnavailable(str(exc)) from exc

        return parse_stream(state)

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if it has one."""
        closer = getattr(self._client, "close", None)
        if callable(closer):
            await closer()


__all__ = [
    "MAX_TOKENS",
    "OPENROUTER_TITLE",
    "REASONING_EFFORT",
    "REFUSAL_REASONS",
    "SCHEMA_NAME",
    "Accumulated",
    "OpenAICompatRecsModel",
    "parse_stream",
]
