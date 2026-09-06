"""The schedule and home endpoints over HTTP (FR-C3, FR-C4, FR-W1).

The app under test is the real one against the real test database. Nothing is
mocked at all here — unlike the catalogue tests there are no sockets to
replace, because neither endpoint calls a source: they read the rows the season
pre-cache wrote (FR-C7). What *is* pinned is the clock, through the ``now``
seam each router exposes, because every number on both pages is a comparison
against the present.

The seeded season is deliberately mixed: an AniList row with a published next
episode, a MAL row with the slot Arc synthesised from a broadcast time, a
finished show with only episode rows to go on, a film, and a show nobody knows
anything about yet. Those five are the whole taxonomy of what the cache can
hold.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.api.deps import ADMIN_REQUIRED, NOT_AUTHENTICATED
from arc.db import SessionFactory
from arc.models import Anime, Episode, Job, ListEntry, ListStatus, User, UserRole
from arc.services.catalog.names import SEASON_SWEEP
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "weekly@arc.test"
USER_PASSWORD = "weekly-password"

#: A Wednesday in the middle of FALL 2026, which is therefore the season the
#: endpoint defaults to under this clock.
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

#: The two broadcasts the seeded season hangs on. The Saturday one is late
#: enough to be Sunday in Tokyo, which is the case the whole timezone rule
#: exists for.
FRIDAY_1400Z = datetime(2026, 11, 6, 14, 0, tzinfo=UTC)
SATURDAY_1600Z = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
MONDAY_1000Z = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

MONDAY = 0
FRIDAY = 4
SATURDAY = 5
SUNDAY = 6


# --- Seeding -----------------------------------------------------------------


async def add_anime(
    factory: SessionFactory,
    *,
    title: str,
    anilist_id: int | None = None,
    mal_id: int | None = None,
    source: str = "anilist",
    format: str | None = "TV",
    status: str | None = "RELEASING",
    season: str | None = "FALL",
    season_year: int | None = 2026,
    episodes: int | None = None,
    next_at: datetime | None = None,
    next_episode: int | None = None,
    estimated: bool = False,
) -> int:
    """One ``anime`` row, written the way the season sweep would write it.

    ``estimated`` marks the slot as one Arc synthesised from a MAL broadcast
    time rather than one AniList published (FR-C6).
    """
    async with factory() as session:
        anime = Anime(
            anilist_id=anilist_id,
            mal_id=mal_id,
            summary_source=source,
            title_romaji=title,
            format=format,
            status=status,
            season=season,
            season_year=season_year,
            episodes=episodes,
        )
        if next_at is not None:
            anime.next_airing = {
                "episode": next_episode,
                "airingAt": int(next_at.timestamp()),
            }
            if estimated:
                anime.next_airing["estimated"] = True
        session.add(anime)
        await session.commit()
        return anime.id


async def add_episodes(
    factory: SessionFactory,
    anime_id: int,
    *,
    count: int,
    first_at: datetime | None,
    step: timedelta = timedelta(weeks=1),
    estimated: bool = False,
) -> None:
    """``count`` weekly episodes from ``first_at``; ``None`` for undated ones."""
    async with factory() as session:
        for number in range(1, count + 1):
            session.add(
                Episode(
                    anime_id=anime_id,
                    number=number,
                    air_at=None if first_at is None else first_at + step * (number - 1),
                    air_at_estimated=estimated,
                )
            )
        await session.commit()


async def follow(
    factory: SessionFactory,
    user: User,
    anime_id: int,
    status: ListStatus,
    *,
    progress: int = 0,
) -> None:
    async with factory() as session:
        session.add(ListEntry(user_id=user.id, anime_id=anime_id, status=status, progress=progress))
        await session.commit()


async def set_timezone(factory: SessionFactory, user: User, name: str) -> None:
    async with factory() as session:
        row = await session.get(User, user.id)
        assert row is not None
        row.timezone = name
        await session.commit()


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin both routers' clocks to :data:`NOW`."""
    monkeypatch.setattr("arc.api.schedule.now", lambda: NOW)
    monkeypatch.setattr("arc.api.home.now", lambda: NOW)
    return NOW


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def user_client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as client:
        yield await login(client, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def anon_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as client:
        yield client


@pytest.fixture
async def season(api_factory: SessionFactory) -> dict[str, int]:
    """Five FALL 2026 rows: everything the cache can look like."""
    friday = await add_anime(
        api_factory,
        title="Friday Night Show",
        anilist_id=900001,
        format="TV",
        episodes=12,
        next_at=FRIDAY_1400Z,
        next_episode=7,
    )
    saturday = await add_anime(
        api_factory,
        title="Saturday Late Show",
        mal_id=900002,
        source="mal",
        format="TV",
        episodes=13,
        # A MAL row: the slot is known, the episode number is not, and the
        # whole thing is Arc's own arithmetic (FR-C6).
        next_at=SATURDAY_1600Z,
        next_episode=None,
        estimated=True,
    )
    monday = await add_anime(
        api_factory,
        title="Monday Rerun",
        anilist_id=900003,
        format="ONA",
        status="FINISHED",
        episodes=6,
    )
    await add_episodes(api_factory, monday, count=6, first_at=MONDAY_1000Z - timedelta(weeks=5))
    film = await add_anime(
        api_factory,
        title="A Film",
        anilist_id=900004,
        format="MOVIE",
        status="NOT_YET_RELEASED",
        next_at=FRIDAY_1400Z,
        next_episode=1,
    )
    unknown = await add_anime(
        api_factory,
        title="Announced Only",
        anilist_id=900005,
        format="TV",
        status="NOT_YET_RELEASED",
    )
    return {
        "friday": friday,
        "saturday": saturday,
        "monday": monday,
        "film": film,
        "unknown": unknown,
    }


def titles(day: dict[str, object]) -> list[str]:
    entries = day["entries"]
    assert isinstance(entries, list)
    return [entry["anime"]["title"]["preferred"] for entry in entries]


# --- Access ------------------------------------------------------------------


async def test_the_schedule_needs_a_session(anon_client: AsyncClient) -> None:
    response = await anon_client.get("/api/schedule")

    assert response.status_code == 401
    assert response.json()["detail"] == NOT_AUTHENTICATED


async def test_home_needs_a_session(anon_client: AsyncClient) -> None:
    response = await anon_client.get("/api/home")

    assert response.status_code == 401
    assert response.json()["detail"] == NOT_AUTHENTICATED


# --- The season ---------------------------------------------------------------


async def test_the_schedule_defaults_to_the_current_season(
    user_client: AsyncClient, season: dict[str, int]
) -> None:
    response = await user_client.get("/api/schedule")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["year"], body["season"]) == (2026, "FALL")
    assert body["prev"] == {"year": 2026, "season": "SUMMER"}
    assert body["next"] == {"year": 2027, "season": "WINTER"}
    assert [day["weekday"] for day in body["days"]] == [0, 1, 2, 3, 4, 5, 6]


async def test_an_explicit_season_is_used_as_given(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    await add_anime(
        api_factory,
        title="Spring Thing",
        anilist_id=900010,
        season="SPRING",
        season_year=2025,
        next_at=FRIDAY_1400Z,
        next_episode=2,
    )

    response = await user_client.get("/api/schedule", params={"year": 2025, "season": "SPRING"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["year"], body["season"]) == (2025, "SPRING")
    assert body["prev"] == {"year": 2025, "season": "WINTER"}
    assert body["next"] == {"year": 2025, "season": "SUMMER"}
    assert titles(body["days"][FRIDAY]) == ["Spring Thing"]


async def test_half_a_season_falls_back_to_the_current_half(user_client: AsyncClient) -> None:
    """``?season=WINTER`` means this year's winter, not a 422."""
    response = await user_client.get("/api/schedule", params={"season": "WINTER"})

    assert response.status_code == 200, response.text
    assert (response.json()["year"], response.json()["season"]) == (2026, "WINTER")


async def test_a_season_that_is_not_a_season_is_rejected(user_client: AsyncClient) -> None:
    response = await user_client.get("/api/schedule", params={"season": "AUTUMN"})

    assert response.status_code == 422


async def test_an_absurd_year_is_rejected(user_client: AsyncClient) -> None:
    response = await user_client.get("/api/schedule", params={"year": 99999999})

    assert response.status_code == 422


# --- Placement ----------------------------------------------------------------


async def test_each_kind_of_row_lands_where_it_belongs(
    user_client: AsyncClient, season: dict[str, int]
) -> None:
    response = await user_client.get("/api/schedule")

    body = response.json()
    assert body["timezone"] == "UTC"
    assert titles(body["days"][MONDAY]) == ["Monday Rerun"]
    assert titles(body["days"][FRIDAY]) == ["Friday Night Show"]
    assert titles(body["days"][SATURDAY]) == ["Saturday Late Show"]
    # The film and the show with no dates at all.
    assert sorted(entry["anime"]["title"]["preferred"] for entry in body["unscheduled"]) == [
        "A Film",
        "Announced Only",
    ]
    placed = sum(len(day["entries"]) for day in body["days"])
    assert placed == 3


async def test_the_entries_carry_the_next_broadcast_when_the_source_knows_it(
    user_client: AsyncClient, season: dict[str, int]
) -> None:
    body = (await user_client.get("/api/schedule")).json()

    anilist_row = body["days"][FRIDAY]["entries"][0]
    assert anilist_row["air_time_local"] == "14:00"
    assert anilist_row["next_episode"] == 7
    assert anilist_row["next_at"] == FRIDAY_1400Z.isoformat().replace("+00:00", "Z")
    # AniList published this one, so it is not badged.
    assert anilist_row["next_at_estimated"] is False

    # The MAL row knows when, not which, and only approximately (FR-C6).
    mal_row = body["days"][SATURDAY]["entries"][0]
    assert mal_row["air_time_local"] == "16:00"
    assert mal_row["next_episode"] is None
    assert mal_row["next_at"] is not None
    assert mal_row["next_at_estimated"] is True

    # The finished show has only its episode rows to go on.
    finished = body["days"][MONDAY]["entries"][0]
    assert finished["air_time_local"] == "10:00"
    assert finished["next_episode"] is None
    assert finished["next_at"] is None
    assert finished["next_at_estimated"] is False


async def test_the_week_is_the_users_own(
    user_client: AsyncClient, user: User, api_factory: SessionFactory, season: dict[str, int]
) -> None:
    """Tokyo is nine hours ahead, so the late Saturday show is a Sunday one."""
    await set_timezone(api_factory, user, "Asia/Tokyo")

    body = (await user_client.get("/api/schedule")).json()

    assert body["timezone"] == "Asia/Tokyo"
    assert titles(body["days"][SATURDAY]) == []
    assert titles(body["days"][SUNDAY]) == ["Saturday Late Show"]
    assert body["days"][SUNDAY]["entries"][0]["air_time_local"] == "01:00"
    # And the Friday afternoon broadcast is Friday night in Tokyo, not Saturday.
    assert body["days"][FRIDAY]["entries"][0]["air_time_local"] == "23:00"


async def test_a_broken_timezone_renders_in_utc(
    user_client: AsyncClient, user: User, api_factory: SessionFactory, season: dict[str, int]
) -> None:
    await set_timezone(api_factory, user, "Nowhere/Special")

    body = (await user_client.get("/api/schedule")).json()

    assert body["timezone"] == "UTC"
    assert titles(body["days"][SATURDAY]) == ["Saturday Late Show"]


async def test_followed_shows_are_marked(
    user_client: AsyncClient, user: User, api_factory: SessionFactory, season: dict[str, int]
) -> None:
    await follow(api_factory, user, season["friday"], ListStatus.WATCHING)
    await follow(api_factory, user, season["saturday"], ListStatus.DROPPED)

    body = (await user_client.get("/api/schedule")).json()

    friday = body["days"][FRIDAY]["entries"][0]
    assert friday["following"] is True
    assert friday["list_status"] == "watching"
    assert friday["anime"]["list_status"] == "watching"

    saturday = body["days"][SATURDAY]["entries"][0]
    assert saturday["following"] is False
    assert saturday["list_status"] == "dropped"


async def test_another_users_list_does_not_leak_into_the_schedule(
    user_client: AsyncClient, api_factory: SessionFactory, season: dict[str, int]
) -> None:
    other = await add_user(api_factory, "someone.else@arc.test", "another-password")
    await follow(api_factory, other, season["friday"], ListStatus.WATCHING)

    body = (await user_client.get("/api/schedule")).json()

    assert body["days"][FRIDAY]["entries"][0]["following"] is False


async def test_an_empty_season_is_seven_empty_days(user_client: AsyncClient) -> None:
    body = (await user_client.get("/api/schedule", params={"year": 1994})).json()

    assert len(body["days"]) == 7
    assert all(day["entries"] == [] for day in body["days"])
    assert body["unscheduled"] == []


# --- The admin season sweep ---------------------------------------------------


@pytest.fixture
async def admin_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as client:
        yield await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


async def test_an_ordinary_user_cannot_trigger_the_season_sweep(user_client: AsyncClient) -> None:
    response = await user_client.post("/api/catalog/season-sweep")

    assert response.status_code == 403
    assert response.json()["detail"] == ADMIN_REQUIRED


async def test_an_admin_queues_the_season_sweep(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    response = await admin_client.post("/api/catalog/season-sweep")

    assert response.status_code == 202, response.text
    assert response.json()["type"] == SEASON_SWEEP

    async with api_factory() as session:
        jobs = list((await session.scalars(select(Job).where(Job.type == SEASON_SWEEP))).all())
    assert len(jobs) == 1
    assert jobs[0].payload["dedupe_key"] == SEASON_SWEEP


async def test_pressing_the_button_twice_queues_one_sweep(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The worker's nightly run uses the same key, so it dedupes against it too."""
    first = await admin_client.post("/api/catalog/season-sweep")
    second = await admin_client.post("/api/catalog/season-sweep")

    assert first.json()["id"] == second.json()["id"]
    async with api_factory() as session:
        jobs = list((await session.scalars(select(Job).where(Job.type == SEASON_SWEEP))).all())
    assert len(jobs) == 1
