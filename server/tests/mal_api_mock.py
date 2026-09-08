"""A stand-in for the *authenticated* MyAnimeList API and its OAuth endpoints.

``tests/mal_mock.py`` fakes the read-only catalogue endpoints; this fakes the
half M9 added — the token endpoint, ``users/@me``, the animelist pages, and
the two ``my_list_status`` writes — behind one ``httpx.MockTransport``.

It is deliberately a *recorder* as well as a responder. The milestone's whole
point is that Arc never writes something the user did not do, and the only way
to test that is to assert on the exact bytes that left: :attr:`FakeMalApi.patches`
holds one ``(mal_id, form)`` per ``PATCH`` with the form exactly as httpx
encoded it, and :attr:`FakeMalApi.deletes` one id per ``DELETE``. A test that
says "a watch event must not lower progress" asserts that ``patches`` contains
no ``num_watched_episodes`` — not that some helper decided not to send one.

The token endpoint rotates its tokens on every call, and requests carrying a
token that is no longer current get a 401. That makes the refresh path real
rather than mocked out: a test can expire the stored token, run a push, and
see the refresh and the retry in :attr:`FakeMalApi.token_calls`.

:attr:`FakeMalApi.probe` goes one step further, for FR-M5's strongest claim:
it is awaited *at the moment of a write*, before the response is made up, so a
test can open its own session and assert that the row the log will close is
sitting there ``pending`` right now. That is the only way to catch an unlogged
write — one where the request left and no queued row existed to record it —
because after the fact the log looks the same either way.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

import httpx

#: Where the fake lives. Tests point ``MAL_API_URL`` and ``MAL_OAUTH_URL`` here.
API_URL = "http://mal.test/v2"
OAUTH_URL = "http://mal.test/v1/oauth2"

#: The registered application, as far as the tests are concerned.
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URI = "http://localhost:8000/api/mal/callback"

#: The MAL account the fake belongs to.
MAL_USER_ID = 4242
MAL_USERNAME = "arc-tester"

#: The authorisation code a happy-path callback presents.
GOOD_CODE = "authorisation-code"


def entry(
    mal_id: int,
    *,
    title: str = "A Show",
    status: str = "watching",
    score: int = 0,
    progress: int = 0,
    updated_at: str = "2026-01-01T00:00:00+00:00",
    episodes: int = 12,
) -> dict[str, Any]:
    """One row of an ``animelist`` page, MAL's shape."""
    return {
        "node": {"id": mal_id, "title": title, "num_episodes": episodes},
        "list_status": {
            "status": status,
            "score": score,
            "num_episodes_watched": progress,
            "updated_at": updated_at,
        },
    }


class FakeMalApi:
    """The authenticated MAL API, in memory, with every write recorded."""

    def __init__(
        self,
        *,
        pages: list[list[dict[str, Any]]] | None = None,
        statuses: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        #: The animelist, one list of entries per page.
        self.pages: list[list[dict[str, Any]]] = pages if pages is not None else [[]]
        #: ``mal_id → my_list_status`` for the by-id reads a push makes first.
        self.statuses: dict[int, dict[str, Any]] = statuses or {}

        #: Every write, exactly as it left.
        self.patches: list[tuple[int, dict[str, str]]] = []
        self.deletes: list[int] = []
        #: Every token request's form.
        self.token_calls: list[dict[str, str]] = []
        #: Every authenticated request, as ``(method, path)``.
        self.calls: list[tuple[str, str]] = []

        #: Token state. The fake accepts only the newest access token it
        #: issued, so a stale one produces the 401 that drives the retry.
        self._issued = 0
        self.access_token = "access-0"
        self.refresh_token = "refresh-0"

        #: Knobs. ``refresh_fails`` makes the token endpoint refuse a refresh
        #: (the "needs relink" path); ``patch_status`` and ``list_status``
        #: make a write or a read fail with a given HTTP status.
        self.refresh_fails = False
        #: MyAnimeList rotates the refresh token on use and refuses the spent
        #: one. With this on, so does the fake — which is what turns "two jobs
        #: refreshed at once" from a wasted request into a link that wrongly
        #: says it needs re-authorising.
        self.invalidate_used_refresh = False
        self.exchange_fails = False
        self.patch_status: int | None = None
        self.list_status: int | None = None
        self.delete_status: int | None = None
        self.missing_on_delete = False

        #: Awaited with ``(method, mal_id)`` immediately before each write is
        #: recorded — see the module docstring.
        self.probe: Callable[[str, int], Awaitable[None]] | None = None

    # --- Tokens ---------------------------------------------------------

    def rotate(self) -> dict[str, Any]:
        """Issue a fresh pair and return the token response body."""
        self._issued += 1
        self.access_token = f"access-{self._issued}"
        self.refresh_token = f"refresh-{self._issued}"
        return {
            "token_type": "Bearer",
            "expires_in": 2415600,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
        }

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.token_calls.append(form)
        grant = form.get("grant_type")
        if grant == "refresh_token" and self.refresh_fails:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if (
            grant == "refresh_token"
            and self.invalidate_used_refresh
            and form.get("refresh_token") != self.refresh_token
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        if grant == "authorization_code" and self.exchange_fails:
            return httpx.Response(400, json={"error": "invalid_request"})
        return httpx.Response(200, json=self.rotate())

    # --- The API --------------------------------------------------------

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        """The transport's entry point: run any probe, then answer.

        Async only so that :attr:`probe` can be — a query against the database
        cannot be made from a synchronous handler running inside the event
        loop. Everything else is :meth:`handle`, unchanged.
        """
        if self.probe is not None:
            path = request.url.path
            if path.endswith("/my_list_status") and request.method in ("PATCH", "DELETE"):
                await self.probe(request.method, int(path.split("/")[-2]))
        return self.handle(request)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/token"):
            return self._token(request)

        if request.headers.get("Authorization") != f"Bearer {self.access_token}":
            return httpx.Response(401, json={"error": "invalid_token"})
        self.calls.append((request.method, path))

        if path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": MAL_USER_ID, "name": MAL_USERNAME})
        if path.endswith("/users/@me/animelist"):
            return self._animelist(request)
        if path.endswith("/my_list_status"):
            mal_id = int(path.split("/")[-2])
            if request.method == "PATCH":
                return self._patch(mal_id, request)
            return self._delete(mal_id)
        if "/anime/" in path:
            return self._anime(int(path.rsplit("/", 1)[-1]))
        return httpx.Response(404, json={"error": "not_found"})

    def _animelist(self, request: httpx.Request) -> httpx.Response:
        if self.list_status:
            return httpx.Response(self.list_status, json={"error": "nope"})
        # Paging is expressed as an explicit page index rather than by offset
        # arithmetic: the client follows ``paging.next``, which this builds,
        # so the fake never has to guess how big a page was.
        index = int(request.url.params.get("page") or 0)
        if index >= len(self.pages):
            return httpx.Response(200, json={"data": [], "paging": {}})
        paging: dict[str, Any] = {}
        if index + 1 < len(self.pages):
            paging["next"] = f"{API_URL}/users/@me/animelist?page={index + 1}"
        return httpx.Response(200, json={"data": self.pages[index], "paging": paging})

    def _anime(self, mal_id: int) -> httpx.Response:
        body: dict[str, Any] = {"id": mal_id, "num_episodes": 12}
        if mal_id in self.statuses:
            body["my_list_status"] = self.statuses[mal_id]
        return httpx.Response(200, json=body)

    def _patch(self, mal_id: int, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.patches.append((mal_id, form))
        if self.patch_status:
            return httpx.Response(self.patch_status, text="upstream said no")
        current = dict(self.statuses.get(mal_id) or {"status": None, "score": 0})
        if "status" in form:
            current["status"] = form["status"]
        if "score" in form:
            current["score"] = int(form["score"])
        if "num_watched_episodes" in form:
            current["num_episodes_watched"] = int(form["num_watched_episodes"])
        current["updated_at"] = "2026-06-01T00:00:00+00:00"
        self.statuses[mal_id] = current
        return httpx.Response(200, json=current)

    def _delete(self, mal_id: int) -> httpx.Response:
        self.deletes.append(mal_id)
        if self.delete_status:
            return httpx.Response(self.delete_status, text="upstream said no")
        if self.missing_on_delete or mal_id not in self.statuses:
            return httpx.Response(404, json={"error": "not_found"})
        del self.statuses[mal_id]
        return httpx.Response(200, text="")

    # --- Wiring ---------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_async)

    def install(self, monkeypatch: Any) -> FakeMalApi:
        """Point every MAL client Arc builds at this fake.

        One seam (:func:`arc.services.mal.factory.transport_for`) rather than a
        ``transport=`` argument threaded through jobs and routes that would
        never use it in production.
        """
        monkeypatch.setattr(
            "arc.services.mal.factory.transport_for", lambda settings: self.transport
        )
        return self

    def patch_form(self, mal_id: int) -> dict[str, str]:
        """The single PATCH sent for ``mal_id``; fails loudly if there was not one."""
        forms = [form for sent_id, form in self.patches if sent_id == mal_id]
        assert len(forms) == 1, f"expected exactly one PATCH for {mal_id}, got {len(forms)}"
        return forms[0]

    def dump(self) -> str:  # pragma: no cover - debugging aid
        return json.dumps({"patches": self.patches, "deletes": self.deletes}, indent=2)


__all__ = [
    "API_URL",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "GOOD_CODE",
    "MAL_USERNAME",
    "MAL_USER_ID",
    "OAUTH_URL",
    "REDIRECT_URI",
    "FakeMalApi",
    "entry",
]
