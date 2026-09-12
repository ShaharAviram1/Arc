"""The AniList client: parsing, retries, and the pure helpers around it.

No database and no network — a real :class:`AniListClient` over
``httpx.MockTransport``, so everything except the socket is the production
path. See ``tests/anilist_mock.py`` for where the fixture JSON comes from.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from arc.services.anilist import (
    AniListClient,
    AniListDisabled,
    AniListError,
    AniListNotFound,
    AniListRateLimited,
    parse_media,
    strip_html,
)
from arc.services.anilist.client import (
    DEFAULT_RETRY_AFTER,
    LOW_BUDGET_PAUSE,
    MAX_RETRY_AFTER,
    MAX_SCHEDULE_PAGES,
    SCHEDULE_PER_PAGE,
)
from arc.services.anilist.source import AniListSource
from arc.services.catalog import Breaker, CatalogService
from arc.services.catalog.source import MediaTitle, SourceNotFound, SourceUnavailable
from tests.anilist_mock import (
    DISABLED_BODY,
    DISABLED_STATUS,
    FRIEREN_ID,
    LONG_RUNNING_AIRED,
    LONG_RUNNING_ID,
    RELEASING_ID,
    FakeAniList,
    frieren_fake,
    load,
    long_running_fake,
    media_payload,
    summary_of,
)
from tests.mal_mock import FRIEREN_MAL_ID, FakeMal
from tests.mal_mock import load as mal_load

# --- Description → plain text -----------------------------------------------


def test_strip_html_turns_br_into_newlines() -> None:
    assert strip_html("one<br>two") == "one\ntwo"
    assert strip_html("one<br />two") == "one\ntwo"
    assert strip_html("one<BR/>two") == "one\ntwo"


def test_strip_html_drops_tags_and_unescapes_entities() -> None:
    raw = "<i>Frieren</i> &amp; friends <b>defeat</b> the demon king &mdash; slowly."
    assert strip_html(raw) == "Frieren & friends defeat the demon king — slowly."


def test_strip_html_collapses_the_paragraph_break_anilist_writes() -> None:
    # AniList separates paragraphs with "<br>\n<br>\n", which becomes four
    # newlines if taken literally.
    assert strip_html("first<br>\n<br>\nsecond") == "first\n\nsecond"


def test_strip_html_passes_none_and_empties_through() -> None:
    assert strip_html(None) is None
    assert strip_html("   ") is None
    assert strip_html("<br><br>") is None


def test_frieren_synopsis_survives_stripping() -> None:
    text = strip_html(media_payload("media_154587")["description"])
    assert text is not None
    assert "<" not in text
    assert text.startswith("The adventure is over but life goes on")
    assert "\n\n" in text


# --- Preferred title --------------------------------------------------------


def test_preferred_title_prefers_english() -> None:
    title = MediaTitle(romaji="Sousou no Frieren", english="Frieren: Beyond Journey's End")
    assert title.preferred == "Frieren: Beyond Journey's End"


def test_preferred_title_falls_back_to_romaji_then_native() -> None:
    assert MediaTitle(romaji="Sousou no Frieren").preferred == "Sousou no Frieren"
    assert MediaTitle(native="葬送のフリーレン").preferred == "葬送のフリーレン"
    assert MediaTitle().preferred == ""


# --- Parsing ----------------------------------------------------------------


async def test_search_parses_summary_fields() -> None:
    fake = frieren_fake()
    async with fake.client() as client:
        page = await client.search("frieren")

    assert page.page == 1
    assert page.has_next is False
    first = page.results[0]
    assert first.anilist_id == FRIEREN_ID
    assert first.source == "anilist"
    assert first.title.preferred == "Frieren: Beyond Journey’s End"
    assert first.title.romaji == "Sousou no Frieren"
    assert (first.format, first.episodes, first.status) == ("TV", 28, "FINISHED")
    assert (first.season, first.season_year) == ("FALL", 2023)
    assert first.cover_url is not None and first.cover_url.endswith(".jpg")
    # A search result is explicitly *not* a full record; the cache relies on
    # this to avoid overwriting a detail fetch.
    assert first.full is False
    assert first.airing == []


async def test_media_parses_the_airing_schedule() -> None:
    fake = frieren_fake()
    async with fake.client() as client:
        media = await client.media(FRIEREN_ID)

    assert media.full is True
    assert media.mal_id == 52991
    assert media.studio == "MADHOUSE"
    assert media.genres == ["Adventure", "Drama", "Fantasy"]
    assert media.next_airing is None

    # AniList's schedule starts at episode 5: the four-episode premiere aired
    # as one two-hour broadcast and has no per-episode slot at all. Everything
    # below the first published number is Arc's problem, not the parser's
    # (see ``_backfill_before_the_schedule``).
    assert [entry.episode for entry in media.airing] == list(range(5, 29))
    assert media.airing[0].at == datetime(2023, 10, 6, 14, 0, tzinfo=UTC)
    assert media.airing[-1].at == datetime(2024, 3, 22, 14, 0, tzinfo=UTC)
    # AniList publishes these; nothing here is a guess (FR-C6).
    assert not any(entry.estimated for entry in media.airing)


async def test_media_parses_the_credits_and_the_episode_art() -> None:
    """The two M15 fields, from the query through to :class:`CatalogMedia`."""
    fake = frieren_fake()
    async with fake.client() as client:
        media = await client.media(FRIEREN_ID)

    assert media.cover_large_url is not None
    assert "/cover/large/" in media.cover_large_url
    assert media.credits[0] == {"role": "Studio", "name": "MADHOUSE"}
    # The fixture's staff connection credits no composer, so the "Made by"
    # block is five rows rather than six; a credit AniList does not publish is
    # a row the show page leaves out, not one to fill from somewhere else.
    assert [row["role"] for row in media.credits[1:]] == [
        "Director",
        "Series Composition",
        "Character Design",
        "Original Creator",
    ]

    # The fixture's ``streamingEpisodes`` covers the whole run, one link per
    # episode, and every one of them is placed by the number in its title.
    assert [art.number for art in media.episode_extras] == list(range(1, 29))
    assert media.episode_extras[0].title == "The Journey's End"
    assert media.episode_extras[0].still_url is not None
    assert media.episode_extras[-1].title == "It Would Be Embarrassing When We Met Again"


async def test_a_search_result_carries_the_key_art_and_nothing_else_new() -> None:
    """``coverImage.extraLarge`` is in the summary fragment; staff is not.

    Which is the point of putting the two new fields in ``DETAIL_SELECTION``:
    a card gets the sharp artwork for free, and a season sweep does not pay for
    two hundred staff connections.
    """
    fake = frieren_fake()
    async with fake.client() as client:
        page = await client.search("frieren")

    first = page.results[0]
    assert first.cover_large_url is not None
    assert first.credits == []
    assert first.episode_extras == []


async def test_media_parses_relations_and_drops_non_anime() -> None:
    fake = frieren_fake()
    async with fake.client() as client:
        media = await client.media(FRIEREN_ID)

    kinds = {relation.relation_type for relation in media.relations}
    assert kinds == {"SEQUEL", "SIDE_STORY", "CHARACTER", "OTHER"}
    # The source manga is in the fixture as an ADAPTATION edge and must not
    # survive: every relation becomes a link to a show page.
    assert "ADAPTATION" not in kinds
    assert all(relation.anilist_id is not None for relation in media.relations)
    sequel = next(r for r in media.relations if r.relation_type == "SEQUEL")
    assert sequel.anilist_id == 182255
    assert sequel.mal_id == 59978
    assert sequel.title.preferred == "Frieren: Beyond Journey’s End Season 2"
    assert sequel.format == "TV"


async def test_media_parses_a_releasing_show_next_airing() -> None:
    fake = FakeAniList(media={RELEASING_ID: load("media_999001_releasing")})
    async with fake.client() as client:
        media = await client.media(RELEASING_ID)

    assert media.status == "RELEASING"
    assert media.next_airing is not None
    assert media.next_airing["episode"] == 6
    # Both schedule pages are merged and re-sorted into one 1..12 run.
    assert [entry.episode for entry in media.airing] == list(range(1, 13))


# --- Paging the aired schedule ----------------------------------------------


async def test_a_show_shorter_than_a_page_is_one_round_trip() -> None:
    """The common case must not pay for the long-running one."""
    fake = frieren_fake()
    async with fake.client() as client:
        media = await client.media(FRIEREN_ID)

    assert len(media.airing) == 24
    assert fake.calls == [("media", {"id": FRIEREN_ID})]


async def test_a_long_back_catalogue_is_paged_until_it_runs_out() -> None:
    fake = long_running_fake()
    async with fake.client() as client:
        media = await client.media(LONG_RUNNING_ID)

    # Every aired episode, not the first hundred.
    assert [entry.episode for entry in media.airing] == list(range(1, LONG_RUNNING_AIRED + 1))
    # One by-id query, then one follow-up per further page — and it stops as
    # soon as ``hasNextPage`` says there is nothing left.
    assert [name for name, _ in fake.calls] == ["media", "schedule", "schedule"]
    assert [variables["page"] for name, variables in fake.calls if name == "schedule"] == [2, 3]


async def test_a_failed_schedule_page_keeps_the_title_that_did_arrive(
    caplog: pytest.LogCaptureFixture, slept: list[float]
) -> None:
    """The show is not lost over the tail of its back catalogue."""
    fake = long_running_fake()
    payload = fake.media[LONG_RUNNING_ID]

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        variables = json.loads(request.content)["variables"]
        if "page" not in variables:
            return httpx.Response(200, json=payload)
        return httpx.Response(503, text="down")

    with caplog.at_level("WARNING"):
        async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
            media = await client.media(LONG_RUNNING_ID)

    assert media.anilist_id == LONG_RUNNING_ID
    assert media.title.preferred == "The Long One"
    assert len(media.airing) == SCHEDULE_PER_PAGE  # page 1, and no more
    assert "schedule page failed" in caplog.text


async def test_the_schedule_paging_stops_at_the_cap(caplog: pytest.LogCaptureFixture) -> None:
    """A show that always claims another page must not become an endless fetch."""
    import json

    pages: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        page = variables.get("page")
        if page is None:  # the by-id query — page 1, and there is always more
            payload = load("media_154587")
            payload["data"]["Media"]["aired"]["pageInfo"] = {
                "currentPage": 1,
                "hasNextPage": True,
            }
            return httpx.Response(200, json=payload)
        pages.append(int(page))
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {
                        "id": FRIEREN_ID,
                        "aired": {
                            "pageInfo": {"currentPage": page, "hasNextPage": True},
                            "nodes": [],
                        },
                    }
                }
            },
        )

    with caplog.at_level("WARNING"):
        async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
            media = await client.media(FRIEREN_ID)

    assert pages == list(range(2, MAX_SCHEDULE_PAGES + 1))
    assert len(media.airing) == 24  # whatever was collected, not nothing
    assert "truncated" in caplog.text


def test_parse_media_survives_missing_optional_blocks() -> None:
    media = parse_media({"id": 1, "title": None}, full=True)
    assert media.anilist_id == 1
    assert media.title.preferred == ""
    assert media.genres == []
    assert media.relations == []
    assert media.airing == []
    assert media.studio is None


# --- Errors and retries -----------------------------------------------------


async def test_null_media_raises_not_found() -> None:
    fake = FakeAniList()
    async with fake.client() as client:
        with pytest.raises(AniListNotFound):
            await client.media(1)


async def test_a_404_graphql_error_raises_not_found() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"message": "Not Found.", "status": 404}]})

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AniListNotFound):
            await client.media(1)


async def test_429_is_slept_off_and_retried_once(slept: list[float]) -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "3", "X-RateLimit-Remaining": "0"}, json={}
            )
        return httpx.Response(200, json=load("media_154587"))

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        media = await client.media(FRIEREN_ID)

    assert media.anilist_id == FRIEREN_ID
    assert len(calls) == 2
    # The header was read, not a fixed guess…
    assert slept[0] == 3.0
    # …and "no budget left" widened the gap before the retry went out.
    assert any(pause >= LOW_BUDGET_PAUSE - 0.01 for pause in slept[1:])


async def test_a_429_without_a_retry_after_waits_seconds_not_a_minute(
    slept: list[float],
) -> None:
    """A missing header is not a request for the maximum.

    Cloudflare in front of AniList answers some overruns with a bare 429, and
    a search request is usually waiting on this: three seconds is a stutter,
    sixty is a broken page.
    """
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json=load("media_154587"))

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        media = await client.media(FRIEREN_ID)

    assert media.anilist_id == FRIEREN_ID
    assert slept == [DEFAULT_RETRY_AFTER]
    assert DEFAULT_RETRY_AFTER < MAX_RETRY_AFTER


async def test_an_unparseable_retry_after_falls_back_to_the_default(
    slept: list[float],
) -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            # AniList's HTTP-date form of Retry-After, which is not a number.
            return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        return httpx.Response(200, json=load("media_154587"))

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        await client.media(FRIEREN_ID)

    assert slept == [DEFAULT_RETRY_AFTER]


async def test_a_retry_after_that_was_sent_is_still_capped_at_a_minute(
    slept: list[float],
) -> None:
    """The ceiling applies to a header, not to its absence."""
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "600"}, json={})
        return httpx.Response(200, json=load("media_154587"))

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        await client.media(FRIEREN_ID)

    assert slept == [MAX_RETRY_AFTER]


async def test_a_second_429_gives_up_rather_than_looping(slept: list[float]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AniListError, match="rate limited twice"):
            await client.media(FRIEREN_ID)


async def test_5xx_is_retried_twice_then_fails(slept: list[float]) -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="upstream is unhappy")

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AniListError, match="HTTP 503"):
            await client.media(FRIEREN_ID)

    assert len(calls) == 3  # one try plus two retries
    assert slept == [1.0, 2.0]  # exponential, not a busy loop


async def test_a_5xx_that_recovers_is_not_reported(slept: list[float]) -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, text="oops")
        return httpx.Response(200, json=load("media_154587"))

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        media = await client.media(FRIEREN_ID)

    assert media.anilist_id == FRIEREN_ID
    assert len(calls) == 2


async def test_a_graphql_error_that_is_not_a_404_is_an_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Validation error"}]})

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AniListError, match="Validation error"):
            await client.media(FRIEREN_ID)
        # …and it must not be the subclass, or a bad query would read as a
        # missing show and 404 the user.
        with pytest.raises(AniListError) as caught:
            await client.media(FRIEREN_ID)
    assert not isinstance(caught.value, AniListNotFound)


async def test_a_non_json_body_is_an_error_not_a_crash() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>cloudflare</html>")

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AniListError, match="non-JSON"):
            await client.media(FRIEREN_ID)


async def test_requests_are_spaced_by_the_minimum_interval() -> None:
    import time

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("media_154587"))

    async with AniListClient(min_interval=0.05, transport=httpx.MockTransport(handle)) as client:
        started = time.monotonic()
        for _ in range(3):
            await client.media(FRIEREN_ID)
        elapsed = time.monotonic() - started

    # Three requests, two gaps of at least 50 ms.
    assert elapsed >= 0.1


# --- Looking a title up by its MAL id ---------------------------------------


async def test_media_by_mal_id_finds_the_same_record() -> None:
    """The query the reconciliation job runs (FR-C6)."""
    fake = frieren_fake()
    async with fake.client() as client:
        media = await client.media_by_mal_id(52991)

    assert media.anilist_id == FRIEREN_ID
    assert media.mal_id == 52991
    assert media.full is True
    assert len(media.airing) == 24
    assert [name for name, _ in fake.calls] == ["media.by_mal"]


async def test_media_by_mal_id_raises_not_found_for_an_unmapped_id() -> None:
    """AniList maps only some MAL ids, and its miss is not "no such show".

    :class:`CatalogService` relies on being able to tell the two apart: this
    one falls back to MAL, a miss on an *AniList* id does not.
    """
    fake = frieren_fake()
    async with fake.client() as client:
        with pytest.raises(AniListNotFound):
            await client.media_by_mal_id(999999)


# --- Seasons -----------------------------------------------------------------


async def test_season_returns_the_summaries_of_one_season() -> None:
    fake = frieren_fake()
    fake.seasons[(2023, "FALL")] = [summary_of(media_payload("media_154587"))]
    async with fake.client() as client:
        media = await client.season(2023, "fall")

    assert [item.anilist_id for item in media] == [FRIEREN_ID]
    assert all(item.full is False for item in media)
    # The season name goes out as AniList's enum, whatever case it arrived in.
    assert fake.calls[0][1]["season"] == "FALL"


async def test_season_stops_when_there_is_no_next_page() -> None:
    fake = frieren_fake()
    fake.seasons[(2023, "FALL")] = [summary_of(media_payload("media_154587"))]
    async with fake.client() as client:
        await client.season(2023, "FALL")

    assert len(fake.calls) == 1


# --- The outage this milestone exists for ------------------------------------


async def test_a_temporarily_disabled_403_is_its_own_error() -> None:
    """AniList's whole-API outage: HTTP 403 with a GraphQL error, not a 5xx.

    It must not be retried (there is nothing to wait for), must not read as
    "not found" (that would 404 every show), and must be distinguishable, so
    the status page can say *why* the catalogue switched to MAL.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(DISABLED_STATUS, json=DISABLED_BODY)

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AniListDisabled) as caught:
            await client.media(FRIEREN_ID)

    assert isinstance(caught.value, AniListError)
    assert not isinstance(caught.value, AniListNotFound)


async def test_a_disabled_api_is_not_retried(slept: list[float]) -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(DISABLED_STATUS, json=DISABLED_BODY)

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AniListDisabled):
            await client.search("frieren")

    assert calls == [1]
    assert slept == []


# --- The source adapter ------------------------------------------------------


async def test_the_source_speaks_the_catalogue_error_vocabulary() -> None:
    """Above this line nobody catches an ``AniListError``: the two sources have
    to fail the same way or the fallback needs a branch per source."""
    fake = frieren_fake()
    source = fake.source()
    try:
        with pytest.raises(SourceNotFound):
            await source.by_anilist_id(424242)

        fake.disabled = True
        with pytest.raises(SourceUnavailable) as caught:
            await source.search("frieren")
    finally:
        await source.aclose()

    assert caught.value.source == "anilist"
    assert "disabled" in caught.value.reason


async def test_a_transport_failure_through_the_source_is_unavailable(
    slept: list[float],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    source = AniListSource.over(httpx.MockTransport(handle))
    try:
        with pytest.raises(SourceUnavailable):
            await source.season(2026, "WINTER")
    finally:
        await source.aclose()


def test_the_source_needs_no_credentials() -> None:
    """Which is why it is the primary: nothing to register, nothing to expire."""
    assert frieren_fake().source().configured is True


# --- Interactive callers do not sleep off a 429 ------------------------------


async def test_an_interactive_client_raises_a_429_rather_than_waiting(
    slept: list[float],
) -> None:
    """A user is on the other end: falling back beats a three-second pause.

    The whole point is the absence of a wait, so the assertion is on the sleep
    hook — which the ``slept`` fixture makes free, meaning a regression here
    would otherwise pass in silence and cost three real seconds in production.
    """
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "3"}, json={})

    async with AniListClient(
        min_interval=0.0, transport=httpx.MockTransport(handle), wait_on_rate_limit=False
    ) as client:
        with pytest.raises(AniListRateLimited) as caught:
            await client.search("frieren")

        assert calls == [1]  # asked once, gave up, did not retry
        assert slept == []
        assert caught.value.retry_after == 3.0
        # …and the window it opened is what the next call will read.
        assert client.rate_limited_until > 0.0


async def test_a_second_interactive_call_inside_the_window_makes_no_request(
    slept: list[float],
) -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "30"}, json={})

    async with AniListClient(
        min_interval=0.0, transport=httpx.MockTransport(handle), wait_on_rate_limit=False
    ) as client:
        with pytest.raises(AniListRateLimited):
            await client.search("frieren")
        with pytest.raises(AniListRateLimited):
            await client.media(FRIEREN_ID)

    assert calls == [1]  # the second call never reached the transport
    assert slept == []


async def test_a_window_that_has_passed_lets_the_next_call_through(
    slept: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip is a few seconds wide, not a breaker: it expires on its own."""
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json=load("media_154587"))

    async with AniListClient(
        min_interval=0.0, transport=httpx.MockTransport(handle), wait_on_rate_limit=False
    ) as client:
        with pytest.raises(AniListRateLimited):
            await client.media(FRIEREN_ID)
        monkeypatch.setattr(
            "arc.services.anilist.client.time.monotonic",
            lambda: client.rate_limited_until + 0.1,
        )
        media = await client.media(FRIEREN_ID)

    assert media.anilist_id == FRIEREN_ID
    assert len(calls) == 2


async def test_a_job_client_still_sleeps_the_429_off(slept: list[float]) -> None:
    """The default is unchanged, and it is the worker's."""
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json=load("media_154587"))

    async with AniListClient(min_interval=0.0, transport=httpx.MockTransport(handle)) as client:
        media = await client.media(FRIEREN_ID)

    assert media.anilist_id == FRIEREN_ID
    assert slept[0] == 3.0
    assert client.rate_limited_until == 0.0


async def test_an_interactive_429_falls_back_to_mal_without_opening_the_breaker() -> None:
    """FR-C6's fallback, minus the five-minute stand-down: a burst limit is a
    moment, and skipping AniList for the next 300 s would be the real outage."""
    fake = frieren_fake()
    fake.rate_limited = True
    mal = FakeMal()
    mal.anime[FRIEREN_MAL_ID] = mal_load("anime_52991")
    breaker = Breaker(300.0)
    catalog = CatalogService(fake.source(wait_on_rate_limit=False), mal.source(), breaker)
    try:
        media = await catalog.by_mal_id(FRIEREN_MAL_ID)
    finally:
        await catalog.aclose()

    assert media is not None
    assert media.source == "mal"
    assert not breaker.is_open("anilist")
    assert catalog.status()["active"] == "anilist"
