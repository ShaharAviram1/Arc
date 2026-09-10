"""Asking a model which show an unsure file is (FR-L5, §5.2).

Everything here is pure: a prompt built out of a parse and some candidate
rows, and a validator that reads an answer back. No session, no clock, no
network — the job (:mod:`arc.services.library.jobs`) does the loading and the
calling, so the two halves that are worth testing are testable from literals.

**The suggestion is never applied.** That is the non-negotiable of this
milestone and it is enforced by what this module cannot do rather than by a
check somewhere: nothing in here links a file, and the job stores the answer
in ``media_files.llm_suggestion`` — a column the review UI renders and no
other code path reads. Linking stays with
:func:`arc.services.library.link.link`, whose two callers are the confident
matcher and a person pressing confirm.

**The model chooses, it does not extend.** The same rule the recommendations
run by: :func:`validate` drops an ``anime_id`` that was not among the
candidates the prompt offered, so a suggestion can only ever point at a title
the matcher already found. An invented id would be a show nobody could confirm
against and, worse, a plausible-looking one somebody might.

**Two answers are both fine.** "It is candidate 3, episode 7" and "none of
these" are equally useful in a review queue — the second says the matcher's
whole shortlist is wrong, which is exactly what a person needs to know before
reaching for the search box (FR-L6). So ``anime_id`` is nullable in the schema
and null is a real answer, not a failure.

Nullables are spelled ``anyOf: [{"type": …}, {"type": "null"}]``. The
structured-output subset both providers implement has no ``nullable`` keyword
and rejects the two-element ``"type"`` array form on at least one of them;
``anyOf`` is what is portable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from arc.config import Settings
from arc.core.config_check import model_chain_configured
from arc.services.library.parser import ParsedName

#: How many candidates the prompt may carry. The matcher stores at most
#: :data:`arc.services.library.matcher.MAX_CANDIDATES` anyway; the bound is
#: here so that a hand-written or migrated blob cannot turn one review item
#: into a very long prompt.
MAX_CANDIDATES = 8

#: Longest reason kept, in characters. A *one-line* reason is what FR-L5 asks
#: for and what the review UI has room for; anything past this is a model
#: writing an essay into a table cell, so it is cut rather than refused.
MAX_REASON_CHARS = 300

#: What the schema is registered under where the provider wants a name for it.
SCHEMA_NAME = "match_suggestion"

#: How sure the model says it is. Rendered as a badge next to the suggestion,
#: which is the whole reason it is asked for: "high" and "low" are the
#: difference between a person clicking confirm and a person reading the
#: filename themselves.
type Confidence = Literal["high", "medium", "low"]

#: The value an unrecognised confidence becomes. A model that could not name
#: one of three words is not a confident model.
DEFAULT_CONFIDENCE: Confidence = "low"

#: The JSON schema the answer is constrained to.
SUGGESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "anime_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "episode_number": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "reason": {"type": "string"},
        "confidence": {"type": "string", "enum": list(get_args(Confidence.__value__))},
    },
    "required": ["anime_id", "episode_number", "reason", "confidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are helping an anime library server decide which catalogue entry a video \
file belongs to. You are given what a deterministic filename parser made of \
the file and a numbered shortlist of catalogue entries the matcher found. The \
matcher was not sure enough to link the file on its own, which is why you are \
being asked.

Rules:
- Choose at most one entry, and only from the numbered candidates. Answer with \
its anime_id, exactly as given. If none of them is the show, answer with \
anime_id null — that is a useful answer, not a failure, and it is better than \
a guess.
- Give the episode number of that show the file most likely is, as \
episode_number. Season-numbered releases often restart at 1 for each season, \
so the number in the filename is usually the number within the chosen entry. \
Use null if you cannot tell. A film is episode 1.
- Give one short line saying why, in reason. Name the evidence you used — a \
title, a season marker, an episode count, the release group. No more than one \
sentence.
- Give confidence: "high" if the file plainly is that entry, "medium" if it is \
the best of the shortlist but something disagrees, "low" if you are guessing.
- Answer with JSON only, matching the schema. No prose outside it.

Your answer is shown to a person as a suggestion in a review queue. It is \
never applied automatically, so say what you actually think — including that \
you do not know."""


@dataclass(frozen=True, slots=True)
class SuggestCandidate:
    """One catalogue entry the model may choose, flattened for the prompt.

    Not an ``Anime`` row, for the reason
    :class:`arc.services.library.matcher.Candidate` is not one either: the
    prompt builder must be callable from a test with no database, and a
    dataclass of exactly the fields that go into the prompt is also the
    shortest statement of what the model is allowed to see.
    """

    anime_id: int
    romaji: str | None = None
    english: str | None = None
    format: str | None = None
    #: The catalogue's episode count, when it has one. Doubles as the upper
    #: bound :func:`validate` holds the answer's ``episode_number`` to.
    episodes: int | None = None
    season: str | None = None
    season_year: int | None = None
    #: The matcher's own score for this candidate, 0..1, and the reasons
    #: behind it. Included so the model can disagree with the matcher for a
    #: stated reason rather than from nothing.
    score: float | None = None
    reasons: tuple[str, ...] = ()

    @property
    def titles(self) -> str:
        """The candidate's names as one quoted phrase for the prompt."""
        names = [name for name in (self.romaji, self.english) if name]
        return " / ".join(f'"{name}"' for name in names) or "(untitled)"

    def line(self, number: int) -> str:
        """One numbered line of the candidate list."""
        facts = [
            self.format or "unknown format",
            f"{self.episodes} episodes" if self.episodes else "episode count unknown",
        ]
        if self.season or self.season_year:
            facts.append(" ".join(str(part) for part in (self.season, self.season_year) if part))
        if self.score is not None:
            facts.append(f"matcher score {self.score:.2f}")
        line = f"{number}. anime_id={self.anime_id} — {self.titles} — " + "; ".join(facts)
        if self.reasons:
            line += f"\n   matcher notes: {', '.join(self.reasons)}"
        return line


@dataclass(frozen=True, slots=True)
class ExpectedEpisode:
    """What Arc believed it was downloading, when it downloaded this file.

    The prior FR-L3 gives the matcher, restated for the model. It is evidence
    no filename carries, and it is the difference between "this could be any
    of three Frieren entries" and "Arc asked Nyaa for episode 3 of this one".
    """

    anime_id: int
    title: str
    episode_number: int


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A validated answer, ready to be stored and shown."""

    anime_id: int | None
    episode_number: int | None
    reason: str
    confidence: Confidence

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe mapping for ``media_files.llm_suggestion``.

        The provenance fields (``model``, ``provider``, ``created_at``) are
        added by the job, which is the half that knows who answered and when.
        """
        return {
            "anime_id": self.anime_id,
            "episode_number": self.episode_number,
            "reason": self.reason,
            "confidence": self.confidence,
        }


class SuggestionInvalid(ValueError):
    """The answer was not shaped like an answer at all.

    Distinct from "the answer named a show that was not offered", which is a
    *valid* answer that :func:`validate` corrects to null. This one means the
    payload was not an object, and there is nothing to correct.
    """


def suggestions_enabled(settings: Settings) -> bool:
    """Whether a suggestion could actually be produced right now (FR-L5).

    Two conditions, and both are needed. ``LLM_MATCH_SUGGESTIONS`` is the
    operator saying the feature should be on; a configured chain is whether
    anything can answer. Either alone is a button that does nothing.
    """
    return settings.llm_match_suggestions and model_chain_configured(settings)


def build_user_message(
    parsed: ParsedName,
    candidates: Sequence[SuggestCandidate],
    *,
    expected: ExpectedEpisode | None = None,
) -> str:
    """The user half of the prompt: the file, the prior, and the shortlist.

    The candidates are numbered for readability and carry their ``anime_id``
    on the same line, because that is what the answer is given in — a model
    that answers "3" when it means candidate 3 is a failure mode worth
    designing out rather than parsing around.
    """
    lines = [
        "File to identify:",
        f"  filename: {parsed.raw}",
        f"  parsed title: {parsed.title or '(none)'}",
        f"  parsed episode: {_or_none(parsed.episode)}",
        f"  parsed season: {_or_none(parsed.season)}",
        f"  release group: {parsed.group or '(none)'}",
        f"  resolution: {parsed.resolution or '(none)'}",
        f"  parser's guess at what kind of file it is: {parsed.kind}",
    ]
    if expected is not None:
        lines += [
            "",
            (
                "Arc downloaded this file itself, having asked for episode "
                f'{expected.episode_number} of "{expected.title}" (anime_id='
                f"{expected.anime_id}). That is a strong prior, but the release "
                "may still be mislabelled or the wrong file."
            ),
        ]
    lines += ["", "Candidates:"]
    lines += [
        candidate.line(number)
        for number, candidate in enumerate(candidates[:MAX_CANDIDATES], start=1)
    ]
    return "\n".join(lines)


def _or_none(value: object) -> str:
    return "(none)" if value is None else str(value)


def validate(answer: Mapping[str, Any], candidates: Sequence[SuggestCandidate]) -> Suggestion:
    """An answer, corrected to something that can safely be shown.

    Four corrections, and every one of them is "the model said something the
    catalogue disagrees with, so drop that part rather than the whole answer":

    * an ``anime_id`` that was not on the shortlist becomes null — the model
      may only choose from what it was offered (FR-L5, and CLAUDE.md's "LLM
      suggestions are shown, never applied" has less to say if the suggestion
      names a show nobody proposed);
    * an ``episode_number`` below 1, or above the chosen show's episode count
      where the catalogue knows it, becomes null;
    * a ``reason`` is trimmed and cut to :data:`MAX_REASON_CHARS`;
    * a ``confidence`` outside the three words becomes
      :data:`DEFAULT_CONFIDENCE`.

    Only a payload that is not a mapping raises: there is nothing to correct.
    """
    if not isinstance(answer, Mapping):
        raise SuggestionInvalid(f"expected an object, got {type(answer).__name__}")

    by_id = {candidate.anime_id: candidate for candidate in candidates[:MAX_CANDIDATES]}
    anime_id = _as_int(answer.get("anime_id"))
    chosen = by_id.get(anime_id) if anime_id is not None else None
    if chosen is None:
        anime_id = None

    episode_number = _as_int(answer.get("episode_number"))
    if episode_number is not None and episode_number < 1:
        episode_number = None
    if (
        episode_number is not None
        and chosen is not None
        and chosen.episodes is not None
        and episode_number > chosen.episodes
    ):
        episode_number = None

    raw_reason = answer.get("reason")
    reason = str(raw_reason).strip()[:MAX_REASON_CHARS] if raw_reason is not None else ""

    raw_confidence = str(answer.get("confidence") or "").strip().lower()
    confidence: Confidence = (
        raw_confidence  # type: ignore[assignment]
        if raw_confidence in get_args(Confidence.__value__)
        else DEFAULT_CONFIDENCE
    )

    return Suggestion(
        anime_id=anime_id,
        episode_number=episode_number,
        reason=reason,
        confidence=confidence,
    )


def _as_int(value: object) -> int | None:
    """``value`` as an int, or ``None`` for anything that is not one.

    A bool is refused explicitly: ``True`` is an ``int`` in Python and an
    ``anime_id`` of 1 arrived at that way would be a real row.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


__all__ = [
    "DEFAULT_CONFIDENCE",
    "MAX_CANDIDATES",
    "MAX_REASON_CHARS",
    "SCHEMA_NAME",
    "SUGGESTION_SCHEMA",
    "SYSTEM_PROMPT",
    "Confidence",
    "ExpectedEpisode",
    "SuggestCandidate",
    "Suggestion",
    "SuggestionInvalid",
    "build_user_message",
    "suggestions_enabled",
    "validate",
]
