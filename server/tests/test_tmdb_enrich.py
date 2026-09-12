"""What TMDB is allowed to write (M15.5, FR-C6, cache rule 3).

:func:`plan_enrichment` is pure, so most of this file needs no database: a
detached :class:`Anime` and a list of :class:`Episode` objects are the whole
input. The two tests that write go through ``db_session``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arc.models import Anime, Episode
from arc.services.catalog.cache import episodes_for
from arc.services.catalog.credits import STUDIO_ROLE
from arc.services.tmdb.client import BACKDROP_SIZE, IMAGE_BASE, POSTER_SIZE, STILL_SIZE
from arc.services.tmdb.enrich import (
    MAX_PER_ROLE,
    Enrichment,
    TmdbPayloads,
    apply_enrichment,
    crew_credits,
    episode_art,
    plan_enrichment,
    resolve_season,
)
from tests.tmdb_mock import (
    FRIEREN_ANILIST_ID,
    FRIEREN_MAL_ID,
    film_credits,
    film_movie,
    frieren_credits,
    frieren_season,
    frieren_show,
)


def anime(**kwargs: Any) -> Anime:
    """A cached row, as a row that has never met TMDB looks."""
    defaults: dict[str, Any] = {
        "id": 1,
        "anilist_id": FRIEREN_ANILIST_ID,
        "mal_id": FRIEREN_MAL_ID,
        "title_romaji": "Sousou no Frieren",
        "episodes": 28,
        "season": "FALL",
        "season_year": 2023,
        "studio": "MADHOUSE",
        "detail_source": "mal",
        "summary_source": "mal",
        "cover_url": "https://cdn.myanimelist.net/images/anime/1015/138006.jpg",
        "cover_large_url": None,
        "banner_url": None,
        "credits": None,
    }
    return Anime(**(defaults | kwargs))


def episodes(count: int, **kwargs: Any) -> list[Episode]:
    return [Episode(anime_id=1, number=number, **kwargs) for number in range(1, count + 1)]


def payloads(**kwargs: Any) -> TmdbPayloads:
    defaults: dict[str, Any] = {
        "show": frieren_show(),
        "season": frieren_season(),
        "credits": frieren_credits(),
        "season_number": 1,
    }
    return TmdbPayloads(**(defaults | kwargs))


# --- Season resolution ------------------------------------------------------


def test_the_mapped_season_wins_when_the_series_has_it() -> None:
    """``offline_ids.tmdb_season`` was written by somebody looking at both."""
    assert resolve_season(anime(), frieren_show(), mapped=1) == 1


def test_a_mapped_season_the_series_does_not_have_falls_through() -> None:
    show = frieren_show()
    assert resolve_season(anime(), show, mapped=9) == 1  # resolved by year instead


def test_specials_are_never_the_answer() -> None:
    """TMDB's season 0 is the OVAs; an anime cour is never it."""
    show = {"seasons": [{"season_number": 0, "air_date": "2023-10-11", "episode_count": 26}]}
    assert resolve_season(anime(), show) is None


def test_the_season_is_the_one_that_started_in_the_shows_year() -> None:
    show = {
        "seasons": [
            {"season_number": 1, "air_date": "2021-04-03", "episode_count": 12},
            {"season_number": 2, "air_date": "2023-10-06", "episode_count": 12},
            {"season_number": 3, "air_date": "2025-01-05", "episode_count": 12},
        ]
    }
    assert resolve_season(anime(episodes=12), show) == 2


def test_within_a_year_the_closest_episode_count_wins() -> None:
    show = {
        "seasons": [
            {"season_number": 1, "air_date": "2023-01-06", "episode_count": 24},
            {"season_number": 2, "air_date": "2023-10-06", "episode_count": 13},
        ]
    }
    assert resolve_season(anime(episodes=12), show) == 2


def test_a_year_with_no_match_and_several_seasons_gives_up() -> None:
    """A wrong still is worse than no still: this is the case that guesses."""
    show = {
        "seasons": [
            {"season_number": 1, "air_date": "2019-04-03", "episode_count": 12},
            {"season_number": 2, "air_date": "2021-04-03", "episode_count": 12},
        ]
    }
    assert resolve_season(anime(season_year=2023), show) is None


def test_a_single_undated_season_with_a_matching_count_is_taken() -> None:
    show = {"seasons": [{"season_number": 1, "air_date": None, "episode_count": 28}]}
    assert resolve_season(anime(), show) == 1


def test_a_single_undated_season_with_a_different_count_is_not() -> None:
    show = {"seasons": [{"season_number": 1, "air_date": None, "episode_count": 12}]}
    assert resolve_season(anime(episodes=28), show) is None


def test_no_seasons_at_all_is_none() -> None:
    assert resolve_season(anime(), {"seasons": []}) is None
    assert resolve_season(anime(), None) is None


# --- Crew -------------------------------------------------------------------


def test_the_series_director_outranks_the_episode_directors() -> None:
    """TMDB credits twenty-two people as "Director" on two episodes each."""
    roles = crew_credits(frieren_credits())
    directors = [name for role, name in roles if role == "Director"]
    assert directors[0] == "Keiichiro Saito"
    assert len(directors) <= MAX_PER_ROLE


def test_the_mapped_roles_come_out_of_a_real_crew() -> None:
    roles = dict(
        (role, name) for role, name in reversed(crew_credits(frieren_credits()))
    )  # first of each role wins after the reverse
    assert roles["Series Composition"] == "Tomohiro Suzuki"
    assert roles["Music"] == "Evan Call"
    assert roles["Original Creator"] == "Kanehito Yamada"
    assert roles["Character Design"] == "Reiko Nagasawa"


def test_an_unmapped_job_is_dropped_rather_than_shown() -> None:
    names = {name for _, name in crew_credits(frieren_credits())}
    # "Sound Director", "Producer", "Theme Song Performance" and friends are in
    # the fixture and have no row in the design.
    assert "Shoji Hata" not in names


def test_a_films_flat_job_field_is_read_too() -> None:
    roles = dict(crew_credits(film_credits()))
    assert roles["Director"] == "Naoko Yamada"
    assert roles["Series Composition"] == "Reiko Yoshida"  # TMDB's "Screenplay"


def test_no_credits_block_is_no_credits() -> None:
    assert crew_credits(None) == []
    assert crew_credits({"crew": []}) == []


# --- Rule 3: what may be written --------------------------------------------


def test_a_mal_row_gains_the_backdrop_the_poster_and_the_credits() -> None:
    plan = plan_enrichment(anime(), episodes(4), payloads())
    assert plan.banner_url == f"{IMAGE_BASE}{BACKDROP_SIZE}{frieren_show()['backdrop_path']}"
    assert plan.cover_large_url == f"{IMAGE_BASE}{POSTER_SIZE}{frieren_show()['poster_path']}"
    assert plan.credits is not None
    assert plan.credits[0] == {"role": STUDIO_ROLE, "name": "MADHOUSE"}
    assert plan.columns() == ["banner_url", "cover_large_url", "credits"]


def test_anilist_key_art_is_never_overwritten() -> None:
    row = anime(
        detail_source="anilist",
        summary_source="anilist",
        banner_url="https://anilist.example/banner.jpg",
        cover_large_url="https://anilist.example/cover.jpg",
    )
    plan = plan_enrichment(row, episodes(4), payloads())
    assert plan.banner_url is None
    assert plan.cover_large_url is None


def test_anilist_credits_are_never_overwritten_even_when_thin() -> None:
    """A one-row AniList answer is still AniList's answer (rule 3)."""
    row = anime(detail_source="anilist", credits=[{"role": STUDIO_ROLE, "name": "Madhouse"}])
    assert plan_enrichment(row, episodes(4), payloads()).credits is None


def test_a_mal_studio_only_credits_list_is_completed() -> None:
    """The hole TMDB exists to fill: MAL knows the studio and nobody else."""
    row = anime(detail_source="mal", credits=[{"role": STUDIO_ROLE, "name": "Madhouse"}])
    plan = plan_enrichment(row, episodes(4), payloads())
    assert plan.credits is not None
    assert any(entry["role"] == "Director" for entry in plan.credits)


def test_credits_that_already_name_a_person_are_left_alone() -> None:
    row = anime(
        detail_source="offline",
        credits=[{"role": "Director", "name": "Somebody Else"}],
    )
    assert plan_enrichment(row, episodes(4), payloads()).credits is None


def test_the_230_px_mal_cover_is_not_touched() -> None:
    """``cover_url`` is not TMDB's column; only ``cover_large_url`` is."""
    plan = plan_enrichment(anime(), episodes(4), payloads())
    assert "cover_url" not in plan.columns()


# --- Episode art ------------------------------------------------------------


def test_stills_and_titles_are_matched_by_episode_number() -> None:
    rows = episodes(3)
    art = episode_art(frieren_season(), rows)
    assert [entry.number for entry in art] == [1, 2, 3]
    assert art[0].title == "The Journey's End"
    assert art[0].still_url is not None
    assert art[0].still_url.startswith(f"{IMAGE_BASE}{STILL_SIZE}/")


def test_an_episode_row_that_already_has_a_title_keeps_it() -> None:
    rows = episodes(2)
    rows[0].title = "A title somebody confirmed in the match queue"
    art = {entry.number: entry for entry in episode_art(frieren_season(), rows)}
    assert art[1].title is None
    assert art[1].still_url is not None
    assert art[2].title == "It Didn't Have to Be Magic..."


def test_an_episode_with_both_fields_filled_is_not_in_the_plan_at_all() -> None:
    rows = episodes(1, title="Have", still_url="https://have.example/still.jpg")
    assert episode_art(frieren_season(), rows) == ()


def test_tmdb_never_invents_an_episode_row() -> None:
    """Rule 4: episodes are Arc's. A still is not evidence of an episode."""
    art = episode_art(frieren_season(), episodes(2))
    assert {entry.number for entry in art} == {1, 2}


def test_art_only_plans_the_key_art_and_nothing_else() -> None:
    """The season pass's mode: the backdrop and the poster, no crew, no stills.

    Asserted against the *full* payloads, so the mode is what decides rather
    than what the caller happened to have fetched.
    """
    plan = plan_enrichment(anime(), episodes(4), payloads(), art_only=True)
    assert plan.banner_url is not None
    assert plan.cover_large_url is not None
    assert plan.credits is None
    assert plan.episodes == ()
    assert plan.columns() == ["banner_url", "cover_large_url"]


def test_art_only_still_never_overwrites_anilist_art() -> None:
    row = anime(
        detail_source="anilist",
        summary_source="anilist",
        banner_url="https://anilist.example/banner.jpg",
    )
    plan = plan_enrichment(row, episodes(4), payloads(), art_only=True)
    assert plan.banner_url is None
    assert plan.cover_large_url is not None


def test_no_season_means_no_stills_but_art_still_lands() -> None:
    plan = plan_enrichment(anime(), episodes(4), payloads(season=None, season_number=None))
    assert plan.episodes == ()
    assert plan.banner_url is not None


def test_a_film_is_art_and_credits_only() -> None:
    row = anime(episodes=1, season_year=2016)
    payload = TmdbPayloads(show=film_movie(), credits=film_credits())
    plan = plan_enrichment(row, episodes(1), payload)
    assert plan.banner_url is not None
    assert plan.credits is not None
    assert plan.episodes == ()


def test_an_empty_plan_knows_it_is_empty() -> None:
    row = anime(
        detail_source="anilist",
        banner_url="b",
        cover_large_url="c",
        credits=[{"role": "Director", "name": "X"}],
    )
    rows = episodes(1, title="t", still_url="s")
    plan = plan_enrichment(row, rows, payloads())
    assert plan.empty
    assert Enrichment().empty


# --- Applying ---------------------------------------------------------------


@pytest.mark.pg
async def test_apply_writes_the_columns_and_the_episode_rows(db_session: Any) -> None:
    row = anime(id=None)
    db_session.add(row)
    await db_session.flush()
    for number in range(1, 4):
        db_session.add(Episode(anime_id=row.id, number=number))
    await db_session.flush()

    plan = plan_enrichment(row, await episodes_for(db_session, row.id), payloads())
    touched = await apply_enrichment(db_session, row, plan)

    assert row.banner_url is not None
    assert row.cover_large_url is not None
    assert touched == 6  # three episodes × (title + still)
    filled = await episodes_for(db_session, row.id)
    assert filled[0].title == "The Journey's End"
    assert filled[0].still_url is not None


@pytest.mark.pg
async def test_apply_is_idempotent(db_session: Any) -> None:
    """A second run plans nothing, because every hole is now full."""
    row = anime(id=None)
    db_session.add(row)
    await db_session.flush()
    db_session.add(Episode(anime_id=row.id, number=1))
    await db_session.flush()

    first = plan_enrichment(row, await episodes_for(db_session, row.id), payloads())
    await apply_enrichment(db_session, row, first)
    second = plan_enrichment(row, await episodes_for(db_session, row.id), payloads())
    assert second.empty
