"""The TMDB client: the five wrappers, the retries, and the breaker.

No database and no network — a real :class:`TmdbClient` over
``httpx.MockTransport`` serving the recorded fixtures, so everything except the
socket is the production path (``tests/tmdb_mock.py``).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from arc.config import Settings
from arc.services.catalog.breaker import Breaker
from arc.services.tmdb import client as tmdb_client
from arc.services.tmdb.client import (
    BACKDROP_SIZE,
    DEFAULT_RETRY_AFTER,
    IMAGE_BASE,
    MAX_RETRY_AFTER,
    SOURCE,
    STILL_SIZE,
    TmdbClient,
    TmdbError,
    TmdbNotFound,
    TmdbUnavailable,
    image_url,
)
from tests.tmdb_mock import (
    API_KEY,
    FILM_ID,
    FRIEREN_SEASON,
    FRIEREN_TV_ID,
    URL,
    FakeTmdb,
)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make the client's waits free, and record what they asked for."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(tmdb_client, "_sleep", fake_sleep)
    return recorded


@pytest.fixture(autouse=True)
def _fresh_breaker() -> Iterator[None]:
    """The breaker is process-wide in production; no test may inherit one."""
    tmdb_client.reset_breaker()
    yield
    tmdb_client.reset_breaker()


def build(fake: FakeTmdb, **kwargs: object) -> TmdbClient:
    return TmdbClient(
        API_KEY,
        url=URL,
        transport=fake.transport(),
        breaker=Breaker(seconds=300.0),
        **kwargs,  # type: ignore[arg-type]
    )


# --- Image URLs -------------------------------------------------------------


def test_image_url_joins_the_base_the_size_and_the_path() -> None:
    assert image_url("/abc.jpg", BACKDROP_SIZE) == f"{IMAGE_BASE}{BACKDROP_SIZE}/abc.jpg"


@pytest.mark.parametrize("value", [None, "", "   ", 12, {"path": "/a.jpg"}])
def test_image_url_is_none_for_anything_that_is_not_a_path(value: object) -> None:
    """A null path must never become the literal URL ``…/w300None``."""
    assert image_url(value, STILL_SIZE) is None  # type: ignore[arg-type]


# --- The wrappers -----------------------------------------------------------


async def test_tv_returns_the_series_with_its_seasons(slept: list[float]) -> None:
    fake = FakeTmdb()
    async with build(fake) as client:
        show = await client.tv(FRIEREN_TV_ID)
    assert show["name"] == "Frieren: Beyond Journey's End"
    assert show["backdrop_path"].endswith(".jpg")
    assert {season["season_number"] for season in show["seasons"]} == {0, 1}
    assert fake.calls == [f"/tv/{FRIEREN_TV_ID}"]


async def test_tv_season_returns_numbered_episodes(slept: list[float]) -> None:
    fake = FakeTmdb()
    async with build(fake) as client:
        season = await client.tv_season(FRIEREN_TV_ID, FRIEREN_SEASON)
    numbers = [episode["episode_number"] for episode in season["episodes"]]
    assert numbers == sorted(numbers)
    assert season["episodes"][0]["name"] == "The Journey's End"


async def test_tv_credits_uses_the_aggregate_endpoint(slept: list[float]) -> None:
    """The flat one lists producers; the aggregate carries episode counts."""
    fake = FakeTmdb()
    async with build(fake) as client:
        await client.tv_credits(FRIEREN_TV_ID)
    assert fake.calls == [f"/tv/{FRIEREN_TV_ID}/aggregate_credits"]


async def test_movie_and_movie_credits(slept: list[float]) -> None:
    fake = FakeTmdb()
    async with build(fake) as client:
        film = await client.movie(FILM_ID)
        credits = await client.movie_credits(FILM_ID)
    assert film["title"] == "A Silent Voice: The Movie"
    assert any(member.get("job") == "Director" for member in credits["crew"])


async def test_the_key_travels_as_a_query_parameter(slept: list[float]) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 1})

    async with TmdbClient(API_KEY, url=URL, transport=httpx.MockTransport(handler)) as client:
        await client.get("/tv/1")
    assert seen[0].url.params["api_key"] == API_KEY
    # ``get`` adds nothing of its own; the wrappers are what ask for a language.
    assert "language" not in seen[0].url.params


# --- Failures ---------------------------------------------------------------


async def test_a_404_is_not_found_and_leaves_the_breaker_closed(slept: list[float]) -> None:
    """A stale id in the weekly cross-id map is not an outage."""
    fake = FakeTmdb()
    client = build(fake)
    try:
        with pytest.raises(TmdbNotFound):
            await client.tv(999999)
        assert not client.breaker.is_open(SOURCE)
    finally:
        await client.aclose()


async def test_a_429_is_slept_off_once_then_opens_the_breaker(slept: list[float]) -> None:
    fake = FakeTmdb()
    fake.status[f"/tv/{FRIEREN_TV_ID}"] = 429
    fake.retry_after = "7"
    client = build(fake)
    try:
        with pytest.raises(TmdbUnavailable):
            await client.tv(FRIEREN_TV_ID)
        assert 7.0 in slept
        assert client.breaker.is_open(SOURCE)
        assert client.breaker.state(SOURCE).reason == "rate limited twice"
    finally:
        await client.aclose()


async def test_a_429_without_a_header_waits_the_default_not_the_cap(slept: list[float]) -> None:
    fake = FakeTmdb()
    fake.status[f"/tv/{FRIEREN_TV_ID}"] = 429
    client = build(fake)
    try:
        with pytest.raises(TmdbUnavailable):
            await client.tv(FRIEREN_TV_ID)
    finally:
        await client.aclose()
    assert DEFAULT_RETRY_AFTER in slept
    assert MAX_RETRY_AFTER not in slept


async def test_a_429_that_clears_is_retried_and_succeeds(slept: list[float]) -> None:
    fake = FakeTmdb()
    fake.status[f"/tv/{FRIEREN_TV_ID}"] = 429
    fake.status_times[f"/tv/{FRIEREN_TV_ID}"] = 1
    async with build(fake) as client:
        show = await client.tv(FRIEREN_TV_ID)
    assert show["id"] == FRIEREN_TV_ID


async def test_a_5xx_is_retried_twice_then_opens_the_breaker(slept: list[float]) -> None:
    fake = FakeTmdb()
    fake.status[f"/tv/{FRIEREN_TV_ID}"] = 503
    client = build(fake)
    try:
        with pytest.raises(TmdbUnavailable):
            await client.tv(FRIEREN_TV_ID)
    finally:
        await client.aclose()
    assert fake.calls.count(f"/tv/{FRIEREN_TV_ID}") == 3
    # The pacing gap goes through the same ``_sleep``; the backoffs are the
    # two long waits in the list.
    assert [pause for pause in slept if pause >= 1.0] == [1.0, 2.0]
    assert client.breaker.is_open(SOURCE)


async def test_an_open_breaker_skips_the_call_entirely(slept: list[float]) -> None:
    fake = FakeTmdb()
    client = build(fake)
    try:
        client.breaker.record_failure(SOURCE, "an earlier outage")
        with pytest.raises(TmdbUnavailable):
            await client.tv(FRIEREN_TV_ID)
    finally:
        await client.aclose()
    assert fake.calls == []


async def test_a_401_opens_the_breaker_rather_than_looping(slept: list[float]) -> None:
    """A wrong key would fail identically on all two hundred shows."""
    fake = FakeTmdb()
    fake.status[f"/tv/{FRIEREN_TV_ID}"] = 401
    client = build(fake)
    try:
        with pytest.raises(TmdbUnavailable):
            await client.tv(FRIEREN_TV_ID)
        assert client.breaker.is_open(SOURCE)
    finally:
        await client.aclose()
    assert fake.calls.count(f"/tv/{FRIEREN_TV_ID}") == 1


async def test_a_non_json_body_is_an_error(slept: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async with TmdbClient(API_KEY, url=URL, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TmdbError):
            await client.tv(1)


# --- Configuration ----------------------------------------------------------


def test_from_settings_refuses_to_build_without_a_key() -> None:
    settings = Settings(env="test", _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(TmdbError):
        TmdbClient.from_settings(settings)
