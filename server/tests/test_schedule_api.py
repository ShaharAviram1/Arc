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
    # Monday of this week to a week of upcoming dates (owner, 2026-10-04).
    assert [day["weekday"] for day in body["days"]] == [0, 1, 2, 3, 4, 5, 6, 0, 1]


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
    # The finished show is on no date of the current view (owner, 2026-10-04):
    # it used to sit on the Monday it last aired, weeks ago.
    assert titles(body["days"][MONDAY]) == []
    assert titles(body["days"][FRIDAY]) == ["Friday Night Show"]
    assert titles(body["days"][SATURDAY]) == ["Saturday Late Show"]
    # The film and the show with no dates at all.
    assert sorted(entry["anime"]["title"]["preferred"] for entry in body["unscheduled"]) == [
        "A Film",
        "Announced Only",
    ]
    placed = sum(len(day["entries"]) for day in body["days"])
    assert placed == 2


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

    # The finished show is on no date of the current view; how a browse still
    # places one on its old weekday is tested below.
    assert body["days"][MONDAY]["entries"] == []


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

    # Monday Rerun, finished, is no longer beside it (owner, 2026-10-04).
    assert titles(body["days"][MONDAY]) == ["Undated Slot Show"]
    assert body["days"][MONDAY]["entries"][0]["carried_over"] is True


async def test_a_finished_show_from_the_previous_season_is_only_on_its_finale(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """Its finale aired two days ago, on a displayed date: it was on then, and
    on no other day (owner, 2026-10-04)."""
    anime_id = await add_two_cour(
        api_factory, title="Summer, Over", status="FINISHED", season="SUMMER", next_at=None
    )
    await add_episodes(
        api_factory, anime_id, count=12, first_at=LAST_MONDAY_1000Z - timedelta(weeks=11)
    )

    body = (await user_client.get("/api/schedule")).json()

    placed = [
        (day["date"], entry["anime"]["title"]["preferred"], entry["next_episode"])
        for day in body["days"]
        for entry in day["entries"]
        if entry["anime"]["title"]["preferred"] == "Summer, Over"
    ]
    assert placed == [("2026-11-02", "Summer, Over", 12)]
    assert "Summer, Over" not in [
        entry["anime"]["title"]["preferred"] for entry in [*body["unscheduled"], *body["ended"]]
    ]


async def test_a_finished_show_from_the_previous_season_with_no_date_here_is_absent(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    anime_id = await add_two_cour(
        api_factory, title="Summer, Long Over", status="FINISHED", season="SUMMER", next_at=None
    )
    await add_episodes(
        api_factory, anime_id, count=12, first_at=LAST_MONDAY_1000Z - timedelta(weeks=14)
    )

    body = (await user_client.get("/api/schedule")).json()

    assert "Summer, Long Over" not in [
        entry["anime"]["title"]["preferred"]
        for entries in [
            *(day["entries"] for day in body["days"]),
            body["unscheduled"],
            body["ended"],
        ]
        for entry in entries
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
    # Friday's and Saturday's shows; the finished Monday Rerun is on no date.
    assert sum(len(day["entries"]) for day in body["days"]) == 2


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


# --- The dated current view (owner, 2026-10-04) ------------------------------
#
# "The schedule should only show what's really playing that date." Each day of
# the current view is one local date and lists exactly the shows with an
# episode airing on it. Under :data:`NOW` (Wednesday 4 Nov) that is Monday 2 to
# Tuesday 10 Nov: this week so far, then a week of upcoming dates.

#: The Sunday of :data:`NOW`'s week, at noon UTC.
SUNDAY_NOON = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def everywhere(body: dict[str, object]) -> list[tuple[str, str]]:
    """``(date, title)`` for every dated entry of a current-view page."""
    days = body["days"]
    assert isinstance(days, list)
    return [(day["date"], title) for day in days for title in titles(day)]


def listed(body: dict[str, object], key: str) -> list[str]:
    entries = body[key]
    assert isinstance(entries, list)
    return [entry["anime"]["title"]["preferred"] for entry in entries]


def dates_of(body: dict[str, object], title: str) -> list[str]:
    return [day for day, shown in everywhere(body) if shown == title]


async def test_the_current_view_runs_from_monday_to_a_week_ahead(
    user_client: AsyncClient, season: dict[str, int]
) -> None:
    body = (await user_client.get("/api/schedule")).json()

    assert [day["date"] for day in body["days"]] == [
        "2026-11-02",
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
        "2026-11-06",
        "2026-11-07",
        "2026-11-08",
        "2026-11-09",
        "2026-11-10",
    ]


async def test_a_finished_show_of_this_season_is_on_no_day_but_still_listed(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """Off the calendar, still a show of Fall 2026 for Search and Home."""
    anime_id = await add_anime(
        api_factory, title="Ended Show", anilist_id=900301, status="FINISHED"
    )
    await add_episodes(
        api_factory, anime_id, count=12, first_at=LAST_MONDAY_1000Z - timedelta(weeks=14)
    )

    body = (await user_client.get("/api/schedule")).json()

    shown = [title for _, title in everywhere(body)]
    assert "Ended Show" not in shown
    assert "Monday Rerun" not in shown
    assert "Ended Show" not in listed(body, "unscheduled")
    assert listed(body, "ended") == ["Ended Show", "Monday Rerun"]


async def test_a_finale_this_week_is_on_its_date(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    anime_id = await add_anime(
        api_factory, title="Finale Monday", anilist_id=900312, status="FINISHED"
    )
    await add_episodes(
        api_factory, anime_id, count=12, first_at=LAST_MONDAY_1000Z - timedelta(weeks=11)
    )

    body = (await user_client.get("/api/schedule")).json()

    assert dates_of(body, "Finale Monday") == ["2026-11-02"]
    assert "Finale Monday" not in listed(body, "ended")


async def test_a_premiere_ten_days_out_is_listed_with_its_date_not_on_a_day(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    await add_anime(
        api_factory,
        title="Starts Later",
        anilist_id=900302,
        status="NOT_YET_RELEASED",
        next_at=NOW + timedelta(days=10),
        next_episode=1,
    )

    body = (await user_client.get("/api/schedule")).json()

    assert dates_of(body, "Starts Later") == []
    entry = next(
        row for row in body["unscheduled"] if row["anime"]["title"]["preferred"] == "Starts Later"
    )
    assert entry["starts_on"] == "2026-11-14"
    # A film's date is a release, not a premiere of a run; it says nothing new.
    film = next(row for row in body["unscheduled"] if row["anime"]["id"] == season["film"])
    assert film["starts_on"] is None


async def test_a_premiere_with_only_an_episode_row_still_names_its_date(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    anime_id = await add_anime(
        api_factory, title="Dated Premiere", anilist_id=900303, status="NOT_YET_RELEASED"
    )
    await add_episodes(api_factory, anime_id, count=3, first_at=NOW + timedelta(days=20))

    body = (await user_client.get("/api/schedule")).json()

    entry = next(
        row for row in body["unscheduled"] if row["anime"]["title"]["preferred"] == "Dated Premiere"
    )
    assert entry["starts_on"] == "2026-11-24"


async def test_a_premiere_on_a_displayed_date_is_on_that_date_only(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    await add_anime(
        api_factory,
        title="Premieres Friday",
        anilist_id=900304,
        status="NOT_YET_RELEASED",
        next_at=FRIDAY_1400Z,
        next_episode=1,
    )

    body = (await user_client.get("/api/schedule")).json()

    assert dates_of(body, "Premieres Friday") == ["2026-11-06"]
    assert "Premieres Friday" not in listed(body, "unscheduled")


async def test_next_seasons_premiere_in_the_turnover_week_is_on_its_date(
    user_client: AsyncClient, api_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tuesday 29 Sep is still SUMMER by the UTC quarter; a Fall premiere on
    Thursday 1 Oct is on Thursday all the same."""
    monkeypatch.setattr("arc.api.schedule.now", lambda: datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    await add_anime(
        api_factory,
        title="Fall Premiere",
        anilist_id=900313,
        status="NOT_YET_RELEASED",
        season="FALL",
        next_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
        next_episode=1,
    )
    dated_only = await add_anime(
        api_factory, title="Fall Dated Premiere", anilist_id=900314, status="NOT_YET_RELEASED"
    )
    await add_episodes(
        api_factory, dated_only, count=2, first_at=datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
    )

    body = (await user_client.get("/api/schedule")).json()

    assert body["season"] == "SUMMER"
    assert dates_of(body, "Fall Premiere") == ["2026-10-01"]
    assert dates_of(body, "Fall Dated Premiere") == ["2026-10-02"]


async def test_a_show_that_aired_two_days_ago_is_on_that_date_and_its_next(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    """Monday's episode aired and ``next_airing`` rolled to next Monday."""
    anime_id = await add_anime(
        api_factory,
        title="Monday Weekly",
        anilist_id=900305,
        next_at=NOW + timedelta(days=5),
        next_episode=5,
    )
    await add_episodes(api_factory, anime_id, count=4, first_at=NOW - timedelta(days=2, weeks=3))

    body = (await user_client.get("/api/schedule")).json()

    assert dates_of(body, "Monday Weekly") == ["2026-11-02", "2026-11-09"]
    entry = body["days"][MONDAY]["entries"][0]
    # The slot names the episode that aired that day, not the next one.
    assert entry["next_episode"] == 4
    assert entry["next_at"] == "2026-11-02T12:00:00Z"
    assert entry["air_time_local"] == "12:00"
    assert body["days"][7]["entries"][0]["next_episode"] == 5


async def test_on_a_sunday_the_next_days_are_next_weeks(
    user_client: AsyncClient,
    season: dict[str, int],
    api_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sunday's "tomorrow" is next Monday, with next Monday's real airings."""
    monkeypatch.setattr("arc.api.schedule.now", lambda: SUNDAY_NOON)
    dated = await add_anime(
        api_factory,
        title="Monday Dated",
        anilist_id=900306,
        next_at=datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        next_episode=5,
    )
    await add_episodes(
        api_factory,
        dated,
        count=4,
        first_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC) - timedelta(weeks=3),
    )
    # Its last episode aired this Monday and nothing follows: not next Monday.
    ended_run = await add_anime(api_factory, title="This Monday Only", anilist_id=900307)
    await add_episodes(
        api_factory, ended_run, count=1, first_at=datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    )
    # A Tuesday show with no episode rows at all: next Tuesday from the slot,
    # this Tuesday inferred one week before it.
    await add_anime(
        api_factory,
        title="Tuesday Slot",
        anilist_id=900308,
        next_at=datetime(2026, 11, 10, 15, 0, tzinfo=UTC),
        next_episode=9,
    )

    body = (await user_client.get("/api/schedule")).json()

    assert body["days"][0]["date"] == "2026-11-02"
    assert body["days"][-1]["date"] == "2026-11-14"
    assert [day["weekday"] for day in body["days"]][6:9] == [6, 0, 1]
    assert dates_of(body, "Monday Dated") == ["2026-11-02", "2026-11-09"]
    assert dates_of(body, "This Monday Only") == ["2026-11-02"]
    assert dates_of(body, "Tuesday Slot") == ["2026-11-03", "2026-11-10"]
    next_monday = body["days"][7]["entries"]
    assert [(e["anime"]["title"]["preferred"], e["next_episode"]) for e in next_monday] == [
        ("Monday Dated", 5)
    ]
    inferred = body["days"][1]["entries"][0]
    assert (inferred["next_episode"], inferred["air_time_local"]) == (8, "15:00")


async def test_a_carried_in_two_cour_show_is_on_its_date(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    await add_two_cour(api_factory)

    body = (await user_client.get("/api/schedule")).json()

    assert dates_of(body, "Second Cour") == ["2026-11-06"]


async def test_dates_are_the_users_across_midnight(
    user_client: AsyncClient, user: User, api_factory: SessionFactory, season: dict[str, int]
) -> None:
    """20:00Z Monday is 05:00 Tuesday in Tokyo; 16:00Z Saturday is Sunday."""
    anime_id = await add_anime(
        api_factory,
        title="Late Monday",
        anilist_id=900309,
        next_at=datetime(2026, 11, 9, 20, 0, tzinfo=UTC),
        next_episode=5,
    )
    await add_episodes(
        api_factory, anime_id, count=4, first_at=datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
    )
    await set_timezone(api_factory, user, "Asia/Tokyo")

    body = (await user_client.get("/api/schedule")).json()

    assert dates_of(body, "Late Monday") == ["2026-11-03", "2026-11-10"]
    assert dates_of(body, "Saturday Late Show") == ["2026-11-08"]
    assert body["days"][1]["entries"][0]["air_time_local"] == "05:00"


async def test_two_episodes_on_one_date_are_both_named(
    user_client: AsyncClient, season: dict[str, int], api_factory: SessionFactory
) -> None:
    anime_id = await add_anime(api_factory, title="Double Bill", anilist_id=900315)
    await add_episodes(
        api_factory,
        anime_id,
        count=4,
        first_at=LAST_MONDAY_1000Z - timedelta(hours=3),
        step=timedelta(hours=1),
    )

    body = (await user_client.get("/api/schedule")).json()

    entry = body["days"][MONDAY]["entries"][0]
    assert (entry["next_episode"], entry["last_episode"]) == (1, 4)


async def test_an_aired_day_carries_the_tick_for_its_own_episode(
    user_client: AsyncClient, user: User, api_factory: SessionFactory
) -> None:
    """Monday's slot names episode 4, watched; next Monday's is not asked."""
    anime_id = await add_anime(
        api_factory,
        title="Monday Weekly",
        anilist_id=900310,
        next_at=NOW + timedelta(days=5),
        next_episode=5,
    )
    await add_episodes(api_factory, anime_id, count=4, first_at=NOW - timedelta(days=2, weeks=3))
    await follow(api_factory, user, anime_id, ListStatus.WATCHING, progress=4)

    body = (await user_client.get("/api/schedule")).json()

    assert body["days"][MONDAY]["entries"][0]["watched"] is True
    assert body["days"][7]["entries"][0]["watched"] is None


async def test_a_browsed_season_is_still_seven_undated_weekdays(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Prev/next are unchanged: finished shows on the weekday they aired."""
    anime_id = await add_anime(
        api_factory,
        title="Summer Finished",
        anilist_id=900311,
        status="FINISHED",
        season="SUMMER",
    )
    await add_episodes(api_factory, anime_id, count=6, first_at=MONDAY_1000Z - timedelta(weeks=10))

    body = (
        await user_client.get("/api/schedule", params={"year": 2026, "season": "SUMMER"})
    ).json()

    assert [day["date"] for day in body["days"]] == [None] * 7
    assert titles(body["days"][MONDAY]) == ["Summer Finished"]
    entry = body["days"][MONDAY]["entries"][0]
    assert (entry["air_time_local"], entry["next_at"], entry["starts_on"]) == ("10:00", None, None)
    assert body["ended"] == []


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
