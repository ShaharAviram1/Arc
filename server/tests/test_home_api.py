"""Behind-by and new-this-week over HTTP (FR-C4, FR-W1).

The counting rules are the whole content of the home page, so they are tested
against a frozen clock and real rows: an episode is behind or it is not
depending on one comparison, and the comparison moves on its own.

The seeding helpers come from :mod:`tests.test_schedule_api` — the two pages
read the same two tables, and a second copy of "make a show with weekly
episodes" is a second place for the fixtures to drift.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from arc.db import SessionFactory
from arc.models import ListStatus, User
from arc.services.catalog.progress import NEW_LIMIT
from tests.conftest import add_user, api_transport, login
from tests.test_schedule_api import add_anime, add_episodes, follow

pytestmark = pytest.mark.pg

USER_EMAIL = "home@arc.test"
USER_PASSWORD = "home-password"

#: The frozen present. A Wednesday, so "aired eight days ago" and "aired two
#: days ago" are unambiguous.
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

#: Episode 1 of the twelve-episode show under test, placed so that episodes
#: 1–7 have aired and 8–12 have not: episode 7 airs a day before ``NOW``.
FIRST_AIRED = NOW - timedelta(weeks=6, days=1)

#: The air time of episode 7, which is what "behind" sorts by.
SEVENTH_AIRED = FIRST_AIRED + timedelta(weeks=6)


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr("arc.api.home.now", lambda: NOW)
    return NOW


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


async def airing_show(
    factory: SessionFactory,
    *,
    title: str,
    anilist_id: int,
    episodes: int = 12,
    aired: int = 7,
    first_at: datetime = FIRST_AIRED,
    estimated: bool = False,
) -> int:
    """A releasing show with ``aired`` of ``episodes`` behind the clock."""
    anime_id = await add_anime(
        factory,
        title=title,
        anilist_id=anilist_id,
        status="RELEASING",
        episodes=episodes,
        next_at=first_at + timedelta(weeks=aired),
        next_episode=aired + 1,
    )
    await add_episodes(factory, anime_id, count=episodes, first_at=first_at, estimated=estimated)
    return anime_id


async def home(client: AsyncClient) -> dict[str, list[dict[str, object]]]:
    response = await client.get("/api/home")
    assert response.status_code == 200, response.text
    body: dict[str, list[dict[str, object]]] = response.json()
    return body


def behind_titles(body: dict[str, list[dict[str, object]]]) -> list[str]:
    return [row["anime"]["title"]["preferred"] for row in body["behind"]]


# --- Behind on ---------------------------------------------------------------


async def test_a_watching_show_reports_how_far_behind_the_user_is(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """Seven aired, three watched: behind by four (FR-C4)."""
    anime_id = await airing_show(api_factory, title="Show A", anilist_id=910001)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=3)

    body = await home(client)

    assert len(body["behind"]) == 1
    row = body["behind"][0]
    assert row["aired"] == 7
    assert row["behind"] == 4
    assert row["entry"]["progress"] == 3
    assert row["entry"]["status"] == "watching"
    assert row["latest_aired_at"] == SEVENTH_AIRED.isoformat().replace("+00:00", "Z")
    assert row["anime"]["list_status"] == "watching"


async def test_a_show_watched_to_the_end_is_not_behind(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await airing_show(
        api_factory,
        title="Show B",
        anilist_id=910002,
        episodes=12,
        aired=12,
        first_at=NOW - timedelta(weeks=12),
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=12)

    assert (await home(client))["behind"] == []


async def test_a_dropped_show_is_never_behind(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """FR-W4: a dropped show generates nothing, including guilt."""
    anime_id = await airing_show(api_factory, title="Show C", anilist_id=910003)
    await follow(api_factory, user, anime_id, ListStatus.DROPPED, progress=0)

    assert (await home(client))["behind"] == []


@pytest.mark.parametrize("status", [ListStatus.PLANNED, ListStatus.ON_HOLD, ListStatus.COMPLETED])
async def test_only_watching_shows_are_counted_as_behind(
    client: AsyncClient, user: User, api_factory: SessionFactory, status: ListStatus
) -> None:
    anime_id = await airing_show(api_factory, title="Show D", anilist_id=910004)
    await follow(api_factory, user, anime_id, status, progress=0)

    assert (await home(client))["behind"] == []


async def test_a_show_nobody_has_started_is_behind_by_everything_aired(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await airing_show(api_factory, title="Show E", anilist_id=910005)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)

    row = (await home(client))["behind"][0]

    assert (row["aired"], row["behind"]) == (7, 7)


async def test_estimated_air_dates_count_once_they_are_past(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """A MAL-sourced show is still a show the user is behind on (FR-C6)."""
    anime_id = await airing_show(
        api_factory, title="Estimated Show", anilist_id=910006, estimated=True
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=2)

    row = (await home(client))["behind"][0]

    assert (row["aired"], row["behind"]) == (7, 5)


async def test_a_finished_show_with_no_dates_still_counts_its_episodes(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """AniList keeps no schedule for old seasons; that is missing data, not the future."""
    anime_id = await add_anime(
        api_factory,
        title="Old Show",
        anilist_id=910007,
        status="FINISHED",
        episodes=26,
        season="FALL",
        season_year=2005,
    )
    await add_episodes(api_factory, anime_id, count=26, first_at=None)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=4)

    row = (await home(client))["behind"][0]

    assert (row["aired"], row["behind"]) == (26, 22)
    assert row["latest_aired_at"] is None


async def test_behind_is_sorted_by_the_newest_episode_first(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    older = await airing_show(
        api_factory,
        title="Older",
        anilist_id=910008,
        first_at=FIRST_AIRED - timedelta(days=3),
    )
    newer = await airing_show(api_factory, title="Newer", anilist_id=910009)
    undated = await add_anime(
        api_factory, title="Undated", anilist_id=910010, status="FINISHED", episodes=3
    )
    await add_episodes(api_factory, undated, count=3, first_at=None)
    for anime_id in (older, newer, undated):
        await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)

    assert behind_titles(await home(client)) == ["Newer", "Older", "Undated"]


# --- New this week ------------------------------------------------------------


async def test_an_episode_from_this_week_is_listed(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await airing_show(api_factory, title="Show A", anilist_id=910011)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=3)

    body = await home(client)

    numbers = [row["episode"]["number"] for row in body["new_this_week"]]
    assert numbers == [7]
    row = body["new_this_week"][0]
    assert row["episode"]["aired"] is True
    assert row["anime"]["title"]["preferred"] == "Show A"
    assert row["anime"]["list_status"] == "watching"


async def test_a_planned_show_is_new_this_week_but_never_behind(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await airing_show(api_factory, title="Planned Show", anilist_id=910012)
    await follow(api_factory, user, anime_id, ListStatus.PLANNED, progress=0)

    body = await home(client)

    assert body["behind"] == []
    assert [row["episode"]["number"] for row in body["new_this_week"]] == [7]


async def test_an_episode_from_eight_days_ago_is_not_this_week(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The window is seven days, and it is a window, not a "recently"."""
    anime_id = await add_anime(
        api_factory, title="Fortnightly", anilist_id=910013, status="RELEASING", episodes=2
    )
    await add_episodes(
        api_factory,
        anime_id,
        count=2,
        first_at=NOW - timedelta(days=15),
        step=timedelta(days=7),
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)

    body = await home(client)

    assert body["new_this_week"] == []
    # It is still an episode the user is behind on; only the *week* excludes it.
    assert body["behind"][0]["behind"] == 2


async def test_an_episode_that_has_not_aired_yet_is_not_this_week(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await add_anime(
        api_factory, title="Tomorrow", anilist_id=910014, status="RELEASING", episodes=1
    )
    await add_episodes(api_factory, anime_id, count=1, first_at=NOW + timedelta(days=1))
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)

    body = await home(client)

    assert body["new_this_week"] == []
    assert body["behind"] == []


async def test_dropped_and_completed_shows_are_not_new_this_week(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    dropped = await airing_show(api_factory, title="Dropped", anilist_id=910015)
    completed = await airing_show(api_factory, title="Completed", anilist_id=910016)
    await follow(api_factory, user, dropped, ListStatus.DROPPED)
    await follow(api_factory, user, completed, ListStatus.COMPLETED, progress=7)

    assert (await home(client))["new_this_week"] == []


async def test_new_this_week_is_newest_first_and_capped(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """Sixty daily episodes in one week is not a real show; the cap is real."""
    anime_id = await add_anime(
        api_factory, title="Daily", anilist_id=910017, status="RELEASING", episodes=60
    )
    await add_episodes(
        api_factory,
        anime_id,
        count=60,
        first_at=NOW - timedelta(hours=60),
        step=timedelta(hours=1),
    )
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)

    rows = (await home(client))["new_this_week"]

    assert len(rows) == NEW_LIMIT
    numbers = [row["episode"]["number"] for row in rows]
    assert numbers == sorted(numbers, reverse=True)
    assert numbers[0] == 60


async def test_another_users_list_is_not_on_this_home_page(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    other = await add_user(api_factory, "not.me@arc.test", "other-password")
    anime_id = await airing_show(api_factory, title="Theirs", anilist_id=910018)
    await follow(api_factory, other, anime_id, ListStatus.WATCHING, progress=0)

    body = await home(client)

    assert body["behind"] == []
    assert body["new_this_week"] == []


async def test_continue_watching_is_present_and_empty_until_m8(
    client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await airing_show(api_factory, title="Show A", anilist_id=910019)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=1)

    assert (await home(client))["continue_watching"] == []
