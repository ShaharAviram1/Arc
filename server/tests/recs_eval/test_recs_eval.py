"""The prompt eval (roadmap M12): properties FR-R3 and FR-R4 imply.

Not a unit test of a function — a test of the *prompt*. Five frozen runs, each
a real user shape (a heavy scorer, someone who scores nothing, someone with a
mood, an almost-empty account, someone who drops a lot), each with a recorded
model answer. Four properties are asserted of every one:

1. **3 to 5 picks** survive validation (FR-R3).
2. **Every pick is in the pool** it was given (FR-R2, and the M12 DoD).
3. **No pick is something they have already watched** — the DoD's own words,
   "picks never include shows already on the list (except planned)".
4. **Every case names a show from their history** (FR-R3: "references the
   user's actual history").

Property 4 is the one a prompt change breaks quietly, and the reason this file
exists at all: a recommender that stops arguing from history still returns five
plausible shows, and nothing else in the suite would notice.

The same four properties are asserted against a *live* call by
:func:`test_a_live_run_satisfies_every_property`, which goes through whichever
backend ``RECS_PROVIDER`` names. It never runs in ``make test``: pytest's
``addopts`` carries ``-m "not live"``, which is a spending guard rather than a
preference — production is on Gemini's free tier, but Anthropic and OpenRouter
are one variable away and a developer must not be billed for running the suite.
``uv run pytest -m live tests/recs_eval`` replaces that filter and runs it;
without a key for the selected provider it skips.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from arc.config import Settings
from arc.services.recs import build_recs_model
from arc.services.recs.base import RecsFailed, RecsModel, RecsResult, RecsUnavailable
from arc.services.recs.pool import POOL_CAP
from arc.services.recs.prompt import SYSTEM_PROMPT, build_user_message
from arc.services.recs.runs import PICK_KIND, validate_picks
from arc.services.recs.schema import MAX_PICKS, MIN_PICKS, PICKS_SCHEMA
from tests.recs_eval.cases import EvalCase, all_cases, mentions_history

CASES = all_cases()

#: The eval is only as good as its fixtures; a silently empty directory would
#: turn every assertion below into a no-op.
MIN_CASES = 4

#: The fixture whose pool is the size a real run produces (:data:`POOL_CAP`).
#: The others are small on purpose — they are about one shape of history each —
#: but a live call against five candidates proves nothing about the prompt that
#: ships, where the model has to choose among forty and the message is an order
#: of magnitude longer.
LIVE_CASE = "full_pool"


def ids(cases: list[EvalCase]) -> list[str]:
    return [case.name for case in cases]


def test_there_are_enough_cases_to_be_an_eval() -> None:
    assert len(CASES) >= MIN_CASES


def test_one_case_has_a_realistically_sized_pool() -> None:
    """The live call uses it, and a five-candidate prompt would flatter it."""
    live = next(item for item in CASES if item.name == LIVE_CASE)

    assert len(live.candidates) == POOL_CAP


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_the_fixture_pool_already_excludes_what_they_have_watched(case: EvalCase) -> None:
    """Fixture integrity: the pool is what FR-R2 would have produced."""
    assert case.pool_ids.isdisjoint(case.excluded_ids)


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_between_three_and_five_picks_survive(case: EvalCase) -> None:
    kept = validate_picks(
        case.answer.picks, candidates=list(case.candidates), excluded=case.excluded_ids
    )

    assert MIN_PICKS <= len(kept) <= MAX_PICKS


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_validated_picks_are_tagged_as_picks(case: EvalCase) -> None:
    """``rec_runs.picks`` holds the model's picks and the deterministic
    continuations in one list, so every entry says which it is.

    The fixtures' ``answer`` deliberately carries no ``kind``: it is a recorded
    *model response*, and the model never emits one — the tag is added when the
    answer is validated on the way to storage.
    """
    kept = validate_picks(
        case.answer.picks, candidates=list(case.candidates), excluded=case.excluded_ids
    )

    assert {entry["kind"] for entry in kept} == {PICK_KIND}


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_every_pick_is_from_the_pool(case: EvalCase) -> None:
    assert {pick.anime_id for pick in case.answer.picks} <= case.pool_ids


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_no_pick_is_something_they_have_already_watched(case: EvalCase) -> None:
    """The milestone's definition of done, in one line."""
    assert {pick.anime_id for pick in case.answer.picks}.isdisjoint(case.excluded_ids)


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_every_case_references_their_history(case: EvalCase) -> None:
    titles = case.history.titles
    for pick in case.answer.picks:
        assert mentions_history(pick.case, titles), (
            f"{case.name}: the case for {pick.title!r} names nothing they have watched"
        )


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_every_case_is_a_few_sentences_rather_than_a_blurb(case: EvalCase) -> None:
    """FR-R3 asks for 2–4 sentences; this is the cheap check that it is prose."""
    for pick in case.answer.picks:
        sentences = [part for part in pick.case.replace("!", ".").split(".") if part.strip()]
        assert 2 <= len(sentences) <= 6, f"{case.name}: {pick.title!r} has {len(sentences)}"


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_the_message_the_model_would_see_carries_every_id(case: EvalCase) -> None:
    message = build_user_message(
        prompt=case.prompt, history=case.history, candidates=list(case.candidates)
    )

    for candidate in case.candidates:
        assert f"anime_id={candidate.anime_id}" in message
    if case.prompt:
        assert case.prompt in message


# --- The live call ----------------------------------------------------------


def live_settings() -> Settings:
    """Settings exactly as the app reads them.

    The default ``env_file`` is ``(".env", "../.env")``, so running pytest from
    ``server/`` finds the repository-root ``.env`` — the same file ``make dev``
    and ``make up`` use. That is deliberate: a live eval that read a different
    configuration from the running app would be testing something else.
    """
    return Settings()  # type: ignore[call-arg]


#: The free tier answers 503 "high demand" and 429 often enough that a single
#: attempt is not a signal about Arc's code. Retried with a widening pause;
#: the elapsed time asserted below measures only the attempt that answered.
LIVE_ATTEMPTS = 4
LIVE_BACKOFF_SECONDS = 15

#: The one :class:`RecsFailed` worth retrying here. A schema violation is a
#: real result about the prompt and must fail the test; a cut-off stream is
#: the free tier having a moment.
RETRYABLE = "truncated"


async def _with_retries(model: RecsModel, message: str) -> tuple[RecsResult, float]:
    """One recommendation, retrying a busy free tier rather than failing on it.

    The tier fails in two ways that say nothing about Arc's prompt: 429/503
    outright, and — less obviously — ending a stream after a single chunk,
    which reaches us as ``RecsFailed("truncated")``. This test measures the
    prompt, not Google's capacity, so both are retried with a widening pause.

    Returns the wall clock of **the attempt that answered**, not of the whole
    loop: the DoD's "under 30 s" is a promise about what a user waits for a
    recommendation, and folding this function's own backoff into it would be
    measuring the free tier's bad minute instead.
    """
    last: Exception | None = None
    for attempt in range(LIVE_ATTEMPTS):
        started = time.monotonic()
        try:
            result = await model.recommend(system=SYSTEM_PROMPT, user=message, schema=PICKS_SCHEMA)
            return result, time.monotonic() - started
        except RecsUnavailable as exc:
            last = exc
        except RecsFailed as exc:
            if RETRYABLE not in str(exc):
                raise
            last = exc
        if attempt < LIVE_ATTEMPTS - 1:
            await asyncio.sleep(LIVE_BACKOFF_SECONDS * (attempt + 1))
    assert last is not None
    pytest.skip(f"the provider never completed a request: {last}")


@pytest.mark.live
async def test_a_live_run_satisfies_every_property(capsys: pytest.CaptureFixture[str]) -> None:
    """The same four properties, against the real backend. Opt-in.

    Whichever backend ``RECS_PROVIDER`` names — the point of the switch is that
    this test does not care. Skipped when that provider has no key. Run it with
    ``uv run pytest -m live tests/recs_eval`` after changing the prompt, and
    paste the answer into a fixture if it is worth recording.

    Prints the model, the wall clock and the picks with ``-s``, because the
    thing a human has to judge here — whether the cases are actually grounded
    in the history — is not something an assertion can check.
    """
    settings = live_settings()
    model = build_recs_model(settings)
    if model is None:
        pytest.skip(
            f"RECS_PROVIDER={settings.recs_provider} has no key "
            f"({settings.recs_key_name.upper()} unset); the live eval is opt-in"
        )

    case = next(item for item in CASES if item.name == LIVE_CASE)
    message = build_user_message(
        prompt=case.prompt, history=case.history, candidates=list(case.candidates)
    )
    try:
        result, elapsed = await _with_retries(model, message)
    finally:
        closer = getattr(model, "aclose", None)
        if closer is not None:
            await closer()

    with capsys.disabled():
        print(
            f"\n[live] provider={result.provider or settings.recs_provider} "
            f"model={result.model} {elapsed:.1f}s"
        )
        # The chain's own view: which entries answered and which are spent.
        status = getattr(model, "status", None)
        if callable(status):
            for row in status():
                mark = "ok" if row["available"] else f"cooldown until {row['cooldown_until']}"
                print(f"[live] chain {row['provider']}/{row['model']}: {mark}")
        print(f"[live] usage={result.usage}")
        for pick in result.picks.picks:
            print(f"[live] {pick.anime_id} — {pick.title}\n         {pick.case}")

    kept = validate_picks(
        result.picks.picks, candidates=list(case.candidates), excluded=case.excluded_ids
    )
    assert MIN_PICKS <= len(kept) <= MAX_PICKS
    assert {pick.anime_id for pick in result.picks.picks} <= case.pool_ids
    assert {pick.anime_id for pick in result.picks.picks}.isdisjoint(case.excluded_ids)
    titles = case.history.titles
    for pick in result.picks.picks:
        assert mentions_history(pick.case, titles), pick.case
    # FR-R3's timing budget lives in the M12 DoD: "under 30 s".
    assert elapsed < 30.0
