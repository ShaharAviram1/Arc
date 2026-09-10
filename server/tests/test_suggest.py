"""The match-suggestion prompt and its validator (FR-L5, §5.2).

Both halves are pure, so this file has no database and no model in it. What is
under test is the two things that would be wrong in production and invisible
in a live run: what the model is *told* (a candidate list it can answer in,
and the prior Arc has when it downloaded the file itself), and what is done
with what it says.

The validator's tests are the ones that matter. A suggestion is shown to a
person next to a confirm button, so an answer naming a show nobody proposed —
or an episode number the show does not have — is worse than no answer at all:
it is a plausible-looking wrong thing one click away from being applied.
"""

from __future__ import annotations

import pytest

from arc.config import Settings
from arc.services.library.parser import parse
from arc.services.library.suggest import (
    DEFAULT_CONFIDENCE,
    MAX_CANDIDATES,
    MAX_REASON_CHARS,
    SUGGESTION_SCHEMA,
    SYSTEM_PROMPT,
    ExpectedEpisode,
    SuggestCandidate,
    SuggestionInvalid,
    build_user_message,
    suggestions_enabled,
    validate,
)

FRIEREN_FILE = "[SubsPlease] Sousou no Frieren - 03 (1080p) [A1B2C3D4].mkv"

FRIEREN_S1 = SuggestCandidate(
    anime_id=11,
    romaji="Sousou no Frieren",
    english="Frieren: Beyond Journey's End",
    format="TV",
    episodes=28,
    season="FALL",
    season_year=2023,
    score=0.91,
    reasons=("title 0.98", "episode 3 ≤ 28"),
)
FRIEREN_S2 = SuggestCandidate(
    anime_id=12,
    romaji="Sousou no Frieren 2nd Season",
    format="TV",
    episodes=None,
    season="WINTER",
    season_year=2026,
    score=0.84,
)
DECOY = SuggestCandidate(
    anime_id=13, romaji="Frieren: Beyond Journey's End Recap", format="SPECIAL", episodes=1
)

SHORTLIST = [FRIEREN_S1, FRIEREN_S2, DECOY]


# --- The schema --------------------------------------------------------------


def test_the_nullables_are_spelled_as_anyof() -> None:
    """``nullable`` is not in the structured-output subset; ``anyOf`` is."""
    for field in ("anime_id", "episode_number"):
        assert SUGGESTION_SCHEMA["properties"][field] == {
            "anyOf": [{"type": "integer"}, {"type": "null"}]
        }
    assert "nullable" not in str(SUGGESTION_SCHEMA)


def test_every_field_is_required_and_nothing_else_is_allowed() -> None:
    """A model that invents a field is a model inventing an answer."""
    assert set(SUGGESTION_SCHEMA["required"]) == set(SUGGESTION_SCHEMA["properties"])
    assert SUGGESTION_SCHEMA["additionalProperties"] is False


def test_confidence_is_the_three_words_the_ui_renders() -> None:
    assert SUGGESTION_SCHEMA["properties"]["confidence"]["enum"] == ["high", "medium", "low"]


def test_the_system_prompt_says_the_answer_is_never_applied() -> None:
    """FR-L5, and the model should know it is being asked for a view."""
    assert "never applied" in SYSTEM_PROMPT
    assert "anime_id null" in SYSTEM_PROMPT


# --- The prompt --------------------------------------------------------------


def message_for(*candidates: SuggestCandidate, expected: ExpectedEpisode | None = None) -> str:
    return build_user_message(parse(FRIEREN_FILE), list(candidates), expected=expected)


def test_the_file_is_described_from_the_parse() -> None:
    text = message_for(*SHORTLIST)

    assert FRIEREN_FILE in text
    assert "parsed title: Sousou no Frieren" in text
    assert "parsed episode: 3" in text
    assert "release group: SubsPlease" in text
    assert "resolution: 1080p" in text


def test_candidates_are_numbered_and_carry_their_id() -> None:
    """The number is for the reader; the id is what the answer is given in."""
    text = message_for(*SHORTLIST)

    assert "1. anime_id=11" in text
    assert "2. anime_id=12" in text
    assert "3. anime_id=13" in text


def test_a_candidate_carries_both_titles_the_format_and_the_count() -> None:
    line = FRIEREN_S1.line(1)

    assert '"Sousou no Frieren" / "Frieren: Beyond Journey\'s End"' in line
    assert "TV" in line
    assert "28 episodes" in line
    assert "FALL 2023" in line
    assert "matcher score 0.91" in line
    assert "title 0.98" in line


def test_an_unknown_episode_count_says_so_rather_than_lying() -> None:
    assert "episode count unknown" in FRIEREN_S2.line(2)


def test_the_prior_is_included_when_arc_downloaded_the_file() -> None:
    text = message_for(
        *SHORTLIST,
        expected=ExpectedEpisode(anime_id=11, title="Sousou no Frieren", episode_number=3),
    )

    assert "Arc downloaded this file itself" in text
    assert "episode 3" in text
    assert "anime_id=11" in text


def test_there_is_no_prior_paragraph_for_a_manual_drop() -> None:
    assert "Arc downloaded" not in message_for(*SHORTLIST)


def test_the_candidate_list_is_capped() -> None:
    many = [
        SuggestCandidate(anime_id=100 + index, romaji=f"Show {index}")
        for index in range(MAX_CANDIDATES + 5)
    ]

    text = build_user_message(parse(FRIEREN_FILE), many)

    assert f"{MAX_CANDIDATES}. anime_id=" in text
    assert f"{MAX_CANDIDATES + 1}. anime_id=" not in text


# --- The validator -----------------------------------------------------------


def good(**overrides: object) -> dict[str, object]:
    return {
        "anime_id": 11,
        "episode_number": 3,
        "reason": "The filename names this exact title and 3 is within 28 episodes.",
        "confidence": "high",
        **overrides,
    }


def test_a_good_answer_survives_intact() -> None:
    suggestion = validate(good(), SHORTLIST)

    assert suggestion.anime_id == 11
    assert suggestion.episode_number == 3
    assert suggestion.confidence == "high"
    assert suggestion.reason.startswith("The filename names")


def test_none_of_these_is_a_real_answer() -> None:
    """A shortlist the model rejects is exactly what a reviewer needs to know."""
    suggestion = validate(good(anime_id=None, episode_number=None), SHORTLIST)

    assert suggestion.anime_id is None
    assert suggestion.episode_number is None
    assert suggestion.confidence == "high"


def test_an_id_that_was_not_offered_becomes_null() -> None:
    """The model chooses from the shortlist; it does not extend it (FR-L5)."""
    suggestion = validate(good(anime_id=999), SHORTLIST)

    assert suggestion.anime_id is None


def test_dropping_the_id_does_not_drop_the_rest() -> None:
    """The reason is still worth showing: it says what the model was thinking."""
    suggestion = validate(good(anime_id=999), SHORTLIST)

    assert suggestion.reason
    assert suggestion.confidence == "high"


@pytest.mark.parametrize("number", [0, -1, -100])
def test_an_episode_number_below_one_becomes_null(number: int) -> None:
    assert validate(good(episode_number=number), SHORTLIST).episode_number is None


def test_an_episode_number_past_the_shows_count_becomes_null() -> None:
    """Episode 60 of a 28-episode show is not an episode of that show."""
    assert validate(good(episode_number=60), SHORTLIST).episode_number is None


def test_the_bound_is_inclusive() -> None:
    assert validate(good(episode_number=28), SHORTLIST).episode_number == 28


def test_an_unknown_episode_count_bounds_nothing() -> None:
    """A show whose count the catalogue does not have cannot contradict one."""
    suggestion = validate(good(anime_id=12, episode_number=600), SHORTLIST)

    assert suggestion.anime_id == 12
    assert suggestion.episode_number == 600


def test_the_bound_follows_the_chosen_show_not_the_first_one() -> None:
    """13 has one episode; 11 has 28. The answer names 13."""
    assert validate(good(anime_id=13, episode_number=5), SHORTLIST).episode_number is None


def test_the_reason_is_trimmed_and_capped() -> None:
    suggestion = validate(good(reason="  " + "x" * (MAX_REASON_CHARS + 50) + "  "), SHORTLIST)

    assert len(suggestion.reason) == MAX_REASON_CHARS
    assert not suggestion.reason.startswith(" ")


def test_a_missing_reason_is_empty_rather_than_a_failure() -> None:
    answer = good()
    del answer["reason"]

    assert validate(answer, SHORTLIST).reason == ""


@pytest.mark.parametrize("value", ["HIGH", " Medium ", "low"])
def test_confidence_is_normalised(value: str) -> None:
    assert validate(good(confidence=value), SHORTLIST).confidence == value.strip().lower()


@pytest.mark.parametrize("value", ["very high", "", None, 3])
def test_an_unrecognised_confidence_is_the_lowest(value: object) -> None:
    """A model that could not name one of three words is not a confident one."""
    assert validate(good(confidence=value), SHORTLIST).confidence == DEFAULT_CONFIDENCE


@pytest.mark.parametrize("value", [True, "eleven", 11.5, [], {}])
def test_a_non_integer_id_becomes_null(value: object) -> None:
    """``True`` in particular: it is an ``int``, and anime 1 is a real row."""
    assert validate(good(anime_id=value), SHORTLIST).anime_id is None


def test_a_numeric_string_id_is_read() -> None:
    """Structured outputs make this unlikely; a lenient read costs nothing."""
    assert validate(good(anime_id="11"), SHORTLIST).anime_id == 11


@pytest.mark.parametrize("answer", ["not an object", ["a", "list"], 7, None])
def test_something_that_is_not_an_object_raises(answer: object) -> None:
    """There is nothing to correct, so the job stores an error instead."""
    with pytest.raises(SuggestionInvalid):
        validate(answer, SHORTLIST)  # type: ignore[arg-type]


def test_a_candidate_past_the_cap_cannot_be_chosen() -> None:
    """The prompt only offered the first eight, so only those are choosable."""
    many = [SuggestCandidate(anime_id=100 + index) for index in range(MAX_CANDIDATES + 2)]
    last = many[-1].anime_id

    assert validate(good(anime_id=last), many).anime_id is None
    assert validate(good(anime_id=many[0].anime_id), many).anime_id == many[0].anime_id


def test_the_stored_shape_is_the_four_fields() -> None:
    assert set(validate(good(), SHORTLIST).as_dict()) == {
        "anime_id",
        "episode_number",
        "reason",
        "confidence",
    }


# --- The feature switch ------------------------------------------------------


def test_suggestions_need_both_the_flag_and_a_provider(settings: Settings) -> None:
    """Either alone is a button that does nothing (FR-L5)."""
    base = settings.model_copy(update={"llm_match_suggestions": False, "gemini_api_key": None})

    assert suggestions_enabled(base) is False
    assert suggestions_enabled(base.model_copy(update={"llm_match_suggestions": True})) is False
    assert suggestions_enabled(base.model_copy(update={"gemini_api_key": "AIza-real"})) is False
    assert (
        suggestions_enabled(
            base.model_copy(update={"llm_match_suggestions": True, "gemini_api_key": "AIza-real"})
        )
        is True
    )
