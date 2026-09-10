"""Walking the model chain, and remembering what is spent (§5.6).

The chain exists because Gemini's free tier gives each model about twenty
requests a day for the whole deployment, so these tests are mostly about one
distinction: a 429 that means "too fast" and a 429 that means "that is your lot
for today" look identical apart from their body, and treating the second as the
first would burn a request on every page view for the rest of the day.

The other half is what must *not* advance the chain. A refusal and a schema
mismatch are answers, not outages; walking on would spend a paid fallback to be
told the same thing again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from arc.services.recs.base import (
    RecsFailed,
    RecsRefused,
    RecsResult,
    RecsUnavailable,
    is_retryable,
)
from arc.services.recs.chain import (
    QUOTA_RESET_HOUR_UTC,
    ChainEntry,
    RecsChain,
    is_daily_quota,
    next_quota_reset,
)
from arc.services.recs.schema import Pick, Picks

#: A Tuesday afternoon, well before the 08:00 UTC reset.
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

#: The real Gemini bodies, trimmed. The daily one is what a spent free tier
#: actually returns — the quota id and the ``limit: 20`` are the two signals.
DAILY_QUOTA_BODY = (
    "Error code: 429 - [{'error': {'code': 429, 'message': 'You exceeded your current "
    "quota... * Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
    "model: gemini-3.8-flash', 'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': "
    "'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': "
    "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'quotaValue': '20'}]}]}}]"
)

#: A per-minute throttle: same status, no per-day quota id, no limit of 20.
PER_MINUTE_BODY = (
    "Error code: 429 - [{'error': {'code': 429, 'message': 'Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_requests_per_minute, limit: 10', "
    "'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': "
    "'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}]}}]"
)

GEMINI_A = ChainEntry(provider="gemini", model="gemini-3.8-flash")
GEMINI_B = ChainEntry(provider="gemini", model="gemini-3.5-flash")
OPENROUTER = ChainEntry(provider="openrouter", model="google/gemini-2.5-flash", fallback=True)


def result_from(model: str, provider: str) -> RecsResult:
    picks = Picks(
        picks=[Pick(anime_id=i, title=f"S{i}", case="because Frieren") for i in (1, 2, 3)]
    )
    return RecsResult(picks=picks, model=model, usage={}, provider=provider)


class FakeBackend:
    """One entry's backend: answers, or raises whatever it was given."""

    def __init__(self, entry: ChainEntry, outcome: Any) -> None:
        self.entry = entry
        self.outcome = outcome
        self.calls = 0

    async def recommend(self, *, system: str, user: str, schema: dict[str, Any]) -> RecsResult:
        self.calls += 1
        outcome = self.outcome
        if isinstance(outcome, list):
            outcome = outcome[min(self.calls - 1, len(outcome) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return result_from(self.entry.model, self.entry.provider)


class FakeBackends:
    """A :class:`~arc.services.recs.chain.BackendBuilder` over a script."""

    def __init__(self, outcomes: dict[ChainEntry, Any]) -> None:
        self.outcomes = outcomes
        self.built: dict[tuple[str, str], FakeBackend] = {}
        self.closed = False

    def build(self, provider: str, model: str) -> FakeBackend:
        key = (provider, model)
        if key not in self.built:
            entry = next(e for e in self.outcomes if e.provider == provider and e.model == model)
            self.built[key] = FakeBackend(entry, self.outcomes[entry])
        return self.built[key]

    async def aclose(self) -> None:
        self.closed = True

    def calls(self, entry: ChainEntry) -> int:
        backend = self.built.get((entry.provider, entry.model))
        return backend.calls if backend else 0


class Clock:
    """A clock a test moves by hand."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def chain_of(outcomes: dict[ChainEntry, Any], *, clock: Clock | None = None) -> Any:
    backends = FakeBackends(outcomes)
    return (
        RecsChain(list(outcomes), backends=backends, now=clock or Clock()),
        backends,
    )


async def ask(chain: RecsChain) -> RecsResult:
    return await chain.recommend(system="s", user="u", schema={})


# --- Telling the two 429s apart ---------------------------------------------


def test_a_daily_quota_429_is_recognised() -> None:
    assert is_daily_quota(RecsUnavailable(DAILY_QUOTA_BODY)) is True


def test_a_per_minute_429_is_not_a_daily_quota() -> None:
    """Same status, same RESOURCE_EXHAUSTED — the quota id is the difference."""
    assert is_daily_quota(RecsUnavailable(PER_MINUTE_BODY)) is False


@pytest.mark.parametrize(
    "message",
    ["connection refused", "Error code: 500", "Error code: 429 - rate limited", ""],
)
def test_anything_unrecognised_is_treated_as_transient(message: str) -> None:
    """Conservative on purpose: a wrong guess costs a request, not a day."""
    assert is_daily_quota(RecsUnavailable(message)) is False


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 11, 4, 12, 0, tzinfo=UTC), datetime(2026, 11, 5, 8, 0, tzinfo=UTC)),
        (datetime(2026, 11, 4, 7, 59, tzinfo=UTC), datetime(2026, 11, 4, 8, 0, tzinfo=UTC)),
        # Exactly the reset instant is *this* reset, not tomorrow's: an entry
        # checked on the stroke of eight must not sit out another whole day.
        (datetime(2026, 11, 4, 8, 0, tzinfo=UTC), datetime(2026, 11, 4, 8, 0, tzinfo=UTC)),
    ],
)
def test_the_reset_is_the_next_eight_hundred_utc(at: datetime, expected: datetime) -> None:
    assert next_quota_reset(at) == expected
    assert expected.hour == QUOTA_RESET_HOUR_UTC


def test_a_spent_daily_quota_is_not_worth_retrying() -> None:
    """Observed live: the backend retried a 429 that could not possibly work,
    costing a second before the chain moved on anyway."""
    assert is_retryable(RecsUnavailable(DAILY_QUOTA_BODY)) is False
    # …while a per-minute one still is.
    assert is_retryable(RecsUnavailable(PER_MINUTE_BODY)) is True


# --- The walk ----------------------------------------------------------------


async def test_the_first_entry_answers_and_nothing_else_is_touched() -> None:
    chain, backends = chain_of({GEMINI_A: None, GEMINI_B: None, OPENROUTER: None})

    result = await ask(chain)

    assert result.model == "gemini-3.8-flash"
    assert result.provider == "gemini"
    assert backends.calls(GEMINI_B) == 0
    assert backends.calls(OPENROUTER) == 0


async def test_a_daily_quota_moves_on_and_marks_a_cooldown() -> None:
    clock = Clock()
    chain, backends = chain_of(
        {GEMINI_A: RecsUnavailable(DAILY_QUOTA_BODY), GEMINI_B: None}, clock=clock
    )

    result = await ask(chain)

    assert result.model == "gemini-3.5-flash"
    assert backends.calls(GEMINI_A) == 1
    status = {row["model"]: row for row in chain.status()}
    assert status["gemini-3.8-flash"]["available"] is False
    assert status["gemini-3.8-flash"]["cooldown_until"] == datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
    assert status["gemini-3.5-flash"]["available"] is True


async def test_a_cooldown_is_honoured_on_the_next_call() -> None:
    """The point of the cooldown: the spent model is not tried again today."""
    clock = Clock()
    chain, backends = chain_of(
        {GEMINI_A: RecsUnavailable(DAILY_QUOTA_BODY), GEMINI_B: None}, clock=clock
    )
    await ask(chain)

    await ask(chain)

    assert backends.calls(GEMINI_A) == 1  # not tried a second time
    assert backends.calls(GEMINI_B) == 2


async def test_a_cooldown_expires_after_the_reset() -> None:
    clock = Clock()
    chain, backends = chain_of(
        {GEMINI_A: [RecsUnavailable(DAILY_QUOTA_BODY), None], GEMINI_B: None}, clock=clock
    )
    await ask(chain)
    assert backends.calls(GEMINI_A) == 1

    clock.at = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
    result = await ask(chain)

    assert backends.calls(GEMINI_A) == 2
    assert result.model == "gemini-3.8-flash"
    assert chain.status()[0]["available"] is True


async def test_a_per_minute_429_moves_on_without_a_cooldown() -> None:
    """It will be over in a minute; a day-long cooldown would waste the model."""
    chain, backends = chain_of({GEMINI_A: RecsUnavailable(PER_MINUTE_BODY), GEMINI_B: None})

    result = await ask(chain)

    assert result.model == "gemini-3.5-flash"
    assert chain.status()[0]["available"] is True
    assert backends.calls(GEMINI_A) == 1


async def test_the_fallback_answers_when_every_gemini_model_is_spent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole reason the chain exists."""
    chain, backends = chain_of(
        {
            GEMINI_A: RecsUnavailable(DAILY_QUOTA_BODY),
            GEMINI_B: RecsUnavailable(DAILY_QUOTA_BODY),
            OPENROUTER: None,
        }
    )

    with caplog.at_level("INFO", logger="arc.services.recs.chain"):
        result = await ask(chain)

    assert result.provider == "openrouter"
    assert result.model == "google/gemini-2.5-flash"
    assert backends.calls(OPENROUTER) == 1
    assert any("served by fallback" in record.message for record in caplog.records)
    assert [row["available"] for row in chain.status()] == [False, False, True]


async def test_a_refusal_stops_the_chain_and_the_fallback_is_not_paid_for() -> None:
    """A refusal is a judgement about the request; every model would agree."""
    chain, backends = chain_of({GEMINI_A: RecsRefused("declined"), OPENROUTER: None})

    with pytest.raises(RecsRefused):
        await ask(chain)

    assert backends.calls(OPENROUTER) == 0


@pytest.mark.parametrize(
    "failure",
    [
        RecsFailed("the model's answer did not match the schema: x"),
        RecsFailed("truncated"),
        RecsFailed("empty"),
    ],
    ids=["schema", "truncated", "empty"],
)
async def test_an_unusable_answer_stops_the_chain(failure: Exception) -> None:
    """The backend already retried once; another model would do the same thing."""
    chain, backends = chain_of({GEMINI_A: failure, OPENROUTER: None})

    with pytest.raises(RecsFailed):
        await ask(chain)

    assert backends.calls(OPENROUTER) == 0


async def test_the_last_failure_is_what_surfaces_when_everything_fails() -> None:
    chain, _ = chain_of(
        {
            GEMINI_A: RecsUnavailable(DAILY_QUOTA_BODY),
            OPENROUTER: RecsUnavailable("openrouter is down"),
        }
    )

    with pytest.raises(RecsUnavailable, match="openrouter is down"):
        await ask(chain)


async def test_a_wholly_exhausted_chain_says_so() -> None:
    clock = Clock()
    chain, _ = chain_of({GEMINI_A: RecsUnavailable(DAILY_QUOTA_BODY)}, clock=clock)
    with pytest.raises(RecsUnavailable):
        await ask(chain)

    with pytest.raises(RecsUnavailable, match="on cooldown"):
        await ask(chain)


async def test_an_empty_chain_says_so() -> None:
    chain, _ = chain_of({})

    with pytest.raises(RecsUnavailable, match="no recommendation models are configured"):
        await ask(chain)


async def test_closing_closes_the_backends() -> None:
    chain, backends = chain_of({GEMINI_A: None})

    await chain.aclose()

    assert backends.closed is True
