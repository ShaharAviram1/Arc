"""Slicing a list into the history the prompt shows (FR-R3).

The interesting content is the caps and the ordering: what the model gets to
see decides what it can argue from, and a summary that silently reordered
itself would make two runs on the same list incomparable.
"""

from __future__ import annotations

from datetime import timedelta

from arc.models import ListStatus
from arc.services.recs.history import (
    COMPLETED_LIMIT,
    DROPPED_LIMIT,
    TOP_RATED_LIMIT,
    WATCHING_LIMIT,
    summarise,
)
from tests.recs_helpers import NOW, anime, entry


def test_an_empty_list_is_an_empty_history() -> None:
    history = summarise([])

    assert history.is_empty
    assert history.titles == frozenset()


def test_top_rated_is_scored_completed_shows_highest_first() -> None:
    rows = [
        (anime(1, "Frieren"), entry(1, ListStatus.COMPLETED, score=10)),
        (anime(2, "Mushishi"), entry(2, ListStatus.COMPLETED, score=8)),
        (anime(3, "Bleach"), entry(3, ListStatus.COMPLETED, score=9)),
        # Unscored: it is history, but it is not a *rating*.
        (anime(4, "Naruto"), entry(4, ListStatus.COMPLETED)),
    ]

    history = summarise(rows)

    assert [item.title for item in history.top_rated] == ["Frieren", "Bleach", "Mushishi"]
    # …and everything completed still shows up in the other slice.
    assert "Naruto" in {item.title for item in history.recently_completed}


def test_recently_completed_keeps_the_order_it_was_given() -> None:
    """``get_my_list`` returns newest change first; that is the whole ordering."""
    rows = [
        (anime(i, f"Show {i}"), entry(i, ListStatus.COMPLETED, updated_at=NOW - timedelta(days=i)))
        for i in range(1, 4)
    ]

    assert [item.title for item in summarise(rows).recently_completed] == [
        "Show 1",
        "Show 2",
        "Show 3",
    ]


def test_watching_carries_progress_and_dropped_is_separate() -> None:
    rows = [
        (anime(1, "Frieren", episodes=28), entry(1, ListStatus.WATCHING, progress=12)),
        (anime(2, "Bleach"), entry(2, ListStatus.DROPPED, progress=3)),
    ]

    history = summarise(rows)

    assert [(i.title, i.progress, i.episodes) for i in history.watching] == [("Frieren", 12, 28)]
    assert [(i.title, i.progress) for i in history.dropped] == [("Bleach", 3)]


def test_planned_is_titles_only() -> None:
    """They are also in the candidate pool; the model only needs to know they
    are already on the list (FR-R2, FR-R3)."""
    rows = [(anime(1, "Mushishi"), entry(1, ListStatus.PLANNED))]

    history = summarise(rows)

    assert history.planned == ("Mushishi",)
    assert history.watching == ()
    # A plan-to-watch list is not "watched nothing": it is a whole page of
    # recommendation on its own.
    assert not history.is_empty


def test_every_slice_is_capped() -> None:
    rows = []
    for i in range(1, 31):
        rows.append((anime(i, f"C{i}"), entry(i, ListStatus.COMPLETED, score=(i % 10) + 1)))
        rows.append((anime(100 + i, f"W{i}"), entry(100 + i, ListStatus.WATCHING)))
        rows.append((anime(200 + i, f"D{i}"), entry(200 + i, ListStatus.DROPPED)))

    history = summarise(rows)

    assert len(history.top_rated) == TOP_RATED_LIMIT
    assert len(history.recently_completed) == COMPLETED_LIMIT
    assert len(history.watching) == WATCHING_LIMIT
    assert len(history.dropped) == DROPPED_LIMIT


def test_titles_gathers_every_slice() -> None:
    """The set the eval checks a case against (FR-R3)."""
    rows = [
        (anime(1, "Frieren"), entry(1, ListStatus.COMPLETED, score=9)),
        (anime(2, "Bleach"), entry(2, ListStatus.DROPPED)),
        (anime(3, "Mushishi"), entry(3, ListStatus.PLANNED)),
        (anime(4, "Dandadan"), entry(4, ListStatus.WATCHING)),
    ]

    assert summarise(rows).titles == {"Frieren", "Bleach", "Mushishi", "Dandadan"}
