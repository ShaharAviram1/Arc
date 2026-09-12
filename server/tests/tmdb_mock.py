"""A stand-in for TMDB, over the recorded fixtures.

``tests/fixtures/tmdb/*.json`` are real responses, captured by
``scripts/capture_tmdb.py`` and trimmed there (five episodes, mapped crew plus
a little noise, no cast). Recorded rather than invented for the reason
architecture.md §10 gives for every other fixture in this suite: a payload
written from the documentation agrees with the parser by construction and with
TMDB by luck.

:class:`FakeTmdb` routes by path, so a test drives a real
:class:`~arc.services.tmdb.client.TmdbClient` over ``httpx.MockTransport`` and
everything except the socket is the production path. It records every request
it answers (:attr:`FakeTmdb.calls`), which is how the tests assert that a film
never asks for a season and that an enrichment costs three requests and not
six.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tmdb"

#: Frieren, as ``offline_ids`` maps it.
FRIEREN_TV_ID = 209867
FRIEREN_SEASON = 1
FRIEREN_MAL_ID = 52991
FRIEREN_ANILIST_ID = 154587

#: "A Silent Voice", the recorded film.
FILM_ID = 378064

#: A URL for the client to talk to. Cosmetic: the mock transport answers
#: whatever host it is given.
URL = "http://tmdb.test/3"

#: Any non-empty string works as a key against the fake, and using an obviously
#: fake one keeps a real key from ever reaching a test.
API_KEY = "test-tmdb-key"


def load(name: str) -> dict[str, Any]:
    """One recorded response by file name (without ``.json``)."""
    payload: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return payload


def frieren_show() -> dict[str, Any]:
    return load(f"tv_{FRIEREN_TV_ID}")


def frieren_season() -> dict[str, Any]:
    return load(f"tv_{FRIEREN_TV_ID}_season_{FRIEREN_SEASON}")


def frieren_credits() -> dict[str, Any]:
    return load(f"tv_{FRIEREN_TV_ID}_aggregate_credits")


def film_movie() -> dict[str, Any]:
    return load(f"movie_{FILM_ID}")


def film_credits() -> dict[str, Any]:
    return load(f"movie_{FILM_ID}_credits")


class FakeTmdb:
    """The recorded responses, served over ``httpx.MockTransport``.

    ``status`` overrides one path with a bare status code — that is how the
    rate-limit and outage tests are written, and ``responses`` how a test
    supplies a payload of its own (a season list with three cours, say).
    """

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {
            f"/tv/{FRIEREN_TV_ID}": frieren_show(),
            f"/tv/{FRIEREN_TV_ID}/season/{FRIEREN_SEASON}": frieren_season(),
            f"/tv/{FRIEREN_TV_ID}/aggregate_credits": frieren_credits(),
            f"/movie/{FILM_ID}": film_movie(),
            f"/movie/{FILM_ID}/credits": film_credits(),
        }
        #: ``path`` → the status to answer with instead of a payload.
        self.status: dict[str, int] = {}
        #: How many statuses to serve before falling back to the payload, per
        #: path. ``None`` means "for ever".
        self.status_times: dict[str, int | None] = {}
        #: Every path asked for, in order.
        self.calls: list[str] = []
        #: Every ``Retry-After`` value to send with a 429.
        self.retry_after: str | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # The client is built with a base of ``/3``; the fixtures are keyed on
        # the part after it, which is what the production URL carries too.
        for prefix in ("/3",):
            if path.startswith(prefix):
                path = path[len(prefix) :]
        self.calls.append(path)

        status = self.status.get(path)
        if status is not None:
            remaining = self.status_times.get(path)
            if remaining is None or remaining > 0:
                if remaining is not None:
                    self.status_times[path] = remaining - 1
                headers = {}
                if status == 429 and self.retry_after is not None:
                    headers["Retry-After"] = self.retry_after
                return httpx.Response(status, json={"status_message": "no"}, headers=headers)

        payload = self.responses.get(path)
        if payload is None:
            return httpx.Response(404, json={"status_code": 34, "status_message": "Not found."})
        return httpx.Response(200, json=payload)


__all__ = [
    "API_KEY",
    "FILM_ID",
    "FRIEREN_ANILIST_ID",
    "FRIEREN_MAL_ID",
    "FRIEREN_SEASON",
    "FRIEREN_TV_ID",
    "URL",
    "FakeTmdb",
    "film_credits",
    "film_movie",
    "frieren_credits",
    "frieren_season",
    "frieren_show",
    "load",
]
