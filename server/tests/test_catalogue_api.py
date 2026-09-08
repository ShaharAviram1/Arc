"""Search, show detail, list states and source health over HTTP.

FR-C1, FR-C2, FR-W2, and — since M3b — FR-C6 and the admin status view. The app
under test is the real one; only the sockets to AniList and MyAnimeList are
replaced, by putting a :class:`CatalogService` over ``httpx.MockTransport`` on
``app.state.catalog``. That is the same attribute
:func:`arc.api.deps.get_catalog` reads in production, so nothing about the
routers is stubbed.

**Ids.** Every path segment here is Arc's internal id, never AniList's. Tests
get one the way the client does: from a search response, or from the row a
seed helper created. Hard-coding 154587 into a URL is exactly the bug M3b
removed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine

from arc.api.deps import ADMIN_REQUIRED, NOT_AUTHENTICATED
from arc.db import SessionFactory
from arc.models import Anime, Episode, Job, ListEntry, ListStatus, UpdatedBy, UserRole
from arc.services.catalog import Breaker, CatalogService
from tests.anilist_mock import (
    FRIEREN_ID,
    FROZEN_NOW,
    LONG_RUNNING_AIRED,
    LONG_RUNNING_ID,
    RELEASING_ID,
    FakeAniList,
    frieren_fake,
    load,
    long_running_fake,
    summary_of,
)
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login
from tests.mal_mock import FRIEREN_MAL_ID, SEASON_NAME, SEASON_YEAR, FakeMal
from tests.mal_mock import frieren_fake as mal_frieren_fake

pytestmark = pytest.mark.pg

USER_EMAIL = "viewer@arc.test"
USER_PASSWORD = "viewer-password"

#: The other AniList ids in the captured search page, which is the live
#: "frieren" result set: the sequel, the third season, and the three ONA
#: shorts.
SEQUEL_ID = 182255
SIDE_STORY_ID = 170068
SEARCH_IDS = {FRIEREN_ID, SEQUEL_ID, SIDE_STORY_ID, 209939, 189513, 206425}


@pytest.fixture
def anilist() -> FakeAniList:
    """Frieren by id and by search, plus the currently-airing fixture show."""
    fake = frieren_fake()
    fake.media[RELEASING_ID] = load("media_999001_releasing")
    fake.search["airing"] = {
        "data": {
            "Page": {
                "pageInfo": {"currentPage": 1, "hasNextPage": False},
                "media": [summary_of(load("media_999001_releasing")["data"]["Media"])],
            }
        }
    }
    fake.seasons[(SEASON_YEAR, SEASON_NAME)] = [
        summary_of(fake.media[FRIEREN_ID]["data"]["Media"])  # type: ignore[index]
    ]
    return fake


@pytest.fixture
def mal() -> FakeMal:
    return mal_frieren_fake()


@pytest.fixture
def catalogue_app(api_app: FastAPI, anilist: FakeAniList, mal: FakeMal) -> FastAPI:
    """The API with both catalogue sources replaced by fakes."""
    api_app.state.catalog = CatalogService(anilist.source(), mal.source(), Breaker(300.0))
    return api_app


@pytest.fixture
async def user_client(
    catalogue_app: FastAPI, api_factory: SessionFactory
) -> AsyncIterator[AsyncClient]:
    """A signed-in ordinary user."""
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(catalogue_app) as client:
        yield await login(client, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def anon_client(catalogue_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_transport(catalogue_app) as client:
        yield client


@pytest.fixture
def statements(pg_engine: AsyncEngine) -> Iterator[list[str]]:
    """Every SQL statement the engine executes while the test runs.

    Attached to the engine rather than to a session so it sees what actually
    went over the wire — the number of round trips is the thing under test,
    and an ORM-level count would not distinguish one multi-row insert from
    twenty single-row ones.
    """
    recorded: list[str] = []

    def before(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        recorded.append(statement)

    event.listen(pg_engine.sync_engine, "before_cursor_execute", before)
    try:
        yield recorded
    finally:
        event.remove(pg_engine.sync_engine, "before_cursor_execute", before)


async def internal_id(client: AsyncClient, term: str, external_id: int) -> int:
    """Arc's id for a show, found the way the client finds it: by searching.

    Nothing in the API takes an AniList or MAL id, so a test that wants to open
    a show page has to go through the same door a browser does (FR-C6).
    """
    response = await client.get("/api/anime/search", params={"q": term})
    assert response.status_code == 200, response.text
    for row in response.json()["results"]:
        if external_id in (row["anilist_id"], row["mal_id"]):
            return int(row["id"])
    raise AssertionError(f"{external_id} not in the results for {term!r}")


async def frieren_id(client: AsyncClient) -> int:
    return await internal_id(client, "frieren", FRIEREN_ID)


async def entry_row(factory: SessionFactory, anime_id: int) -> ListEntry:
    """Read a list entry straight from the database."""
    async with factory() as session:
        rows = await session.execute(
            select(ListEntry).where(ListEntry.anime_id == anime_id).order_by(ListEntry.user_id)
        )
        entry = rows.scalars().first()
        assert entry is not None, "no list entry"
        return entry


# --- Search -----------------------------------------------------------------


async def test_search_returns_results_with_no_list_status(user_client: AsyncClient) -> None:
    response = await user_client.get("/api/anime/search", params={"q": "frieren"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"] == 1
    assert body["has_next"] is False
    first = body["results"][0]
    # The id is Arc's; AniList's and MAL's ride alongside it.
    assert first["id"] > 0
    assert first["anilist_id"] == FRIEREN_ID
    assert first["mal_id"] == FRIEREN_MAL_ID
    assert first["source"] == "anilist"
    assert first["title"] == {
        "romaji": "Sousou no Frieren",
        "english": "Frieren: Beyond Journey’s End",
        "native": "葬送のフリーレン",
        "preferred": "Frieren: Beyond Journey’s End",
    }
    assert first["format"] == "TV"
    assert first["episodes"] == 28
    assert first["status"] == "FINISHED"
    assert (first["season"], first["season_year"]) == ("FALL", 2023)
    assert first["cover_url"].startswith("https://")
    assert first["list_status"] is None


async def test_search_caches_what_it_finds(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await user_client.get("/api/anime/search", params={"q": "frieren"})

    async with api_factory() as session:
        rows = list((await session.scalars(select(Anime))).all())
    assert {row.anilist_id for row in rows} == SEARCH_IDS
    # …but as summary rows only: opening one must still trigger a full fetch.
    assert all(row.refreshed_at is None for row in rows)
    assert all(row.summary_source == "anilist" for row in rows)
    assert all(row.detail_source is None for row in rows)


async def test_search_caches_the_whole_page_in_one_insert(
    user_client: AsyncClient, statements: list[str]
) -> None:
    """Twenty results used to be twenty sequential round trips.

    A search is on the keystroke path, so the upserts are one multi-row
    ``INSERT … ON CONFLICT DO UPDATE … RETURNING`` — with the same rule as
    before, that a summary never touches a detail column.
    """
    statements.clear()
    response = await user_client.get("/api/anime/search", params={"q": "frieren"})

    assert response.status_code == 200
    assert len(response.json()["results"]) == len(SEARCH_IDS)
    inserts = [sql for sql in statements if "INSERT INTO anime" in sql]
    assert len(inserts) == 1, inserts
    # One statement, every row, and only the summary columns updated: the
    # SET clause (between DO UPDATE SET and RETURNING) names no detail column.
    assert "ON CONFLICT" in inserts[0]
    assignments = inserts[0].split("DO UPDATE SET")[1].split("RETURNING")[0]
    assert "refreshed_at" not in assignments
    assert "description" not in assignments
    assert "title_english" in assignments


async def test_a_repeated_search_reuses_the_same_internal_ids(
    user_client: AsyncClient,
) -> None:
    """An id the client has bookmarked must not change under it."""
    first = (await user_client.get("/api/anime/search", params={"q": "frieren"})).json()
    second = (await user_client.get("/api/anime/search", params={"q": "frieren"})).json()

    assert [row["id"] for row in first["results"]] == [row["id"] for row in second["results"]]


async def test_search_shows_the_callers_own_list_status(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching"})

    body = (await user_client.get("/api/anime/search", params={"q": "frieren"})).json()

    by_id = {row["id"]: row for row in body["results"]}
    assert by_id[anime_id]["list_status"] == "watching"
    others = [row["list_status"] for row in body["results"] if row["id"] != anime_id]
    assert others == [None] * (len(SEARCH_IDS) - 1)


async def test_search_with_no_hits_is_an_empty_page(user_client: AsyncClient) -> None:
    response = await user_client.get("/api/anime/search", params={"q": "zzzznothing"})

    assert response.status_code == 200
    assert response.json() == {"results": [], "page": 1, "has_next": False}


async def test_a_one_character_query_is_rejected(user_client: AsyncClient) -> None:
    assert (await user_client.get("/api/anime/search", params={"q": "f"})).status_code == 422
    assert (await user_client.get("/api/anime/search")).status_code == 422


async def test_search_needs_a_session(anon_client: AsyncClient) -> None:
    response = await anon_client.get("/api/anime/search", params={"q": "frieren"})

    assert response.status_code == 401
    assert response.json()["detail"] == NOT_AUTHENTICATED


# --- Show detail ------------------------------------------------------------


async def test_detail_fetches_caches_and_renders_frieren(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await frieren_id(user_client)
    response = await user_client.get(f"/api/anime/{anime_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == anime_id
    assert body["anilist_id"] == FRIEREN_ID
    assert body["mal_id"] == FRIEREN_MAL_ID
    assert body["source"] == "anilist"
    assert body["title"]["preferred"] == "Frieren: Beyond Journey’s End"
    assert body["episode_count"] == 28
    assert body["studio"] == "MADHOUSE"
    assert body["genres"] == ["Adventure", "Drama", "Fantasy"]
    assert body["banner_url"].startswith("https://")
    assert body["synopsis"].startswith("The adventure is over but life goes on")
    assert "<br>" not in body["synopsis"]
    assert body["next_airing"] is None
    assert body["list_entry"] is None
    assert {relation["relation_type"] for relation in body["relations"]} == {
        "SEQUEL",
        "SIDE_STORY",
        "CHARACTER",
        "OTHER",
    }

    episodes = body["episodes"]
    assert len(episodes) == 28
    assert [episode["number"] for episode in episodes] == list(range(1, 29))
    assert all(episode["aired"] for episode in episodes)
    assert all(episode["state"] == "not_wanted" for episode in episodes)
    assert all(episode["watched"] is False for episode in episodes)
    # AniList publishes 5..28; the four-episode premiere has no slot of its
    # own, so 1–4 are dated backwards from episode 5 and badged as estimates.
    assert all(episode["air_at_estimated"] is False for episode in episodes[4:])
    assert all(episode["air_at_estimated"] for episode in episodes[:4])
    assert episodes[0]["air_at"].startswith("2023-09-08T14:00:00")
    assert episodes[4]["air_at"].startswith("2023-10-06T14:00:00")
    assert episodes[-1]["air_at"].startswith("2024-03-22T14:00:00")
    air_times = [episode["air_at"] for episode in episodes]
    assert air_times == sorted(air_times)  # strictly ordered, estimates included
    assert len(set(air_times)) == 28

    async with api_factory() as session:
        stored = list((await session.scalars(select(Episode))).all())
    assert len(stored) == 28


async def test_relations_link_only_to_shows_arc_has_a_row_for(
    user_client: AsyncClient,
) -> None:
    """A relation's ``id`` is internal, so it is null for a show nobody added.

    The search page happens to have cached the sequel, which is what makes the
    two cases visible side by side in one response.
    """
    anime_id = await frieren_id(user_client)
    body = (await user_client.get(f"/api/anime/{anime_id}")).json()

    by_kind = {relation["relation_type"]: relation for relation in body["relations"]}
    sequel = by_kind["SEQUEL"]
    assert sequel["anilist_id"] == SEQUEL_ID
    assert sequel["id"] is not None  # cached by the search, so it is linkable
    assert sequel["id"] != sequel["anilist_id"]
    assert sequel["title"]["preferred"] == "Frieren: Beyond Journey’s End Season 2"


async def test_detail_marks_unaired_episodes_correctly(
    user_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixture show has five aired episodes and seven to come.

    Against a frozen clock: the fixture's air times are absolute, so without
    pinning "now" this test would pass until 2026-02-15 and then start
    reporting six aired episodes, then seven.
    """
    monkeypatch.setattr("arc.api.anime.now", lambda: FROZEN_NOW)
    anime_id = await internal_id(user_client, "airing", RELEASING_ID)
    body = (await user_client.get(f"/api/anime/{anime_id}")).json()

    aired = [episode["number"] for episode in body["episodes"] if episode["aired"]]
    assert aired == [1, 2, 3, 4, 5]
    assert body["next_airing"]["episode"] == 6
    assert body["next_airing"]["at"].startswith("2026-02-15T12:00:00")
    assert body["status"] == "RELEASING"


async def test_detail_of_a_long_running_show_marks_every_aired_episode(
    api_app: FastAPI, api_factory: SessionFactory, mal: FakeMal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """250 aired episodes over three schedule pages, and one still to come.

    This is the whole truncation finding end to end: one page of schedule used
    to mean air times for episodes 1–100 and nulls for 101–250, and a null on
    a releasing show read as "not aired". Both halves of the fix are needed
    here — the paging fills 101–250 in, and the boundary rule places episode
    251, which has no air time at all, after the line rather than before it.
    """
    fake = long_running_fake()
    fake.search["long"] = {
        "data": {
            "Page": {
                "pageInfo": {"currentPage": 1, "hasNextPage": False},
                "media": [summary_of(fake.media[LONG_RUNNING_ID]["data"]["Media"])],  # type: ignore[index]
            }
        }
    }
    api_app.state.catalog = CatalogService(fake.source(), mal.source(), Breaker(300.0))
    monkeypatch.setattr("arc.api.anime.now", lambda: FROZEN_NOW)
    await add_user(api_factory, "long@arc.test", USER_PASSWORD)
    async with api_transport(api_app) as client:
        await login(client, "long@arc.test", USER_PASSWORD)
        anime_id = await internal_id(client, "long", LONG_RUNNING_ID)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    episodes = body["episodes"]
    assert len(episodes) == LONG_RUNNING_AIRED + 1
    aired = [episode["number"] for episode in episodes if episode["aired"]]
    assert aired == list(range(1, LONG_RUNNING_AIRED + 1))
    assert episodes[-1]["number"] == LONG_RUNNING_AIRED + 1
    assert episodes[-1]["aired"] is False
    assert episodes[-1]["air_at"] is None
    assert body["next_airing"]["episode"] == LONG_RUNNING_AIRED + 1
    # Every aired one has a real date, not a null the client has to guess at.
    assert all(episode["air_at"] for episode in episodes[:LONG_RUNNING_AIRED])


async def test_a_finished_show_with_no_schedule_has_aired_entirely(
    catalogue_app: FastAPI, api_factory: SessionFactory, anilist: FakeAniList
) -> None:
    """AniList keeps no ``airingSchedule`` for older seasons.

    Twelve episodes, no dates, ``FINISHED``: every one of them has aired, and
    a show page that says otherwise is unusable for anything in the archive.
    """
    payload = load("media_999001_releasing")
    payload["data"]["Media"] |= {
        "id": 999003,
        "idMal": 999003,
        "status": "FINISHED",
        "episodes": 12,
        "nextAiringEpisode": None,
        "aired": {"pageInfo": {"currentPage": 1, "hasNextPage": False}, "nodes": []},
        "upcoming": {"nodes": []},
    }
    anilist.media[999003] = payload
    anilist.search["archive"] = {
        "data": {
            "Page": {
                "pageInfo": {"currentPage": 1, "hasNextPage": False},
                "media": [summary_of(payload["data"]["Media"])],
            }
        }
    }

    await add_user(api_factory, "archive@arc.test", USER_PASSWORD)
    async with api_transport(catalogue_app) as client:
        await login(client, "archive@arc.test", USER_PASSWORD)
        anime_id = await internal_id(client, "archive", 999003)
        body = (await client.get(f"/api/anime/{anime_id}")).json()

    episodes = body["episodes"]
    assert len(episodes) == 12
    assert all(episode["air_at"] is None for episode in episodes)
    assert all(episode["aired"] for episode in episodes)


async def test_detail_serves_a_fresh_row_without_asking_upstream(
    user_client: AsyncClient, anilist: FakeAniList
) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.get(f"/api/anime/{anime_id}")
    await user_client.get(f"/api/anime/{anime_id}")

    assert [name for name, _ in anilist.calls] == ["search", "media"]


async def test_detail_of_an_unknown_id_is_a_404(user_client: AsyncClient) -> None:
    response = await user_client.get("/api/anime/424242")

    assert response.status_code == 404
    assert response.json()["detail"] == "anime not found"


async def test_detail_needs_a_session(anon_client: AsyncClient) -> None:
    assert (await anon_client.get("/api/anime/1")).status_code == 401


# --- List states ------------------------------------------------------------


async def test_put_creates_the_entry_and_caches_the_show(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await frieren_id(user_client)
    response = await user_client.put(f"/api/list/{anime_id}", json={"status": "watching"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["anime_id"] == anime_id
    assert body["status"] == "watching"
    assert body["progress"] == 0
    assert body["score"] is None
    assert body["updated_at"]

    async with api_factory() as session:
        anime = await session.get(Anime, anime_id)
        episodes = list((await session.scalars(select(Episode))).all())
    assert anime is not None and anime.title_english == "Frieren: Beyond Journey’s End"
    assert len(episodes) == 28


async def test_put_marks_the_entry_dirty_for_mal_but_writes_nothing(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """FR-M4/FR-M7: Arc records the intent; M9's job is what writes."""
    before = datetime.now(UTC) - timedelta(seconds=1)
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching", "score": 9})

    entry = await entry_row(api_factory, anime_id)
    assert entry.mal_dirty is True
    assert entry.updated_by is UpdatedBy.ARC
    assert entry.mal_synced_at is None
    assert entry.updated_at >= before
    assert entry.score == 9


async def test_put_updates_an_existing_entry(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching"})

    response = await user_client.put(f"/api/list/{anime_id}", json={"progress": 4, "score": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "watching"  # untouched
    assert body["progress"] == 4
    assert body["score"] == 10


async def test_omitting_score_leaves_it_and_null_clears_it(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching", "score": 8})

    kept = await user_client.put(f"/api/list/{anime_id}", json={"progress": 1})
    assert kept.json()["score"] == 8

    cleared = await user_client.put(f"/api/list/{anime_id}", json={"score": None})
    assert cleared.json()["score"] is None


async def test_completed_sets_progress_to_the_episode_count(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    response = await user_client.put(f"/api/list/{anime_id}", json={"status": "completed"})

    assert response.status_code == 200
    assert response.json()["progress"] == 28


async def test_a_new_entry_without_a_status_is_rejected(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    response = await user_client.put(f"/api/list/{anime_id}", json={"progress": 3})

    assert response.status_code == 422
    assert "status is required" in response.json()["detail"]


async def test_an_out_of_range_score_is_rejected(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    high = await user_client.put(f"/api/list/{anime_id}", json={"status": "watching", "score": 11})
    assert high.status_code == 422

    low = await user_client.put(f"/api/list/{anime_id}", json={"status": "watching", "score": 0})
    assert low.status_code == 422

    negative = await user_client.put(
        f"/api/list/{anime_id}", json={"status": "watching", "progress": -1}
    )
    assert negative.status_code == 422


async def test_an_unknown_status_is_rejected(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    response = await user_client.put(f"/api/list/{anime_id}", json={"status": "rewatching"})

    assert response.status_code == 422


async def test_putting_a_show_arc_has_never_seen_is_a_404(user_client: AsyncClient) -> None:
    """The internal id has to exist: it is minted by a search, not by a client."""
    response = await user_client.put("/api/list/424242", json={"status": "watching"})

    assert response.status_code == 404
    assert response.json()["detail"] == "anime not found"


async def test_get_list_returns_the_show_and_the_entry(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching", "score": 9})

    response = await user_client.get("/api/list")

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["anime"]["id"] == anime_id
    assert rows[0]["anime"]["anilist_id"] == FRIEREN_ID
    assert rows[0]["anime"]["title"]["preferred"] == "Frieren: Beyond Journey’s End"
    assert rows[0]["anime"]["list_status"] == "watching"
    assert rows[0]["entry"] == {
        "anime_id": anime_id,
        "status": "watching",
        "progress": 0,
        "score": 9,
        "updated_at": rows[0]["entry"]["updated_at"],
        # The MyAnimeList badge is a show-page field (M9): computing it per row
        # here would be a query per card, and the list does not render it.
        "mal_sync": None,
    }


async def test_get_list_is_newest_change_first_and_filterable(
    user_client: AsyncClient,
) -> None:
    frieren = await frieren_id(user_client)
    airing = await internal_id(user_client, "airing", RELEASING_ID)
    await user_client.put(f"/api/list/{frieren}", json={"status": "watching"})
    await user_client.put(f"/api/list/{airing}", json={"status": "planned"})

    everything = (await user_client.get("/api/list")).json()
    assert [row["anime"]["id"] for row in everything] == [airing, frieren]

    planned = (await user_client.get("/api/list", params={"status": "planned"})).json()
    assert [row["anime"]["id"] for row in planned] == [airing]


async def test_a_list_is_private_to_its_owner(
    user_client: AsyncClient, catalogue_app: FastAPI, api_factory: SessionFactory
) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching"})

    await add_user(api_factory, "other@arc.test", "other-password")
    async with api_transport(catalogue_app) as other:
        await login(other, "other@arc.test", "other-password")
        assert (await other.get("/api/list")).json() == []
        detail = (await other.get(f"/api/anime/{anime_id}")).json()
        assert detail["list_status"] is None
        assert detail["list_entry"] is None


async def test_detail_shows_the_callers_entry(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching", "progress": 3})

    body = (await user_client.get(f"/api/anime/{anime_id}")).json()

    assert body["list_status"] == "watching"
    assert body["list_entry"]["status"] == "watching"
    assert body["list_entry"]["progress"] == 3


async def test_delete_removes_the_entry(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching"})

    removed = await user_client.delete(f"/api/list/{anime_id}")

    assert removed.status_code == 204
    assert (await user_client.get("/api/list")).json() == []


async def test_deleting_something_not_on_the_list_is_a_404(user_client: AsyncClient) -> None:
    anime_id = await frieren_id(user_client)
    response = await user_client.delete(f"/api/list/{anime_id}")

    assert response.status_code == 404
    assert response.json()["detail"] == "not on your list"


async def test_delete_keeps_the_cached_show(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await frieren_id(user_client)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching"})
    await user_client.delete(f"/api/list/{anime_id}")

    async with api_factory() as session:
        assert await session.get(Anime, anime_id) is not None


async def test_list_routes_need_a_session(anon_client: AsyncClient) -> None:
    assert (await anon_client.get("/api/list")).status_code == 401
    put = await anon_client.put("/api/list/1", json={"status": "watching"})
    assert put.status_code == 401
    assert (await anon_client.delete("/api/list/1")).status_code == 401


# --- The MAL fallback, end to end (FR-C6) -----------------------------------


@pytest.fixture
def outage(catalogue_app: FastAPI, anilist: FakeAniList) -> FakeAniList:
    """AniList answering 403 "temporarily disabled" to everything."""
    anilist.disabled = True
    return anilist


async def test_search_falls_back_to_mal_and_still_mints_internal_ids(
    user_client: AsyncClient, outage: FakeAniList, mal: FakeMal, slept: list[float]
) -> None:
    response = await user_client.get("/api/anime/search", params={"q": "frieren"})

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert results
    first = results[0]
    assert first["source"] == "mal"
    assert first["mal_id"] == FRIEREN_MAL_ID
    assert first["anilist_id"] is None
    assert first["id"] > 0
    assert first["title"]["preferred"] == "Frieren: Beyond Journey's End"
    assert len(mal.calls) == 1


async def test_adding_a_mal_sourced_show_creates_estimated_episodes(
    user_client: AsyncClient, outage: FakeAniList, slept: list[float]
) -> None:
    """The FR-C6 promise in one request: list changes keep working."""
    anime_id = await internal_id(user_client, "frieren", FRIEREN_MAL_ID)

    added = await user_client.put(f"/api/list/{anime_id}", json={"status": "watching"})
    assert added.status_code == 200, added.text

    body = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert body["source"] == "mal"
    assert body["anilist_id"] is None
    assert body["mal_id"] == FRIEREN_MAL_ID
    assert body["episode_count"] == 28
    assert body["synopsis"].startswith("During their decade-long quest")
    episodes = body["episodes"]
    assert len(episodes) == 28
    assert all(episode["air_at_estimated"] for episode in episodes)
    assert episodes[0]["air_at"].startswith("2023-09-29T14:00:00")
    assert episodes[1]["air_at"].startswith("2023-10-06T14:00:00")


async def test_both_sources_down_is_a_502_that_names_the_catalogue(
    user_client: AsyncClient, catalogue_app: FastAPI, anilist: FakeAniList, slept: list[float]
) -> None:
    anilist.disabled = True
    catalogue_app.state.catalog = CatalogService(
        anilist.source(), FakeMal(fail_with=503).source(), Breaker(300.0)
    )

    response = await user_client.get("/api/anime/search", params={"q": "frieren"})

    assert response.status_code == 502
    assert response.json()["detail"] == "catalogue is unavailable"


async def test_reconciling_and_refreshing_upgrades_a_mal_sourced_row(
    user_client: AsyncClient,
    catalogue_app: FastAPI,
    api_factory: SessionFactory,
    outage: FakeAniList,
    settings: Any,
    slept: list[float],
) -> None:
    """The whole M3b round trip, through the API and the two jobs.

    Search and add during an outage, then AniList comes back: the
    reconciliation attaches the AniList id, the refresh replaces the estimates,
    and the show page the user had open is still the same show at the same URL.
    """
    import logging
    from contextlib import asynccontextmanager

    from arc.models import Job, JobStatus
    from arc.services.catalog import jobs as catalog_jobs
    from arc.services.jobs import JobContext

    anime_id = await internal_id(user_client, "frieren", FRIEREN_MAL_ID)
    await user_client.put(f"/api/list/{anime_id}", json={"status": "watching"})

    outage.disabled = False
    service: CatalogService = catalogue_app.state.catalog
    service.breaker.reset()

    @asynccontextmanager
    async def fake_catalog_for(_settings: Any) -> AsyncIterator[CatalogService]:
        yield service

    async with api_factory() as session:
        ctx = JobContext(
            job=Job(id=1, type="x", payload={}, status=JobStatus.RUNNING),
            session=session,
            settings=settings,
            log=logging.getLogger("test.jobs"),
        )
        original = catalog_jobs.catalog_for
        catalog_jobs.catalog_for = fake_catalog_for  # type: ignore[assignment]
        try:
            await catalog_jobs.catalog_reconcile(ctx)
            await session.commit()
            ctx.job.payload = {"anime_id": anime_id}
            await catalog_jobs.catalog_refresh(ctx)
            await session.commit()
        finally:
            catalog_jobs.catalog_for = original  # type: ignore[assignment]

    body = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert body["id"] == anime_id  # the URL the user had open still works
    assert body["anilist_id"] == FRIEREN_ID
    assert body["mal_id"] == FRIEREN_MAL_ID
    assert body["source"] == "anilist"
    assert body["synopsis"].startswith("The adventure is over but life goes on")
    assert all(episode["air_at_estimated"] is False for episode in body["episodes"][4:])
    assert body["episodes"][4]["air_at"].startswith("2023-10-06T14:00:00")

    # Still one row, and the list entry still points at it.
    async with api_factory() as session:
        rows = list((await session.scalars(select(Anime))).all())
        entries = list((await session.scalars(select(ListEntry))).all())
    assert len([row for row in rows if row.mal_id == FRIEREN_MAL_ID]) == 1
    assert [entry.anime_id for entry in entries] == [anime_id]


# --- Catalogue status (admin) -----------------------------------------------


async def test_catalog_status_is_admin_only(user_client: AsyncClient) -> None:
    response = await user_client.get("/api/catalog/status")

    assert response.status_code == 403
    assert response.json()["detail"] == ADMIN_REQUIRED


async def test_catalog_status_needs_a_session(anon_client: AsyncClient) -> None:
    assert (await anon_client.get("/api/catalog/status")).status_code == 401


async def test_catalog_status_reports_a_healthy_catalogue(
    catalogue_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(catalogue_app) as admin:
        await login(admin, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = (await admin.get("/api/catalog/status")).json()

    assert body["active"] == "anilist"
    assert body["sources"]["anilist"]["state"] == "closed"
    assert body["sources"]["mal"] == {
        "state": "closed",
        "healthy_at": None,
        "failed_at": None,
        "reason": None,
        "configured": True,
    }


async def test_catalog_status_shows_the_open_breaker_after_an_outage(
    catalogue_app: FastAPI, api_factory: SessionFactory, outage: FakeAniList, slept: list[float]
) -> None:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(catalogue_app) as admin:
        await login(admin, ADMIN_EMAIL, ADMIN_PASSWORD)
        await admin.get("/api/anime/search", params={"q": "frieren"})
        body = (await admin.get("/api/catalog/status")).json()

    assert body["active"] == "mal"
    anilist_state = body["sources"]["anilist"]
    assert anilist_state["state"] == "open"
    assert anilist_state["failed_at"] is not None
    assert "disabled" in anilist_state["reason"]
    assert body["sources"]["mal"]["state"] == "closed"
    assert body["sources"]["mal"]["healthy_at"] is not None


async def test_catalog_status_reports_an_unconfigured_fallback(
    api_app: FastAPI, api_factory: SessionFactory, anilist: FakeAniList, mal: FakeMal
) -> None:
    api_app.state.catalog = CatalogService(
        anilist.source(), mal.source(client_id=None), Breaker(300.0)
    )
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as admin:
        await login(admin, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = (await admin.get("/api/catalog/status")).json()

    assert body["sources"]["mal"]["configured"] is False
    assert body["active"] == "anilist"


# --- Manual refresh ---------------------------------------------------------


async def test_refresh_is_admin_only(user_client: AsyncClient) -> None:
    response = await user_client.post("/api/anime/1/refresh")

    assert response.status_code == 403
    assert response.json()["detail"] == ADMIN_REQUIRED


async def test_refresh_enqueues_one_job_however_often_it_is_pressed(
    catalogue_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(catalogue_app) as admin:
        await login(admin, ADMIN_EMAIL, ADMIN_PASSWORD)
        anime_id = await frieren_id(admin)

        first = await admin.post(f"/api/anime/{anime_id}/refresh")
        second = await admin.post(f"/api/anime/{anime_id}/refresh")

    assert first.status_code == 202, first.text
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]

    async with api_factory() as session:
        jobs = list((await session.scalars(select(Job))).all())
    assert len(jobs) == 1
    assert jobs[0].type == "catalog_refresh"
    assert jobs[0].payload["anime_id"] == anime_id
    assert jobs[0].payload["dedupe_key"] == f"catalog_refresh:{anime_id}"


async def test_refresh_needs_a_session(anon_client: AsyncClient) -> None:
    response = await anon_client.post("/api/anime/1/refresh")

    assert response.status_code == 401


# --- Status vocabulary ------------------------------------------------------


@pytest.mark.parametrize("status", [state.value for state in ListStatus])
async def test_every_list_state_is_accepted(user_client: AsyncClient, status: str) -> None:
    anime_id = await frieren_id(user_client)
    response = await user_client.put(f"/api/list/{anime_id}", json={"status": status})

    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["status"] == status
