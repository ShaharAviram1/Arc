"""Gemini and OpenRouter, without either (§5.6).

The stream is a fake async iterator of hand-built chunks, which is enough to
exercise everything the endpoint can do to an answer. Two of these tests exist
because of behaviour observed on the live API rather than guessed at:

* ``test_a_length_finish_is_truncated_even_with_no_content`` — Gemini 3.x Flash
  spends ``max_tokens`` on reasoning before it writes anything, so a small
  budget produces ``finish_reason: "length"`` with ``content: None`` and zero
  completion tokens. That must be a clean :class:`RecsFailed`, not a crash on
  ``None``.
* ``test_usage_arrives_on_a_choiceless_final_chunk`` — usage comes last, in a
  chunk with an empty ``choices`` list.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from arc.config import Settings
from arc.services.recs.base import RecsFailed, RecsRefused, RecsUnavailable
from arc.services.recs.openai_compat import (
    MAX_TOKENS,
    OPENROUTER_TITLE,
    REASONING_EFFORT,
    SCHEMA_NAME,
    OpenAICompatRecsModel,
)
from arc.services.recs.schema import PICKS_SCHEMA

ANSWER = {
    "picks": [
        {"anime_id": 11, "title": "Mushishi", "case": "Because you gave Frieren a 10."},
        {"anime_id": 12, "title": "Dandadan", "case": "Because you are watching Kaiju No. 8."},
        {"anime_id": 13, "title": "Ping Pong", "case": "Because you completed Devilman."},
    ]
}


def chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    refusal: str | None = None,
    error: Any = None,
    model: str = "gemini-3.5-flash",
    usage: Any = None,
    choices: bool = True,
) -> SimpleNamespace:
    """One streamed chunk, shaped like the SDK's."""
    picked = (
        [
            SimpleNamespace(
                finish_reason=finish_reason,
                delta=SimpleNamespace(content=content, refusal=refusal),
            )
        ]
        if choices
        else []
    )
    return SimpleNamespace(model=model, choices=picked, usage=usage, error=error)


def stream_of(text: str, **kwargs: Any) -> list[SimpleNamespace]:
    """A well-formed stream: the text in three pieces, then a stop."""
    third = max(1, len(text) // 3)
    pieces = [text[i : i + third] for i in range(0, len(text), third)]
    return [chunk(content=piece) for piece in pieces] + [chunk(finish_reason="stop", **kwargs)]


class FakeStream:
    """An async iterator that is also a context manager, like the SDK's.

    ``exited`` is what proves the response is released even when iteration
    raises — the reason the production code says ``async with``.
    """

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.exited = False

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.exited = True

    def __aiter__(self) -> FakeStream:
        self._it = iter(self._chunks)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeCompletions:
    """Records the request, and hands back one scripted stream per call.

    ``results`` is a list so a test can script a first attempt that fails and a
    second that succeeds — which is the only way to see the retry.
    """

    def __init__(self, result: Any) -> None:
        self.results = result if isinstance(result, list) and _is_script(result) else [result]
        self.kwargs: dict[str, Any] = {}
        self.calls = 0
        self.streams: list[FakeStream] = []

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        outcome = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        stream = FakeStream(outcome)
        self.streams.append(stream)
        return stream


def _is_script(result: list[Any]) -> bool:
    """Whether ``result`` is a list of *attempts* rather than a list of chunks."""
    return all(isinstance(item, (list, Exception)) for item in result)


class FakeClient:
    """Just enough of ``AsyncOpenAI`` to record one call."""

    def __init__(self, result: Any) -> None:
        self.completions = FakeCompletions(result)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def model_over(
    result: Any, *, provider: str = "gemini", public_url: str = "https://arc.example.com"
) -> tuple[OpenAICompatRecsModel, FakeClient]:
    client = FakeClient(result)
    model = OpenAICompatRecsModel(
        api_key="key",
        model="gemini-3.5-flash",
        provider=provider,
        public_url=public_url,
        client=client,
    )
    return model, client


# --- The request ------------------------------------------------------------


async def test_the_request_carries_the_schema_and_streams() -> None:
    recs, client = model_over(stream_of(json.dumps(ANSWER)))

    await recs.recommend(system="SYS", user="USER", schema=PICKS_SCHEMA)

    sent = client.completions.kwargs
    assert sent["model"] == "gemini-3.5-flash"
    assert sent["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": SCHEMA_NAME, "schema": PICKS_SCHEMA, "strict": True},
    }
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    # Large enough that Gemini's reasoning does not eat the answer.
    assert sent["max_tokens"] == MAX_TOKENS
    assert MAX_TOKENS >= 6000


@pytest.mark.parametrize("provider", ["gemini", "openrouter"])
async def test_reasoning_effort_is_sent_to_both_providers(provider: str) -> None:
    """The portable spelling, so there is no Google-specific branch.

    Google's own ``thinking_config`` passthrough is rejected outright by
    ``gemini-2.5-flash``; ``reasoning_effort`` is accepted on 2.5, 3.5 and 3.8,
    and a provider that does not understand it ignores it.
    """
    recs, client = model_over(stream_of(json.dumps(ANSWER)), provider=provider)

    await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.completions.kwargs["reasoning_effort"] == REASONING_EFFORT
    assert "extra_body" not in client.completions.kwargs


async def test_gemini_gets_no_attribution_headers() -> None:
    recs, client = model_over(stream_of(json.dumps(ANSWER)), provider="gemini")

    await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.completions.kwargs["extra_headers"] is None


async def test_openrouter_gets_attribution_headers() -> None:
    recs, client = model_over(stream_of(json.dumps(ANSWER)), provider="openrouter")

    await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.completions.kwargs["extra_headers"] == {
        "HTTP-Referer": "https://arc.example.com",
        "X-Title": OPENROUTER_TITLE,
    }


# --- The answer -------------------------------------------------------------


async def test_a_good_answer_becomes_picks() -> None:
    recs, _ = model_over(stream_of(json.dumps(ANSWER)))

    result = await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert [pick.anime_id for pick in result.picks.picks] == [11, 12, 13]
    assert result.model == "gemini-3.5-flash"


async def test_usage_arrives_on_a_choiceless_final_chunk() -> None:
    chunks = stream_of(json.dumps(ANSWER))
    chunks.append(
        chunk(choices=False, usage=SimpleNamespace(prompt_tokens=4200, completion_tokens=900))
    )
    recs, _ = model_over(chunks)

    result = await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert result.usage == {"input_tokens": 4200, "output_tokens": 900, "fallback": False}


async def test_a_missing_usage_chunk_is_tolerated() -> None:
    """Not every gateway honours ``include_usage``."""
    recs, _ = model_over(stream_of(json.dumps(ANSWER)))

    result = await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert result.usage == {"fallback": False}


async def test_a_length_finish_is_truncated_even_with_no_content() -> None:
    """Gemini's reasoning shares the output budget; a small one returns nothing."""
    spent = SimpleNamespace(prompt_tokens=10, completion_tokens=0)
    recs, _ = model_over([chunk(finish_reason="length", usage=spent)])

    with pytest.raises(RecsFailed, match="truncated"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


async def test_a_content_filter_finish_is_a_refusal() -> None:
    recs, _ = model_over([chunk(finish_reason="content_filter")])

    with pytest.raises(RecsRefused) as raised:
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert raised.value.stop_details == "content_filter"


async def test_a_refusal_field_is_a_refusal() -> None:
    recs, _ = model_over([chunk(refusal="I can't help with that."), chunk(finish_reason="stop")])

    with pytest.raises(RecsRefused) as raised:
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert raised.value.stop_details == "I can't help with that."


async def test_a_clean_stop_with_no_content_is_empty() -> None:
    recs, _ = model_over([chunk(content="", finish_reason="stop")])

    with pytest.raises(RecsFailed, match="empty"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


@pytest.mark.parametrize(
    "text",
    ["not json", '{"picks": [{"anime_id": "eleven", "title": "M", "case": "c"}]}'],
)
async def test_an_answer_that_is_not_the_schema_fails(text: str) -> None:
    """A clean ``stop`` with bad JSON is the model's fault, and says so."""
    recs, _ = model_over(stream_of(text))

    with pytest.raises(RecsFailed, match="did not match the schema"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


async def test_half_written_json_without_a_stop_is_truncated_not_a_schema_error() -> None:
    """Observed on Gemini: the answer is cut off and ``length`` is never sent.

    The distinction matters to whoever reads the log — one is a budget to
    raise, the other is a prompt or a model to change.
    """
    cut = '{"picks": [{"anime_id": 11, "title": "Mushishi", "case": "Because you'
    recs, _ = model_over([chunk(content=cut)])

    with pytest.raises(RecsFailed, match="truncated"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


async def test_a_mid_stream_error_chunk_is_unavailable() -> None:
    """An OpenAI-compatible gateway can fail at HTTP 200, mid-stream.

    OpenRouter does this when an upstream model dies. Without this it would
    read as "the model wrote half a sentence" and become a schema error.
    """
    chunks = [chunk(content='{"picks": ['), chunk(error={"message": "upstream model died"})]
    recs, _ = model_over(chunks)

    with pytest.raises(RecsUnavailable, match="upstream model died"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


# --- The retry (base) -------------------------------------------------------


async def test_a_truncated_first_attempt_is_retried_and_can_succeed() -> None:
    recs, client = model_over([[chunk(finish_reason="length")], stream_of(json.dumps(ANSWER))])

    result = await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.completions.calls == 2
    assert len(result.picks.picks) == 3


async def test_two_truncated_attempts_raise() -> None:
    recs, client = model_over([[chunk(finish_reason="length")], [chunk(finish_reason="length")]])

    with pytest.raises(RecsFailed, match="truncated"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.completions.calls == 2


async def test_a_refusal_is_never_retried() -> None:
    """Asking a second time is asking the same question."""
    recs, client = model_over([[chunk(finish_reason="content_filter")], stream_of("{}")])

    with pytest.raises(RecsRefused):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.completions.calls == 1


async def test_a_schema_mismatch_is_never_retried() -> None:
    """A real answer that was wrong; a second one costs quota for nothing."""
    recs, client = model_over([stream_of('{"nope": 1}'), stream_of(json.dumps(ANSWER))])

    with pytest.raises(RecsFailed, match="did not match the schema"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.completions.calls == 1


async def test_an_unavailable_first_attempt_is_retried() -> None:
    recs, client = model_over([sdk_errors()[0], stream_of(json.dumps(ANSWER))])

    result = await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.completions.calls == 2
    assert len(result.picks.picks) == 3


# --- SDK errors -------------------------------------------------------------


def sdk_errors() -> list[Exception]:
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/")
    return [
        openai.RateLimitError("429", response=httpx.Response(429, request=request), body=None),
        openai.APITimeoutError(request=request),
        openai.APIConnectionError(message="no route", request=request),
        openai.APIStatusError("500", response=httpx.Response(500, request=request), body=None),
    ]


@pytest.mark.parametrize("error", sdk_errors(), ids=lambda e: type(e).__name__)
async def test_every_sdk_error_becomes_unavailable(error: Exception) -> None:
    """The same mapping as the Anthropic backend: the router has one 502 path.

    Caught as ``openai.APIError``, the base class, so a subclass this list does
    not name — ``APIResponseValidationError``, or something a future SDK adds —
    still reaches the router as a 502 rather than a 500.
    """
    recs, _ = model_over([error, error])

    with pytest.raises(RecsUnavailable):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


async def test_the_response_is_released_even_when_the_answer_is_unusable() -> None:
    """``async with``: an abandoned streaming response holds its connection."""
    recs, client = model_over([[chunk(finish_reason="content_filter")]])

    with pytest.raises(RecsRefused):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert [stream.exited for stream in client.completions.streams] == [True]


async def test_the_response_is_released_on_the_happy_path_too() -> None:
    recs, client = model_over(stream_of(json.dumps(ANSWER)))

    await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert [stream.exited for stream in client.completions.streams] == [True]


async def test_a_teardown_runtime_error_after_a_full_answer_is_not_a_failure() -> None:
    """httpx has been seen raising at stream close *after* the answer arrived.

    It is a teardown artefact, not a failed request, so the answer stands.
    """

    class Exploding(FakeStream):
        async def __aexit__(self, *_: object) -> None:
            self.exited = True
            raise RuntimeError("generator didn't stop after athrow()")

    recs, client = model_over(stream_of(json.dumps(ANSWER)))
    client.completions.results = [stream_of(json.dumps(ANSWER))]
    original = client.completions.create

    async def create(**kwargs: Any) -> Any:
        await original(**kwargs)
        return Exploding(stream_of(json.dumps(ANSWER)))

    client.completions.create = create  # type: ignore[method-assign]

    result = await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert len(result.picks.picks) == 3


async def test_closing_closes_the_client() -> None:
    recs, client = model_over(stream_of(json.dumps(ANSWER)))

    await recs.aclose()

    assert client.closed is True


# --- Settings the backend reads ----------------------------------------------
#
# The base-URL and key-per-provider rules are exercised in
# ``test_recs_factory.py``, where the thing that consumes them lives. What
# belongs here is the pair this module is written against.


def settings_with(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_the_default_chain_leads_with_the_model_this_backend_was_tuned_for() -> None:
    """3.5 first on measurement, not novelty (see the config docstring).

    The tail is extra daily quota rather than better answers, which is why the
    default is a list at all.
    """
    settings = settings_with()

    assert settings.recs_provider == "gemini"
    assert settings.recs_models[0] == "gemini-3.5-flash"
    assert len(settings.recs_models) > 1
