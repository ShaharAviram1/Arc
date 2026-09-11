"""The MyAnimeList catalogue source: parsing, mapping, synthesised air times.

No database and no network — a real :class:`MalSource` over
``httpx.MockTransport``, so everything except the socket is the production
path. See ``tests/mal_mock.py`` for where the fixture JSON comes from.

The interesting half is :func:`synthesise_airing`. MAL publishes a weekly
broadcast slot rather than per-episode times, and every air date the client
badges "estimated" is worked out here (FR-C6), so it is asserted against real
values: Frieren premiered on Friday 2023-09-29 at 23:00 JST, which is
14:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import httpx
import pytest

from arc.services.catalog.source import SourceNotFound, SourceUnavailable
from arc.services.mal import parse_anime, parse_broadcast, parse_date, synthesise_airing
from arc.services.mal.catalog import JST, MalSource, next_broadcast, synthesise_next_airing
from arc.services.mal.catalog import parse_anime as parse
from tests.mal_mock import (
    CLIENT_ID_HEADER,
    FRIEREN_MAL_ID,
    SEASON_NAME,
    SEASON_YEAR,
    FakeMal,
    anime_payload,
    frieren_fake,
    load,
)

#: Frieren's premiere, in both timezones. 23:00 in Tokyo is 14:00 UTC.
FIRST_EPISODE_UTC = datetime(2023, 9, 29, 14, 0, tzinfo=UTC)


# --- Small parsers ----------------------------------------------------------


def test_a_full_start_date_parses() -> None:
    assert parse_date("2023-09-29") == date(2023, 9, 29)


@pytest.mark.parametrize("value", ["2023", "2023-09", "", None, "not a date"])
def test_a_partial_start_date_is_no_date(value: str | None) -> None:
    """MAL says "I only know the year" by sending a shorter string.

    Guessing a day from it would be wrong for every episode of the show, since
    each synthesised air time is an offset from this one.
    """
    assert parse_date(value) is None


def test_broadcast_parses_to_a_weekday_and_a_local_time() -> None:
    assert parse_broadcast({"day_of_the_week": "friday", "start_time": "23:00"}) == (
        4,
        time(23, 0),
    )
    assert parse_broadcast({"day_of_the_week": "monday", "start_time": "00:30"}) == (
        0,
        time(0, 30),
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"day_of_the_week": "friday"},  # a slot that moves: no time given
        {"start_time": "23:00"},
        {"day_of_the_week": "someday", "start_time": "23:00"},
        {"day_of_the_week": "friday", "start_time": "half past"},
    ],
)
def test_an_incomplete_broadcast_is_no_broadcast(raw: dict[str, str] | None) -> None:
    assert parse_broadcast(raw) is None


# --- Synthesised air times --------------------------------------------------


def test_airing_is_synthesised_weekly_from_the_broadcast_slot() -> None:
    """FR-C6: episode 1 on the start date at the broadcast time, then weekly."""
    airing = synthesise_airing(
        start_date=date(2023, 9, 29), broadcast=(4, time(23, 0)), episodes=28
    )

    assert len(airing) == 28
    assert [entry.episode for entry in airing] == list(range(1, 29))
    assert airing[0].at == FIRST_EPISODE_UTC
    assert airing[1].at == datetime(2023, 10, 6, 14, 0, tzinfo=UTC)
    assert airing[-1].at == datetime(2024, 4, 5, 14, 0, tzinfo=UTC)
    # Every one of them is a guess, and says so.
    assert all(entry.estimated for entry in airing)


def test_a_missing_broadcast_falls_back_to_the_late_night_slot() -> None:
    """A date with no time is still a date, and the day is the useful half.

    Which is why the fallback is 23:00 JST rather than midnight: midnight in
    Tokyo is 15:00 UTC on the *previous* day, so defaulting to it would move
    every episode of the show back a calendar day for everyone west of Japan —
    including the premiere, whose date is the one thing MAL did tell us.
    """
    airing = synthesise_airing(start_date=date(2023, 9, 29), broadcast=None, episodes=2)

    assert airing[0].at == datetime(2023, 9, 29, 14, 0, tzinfo=UTC)  # 23:00 JST
    assert airing[0].at.date() == date(2023, 9, 29)
    assert airing[1].at == datetime(2023, 10, 6, 14, 0, tzinfo=UTC)
    assert all(entry.estimated for entry in airing)


@pytest.mark.parametrize(
    ("start_date", "episodes"),
    [(None, 12), (date(2023, 9, 29), None), (date(2023, 9, 29), 0)],
)
def test_nothing_is_synthesised_without_a_date_and_a_count(
    start_date: date | None, episodes: int | None
) -> None:
    assert synthesise_airing(start_date=start_date, broadcast=None, episodes=episodes) == []


def test_a_nonsense_episode_count_is_capped() -> None:
    """A bad ``num_episodes`` must not write ten thousand episode rows."""
    airing = synthesise_airing(start_date=date(2000, 1, 1), broadcast=None, episodes=100_000)

    assert len(airing) == 2000


# --- The synthesised next broadcast -----------------------------------------


#: A Wednesday, so "the next Friday" is later the same week.
WEDNESDAY_JST = datetime(2026, 11, 4, 12, 0, tzinfo=JST)
FRIDAY_SLOT = (4, time(23, 0))


def test_the_next_broadcast_is_the_coming_weekday_in_tokyo() -> None:
    at = next_broadcast(FRIDAY_SLOT, now=WEDNESDAY_JST)

    assert at == datetime(2026, 11, 6, 23, 0, tzinfo=JST)
    assert at.tzinfo is UTC  # returned in UTC, like AniList's own airingAt
    assert at.astimezone(JST).weekday() == 4


def test_a_slot_that_has_just_passed_moves_to_next_week() -> None:
    """Half an hour after Friday's broadcast, the next one is a week away."""
    just_after = datetime(2026, 11, 6, 23, 30, tzinfo=JST)

    at = next_broadcast(FRIDAY_SLOT, now=just_after)

    assert at.astimezone(JST) == datetime(2026, 11, 13, 23, 0, tzinfo=JST)


def test_a_slot_still_to_come_today_is_today() -> None:
    before = datetime(2026, 11, 6, 9, 0, tzinfo=JST)

    at = next_broadcast(FRIDAY_SLOT, now=before)

    assert at.astimezone(JST) == datetime(2026, 11, 6, 23, 0, tzinfo=JST)


def test_the_weekday_is_japanese_not_the_callers() -> None:
    """A late-night Tokyo slot is the previous day almost everywhere else."""
    at = next_broadcast(FRIDAY_SLOT, now=WEDNESDAY_JST)

    assert at.astimezone(UTC).weekday() == 4  # 14:00 UTC on the Friday
    assert at.astimezone(ZoneInfo("America/Los_Angeles")).weekday() == 4


def test_only_a_currently_airing_show_gets_a_synthesised_slot() -> None:
    assert (
        synthesise_next_airing(status="FINISHED", broadcast=FRIDAY_SLOT, now=WEDNESDAY_JST) is None
    )
    assert synthesise_next_airing(status="RELEASING", broadcast=None, now=WEDNESDAY_JST) is None


def test_the_synthesised_slot_names_no_episode() -> None:
    """MAL's ``num_episodes`` is the total, not the count aired (FR-C6)."""
    blob = synthesise_next_airing(status="RELEASING", broadcast=FRIDAY_SLOT, now=WEDNESDAY_JST)

    assert blob is not None
    assert blob["episode"] is None
    assert blob["estimated"] is True
    assert datetime.fromtimestamp(blob["airingAt"], JST) == datetime(2026, 11, 6, 23, 0, tzinfo=JST)


def test_a_currently_airing_summary_carries_the_slot() -> None:
    """This is what puts a MAL-sourced season row on a weekday (FR-C3)."""
    raw = {
        "id": 4242,
        "title": "Airing Now",
        "media_type": "tv",
        "status": "currently_airing",
        "num_episodes": 12,
        "broadcast": {"day_of_the_week": "friday", "start_time": "23:00"},
    }

    media = parse_anime(raw, full=False, now=WEDNESDAY_JST)

    assert media.next_airing is not None
    assert media.next_airing["episode"] is None
    assert datetime.fromtimestamp(media.next_airing["airingAt"], JST).weekday() == 4


def test_a_finished_summary_carries_no_slot() -> None:
    raw = load("search_frieren")["data"][0]["node"]

    assert parse(raw, full=False, now=WEDNESDAY_JST).next_airing is None


# --- Parsing a whole record -------------------------------------------------


def test_detail_parses_into_the_shared_vocabulary() -> None:
    """MAL's words, stored as AniList's, because the columns already hold those."""
    media = parse(anime_payload(), full=True)

    assert media.source == "mal"
    assert media.mal_id == FRIEREN_MAL_ID
    assert media.anilist_id is None  # MAL has never heard of AniList
    assert media.title.romaji == "Sousou no Frieren"
    assert media.title.english == "Frieren: Beyond Journey's End"
    assert media.title.native == "葬送のフリーレン"
    assert media.title.preferred == "Frieren: Beyond Journey's End"
    assert "Frieren at the Funeral" in media.synonyms
    # finished_airing → FINISHED, tv → TV, fall → FALL.
    assert (media.status, media.format) == ("FINISHED", "TV")
    assert (media.season, media.season_year) == ("FALL", 2023)
    assert media.episodes == 28
    assert media.studio == "Madhouse"
    assert "Fantasy" in media.genres
    assert media.cover_url is not None and media.cover_url.startswith("https://")
    assert media.start_date == date(2023, 9, 29)
    assert media.broadcast == (4, time(23, 0))
    assert media.full is True
    # MAL publishes no per-episode schedule and no "next airing".
    assert media.next_airing is None
    assert media.tags == []


def test_the_fallback_carries_the_studio_credit_and_nothing_else_new() -> None:
    """M15's three fields, from the source that publishes almost none of them.

    MAL's official API has no staff endpoint, no per-episode list, and nothing
    bigger than a 230 px ``main_picture.large``. So the credits block is the
    studio row alone and the other two are empty — which is what the show page
    renders during an AniList outage, and what AniList overwrites the moment it
    answers again.
    """
    media = parse(anime_payload(), full=True)

    assert media.credits == [{"role": "Studio", "name": "Madhouse"}]
    assert media.cover_large_url is None
    assert media.episode_extras == []


def test_a_summary_carries_no_credits_either() -> None:
    media = parse(anime_payload(), full=False)

    assert media.credits == []
    assert media.cover_large_url is None


def test_the_synopsis_is_plain_text_already() -> None:
    media = parse(anime_payload(), full=True)

    assert media.description is not None
    assert media.description.startswith("During their decade-long quest")
    assert "<br>" not in media.description
    # Real newlines, not HTML paragraph breaks: nothing is stripped out of it.
    assert "\n\n" in media.description


def test_detail_synthesises_the_whole_schedule() -> None:
    media = parse(anime_payload(), full=True)

    assert len(media.airing) == 28
    assert media.airing[0].at == FIRST_EPISODE_UTC
    assert media.airing[1].at == datetime(2023, 10, 6, 14, 0, tzinfo=UTC)
    assert all(entry.estimated for entry in media.airing)


def test_relations_are_mal_keyed_and_use_the_shared_relation_words() -> None:
    media = parse(anime_payload(), full=True)

    kinds = {relation.relation_type for relation in media.relations}
    assert "SEQUEL" in kinds  # MAL's "sequel", upper-cased to match AniList
    assert "SIDE_STORY" in kinds
    sequel = next(r for r in media.relations if r.relation_type == "SEQUEL")
    assert sequel.mal_id == 59978
    assert sequel.anilist_id is None
    assert sequel.format == "TV"


def test_a_summary_carries_no_detail_columns() -> None:
    """The flag the cache's "a search must not blank a detail fetch" rule reads."""
    raw = load("search_frieren")["data"][0]["node"]
    media = parse(raw, full=False)

    assert media.full is False
    assert media.mal_id == FRIEREN_MAL_ID
    assert media.episodes == 28
    assert media.description is None
    assert media.relations == []
    assert media.airing == []
    # …but the broadcast slot is still there, because the season pre-cache only
    # ever sees summaries and the schedule page needs a weekday (FR-C7).
    assert media.broadcast == (4, time(23, 0))


def test_parse_survives_a_record_with_almost_nothing_in_it() -> None:
    media = parse_anime({"id": 1}, full=True)

    assert media.mal_id == 1
    assert media.title.preferred == ""
    assert (media.status, media.format, media.episodes) == (None, None, None)
    assert media.airing == []
    assert media.genres == []


# --- The client -------------------------------------------------------------


async def test_search_returns_a_page() -> None:
    fake = frieren_fake()
    source = fake.source()
    try:
        page = await source.search("frieren")
    finally:
        await source.aclose()

    assert [media.mal_id for media in page.results][0] == FRIEREN_MAL_ID
    assert page.page == 1
    assert page.has_next is False
    assert fake.unauthenticated == 0
    operation, _path, query = fake.calls[0]
    assert operation == "search"
    assert query["q"] == "frieren"
    assert query["limit"] == "20"


async def test_search_pages_by_offset_not_page_number() -> None:
    """MAL has no page parameter; the service's page number is translated."""
    fake = frieren_fake()
    source = fake.source()
    try:
        await source.search("frieren", page=3)
    finally:
        await source.aclose()

    assert fake.calls[0][2]["offset"] == "40"


async def test_by_mal_id_returns_the_detail_record() -> None:
    fake = frieren_fake()
    source = fake.source()
    try:
        media = await source.by_mal_id(FRIEREN_MAL_ID)
    finally:
        await source.aclose()

    assert media is not None
    assert media.full is True
    assert media.episodes == 28


async def test_by_anilist_id_is_none_rather_than_an_error() -> None:
    """MAL cannot answer the question. That is not the same as "no such show".

    The service reads ``None`` as "ask the next source" and a raise as "stop",
    so getting this wrong would turn a fallback into a 502.
    """
    source = frieren_fake().source()
    try:
        assert await source.by_anilist_id(154587) is None
    finally:
        await source.aclose()


async def test_an_unknown_id_is_not_found() -> None:
    source = frieren_fake().source()
    try:
        with pytest.raises(SourceNotFound):
            await source.by_mal_id(999999)
    finally:
        await source.aclose()


async def test_a_season_is_a_list_of_summaries() -> None:
    fake = frieren_fake()
    source = fake.source()
    try:
        media = await source.season(SEASON_YEAR, SEASON_NAME)
    finally:
        await source.aclose()

    assert FRIEREN_MAL_ID in {item.mal_id for item in media}
    assert all(item.full is False for item in media)
    assert all(item.source == "mal" for item in media)
    # The season name goes out lower-cased, which is MAL's spelling of it.
    assert fake.calls[0][1].endswith("/anime/season/2023/fall")


async def test_no_client_id_is_unavailable_not_a_crash(slept: list[float]) -> None:
    """A deployment without a MAL registration simply has no fallback (FR-C6)."""
    fake = frieren_fake()
    source = fake.source(client_id=None)
    try:
        assert source.configured is False
        with pytest.raises(SourceUnavailable) as caught:
            await source.search("frieren")
    finally:
        await source.aclose()

    assert caught.value.reason == "unconfigured"
    assert fake.calls == []  # nothing was even attempted


async def test_a_5xx_is_retried_once_then_reported(slept: list[float]) -> None:
    fake = FakeMal(fail_with=503)
    source = fake.source()
    try:
        with pytest.raises(SourceUnavailable, match="HTTP 503"):
            await source.by_mal_id(FRIEREN_MAL_ID)
    finally:
        await source.aclose()

    assert len(fake.calls) == 2  # one try plus one retry
    assert len(slept) == 1


async def test_a_5xx_that_recovers_is_not_reported(slept: list[float]) -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=anime_payload())

    source = MalSource(
        url="http://mal.test/v2",
        client_id="test",
        transport=httpx.MockTransport(handle),
    )
    try:
        media = await source.by_mal_id(FRIEREN_MAL_ID)
    finally:
        await source.aclose()

    assert media is not None and media.mal_id == FRIEREN_MAL_ID
    assert len(calls) == 2


async def test_a_429_is_retried_once(slept: list[float]) -> None:
    fake = FakeMal(fail_with=429)
    source = fake.source()
    try:
        with pytest.raises(SourceUnavailable, match="HTTP 429"):
            await source.search("frieren")
    finally:
        await source.aclose()

    assert len(fake.calls) == 2


async def test_a_bad_client_id_is_unavailable_not_forbidden(slept: list[float]) -> None:
    """A 401/403 on a *read* endpoint means Arc's credential is wrong.

    Reporting it as anything but "this source is down" would leak an operator's
    misconfiguration into a user-facing 403.
    """
    fake = FakeMal(fail_with=401)
    source = fake.source()
    try:
        with pytest.raises(SourceUnavailable, match="HTTP 401"):
            await source.search("frieren")
    finally:
        await source.aclose()


async def test_a_connection_failure_is_unavailable(slept: list[float]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    source = MalSource(
        url="http://mal.test/v2", client_id="test", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(SourceUnavailable, match="ConnectError"):
            await source.search("frieren")
    finally:
        await source.aclose()


async def test_a_non_json_body_is_unavailable(slept: list[float]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>cloudflare</html>")

    source = MalSource(
        url="http://mal.test/v2", client_id="test", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(SourceUnavailable, match="non-JSON"):
            await source.by_mal_id(1)
    finally:
        await source.aclose()


async def test_the_client_id_travels_as_a_header() -> None:
    """MAL API v2 reads authenticate with this and nothing else."""
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(CLIENT_ID_HEADER))
        return httpx.Response(200, json=anime_payload())

    source = MalSource(
        url="http://mal.test/v2", client_id="abc123", transport=httpx.MockTransport(handle)
    )
    try:
        await source.by_mal_id(FRIEREN_MAL_ID)
    finally:
        await source.aclose()

    assert seen == ["abc123"]
