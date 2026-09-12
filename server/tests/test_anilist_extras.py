"""Reading AniList's ``staff`` roles and ``streamingEpisodes`` titles (M15).

Both fields are free text written by whoever edited the entry, and both are
rendered as fact on the show page — a wrong role puts the sound director in the
director's row, and a wrong episode number puts episode 3's still on episode 8.
So the two parsers get their own tests, at the level where they are pure
functions and a case is one line.

No database and no HTTP here: :mod:`tests.test_catalog_cache` covers what the
cache does with the results.
"""

from __future__ import annotations

import pytest

from arc.services.anilist.extras import (
    credit_role,
    parse_streaming_episodes,
    staff_credits,
)
from arc.services.catalog.credits import CREDIT_ORDER, STUDIO_ROLE, credits_from
from arc.services.catalog.source import EpisodeArt

# --- Roles ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("Director", "Director"),
        ("director", "Director"),
        ("Chief Director", "Director"),
        ("Director (eps 1, 14)", "Director"),
        ("Director (eps 1-12)", "Director"),
        ("Series Composition", "Series Composition"),
        ("Character Design", "Character Design"),
        ("Music", "Music"),
        ("Original Creator", "Original Creator"),
        ("Original Story", "Original Creator"),
        ("Original Work", "Original Creator"),
        ("Original Manga", "Original Creator"),
        # One person, two jobs: each half is read in its own right.
        ("Director, Series Composition", "Director"),
    ],
)
def test_the_six_credits_are_recognised(role: str, expected: str) -> None:
    assert credit_role(role) == expected


@pytest.mark.parametrize(
    "role",
    [
        "Animation Director",
        "Chief Animation Director",
        "Sound Director",
        "Art Director",
        "Episode Director",
        "Assistant Director",
        "Action Director",
        "Music Director",
        "CG Director",
        "3D Director",
        "Director of Photography",
        "Original Character Design",
        "Original Work Assistance",
        "Theme Song Performance",
        "Insert Song Performance",
        "Music Producer",
        "Key Animation",
        "Producer",
        "Title Logo Design",
        "Design Works",
        "",
        None,
    ],
)
def test_a_role_that_is_not_one_of_the_six_is_dropped(role: str | None) -> None:
    """A near-miss must not be promoted.

    Every string here contains one of the words Arc's six credits are spelled
    with, which is the whole reason the role is matched whole: a substring
    match reads the action director as the director and "Original Work
    Assistance" as the author.
    """
    assert credit_role(role) is None


def test_staff_credits_keeps_anilists_order_and_drops_the_nameless() -> None:
    pairs = staff_credits(
        {
            "edges": [
                {"role": "Music", "node": {"name": {"full": "Evan Call"}}},
                {"role": "Director", "node": {"name": {"full": "Keiichirou Saitou"}}},
                {"role": "Key Animation", "node": {"name": {"full": "Someone Else"}}},
                {"role": "Character Design", "node": {"name": {"full": ""}}},
                {"role": "Series Composition", "node": {}},
                "not an edge",
            ]
        }
    )
    assert pairs == [("Music", "Evan Call"), ("Director", "Keiichirou Saitou")]


def test_staff_credits_of_nothing_is_empty() -> None:
    assert staff_credits(None) == []
    assert staff_credits({}) == []


# --- The credits block ------------------------------------------------------


def test_the_studio_is_the_first_row_and_the_rest_are_in_the_designs_order() -> None:
    rows = credits_from(
        "MADHOUSE",
        [
            ("Music", "Evan Call"),
            ("Original Creator", "Kanehito Yamada"),
            ("Director", "Keiichirou Saitou"),
            ("Character Design", "Reiko Nagasawa"),
            ("Series Composition", "Tomohiro Suzuki"),
        ],
    )
    assert [row["role"] for row in rows] == list(CREDIT_ORDER)
    assert rows[0] == {"role": STUDIO_ROLE, "name": "MADHOUSE"}


def test_a_co_credit_is_kept_in_the_sources_own_order() -> None:
    """Two people really do share a role; picking one would be editorial."""
    rows = credits_from(
        "MADHOUSE",
        [("Original Creator", "Kanehito Yamada"), ("Original Creator", "Tsukasa Abe")],
    )
    assert [row["name"] for row in rows] == ["MADHOUSE", "Kanehito Yamada", "Tsukasa Abe"]


def test_duplicates_and_blanks_are_dropped() -> None:
    rows = credits_from(
        "  MADHOUSE  ",
        [("Director", "Keiichirou Saitou"), ("Director", "Keiichirou Saitou"), ("Music", "  ")],
    )
    assert rows == [
        {"role": STUDIO_ROLE, "name": "MADHOUSE"},
        {"role": "Director", "name": "Keiichirou Saitou"},
    ]


def test_an_unmapped_label_never_reaches_the_column() -> None:
    """``credits_from`` is the last gate, not only the source adapter."""
    assert credits_from(None, [("Sound Director", "Satoki Iida")]) == []


def test_nothing_known_is_an_empty_block() -> None:
    assert credits_from(None) == []


# --- Streaming episodes -----------------------------------------------------


def episode(number: int, title: str) -> dict[str, str]:
    return {"title": title, "thumbnail": f"https://img.test/e{number:02d}.jpg"}


def test_the_number_and_the_title_come_out_of_the_entrys_title() -> None:
    found = parse_streaming_episodes(
        [
            episode(1, "Episode 1 - The Journey's End"),
            episode(2, "Episode 2 - It Didn't Have to Be Magic"),
        ],
        episodes=2,
    )
    assert found == [
        EpisodeArt(1, "The Journey's End", "https://img.test/e01.jpg"),
        EpisodeArt(2, "It Didn't Have to Be Magic", "https://img.test/e02.jpg"),
    ]


@pytest.mark.parametrize(
    ("title", "number", "episode_title"),
    [
        ("Episode 7", 7, None),
        ("Episode 7 - ", 7, None),
        ("episode 7 - Sole Heir", 7, "Sole Heir"),
        ("Ep. 7 - Sole Heir", 7, "Sole Heir"),
        ("Episode 7 – Sole Heir", 7, "Sole Heir"),
        ("Episode 7: Sole Heir", 7, "Sole Heir"),
        ("Episode 007 - Sole Heir", 7, "Sole Heir"),
    ],
)
def test_the_shapes_the_pattern_accepts(title: str, number: int, episode_title: str | None) -> None:
    found = parse_streaming_episodes([{"title": title, "thumbnail": "t"}], episodes=99)
    assert found == [EpisodeArt(number, episode_title, "t")]


@pytest.mark.parametrize(
    "title",
    [
        "Trailer",
        "PV 2",
        "Frieren recap: Episode 1 to 4",
        "Season 2 Episode 1",
        "Episode Zero",
        "",
    ],
)
def test_an_entry_that_names_no_episode_is_ignored(title: str) -> None:
    """Ignored, never guessed at: a still on the wrong row is worse than none.

    The list is longer than one so the positional fallback cannot fire and
    hide the ignoring.
    """
    found = parse_streaming_episodes(
        [{"title": title, "thumbnail": "t"}, episode(1, "Episode 1 - Real")],
        episodes=2,
    )
    assert found == [EpisodeArt(1, "Real", "https://img.test/e01.jpg")]


def test_the_first_entry_to_claim_a_number_keeps_it() -> None:
    """AniList lists the same episode once per streaming service on some rows."""
    found = parse_streaming_episodes(
        [
            {"title": "Episode 2 - A Middle", "thumbnail": "sub.jpg"},
            {"title": "Episode 2 - A Middle (dub)", "thumbnail": "dub.jpg"},
        ],
        episodes=12,
    )
    assert found == [EpisodeArt(2, "A Middle", "sub.jpg")]


def test_positional_order_is_used_when_nothing_parses_and_the_count_matches() -> None:
    """Older entries title their links with the episode name alone."""
    found = parse_streaming_episodes(
        [
            {"title": "The Journey's End", "thumbnail": "a.jpg"},
            {"title": "Killing Magic", "thumbnail": "b.jpg"},
            {"title": "Sole Heir", "thumbnail": "c.jpg"},
        ],
        episodes=3,
    )
    assert found == [
        EpisodeArt(1, "The Journey's End", "a.jpg"),
        EpisodeArt(2, "Killing Magic", "b.jpg"),
        EpisodeArt(3, "Sole Heir", "c.jpg"),
    ]


@pytest.mark.parametrize("episodes", [None, 2, 4])
def test_the_positional_fallback_needs_the_count_to_match_exactly(episodes: int | None) -> None:
    """A partial list slid into position would put every still on the wrong row."""
    nodes = [
        {"title": "The Journey's End", "thumbnail": "a.jpg"},
        {"title": "Killing Magic", "thumbnail": "b.jpg"},
        {"title": "Sole Heir", "thumbnail": "c.jpg"},
    ]
    assert parse_streaming_episodes(nodes, episodes=episodes) == []


def test_one_parsed_entry_is_enough_to_switch_the_fallback_off() -> None:
    """Mixed titles are read, not counted: the parsed one is the evidence."""
    found = parse_streaming_episodes(
        [
            {"title": "The Journey's End", "thumbnail": "a.jpg"},
            {"title": "Episode 2 - Killing Magic", "thumbnail": "b.jpg"},
        ],
        episodes=2,
    )
    assert found == [EpisodeArt(2, "Killing Magic", "b.jpg")]


def test_an_entry_may_carry_a_thumbnail_without_a_title_and_the_other_way_round() -> None:
    found = parse_streaming_episodes(
        [
            {"title": "Episode 1 - Named", "thumbnail": None},
            {"title": "Episode 2", "thumbnail": "  b.jpg  "},
            {"title": "Episode 3 - Blank", "thumbnail": "   "},
        ],
        episodes=3,
    )
    assert found == [
        EpisodeArt(1, "Named", None),
        EpisodeArt(2, None, "b.jpg"),
        EpisodeArt(3, "Blank", None),
    ]


def test_nothing_at_all_is_an_empty_list() -> None:
    assert parse_streaming_episodes(None, episodes=12) == []
    assert parse_streaming_episodes([], episodes=12) == []
