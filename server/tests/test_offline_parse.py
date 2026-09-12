"""Reading the two offline datasets (M15.5, FR-C6).

Everything here is a pure function over text, and it is tested against the
**real files** — ``tests/fixtures/offline/`` is a slice captured from the live
release by ``scripts/capture_offline.py``, not something written by hand. That
matters because the only hard part of this code is the leniency, and the shapes
it has to tolerate are shapes somebody else chose: ``themoviedb_id.movie`` is a
*list* while ``tv`` is a bare integer, ``imdb_id`` is a list in every current
entry and was a string in older ones, ``animeSeason.year`` is genuinely null
for 1,557 titles, and the file carries records with no MyAnimeList id at all.
A fixture invented from the documentation would agree with the documentation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arc.services.catalog.offline.parse import (
    build_search_text,
    duration_seconds,
    fribb_row,
    ids_from_sources,
    manami_row,
    parse_fribb,
    parse_manami,
    parse_manami_header,
)

FIXTURES = Path(__file__).parent / "fixtures" / "offline"

#: The release the fixture was captured from. Asserted, because the header is
#: where the version comes from and a re-capture that silently changed its
#: shape would otherwise show up as a null version in production.
CAPTURED_TAG = "2026-27"

#: Two ids the rest of the suite already knows Frieren by (tests/anilist_mock).
FRIEREN_ANILIST_ID = 154587
FRIEREN_MAL_ID = 52991
#: The roadmap's worked example for M15.5.
MUSHOKU_III_MAL_ID = 59193
#: The manami record for the same show that carries no MyAnimeList source.
MUSHOKU_III_NO_MAL_TITLE = "Mushoku Tensei: Jobless Reincarnation Season 3"


def manami_lines() -> list[str]:
    return FIXTURES.joinpath("manami-slice.jsonl").read_text().splitlines()


def fribb_entries() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = json.loads(FIXTURES.joinpath("fribb-slice.json").read_text())
    return payload


def rows_by_mal() -> dict[int | None, Any]:
    _, rows = parse_manami(manami_lines())
    return {row["mal_id"]: row for row in rows}


# --- ids out of source URLs -------------------------------------------------


def test_every_known_host_is_read_and_the_rest_ignored() -> None:
    found = ids_from_sources(
        [
            "https://anilist.co/anime/142051",
            "https://anime-planet.com/anime/raise-a-suilen-nvade-show",
            "https://kitsu.app/anime/47450",
            "https://myanimelist.net/anime/51478",
            "https://anidb.net/anime/17389",
            "https://livechart.me/anime/10745",
        ]
    )

    assert found == {"anilist_id": 142051, "mal_id": 51478, "kitsu_id": 47450, "anidb_id": 17389}


def test_kitsus_old_domain_still_parses() -> None:
    """Kitsu moved from ``kitsu.io`` to ``kitsu.app``; an old release must load."""
    assert ids_from_sources(["https://kitsu.io/anime/265"])["kitsu_id"] == 265


@pytest.mark.parametrize(
    "sources",
    [
        None,
        "https://myanimelist.net/anime/1",
        [],
        [None, 7, {"url": "https://myanimelist.net/anime/1"}],
        ["https://myanimelist.net/anime/not-a-number"],
        ["https://evil.example/myanimelist.net/anime/1"],
    ],
    ids=["null", "string", "empty", "junk", "no-number", "host-not-a-prefix"],
)
def test_anything_unreadable_leaves_every_id_null(sources: object) -> None:
    """The keys are always all four, so a row's shape never depends on data."""
    assert ids_from_sources(sources) == {
        "anilist_id": None,
        "mal_id": None,
        "kitsu_id": None,
        "anidb_id": None,
    }


# --- the header -------------------------------------------------------------


def test_the_release_tag_and_date_come_out_of_the_real_header() -> None:
    header = parse_manami_header(manami_lines()[0])

    assert header.tag == CAPTURED_TAG
    assert header.last_update is not None


@pytest.mark.parametrize("line", ["", "not json", "[]", '{"$schema": "https://example/x.json"}'])
def test_a_header_that_cannot_be_read_is_null_rather_than_fatal(line: str) -> None:
    """A dataset whose header changed shape still has 41k usable rows."""
    assert parse_manami_header(line).tag is None


# --- search_text ------------------------------------------------------------


def test_search_text_is_lowercased_and_deduplicated_in_order() -> None:
    text = build_search_text("Sousou no Frieren", ["Frieren", "SOUSOU NO FRIEREN", " ", "葬送"])

    assert text == "sousou no frieren | frieren | 葬送"


def test_search_text_holds_every_synonym_of_a_real_entry() -> None:
    row = rows_by_mal()[FRIEREN_MAL_ID]

    assert row["search_text"].startswith("sousou no frieren")
    assert "frieren: beyond journey's end" in row["search_text"]
    assert row["search_text"] == row["search_text"].lower()


# --- duration ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        ({"value": 120, "unit": "SECONDS"}, 120),
        ({"value": 24, "unit": "MINUTES"}, 1440),
        ({"value": 2, "unit": "HOURS"}, 7200),
        ({"value": 120, "unit": "FORTNIGHTS"}, None),
        ({"value": None, "unit": "SECONDS"}, None),
        ({}, None),
        (None, None),
        (1440, None),
    ],
)
def test_duration_is_normalised_to_seconds_or_dropped(
    duration: object, expected: int | None
) -> None:
    assert duration_seconds(duration) == expected


# --- manami rows ------------------------------------------------------------


def test_the_fixture_parses_whole() -> None:
    header, rows = parse_manami(manami_lines())
    parsed = list(rows)

    assert header.tag == CAPTURED_TAG
    assert len(parsed) == len(manami_lines()) - 1
    assert all(row["title"] and row["search_text"] for row in parsed)


def test_a_real_entry_keeps_its_cross_ids_and_season() -> None:
    row = rows_by_mal()[FRIEREN_MAL_ID]

    assert row["anilist_id"] == FRIEREN_ANILIST_ID
    assert row["title"] == "Sousou no Frieren"
    assert row["type"] == "TV"
    assert row["season"] == "FALL"
    assert row["season_year"] == 2023
    assert row["kitsu_id"] is not None
    assert row["anidb_id"] is not None
    assert row["score"] is not None and 0 < row["score"] <= 10
    assert row["picture"] and row["thumbnail"]


def test_the_mushoku_tensei_family_is_five_distinct_rows() -> None:
    """The case M15.5's matcher work has to tell apart, and the reason the
    fixture carries all of them: five titles that differ by a numeral."""
    rows = rows_by_mal()
    seasons = {mal_id: rows[mal_id]["title"] for mal_id in (39535, 45576, 51179, 55888, 59193)}

    assert len(set(seasons.values())) == 5
    assert seasons[MUSHOKU_III_MAL_ID].startswith("Mushoku Tensei III")


def test_an_entry_with_no_myanimelist_source_still_becomes_a_row() -> None:
    """It is a real record in the file, and dropping it would lose a title."""
    _, rows = parse_manami(manami_lines())
    row = next(row for row in rows if row["title"] == MUSHOKU_III_NO_MAL_TITLE)

    assert row["mal_id"] is None
    assert row["anilist_id"] is None
    assert row["search_text"].startswith("mushoku tensei: jobless reincarnation season 3")


def test_a_missing_season_year_is_null_rather_than_zero() -> None:
    rows = rows_by_mal()
    upcoming = rows[63794]

    assert upcoming["season_year"] is None
    assert upcoming["status"] == "UPCOMING"


def test_a_film_is_a_film() -> None:
    assert rows_by_mal()[32281]["type"] == "MOVIE"


def test_an_entry_with_no_title_is_dropped() -> None:
    assert manami_row({"sources": ["https://myanimelist.net/anime/1"]}) is None
    assert manami_row({"title": "   "}) is None


def test_one_unreadable_line_does_not_cost_the_others() -> None:
    lines = [manami_lines()[0], "{not json", "", "[]", "null", *manami_lines()[1:3]]
    _, rows = parse_manami(lines)

    assert len(list(rows)) == 2


def test_rows_are_produced_lazily() -> None:
    """62 MB of JSON must not be forty thousand dicts before the first insert."""
    _, rows = parse_manami(manami_lines())

    assert next(iter(rows))["title"]


# --- Fribb rows -------------------------------------------------------------


def test_the_fribb_fixture_parses_whole() -> None:
    rows = parse_fribb(fribb_entries())

    assert len(rows) == len(fribb_entries())
    assert all(row["mal_id"] or row["anilist_id"] for row in rows)


def test_a_films_tmdb_id_is_a_list_and_lands_in_the_movie_column() -> None:
    """Fribb writes ``{"movie": [129]}`` and ``{"tv": 1234}``; both are ids."""
    row = fribb_row({"mal_id": 199, "themoviedb_id": {"movie": [129, 130]}})

    assert row is not None
    assert (row["tmdb_movie_id"], row["tmdb_tv_id"]) == (129, None)


def test_a_series_tmdb_id_lands_in_the_tv_column_with_its_season() -> None:
    row = fribb_row(
        {"mal_id": 290, "themoviedb_id": {"tv": 26209}, "season": {"tmdb": 1, "tvdb": 2}}
    )

    assert row is not None
    assert row["tmdb_tv_id"] == 26209
    assert row["tmdb_season"] == 1
    assert row["tvdb_season"] == 2


def test_an_older_bare_integer_tmdb_id_is_read_as_a_series() -> None:
    row = fribb_row({"anilist_id": 1, "themoviedb_id": 5678})

    assert row is not None
    assert (row["tmdb_tv_id"], row["tmdb_movie_id"]) == (5678, None)


@pytest.mark.parametrize(
    ("imdb", "expected"),
    [
        (["tt0245429", "tt9999999"], "tt0245429"),
        ("tt0286390", "tt0286390"),
        ([], None),
        (None, None),
        ([None, "tt1"], "tt1"),
        (12345, None),
    ],
)
def test_imdb_is_read_whether_it_is_a_string_or_a_list(imdb: object, expected: str | None) -> None:
    row = fribb_row({"mal_id": 1, "imdb_id": imdb})

    assert row is not None
    assert row["imdb_id"] == expected


def test_an_entry_with_no_tmdb_at_all_is_still_a_row() -> None:
    """It still maps MAL to TVDB and IMDb, which is most of what it is for."""
    row = fribb_row({"mal_id": 403, "tvdb_id": 80654, "season": {"tvdb": 1}})

    assert row is not None
    assert row["tmdb_tv_id"] is None and row["tmdb_movie_id"] is None
    assert row["tvdb_id"] == 80654


def test_an_entry_arc_could_never_reach_is_dropped() -> None:
    """No AniList id and no MAL id means no way in from an Arc show."""
    assert fribb_row({"anidb_id": 1, "tvdb_id": 2}) is None


def test_a_numeric_string_id_is_read_and_a_word_is_not() -> None:
    row = fribb_row({"mal_id": "290", "tvdb_id": "unknown"})

    assert row is not None
    assert row["mal_id"] == 290
    assert row["tvdb_id"] is None


@pytest.mark.parametrize("payload", [None, {}, "[]", 7, [None, 3, "x"]])
def test_a_body_that_is_not_the_id_map_yields_no_rows(payload: object) -> None:
    """An HTML error page saved as JSON must leave the table alone, not raise."""
    assert parse_fribb(payload) == []
