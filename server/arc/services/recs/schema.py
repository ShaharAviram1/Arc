"""The shape of an answer: JSON schema out, pydantic back in (FR-R4).

Two representations of one contract, and they are not redundant.

:data:`PICKS_SCHEMA` goes to the API as ``output_config.format`` and is what
guarantees the response is JSON with the right keys at all. Structured outputs
constrain *shape*, not *content*: the schema language available there has no
``minItems``/``maxItems``, no numeric bounds and no string lengths, so "3 to 5
picks" and "a real ``anime_id``" cannot be expressed in it. `additionalProperties`
is false everywhere, because a model that invents a ``cover_url`` field is a
model that thinks it is allowed to invent a show.

:class:`Picks` is the same contract enforced in Python, after the answer comes
back. It is what catches a well-formed response with two picks in it. Counting
and membership are :mod:`arc.services.recs.runs`' job — this module says only
what a pick *is*.

Note on the id: architecture.md §5.6 originally wrote ``anilist_id`` here.
Arc's catalogue moved to internal ids with AniList and MAL as fallbacks (M3b),
and a cached row is allowed to have no AniList id at all, so the pool numbers
its candidates by ``anime_id`` and the model answers in the same currency.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Minimum and maximum picks (FR-R3). Enforced in code, not in the schema:
#: ``minItems``/``maxItems`` are not part of the structured-output subset.
MIN_PICKS = 3
MAX_PICKS = 5

#: How long an argued case may be before it is obviously not 2–4 sentences.
#: A ceiling, not a target: also not expressible in the schema.
MAX_CASE_CHARS = 1200

#: The JSON schema sent as ``output_config.format``.
PICKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "anime_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "case": {"type": "string"},
                },
                "required": ["anime_id", "title", "case"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}


class Pick(BaseModel):
    """One recommendation: a candidate id, its title, and the argument."""

    model_config = ConfigDict(extra="forbid")

    anime_id: int
    title: str
    case: str = Field(min_length=1, max_length=MAX_CASE_CHARS)


class Picks(BaseModel):
    """The whole answer, as parsed from the model's single text block."""

    model_config = ConfigDict(extra="forbid")

    picks: list[Pick]


__all__ = ["MAX_CASE_CHARS", "MAX_PICKS", "MIN_PICKS", "PICKS_SCHEMA", "Pick", "Picks"]
