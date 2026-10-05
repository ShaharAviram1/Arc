"""Season arithmetic and weekday placement (FR-C3).

No database and no HTTP: everything under test here is a pure function over
rows, a timezone and a clock, which is the whole reason
:mod:`arc.services.catalog.schedule` exists as a module of its own. The
interesting cases are all about the two things that make a schedule wrong in
ways nobody notices — the year rolling over between FALL and WINTER, and a
weekday that is not the same weekday everywhere.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from arc.models import Anime, ListStatus
from arc.services.catalog.schedule import (
    STALE_NEXT_AIRING,
    DatedEpisode,
    ScheduleRow,
    WeekPlacement,
    adjacent_seasons,
    current_season,
    displayed_dates,
    place_dated,
    place_entries,
    range_bounds,
    user_timezone,
)

#: A Wednesday, so nothing in these tests accidentally passes because "now"
#: happened to be the weekday under test.
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

#: The Friday and Saturday the placement cases are anchored to. July, so the
#: northern-hemisphere zones are all on summer time and the offsets in the
#: assertions are the ones a reader can check in their head: Jerusalem +3,
#: Los Angeles -7, Tokyo +9.
FRIDAY_1400Z = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
SATURDAY_1600Z = datetime(2026, 7, 11, 16, 0, tzinfo=UTC)

MONDAY = 0
FRIDAY = 4
SATURDAY = 5
SUNDAY = 6


def show(
    anime_id: int = 1,
    *,
    title: str = "A Show",
    format: str | None = "TV",
    status: str | None = "RELEASING",
    next_at: datetime | None = None,
    next_episode: int | None = 7,
    estimated: bool = False,
) -> Anime:
    """An ``anime`` row as the cache would hold it, unsaved.

    ``estimated`` writes the flag the MAL fallback puts on a slot it worked out
    from a broadcast time (FR-C6); AniList's own blob carries no such key.
    """
    anime = Anime(
        id=anime_id,
        anilist_id=anime_id,
        title_romaji=title,
        format=format,
        status=status,
    )
    if next_at is not None:
        anime.next_airing = {"episode": next_episode, "airingAt": int(next_at.timestamp())}
        if estimated:
            anime.next_airing["estimated"] = True
    return anime


# --- Season arithmetic -------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 1, 1, tzinfo=UTC), (2026, "WINTER")),
        (datetime(2026, 3, 31, 23, 59, tzinfo=UTC), (2026, "WINTER")),
        (datetime(2026, 4, 1, tzinfo=UTC), (2026, "SPRING")),
        (datetime(2026, 7, 8, tzinfo=UTC), (2026, "SUMMER")),
        (datetime(2026, 10, 1, tzinfo=UTC), (2026, "FALL")),
        (datetime(2026, 12, 31, 23, 59, tzinfo=UTC), (2026, "FALL")),
    ],
)
def test_current_season_is_the_quarter_of_the_date(
    when: datetime, expected: tuple[int, str]
) -> None:
    assert current_season(when) == expected


@pytest.mark.parametrize(
    ("year", "season", "expected"),
    [
        (2026, "FALL", ((2026, "SUMMER"), (2027, "WINTER"))),
        (2027, "WINTER", ((2026, "FALL"), (2027, "SPRING"))),
        (2026, "SPRING", ((2026, "WINTER"), (2026, "SUMMER"))),
        (2026, "SUMMER", ((2026, "SPRING"), (2026, "FALL"))),
    ],
)
def test_adjacent_seasons_roll_the_year_over_at_both_ends(
    year: int, season: str, expected: tuple[tuple[int, str], tuple[int, str]]
) -> None:
    """The prev/next links are the one place a year can silently go missing."""
    assert adjacent_seasons(year, season) == expected


def test_adjacent_seasons_accepts_the_season_in_any_case() -> None:
    assert adjacent_seasons(2026, "fall") == ((2026, "SUMMER"), (2027, "WINTER"))


# --- Timezones ---------------------------------------------------------------


def test_an_unknown_timezone_falls_back_to_utc_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert user_timezone("Mars/Olympus_Mons").key == "UTC"
    assert "unknown user timezone" in caplog.text


@pytest.mark.parametrize("name", ["", None])
def test_no_timezone_at_all_is_utc(name: str | None) -> None:
    assert user_timezone(name).key == "UTC"


# --- Placement ---------------------------------------------------------------


def test_a_releasing_show_lands_on_the_users_weekday_and_local_time() -> None:
    """The same broadcast, two timezones, and the same weekday in both."""
    row = ScheduleRow(anime=show(next_at=FRIDAY_1400Z))

    jerusalem = place_entries([row], tz=ZoneInfo("Asia/Jerusalem"), now=NOW)
    assert [len(day) for day in jerusalem.days] == [0, 0, 0, 0, 1, 0, 0]
    assert jerusalem.days[FRIDAY][0].air_time_local.isoformat() == "17:00:00"

    angeles = place_entries([row], tz=ZoneInfo("America/Los_Angeles"), now=NOW)
    assert [len(day) for day in angeles.days] == [0, 0, 0, 0, 1, 0, 0]
    assert angeles.days[FRIDAY][0].air_time_local.isoformat() == "07:00:00"


def test_a_late_saturday_broadcast_is_a_sunday_in_tokyo() -> None:
    """The weekday crossing, which is why grouping cannot be done in UTC."""
    row = ScheduleRow(anime=show(next_at=SATURDAY_1600Z))

    tokyo = place_entries([row], tz=ZoneInfo("Asia/Tokyo"), now=NOW)

    assert not tokyo.days[SATURDAY]
    assert len(tokyo.days[SUNDAY]) == 1
    assert tokyo.days[SUNDAY][0].air_time_local.isoformat() == "01:00:00"


def test_the_next_episode_is_reported_with_its_instant() -> None:
    row = ScheduleRow(anime=show(next_at=FRIDAY_1400Z, next_episode=7))

    entry = place_entries([row], tz=UTC, now=NOW).days[FRIDAY][0]

    assert entry.next_episode == 7
    assert entry.next_at == FRIDAY_1400Z


def test_a_mal_slot_places_the_show_without_naming_an_episode() -> None:
    """MAL knows when the next episode airs and not which one (FR-C6)."""
    row = ScheduleRow(anime=show(next_at=FRIDAY_1400Z, next_episode=None))

    entry = place_entries([row], tz=UTC, now=NOW).days[FRIDAY][0]

    assert entry.next_episode is None
    assert entry.next_at == FRIDAY_1400Z


def test_a_synthesised_slot_is_reported_as_an_estimate() -> None:
    """The client badges it: the weekday is a guess, not a published time."""
    row = ScheduleRow(anime=show(next_at=FRIDAY_1400Z, next_episode=None, estimated=True))

    entry = place_entries([row], tz=UTC, now=NOW).days[FRIDAY][0]

    assert entry.next_at == FRIDAY_1400Z
    assert entry.next_at_estimated is True


def test_a_published_slot_is_not_an_estimate() -> None:
    row = ScheduleRow(anime=show(next_at=FRIDAY_1400Z))

    entry = place_entries([row], tz=UTC, now=NOW).days[FRIDAY][0]

    assert entry.next_at_estimated is False


def test_an_entry_with_no_next_at_is_never_flagged_as_an_estimate() -> None:
    """A stale blob is dropped whole, and its flag goes with it."""
    stale = NOW - STALE_NEXT_AIRING - timedelta(days=1)
    dropped = ScheduleRow(
        anime=show(1, next_at=stale, estimated=True),
        latest_air_at=FRIDAY_1400Z - timedelta(weeks=2),
    )
    finished = ScheduleRow(
        anime=show(2, status="FINISHED", next_at=None),
        latest_air_at=FRIDAY_1400Z - timedelta(weeks=4),
    )

    placement = place_entries([dropped, finished], tz=UTC, now=NOW)

    assert [entry.next_at for entry in placement.days[FRIDAY]] == [None, None]
    assert [entry.next_at_estimated for entry in placement.days[FRIDAY]] == [False, False]


def test_a_finished_show_keeps_the_weekday_of_its_last_episode() -> None:
    row = ScheduleRow(
        anime=show(status="FINISHED", next_at=None),
        latest_air_at=FRIDAY_1400Z - timedelta(weeks=4),
    )

    placement = place_entries([row], tz=ZoneInfo("Asia/Jerusalem"), now=NOW)

    assert [len(day) for day in placement.days] == [0, 0, 0, 0, 1, 0, 0]
    entry = placement.days[FRIDAY][0]
    assert entry.air_time_local.isoformat() == "17:00:00"
    # Nothing is upcoming: the show is over.
    assert entry.next_episode is None
    assert entry.next_at is None


def test_a_stale_next_airing_gives_way_to_the_last_real_air_time() -> None:
    """A row nobody has refreshed for a fortnight is not describing the future."""
    stale = NOW - STALE_NEXT_AIRING - timedelta(days=1)
    row = ScheduleRow(
        anime=show(next_at=stale),  # a Tuesday
        latest_air_at=FRIDAY_1400Z - timedelta(weeks=2),
    )

    placement = place_entries([row], tz=UTC, now=NOW)

    assert [len(day) for day in placement.days] == [0, 0, 0, 0, 1, 0, 0]
    assert placement.days[FRIDAY][0].next_at is None


@pytest.mark.parametrize("format", ["MOVIE", "OVA", "SPECIAL", "MUSIC", None])
def test_only_the_weekly_formats_get_a_weekday(format: str | None) -> None:
    """A film has a release date, not a slot on Fridays."""
    row = ScheduleRow(anime=show(format=format, next_at=FRIDAY_1400Z))

    placement = place_entries([row], tz=UTC, now=NOW)

    assert all(not day for day in placement.days)
    assert len(placement.unscheduled) == 1
    entry = placement.unscheduled[0]
    assert entry.air_time_local is None
    # The release date itself is still reported; it is the weekday that is a lie.
    assert entry.next_at == FRIDAY_1400Z


@pytest.mark.parametrize("format", ["TV", "TV_SHORT", "ONA"])
def test_the_weekly_formats_are_placed(format: str) -> None:
    row = ScheduleRow(anime=show(format=format, next_at=FRIDAY_1400Z))

    assert len(place_entries([row], tz=UTC, now=NOW).days[FRIDAY]) == 1


def test_a_row_with_no_air_time_at_all_is_unscheduled() -> None:
    row = ScheduleRow(anime=show(status="NOT_YET_RELEASED", next_at=None))

    placement = place_entries([row], tz=UTC, now=NOW)

    assert all(not day for day in placement.days)
    assert [entry.air_time_local for entry in placement.unscheduled] == [None]


def test_a_days_entries_are_sorted_by_local_time() -> None:
    rows = [
        ScheduleRow(anime=show(1, title="Late", next_at=FRIDAY_1400Z.replace(hour=20))),
        ScheduleRow(anime=show(2, title="Early", next_at=FRIDAY_1400Z.replace(hour=6))),
        ScheduleRow(anime=show(3, title="Middle", next_at=FRIDAY_1400Z)),
    ]

    placement = place_entries(rows, tz=UTC, now=NOW)

    assert [entry.anime.title_romaji for entry in placement.days[FRIDAY]] == [
        "Early",
        "Middle",
        "Late",
    ]


def test_two_shows_in_one_slot_are_ordered_by_title() -> None:
    rows = [
        ScheduleRow(anime=show(1, title="Zebra", next_at=FRIDAY_1400Z)),
        ScheduleRow(anime=show(2, title="apple", next_at=FRIDAY_1400Z)),
    ]

    placement = place_entries(rows, tz=UTC, now=NOW)

    assert [entry.anime.title_romaji for entry in placement.days[FRIDAY]] == ["apple", "Zebra"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ListStatus.WATCHING, True),
        (ListStatus.PLANNED, True),
        (ListStatus.ON_HOLD, True),
        (ListStatus.DROPPED, False),
        (ListStatus.COMPLETED, False),
        (None, False),
    ],
)
def test_following_is_the_three_states_that_mean_i_am_watching_for_this(
    status: ListStatus | None, expected: bool
) -> None:
    row = ScheduleRow(anime=show(next_at=FRIDAY_1400Z), list_status=status)

    entry = place_entries([row], tz=UTC, now=NOW).days[FRIDAY][0]

    assert entry.following is expected
    assert entry.list_status == status


def test_placement_covers_the_whole_week_even_when_empty() -> None:
    """The client renders seven columns whatever the season holds."""
    placement = place_entries([], tz=UTC, now=NOW)

    assert len(placement.days) == 7
    assert all(day == [] for day in placement.days)
    assert placement.unscheduled == []
    assert placement.days[MONDAY] == []


# --- The dated current view (owner, 2026-10-04) ------------------------------
#
# Under :data:`NOW` (Wednesday 8 July) the current view is Monday 6 July to
# Tuesday 14 July: this week so far, then a week of upcoming dates. Each column
# is that one local date and holds only shows with an episode airing on it.

WEEK_OF_NOW = displayed_dates(now=NOW, tz=UTC)


def dated(
    rows: list[ScheduleRow], *, tz: ZoneInfo | None = None, now: datetime = NOW
) -> WeekPlacement:
    zone = tz if tz is not None else ZoneInfo("UTC")
    return place_dated(rows, tz=zone, now=now, dates=displayed_dates(now=now, tz=zone))


def on(placement: WeekPlacement) -> list[tuple[date, int]]:
    """``(date, anime id)`` for every dated entry."""
    return [
        (day, entry.anime.id)
        for day, entries in zip(placement.dates, placement.days, strict=True)
        for entry in entries
    ]


def test_the_current_view_runs_from_monday_to_a_week_ahead() -> None:
    assert WEEK_OF_NOW == tuple(date(2026, 7, 6) + timedelta(days=n) for n in range(9))


@pytest.mark.parametrize(
    ("when", "last"),
    [
        (datetime(2026, 7, 6, 0, 30, tzinfo=UTC), date(2026, 7, 12)),  # Monday
        (datetime(2026, 7, 10, 12, 0, tzinfo=UTC), date(2026, 7, 16)),  # Friday
        (datetime(2026, 7, 11, 12, 0, tzinfo=UTC), date(2026, 7, 17)),  # Saturday
        (datetime(2026, 7, 12, 12, 0, tzinfo=UTC), date(2026, 7, 18)),  # Sunday
    ],
)
def test_the_range_always_reaches_six_days_past_today(when: datetime, last: date) -> None:
    dates = displayed_dates(now=when, tz=UTC)

    assert dates[0] == date(2026, 7, 6)
    assert dates[-1] == last


def test_the_range_is_read_in_the_users_zone() -> None:
    """23:00Z Sunday is already Monday in Tokyo: a new week starts there."""
    when = datetime(2026, 7, 12, 23, 0, tzinfo=UTC)

    assert displayed_dates(now=when, tz=UTC)[-1] == date(2026, 7, 18)
    tokyo = displayed_dates(now=when, tz=ZoneInfo("Asia/Tokyo"))
    assert (tokyo[0], tokyo[-1]) == (date(2026, 7, 13), date(2026, 7, 19))


def test_the_bounds_are_local_midnights() -> None:
    start, end = range_bounds(WEEK_OF_NOW, tz=ZoneInfo("Asia/Tokyo"))

    assert start == datetime(2026, 7, 5, 15, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 14, 15, 0, tzinfo=UTC)


def test_a_finished_show_with_nothing_in_range_is_ended_not_on_a_date() -> None:
    row = ScheduleRow(
        anime=show(status="FINISHED", next_at=None),
        latest_air_at=FRIDAY_1400Z - timedelta(weeks=4),
    )

    placement = dated([row])

    assert on(placement) == []
    assert placement.unscheduled == []
    assert [entry.anime.id for entry in placement.ended] == [1]


def test_a_finale_that_aired_on_a_displayed_date_is_on_it() -> None:
    finale = NOW - timedelta(days=2)
    row = ScheduleRow(
        anime=show(status="FINISHED", next_at=None),
        latest_air_at=finale,
        dated_episodes=(DatedEpisode(12, finale),),
    )

    placement = dated([row])

    assert on(placement) == [(date(2026, 7, 6), 1)]
    assert placement.days[MONDAY][0].next_episode == 12
    assert placement.ended == []


def test_a_finished_show_is_never_placed_by_its_slot_or_an_inference() -> None:
    """A leftover ``next_airing`` on a finished row is not a broadcast."""
    row = ScheduleRow(anime=show(status="FINISHED", next_at=NOW + timedelta(days=2)))

    placement = dated([row])

    assert on(placement) == []
    assert [entry.anime.id for entry in placement.ended] == [1]


def test_a_carried_in_finished_show_with_nothing_in_range_is_dropped() -> None:
    row = ScheduleRow(anime=show(status="FINISHED", next_at=None), carried_over=True)

    placement = dated([row])

    assert (on(placement), placement.unscheduled, placement.ended) == ([], [], [])


def test_a_premiere_weeks_away_is_unscheduled_with_its_date() -> None:
    row = ScheduleRow(
        anime=show(status="NOT_YET_RELEASED", next_at=NOW + timedelta(days=10), next_episode=1)
    )

    placement = dated([row])

    assert on(placement) == []
    assert [entry.starts_on for entry in placement.unscheduled] == [date(2026, 7, 18)]


def test_a_premiere_with_no_slot_takes_its_date_from_the_first_episode() -> None:
    row = ScheduleRow(
        anime=show(status="NOT_YET_RELEASED", next_at=None),
        first_air_at=datetime(2026, 7, 31, 23, 30, tzinfo=UTC),
    )

    placement = dated([row], tz=ZoneInfo("Asia/Tokyo"))

    # 23:30Z on the 31st is the 1st of August in Tokyo.
    assert [entry.starts_on for entry in placement.unscheduled] == [date(2026, 8, 1)]


def test_a_premiere_on_a_displayed_date_is_on_that_date_only() -> None:
    row = ScheduleRow(anime=show(status="NOT_YET_RELEASED", next_at=FRIDAY_1400Z, next_episode=1))

    placement = dated([row])

    assert on(placement) == [(date(2026, 7, 10), 1)]
    assert placement.unscheduled == []


def test_an_aired_episode_keeps_its_date_after_the_slot_moves_on() -> None:
    aired = NOW - timedelta(days=2)
    row = ScheduleRow(
        anime=show(next_at=NOW + timedelta(days=5), next_episode=5),
        latest_air_at=aired,
        dated_episodes=(DatedEpisode(4, aired),),
    )

    placement = dated([row])

    assert on(placement) == [(date(2026, 7, 6), 1), (date(2026, 7, 13), 1)]
    entry = placement.days[MONDAY][0]
    assert (entry.next_episode, entry.next_at, entry.next_at_estimated) == (4, aired, False)
    assert placement.days[7][0].next_episode == 5


def test_an_episode_row_at_or_past_the_slot_gives_way_to_the_slot() -> None:
    """The slot moved from Thursday to Friday; the Thursday row is stale."""
    row = ScheduleRow(
        anime=show(next_at=FRIDAY_1400Z, next_episode=5),
        latest_air_at=FRIDAY_1400Z - timedelta(days=1),
        dated_episodes=(
            DatedEpisode(5, FRIDAY_1400Z - timedelta(days=1)),
            DatedEpisode(6, FRIDAY_1400Z + timedelta(days=1)),
        ),
    )

    assert on(dated([row])) == [(date(2026, 7, 10), 1)]


def test_two_episodes_on_one_date_are_both_named() -> None:
    first = NOW - timedelta(days=1)
    row = ScheduleRow(
        anime=show(next_at=None),
        latest_air_at=first,
        dated_episodes=(DatedEpisode(4, first), DatedEpisode(3, first - timedelta(minutes=30))),
    )

    entry = dated([row]).days[1][0]

    assert (entry.next_episode, entry.last_episode) == (3, 4)


def test_an_estimated_episode_date_stays_estimated() -> None:
    aired = NOW - timedelta(days=1)
    row = ScheduleRow(
        anime=show(next_at=None),
        latest_air_at=aired,
        dated_episodes=(DatedEpisode(3, aired, estimated=True),),
    )

    assert dated([row]).days[1][0].next_at_estimated is True


def test_a_slot_with_no_dated_rows_infers_last_weeks_broadcast() -> None:
    row = ScheduleRow(anime=show(next_at=NOW + timedelta(days=5), next_episode=5))

    placement = dated([row])

    assert on(placement) == [(date(2026, 7, 6), 1), (date(2026, 7, 13), 1)]
    assert placement.days[MONDAY][0].next_episode == 4


def test_nothing_is_inferred_into_the_future() -> None:
    """Sun 4 Oct, slot Tue 13 Oct: a week before it is Tue 6 Oct, still ahead."""
    sunday = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    row = ScheduleRow(anime=show(next_at=datetime(2026, 10, 13, 15, 0, tzinfo=UTC), next_episode=9))

    placement = dated([row], now=sunday)

    assert on(placement) == []
    assert [entry.anime.id for entry in placement.unscheduled] == [1]


def test_nothing_is_inferred_for_a_show_with_dated_episodes() -> None:
    """Its rows have said when it aired; a gap in them is a break."""
    row = ScheduleRow(
        anime=show(next_at=NOW + timedelta(days=5), next_episode=5),
        latest_air_at=NOW - timedelta(weeks=3),
    )

    assert on(dated([row])) == [(date(2026, 7, 13), 1)]


def test_a_numberless_slot_is_inferred_only_for_a_show_known_to_have_started() -> None:
    """Wed 7 Oct, MAL slot Mon 12 Oct, no episode rows: Mon 5 Oct aired —
    provided the season tag says the show had started by then."""
    wednesday = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    slot = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
    started = show(1, next_at=slot, next_episode=None, estimated=True)
    started.season, started.season_year = "SUMMER", 2026
    unknown = show(2, next_at=slot, next_episode=None, estimated=True)
    this_season = show(3, next_at=slot, next_episode=None, estimated=True)
    this_season.season, this_season.season_year = "FALL", 2026

    placement = dated(
        [ScheduleRow(anime=row, carried_over=True) for row in (started, unknown, this_season)],
        now=wednesday,
    )

    assert sorted(on(placement)) == [
        (date(2026, 10, 5), 1),
        (date(2026, 10, 12), 1),
        (date(2026, 10, 12), 2),
        (date(2026, 10, 12), 3),
    ]
    inferred = placement.days[0][0]
    assert (inferred.next_episode, inferred.next_at_estimated) == (None, True)


def test_on_a_sunday_tomorrow_is_next_monday() -> None:
    sunday = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    next_monday = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    this_monday = next_monday - timedelta(weeks=1)
    weekly = ScheduleRow(
        anime=show(1, next_at=next_monday, next_episode=8),
        latest_air_at=this_monday,
        dated_episodes=(DatedEpisode(7, this_monday),),
    )
    # Aired this Monday and has nothing after it.
    once = ScheduleRow(
        anime=show(2, next_at=None),
        latest_air_at=this_monday,
        dated_episodes=(DatedEpisode(1, this_monday),),
    )

    placement = dated([weekly, once], now=sunday)

    assert len(placement.dates) == 13
    assert sorted(on(placement)) == [
        (date(2026, 7, 6), 1),
        (date(2026, 7, 6), 2),
        (date(2026, 7, 13), 1),
    ]
    assert placement.days[7][0].next_episode == 8


def test_a_dated_column_is_the_users_date_across_midnight() -> None:
    """02:00Z Wednesday is still Tuesday evening in Los Angeles."""
    aired = datetime(2026, 7, 8, 2, 0, tzinfo=UTC)
    row = ScheduleRow(
        anime=show(next_at=None),
        latest_air_at=aired,
        dated_episodes=(DatedEpisode(3, aired),),
    )

    placement = dated([row], tz=ZoneInfo("America/Los_Angeles"))

    assert on(placement) == [(date(2026, 7, 7), 1)]
    assert placement.days[1][0].air_time_local is not None
    assert placement.days[1][0].air_time_local.isoformat() == "19:00:00"


def test_a_carried_in_show_with_nothing_in_range_is_dropped() -> None:
    row = ScheduleRow(
        anime=show(next_at=NOW + timedelta(days=12), next_episode=3),
        latest_air_at=NOW - timedelta(weeks=1),
        carried_over=True,
    )

    placement = dated([row])

    assert on(placement) == []
    assert placement.unscheduled == []


def test_a_season_show_with_nothing_in_range_is_unscheduled() -> None:
    row = ScheduleRow(anime=show(next_at=None))

    placement = dated([row])

    assert on(placement) == []
    assert [(entry.anime.id, entry.starts_on) for entry in placement.unscheduled] == [(1, None)]


@pytest.mark.parametrize("format", ["MOVIE", "OVA", "SPECIAL"])
def test_films_and_ovas_are_unscheduled_as_before(format: str) -> None:
    row = ScheduleRow(anime=show(format=format, status="FINISHED", next_at=FRIDAY_1400Z))

    placement = dated([row])

    assert on(placement) == []
    assert [entry.anime.id for entry in placement.unscheduled] == [1]
    assert placement.unscheduled[0].starts_on is None
    assert placement.ended == []
