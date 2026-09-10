"""Loading a recommendation eval case from JSON.

A case is a whole run frozen on disk: one user's list, the candidate pool that
list would produce, the mood prompt they typed, and a **recorded** answer — a
real model response, kept so the properties FR-R3 and FR-R4 imply can be
asserted in CI without a key, a bill, or a network.

Recorded answers age. When the prompt changes materially, re-record them with
``pytest -m live`` against a real key and paste the new answers back in; the
properties asserted in :mod:`tests.recs_eval.test_recs_eval` are the contract
either way, and they are what a re-recorded answer has to keep satisfying.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc.models import Anime, ListEntry, ListStatus
from arc.services.recs.history import History, summarise
from arc.services.recs.pool import Candidate
from arc.services.recs.schema import Picks

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One frozen run: who, what they asked for, what they could be offered."""

    name: str
    prompt: str | None
    rows: tuple[tuple[Anime, ListEntry], ...]
    candidates: tuple[Candidate, ...]
    answer: Picks

    @property
    def history(self) -> History:
        return summarise(list(self.rows))

    @property
    def pool_ids(self) -> set[int]:
        return {candidate.anime_id for candidate in self.candidates}

    @property
    def excluded_ids(self) -> set[int]:
        """Everything the user has decided about — planned excepted (FR-R2)."""
        return {anime.id for anime, entry in self.rows if entry.status is not ListStatus.PLANNED}


def _row(raw: dict[str, Any]) -> tuple[Anime, ListEntry]:
    anime = Anime(
        id=raw["anime_id"],
        anilist_id=raw["anime_id"],
        summary_source="anilist",
        title_romaji=raw["title"],
        genres=raw.get("genres"),
        episodes=raw.get("episodes"),
        format=raw.get("format", "TV"),
    )
    entry = ListEntry(
        user_id=1,
        anime_id=raw["anime_id"],
        status=ListStatus(raw["status"]),
        score=raw.get("score"),
        progress=raw.get("progress", 0),
    )
    return anime, entry


def _candidate(raw: dict[str, Any]) -> Candidate:
    return Candidate(
        anime_id=raw["anime_id"],
        anilist_id=raw.get("anilist_id"),
        mal_id=raw.get("mal_id"),
        title=raw["title"],
        genres=tuple(raw.get("genres", ())),
        tags=tuple(raw.get("tags", ())),
        synopsis=raw.get("synopsis"),
        season=raw.get("season"),
        season_year=raw.get("season_year"),
        format=raw.get("format", "TV"),
        episodes=raw.get("episodes"),
        why=raw["why"],
    )


def load(path: Path) -> EvalCase:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return EvalCase(
        name=raw.get("name", path.stem),
        prompt=raw.get("prompt"),
        rows=tuple(_row(item) for item in raw["list"]),
        candidates=tuple(_candidate(item) for item in raw["pool"]),
        answer=Picks.model_validate(raw["answer"]),
    )


def all_cases() -> list[EvalCase]:
    """Every fixture, in filename order."""
    return [load(path) for path in sorted(FIXTURES.glob("*.json"))]


def mentions_history(case_text: str, titles: frozenset[str]) -> bool:
    """Whether an argued case names something the user has actually watched.

    Substring rather than token matching, deliberately: a case that writes
    *Frieren: Beyond Journey's End* for a list entry called *Frieren* has
    referenced their history, and an exact-match assertion would call that a
    failure. The check that matters is that a title is named at all.
    """
    return any(title and title.lower() in case_text.lower() for title in titles)


__all__ = ["FIXTURES", "EvalCase", "all_cases", "load", "mentions_history"]
