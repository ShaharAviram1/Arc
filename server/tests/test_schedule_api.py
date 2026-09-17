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

#: Two days before :data:`NOW`, so an episode dated here is inside the week the
#: carried-over rule looks at (:data:`~arc.services.catalog.schedule.
#: AIRING_WINDOW`) while ``MONDAY_1000Z`` above — nine days back — is not.
LAST_MONDAY_1000Z = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)

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


# --- Shows carried into the current week from another season -----------------
#
# The Slime case (owner, 2026-09-13): a two-cour show tagged with the season it
# started in, still airing every Friday, absent from the grid the owner looks
# at. The current week's membership is "what is on", not "what started when".


async def add_two_cour(
    factory: SessionFactory,
    *,
    title: str = "Second Cour",
    anilist_id: int = 900020,
    status: str | None = "RELEASING",
    format: str | None = "TV",
    season: str | None = "SPRING",
    season_year: int | None = 2026,
    next_at: datetime | None = FRIDAY_1400Z,
    next_episode: int | None = 23,
) -> int:
    """A show tagged with an earlier season than the one being rendered."""
    return await add_anime(
        factory,
        title=title,
        anilist_id=anilist_id,
        status=status,
        format=format,
        season=season,
        season_year=season_year,
        episodes=24,
        next_at=next_at,
        next_episode=next_episode,
    )


async def test_a_two_cour_show_from_an_earlier_season_is_on_the_current_week(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    await add_two_cour(api_factory)

    body = (await user_client.get("/api/schedule")).json()

    assert (body["year"], body["season"]) == (2026, "FALL")
    # Both air at 14:00, so the day's order is by title.
    assert titles(body["days"][FRIDAY]) == ["Friday Night Show", "Second Cour"]
    carried = body["days"][FRIDAY]["entries"][1]
    assert carried["carried_over"] is True
    assert carried["air_time_local"] == "14:00"
    assert carried["next_episode"] == 23
    # The season tag is untouched: the grid's membership changed, not the row.
    assert (carried["anime"]["season"], carried["anime"]["season_year"]) == ("SPRING", 2026)
    # And a row that is here because of its own season is not marked.
    assert body["days"][FRIDAY]["entries"][0]["carried_over"] is False


async def test_a_carried_over_show_is_placed_in_the_users_own_week(
    user_client: AsyncClient, user: User, api_factory: SessionFactory, season: dict[str, int]
) -> None:
    """Same timezone rule as every other row: Saturday 16:00Z is Sunday in Tokyo."""
    await add_two_cour(api_factory, next_at=SATURDAY_1600Z)
    await set_timezone(api_factory, user, "Asia/Tokyo")

    body = (await user_client.get("/api/schedule")).json()

    assert "Second Cour" not in titles(body["days"][SATURDAY])
    assert titles(body["days"][SUNDAY]) == ["Saturday Late Show", "Second Cour"]
    assert body["days"][SUNDAY]["entries"][1]["air_time_local"] == "01:00"


async def test_a_long_runner_with_no_season_at_all_is_on_the_current_week(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """One Piece-style: ``season`` and ``season_year`` both null, still on air."""
    await add_two_cour(
        api_factory, title="The Long Runner", season=None, season_year=None, next_episode=1140
    )

    body = (await user_client.get("/api/schedule")).json()

    assert "The Long Runner" in titles(body["days"][FRIDAY])
    entry = next(
        row
        for row in body["days"][FRIDAY]["entries"]
        if row["anime"]["title"]["preferred"] == "The Long Runner"
    )
    assert entry["carried_over"] is True
    assert entry["anime"]["season"] is None


async def test_a_followed_carried_over_show_is_still_highlighted(
    user_client: AsyncClient, user: User, api_factory: SessionFactory, season: dict[str, int]
) -> None:
    anime_id = await add_two_cour(api_factory)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING)

    body = (await user_client.get("/api/schedule")).json()

    carried = body["days"][FRIDAY]["entries"][1]
    assert carried["following"] is True
    assert carried["list_status"] == "watching"


async def test_an_episode_dated_this_week_carries_a_slotless_show_in(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """No ``next_airing`` blob, but an episode dated inside the window."""
    anime_id = await add_two_cour(api_factory, title="Undated Slot Show", next_at=None)
    await add_episodes(
        api_factory, anime_id, count=3, first_at=LAST_MONDAY_1000Z - timedelta(weeks=2)
    )

    body = (await user_client.get("/api/schedule")).json()

    assert titles(body["days"][MONDAY]) == ["Monday Rerun", "Undated Slot Show"]
    assert body["days"][MONDAY]["entries"][1]["carried_over"] is True


async def test_a_finished_show_from_the_previous_season_is_not_carried_in(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """It aired two days ago and is over; it is not on this week."""
    anime_id = await add_two_cour(
        api_factory, title="Summer, Over", status="FINISHED", season="SUMMER", next_at=None
    )
    await add_episodes(
        api_factory, anime_id, count=12, first_at=LAST_MONDAY_1000Z - timedelta(weeks=11)
    )

    body = (await user_client.get("/api/schedule")).json()

    assert "Summer, Over" not in [
        entry["anime"]["title"]["preferred"] for day in body["days"] for entry in day["entries"]
    ]
    assert "Summer, Over" not in [
        entry["anime"]["title"]["preferred"] for entry in body["unscheduled"]
    ]


async def test_an_airing_show_with_no_air_time_anywhere_is_not_carried_in(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """Nothing places it on a weekday, and it is not this season's unscheduled."""
    await add_two_cour(api_factory, title="On Air, Somewhere", next_at=None)

    body = (await user_client.get("/api/schedule")).json()

    assert "On Air, Somewhere" not in [
        entry["anime"]["title"]["preferred"] for entry in body["unscheduled"]
    ]
    assert sum(len(day["entries"]) for day in body["days"]) == 3


async def test_an_airing_show_of_this_season_with_no_air_time_stays_unscheduled(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """The rule that did not change: its own season still lists it (FR-C7)."""
    await add_two_cour(api_factory, title="Cached, Unplaced", season="FALL", next_at=None)

    body = (await user_client.get("/api/schedule")).json()

    unscheduled = sorted(entry["anime"]["title"]["preferred"] for entry in body["unscheduled"])
    assert unscheduled == ["A Film", "Announced Only", "Cached, Unplaced"]
    assert body["unscheduled"][0]["carried_over"] is False


async def test_a_broadcast_a_month_out_is_not_this_week(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """A show on a long break is still ``RELEASING``; it is not on this week."""
    await add_two_cour(api_factory, title="On Hiatus", next_at=NOW + timedelta(days=30))

    body = (await user_client.get("/api/schedule")).json()

    assert "On Hiatus" not in [
        entry["anime"]["title"]["preferred"] for day in body["days"] for entry in day["entries"]
    ]


async def test_only_the_weekly_formats_are_carried_in(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """A film's premiere is a date, not a slot, and this is not its season."""
    await add_two_cour(api_factory, title="A Premiere", format="MOVIE")

    body = (await user_client.get("/api/schedule")).json()

    titles_everywhere = [
        entry["anime"]["title"]["preferred"]
        for day in [*body["days"], {"entries": body["unscheduled"]}]
        for entry in day["entries"]
    ]
    assert "A Premiere" not in titles_everywhere


async def test_another_seasons_view_takes_no_airing_rows_from_elsewhere(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """Prev/next are a catalogue browse: exactly the shows of that season."""
    await add_two_cour(api_factory, title="Spring Only")

    body = (
        await user_client.get("/api/schedule", params={"year": 2026, "season": "SPRING"})
    ).json()

    assert titles(body["days"][FRIDAY]) == ["Spring Only"]
    assert body["days"][FRIDAY]["entries"][0]["carried_over"] is False
    # None of FALL's own airing rows leaked in.
    assert sum(len(day["entries"]) for day in body["days"]) == 1
    assert body["unscheduled"] == []


async def test_an_empty_season_is_seven_empty_days(user_client: AsyncClient) -> None:
    body = (await user_client.get("/api/schedule", params={"year": 1994})).json()

    assert len(body["days"]) == 7
    assert all(day["entries"] == [] for day in body["days"])
    assert body["unscheduled"] == []


# --- The tick on a slot (FR-W5, owner 2026-09-17) -----------------------------
#
# ``ScheduleEntry.watched`` is FR-W5 applied to the episode the slot *names*,
# and only once that episode has aired. Watch Now's appointment cards read it,
# and before this they read the show's latest aired episode instead — so a
# Friday broadcast that has not happened carried "✓ Watched" off last week's.

#: Three hours before :data:`NOW`, which is a Wednesday: a broadcast that has
#: already happened today, on a row nothing has refreshed since.
WEDNESDAY_0900Z = NOW - timedelta(hours=3)

WEDNESDAY = 2


async def aired_today(
    factory: SessionFactory, *, title: str, anilist_id: int, number: int = 5
) -> int:
    """A show whose cached slot points at a broadcast earlier today."""
    anime_id = await add_anime(
        factory,
        title=title,
        anilist_id=anilist_id,
        format="TV",
        episodes=12,
        next_at=WEDNESDAY_0900Z,
        next_episode=number,
    )
    await add_episodes(
        factory,
        anime_id,
        count=number,
        first_at=WEDNESDAY_0900Z - timedelta(weeks=number - 1),
    )
    return anime_id


async def test_an_upcoming_slot_never_carries_a_watched_mark(
    user_client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """The bug, in one test: a list past the named episode, and still no tick.

    Nobody has watched a broadcast that has not happened. The viewer here is at
    episode 12 of a show whose slot names episode 7 on Friday — every rule that
    asks about the *show* says "watched", and the only one that matters asks
    about the episode on the card.
    """
    anime_id = await add_anime(
        api_factory,
        title="Friday Night Show",
        anilist_id=900101,
        format="TV",
        episodes=12,
        next_at=FRIDAY_1400Z,
        next_episode=7,
    )
    await add_episodes(api_factory, anime_id, count=12, first_at=FRIDAY_1400Z - timedelta(weeks=6))
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=12)

    body = (await user_client.get("/api/schedule")).json()

    assert body["days"][FRIDAY]["entries"][0]["next_episode"] == 7
    assert body["days"][FRIDAY]["entries"][0]["watched"] is None


async def test_a_slot_whose_episode_has_aired_carries_the_viewers_mark(
    user_client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """Tonight's broadcast, already watched: this is the tick's one true case."""
    anime_id = await aired_today(api_factory, title="Tonight Watched", anilist_id=900102)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=5)

    body = (await user_client.get("/api/schedule")).json()

    entry = body["days"][WEDNESDAY]["entries"][0]
    assert entry["next_episode"] == 5
    assert entry["watched"] is True


async def test_an_aired_slot_the_viewer_has_not_reached_says_so(
    user_client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """False, not null: the episode aired, and the answer is "not yet"."""
    anime_id = await aired_today(api_factory, title="Tonight Unwatched", anilist_id=900103)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=4)

    entry = (await user_client.get("/api/schedule")).json()["days"][WEDNESDAY]["entries"][0]

    assert entry["watched"] is False


async def test_a_slot_that_names_no_episode_carries_no_mark(
    user_client: AsyncClient, user: User, api_factory: SessionFactory, season: dict[str, int]
) -> None:
    """FR-C6's synthesised slot knows when, not which; there is nothing to tick."""
    await follow(api_factory, user, season["saturday"], ListStatus.WATCHING, progress=13)

    entry = (await user_client.get("/api/schedule")).json()["days"][SATURDAY]["entries"][0]

    assert entry["next_episode"] is None
    assert entry["watched"] is None


async def test_another_users_progress_does_not_tick_this_slot(
    user_client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    anime_id = await aired_today(api_factory, title="Theirs Tonight", anilist_id=900104)
    other = await add_user(api_factory, "sched.other@arc.test", "other-password")
    await follow(api_factory, other, anime_id, ListStatus.WATCHING, progress=5)
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=0)

    entry = (await user_client.get("/api/schedule")).json()["days"][WEDNESDAY]["entries"][0]

    assert entry["watched"] is False


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
