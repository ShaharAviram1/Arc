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
from typing import Any

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
    "CandidateOut",
    "ConfirmRequest",
    "ParsedOut",
    "ReviewItem",
    "ReviewPage",
    "ReviewSummary",
    "relative_directory",
]
