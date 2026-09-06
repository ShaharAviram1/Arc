"""A stand-in for AniList, built on ``httpx.MockTransport``.

No new dependency and no live network: the tests drive a real
:class:`AniListClient` — its pacing, its retry logic, its parsing — over a
transport that answers from the JSON in ``tests/fixtures/anilist/``.

**Where the fixtures come from.** ``search_frieren.json``,
``media_154587.json`` and ``media_999001_releasing.json`` hold AniList's exact
response shape for the queries in :mod:`arc.services.anilist.queries`.
The first two carry Frieren's real values (AniList id 154587, MAL id 52991,
28 episodes, Fall 2023, MADHOUSE, a weekly Friday slot to 2024-03-22). Note
what the schedule does *not* contain: the four-episode premiere aired as one
two-hour broadcast on 2023-09-29 and AniList publishes no per-episode slot for
it, so ``aired`` runs from episode 5 (2023-10-06) to 28. Episodes 1–4 are
estimated by :func:`arc.services.catalog.cache.sync_episodes`, not read.

The third is a synthetic
currently-airing show whose air times are anchored to :data:`FROZEN_NOW`, so
the aired / not-yet-aired boundary is a fact of the fixture rather than of the
day the suite happens to run.

The fourth, :func:`long_running`, is built in code rather than stored: it is
250 weekly episodes across three schedule pages, and every one of its values
is derived from :data:`FROZEN_NOW` and the page size, so a JSON file of it
would be six hundred lines that say the same thing less clearly.

Regenerate the two captured ones against the live API with
``scripts/capture_anilist.py`` whenever the shape of a query changes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from arc.services.anilist import AniListClient, AniListSource
from arc.services.anilist.client import SCHEDULE_PER_PAGE

FIXTURES = Path(__file__).parent / "fixtures" / "anilist"

#: Frieren, the currently-airing fixture show, and the long-running one.
FRIEREN_ID = 154587
RELEASING_ID = 999001
LONG_RUNNING_ID = 999002

#: The instant ``media_999001_releasing.json`` is written around: five of its
#: twelve episodes have aired, the sixth is due in five days.
FROZEN_NOW = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)

#: How many aired episodes :func:`long_running` has — two full schedule pages
#: and a half one, so the paging is exercised rather than merely enabled.
LONG_RUNNING_AIRED = 250


def load(name: str) -> dict[str, Any]:
    """One captured response body."""
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return payload


def media_payload(name: str) -> dict[str, Any]:
    """The ``Media`` object inside a captured by-id response."""
    media: dict[str, Any] = load(name)["data"]["Media"]
    return media


#: ``{"data": {"Media": null}}`` — what AniList answers for an id it does not
#: have. Not a 404: the query itself succeeded.
NULL_MEDIA: dict[str, Any] = {"data": {"Media": None}}

#: What AniList answered for every query during the outage that motivated the
#: MAL fallback: HTTP 403 with a GraphQL error rather than a transport failure,
#: which is why it needs its own detection (FR-C6).
DISABLED_STATUS = 403
DISABLED_BODY: dict[str, Any] = {
    "errors": [
        {
            "message": "The AniList API is temporarily disabled. Please try again later.",
            "status": 403,
        }
    ],
    "data": None,
}


class FakeAniList:
    """Routes the three Arc queries to canned responses and counts requests.

    ``media`` maps AniList id → response body (or a callable returning one, so
    a test can change the answer between calls). ``search`` does the same for
    the search term, lower-cased. ``schedule`` maps an id to its aired schedule
    split into pages of :data:`SCHEDULE_PER_PAGE` nodes — page 1 included, so
    that one list describes the whole show and the fake decides which slice
    goes in the by-id response and which needs a follow-up query. Anything not
    in a map gets :data:`NULL_MEDIA` / an empty page, which is what "AniList
    does not know this" looks like on the wire.
    """

    def __init__(
        self,
        *,
        media: dict[int, dict[str, Any] | Callable[[], dict[str, Any]]] | None = None,
        search: dict[str, dict[str, Any]] | None = None,
        schedule: dict[int, list[list[dict[str, Any]]]] | None = None,
        seasons: dict[tuple[int, str], list[dict[str, Any]]] | None = None,
        disabled: bool = False,
    ) -> None:
        self.media = media or {}
        self.search = search or {}
        self.schedule = schedule or {}
        #: ``(year, SEASON)`` → the ``Media`` objects of that season.
        self.seasons = seasons or {}
        #: When true every query answers 403 "temporarily disabled", which is
        #: exactly what the live API did all day on 2026-09-06. Flip it back to
        #: false mid-test to play AniList coming back.
        self.disabled = disabled
        #: One entry per request the client actually sent, as
        #: ``(operation, variables)``. Retries show up as repeats.
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def by_mal(self) -> dict[int, int]:
        """MAL id → AniList id, read off whatever ``media`` holds."""
        found: dict[int, int] = {}
        for anilist_id, entry in self.media.items():
            payload = entry() if callable(entry) else entry
            raw = ((payload or {}).get("data") or {}).get("Media") or {}
            if raw.get("idMal") is not None:
                found[int(raw["idMal"])] = anilist_id
        return found

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body.get("variables") or {}
        if self.disabled:
            self.calls.append(("disabled", variables))
            return httpx.Response(DISABLED_STATUS, json=DISABLED_BODY)
        if "seasonYear" in variables:
            self.calls.append(("season", variables))
            return httpx.Response(200, json=self._season_page(variables))
        if "idMal" in variables:
            self.calls.append(("media.by_mal", variables))
            anilist_id = self.by_mal.get(int(variables["idMal"]))
            entry = self.media.get(anilist_id) if anilist_id is not None else None
            payload = entry() if callable(entry) else entry
            return httpx.Response(200, json=payload if payload is not None else NULL_MEDIA)
        if "search" in variables:
            self.calls.append(("search", variables))
            term = str(variables["search"]).lower()
            payload = self.search.get(term)
            if payload is None:
                payload = {
                    "data": {
                        "Page": {
                            "pageInfo": {
                                "currentPage": variables.get("page", 1),
                                "hasNextPage": False,
                            },
                            "media": [],
                        }
                    }
                }
            return httpx.Response(200, json=payload)

        # The follow-up schedule query is the only one that names both an id
        # and a page; the by-id query names an id alone.
        if "page" in variables:
            self.calls.append(("schedule", variables))
            return httpx.Response(200, json=self._schedule_page(variables))

        self.calls.append(("media", variables))
        entry = self.media.get(int(variables["id"]))
        payload = entry() if callable(entry) else entry
        return httpx.Response(200, json=payload if payload is not None else NULL_MEDIA)

    def _season_page(self, variables: dict[str, Any]) -> dict[str, Any]:
        """One page of a season. The fixture seasons all fit in one page."""
        key = (int(variables["seasonYear"]), str(variables["season"]).upper())
        nodes = self.seasons.get(key) or []
        return {
            "data": {
                "Page": {
                    "pageInfo": {"currentPage": variables.get("page", 1), "hasNextPage": False},
                    "media": nodes if int(variables.get("page", 1)) == 1 else [],
                }
            }
        }

    def _schedule_page(self, variables: dict[str, Any]) -> dict[str, Any]:
        anime_id = int(variables["id"])
        page = int(variables["page"])
        pages = self.schedule.get(anime_id) or []
        nodes = pages[page - 1] if 1 <= page <= len(pages) else []
        return {
            "data": {
                "Media": {
                    "id": anime_id,
                    "aired": {
                        "pageInfo": {"currentPage": page, "hasNextPage": page < len(pages)},
                        "nodes": nodes,
                    },
                }
            }
        }

    def client(self) -> AniListClient:
        """A real :class:`AniListClient` wired to this fake.

        ``min_interval=0``: the pacing is tested on its own, and paying 700 ms
        per request would add minutes to the suite.
        """
        return AniListClient(
            url="http://anilist.test/graphql",
            min_interval=0.0,
            transport=httpx.MockTransport(self.handle),
        )

    def source(self) -> AniListSource:
        """The same fake behind the :class:`CatalogSource` interface."""
        return AniListSource(self.client())


def frieren_fake() -> FakeAniList:
    """The common case: Frieren by id, and "frieren" as a search term."""
    return FakeAniList(
        media={FRIEREN_ID: load("media_154587")},
        search={"frieren": load("search_frieren")},
    )


def _schedule_pages(count: int, *, ending: datetime) -> list[list[dict[str, Any]]]:
    """``count`` weekly episodes, last one just before ``ending``, in pages."""
    nodes = [
        {
            "episode": number,
            "airingAt": int((ending - timedelta(weeks=count - number + 1)).timestamp()),
        }
        for number in range(1, count + 1)
    ]
    return [
        nodes[start : start + SCHEDULE_PER_PAGE]
        for start in range(0, len(nodes), SCHEDULE_PER_PAGE)
    ]


def long_running_fake(*, aired: int = LONG_RUNNING_AIRED) -> FakeAniList:
    """A show whose back catalogue does not fit in one schedule page.

    ``episodes: null`` and ``status: RELEASING`` — the shape that made the
    truncation visible: with only the first page fetched, everything past
    episode 100 had no air time, and an episode with no air time on a
    releasing show used to read as "not aired yet".

    ``upcoming`` is deliberately empty while ``nextAiringEpisode`` names
    episode ``aired + 1``, which is exactly what AniList returns in the hours
    around a broadcast. That episode therefore has no air time of its own and
    is the one row the boundary rule has to place *after* the line rather than
    before it.
    """
    pages = _schedule_pages(aired, ending=FROZEN_NOW)
    payload = {
        "data": {
            "Media": {
                "id": LONG_RUNNING_ID,
                "idMal": 999002,
                "title": {
                    "romaji": "Nagai Monogatari",
                    "english": "The Long One",
                    "native": "長い物語",
                },
                "format": "TV",
                "episodes": None,
                "status": "RELEASING",
                "season": "SPRING",
                "seasonYear": 2021,
                "coverImage": {"extraLarge": "https://img.test/long.jpg", "large": None},
                "bannerImage": None,
                "description": "It has been going on for a while.",
                "genres": ["Adventure"],
                "synonyms": [],
                "tags": [],
                "studios": {"nodes": [{"name": "Studio Endless"}]},
                "nextAiringEpisode": {
                    "episode": aired + 1,
                    "airingAt": int((FROZEN_NOW + timedelta(days=5)).timestamp()),
                    "timeUntilAiring": 432000,
                },
                "relations": {"edges": []},
                "aired": {
                    "pageInfo": {"currentPage": 1, "hasNextPage": len(pages) > 1},
                    "nodes": pages[0] if pages else [],
                },
                "upcoming": {"nodes": []},
            }
        }
    }
    return FakeAniList(media={LONG_RUNNING_ID: payload}, schedule={LONG_RUNNING_ID: pages})


def summary_of(payload: dict[str, Any]) -> dict[str, Any]:
    """The summary half of a captured detail response.

    A season list and a search page carry only the fragment's fields, so a test
    that needs one from a fixture must trim it rather than hand the whole
    ``Media`` object over — otherwise the fake would answer a summary query
    with relations and a synopsis, and the "a summary must not overwrite a
    detail fetch" rule would never be exercised against it.
    """
    return {
        key: payload[key]
        for key in (
            "id",
            "idMal",
            "title",
            "format",
            "episodes",
            "status",
            "season",
            "seasonYear",
            "coverImage",
        )
        if key in payload
    }


__all__ = [
    "DISABLED_BODY",
    "DISABLED_STATUS",
    "FIXTURES",
    "FRIEREN_ID",
    "FROZEN_NOW",
    "LONG_RUNNING_AIRED",
    "LONG_RUNNING_ID",
    "NULL_MEDIA",
    "RELEASING_ID",
    "FakeAniList",
    "frieren_fake",
    "load",
    "long_running_fake",
    "media_payload",
    "summary_of",
]
