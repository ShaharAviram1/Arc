"""Fixtures shared by the M12 recommendation tests.

Two things live here. Unsaved ``Anime``/``ListEntry`` objects, so the pure
functions (history, prompt, validation) can be tested without a database — the
services take rows, not queries, precisely so this is possible. And
:class:`FakeModel`, a :class:`~arc.services.recs.claude.RecsModel` that answers
from a script: nothing in the suite except the ``live`` eval ever reaches
Anthropic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from arc.models import Anime, ListEntry, ListStatus
from arc.services.recs.claude import RecsResult
from arc.services.recs.schema import Pick, Picks

#: A fixed present, so "24 hours ago" is a number rather than a race.
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def anime(
    anime_id: int,
    title: str,
    *,
    anilist_id: int | None = None,
    mal_id: int | None = None,
    genres: list[str] | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
    format: str | None = "TV",
    episodes: int | None = 12,
    season: str | None = None,
    season_year: int | None = None,
    relations: list[dict[str, Any]] | None = None,
) -> Anime:
    """An unsaved ``anime`` row with the columns the recommender reads."""
    row = Anime(
        id=anime_id,
        anilist_id=anilist_id if anilist_id is not None else anime_id,
        mal_id=mal_id,
        summary_source="anilist",
        detail_source="anilist",
        title_romaji=title,
        format=format,
        episodes=episodes,
        season=season,
        season_year=season_year,
        genres=genres,
        description=description,
        relations=relations,
    )
    if tags is not None:
        row.tags = [{"name": name, "rank": 90 - index} for index, name in enumerate(tags)]
    return row


def entry(
    anime_id: int,
    status: ListStatus,
    *,
    user_id: int = 1,
    score: int | None = None,
    progress: int = 0,
    updated_at: datetime = NOW,
) -> ListEntry:
    """An unsaved ``list_entries`` row.

    ``updated_at`` is set explicitly because the column is defaulted by the
    database, and the relation seeds sort on it.
    """
    return ListEntry(
        user_id=user_id,
        anime_id=anime_id,
        status=status,
        score=score,
        progress=progress,
        updated_at=updated_at,
    )


class FakeModel:
    """A :class:`~arc.services.recs.claude.RecsModel` that answers from a script.

    Either returns ``picks`` (a list of ``(anime_id, title, case)``) or raises
    ``error``. Records the last system prompt and user message so a test can
    assert on what was actually sent.
    """

    def __init__(
        self,
        picks: list[tuple[int, str, str]] | None = None,
        *,
        error: Exception | None = None,
        model: str = "claude-opus-5",
    ) -> None:
        self.picks = picks or []
        self.error = error
        self.model = model
        self.calls = 0
        self.system: str | None = None
        self.user: str | None = None
        self.schema: dict[str, Any] | None = None

    async def recommend(self, *, system: str, user: str, schema: dict[str, Any]) -> RecsResult:
        self.calls += 1
        self.system = system
        self.user = user
        self.schema = schema
        if self.error is not None:
            raise self.error
        return RecsResult(
            picks=Picks(
                picks=[
                    Pick(anime_id=anime_id, title=title, case=case)
                    for anime_id, title, case in self.picks
                ]
            ),
            model=self.model,
            usage={"input_tokens": 100, "output_tokens": 50, "fallback": False},
        )


__all__ = ["NOW", "FakeModel", "anime", "entry"]
