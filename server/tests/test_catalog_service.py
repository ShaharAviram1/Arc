"""AniList first, MAL second, and a breaker that remembers (FR-C6).

These are the decisions that keep Arc usable through an AniList outage, so
they are tested against both fakes at once — a real
:class:`~arc.services.anilist.source.AniListSource` and a real
:class:`~arc.services.mal.catalog.MalSource`, each over ``MockTransport`` —
rather than against stubs of the protocol. What is asserted is mostly *which
source was called*, counted off the fakes, because "did not call AniList
again" is the entire content of a circuit breaker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arc.services.anilist.source import DISABLED_REASON
from arc.services.catalog.breaker import Breaker
from arc.services.catalog.local import MAX_TERMS, escape_like, terms_of
from arc.services.catalog.service import CatalogService
from arc.services.catalog.source import SourceNotFound, SourceUnavailable
from tests.anilist_mock import FRIEREN_ID, FakeAniList, frieren_fake, summary_of
from tests.mal_mock import FRIEREN_MAL_ID, SEASON_NAME, SEASON_YEAR, FakeMal
from tests.mal_mock import frieren_fake as mal_frieren_fake

#: Long enough that nothing reopens on its own during a test.
BREAKER_SECONDS = 300.0


@pytest.fixture
def anilist() -> FakeAniList:
    fake = frieren_fake()
    fake.seasons[(SEASON_YEAR, SEASON_NAME)] = [
        summary_of(fake.media[FRIEREN_ID]["data"]["Media"])  # type: ignore[index]
    ]
    return fake


@pytest.fixture
def mal() -> FakeMal:
    return mal_frieren_fake()


@pytest.fixture
async def catalog(anilist: FakeAniList, mal: FakeMal) -> AsyncIterator[CatalogService]:
    service = CatalogService(anilist.source(), mal.source(), Breaker(BREAKER_SECONDS))
    try:
        yield service
    finally:
        await service.aclose()


# --- The happy path ---------------------------------------------------------


async def test_a_healthy_primary_answers_and_the_fallback_is_untouched(
    catalog: CatalogService, anilist: FakeAniList, mal: FakeMal
) -> None:
    page = await catalog.search("frieren")

    assert page.results[0].anilist_id == FRIEREN_ID
    assert page.results[0].source == "anilist"
    assert len(anilist.calls) == 1
    assert mal.calls == []
    assert catalog.status()["active"] == "anilist"


async def test_an_empty_result_is_an_answer_not_a_failure(
    catalog: CatalogService, mal: FakeMal
) -> None:
    """A term nobody has heard of must not cost a second round trip."""
    page = await catalog.search("zzzznothing")

    assert page.results == []
    assert mal.calls == []


# --- Falling back -----------------------------------------------------------


async def test_a_disabled_primary_falls_back_to_mal(
    catalog: CatalogService, anilist: FakeAniList, mal: FakeMal, slept: list[float]
) -> None:
    """The outage that motivated all of this: 403 "temporarily disabled"."""
    anilist.disabled = True

    page = await catalog.search("frieren")

    assert [media.mal_id for media in page.results][0] == FRIEREN_MAL_ID
    assert all(media.source == "mal" for media in page.results)
    assert all(media.anilist_id is None for media in page.results)
    assert len(mal.calls) == 1


async def test_the_breaker_opens_and_the_second_call_skips_anilist(
    catalog: CatalogService, anilist: FakeAniList, mal: FakeMal, slept: list[float]
) -> None:
    """The whole point of the breaker: an outage costs one timeout, not one each."""
    anilist.disabled = True

    await catalog.search("frieren")
    calls_after_first = len(anilist.calls)
    await catalog.search("frieren")

    assert len(anilist.calls) == calls_after_first  # not asked again
    assert len(mal.calls) == 2
    assert catalog.breaker.is_open("anilist") is True
    assert catalog.status()["active"] == "mal"
    assert catalog.status()["sources"]["anilist"]["reason"] == DISABLED_REASON


async def test_the_switch_is_logged_once_per_outage(
    catalog: CatalogService,
    anilist: FakeAniList,
    caplog: pytest.LogCaptureFixture,
    slept: list[float],
) -> None:
    """A five-hour outage is one log line, not one per page view."""
    anilist.disabled = True

    with caplog.at_level("WARNING", logger="arc.services.catalog.service"):
        for _ in range(4):
            await catalog.search("frieren")

    switches = [r for r in caplog.records if "falling back" in r.message]
    assert len(switches) == 1


async def test_after_the_window_the_next_call_probes_again(
    anilist: FakeAniList, mal: FakeMal, slept: list[float]
) -> None:
    """A zero-length window is "the outage is over"; the probe must happen."""
    service = CatalogService(anilist.source(), mal.source(), Breaker(0.0))
    anilist.disabled = True
    try:
        await service.search("frieren")
        first = len(anilist.calls)

        anilist.disabled = False
        page = await service.search("frieren")
    finally:
        await service.aclose()

    assert len(anilist.calls) > first  # it was asked again
    assert page.results[0].source == "anilist"
    assert service.breaker.is_open("anilist") is False


async def test_a_success_closes_the_breaker_again(
    anilist: FakeAniList, mal: FakeMal, slept: list[float]
) -> None:
    service = CatalogService(anilist.source(), mal.source(), Breaker(0.0))
    anilist.disabled = True
    try:
        await service.search("frieren")
        assert service.status()["sources"]["anilist"]["failed_at"] is not None
        anilist.disabled = False
        await service.search("frieren")
    finally:
        await service.aclose()

    state = service.status()["sources"]["anilist"]
    assert state["state"] == "closed"
    assert state["healthy_at"] is not None
    assert state["reason"] is None


async def test_both_sources_down_is_an_error_not_an_empty_page(
    anilist: FakeAniList, slept: list[float]
) -> None:
    """ "No results" and "the catalogue is down" must not look the same."""
    anilist.disabled = True
    mal = FakeMal(fail_with=503)
    service = CatalogService(anilist.source(), mal.source(), Breaker(BREAKER_SECONDS))
    try:
        with pytest.raises(SourceUnavailable):
            await service.search("frieren")
    finally:
        await service.aclose()

    assert service.status()["active"] == "none"


async def test_an_unconfigured_fallback_is_never_called(
    anilist: FakeAniList, slept: list[float]
) -> None:
    """Without ``MAL_CLIENT_ID`` there is simply no fallback."""
    mal = mal_frieren_fake()
    service = CatalogService(anilist.source(), mal.source(client_id=None), Breaker(1.0))
    anilist.disabled = True
    try:
        with pytest.raises(SourceUnavailable):
            await service.search("frieren")
    finally:
        await service.aclose()

    assert mal.calls == []
    assert service.status()["sources"]["mal"]["configured"] is False


# --- "Not found" is an answer ------------------------------------------------


async def test_a_genuine_404_on_an_anilist_id_does_not_fall_back(
    catalog: CatalogService, mal: FakeMal
) -> None:
    """AniList saying "no such id" is the answer; MAL cannot improve on it.

    Falling back here would either fail (MAL cannot look up an AniList id) or,
    worse, take an id that means a different show on MAL and return that.
    """
    with pytest.raises(SourceNotFound):
        await catalog.by_anilist_id(424242)

    assert mal.calls == []


async def test_a_404_on_a_mal_id_does_fall_back(
    catalog: CatalogService, anilist: FakeAniList, mal: FakeMal
) -> None:
    """AniList maps only some MAL ids, so its miss means "ask MAL"."""
    media = await catalog.by_mal_id(FRIEREN_MAL_ID)
    assert media is not None and media.source == "anilist"  # it happens to know this one

    anilist.media.clear()  # now it does not
    media = await catalog.by_mal_id(FRIEREN_MAL_ID)

    assert media is not None
    assert media.source == "mal"
    assert media.mal_id == FRIEREN_MAL_ID


async def test_by_anilist_id_uses_anilist_when_it_is_healthy(
    catalog: CatalogService, mal: FakeMal
) -> None:
    media = await catalog.by_anilist_id(FRIEREN_ID)

    assert media is not None
    assert media.source == "anilist"
    assert media.mal_id == FRIEREN_MAL_ID  # AniList's idMal, which is the join
    assert mal.calls == []


async def test_by_anilist_id_cannot_fall_back_because_mal_has_no_such_id(
    catalog: CatalogService, anilist: FakeAniList, mal: FakeMal, slept: list[float]
) -> None:
    """MAL answers ``None``, so the failure that is reported is AniList's."""
    anilist.disabled = True

    with pytest.raises(SourceUnavailable) as caught:
        await catalog.by_anilist_id(FRIEREN_ID)

    assert caught.value.source == "anilist"
    assert mal.calls == []  # asked in Python, never over the wire


# --- Seasons -----------------------------------------------------------------


async def test_a_season_comes_from_anilist_and_falls_back_to_mal(
    catalog: CatalogService, anilist: FakeAniList, mal: FakeMal, slept: list[float]
) -> None:
    first = await catalog.season(SEASON_YEAR, SEASON_NAME)
    assert [media.source for media in first] == ["anilist"]

    anilist.disabled = True
    second = await catalog.season(SEASON_YEAR, SEASON_NAME)

    assert len(second) > 1
    assert all(media.source == "mal" for media in second)
    assert FRIEREN_MAL_ID in {media.mal_id for media in second}


# --- The admin view ----------------------------------------------------------


async def test_status_reports_both_sources_before_anything_has_happened(
    catalog: CatalogService,
) -> None:
    status = catalog.status()

    assert set(status["sources"]) == {"anilist", "mal"}
    assert status["sources"]["anilist"] == {
        "state": "closed",
        "healthy_at": None,
        "failed_at": None,
        "reason": None,
        "configured": True,
    }
    assert status["sources"]["mal"]["configured"] is True
    assert status["active"] == "anilist"


def test_healthy_is_false_for_an_open_or_unconfigured_source() -> None:
    fake_anilist = frieren_fake()
    service = CatalogService(
        fake_anilist.source(), mal_frieren_fake().source(client_id=None), Breaker(60.0)
    )

    assert service.healthy("anilist") is True
    assert service.healthy("mal") is False  # unconfigured
    assert service.healthy("nyaa") is False  # not a source of this service

    service.breaker.record_failure("anilist", "boom")
    assert service.healthy("anilist") is False


# --- The local half of a search ---------------------------------------------
#
# The query goes into an ``ILIKE`` pattern, so what it is allowed to mean is
# part of the contract (``catalog/local.py``); the SQL itself is exercised over
# a real database in ``test_catalogue_api``.


def test_a_query_is_every_word_it_contains() -> None:
    assert terms_of("jobless  reincarnation ") == ["jobless", "reincarnation"]
    assert terms_of("   ") == []
    assert len(terms_of(" ".join(str(n) for n in range(20)))) == MAX_TERMS


def test_like_metacharacters_are_escaped_not_honoured() -> None:
    """Otherwise "100%" matches the whole table and "_" matches any letter."""
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    # The escape character itself goes first, or escaping would double-escape.
    assert escape_like("a\\b") == "a\\\\b"
    assert escape_like("frieren") == "frieren"
