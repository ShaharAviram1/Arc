"""A stand-in for the MyAnimeList API, built on ``httpx.MockTransport``.

The same shape as ``tests/anilist_mock.py`` and for the same reason: the tests
drive a real :class:`MalSource` — its header, its retry, its parsing — over a
transport that answers from the JSON in ``tests/fixtures/mal/``.

**Where the fixtures come from.** All three were captured from the live API
with ``scripts/capture_mal.py`` and trimmed to a handful of entries:

* ``search_frieren.json`` — ``GET /v2/anime?q=frieren``, first five hits.
* ``anime_52991.json`` — ``GET /v2/anime/52991`` with every field Arc asks
  for. Frieren: 28 episodes, ``finished_airing``, first aired 2023-09-29,
  broadcast Friday 23:00 JST, Madhouse.
* ``season_2023_fall.json`` — ``GET /v2/anime/season/2023/fall``, Frieren plus
  four of its season-mates.

``paging`` is emptied in the captured files so the fixtures describe a single
page; the fake sets it per request when a test asks for more.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from arc.services.mal.catalog import MalSource

FIXTURES = Path(__file__).parent / "fixtures" / "mal"

#: Frieren, as MyAnimeList and AniList respectively know it. The pair is the
#: whole point of the reconciliation tests: one show, two ids.
FRIEREN_MAL_ID = 52991
FRIEREN_ANILIST_ID = 154587

#: The season the fixture covers.
SEASON_YEAR = 2023
SEASON_NAME = "FALL"

#: MAL's client id header. Its *value* never matters to the fake — the tests
#: must not need a real registration — but its presence does: forgetting to
#: send it is exactly the bug this asserts against.
CLIENT_ID_HEADER = "X-MAL-CLIENT-ID"
TEST_CLIENT_ID = "test-client-id"

#: What MAL answers for an anime id it does not have.
NOT_FOUND: dict[str, Any] = {"error": "not_found", "message": ""}


def load(name: str) -> dict[str, Any]:
    """One captured response body."""
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return payload


def anime_payload(name: str = "anime_52991") -> dict[str, Any]:
    """A captured by-id response, which is the anime object itself."""
    return load(name)


class FakeMal:
    """Routes the three MAL endpoints to canned responses and counts requests.

    ``anime`` maps MAL id → response body, ``search`` maps a lower-cased query
    term → a search page, and ``seasons`` maps ``(year, season)`` → a season
    page. Anything not in a map is a 404 (for a by-id lookup) or an empty page,
    which is what "MAL does not know this" looks like on the wire.

    ``fail_with`` short-circuits every request with one status code, so an
    outage is one line in a test rather than a bespoke transport.
    """

    def __init__(
        self,
        *,
        anime: dict[int, dict[str, Any]] | None = None,
        search: dict[str, dict[str, Any]] | None = None,
        seasons: dict[tuple[int, str], dict[str, Any]] | None = None,
        fail_with: int | None = None,
    ) -> None:
        self.anime = anime or {}
        self.search = search or {}
        self.seasons = seasons or {}
        self.fail_with = fail_with
        #: One entry per request, as ``(operation, path, query)``.
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        #: Requests that arrived without the client-id header.
        self.unauthenticated = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        if not request.headers.get(CLIENT_ID_HEADER):
            self.unauthenticated += 1

        if path.endswith("/anime") and "q" in query:
            self.calls.append(("search", path, query))
            if self.fail_with:
                return httpx.Response(self.fail_with, json={})
            return httpx.Response(200, json=self.search.get(query["q"].lower()) or _empty_page())

        if "/anime/season/" in path:
            self.calls.append(("season", path, query))
            if self.fail_with:
                return httpx.Response(self.fail_with, json={})
            year, season = path.rsplit("/", 2)[-2:]
            payload = self.seasons.get((int(year), season.upper()))
            return httpx.Response(200, json=payload or _empty_page())

        self.calls.append(("anime", path, query))
        if self.fail_with:
            return httpx.Response(self.fail_with, json={})
        anime_id = int(path.rsplit("/", 1)[-1])
        payload = self.anime.get(anime_id)
        if payload is None:
            return httpx.Response(404, json=NOT_FOUND)
        return httpx.Response(200, json=payload)

    def source(self, *, client_id: str | None = TEST_CLIENT_ID) -> MalSource:
        """A real :class:`MalSource` wired to this fake."""
        return MalSource(
            url="http://mal.test/v2",
            client_id=client_id,
            transport=httpx.MockTransport(self.handle),
        )


def _empty_page() -> dict[str, Any]:
    return {"data": [], "paging": {}}


def frieren_fake() -> FakeMal:
    """The common case: Frieren by id, by search, and in its season."""
    return FakeMal(
        anime={FRIEREN_MAL_ID: load("anime_52991")},
        search={"frieren": load("search_frieren")},
        seasons={(SEASON_YEAR, SEASON_NAME): load("season_2023_fall")},
    )


__all__ = [
    "CLIENT_ID_HEADER",
    "FIXTURES",
    "FRIEREN_ANILIST_ID",
    "FRIEREN_MAL_ID",
    "NOT_FOUND",
    "SEASON_NAME",
    "SEASON_YEAR",
    "TEST_CLIENT_ID",
    "FakeMal",
    "anime_payload",
    "frieren_fake",
    "load",
]
