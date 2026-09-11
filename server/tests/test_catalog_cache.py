"""The local catalogue: upserts, identity, episode sync, and the freshness rule.

These are the rules that decide what Arc keeps when a source and the database
disagree — and, since M3b, when the two *sources* disagree — so they are tested
against a real Postgres (the upserts are ``ON CONFLICT`` statements and the
identity rules lean on two nullable unique indexes; SQLite would prove nothing
about either).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from arc.models import Anime, Episode, EpisodeState
from arc.services.anilist import parse_media
from arc.services.anilist.source import AniListSource
from arc.services.catalog import (
    AiringEntry,
    Breaker,
    CatalogMedia,
    CatalogService,
    MediaTitle,
    SourceNotFound,
    SourceUnavailable,
    cache,
    ensure_anime,
    episodes_for,
    preferred_title,
    sync_episodes,
    upsert_detail,
    upsert_summaries,
)
from arc.services.mal.catalog import parse_anime
from tests.anilist_mock import (
    FRIEREN_ID,
    LONG_RUNNING_AIRED,
    LONG_RUNNING_ID,
    RELEASING_ID,
    FakeAniList,
    frieren_fake,
    load,
    long_running_fake,
    media_payload,
)
from tests.mal_mock import FRIEREN_MAL_ID, FakeMal, anime_payload
from tests.mal_mock import frieren_fake as mal_frieren_fake
from tests.mal_mock import load as mal_load

pytestmark = pytest.mark.pg


def schedule(*pairs: tuple[int, datetime]) -> list[AiringEntry]:
    return [AiringEntry(episode=number, at=at) for number, at in pairs]


def weekly(count: int, *, start: datetime) -> list[AiringEntry]:
    return schedule(*((n, start + timedelta(weeks=n - 1)) for n in range(1, count + 1)))


def estimated(entries: list[AiringEntry]) -> list[AiringEntry]:
    """The same schedule, marked as a MAL-style guess."""
    return [AiringEntry(episode=e.episode, at=e.at, estimated=True) for e in entries]


def anilist_detail() -> CatalogMedia:
    """Frieren, as the captured AniList detail response."""
    return parse_media(media_payload("media_154587"), full=True)


def mal_detail() -> CatalogMedia:
    """The same show, as the captured MAL detail response."""
    return parse_anime(anime_payload(), full=True)


def catalog_over(anilist: FakeAniList, mal: FakeMal | None = None) -> CatalogService:
    """A service over the fakes, with a breaker that never reopens by itself."""
    fallback = (mal or mal_frieren_fake()).source()
    return CatalogService(anilist.source(), fallback, Breaker(300.0))


async def frieren(session: AsyncSession) -> Anime:
    """Frieren, upserted from the captured AniList detail response."""
    return await upsert_detail(session, anilist_detail())


# --- upsert_detail ----------------------------------------------------------


async def test_upsert_writes_every_column_from_a_detail_fetch(db_session: AsyncSession) -> None:
    anime = await frieren(db_session)

    assert anime.id > 0  # Arc's own, not AniList's
    assert anime.anilist_id == FRIEREN_ID
    assert anime.mal_id == FRIEREN_MAL_ID
    assert anime.summary_source == "anilist"
    assert anime.detail_source == "anilist"
    assert anime.title_romaji == "Sousou no Frieren"
    assert anime.title_english == "Frieren: Beyond Journey’s End"
    assert anime.title_native == "葬送のフリーレン"
    assert (anime.format, anime.episodes, anime.status) == ("TV", 28, "FINISHED")
    assert (anime.season, anime.season_year) == ("FALL", 2023)
    assert anime.studio == "MADHOUSE"
    assert anime.genres == ["Adventure", "Drama", "Fantasy"]
    assert anime.synonyms is not None and "Frieren at the Funeral" in anime.synonyms
    assert anime.tags is not None and anime.tags[0]["name"] == "Travel"
    assert anime.refreshed_at is not None
    # The synopsis arrives as HTML and is stored as text.
    assert anime.description is not None
    assert "<br>" not in anime.description
    assert anime.description.startswith("The adventure is over but life goes on")


async def test_the_internal_id_is_not_the_anilist_id(db_session: AsyncSession) -> None:
    """The whole reason for M3b's schema change (FR-C6).

    A row's identity has to survive gaining and losing external ids, so it
    cannot *be* one of them.
    """
    anime = await frieren(db_session)

    assert anime.id != anime.anilist_id
    assert anime.id != anime.mal_id


async def test_relations_are_stored_with_both_external_ids(db_session: AsyncSession) -> None:
    anime = await frieren(db_session)

    assert anime.relations is not None
    sequel = next(r for r in anime.relations if r["relation_type"] == "SEQUEL")
    assert sequel["anilist_id"] == 182255
    assert sequel["mal_id"] == 59978
    assert sequel["title"]["preferred"] == "Frieren: Beyond Journey’s End Season 2"
    # The source manga was dropped by the parser, not stored and filtered later.
    assert {r["relation_type"] for r in anime.relations} == {
        "SEQUEL",
        "SIDE_STORY",
        "CHARACTER",
        "OTHER",
    }


async def test_upsert_is_idempotent(db_session: AsyncSession) -> None:
    first = await frieren(db_session)
    first_refreshed = first.refreshed_at
    second = await frieren(db_session)

    assert second.id == first.id
    assert second.refreshed_at is not None and first_refreshed is not None
    assert second.refreshed_at >= first_refreshed
    rows = list((await db_session.scalars(select(Anime))).all())
    assert len(rows) == 1


async def test_a_search_result_does_not_blank_a_detail_fetch(db_session: AsyncSession) -> None:
    """The rule the whole ``full`` flag exists for.

    Searching for a show already on the user's list must not cost it its
    synopsis, relations and freshness — which is exactly what a naive upsert
    of the summary-only search response would do.
    """
    detailed = await frieren(db_session)
    refreshed_at = detailed.refreshed_at
    assert refreshed_at is not None

    summary_raw = load("search_frieren")["data"]["Page"]["media"][0]
    # Pretend AniList renamed it between the two calls, so the summary
    # columns are visibly written.
    summary_raw = {**summary_raw, "episodes": 29}
    (after,) = await upsert_summaries(db_session, [parse_media(summary_raw, full=False)])

    assert after.id == detailed.id
    assert after.episodes == 29  # summary columns are written…
    assert after.description is not None  # …and detail columns are not touched
    assert after.relations
    assert after.studio == "MADHOUSE"
    assert after.refreshed_at == refreshed_at  # so the row is still "stale"


async def test_preferred_title_uses_the_cached_row(db_session: AsyncSession) -> None:
    anime = await frieren(db_session)
    assert preferred_title(anime) == "Frieren: Beyond Journey’s End"

    anime.title_english = None
    assert preferred_title(anime) == "Sousou no Frieren"


# --- Identity across the two sources ----------------------------------------


async def test_a_mal_first_row_gains_its_anilist_id_and_stays_one_row(
    db_session: AsyncSession,
) -> None:
    """The FR-C6 promise: a show first seen through MAL is the same show later.

    This is the order an outage produces — MAL creates the row, AniList comes
    back — and the AniList payload's ``idMal`` is what joins them.
    """
    mal_row = await upsert_detail(db_session, mal_detail())
    assert mal_row.anilist_id is None
    assert mal_row.mal_id == FRIEREN_MAL_ID
    assert mal_row.detail_source == "mal"
    internal_id = mal_row.id

    anilist_row = await upsert_detail(db_session, anilist_detail())

    assert anilist_row.id == internal_id  # the same row, not a second one
    assert anilist_row.anilist_id == FRIEREN_ID
    assert anilist_row.mal_id == FRIEREN_MAL_ID
    assert anilist_row.detail_source == "anilist"
    assert anilist_row.summary_source == "anilist"
    assert len(list((await db_session.scalars(select(Anime))).all())) == 1


async def test_anilist_overwrites_the_detail_a_mal_row_had(db_session: AsyncSession) -> None:
    mal_row = await upsert_detail(db_session, mal_detail())
    mal_synopsis = mal_row.description
    assert mal_synopsis is not None and mal_synopsis.startswith("During their decade-long")

    upgraded = await upsert_detail(db_session, anilist_detail())

    assert upgraded.description is not None
    assert upgraded.description.startswith("The adventure is over but life goes on")
    assert upgraded.tags  # MAL has none; AniList's arrive
    assert upgraded.banner_url is not None


async def test_mal_never_overwrites_an_anilist_sourced_detail(
    db_session: AsyncSession,
) -> None:
    """The reverse order: MAL may complete an AniList row, never rewrite it.

    Its synopsis is a different translation and it has no tags or banner, so
    letting it win would make a five-minute outage cost a day of worse data.
    """
    anilist_row = await frieren(db_session)
    internal_id = anilist_row.id
    synopsis = anilist_row.description
    tags = anilist_row.tags
    banner = anilist_row.banner_url

    after = await upsert_detail(db_session, mal_detail())

    assert after.id == internal_id
    assert after.description == synopsis
    assert after.tags == tags
    assert after.banner_url == banner
    assert after.detail_source == "anilist"  # not downgraded
    assert len(list((await db_session.scalars(select(Anime))).all())) == 1


async def test_mal_fills_a_detail_column_that_is_null(db_session: AsyncSession) -> None:
    """ "Never overwrite" is not "never write": a null is not data to protect."""
    anilist_row = await frieren(db_session)
    anilist_row.studio = None
    await db_session.flush()

    after = await upsert_detail(db_session, mal_detail())

    assert after.studio == "Madhouse"  # MAL's spelling, since AniList had none
    assert after.detail_source == "anilist"


async def test_mal_does_not_null_a_summary_column_an_anilist_row_has(
    db_session: AsyncSession,
) -> None:
    """The other half of "weaker data never overwrites stronger data".

    Summary columns *are* overwritten — a renamed show has to land — but a null
    is not a correction. MAL sending ``num_episodes: 0`` (which it does for a
    show it has not counted yet) must not blank the 28 AniList published:
    ``sync_episodes`` reads that column, so a blanked count is 28 missing
    episode rows and 28 lost wants.
    """
    anilist_row = await frieren(db_session)
    assert anilist_row.episodes == 28

    thin = replace(
        mal_detail(),
        title=MediaTitle(),
        episodes=None,
        status=None,
        season=None,
        cover_url=None,
    )
    after = await upsert_detail(db_session, thin)

    assert after.id == anilist_row.id
    assert after.episodes == 28
    assert after.status == "FINISHED"
    assert after.season == "FALL"
    assert after.cover_url is not None
    assert after.title_english == "Frieren: Beyond Journey’s End"
    assert after.detail_source == "anilist"


async def test_a_row_created_between_the_lookup_and_the_insert_is_used(
    pg_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """The race the single-column ``ON CONFLICT`` arbiter cannot cover.

    Two writers, one show: a reconciliation job creates the MAL-only row while
    a search is between its ``_find_rows`` and its insert. The insert is
    grouped on ``anilist_id`` — which does not conflict — and violates the
    ``mal_id`` index instead. The savepoint retry has to turn that into the
    ordinary update path rather than a 500.

    Real, committing sessions: a rolled-back fixture transaction cannot show
    one session another's committed row, which is the whole mechanism here.
    """
    media = anilist_detail()
    competitor_seen = False
    real_find_rows = cache._find_rows

    async def racing_find_rows(
        session: AsyncSession, items: list[CatalogMedia]
    ) -> tuple[dict[int, Anime], dict[int, Anime]]:
        nonlocal competitor_seen
        found = await real_find_rows(session, items)
        if not competitor_seen:
            competitor_seen = True
            async with AsyncSession(pg_engine, expire_on_commit=False) as other:
                other.add(Anime(mal_id=FRIEREN_MAL_ID, title_romaji="MAL got there first"))
                await other.commit()
        return found

    try:
        with pytest.MonkeyPatch.context() as patch, caplog.at_level("INFO", logger=cache.__name__):
            patch.setattr(cache, "_find_rows", racing_find_rows)
            async with AsyncSession(pg_engine, expire_on_commit=False) as session:
                row = await upsert_detail(session, media)
                await session.commit()
                row_id, anilist_id, mal_id = row.id, row.anilist_id, row.mal_id

        async with AsyncSession(pg_engine, expire_on_commit=False) as check:
            stored = list((await check.scalars(select(Anime))).all())
    finally:
        async with pg_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM anime WHERE anilist_id = :a OR mal_id = :m"),
                {"a": FRIEREN_ID, "m": FRIEREN_MAL_ID},
            )

    assert competitor_seen  # the race really happened…
    # …and the insert really lost it, rather than the test proving nothing.
    assert "created concurrently" in caplog.text
    assert len(stored) == 1  # one show, one row
    assert stored[0].id == row_id
    assert (anilist_id, mal_id) == (FRIEREN_ID, FRIEREN_MAL_ID)
    assert stored[0].title_romaji == "Sousou no Frieren"  # the payload won
    assert stored[0].detail_source == "anilist"


async def test_a_row_keeps_an_external_id_it_already_has(db_session: AsyncSession) -> None:
    """Changing a known id would re-point every list entry of one show at another."""
    row = await frieren(db_session)
    row.mal_id = 111111
    await db_session.flush()

    after = await upsert_detail(db_session, anilist_detail())

    assert after.id == row.id
    assert after.mal_id == 111111  # not replaced by the payload's 52991


async def test_a_mal_only_row_is_the_row_an_anilist_payload_lands_on(
    db_session: AsyncSession,
) -> None:
    """Matching is by *either* id, so ``idMal`` alone is enough to join them."""
    existing = Anime(mal_id=FRIEREN_MAL_ID, title_romaji="Placeholder")
    db_session.add(existing)
    await db_session.flush()

    row = await upsert_detail(db_session, anilist_detail())

    assert row.id == existing.id
    assert row.anilist_id == FRIEREN_ID
    assert len(list((await db_session.scalars(select(Anime))).all())) == 1


async def test_an_id_belonging_to_another_row_is_not_stolen(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Two rows cannot both hold one MAL id, and the unique index says so.

    The setup is what a botched reconciliation would leave behind: one row
    already carrying Frieren's AniList id, a *different* row carrying its MAL
    id. Writing the payload's ``idMal`` onto the first would violate the index
    and fail the whole request, so the conflict is logged and the attach
    skipped for a human to sort out.
    """
    anilist_row = Anime(anilist_id=FRIEREN_ID, title_romaji="Has the anilist id")
    mal_row = Anime(mal_id=FRIEREN_MAL_ID, title_romaji="Has the mal id")
    db_session.add_all([anilist_row, mal_row])
    await db_session.flush()

    with caplog.at_level("WARNING"):
        row = await upsert_detail(db_session, anilist_detail())

    assert row.id == anilist_row.id  # matched on the anilist id, which comes first
    assert row.mal_id is None  # left alone rather than taken from the other row
    assert "already belongs to another row" in caplog.text
    await db_session.flush()  # the flush must not raise: nothing violated the index


async def test_a_payload_with_no_external_id_is_ignored(db_session: AsyncSession) -> None:
    """The check constraint would refuse it, and there is nothing to key it by."""
    with pytest.raises(SourceNotFound):
        await upsert_detail(db_session, CatalogMedia(source="mal", title=anilist_detail().title))


# --- upsert_summaries -------------------------------------------------------


def search_results() -> list[CatalogMedia]:
    """The captured search page, parsed as summary media."""
    raw = load("search_frieren")["data"]["Page"]["media"]
    return [parse_media(item, full=False) for item in raw]


async def test_upsert_summaries_writes_every_result(db_session: AsyncSession) -> None:
    media = search_results()

    rows = await upsert_summaries(db_session, media)

    assert [row.anilist_id for row in rows] == [item.anilist_id for item in media]
    assert rows[0].title_english == "Frieren: Beyond Journey’s End"
    assert all(row.summary_source == "anilist" for row in rows)
    stored = list((await db_session.scalars(select(Anime))).all())
    assert {row.anilist_id for row in stored} == {item.anilist_id for item in media}
    # Summary rows only: opening one must still trigger a full fetch.
    assert all(row.refreshed_at is None for row in stored)
    assert all(row.detail_source is None for row in stored)


async def test_upsert_summaries_does_not_blank_a_detail_fetch(db_session: AsyncSession) -> None:
    """The same rule as the single-row upsert, over a whole page."""
    detailed = await frieren(db_session)
    refreshed_at = detailed.refreshed_at
    assert refreshed_at is not None

    await upsert_summaries(db_session, search_results())

    after = await db_session.get(Anime, detailed.id, populate_existing=True)
    assert after is not None
    assert after.description is not None
    assert after.relations
    assert after.studio == "MADHOUSE"
    assert after.refreshed_at == refreshed_at


async def test_a_detail_record_in_a_summary_page_is_trimmed(db_session: AsyncSession) -> None:
    """Otherwise the whole page would be written ``refreshed_at`` and lie."""
    rows = await upsert_summaries(db_session, [anilist_detail()])

    assert rows[0].refreshed_at is None
    assert rows[0].description is None


async def test_upsert_summaries_is_idempotent_and_updates(db_session: AsyncSession) -> None:
    media = search_results()
    first = await upsert_summaries(db_session, media)

    renamed = [
        parse_media({**raw, "episodes": 29}, full=False)
        for raw in load("search_frieren")["data"]["Page"]["media"]
    ]
    rows = await upsert_summaries(db_session, renamed)

    assert [row.id for row in rows] == [row.id for row in first]
    assert rows[0].episodes == 29
    assert len(list((await db_session.scalars(select(Anime))).all())) == len(media)


async def test_upsert_summaries_collapses_a_repeated_id(db_session: AsyncSession) -> None:
    """Postgres refuses to touch one row twice in a statement; the first wins."""
    media = search_results()
    rows = await upsert_summaries(db_session, [media[0], media[0], media[1]])

    assert [row.anilist_id for row in rows] == [media[0].anilist_id, media[1].anilist_id]


async def test_upsert_summaries_of_nothing_is_nothing(db_session: AsyncSession) -> None:
    assert await upsert_summaries(db_session, []) == []


async def test_a_mal_summary_page_attaches_to_the_anilist_rows(
    db_session: AsyncSession,
) -> None:
    """A search during an outage must not duplicate the rows already cached."""
    existing = await upsert_summaries(db_session, search_results())
    frieren_id = next(row.id for row in existing if row.anilist_id == FRIEREN_ID)
    # An AniList *summary* carries idMal, so the row already has a MAL id to
    # match on; that is what makes the fallback's results land on it.
    assert next(row.mal_id for row in existing if row.id == frieren_id) == FRIEREN_MAL_ID

    mal_page = [
        parse_anime(entry["node"], full=False) for entry in mal_load("search_frieren")["data"]
    ]
    rows = await upsert_summaries(db_session, mal_page)

    assert frieren_id in {row.id for row in rows}
    # Landing on the row is the point; owning it is not. The rows that already
    # carried AniList summaries keep ``summary_source = "anilist"``, because
    # the fallback only filled whatever was null (rule 3) and a row whose
    # columns AniList still owns must not be re-labelled — that label is what
    # holds the *next* MAL page to the same rule.
    by_id = {row.id: row for row in rows}
    assert by_id[frieren_id].summary_source == "anilist"
    # …and a title the AniList page never returned is a new row, which MAL
    # does own.
    fresh = [row for row in rows if row.anilist_id is None]
    assert fresh and all(row.summary_source == "mal" for row in fresh)


# --- The next broadcast (FR-C3, FR-C6, FR-C7) -------------------------------
#
# ``next_airing`` is the one column a *summary* writes as well as a detail
# fetch, because the schedule is built out of season rows. So its precedence
# cannot be read off ``detail_source``, which a season sweep never sets, and
# these are the cases that go wrong when it is.

SLOT_ANILIST_ID = 999501
SLOT_MAL_ID = 999502

#: AniList's own blob: a real episode number and no ``estimated`` key.
PUBLISHED_SLOT = {"episode": 6, "airingAt": 1_800_000_000, "timeUntilAiring": 604_800}
#: The one Arc synthesises from a MAL broadcast time: no episode, and flagged.
ESTIMATED_SLOT = {"episode": None, "airingAt": 1_800_600_000, "estimated": True}


def slot_payload(
    source: str, next_airing: dict[str, object] | None, *, full: bool = False
) -> CatalogMedia:
    """One season row from ``source``, with or without a next broadcast.

    The AniList payload carries both ids (its summary includes ``idMal``); the
    MAL one carries only its own, which is what makes the two land on one row
    during an outage.
    """
    return CatalogMedia(
        source="anilist" if source == "anilist" else "mal",
        title=MediaTitle(romaji="Next Broadcast"),
        anilist_id=SLOT_ANILIST_ID if source == "anilist" else None,
        mal_id=SLOT_MAL_ID,
        format="TV",
        status="RELEASING",
        season="FALL",
        season_year=2026,
        next_airing=next_airing,
        full=full,
    )


async def test_a_mal_slot_does_not_replace_an_anilist_one_on_a_summary_row(
    db_session: AsyncSession,
) -> None:
    """The bug this rule exists for: a season sweep sets no ``detail_source``.

    Without it, the MAL sweep of the same season during an outage would move
    the show to whatever weekday MAL's broadcast slot implies and leave it
    there until somebody opened the title.
    """
    [row] = await upsert_summaries(db_session, [slot_payload("anilist", PUBLISHED_SLOT)])
    assert row.detail_source is None and row.summary_source == "anilist"

    [after] = await upsert_summaries(db_session, [slot_payload("mal", ESTIMATED_SLOT)])

    assert after.id == row.id
    assert after.next_airing == PUBLISHED_SLOT
    # Since M15 the rest of the summary is protected too, not just the slot:
    # the fallback fills nulls and leaves ``summary_source`` naming the source
    # whose columns these still are.
    assert after.summary_source == "anilist"


async def test_an_anilist_slot_replaces_a_mal_estimate_on_a_summary_row(
    db_session: AsyncSession,
) -> None:
    """The other direction: AniList's return corrects the outage's guess."""
    [row] = await upsert_summaries(db_session, [slot_payload("mal", ESTIMATED_SLOT)])
    assert row.next_airing == ESTIMATED_SLOT

    [after] = await upsert_summaries(db_session, [slot_payload("anilist", PUBLISHED_SLOT)])

    assert after.id == row.id
    assert after.next_airing == PUBLISHED_SLOT


async def test_a_mal_slot_fills_a_summary_row_that_has_none(db_session: AsyncSession) -> None:
    """ "Never overwrite" is not "never write": a row with no slot has no weekday."""
    [row] = await upsert_summaries(db_session, [slot_payload("anilist", None)])
    assert row.next_airing is None

    [after] = await upsert_summaries(db_session, [slot_payload("mal", ESTIMATED_SLOT)])

    assert after.id == row.id
    assert after.next_airing == ESTIMATED_SLOT


async def test_a_mal_detail_fetch_does_not_blank_an_anilist_slot(
    db_session: AsyncSession,
) -> None:
    """A detail fetch writes the column even when null; MAL's null is not news."""
    [row] = await upsert_summaries(db_session, [slot_payload("anilist", PUBLISHED_SLOT)])

    after = await upsert_detail(db_session, slot_payload("mal", None, full=True))

    assert after.id == row.id
    assert after.next_airing == PUBLISHED_SLOT
    assert after.detail_source == "mal"


async def test_an_anilist_detail_fetch_clears_a_slot_the_show_no_longer_has(
    db_session: AsyncSession,
) -> None:
    """The control: a finished show has no next episode, and must lose the one it had."""
    [row] = await upsert_summaries(db_session, [slot_payload("anilist", PUBLISHED_SLOT)])

    after = await upsert_detail(db_session, slot_payload("anilist", None, full=True))

    assert after.id == row.id
    assert after.next_airing is None


# --- sync_episodes ----------------------------------------------------------


async def test_sync_creates_a_row_per_episode_with_its_air_time(
    db_session: AsyncSession,
) -> None:
    """Twelve episodes, five of them aired — the fixture schedule."""
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)

    created = await sync_episodes(db_session, anime, media.airing)
    assert created == 12

    episodes = await episodes_for(db_session, anime.id)
    assert [episode.number for episode in episodes] == list(range(1, 13))
    assert all(episode.air_at is not None for episode in episodes)
    assert all(episode.air_at_estimated is False for episode in episodes)
    assert all(episode.state is EpisodeState.NOT_WANTED for episode in episodes)

    by_number = {episode.number: episode for episode in episodes}
    expected = {entry.episode: entry.at for entry in media.airing}
    assert by_number[1].air_at == expected[1]
    assert by_number[5].air_at == expected[5]
    assert by_number[6].air_at == expected[6]
    # Five have aired, seven have not, measured against the fixture's anchor.
    now = expected[5] + timedelta(days=2)
    aired = [n for n, episode in by_number.items() if episode.air_at and episode.air_at <= now]
    assert aired == [1, 2, 3, 4, 5]


async def test_sync_never_clobbers_an_episodes_state(db_session: AsyncSession) -> None:
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)
    await sync_episodes(db_session, anime, media.airing)

    episode_three = (await episodes_for(db_session, anime.id))[2]
    episode_three.state = EpisodeState.READY
    await db_session.flush()
    ready_id = episode_three.id

    await sync_episodes(db_session, anime, media.airing)

    again = await db_session.get(Episode, ready_id, populate_existing=True)
    assert again is not None
    assert again.state is EpisodeState.READY
    assert again.number == 3


async def test_sync_is_idempotent(db_session: AsyncSession) -> None:
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)

    await sync_episodes(db_session, anime, media.airing)
    first = [(e.id, e.number, e.air_at) for e in await episodes_for(db_session, anime.id)]
    await sync_episodes(db_session, anime, media.airing)
    second = [(e.id, e.number, e.air_at) for e in await episodes_for(db_session, anime.id)]

    assert first == second


async def test_sync_backfills_an_air_time_and_never_blanks_one(
    db_session: AsyncSession,
) -> None:
    """A later page that knows less must not erase what an earlier one found."""
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)

    await sync_episodes(db_session, anime, [])  # no schedule at all
    episodes = await episodes_for(db_session, anime.id)
    assert len(episodes) == 12
    assert all(episode.air_at is None for episode in episodes)

    await sync_episodes(db_session, anime, media.airing)  # now with dates
    assert all(e.air_at is not None for e in await episodes_for(db_session, anime.id))

    await sync_episodes(db_session, anime, [])  # and back to nothing
    assert all(e.air_at is not None for e in await episodes_for(db_session, anime.id))


# --- Episode titles and stills (M15) ----------------------------------------


async def test_a_detail_fetch_fills_episode_titles_and_stills(
    db_session: AsyncSession,
) -> None:
    """The fixture's ``streamingEpisodes``, placed on the right rows.

    Six entries: three that parse, a duplicate of one of them, a trailer, and
    an episode 99 this twelve-episode show does not have.
    """
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)

    assert await sync_episodes(db_session, anime, media.airing, extras=media.episode_extras) == 12

    by_number = {episode.number: episode for episode in await episodes_for(db_session, anime.id)}
    assert by_number[1].title == "A Beginning"
    assert by_number[1].still_url == "https://img.test/arc-e01.jpg"
    # The dub link is the same episode with a worse thumbnail, not a new one.
    assert by_number[2].title == "A Middle"
    assert by_number[2].still_url == "https://img.test/arc-e02.jpg"
    # "Episode 3" carries no title of its own; the still still lands.
    assert by_number[3].title is None
    assert by_number[3].still_url == "https://img.test/arc-e03.jpg"
    # The trailer named no episode, so nothing was placed from it.
    assert by_number[4].title is None
    assert by_number[4].still_url is None


async def test_an_episode_the_show_does_not_have_is_not_invented(
    db_session: AsyncSession,
) -> None:
    """A thumbnail must never create an episode row.

    The fixture lists "Episode 99 - Not This Show", which is what a recap or a
    sequel's link looks like when it lands on the wrong entry.
    """
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)
    await sync_episodes(db_session, anime, media.airing, extras=media.episode_extras)

    episodes = await episodes_for(db_session, anime.id)
    assert [episode.number for episode in episodes] == list(range(1, 13))


async def test_a_title_that_is_already_there_is_never_overwritten(
    db_session: AsyncSession,
) -> None:
    """A title confirmed by hand outranks anything a refresh brings back."""
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)
    await sync_episodes(db_session, anime, media.airing)

    episodes = await episodes_for(db_session, anime.id)
    episodes[0].title = "What the admin called it"
    episodes[0].still_url = "https://img.test/chosen-by-hand.jpg"
    await db_session.flush()

    await sync_episodes(db_session, anime, media.airing, extras=media.episode_extras)

    by_number = {episode.number: episode for episode in await episodes_for(db_session, anime.id)}
    assert by_number[1].title == "What the admin called it"
    assert by_number[1].still_url == "https://img.test/chosen-by-hand.jpg"
    # The episodes that had nothing still filled in.
    assert by_number[2].title == "A Middle"


async def test_a_later_fetch_with_no_art_does_not_blank_it(db_session: AsyncSession) -> None:
    """A MAL refresh during an outage must not cost the stills AniList found."""
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)
    await sync_episodes(db_session, anime, media.airing, extras=media.episode_extras)

    await sync_episodes(db_session, anime, media.airing)

    by_number = {episode.number: episode for episode in await episodes_for(db_session, anime.id)}
    assert by_number[1].title == "A Beginning"
    assert by_number[1].still_url == "https://img.test/arc-e01.jpg"


async def test_filling_the_art_is_idempotent(db_session: AsyncSession) -> None:
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)

    await sync_episodes(db_session, anime, media.airing, extras=media.episode_extras)
    first = [
        (e.id, e.number, e.title, e.still_url) for e in await episodes_for(db_session, anime.id)
    ]
    await sync_episodes(db_session, anime, media.airing, extras=media.episode_extras)
    second = [
        (e.id, e.number, e.title, e.still_url) for e in await episodes_for(db_session, anime.id)
    ]

    assert first == second


async def test_a_mal_detail_fetch_leaves_the_episode_art_alone(
    db_session: AsyncSession,
) -> None:
    """MAL publishes no per-episode anything, so nothing is written (FR-C6)."""
    media = mal_detail()
    assert media.episode_extras == []

    anime = await upsert_detail(db_session, media)
    await sync_episodes(db_session, anime, media.airing, extras=media.episode_extras)

    episodes = await episodes_for(db_session, anime.id)
    assert episodes
    assert all(episode.title is None for episode in episodes)
    assert all(episode.still_url is None for episode in episodes)


async def test_a_refresh_fills_the_art_of_a_row_that_predates_the_columns(
    db_session: AsyncSession,
) -> None:
    """The whole migration story: existing rows fill in on their next refresh.

    A row written before M15 has null artwork columns, which is exactly the
    state a fresh row is in, so the test blanks them and runs the refresh job's
    own path (``ensure_anime`` with ``max_age=0``).
    """
    fake = FakeAniList(media={RELEASING_ID: load("media_999001_releasing")})
    catalog = catalog_over(fake)
    anime = await ensure_anime(db_session, catalog, anilist_id=RELEASING_ID)

    anime.cover_large_url = None
    anime.credits = None
    for episode in await episodes_for(db_session, anime.id):
        episode.title = None
        episode.still_url = None
    await db_session.flush()

    refreshed = await ensure_anime(db_session, catalog, anime_id=anime.id, max_age=timedelta(0))
    assert refreshed.cover_large_url == (
        "https://s4.anilist.co/file/anilistcdn/media/anime/cover/xl/bx999001.jpg"
    )
    assert refreshed.credits is not None
    by_number = {e.number: e for e in await episodes_for(db_session, anime.id)}
    assert by_number[1].still_url == "https://img.test/arc-e01.jpg"


# --- Key art and credits (M15) ----------------------------------------------


async def test_an_anilist_detail_fetch_stores_the_key_art_and_the_credits(
    db_session: AsyncSession,
) -> None:
    anime = await frieren(db_session)

    assert anime.cover_large_url == (
        "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx154587-qQTzQnEJJ3oB.jpg"
    )
    assert anime.credits is not None
    assert anime.credits[0] == {"role": "Studio", "name": "MADHOUSE"}
    assert [row["role"] for row in anime.credits] == [
        "Studio",
        "Director",
        "Series Composition",
        "Character Design",
        "Music",
        "Original Creator",
        "Original Creator",
    ]
    # The sound director, the art director and the theme song performer are in
    # the fixture's twelve staff edges and in none of these rows.
    assert "Satoki Iida" not in [row["name"] for row in anime.credits]


async def test_a_search_result_carries_the_key_art_but_no_credits(
    db_session: AsyncSession,
) -> None:
    """``coverImage.extraLarge`` is already in the summary fragment (M15).

    So a card gets the sharp artwork for nothing, while the credits stay a
    detail column — the staff connection is only asked for by the by-id query.
    """
    page = load("search_frieren")["data"]["Page"]["media"]
    rows = await upsert_summaries(db_session, [parse_media(raw, full=False) for raw in page[:1]])

    assert rows[0].cover_large_url is not None
    assert rows[0].cover_large_url.endswith(".jpg")
    assert rows[0].credits is None


async def test_a_mal_row_has_no_key_art_and_a_credits_block_of_one(
    db_session: AsyncSession,
) -> None:
    """MAL's biggest picture is 230 px and it publishes no staff at all."""
    media = mal_detail()
    assert media.cover_large_url is None

    anime = await upsert_detail(db_session, media)
    assert anime.cover_large_url is None
    assert anime.credits == [{"role": "Studio", "name": "Madhouse"}]


async def test_anilist_overwrites_a_mal_credits_block(db_session: AsyncSession) -> None:
    """The fill-only rule runs the right way round for the new columns too."""
    anime = await upsert_detail(db_session, mal_detail())
    assert anime.credits == [{"role": "Studio", "name": "Madhouse"}]

    anime = await upsert_detail(db_session, anilist_detail())
    assert anime.credits is not None
    assert len(anime.credits) > 1
    assert anime.cover_large_url is not None


async def test_mal_does_not_overwrite_an_anilist_credits_block(
    db_session: AsyncSession,
) -> None:
    anime = await upsert_detail(db_session, anilist_detail())
    before = list(anime.credits or [])
    cover = anime.cover_large_url

    anime = await upsert_detail(db_session, mal_detail())

    assert anime.credits == before
    # A null from the weaker source is not a correction (rule 3).
    assert anime.cover_large_url == cover


# --- The fallback never overwrites the primary (rule 3) ---------------------
#
# Judged per half of the row: ``summary_source`` for the card columns,
# ``detail_source`` for the rest. The artwork columns are the visible half of
# this — MAL's cover is 230 px where AniList's is 1900 — which is what made it
# worth tightening for the M15 redesign.


async def test_a_mal_refresh_keeps_anilists_art_and_prose_but_fills_the_nulls(
    db_session: AsyncSession,
) -> None:
    """An outage must not cost a good row its cover, banner or synopsis."""
    anime = await upsert_detail(db_session, anilist_detail())
    cover, banner = anime.cover_url, anime.banner_url
    description, key_art = anime.description, anime.cover_large_url
    assert cover and banner and description and key_art
    # A column AniList left empty, so there is something for MAL to fill.
    anime.season_year = None
    await db_session.flush()

    after = await upsert_detail(db_session, mal_detail())

    assert after.id == anime.id
    assert after.cover_url == cover
    assert after.banner_url == banner
    assert after.description == description
    assert after.cover_large_url == key_art
    assert after.title_romaji == "Sousou no Frieren"
    # …and the null was filled, which is the half of the rule that is not "no".
    assert after.season_year == 2023
    # Neither source column changed hands: the columns are still AniList's.
    assert (after.summary_source, after.detail_source) == ("anilist", "anilist")
    # The row was still asked about and answered, so it is still fresh (FR-C5).
    assert after.refreshed_at is not None


async def test_an_anilist_refresh_replaces_everything_on_a_mal_row(
    db_session: AsyncSession,
) -> None:
    """The other direction: the primary's return upgrades the whole row."""
    anime = await upsert_detail(db_session, mal_detail())
    assert anime.cover_url is not None and "myanimelist" in anime.cover_url
    assert anime.cover_large_url is None
    mal_description = anime.description

    after = await upsert_detail(db_session, anilist_detail())

    assert after.id == anime.id
    assert after.cover_url is not None and "anilist" in after.cover_url
    assert after.cover_large_url is not None
    assert after.banner_url is not None
    assert after.description != mal_description
    assert after.tags  # MAL publishes none at all
    assert (after.summary_source, after.detail_source) == ("anilist", "anilist")


async def test_mal_updates_its_own_row_in_full(db_session: AsyncSession) -> None:
    """A source always gets to correct itself; the rule is about *other* ones.

    Equal ranks are not outranked — otherwise a MAL-only deployment, or a show
    AniList has never heard of, would freeze at whatever the first fetch said.
    """
    anime = await upsert_detail(db_session, mal_detail())
    assert anime.description is not None

    corrected = replace(
        mal_detail(),
        title=MediaTitle(romaji="Sousou no Frieren", english="Frieren, renamed"),
        episodes=29,
        cover_url="https://cdn.myanimelist.net/images/anime/1015/corrected.jpg",
        description="A corrected synopsis.",
    )
    after = await upsert_detail(db_session, corrected)

    assert after.id == anime.id
    assert after.title_english == "Frieren, renamed"
    assert after.episodes == 29
    assert after.cover_url is not None and after.cover_url.endswith("corrected.jpg")
    assert after.description == "A corrected synopsis."
    assert (after.summary_source, after.detail_source) == ("mal", "mal")


async def test_a_mal_search_does_not_rewrite_an_anilist_card(
    db_session: AsyncSession,
) -> None:
    """The summary half on its own, with no detail fetch anywhere in sight.

    A season sweep or a search during an outage leaves ``detail_source`` null,
    so this case is decided entirely by ``summary_source`` — which is why the
    two halves are judged separately rather than by one shared flag.
    """
    page = load("search_frieren")["data"]["Page"]["media"]
    [row] = await upsert_summaries(db_session, [parse_media(page[0], full=False)])
    assert row.detail_source is None
    cover, key_art = row.cover_url, row.cover_large_url

    mal_page = [
        parse_anime(entry["node"], full=False) for entry in mal_load("search_frieren")["data"]
    ]
    mal_frieren = next(item for item in mal_page if item.mal_id == FRIEREN_MAL_ID)
    [after] = await upsert_summaries(db_session, [mal_frieren])

    assert after.id == row.id
    assert after.cover_url == cover
    assert after.cover_large_url == key_art
    assert after.summary_source == "anilist"


async def test_a_mal_detail_fetch_still_fills_a_row_only_a_search_had_touched(
    db_session: AsyncSession,
) -> None:
    """The rule must not become "MAL may never write to an AniList row".

    A row from an AniList *search* has ``summary_source = "anilist"`` and no
    detail at all. MAL is barred from its card columns and must still be free
    to fill the synopsis, genres and relations — during an outage that is the
    only way the show page gets a synopsis at all.
    """
    page = load("search_frieren")["data"]["Page"]["media"]
    [row] = await upsert_summaries(db_session, [parse_media(page[0], full=False)])
    assert row.description is None
    cover = row.cover_url

    after = await upsert_detail(db_session, mal_detail())

    assert after.id == row.id
    assert after.description is not None
    assert after.genres
    assert after.credits == [{"role": "Studio", "name": "Madhouse"}]
    assert after.detail_source == "mal"
    # The card columns are still AniList's, and still say so.
    assert after.cover_url == cover
    assert after.summary_source == "anilist"


# --- Estimated air times (FR-C6) --------------------------------------------


async def test_a_published_air_time_replaces_an_estimate_and_clears_the_flag(
    db_session: AsyncSession,
) -> None:
    """What happens the moment AniList comes back: the badges go away."""
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)
    guesses = estimated(weekly(12, start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)))

    await sync_episodes(db_session, anime, guesses)
    rows = await episodes_for(db_session, anime.id)
    assert all(episode.air_at_estimated for episode in rows)
    assert rows[0].air_at == guesses[0].at

    await sync_episodes(db_session, anime, media.airing)

    rows = await episodes_for(db_session, anime.id)
    assert all(episode.air_at_estimated is False for episode in rows)
    assert rows[0].air_at == media.airing[0].at


async def test_an_estimate_never_replaces_a_published_air_time(
    db_session: AsyncSession,
) -> None:
    """The other direction: a MAL refresh must not undo AniList's correction."""
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)
    await sync_episodes(db_session, anime, media.airing)

    await sync_episodes(
        db_session, anime, estimated(weekly(12, start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)))
    )

    rows = await episodes_for(db_session, anime.id)
    assert rows[0].air_at == media.airing[0].at
    assert all(episode.air_at_estimated is False for episode in rows)


async def test_an_estimate_fills_an_episode_that_had_no_date(
    db_session: AsyncSession,
) -> None:
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)
    await sync_episodes(db_session, anime, [])

    guesses = estimated(weekly(12, start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)))
    await sync_episodes(db_session, anime, guesses)

    rows = await episodes_for(db_session, anime.id)
    assert all(episode.air_at is not None for episode in rows)
    assert all(episode.air_at_estimated for episode in rows)


async def test_a_newer_estimate_replaces_an_older_one(db_session: AsyncSession) -> None:
    """Two guesses: the newer one is at least no worse informed."""
    media = parse_media(media_payload("media_999001_releasing"), full=True)
    anime = await upsert_detail(db_session, media)
    await sync_episodes(
        db_session, anime, estimated(weekly(12, start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)))
    )

    later = estimated(weekly(12, start=datetime(2026, 1, 2, 12, 0, tzinfo=UTC)))
    await sync_episodes(db_session, anime, later)

    rows = await episodes_for(db_session, anime.id)
    assert rows[0].air_at == later[0].at
    assert rows[0].air_at_estimated is True


async def test_a_mal_detail_fetch_gives_every_episode_an_estimated_date(
    db_session: AsyncSession,
) -> None:
    """End to end for FR-C6: MAL's broadcast slot becomes 28 episode rows."""
    media = mal_detail()
    anime = await upsert_detail(db_session, media)

    assert await sync_episodes(db_session, anime, media.airing) == 28
    rows = await episodes_for(db_session, anime.id)
    assert len(rows) == 28
    assert all(episode.air_at_estimated for episode in rows)
    assert rows[0].air_at == datetime(2023, 9, 29, 14, 0, tzinfo=UTC)
    assert rows[1].air_at == datetime(2023, 10, 6, 14, 0, tzinfo=UTC)


async def test_frieren_gets_twenty_eight_rows(db_session: AsyncSession) -> None:
    """AniList publishes 5..28; 1..4 are Arc's to work out.

    The four-episode premiere aired as one two-hour broadcast and AniList has
    no per-episode slot for it, so the schedule begins at episode 5.
    """
    media = anilist_detail()
    anime = await upsert_detail(db_session, media)

    assert await sync_episodes(db_session, anime, media.airing) == 28
    episodes = await episodes_for(db_session, anime.id)
    assert len(episodes) == 28
    by_number = {episode.number: episode for episode in episodes}
    assert by_number[5].air_at == datetime(2023, 10, 6, 14, 0, tzinfo=UTC)
    assert by_number[5].air_at_estimated is False
    assert episodes[-1].air_at == datetime(2024, 3, 22, 14, 0, tzinfo=UTC)
    # Everything below the schedule is dated backwards from it and badged.
    assert [by_number[n].air_at for n in (1, 2, 3, 4)] == [
        datetime(2023, 9, 8, 14, 0, tzinfo=UTC),
        datetime(2023, 9, 15, 14, 0, tzinfo=UTC),
        datetime(2023, 9, 22, 14, 0, tzinfo=UTC),
        datetime(2023, 9, 29, 14, 0, tzinfo=UTC),
    ]
    assert all(by_number[n].air_at_estimated for n in (1, 2, 3, 4))
    assert all(episode.air_at is not None for episode in episodes)


async def test_estimates_below_the_published_schedule_are_dated_backwards(
    db_session: AsyncSession,
) -> None:
    """Frieren's real pattern: MAL guessed 1..28, AniList publishes 5..28.

    The MAL estimates for 1–4 are three weeks out (MAL dates every episode from
    the 2023-09-29 premiere, one a week), and after the real schedule arrives
    they must be re-anchored to it rather than left where MAL put them —
    otherwise episode 4 sits *after* episode 5.
    """
    mal = mal_detail()
    anime = await upsert_detail(db_session, mal)
    await sync_episodes(db_session, anime, mal.airing)
    rows = await episodes_for(db_session, anime.id)
    assert all(episode.air_at_estimated for episode in rows)

    anilist = anilist_detail()
    await sync_episodes(db_session, anime, anilist.airing)

    rows = await episodes_for(db_session, anime.id)
    by_number = {episode.number: episode for episode in rows}
    # Published, from AniList, and no longer badged.
    assert by_number[5].air_at == datetime(2023, 10, 6, 14, 0, tzinfo=UTC)
    assert all(by_number[n].air_at_estimated is False for n in range(5, 29))
    # Estimated, weekly backwards from episode 5, still badged.
    assert [by_number[n].air_at for n in (1, 2, 3, 4)] == [
        datetime(2023, 9, 8, 14, 0, tzinfo=UTC),
        datetime(2023, 9, 15, 14, 0, tzinfo=UTC),
        datetime(2023, 9, 22, 14, 0, tzinfo=UTC),
        datetime(2023, 9, 29, 14, 0, tzinfo=UTC),
    ]
    assert all(by_number[n].air_at_estimated for n in (1, 2, 3, 4))
    # Strictly increasing: nothing shares an instant with anything else, which
    # is the property the show page and the "aired" boundary both rely on.
    times = [by_number[n].air_at for n in range(1, 29)]
    assert None not in times
    assert times == sorted(times)  # type: ignore[type-var]
    assert len(set(times)) == 28


async def test_an_estimate_above_the_first_published_episode_is_left_alone(
    db_session: AsyncSession,
) -> None:
    """Only the run *below* the schedule is re-dated.

    An episode the published schedule simply skipped keeps whatever estimate it
    had: there is no evidence it belongs a week before the next one, and the
    back-fill's arithmetic only makes sense walking away from the anchor.
    """
    anime = Anime(anilist_id=910004, title_romaji="Gap", status="RELEASING", episodes=6)
    db_session.add(anime)
    await db_session.flush()
    guesses = estimated(weekly(6, start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)))
    await sync_episodes(db_session, anime, guesses)

    published = [AiringEntry(episode=n, at=datetime(2026, 3, n, 12, 0, tzinfo=UTC)) for n in (3, 5)]
    await sync_episodes(db_session, anime, published)

    by_number = {episode.number: episode for episode in await episodes_for(db_session, anime.id)}
    # 1 and 2 are below the first published episode, so they move.
    assert by_number[1].air_at == datetime(2026, 2, 17, 12, 0, tzinfo=UTC)
    assert by_number[2].air_at == datetime(2026, 2, 24, 12, 0, tzinfo=UTC)
    assert by_number[1].air_at_estimated and by_number[2].air_at_estimated
    # 4 and 6 are above it and absent from the schedule, so they do not.
    assert by_number[4].air_at == guesses[3].at
    assert by_number[6].air_at == guesses[5].at
    assert by_number[4].air_at_estimated and by_number[6].air_at_estimated


async def test_an_all_estimated_schedule_anchors_nothing(db_session: AsyncSession) -> None:
    """There is nothing firmer to walk back from, so nothing is rewritten."""
    anime = Anime(anilist_id=910005, title_romaji="Guesses only", status="RELEASING", episodes=4)
    db_session.add(anime)
    await db_session.flush()

    guesses = estimated(
        [AiringEntry(episode=n, at=datetime(2026, 4, n, 12, 0, tzinfo=UTC)) for n in (3, 4)]
    )
    await sync_episodes(db_session, anime, guesses)

    by_number = {episode.number: episode for episode in await episodes_for(db_session, anime.id)}
    assert by_number[1].air_at is None
    assert by_number[2].air_at is None
    assert by_number[3].air_at == guesses[0].at


async def test_an_unknown_episode_count_falls_back_to_the_schedule(
    db_session: AsyncSession,
) -> None:
    """A show airing without an announced count still gets rows.

    Including one for the episode that has not aired yet: acquisition works
    from ``episodes`` rows, so the next one has to exist before its air date.
    """
    start = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    anime = Anime(anilist_id=910001, title_romaji="Ongoing", status="RELEASING", episodes=None)
    anime.next_airing = {"episode": 6, "airingAt": int((start + timedelta(weeks=5)).timestamp())}
    db_session.add(anime)
    await db_session.flush()

    assert await sync_episodes(db_session, anime, weekly(5, start=start)) == 6
    episodes = await episodes_for(db_session, anime.id)
    assert [e.number for e in episodes] == [1, 2, 3, 4, 5, 6]
    assert episodes[5].air_at is None  # not in the schedule pages, only in next_airing


async def test_a_show_with_nothing_known_gets_no_rows(db_session: AsyncSession) -> None:
    anime = Anime(anilist_id=910002, title_romaji="Announced only", status="NOT_YET_RELEASED")
    db_session.add(anime)
    await db_session.flush()

    assert await sync_episodes(db_session, anime, []) == 0
    assert await episodes_for(db_session, anime.id) == []


async def test_a_shrinking_episode_count_never_deletes_rows(db_session: AsyncSession) -> None:
    start = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    anime = Anime(anilist_id=910003, title_romaji="Recount", status="RELEASING", episodes=12)
    db_session.add(anime)
    await db_session.flush()
    await sync_episodes(db_session, anime, weekly(12, start=start))

    anime.episodes = 10  # the source changed its mind
    await sync_episodes(db_session, anime, weekly(10, start=start))

    # A row may already own a file or somebody's progress; dropping it is not
    # this function's decision to make.
    assert len(await episodes_for(db_session, anime.id)) == 12


# --- ensure_anime -----------------------------------------------------------


async def test_ensure_fetches_when_nothing_is_cached(db_session: AsyncSession) -> None:
    fake = frieren_fake()
    catalog = catalog_over(fake)
    try:
        anime = await ensure_anime(db_session, catalog, anilist_id=FRIEREN_ID)
    finally:
        await catalog.aclose()

    assert anime.anilist_id == FRIEREN_ID
    assert len(await episodes_for(db_session, anime.id)) == 28
    assert fake.calls == [("media", {"id": FRIEREN_ID})]


async def test_ensure_finds_a_row_by_its_internal_id(db_session: AsyncSession) -> None:
    """Which is how every API route reaches it (FR-C6)."""
    fake = frieren_fake()
    catalog = catalog_over(fake)
    try:
        created = await ensure_anime(db_session, catalog, anilist_id=FRIEREN_ID)
        again = await ensure_anime(db_session, catalog, anime_id=created.id)
    finally:
        await catalog.aclose()

    assert again.id == created.id
    assert len(fake.calls) == 1  # the second call was served from cache


async def test_ensure_of_an_unknown_internal_id_is_not_found(
    db_session: AsyncSession,
) -> None:
    catalog = catalog_over(frieren_fake())
    try:
        with pytest.raises(SourceNotFound):
            await ensure_anime(db_session, catalog, anime_id=424242)
    finally:
        await catalog.aclose()


async def test_ensure_serves_a_fresh_row_without_asking_upstream(
    db_session: AsyncSession,
) -> None:
    fake = frieren_fake()
    catalog = catalog_over(fake)
    try:
        await ensure_anime(db_session, catalog, anilist_id=FRIEREN_ID)
        await ensure_anime(db_session, catalog, anilist_id=FRIEREN_ID)
    finally:
        await catalog.aclose()

    assert len(fake.calls) == 1


async def test_ensure_refetches_a_stale_row(db_session: AsyncSession) -> None:
    fake = frieren_fake()
    catalog = catalog_over(fake)
    try:
        anime = await ensure_anime(db_session, catalog, anilist_id=FRIEREN_ID)
        anime.refreshed_at = datetime.now(UTC) - timedelta(hours=25)
        await db_session.flush()
        await ensure_anime(db_session, catalog, anime_id=anime.id)
    finally:
        await catalog.aclose()

    assert len(fake.calls) == 2


async def test_ensure_with_zero_max_age_always_fetches(db_session: AsyncSession) -> None:
    fake = frieren_fake()
    catalog = catalog_over(fake)
    try:
        anime = await ensure_anime(db_session, catalog, anilist_id=FRIEREN_ID)
        await ensure_anime(db_session, catalog, anime_id=anime.id, max_age=timedelta(0))
    finally:
        await catalog.aclose()

    assert len(fake.calls) == 2


async def test_a_mal_filled_row_is_stale_as_soon_as_anilist_is_healthy(
    db_session: AsyncSession, slept: list[float]
) -> None:
    """FR-C6's "upgrade promptly" rule.

    Without it, a five-minute outage would leave a show page reading
    "estimated" until tomorrow — the row is only an hour old, after all.
    """
    fake = frieren_fake()
    fake.disabled = True
    catalog = catalog_over(fake)
    try:
        row = await ensure_anime(db_session, catalog, mal_id=FRIEREN_MAL_ID)
        assert row.detail_source == "mal"
        assert row.refreshed_at is not None

        # AniList is back, and its breaker with it.
        fake.disabled = False
        catalog.breaker.reset()
        upgraded = await ensure_anime(db_session, catalog, anime_id=row.id)
    finally:
        await catalog.aclose()

    assert upgraded.id == row.id
    assert upgraded.detail_source == "anilist"
    assert upgraded.anilist_id == FRIEREN_ID
    rows = await episodes_for(db_session, upgraded.id)
    # Everything AniList publishes loses its badge; the four episodes it has no
    # slot for keep one, because their dates are still Arc's arithmetic.
    assert all(episode.air_at_estimated is False for episode in rows[4:])
    assert all(episode.air_at_estimated for episode in rows[:4])


async def test_a_mal_filled_row_is_left_alone_while_anilist_is_still_down(
    db_session: AsyncSession, slept: list[float]
) -> None:
    """The upgrade rule must not become "re-fetch on every request"."""
    fake = frieren_fake()
    fake.disabled = True
    mal = mal_frieren_fake()
    catalog = CatalogService(fake.source(), mal.source(), Breaker(300.0))
    try:
        row = await ensure_anime(db_session, catalog, mal_id=FRIEREN_MAL_ID)
        before = len(mal.calls)
        again = await ensure_anime(db_session, catalog, anime_id=row.id)
    finally:
        await catalog.aclose()

    assert again.id == row.id
    assert len(mal.calls) == before  # served from cache


async def test_ensure_falls_back_to_mal_when_anilist_is_disabled(
    db_session: AsyncSession, slept: list[float]
) -> None:
    fake = frieren_fake()
    fake.disabled = True
    catalog = catalog_over(fake)
    try:
        anime = await ensure_anime(db_session, catalog, mal_id=FRIEREN_MAL_ID)
    finally:
        await catalog.aclose()

    assert anime.mal_id == FRIEREN_MAL_ID
    assert anime.anilist_id is None
    assert anime.detail_source == "mal"
    rows = await episodes_for(db_session, anime.id)
    assert len(rows) == 28
    assert all(episode.air_at_estimated for episode in rows)


async def test_ensure_tries_the_mal_id_when_the_anilist_id_is_a_404(
    db_session: AsyncSession,
) -> None:
    """A row Arc has must not 404 because one source forgot the show.

    Within :class:`CatalogService` a not-found on an AniList id is final; here,
    where both ids are known to name the same row, it is a reason to try the
    other one.
    """
    row = Anime(anilist_id=424242, mal_id=FRIEREN_MAL_ID, title_romaji="Both ids")
    db_session.add(row)
    await db_session.flush()

    fake = frieren_fake()
    fake.media.clear()  # AniList knows neither the id nor the MAL id
    catalog = catalog_over(fake)
    try:
        found = await ensure_anime(db_session, catalog, anime_id=row.id)
    finally:
        await catalog.aclose()

    assert found.id == row.id
    assert found.detail_source == "mal"
    assert found.episodes == 28


async def test_ensure_gives_a_long_running_show_an_air_time_for_every_episode(
    db_session: AsyncSession,
) -> None:
    """The truncation bug, at the layer that stores it.

    250 aired episodes arrive over three schedule pages. Before the paging,
    only the first hundred had an ``air_at`` and episodes 101–250 were stored
    with nulls — which the show page then rendered as "not aired yet".
    """
    fake = long_running_fake()
    catalog = catalog_over(fake)
    try:
        anime = await ensure_anime(db_session, catalog, anilist_id=LONG_RUNNING_ID)
    finally:
        await catalog.aclose()

    assert anime.episodes is None  # AniList announces no count for it
    episodes = await episodes_for(db_session, anime.id)
    # 250 aired plus the one ``nextAiringEpisode`` names.
    assert [e.number for e in episodes] == list(range(1, LONG_RUNNING_AIRED + 2))
    assert all(e.air_at is not None for e in episodes[:LONG_RUNNING_AIRED])
    # …and the next one has no schedule row at all, which is the case the
    # ``aired`` boundary rule in the API has to get right.
    assert episodes[LONG_RUNNING_AIRED].air_at is None


async def test_ensure_raises_not_found_when_no_source_has_the_id(
    db_session: AsyncSession,
) -> None:
    catalog = CatalogService(FakeAniList().source(), FakeMal().source(), Breaker(300.0))
    try:
        with pytest.raises(SourceNotFound):
            await ensure_anime(db_session, catalog, anilist_id=12345)
    finally:
        await catalog.aclose()


async def test_ensure_serves_the_cached_row_when_everything_is_down(
    db_session: AsyncSession, slept: list[float]
) -> None:
    """Stale beats absent: an outage must not empty the show page."""
    fake = frieren_fake()
    catalog = catalog_over(fake)
    try:
        anime = await ensure_anime(db_session, catalog, anilist_id=FRIEREN_ID)
        anime.refreshed_at = datetime.now(UTC) - timedelta(days=3)
        await db_session.flush()
        anime_id = anime.id
    finally:
        await catalog.aclose()

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    down = CatalogService(
        AniListSource.over(httpx.MockTransport(broken)),
        FakeMal(fail_with=503).source(),
        Breaker(300.0),
    )
    try:
        served = await ensure_anime(db_session, down, anime_id=anime_id)
    finally:
        await down.aclose()

    assert served.id == anime_id
    assert served.title_english == "Frieren: Beyond Journey’s End"


async def test_ensure_raises_when_everything_is_down_and_nothing_is_cached(
    db_session: AsyncSession, slept: list[float]
) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    catalog = CatalogService(
        AniListSource.over(httpx.MockTransport(broken)),
        FakeMal(fail_with=503).source(),
        Breaker(300.0),
    )
    try:
        with pytest.raises(SourceUnavailable):
            await ensure_anime(db_session, catalog, anilist_id=RELEASING_ID)
    finally:
        await catalog.aclose()
