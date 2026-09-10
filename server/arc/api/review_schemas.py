"""Request and response shapes for the match-review queue (FR-L6).

Separate from :mod:`arc.api.anime_schemas` because these belong to one router
and are built from two tables plus a JSONB blob, which needs enough
constructor logic to be worth its own module.

One rule dominates: **no response ever carries an absolute path**. The queue
is visible to every signed-in user (spec §2 gives ordinary users review of
their own requested shows), and a path is a statement about the server's
filesystem, not about the file. ``ReviewItem`` therefore carries the basename
and the directory *relative to* ``DATA_DIR``, and the id is what every action
addresses — the same rule the media routes follow (spec §7: "paths are derived
from ids, never from user input").
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.api.anime_schemas import AnimeSummary
from arc.models import Anime, MediaFile, ReviewState


class ParsedOut(BaseModel):
    """The part of ``media_files.parsed`` the review UI renders.

    A projection, not the whole blob: the stored parse also holds the codec,
    the source, the checksum-bearing raw name and — when there is an ffprobe —
    a stream summary, none of which helps somebody decide which show this is.
    """

    title: str | None = None
    episode: int | None = None
    season: int | None = None
    group: str | None = None
    resolution: str | None = None
    kind: str | None = None

    @classmethod
    def from_blob(cls, raw: dict[str, Any] | None) -> ParsedOut:
        raw = raw or {}
        return cls(
            title=raw.get("title"),
            episode=raw.get("episode"),
            season=raw.get("season"),
            group=raw.get("group"),
            resolution=raw.get("resolution"),
            kind=raw.get("kind"),
        )


class CandidateOut(BaseModel):
    """One scored candidate, with the show it names resolved to a card."""

    anime: AnimeSummary | None = None
    episode_number: int | None = None
    score: float | None = None
    reasons: list[str] = Field(default_factory=list)
    #: True when the episode number came from the absolute-numbering rule.
    absolute: bool = False
    #: Set instead of ``anime`` when the matcher had nothing to offer, and the
    #: entry is a sentence rather than a candidate ("no good candidates").
    reason: str | None = None

    @classmethod
    def from_blob(cls, raw: dict[str, Any], shows: dict[int, Anime]) -> CandidateOut:
        anime_id = raw.get("anime_id")
        anime = shows.get(int(anime_id)) if anime_id is not None else None
        return cls(
            anime=AnimeSummary.from_anime(anime) if anime is not None else None,
            episode_number=raw.get("episode_number"),
            score=raw.get("score"),
            reasons=[str(item) for item in (raw.get("reasons") or [])],
            absolute=bool(raw.get("absolute")),
            reason=raw.get("reason"),
        )

    @staticmethod
    def anime_ids(candidates: list[dict[str, Any]] | None) -> set[int]:
        """The internal ids a candidate list names, for one bulk lookup."""
        found: set[int] = set()
        for raw in candidates or []:
            if isinstance(raw, dict) and raw.get("anime_id") is not None:
                found.add(int(raw["anime_id"]))
        return found


class SuggestionOut(BaseModel):
    """What a model made of an unsure file, if it was asked (FR-L5).

    **A suggestion, never a decision.** The client renders it next to the
    candidates as something a person may act on, and nothing in the API turns
    it into a link — confirming is the same
    ``POST /api/review/{id}/confirm`` it always was, with the same body,
    whether or not a suggestion exists.

    ``error`` is the other half of the contract: a file can have been asked
    about and got nothing back (the model declined, nothing was configured,
    the shortlist was empty). Then ``error`` is set, every other field may be
    null, and the client says "no suggestion: <error>" rather than showing an
    empty card. Every field is optional so that both shapes are the same type.

    **The two are never both set.** A failed re-ask does not destroy a good
    suggestion — the job files it under ``last_error`` beside the answer
    (:func:`arc.services.library.jobs._store_error`) — so a row that has an
    answer renders as the answer, and ``error`` is populated only when there
    is no answer to show. The client therefore never has to decide which of
    the two to believe.
    """

    #: The candidate the model chose. Null when it said none of them fits —
    #: which is a real answer — and null on the error shape.
    anime_id: int | None = None
    #: ``anime_id`` resolved to a card, when Arc still has the row.
    anime: AnimeSummary | None = None
    episode_number: int | None = None
    #: One line, already trimmed and capped server-side.
    reason: str | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    #: The model id the provider reported, for the "suggested by" line.
    model: str | None = None
    created_at: datetime | None = None
    #: Set instead of the rest when there is no suggestion to show.
    error: str | None = None

    @classmethod
    def from_blob(cls, raw: dict[str, Any] | None, shows: dict[int, Anime]) -> SuggestionOut | None:
        """``media_files.llm_suggestion`` as a card, or ``None`` if unasked.

        Read defensively field by field rather than validated as a whole: the
        column is JSONB written by a job, and a row written by an older
        version of that job must render as much of itself as it can rather
        than 500 the whole queue.
        """
        if not raw:
            return None
        anime_id = raw.get("anime_id")
        anime = shows.get(int(anime_id)) if isinstance(anime_id, int) else None
        confidence = raw.get("confidence")
        # An answer and a failure are never reported together: a row that has
        # one keeps it through a failed re-ask, and the failure sits in
        # ``last_error``, which is a note about an attempt rather than
        # something the queue asks a person to act on.
        answered = confidence in CONFIDENCES
        return cls(
            anime_id=int(anime_id) if isinstance(anime_id, int) else None,
            anime=AnimeSummary.from_anime(anime) if anime is not None else None,
            episode_number=(
                int(raw["episode_number"]) if isinstance(raw.get("episode_number"), int) else None
            ),
            reason=str(raw["reason"]) if raw.get("reason") is not None else None,
            confidence=confidence if answered else None,
            model=str(raw["model"]) if raw.get("model") else None,
            created_at=_as_datetime(raw.get("created_at")),
            error=None if answered else (str(raw["error"]) if raw.get("error") else None),
        )

    @staticmethod
    def anime_ids(raw: dict[str, Any] | None) -> set[int]:
        """The internal id a suggestion names, for the same bulk lookup."""
        anime_id = (raw or {}).get("anime_id")
        return {int(anime_id)} if isinstance(anime_id, int) else set()


#: The three words :data:`arc.services.library.suggest.Confidence` allows.
#: Restated rather than imported so that this module stays a schema module —
#: importing the service would pull the model SDKs behind it into the API.
CONFIDENCES = frozenset({"high", "medium", "low"})


def _as_datetime(value: Any) -> datetime | None:
    """An ISO-8601 string from the job as a datetime, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:  # pragma: no cover - a hand-edited row
        return None


class ReviewItem(BaseModel):
    """One row of the match-review queue (FR-L4, FR-L6)."""

    id: int
    #: The filename alone. Never a path.
    name: str
    #: The directory holding it, relative to ``DATA_DIR`` — ``"manual"``,
    #: ``"downloads/Frieren"``, or ``""`` for a file directly in ``DATA_DIR``.
    #: Enough for a person to find it; not enough to say anything about the
    #: host's filesystem.
    directory: str
    size: int | None = None
    parsed: ParsedOut
    confidence: float | None = None
    candidates: list[CandidateOut] = Field(default_factory=list)
    review_state: ReviewState
    #: The episode this file is linked to, once it is. Null while pending.
    episode_id: int | None = None
    created_at: datetime
    #: What a model made of it (FR-L5). Null when nothing was asked — the
    #: feature is off, or the job has not run yet.
    suggestion: SuggestionOut | None = None

    @classmethod
    def build(cls, media_file: MediaFile, *, data_dir: Path, shows: dict[int, Anime]) -> ReviewItem:
        path = Path(media_file.path)
        return cls(
            id=media_file.id,
            name=path.name,
            directory=relative_directory(path, data_dir),
            size=media_file.size,
            parsed=ParsedOut.from_blob(media_file.parsed),
            confidence=media_file.match_confidence,
            candidates=[
                CandidateOut.from_blob(raw, shows)
                for raw in (media_file.match_candidates or [])
                if isinstance(raw, dict)
            ],
            review_state=media_file.review_state,
            episode_id=media_file.episode_id,
            created_at=media_file.created_at,
            suggestion=SuggestionOut.from_blob(media_file.llm_suggestion, shows),
        )


def relative_directory(path: Path, data_dir: Path) -> str:
    """``path``'s directory relative to ``data_dir``, or its basename.

    A file outside ``DATA_DIR`` — which should not happen, but a
    reconfiguration or a symlink can produce one — falls back to the parent's
    *name* rather than its path, so the invariant "no absolute path leaves the
    API" holds even when the assumption behind it does not.
    """
    parent = path.parent
    try:
        relative = parent.resolve().relative_to(data_dir.resolve())
    except ValueError, OSError:
        return parent.name
    text = str(relative)
    return "" if text == "." else text


class ReviewPage(BaseModel):
    """``GET /api/review``: the queue, and how much of it is pending."""

    items: list[ReviewItem]
    #: Always the *pending* count, whatever ``?state=`` asked for. The client
    #: sidebar renders this number and must not have it change because
    #: somebody filtered the list.
    pending: int
    #: Whether ``POST /api/review/{id}/suggest`` would do anything:
    #: ``LLM_MATCH_SUGGESTIONS`` is on **and** a model provider is configured
    #: (FR-L5). False means the client hides the "ask a model" button rather
    #: than offering one that answers 503.
    suggestions_enabled: bool = False


class SuggestJobOut(BaseModel):
    """``POST /api/review/{id}/suggest``: the job that will answer.

    202 rather than the suggestion itself: the call goes to a third party and
    takes seconds, and the queue is where work that talks to third parties
    belongs. The client polls the item (or the queue) for the suggestion to
    appear.
    """

    job_id: int
    #: Always ``"pending"``. It describes the *request* — accepted, queued —
    #: rather than the row, which may already be running if a suggestion for
    #: this file was queued a moment ago and the enqueue deduplicated onto it.
    status: Literal["pending"] = "pending"


class ReviewSummary(BaseModel):
    """``GET /api/review/summary``: the one number the sidebar polls."""

    pending: int


class ConfirmRequest(BaseModel):
    """The body of ``POST /api/review/{id}/confirm`` (FR-L6).

    ``anime_id`` is Arc's internal id, the same one every other route takes.
    ``episode_number`` is set by hand here: "set episode number manually" is
    half of what the review UI is for, so it is required rather than inferred
    from the candidate.
    """

    model_config = ConfigDict(extra="forbid")

    anime_id: int = Field(ge=1)
    episode_number: int = Field(ge=1)


__all__ = [
    "CONFIDENCES",
    "CandidateOut",
    "ConfirmRequest",
    "ParsedOut",
    "ReviewItem",
    "ReviewPage",
    "ReviewSummary",
    "SuggestJobOut",
    "SuggestionOut",
    "relative_directory",
]
