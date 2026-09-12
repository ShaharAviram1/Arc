"""HTTP client for the TMDB API (architecture.md §5.8, §6).

TMDB is Arc's *third* catalogue source and the only one that is never asked
about a title: it is reached by id, through the offline cross-id map, and it
answers with the three things AniList sometimes has not got — a backdrop, a
poster at key-art size, and a still per episode (FR-C6, M15.5).

**Politeness.** TMDB's published ceiling is around 50 requests a second, which
is not a limit Arc could reach if it tried: an enrichment is three requests and
they are queued one show at a time. The client still paces itself at
:data:`MIN_INTERVAL` (4 req/s) because the nightly sweep is the one thing that
*could* — two hundred shows back to back — and a fixed gap is cheaper to reason
about than a token bucket nothing will ever empty.

**Failures.** A 5xx or a transport error is retried twice with exponential
backoff; a 429 is slept off once for its ``Retry-After``. Past that the
source's breaker is opened (:mod:`arc.services.catalog.breaker`, the same class
the live catalogue uses) so that the rest of a sweep is skipped without calling
rather than timing out once per show. A 404 is a :class:`TmdbNotFound` and is
final — the id map is somebody else's file and does go stale.

The breaker is **process-wide** by default, unlike the catalogue's per-job one:
enrichment runs as one job per show, so a per-client breaker would be
rediscovered from scratch two hundred times a night and would never skip
anything.

Image paths are relative (``/rBOnr….jpg``); the base and the size are the
caller's choice, and Arc's are :data:`BACKDROP_SIZE`, :data:`POSTER_SIZE` and
:data:`STILL_SIZE` below. They are hardcoded rather than read from
``/configuration`` because that endpoint's answer has not changed in a decade
and asking it would be one more request per sweep to learn a constant.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType
from typing import Any, Self

import httpx

from arc.config import Settings
from arc.services.catalog.breaker import Breaker

log = logging.getLogger(__name__)

#: The v3 API. A setting would be one more knob for a service with no mirrors;
#: the tests reach it through ``httpx.MockTransport``, which does not care what
#: the URL says.
TMDB_API_URL = "https://api.themoviedb.org/3"

#: Where TMDB serves the images whose *paths* the JSON carries.
IMAGE_BASE = "https://image.tmdb.org/t/p/"

#: The sizes Arc asks for, one per column it fills. ``banner_url`` is rendered
#: full-bleed behind the show page's title block, ``cover_large_url`` at 172 px
#: on a shelf and wider in the browse grid, and a still at 16:9 on an episode
#: row — so a backdrop is worth 1280 px, a poster 780, and a still 300.
BACKDROP_SIZE = "w1280"
POSTER_SIZE = "w780"
STILL_SIZE = "w300"

#: The name the breaker knows this source by, and the one that appears in the
#: log when it opens.
SOURCE = "tmdb"

#: How long a single request may take.
TIMEOUT_SECONDS = 15.0

#: Minimum gap between two requests: four a second, well inside TMDB's fifty.
MIN_INTERVAL = 0.25

#: Attempts for a 5xx / transport failure: the first try plus two retries.
SERVER_ERROR_ATTEMPTS = 3

#: Base of the 5xx backoff, doubled per attempt (1 s, 2 s).
BACKOFF_BASE = 1.0

#: How long to wait on a 429 that carries no ``Retry-After``, and the longest
#: one that will be honoured. TMDB sends single-digit values when it sends one
#: at all; anything past the cap is an outage, not a pause.
DEFAULT_RETRY_AFTER = 2.0
MAX_RETRY_AFTER = 30.0

#: The language Arc asks for: English episode titles and English overviews
#: where TMDB has them.
#:
#: Deliberately *not* accompanied by ``include_image_language``. That parameter
#: only widens the ``images`` block, which Arc never asks for — the backdrop,
#: poster and stills it reads are the ``*_path`` fields on the main object, and
#: those are already the entry's primary artwork whatever language it is
#: tagged with. Sending it here would be a parameter that changes nothing.
LANGUAGE = "en-US"

#: One breaker for the process (see the module docstring). Reset by the tests
#: through :func:`reset_breaker`.
_BREAKER = Breaker()


def reset_breaker() -> None:
    """Forget that TMDB was ever down. For tests and for a worker restart."""
    _BREAKER.reset()


def breaker() -> Breaker:
    """The process-wide TMDB breaker, for a caller that wants its state."""
    return _BREAKER


async def _sleep(seconds: float) -> None:
    """Indirection so a test can skip a backoff without touching ``asyncio``."""
    await asyncio.sleep(seconds)


class TmdbError(RuntimeError):
    """TMDB could not be reached, or answered with an error."""


class TmdbNotFound(TmdbError):
    """TMDB has no such id.

    Separate because the caller acts on it: the cross-id map is a weekly
    snapshot of somebody else's file, and an id that has been merged away is a
    line in the log rather than a job that should be retried.
    """


class TmdbUnavailable(TmdbError):
    """TMDB is failing, or its breaker is open and it is not being called."""


def image_url(path: str | None, size: str) -> str | None:
    """An absolute image URL from one of TMDB's relative paths.

    ``None`` for a missing path *and* for anything that is not a string, so
    that a field TMDB has left null never becomes the literal URL
    ``https://image.tmdb.org/t/p/w1280None``.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    return f"{IMAGE_BASE}{size}{path.strip()}"


class TmdbClient:
    """A paced, retrying JSON client for the five endpoints Arc reads.

    Owns an :class:`httpx.AsyncClient`; close it with :meth:`aclose` or use the
    object as an async context manager.
    """

    def __init__(
        self,
        api_key: str,
        *,
        url: str = TMDB_API_URL,
        min_interval: float = MIN_INTERVAL,
        timeout: float = TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        breaker: Breaker | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.min_interval = max(min_interval, 0.0)
        self.breaker = breaker if breaker is not None else _BREAKER
        self._key = api_key
        self._pace_lock = asyncio.Lock()
        #: Monotonic time before which the next request must not be sent.
        self._next_at = 0.0
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "arc/0.1 (self-hosted anime server)",
            },
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build a client from configuration. Raises when there is no key.

        The callers check :attr:`Settings.tmdb_api_key` first and skip the work
        entirely when it is unset; this raise is the backstop for a code path
        that forgets, because a TMDB request without a key is a 401 loop.
        """
        key = (settings.tmdb_api_key or "").strip()
        if not key:
            raise TmdbError("TMDB_API_KEY is not set")
        return cls(key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- Pacing ---

    async def _wait_turn(self) -> None:
        """Block until this request is allowed to go out."""
        async with self._pace_lock:
            delay = self._next_at - time.monotonic()
            if delay > 0:
                await _sleep(delay)
            self._next_at = time.monotonic() + self.min_interval

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return DEFAULT_RETRY_AFTER
        try:
            seconds = float(raw)
        except ValueError:
            return DEFAULT_RETRY_AFTER
        return min(max(seconds, 0.0), MAX_RETRY_AFTER)

    def _fail(self, reason: str) -> TmdbUnavailable:
        """Open the breaker and return the exception to raise."""
        if self.breaker.record_failure(SOURCE, reason):
            log.warning("tmdb is unavailable; skipping it for now", extra={"reason": reason})
        return TmdbUnavailable(f"tmdb: {reason}")

    # --- The one request method ---

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        """``GET {url}{path}`` with the key attached, decoded as an object.

        The key travels as ``api_key`` in the query string — TMDB's v3 scheme,
        and the one a free key from ``themoviedb.org/settings/api`` works with.
        It is never logged: the log line carries the path and the status.
        """
        if self.breaker.is_open(SOURCE):
            raise TmdbUnavailable(f"tmdb: breaker is open ({self.breaker.state(SOURCE).reason})")

        query: dict[str, Any] = {"api_key": self._key, **params}
        started = time.monotonic()
        attempt = 0
        rate_limited_once = False

        while True:
            attempt += 1
            await self._wait_turn()
            try:
                response = await self._http.get(f"{self.url}{path}", params=query)
            except httpx.HTTPError as exc:
                reason = f"{type(exc).__name__}: {exc}"
                if attempt < SERVER_ERROR_ATTEMPTS:
                    await _sleep(BACKOFF_BASE * 2 ** (attempt - 1))
                    continue
                raise self._fail(reason) from exc

            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                if rate_limited_once:
                    raise self._fail("rate limited twice")
                rate_limited_once = True
                pause = self._retry_after(response)
                log.warning("tmdb rate limited", extra={"path": path, "retry_after_s": pause})
                await _sleep(pause)
                continue

            if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                if attempt < SERVER_ERROR_ATTEMPTS:
                    await _sleep(BACKOFF_BASE * 2 ** (attempt - 1))
                    continue
                raise self._fail(f"HTTP {response.status_code}")

            if response.status_code == httpx.codes.NOT_FOUND:
                # Answering is not failing: the breaker stays closed and the
                # caller skips this one show.
                self.breaker.record_success(SOURCE)
                raise TmdbNotFound(f"tmdb has nothing at {path}")

            if response.status_code >= httpx.codes.BAD_REQUEST:
                # A 401 (bad key) or a 422 is a configuration problem, not a
                # transient one, and every following request would fail the
                # same way — so it opens the breaker too.
                raise self._fail(f"HTTP {response.status_code}")

            payload = self._decode(response, path)
            self.breaker.record_success(SOURCE)
            log.info(
                "tmdb request",
                extra={
                    "path": path,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "attempts": attempt,
                    "status": response.status_code,
                },
            )
            return payload

    @staticmethod
    def _decode(response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise TmdbError(f"tmdb {path}: HTTP {response.status_code} with a non-JSON body") from (
                exc
            )
        if not isinstance(payload, dict):
            raise TmdbError(f"tmdb {path}: response was not a JSON object")
        return payload

    # --- Typed wrappers ---

    async def tv(self, tv_id: int) -> dict[str, Any]:
        """One series: ``backdrop_path``, ``poster_path``, ``seasons``, ``name``."""
        return await self.get(f"/tv/{tv_id}", language=LANGUAGE)

    async def tv_season(self, tv_id: int, season: int) -> dict[str, Any]:
        """One season, with an ``episodes`` list carrying names and stills."""
        return await self.get(f"/tv/{tv_id}/season/{season}", language=LANGUAGE)

    async def tv_credits(self, tv_id: int) -> dict[str, Any]:
        """The series' crew, aggregated across every episode.

        ``aggregate_credits`` rather than ``credits``: the flat endpoint lists
        whoever happens to be attached to the series record, which for anime is
        usually a handful of producers, while the aggregate carries the whole
        crew with an episode count per job — and that count is how the series
        director is told apart from the twenty people credited as "Director" on
        two episodes each (:func:`arc.services.tmdb.enrich.crew_credits`).
        """
        return await self.get(f"/tv/{tv_id}/aggregate_credits", language=LANGUAGE)

    async def movie(self, movie_id: int) -> dict[str, Any]:
        """One film. Same art fields as a series, and no seasons."""
        return await self.get(f"/movie/{movie_id}", language=LANGUAGE)

    async def movie_credits(self, movie_id: int) -> dict[str, Any]:
        """A film's crew: one flat ``job`` per member rather than a list."""
        return await self.get(f"/movie/{movie_id}/credits", language=LANGUAGE)


__all__ = [
    "BACKDROP_SIZE",
    "DEFAULT_RETRY_AFTER",
    "IMAGE_BASE",
    "LANGUAGE",
    "MAX_RETRY_AFTER",
    "MIN_INTERVAL",
    "POSTER_SIZE",
    "SOURCE",
    "STILL_SIZE",
    "TIMEOUT_SECONDS",
    "TMDB_API_URL",
    "TmdbClient",
    "TmdbError",
    "TmdbNotFound",
    "TmdbUnavailable",
    "breaker",
    "image_url",
    "reset_breaker",
]
