"""The aired rule, and the two sanity rules over it (FR-C4, FR-C5).

Pure, like :mod:`arc.services.catalog.airing` itself: rows built in memory and
a frozen clock, so the table below is the rule rather than a description of it.
The database-shaped consequences — "behind by N", the acquisition window, the
show page's rows — are tested where they live; what is here is the derivation
all of them read.

The motivating case is real. AniList lists *One Room, Third Season* (205068)
as ``FINISHED`` with episode 3 airing on 2026-09-27, a month after episode 2
and three weeks after episode 4. Arc copied the typo onto the show page and
told a user episode 3 "will air 27 Sep" in between two episodes they had
already watched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arc.api.anime_schemas import AnimeDetail
from arc.models import Anime, Episode, EpisodeState
from arc.services.catalog.airing import (
    FINISHED,
    RELEASING,
    aired_episodes,
    aired_through,
    effective_air_at,
    is_aired,
    out_of_order,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def episode(number: int, at: datetime | None) -> Episode:
    """One row, never persisted. ``id`` and ``state`` are only here because
    :class:`~arc.api.anime_schemas.EpisodeOut` renders them."""
    return Episode(id=number, anime_id=1, number=number, air_at=at, state=EpisodeState.NOT_WANTED)


def day(month: int, number: int) -> datetime:
    return datetime(2026, month, number, 12, 0, tzinfo=UTC)


def one_room() -> list[Episode]:
    """The real list: episode 3 dated after episodes 4 to 7 (AniList 205068)."""
    return [
        episode(1, day(8, 27)),
        episode(2, day(8, 27)),
        episode(3, day(9, 27)),  # the typo
        episode(4, day(9, 3)),
        episode(5, day(9, 3)),
        episode(6, day(9, 10)),
        episode(7, day(9, 10)),
    ]


def aired_numbers(episodes: list[Episode], *, status: str | None) -> list[int]:
    return [
        row.number
        for row in aired_episodes(episodes, now=NOW, anime_status=status, next_airing=None)
    ]


# --- Rule 1: a finished show has no future episodes -------------------------


def test_a_finished_show_counts_a_future_dated_episode_as_aired() -> None:
    """The status is the source's summary of the whole run; the date is not."""
    episodes = [episode(1, day(8, 27)), episode(2, day(9, 27)), episode(3, day(9, 3))]

    assert aired_numbers(episodes, status=FINISHED) == [1, 2, 3]


def test_a_finished_show_with_one_date_in_the_future_and_nothing_else() -> None:
    """Even alone, with no sibling to contradict it: the status decides."""
    rows = [episode(1, NOW + timedelta(days=30))]
    boundary = aired_through(rows, now=NOW, anime_status=FINISHED, next_airing=None)

    assert is_aired(rows[0], now=NOW, anime_status=FINISHED, boundary=boundary)


# --- Rule 2: air times do not go backwards ----------------------------------


def test_a_non_monotonic_date_is_flagged_estimated() -> None:
    assert out_of_order(one_room()) == {3}


def test_a_well_ordered_list_flags_nothing() -> None:
    rows = [episode(number, day(9, number)) for number in range(1, 8)]

    assert out_of_order(rows) == frozenset()
    assert effective_air_at(rows) == {number: day(9, number) for number in range(1, 8)}


def test_equal_air_times_are_not_out_of_order() -> None:
    """Two episodes in one broadcast is ordinary, not a contradiction."""
    rows = [episode(1, day(9, 3)), episode(2, day(9, 3)), episode(3, day(9, 10))]

    assert out_of_order(rows) == frozenset()


def test_the_effective_time_is_the_earlier_of_its_own_and_the_next_episodes() -> None:
    times = effective_air_at(one_room())

    assert times[3] == day(9, 3)  # episode 4's date, not its own 27 Sep
    assert times[4] == day(9, 3)  # and every other row is untouched
    assert times[7] == day(9, 10)


def test_a_releasing_show_airs_a_non_monotonic_episode_with_its_neighbour() -> None:
    """The sibling decides the aired line, and the status plays no part."""
    rows = [
        episode(1, day(9, 3)),
        episode(2, day(10, 30)),  # dated after episode 3
        episode(3, day(9, 10)),
        episode(4, day(9, 20)),  # genuinely still to come
    ]

    assert aired_numbers(rows, status=RELEASING) == [1, 2, 3]


def test_a_non_monotonic_episode_is_not_aired_before_its_neighbour_is() -> None:
    """The *earlier* of the two dates, which can still be in the future."""
    rows = [episode(1, day(9, 3)), episode(2, day(11, 1)), episode(3, day(9, 20))]

    assert aired_numbers(rows, status=RELEASING) == [1]


def test_the_stored_air_time_is_never_rewritten() -> None:
    """Only the derived answers change; the next refresh still has the typo."""
    rows = one_room()

    aired_episodes(rows, now=NOW, anime_status=FINISHED, next_airing=None)
    out_of_order(rows)

    assert rows[2].air_at == day(9, 27)
    assert not rows[2].air_at_estimated  # the stored flag is untouched too


# --- A genuinely future episode is still future -----------------------------


def test_a_releasing_show_keeps_a_genuinely_future_episode_unaired() -> None:
    rows = [episode(1, day(9, 3)), episode(2, day(9, 10)), episode(3, day(9, 20))]

    assert aired_numbers(rows, status=RELEASING) == [1, 2]
    assert out_of_order(rows) == frozenset()


def test_the_next_airing_boundary_still_places_an_undated_back_catalogue() -> None:
    """Rule 2 sits over the old boundary rule; it does not replace it."""
    rows = [episode(number, None) for number in range(1, 13)]

    aired = aired_episodes(
        rows,
        now=NOW,
        anime_status=RELEASING,
        next_airing={"episode": 7, "airingAt": int(NOW.timestamp()) + 3600},
    )

    assert [row.number for row in aired] == [1, 2, 3, 4, 5, 6]


# --- What the show page renders ---------------------------------------------


@pytest.mark.parametrize("status", [FINISHED, RELEASING])
def test_the_show_page_marks_a_non_monotonic_row_aired_and_estimated(status: str) -> None:
    """FR-C4/FR-C5 end to end, minus the database and the router."""
    anime = Anime(id=1, status=status, episodes=7)
    detail = AnimeDetail.build(anime, episodes=one_room(), now=NOW)

    rows = {row.number: row for row in detail.episodes}
    assert rows[3].aired is True
    assert rows[3].air_at_estimated is True
    # The date the source published is still what the client is given: Arc
    # shows it as an estimate rather than inventing a replacement.
    assert rows[3].air_at == day(9, 27)
    assert [row.number for row in detail.episodes if row.air_at_estimated] == [3]
    assert all(row.aired for row in detail.episodes)
