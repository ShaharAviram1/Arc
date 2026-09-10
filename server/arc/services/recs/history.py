"""What the user has watched, summarised for the prompt (FR-R3).

The whole point of FR-R3 is that a pick's argued case must "reference the
user's actual history", so the model has to be given a history worth
referencing — and one small enough to sit above forty synopses without
crowding them out. Five slices, capped:

* **top rated** — the taste signal. Score descending.
* **recently completed** — what the user is in the mood for *now*, which a
  score-ordered list from three years ago does not say.
* **watching** — with progress, so the model does not pitch a show as "start
  something new" against six half-finished ones.
* **dropped** — the negative signal. Small; five is enough to see a pattern.
* **planned** — titles only. Everything here is also in the candidate pool
  (FR-R2 excludes every list state *but* planned), and the model is told these
  are already on the list so it can prefer them: "you said you would watch
  this" is the most persuasive case there is.

A pure function over rows the caller already has. No queries, no clock, no
network: :func:`summarise` is given ``(anime, entry)`` pairs and returns a
:class:`History`, which makes both the prompt builder and the eval fixtures
straightforward to write.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from arc.models import Anime, ListEntry, ListStatus
from arc.services.catalog import preferred_title

#: Per-slice caps. Together they bound the history at 45 lines.
TOP_RATED_LIMIT = 10
COMPLETED_LIMIT = 10
WATCHING_LIMIT = 10
DROPPED_LIMIT = 5
PLANNED_LIMIT = 20


@dataclass(frozen=True, slots=True)
class HistoryItem:
    """One show on the user's list, as the prompt describes it."""

    anime_id: int
    title: str
    genres: tuple[str, ...]
    score: int | None
    progress: int
    episodes: int | None


@dataclass(frozen=True, slots=True)
class History:
    """One user's list, sliced for the prompt."""

    top_rated: tuple[HistoryItem, ...] = ()
    recently_completed: tuple[HistoryItem, ...] = ()
    watching: tuple[HistoryItem, ...] = ()
    dropped: tuple[HistoryItem, ...] = ()
    planned: tuple[str, ...] = ()

    @property
    def titles(self) -> frozenset[str]:
        """Every title named anywhere in the summary.

        The property the eval asserts on (FR-R3): a case that references the
        user's history must contain one of these.
        """
        return frozenset(
            [
                item.title
                for slice_ in (self.top_rated, self.recently_completed, self.watching, self.dropped)
                for item in slice_
            ]
            + list(self.planned)
        )

    @property
    def is_empty(self) -> bool:
        """True for an account that has watched nothing at all.

        A planned list on its own is not empty: "you planned these, start with
        this one" is a perfectly good recommendation.
        """
        return (
            not (self.top_rated or self.recently_completed or self.watching or self.dropped)
            and not self.planned
        )


def _item(anime: Anime, entry: ListEntry) -> HistoryItem:
    return HistoryItem(
        anime_id=anime.id,
        title=preferred_title(anime),
        genres=tuple(anime.genres or ()),
        score=entry.score,
        progress=entry.progress,
        episodes=anime.episodes,
    )


def summarise(rows: Sequence[tuple[Anime, ListEntry]]) -> History:
    """Slice one user's list into the five groups the prompt describes.

    ``rows`` is ``(anime, entry)`` in any order;
    :func:`arc.services.catalog.lists.get_my_list` returns them newest change
    first, which is the order "recently completed" and "watching" keep. Ties
    break on ``anime_id`` so the same list always produces the same summary.
    """
    by_status: dict[ListStatus, list[tuple[Anime, ListEntry]]] = {}
    for anime, entry in rows:
        by_status.setdefault(entry.status, []).append((anime, entry))

    completed = by_status.get(ListStatus.COMPLETED, [])
    scored = [(anime, entry) for anime, entry in completed if entry.score]
    top = sorted(scored, key=lambda pair: (-(pair[1].score or 0), pair[0].id))

    return History(
        top_rated=tuple(_item(a, e) for a, e in top[:TOP_RATED_LIMIT]),
        recently_completed=tuple(_item(a, e) for a, e in completed[:COMPLETED_LIMIT]),
        watching=tuple(
            _item(a, e) for a, e in by_status.get(ListStatus.WATCHING, [])[:WATCHING_LIMIT]
        ),
        dropped=tuple(
            _item(a, e) for a, e in by_status.get(ListStatus.DROPPED, [])[:DROPPED_LIMIT]
        ),
        planned=tuple(
            preferred_title(anime)
            for anime, _ in by_status.get(ListStatus.PLANNED, [])[:PLANNED_LIMIT]
        ),
    )


__all__ = [
    "COMPLETED_LIMIT",
    "DROPPED_LIMIT",
    "PLANNED_LIMIT",
    "TOP_RATED_LIMIT",
    "WATCHING_LIMIT",
    "History",
    "HistoryItem",
    "summarise",
]
