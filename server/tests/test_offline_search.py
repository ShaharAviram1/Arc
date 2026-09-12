"""The offline catalogue as the first stop (M15.5 bullets 2 and 3).

Four things are under test, and they are the four the milestone promises with
AniList and MyAnimeList both blocked.

**Search answers.** ``offline_search`` matches the imported titles *and their
synonyms* the way ``catalog/local.py`` matches the cached rows — every word, as
an unanchored substring, with ``%`` and ``_`` escaped — and ranks an exact name
first. The hits are materialised as ``anime`` rows on the way to the response,
because a card links to ``/anime/{id}`` and that id is Arc's.

**Identity holds.** A show found through MAL lands on the row AniList made, and
the other way round, because the offline id map fills in whichever id the
payload was missing before the row is looked up (FR-C6, cache rule 1). The
reconciliation job does the same repair over the whole table with no network at
all — which is the case it exists for, since the rows needing repair are
exactly the ones an outage created.

**Matching consults it first.** A release name is written in the vocabulary
manami's synonyms are in ("Mushoku Tensei S3") and not the one AniList
publishes, so the offline catalogue is the first candidate source and the live
search is what happens when it has nothing. Nothing about the scoring, the
threshold or the review rule changes, which is what the corpus tests next door
keep honest.

**The season still lists.** With both sources failing, the sweep seeds the
season from the import instead — the titles, and no air times at all, which is
what the outage costs.

The slices are the fixtures the import task captured: 29 titles and 29 id-map
entries, loaded through the real ``import_manami`` / ``import_fribb``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Anime, Job, JobStatus, OfflineAnime
from arc.services.catalog import Breaker, CatalogService
from arc.services.catalog import jobs as catalog_jobs
from arc.services.catalog.cache import upsert_detail, upsert_summaries
from arc.services.catalog.jobs import RECONCILE, SEASON_SWEEP
from arc.services.catalog.offline.ids import (
    anilist_for_mal,
    fill_missing_ids,
    lookup_ids,
    mal_for_anilist,
)
from arc.services.catalog.offline.importer import import_fribb, import_manami
from arc.services.catalog.offline.materialise import media_from, upsert_offline_summaries
from arc.services.catalog.offline.search import offline_search, offline_season
from arc.services.catalog.source import CatalogMedia, MediaTitle
from arc.services.jobs import JobContext
from arc.services.library.matcher import match
from arc.services.library.parser import parse
from tests.conftest import add_user, api_transport, login
from tests.mal_mock import FakeMal

pytestmark = pytest.mark.pg

FIXTURES = Path(__file__).parent / "fixtures" / "offline"
MANAMI_SLICE = FIXTURES / "manami-slice.jsonl"
FRIBB_SLICE = FIXTURES / "fribb-slice.json"

USER_EMAIL = "offline@arc.test"
USER_PASSWORD = "offline-password"

#: Ids out of the slices, named so an assertion reads as a show rather than a
#: number. Mushoku Tensei's first and third seasons and Frieren are the three
#: the cases below turn on.
MUSHOKU_1_ANILIST, MUSHOKU_1_MAL = 108465, 39535
MUSHOKU_3_ANILIST, MUSHOKU_3_MAL = 178789, 59193
FRIEREN_ANILIST, FRIEREN_MAL = 154587, 52991
ONE_PIECE_ANILIST, ONE_PIECE_MAL = 21, 21

#: The one slice entry with no ``sources`` at all — no AniList id, no MAL id.
#: Deliberately in the fixture: a title Arc can never address is a card that
#: leads nowhere, and it has to be dropped rather than shown.
UNADDRESSABLE_TITLE = "Mushoku Tensei: Jobless Reincarnation Season 3"

#: The season the slice has more than one entry in.
SUMMER_2026 = (2026, "SUMMER")


# --- Loading the slices -----------------------------------------------------


async def load_slices(session: AsyncSession) -> None:
    """Both public files into the offline tables, through the real importer."""
    await import_manami(session, MANAMI_SLICE, checksum="manami-slice")
    await import_fribb(session, FRIBB_SLICE, version="slice", checksum="fribb-slice")


@pytest.fixture
async def offline(db_session: AsyncSession) -> AsyncSession:
    """A session whose offline tables hold the slices."""
    await load_slices(db_session)
    return db_session


async def offline_rows(factory: SessionFactory) -> None:
    """The same, for the tests that go through HTTP and commit for real."""
    async with factory() as session:
        await load_slices(session)


async def titles(rows: list[OfflineAnime]) -> list[str]:
    return [row.title for row in rows]


# --- offline_search: the matching rules -------------------------------------


async def test_every_word_must_appear_somewhere_in_the_names(offline: AsyncSession) -> None:
    """``local.py``'s rule 1, over the title *and* the synonyms.

    "jobless frieren" is two words that each match a show, and no show that
    both match.
    """
    assert await offline_search(offline, "mushoku tensei")
    assert await offline_search(offline, "jobless reincarnation")  # a synonym only
    assert await offline_search(offline, "jobless frieren") == []


async def test_a_synonym_finds_a_show_the_title_never_would(offline: AsyncSession) -> None:
    """The reason the dataset is worth importing.

    manami publishes one title per entry and it is the romaji one, so Frieren's
    English name is a synonym. A rule that read titles only would rank the show
    nowhere for the name most people type.
    """
    rows = await offline_search(offline, "beyond journey's end")

    assert [row.anilist_id for row in rows] == [FRIEREN_ANILIST]


async def test_an_exact_name_leads_a_higher_scoring_one(offline: AsyncSession) -> None:
    """Rank before score: what you typed beats what is popular.

    "Vinland Saga" is exactly the first season's name and only a prefix of the
    second's — which the dataset scores fractionally higher. Season one has to
    win anyway, because it is the thing that was typed.
    """
    rows = await offline_search(offline, "vinland saga")

    assert [row.season_year for row in rows] == [2019, 2023]
    assert rows[0].score is not None and rows[1].score is not None
    assert rows[0].score < rows[1].score  # …and it won despite the score


async def test_a_prefix_leads_anything_the_words_merely_appear_in(
    offline: AsyncSession,
) -> None:
    """The middle rank, which no pair in the slice happens to isolate."""
    await offline.execute(
        OfflineAnime.__table__.insert(),
        [
            {
                "title": "Alpha Marker",
                "search_text": "alpha marker",
                "score": 9.0,
                "anilist_id": 900011,
            },
            {
                "title": "Marker Alpha",
                "search_text": "marker alpha",
                "score": 1.0,
                "anilist_id": 900012,
            },
        ],
    )
    await offline.flush()

    rows = await offline_search(offline, "marker")

    assert [row.title for row in rows] == ["Marker Alpha", "Alpha Marker"]


async def test_every_exact_holder_leads_and_then_the_score_decides(
    offline: AsyncSession,
) -> None:
    """Four entries of one franchise list "Mushoku Tensei" as a name.

    All four rank above the ones that merely start with it, and among the four
    the dataset's own score is the order.
    """
    rows = await offline_search(offline, "Mushoku Tensei")

    exact = [row for row in rows[:4]]
    assert {row.anilist_id for row in exact} == {MUSHOKU_1_ANILIST, 127720, 146065, 166873}
    scores = [row.score for row in exact]
    assert scores == sorted(scores, reverse=True)
    assert MUSHOKU_3_ANILIST in [row.anilist_id for row in rows[4:]]


async def test_score_orders_what_the_rank_ties(offline: AsyncSession) -> None:
    """Two entries that are both merely "contains", ordered by the dataset."""
    rows = await offline_search(offline, "isekai ittara honki dasu")

    scores = [row.score for row in rows if row.score is not None]
    assert scores == sorted(scores, reverse=True)


async def test_the_type_breaks_a_tie_the_score_cannot(offline: AsyncSession) -> None:
    """An unscored entry is not the end of the ordering.

    Both of these are UPCOMING with no score at all, so ``score`` ties and the
    TV entry has to come before the OVA on the type rank alone.
    """
    await offline.execute(
        OfflineAnime.__table__.insert(),
        [
            {
                "title": "Tie Breaker",
                "search_text": "tie breaker",
                "type": "OVA",
                "anilist_id": 900001,
            },
            {
                "title": "Tie Breaker",
                "search_text": "tie breaker",
                "type": "TV",
                "anilist_id": 900002,
            },
        ],
    )
    await offline.flush()

    rows = await offline_search(offline, "tie breaker")

    assert [row.type for row in rows] == ["TV", "OVA"]


async def test_like_metacharacters_in_the_query_are_escaped(offline: AsyncSession) -> None:
    """Without the escape, ``%`` and ``_`` would match the whole table."""
    assert await offline_search(offline, "%") == []
    assert await offline_search(offline, "_") == []
    assert await offline_search(offline, "one_piece") == []
    assert [row.anilist_id for row in await offline_search(offline, "one piece")] == [
        ONE_PIECE_ANILIST
    ]


async def test_a_blank_query_and_an_empty_table_both_answer_nothing(
    offline: AsyncSession, db_session: AsyncSession
) -> None:
    """A deployment that has never imported must degrade, not fail."""
    assert await offline_search(offline, "   ") == []

    await offline.execute(OfflineAnime.__table__.delete())
    await offline.flush()
    assert await offline_search(offline, "frieren") == []


async def test_the_limit_is_honoured(offline: AsyncSession) -> None:
    assert len(await offline_search(offline, "mushoku", limit=2)) == 2
    assert await offline_search(offline, "mushoku", limit=0) == []


# --- offline_season ---------------------------------------------------------


async def test_the_season_is_every_type_of_that_season(offline: AsyncSession) -> None:
    year, season = SUMMER_2026
    media = await offline_season(offline, year, season)

    assert media
    assert all(item.season == season and item.season_year == year for item in media)
    assert all(item.source == "offline" for item in media)
    # No air times anywhere in it: the dataset has none, and that is the
    # documented cost of the fallback (FR-C7).
    assert all(item.next_airing is None and not item.airing for item in media)


async def test_an_empty_season_is_an_empty_list(offline: AsyncSession) -> None:
    assert await offline_season(offline, 1066, "WINTER") == []


async def test_the_season_name_is_matched_case_insensitively(offline: AsyncSession) -> None:
    year, season = SUMMER_2026
    assert len(await offline_season(offline, year, season.lower())) == len(
        await offline_season(offline, year, season)
    )


# --- The id map -------------------------------------------------------------


async def test_the_map_answers_in_both_directions(offline: AsyncSession) -> None:
    assert await anilist_for_mal(offline, MUSHOKU_1_MAL) == MUSHOKU_1_ANILIST
    assert await mal_for_anilist(offline, MUSHOKU_1_ANILIST) == MUSHOKU_1_MAL


async def test_an_id_nobody_has_heard_of_is_a_miss(offline: AsyncSession) -> None:
    assert await anilist_for_mal(offline, 999_999) is None
    assert await mal_for_anilist(offline, 999_999) is None


async def test_lookup_ids_hands_back_the_whole_mapping_row(offline: AsyncSession) -> None:
    """What the TMDB enrichment reads: the id map's record, not one id."""
    row = await lookup_ids(offline, mal_id=MUSHOKU_3_MAL)

    assert row is not None
    assert (row.anilist_id, row.tmdb_tv_id, row.tmdb_season) == (MUSHOKU_3_ANILIST, 94664, 3)


async def test_fill_missing_ids_fills_only_the_gaps(offline: AsyncSession) -> None:
    filled = await fill_missing_ids(
        offline,
        [
            (None, MUSHOKU_1_MAL),  # MAL only: gains the AniList id
            (FRIEREN_ANILIST, None),  # AniList only: gains the MAL id
            (MUSHOKU_3_ANILIST, 1),  # both known: a disagreement is left alone
            (None, None),  # nothing to go on
            (None, 999_999),  # no mapping
        ],
    )

    assert filled == [
        (MUSHOKU_1_ANILIST, MUSHOKU_1_MAL),
        (FRIEREN_ANILIST, FRIEREN_MAL),
        (MUSHOKU_3_ANILIST, 1),
        (None, None),
        (None, 999_999),
    ]


async def test_the_map_declines_to_answer_when_two_entries_disagree(
    offline: AsyncSession,
) -> None:
    """Somebody else's file, so a duplicate is a row and not a failed import.

    Two answers is worse than none: attaching one of them would re-point a
    user's list entries and episodes at the wrong show.
    """
    await offline.execute(
        OfflineAnime.__table__.insert(),
        [{"title": "Impostor", "search_text": "impostor", "mal_id": FRIEREN_MAL, "anilist_id": 7}],
    )
    await offline.flush()

    # Fribb still has exactly one row for this MAL id, and Fribb is asked
    # first — so the manami duplicate never gets a vote.
    assert await anilist_for_mal(offline, FRIEREN_MAL) == FRIEREN_ANILIST


async def test_a_manami_duplicate_with_no_fribb_row_is_a_miss(offline: AsyncSession) -> None:
    await offline.execute(
        OfflineAnime.__table__.insert(),
        [
            {"title": "A", "search_text": "a", "mal_id": 555_001, "anilist_id": 1},
            {"title": "B", "search_text": "b", "mal_id": 555_001, "anilist_id": 2},
        ],
    )
    await offline.flush()

    assert await anilist_for_mal(offline, 555_001) is None


# --- Materialising ----------------------------------------------------------


async def test_an_offline_row_becomes_a_summary_anime_row(offline: AsyncSession) -> None:
    rows = await offline_search(offline, "sousou no frieren")
    anime = await upsert_offline_summaries(offline, rows)

    assert len(anime) == 1
    row = anime[0]
    assert (row.anilist_id, row.mal_id) == (FRIEREN_ANILIST, FRIEREN_MAL)
    # The title goes to romaji and nothing guesses at an English one.
    assert row.title_romaji == "Sousou no Frieren"
    assert row.title_english is None
    assert (row.format, row.episodes, row.status) == ("TV", 28, "FINISHED")
    assert (row.season, row.season_year) == ("FALL", 2023)
    assert row.cover_url is not None
    assert row.average_score == 91
    assert row.summary_source == "offline"
    assert row.studio == "Madhouse Inc."
    assert "Frieren: Beyond Journey’s End" in (row.synonyms or [])
    # …and it is still a row nothing has ever fetched, so opening it fetches.
    assert row.refreshed_at is None
    assert row.detail_source is None


async def test_manami_status_words_become_anilist_ones(offline: AsyncSession) -> None:
    rows = {row.title: row for row in await offline_search(offline, "mushoku tensei")}
    upcoming = next(row for row in rows.values() if row.status == "UPCOMING")

    assert media_from(upcoming).status == "NOT_YET_RELEASED"
    assert media_from(next(row for row in rows.values() if row.status == "FINISHED")).status == (
        "FINISHED"
    )


async def test_an_uncounted_show_gets_a_null_episode_count(offline: AsyncSession) -> None:
    """``episodes: 0`` is "nobody has announced one", not "there are none"."""
    row = OfflineAnime(title="x", search_text="x", episodes=0, anilist_id=1)

    assert media_from(row).episodes is None


async def test_a_title_no_source_can_address_is_dropped(offline: AsyncSession) -> None:
    rows = await offline_search(offline, "jobless reincarnation season 3")
    assert UNADDRESSABLE_TITLE in await titles(rows)

    anime = await upsert_offline_summaries(offline, rows)

    assert UNADDRESSABLE_TITLE not in [row.title_romaji for row in anime]


# --- Cache precedence -------------------------------------------------------


async def test_anilist_overwrites_everything_the_offline_import_wrote(
    offline: AsyncSession,
) -> None:
    """Rule 3, with the offline import as the weakest source."""
    rows = await offline_search(offline, "sousou no frieren")
    [materialised] = await upsert_offline_summaries(offline, rows)

    [live] = await upsert_summaries(
        offline,
        [
            CatalogMedia(
                source="anilist",
                anilist_id=FRIEREN_ANILIST,
                mal_id=FRIEREN_MAL,
                title=MediaTitle(romaji="Sousou no Frieren", english="Frieren"),
                episodes=28,
                cover_url="https://anilist/cover.jpg",
                average_score=94,
            )
        ],
    )

    assert live.id == materialised.id  # one show, one row
    assert live.title_english == "Frieren"
    assert live.cover_url == "https://anilist/cover.jpg"
    assert live.average_score == 94
    assert live.summary_source == "anilist"


async def test_the_offline_import_never_overwrites_a_live_answer(
    offline: AsyncSession,
) -> None:
    [live] = await upsert_summaries(
        offline,
        [
            CatalogMedia(
                source="mal",
                mal_id=FRIEREN_MAL,
                anilist_id=FRIEREN_ANILIST,
                title=MediaTitle(romaji="Sousou no Frieren"),
                cover_url="https://mal/cover.jpg",
                episodes=28,
            )
        ],
    )

    rows = await offline_search(offline, "sousou no frieren")
    [materialised] = await upsert_offline_summaries(offline, rows)

    assert materialised.id == live.id
    assert materialised.cover_url == "https://mal/cover.jpg"
    assert materialised.summary_source == "mal"


async def test_a_mal_payload_lands_on_the_row_anilist_made(offline: AsyncSession) -> None:
    """FR-C6, without asking anybody: the id map closes the loop.

    MAL's payloads never carry an AniList id, so before M15.5 this produced a
    second row for the same show and waited for ``catalog_reconcile`` — over
    the network, from the source that was down.
    """
    [first] = await upsert_summaries(
        offline,
        [
            CatalogMedia(
                source="anilist",
                anilist_id=MUSHOKU_1_ANILIST,
                title=MediaTitle(romaji="Mushoku Tensei"),
            )
        ],
    )

    [second] = await upsert_summaries(
        offline,
        [CatalogMedia(source="mal", mal_id=MUSHOKU_1_MAL, title=MediaTitle(romaji="Mushoku"))],
    )

    assert second.id == first.id
    assert (second.anilist_id, second.mal_id) == (MUSHOKU_1_ANILIST, MUSHOKU_1_MAL)
    assert await offline.scalar(select(func.count()).select_from(Anime)) == 1


async def test_an_anilist_payload_lands_on_the_row_mal_made(offline: AsyncSession) -> None:
    [first] = await upsert_summaries(
        offline,
        [CatalogMedia(source="mal", mal_id=MUSHOKU_1_MAL, title=MediaTitle(romaji="Mushoku"))],
    )

    [second] = await upsert_summaries(
        offline,
        [
            CatalogMedia(
                source="anilist",
                anilist_id=MUSHOKU_1_ANILIST,
                title=MediaTitle(romaji="Mushoku Tensei"),
            )
        ],
    )

    assert second.id == first.id
    assert (second.anilist_id, second.mal_id) == (MUSHOKU_1_ANILIST, MUSHOKU_1_MAL)


async def test_the_map_never_overwrites_an_id_a_source_stated(offline: AsyncSession) -> None:
    """A source's word about itself beats a third party's file."""
    [row] = await upsert_summaries(
        offline,
        [
            CatalogMedia(
                source="anilist",
                anilist_id=MUSHOKU_1_ANILIST,
                mal_id=12345,
                title=MediaTitle(romaji="Mushoku Tensei"),
            )
        ],
    )

    assert row.mal_id == 12345


# --- catalog_reconcile ------------------------------------------------------


def context(session: AsyncSession, settings: Settings, job_type: str) -> JobContext:
    job = Job(id=1, type=job_type, payload={}, status=JobStatus.RUNNING)
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("test.offline")
    )


@pytest.fixture
def no_catalogue(monkeypatch: pytest.MonkeyPatch) -> Iterator[CatalogService]:
    """Every job's ``catalog_for`` yields a service whose sources both fail."""
    service = CatalogService(
        FakeMal(fail_with=503).source(), FakeMal(fail_with=503).source(), Breaker(300.0)
    )

    @asynccontextmanager
    async def fake_catalog_for(settings: Settings) -> Any:
        yield service

    monkeypatch.setattr("arc.services.catalog.jobs.catalog_for", fake_catalog_for)
    yield service


async def test_reconcile_fills_ids_from_the_map_with_no_network(
    offline: AsyncSession, settings: Settings, no_catalogue: CatalogService
) -> None:
    """The pass that runs during the outage, not after it.

    Both sources are failing, so the AniList half of the job does nothing at
    all — and the row is placed anyway.
    """
    row = Anime(mal_id=MUSHOKU_1_MAL, title_romaji="Mushoku Tensei")
    offline.add(row)
    await offline.flush()

    await catalog_jobs.catalog_reconcile(context(offline, settings, RECONCILE))

    refreshed = await offline.get(Anime, row.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.anilist_id == MUSHOKU_1_ANILIST


async def test_reconcile_leaves_an_id_another_row_already_holds(
    offline: AsyncSession, settings: Settings, no_catalogue: CatalogService
) -> None:
    """Two rows for one show is a mess; the unique index is not the place to find out."""
    offline.add(Anime(anilist_id=MUSHOKU_1_ANILIST, title_romaji="Mushoku Tensei"))
    orphan = Anime(mal_id=MUSHOKU_1_MAL, title_romaji="Mushoku Tensei (MAL)")
    offline.add(orphan)
    await offline.flush()

    await catalog_jobs.catalog_reconcile(context(offline, settings, RECONCILE))

    refreshed = await offline.get(Anime, orphan.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.anilist_id is None


async def test_reconcile_leaves_a_row_the_map_has_never_heard_of(
    offline: AsyncSession, settings: Settings, no_catalogue: CatalogService
) -> None:
    row = Anime(mal_id=999_999, title_romaji="Not In The Files")
    offline.add(row)
    await offline.flush()

    await catalog_jobs.catalog_reconcile(context(offline, settings, RECONCILE))

    refreshed = await offline.get(Anime, row.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.anilist_id is None


# --- catalog_season_sweep ---------------------------------------------------


async def test_the_season_sweep_seeds_from_the_import_when_both_sources_fail(
    offline: AsyncSession,
    settings: Settings,
    no_catalogue: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-C7: a day when nobody answers costs the air times, not the season."""
    year, season = SUMMER_2026
    monkeypatch.setattr("arc.services.catalog.jobs.current_season", lambda: (year, season))

    await catalog_jobs.catalog_season_sweep(context(offline, settings, SEASON_SWEEP))

    rows = list(
        (
            await offline.scalars(
                select(Anime).where(Anime.season == season, Anime.season_year == year)
            )
        ).all()
    )
    assert rows
    assert all(row.summary_source == "offline" for row in rows)
    # Listed, but unplaced: the import knows of no broadcast at all.
    assert all(row.next_airing is None for row in rows)


async def test_a_season_the_import_has_never_heard_of_is_skipped_quietly(
    offline: AsyncSession,
    settings: Settings,
    no_catalogue: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("arc.services.catalog.jobs.current_season", lambda: (1066, "WINTER"))

    await catalog_jobs.catalog_season_sweep(context(offline, settings, SEASON_SWEEP))

    assert await offline.scalar(select(func.count()).select_from(Anime)) == 0


# --- The matcher ------------------------------------------------------------


@pytest.fixture
def dead_catalog() -> CatalogService:
    """A catalogue that cannot answer anything, for the matcher tests."""
    return CatalogService(
        FakeMal(fail_with=503).source(), FakeMal(fail_with=503).source(), Breaker(300.0)
    )


async def test_the_matcher_finds_a_release_name_only_a_synonym_holds(
    offline: AsyncSession, dead_catalog: CatalogService
) -> None:
    """The case M15.5 was written for.

    "Mushoku Tensei S3" is exactly one of manami's synonyms for the third
    season and is a string no live search matches — and here the live search
    could not be asked anyway.
    """
    parsed = parse("[SubsPlease] Mushoku Tensei S3 - 10 (1080p) [ABCD1234].mkv")

    result = await match(offline, dead_catalog, parsed, minimum=0.40)

    best = result.best
    assert best is not None
    assert best.episode_number == 10
    row = await offline.get(Anime, best.anime_id)
    assert row is not None
    assert row.anilist_id == MUSHOKU_3_ANILIST


async def test_an_offline_candidate_is_a_real_anime_row(
    offline: AsyncSession, dead_catalog: CatalogService
) -> None:
    """A candidate is addressed by Arc's id, so the row has to exist."""
    parsed = parse("[Group] Sousou no Frieren - 05 [1080p].mkv")

    result = await match(offline, dead_catalog, parsed, minimum=0.40)

    best = result.best
    assert best is not None
    row = await offline.get(Anime, best.anime_id)
    assert row is not None
    assert row.summary_source == "offline"
    assert "offline" in " ".join(best.reasons)


async def test_the_matcher_still_asks_the_catalogue_when_the_import_has_nothing(
    db_session: AsyncSession, dead_catalog: CatalogService
) -> None:
    """No import, no offline hits: the live path is exactly what it was."""
    parsed = parse("[Group] Sousou no Frieren - 05 [1080p].mkv")

    result = await match(db_session, dead_catalog, parsed, minimum=0.40)

    assert result.best is None  # the live search failed; nothing was invented


async def test_a_below_threshold_offline_hit_is_still_a_review(
    offline: AsyncSession, dead_catalog: CatalogService
) -> None:
    """The non-negotiable: below the threshold it goes to review, never links."""
    parsed = parse("[Group] Completely Unrelated Nonsense - 03 [1080p].mkv")

    result = await match(offline, dead_catalog, parsed, minimum=0.40)

    assert not result.auto_links(0.85, min_title=0.70)


# --- The search endpoint ----------------------------------------------------


@pytest.fixture
def blocked_app(api_app: FastAPI) -> FastAPI:
    """The real API with both catalogue sources refusing to answer."""
    api_app.state.catalog = CatalogService(
        FakeMal(fail_with=503).source(), FakeMal(fail_with=503).source(), Breaker(300.0)
    )
    return api_app


@pytest.fixture
async def user_client(
    blocked_app: FastAPI, api_factory: SessionFactory
) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(blocked_app) as client:
        yield await login(client, USER_EMAIL, USER_PASSWORD)


async def test_search_answers_from_the_import_with_both_sources_down(
    user_client: AsyncClient, api_factory: SessionFactory, slept: list[float]
) -> None:
    """M15.5's definition of done: a known title is still findable."""
    await offline_rows(api_factory)

    response = await user_client.get("/api/anime/search", params={"q": "jobless reincarnation"})

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert results
    assert results[0]["anilist_id"] == MUSHOKU_1_ANILIST
    assert results[0]["source"] == "offline"
    assert results[0]["id"] > 0  # an internal id its card can link to


async def test_searching_twice_reuses_the_rows_rather_than_duplicating_them(
    user_client: AsyncClient, api_factory: SessionFactory, slept: list[float]
) -> None:
    await offline_rows(api_factory)

    first = await user_client.get("/api/anime/search", params={"q": "mushoku tensei"})
    second = await user_client.get("/api/anime/search", params={"q": "mushoku tensei"})

    assert first.status_code == second.status_code == 200
    assert [row["id"] for row in first.json()["results"]] == [
        row["id"] for row in second.json()["results"]
    ]
    async with api_factory() as session:
        rows = list((await session.scalars(select(Anime))).all())
    assert len({row.id for row in rows}) == len(rows)
    assert len({row.anilist_id for row in rows if row.anilist_id}) == len(
        [row for row in rows if row.anilist_id]
    )


async def test_the_second_search_serves_the_rows_as_local_hits(
    user_client: AsyncClient, api_factory: SessionFactory, slept: list[float]
) -> None:
    """Materialising is what feeds the *first* tier next time round."""
    await offline_rows(api_factory)
    await user_client.get("/api/anime/search", params={"q": "frieren"})

    response = await user_client.get("/api/anime/search", params={"q": "frieren"})

    assert response.status_code == 200
    assert [row["anilist_id"] for row in response.json()["results"]] == [FRIEREN_ANILIST]


async def test_nothing_anywhere_with_the_catalogue_down_is_still_a_502(
    user_client: AsyncClient, api_factory: SessionFactory, slept: list[float]
) -> None:
    """ "No results" and "the catalogue is down" must not look the same."""
    await offline_rows(api_factory)

    response = await user_client.get("/api/anime/search", params={"q": "zzzznothing"})

    assert response.status_code == 502
    assert response.json()["detail"] == "catalogue is unavailable"


async def test_opening_an_offline_result_serves_it_rather_than_502ing(
    user_client: AsyncClient, api_factory: SessionFactory, slept: list[float]
) -> None:
    """Rule 5, stale beats absent — for a row that has never been fetched."""
    await offline_rows(api_factory)
    search = await user_client.get("/api/anime/search", params={"q": "sousou no frieren"})
    anime_id = search.json()["results"][0]["id"]

    response = await user_client.get(f"/api/anime/{anime_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"]["romaji"] == "Sousou no Frieren"
    assert body["episode_count"] == 28


async def test_an_offline_row_is_upgraded_by_the_first_live_answer(
    user_client: AsyncClient, api_factory: SessionFactory, slept: list[float]
) -> None:
    """The whole point of writing as the weakest source."""
    await offline_rows(api_factory)
    search = await user_client.get("/api/anime/search", params={"q": "sousou no frieren"})
    anime_id = search.json()["results"][0]["id"]

    async with api_factory() as session:
        row = await upsert_detail(
            session,
            CatalogMedia(
                source="anilist",
                anilist_id=FRIEREN_ANILIST,
                mal_id=FRIEREN_MAL,
                title=MediaTitle(romaji="Sousou no Frieren", english="Frieren"),
                description="A real synopsis.",
                episodes=28,
                full=True,
            ),
        )
        assert row.id == anime_id  # the same row, not a second one
        await session.commit()

    response = await user_client.get(f"/api/anime/{anime_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"]["english"] == "Frieren"
    assert body["synopsis"] == "A real synopsis."
