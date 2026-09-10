"""The Anthropic call, without Anthropic (§5.6).

Nothing here opens a socket. The stream helper is replaced by a fake with the
same surface — an async context manager whose ``get_final_message`` returns a
hand-built message — which is enough to exercise everything that can happen to
a response: the happy path, a refusal, a truncation, a well-formed answer that
is not a valid one, and each SDK error the router has to map.

The one test that *does* touch the real SDK is
:func:`test_the_installed_sdk_accepts_every_argument_the_call_makes`, and it
touches only its signature. It is the guard against the failure this module is
most likely to have: a version bump that quietly drops ``output_config`` or
``fallbacks`` and turns every recommendation into a 400.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

from arc.services.recs.base import RecsFailed, RecsRefused, RecsUnavailable
from arc.services.recs.claude import (
    EFFORT,
    FALLBACK_BETA,
    MAX_TOKENS,
    REQUIRED_STREAM_PARAMS,
    ClaudeRecsModel,
    parse_message,
    served_by_fallback,
    stream_parameters,
)
from arc.services.recs.schema import PICKS_SCHEMA

ANSWER = {
    "picks": [
        {"anime_id": 11, "title": "Mushishi", "case": "Because you gave Frieren a 10."},
        {"anime_id": 12, "title": "Dandadan", "case": "Because you are watching Kaiju No. 8."},
        {"anime_id": 13, "title": "Ping Pong", "case": "Because you completed Devilman."},
    ]
}


def message(
    *,
    text: str | None = None,
    stop_reason: str = "end_turn",
    stop_details: Any = None,
    model: str = "claude-opus-5",
    iterations: list[Any] | None = None,
    thinking_first: bool = True,
) -> SimpleNamespace:
    """A finished message, shaped like the SDK's.

    ``thinking_first`` is on by default because that is what an adaptive-thinking
    response actually looks like: the text block is not ``content[0]``.
    """
    content: list[Any] = []
    if thinking_first:
        content.append(SimpleNamespace(type="thinking", thinking=""))
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=SimpleNamespace(input_tokens=1200, output_tokens=400, iterations=iterations),
    )


class FakeStream:
    """The async context manager ``client.beta.messages.stream`` returns."""

    def __init__(self, result: Any) -> None:
        self._result = result

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_final_message(self) -> Any:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    """Just enough of ``AsyncAnthropic`` to record the calls.

    ``result`` may be a list of outcomes, one per attempt, so a test can script
    a first attempt that fails and a second that succeeds.
    """

    def __init__(self, result: Any) -> None:
        self.results = result if isinstance(result, list) else [result]
        self.kwargs: dict[str, Any] = {}
        self.calls = 0
        self.closed = False
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs: Any) -> FakeStream:
        self.kwargs = kwargs
        outcome = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return FakeStream(outcome)

    async def close(self) -> None:
        self.closed = True


def model_over(result: Any) -> tuple[ClaudeRecsModel, FakeClient]:
    client = FakeClient(result)
    return ClaudeRecsModel(api_key="sk-ant-test", model="claude-opus-5", client=client), client


# --- The request ------------------------------------------------------------


async def test_the_request_is_the_one_the_architecture_specifies() -> None:
    recs, client = model_over(message(text=json.dumps(ANSWER)))

    await recs.recommend(system="SYS", user="USER", schema=PICKS_SCHEMA)

    assert client.kwargs["model"] == "claude-opus-5"
    # Adaptive, never budget_tokens: the old form is a 400 on Claude 5.
    assert client.kwargs["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(client.kwargs["thinking"])
    # Both halves matter: the schema shapes the answer, and the effort keeps
    # adaptive thinking from spending the budget the answer needs.
    assert client.kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": PICKS_SCHEMA},
        "effort": EFFORT,
    }
    assert client.kwargs["max_tokens"] == MAX_TOKENS >= 16000
    assert client.kwargs["fallbacks"] == "default"
    assert client.kwargs["betas"] == [FALLBACK_BETA]
    assert client.kwargs["system"] == "SYS"
    assert client.kwargs["messages"] == [{"role": "user", "content": "USER"}]


def test_the_installed_sdk_accepts_every_argument_the_call_makes() -> None:
    """A version bump that drops one of these is a failing test, not a 400."""
    assert REQUIRED_STREAM_PARAMS <= stream_parameters()


# --- The answer -------------------------------------------------------------


async def test_a_good_answer_becomes_picks() -> None:
    recs, _ = model_over(message(text=json.dumps(ANSWER)))

    result = await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert [pick.anime_id for pick in result.picks.picks] == [11, 12, 13]
    assert result.model == "claude-opus-5"
    assert result.usage == {"input_tokens": 1200, "output_tokens": 400, "fallback": False}


async def test_a_refusal_is_its_own_error_and_carries_the_details() -> None:
    details = SimpleNamespace(type="refusal", category="cyber", explanation="no")
    recs, _ = model_over(message(stop_reason="refusal", stop_details=details))

    with pytest.raises(RecsRefused) as raised:
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert raised.value.stop_details is details


async def test_a_truncated_answer_fails_rather_than_half_parsing() -> None:
    truncated = message(text='{"picks": [', stop_reason="max_tokens")
    recs, _ = model_over([truncated, truncated])

    with pytest.raises(RecsFailed, match="truncated"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        '{"picks": [{"anime_id": "eleven", "title": "M", "case": "c"}]}',
        '{"recommendations": []}',
    ],
)
async def test_an_answer_that_is_not_the_schema_fails(text: str) -> None:
    recs, _ = model_over(message(text=text))

    with pytest.raises(RecsFailed):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


async def test_a_response_with_no_text_block_fails() -> None:
    """Reported as "empty" so the shared retry policy recognises it."""
    recs, _ = model_over([message(text=None), message(text=None)])

    with pytest.raises(RecsFailed, match="empty"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


def test_the_text_block_is_found_past_the_thinking_block() -> None:
    result = parse_message(message(text=json.dumps(ANSWER), thinking_first=True))

    assert len(result.data["picks"]) == 3


# --- Fallbacks --------------------------------------------------------------


def test_a_fallback_that_served_the_answer_is_recorded() -> None:
    served = message(
        text=json.dumps(ANSWER),
        iterations=[SimpleNamespace(type="fallback_message")],
    )

    assert served_by_fallback(served) is True
    assert parse_message(served).usage["fallback"] is True


def test_no_iterations_is_not_a_fallback() -> None:
    assert served_by_fallback(message(text="{}", iterations=None)) is False
    assert served_by_fallback(SimpleNamespace()) is False


# --- SDK errors -------------------------------------------------------------


def sdk_errors() -> list[Exception]:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return [
        RateLimitError("429", response=httpx.Response(429, request=request), body=None),
        APITimeoutError(request=request),
        APIConnectionError(message="no route", request=request),
        APIStatusError("500", response=httpx.Response(500, request=request), body=None),
    ]


@pytest.mark.parametrize("error", sdk_errors(), ids=lambda e: type(e).__name__)
async def test_every_sdk_error_becomes_unavailable(error: Exception) -> None:
    """The router turns exactly one exception into a 502; this is where the
    SDK's become it.

    Caught as ``anthropic.APIError``, the base class, so a subclass this list
    does not name — ``APIResponseValidationError``, or something a future SDK
    adds — is a 502 rather than a 500.
    """
    recs, _ = model_over([error, error])

    with pytest.raises(RecsUnavailable):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)


# --- The retry (base) -------------------------------------------------------


async def test_a_truncated_first_attempt_is_retried_and_can_succeed() -> None:
    recs, client = model_over([message(stop_reason="max_tokens"), message(text=json.dumps(ANSWER))])

    result = await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.calls == 2
    assert len(result.picks.picks) == 3


async def test_a_refusal_is_never_retried() -> None:
    recs, client = model_over([message(stop_reason="refusal"), message(text=json.dumps(ANSWER))])

    with pytest.raises(RecsRefused):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.calls == 1


async def test_a_schema_mismatch_is_never_retried() -> None:
    recs, client = model_over([message(text='{"nope": 1}'), message(text=json.dumps(ANSWER))])

    with pytest.raises(RecsFailed, match="did not match the schema"):
        await recs.recommend(system="s", user="u", schema=PICKS_SCHEMA)

    assert client.calls == 1


async def test_closing_closes_the_client() -> None:
    recs, client = model_over(message(text=json.dumps(ANSWER)))

    await recs.aclose()

    assert client.closed is True
