"""Which sequel "Next season" offers, and when a season has ended (FR-S5 item 3).

Pure: the rules in :mod:`arc.services.catalog.sequels` over rows built here.
The HTTP cases are in ``tests/test_playback_api.py``.
"""

from __future__ import annotations

from typing import Any

from arc.models import Anime, Episode
from arc.services.catalog.sequels import (
    SequelCandidate,
    first_regular_episode,
    pick_sequel,
    season_ends_here,
    sequel_ids,
)


def relation(
    anilist_id: int | None,
    relation_type: str = "SEQUEL",
    fmt: str | None = None,
    mal_id: int | None = None,
) -> dict[str, Any]:
    return {
        "anilist_id": anilist_id,
        "mal_id": mal_id,
        "relation_type": relation_type,
        "title": {"romaji": "x"},
        "format": fmt,
    }


def rows(*candidates: tuple[str, int, SequelCandidate]) -> dict[tuple[str, int], SequelCandidate]:
    return {(source, external): candidate for source, external, candidate in candidates}


def test_only_sequel_relations_are_followed() -> None:
    blob = [relation(1, "PREQUEL"), relation(2), relation(3, "SIDE_STORY"), relation(4, mal_id=9)]
    assert sequel_ids(blob) == ({2, 4}, {9})
    found = rows(
        ("anilist", 1, SequelCandidate(10, "TV", 2019)),
        ("anilist", 3, SequelCandidate(30, "TV", 2019)),
    )
    assert pick_sequel(blob, found) is None


def test_a_relation_resolves_by_mal_id_when_anilist_has_no_row() -> None:
    found = rows(("mal", 77, SequelCandidate(70, "TV", 2021)))
    assert pick_sequel([relation(5, mal_id=77)], found) == 70


def test_a_tv_season_wins_over_a_film() -> None:
    blob = [relation(1, fmt="MOVIE"), relation(2, fmt="TV")]
    found = rows(
        ("anilist", 1, SequelCandidate(10, None, 2018)),
        ("anilist", 2, SequelCandidate(20, None, 2022)),
    )
    assert pick_sequel(blob, found) == 20


def test_among_tv_seasons_the_earliest_year_wins_and_an_unknown_year_is_last() -> None:
    blob = [relation(1), relation(2), relation(3)]
    found = rows(
        ("anilist", 1, SequelCandidate(10, "TV", None)),
        ("anilist", 2, SequelCandidate(20, "TV", 2023)),
        ("anilist", 3, SequelCandidate(30, "TV", 2021)),
    )
    assert pick_sequel(blob, found) == 30


def test_the_stored_format_is_used_when_the_relation_states_none() -> None:
    blob = [relation(1), relation(2)]
    found = rows(
        ("anilist", 1, SequelCandidate(10, "OVA", 2019)),
        ("anilist", 2, SequelCandidate(20, "TV", 2024)),
    )
    assert pick_sequel(blob, found) == 20


def test_a_finished_show_ends_at_its_last_row() -> None:
    anime = Anime(status="FINISHED", episodes=None)
    assert season_ends_here(anime, Episode(number=10), has_following=False)
    assert not season_ends_here(anime, Episode(number=10), has_following=True)


def test_an_airing_show_ends_only_at_its_stated_count() -> None:
    anime = Anime(status="RELEASING", episodes=12)
    assert season_ends_here(anime, Episode(number=12), has_following=False)
    assert not season_ends_here(anime, Episode(number=11), has_following=False)
    assert not season_ends_here(
        Anime(status="RELEASING", episodes=None), Episode(number=12), has_following=False
    )


def test_episode_zero_is_not_the_first_episode() -> None:
    episodes = [Episode(number=0), Episode(number=2), Episode(number=1)]
    first = first_regular_episode(episodes)
    assert first is not None and first.number == 1
    assert first_regular_episode([Episode(number=0)]) is None
