"""HTTP client for the AniList GraphQL API (architecture.md §6).

One public method, :meth:`AniListClient.query`, plus four typed wrappers around
it (:meth:`search`, :meth:`media`, :meth:`media_by_mal_id`, :meth:`season`).
Everything above this module works with the source-neutral dataclasses in
:mod:`arc.services.catalog.source` and never sees a raw AniList dict — which is
what lets :class:`arc.services.mal.MalSource` stand in for this one (FR-C6).

**Politeness.** AniList documents 90 requests/minute but currently enforces
30, and answers an overrun with 429 plus ``Retry-After``. Arc therefore does
three things rather than trusting the documented number: at most
``concurrency`` requests are in flight (a semaphore), consecutive requests are
spaced by ``min_interval`` (default 700 ms — comfortably under 30/min even at
full tilt), and the ``X-RateLimit-Remaining`` header is read on every response
so that a budget running low widens the spacing before the 429 arrives.

**Retries.** A 429 is slept off once, for the ``Retry-After`` AniList sent,
capped at 60 s. When it sends no header at all the wait is
:data:`DEFAULT_RETRY_AFTER`. A 5xx or a transport error is retried twice with
exponential backoff. Everything else — a GraphQL error, a 4xx — is final;
there is nothing to gain by asking again.

**Who may wait.** Sleeping off a 429 is right for a background job, which has
nowhere else to go and all night to get there. It is wrong for a user's search
or show page, which has somewhere else to go — MAL, then the offline
catalogue — and had to sit through three seconds to find out. So
``wait_on_rate_limit=False`` turns a 429 into :class:`AniListRateLimited`
immediately, and records ``Retry-After`` as a per-client
:attr:`AniListClient.rate_limited_until`: within that window the next
interactive call fails without a request at all, rather than spending Arc's
next slot on a request AniList has already said it will refuse. The window is
seconds long and per process, which is why it is a plain timestamp and not the
breaker — a burst limit is a moment, not an outage, and opening the breaker
for five minutes would take AniList away from every other caller over one
noisy keystroke.

Logging records the operation name and the duration, never the response body:
a media response is several kilobytes of synopsis and would drown the log.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from types import TracebackType
from typing import Any, Self

import httpx

from arc.config import Settings
from arc.services.anilist.extras import parse_streaming_episodes, staff_credits
from arc.services.anilist.queries import (
    AIRED_SCHEDULE_PAGE,
    MEDIA_BY_ID,
    MEDIA_BY_MAL_ID,
    SEARCH,
    SEASON,
)
from arc.services.catalog.credits import credits_from
from arc.services.catalog.source import (
    AiringEntry,
    CatalogMedia,
    MediaRelation,
    MediaTitle,
    SearchPage,
)

log = logging.getLogger(__name__)

#: The public endpoint. Overridden by ``ANILIST_URL`` (tests point it at a
#: mock transport, so the value there is cosmetic).
ANILIST_URL = "https://graphql.anilist.co"

#: How long a single request may take. AniList is usually well under a second;
#: 15 s is "something is wrong" rather than "be patient".
TIMEOUT_SECONDS = 15.0

#: Requests in flight at once.
CONCURRENCY = 2

#: Longest a 429's ``Retry-After`` will be honoured before giving up. AniList
#: sends 60 at most; a larger value would be a bug or an outage, and blocking
#: a request handler on it is worse than failing.
MAX_RETRY_AFTER = 60.0

#: How long to wait on a 429 that carries no ``Retry-After`` (or an unparseable
#: one). A ceiling is the wrong default for a missing header: nothing said a
#: minute was needed, and a search request that hangs for 60 s is a broken
#: page, whereas 3 s is a pause the user barely notices. The cap above still
#: applies to a header that *was* sent.
DEFAULT_RETRY_AFTER = 3.0

#: Attempts for a 5xx / transport failure: the first try plus two retries.
SERVER_ERROR_ATTEMPTS = 3

#: Base of the 5xx backoff, doubled per attempt (1 s, 2 s).
BACKOFF_BASE = 1.0

#: When the remaining budget drops to this, start spacing requests out by
#: :data:`LOW_BUDGET_PAUSE` instead of the configured minimum.
LOW_BUDGET_THRESHOLD = 5
LOW_BUDGET_PAUSE = 5.0

#: AniList's per-page ceiling for ``Page``; also what search asks for.
SEARCH_PER_PAGE = 20

#: ``airingSchedule`` pages at 100 whatever ``perPage`` asks for.
SCHEDULE_PER_PAGE = 100

#: What ``Page`` asks for when listing a season, and how many pages of it a
#: single :meth:`AniListClient.season` will follow. 4 x 50 is the two hundred
#: most popular shows of a season, which is more than any schedule page shows.
SEASON_PER_PAGE = 50
MAX_SEASON_PAGES = 4

#: How many pages of aired schedule a single :meth:`AniListClient.media` will
#: follow, page 1 included. 20 × 100 covers every show in existence bar the
#: three or four that have been running since the nineties; past that the cost
#: of a refresh stops being proportionate to what a show page renders, so the
#: fetch stops and says so in the log.
MAX_SCHEDULE_PAGES = 20


async def _sleep(seconds: float) -> None:
    """Indirection so a test can skip a backoff without touching ``asyncio``.

    Everything this module waits on — the pacing gap between requests and
    both backoffs — goes through here, so a test can make the client's whole
    sense of time free without patching ``asyncio.sleep`` for the event loop
    it is running on.
    """
    await asyncio.sleep(seconds)


class AniListError(RuntimeError):
    """AniList could not be reached, or answered with an error."""


class AniListNotFound(AniListError):
    """AniList has no media with that id.

    Separate from :class:`AniListError` because the callers act on it: a
    missing id is a 404 to the user, while a transport failure is a 502 or a
    fall back to whatever is cached.
    """


class AniListDisabled(AniListError):
    """AniList has switched its API off for everyone.

    It answers 403 with a GraphQL error saying so — which is neither a
    transport failure nor a rate limit, and retrying it is pointless. It is
    the outage that motivated the MAL fallback in the first place (FR-C6), so
    it gets a class of its own and :class:`AniListSource` turns it into a
    ``SourceUnavailable`` with a reason a human can read.
    """


class AniListRateLimited(AniListError):
    """The minute's budget is spent, and this caller is not waiting for it.

    Only ever raised by a client built with ``wait_on_rate_limit=False`` — the
    interactive one. A class of its own because the answer above is different
    in kind: a rate limit is a source that is up and will answer again in a few
    seconds, so :class:`~arc.services.anilist.source.AniListSource` turns it
    into a :class:`~arc.services.catalog.source.SourceRateLimited`, which falls
    back to MAL exactly like any other unavailability and, unlike one, leaves
    the breaker closed.
    """

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message)
        #: What AniList asked for, in seconds, for whoever wants to log it.
        self.retry_after = retry_after


#: Substrings that mark a GraphQL error as "the whole API is off", not "your
#: query was wrong". Matched case-insensitively against the joined messages.
DISABLED_MARKERS = ("temporarily disabled", "temporarily unavailable")


# --- Parsing ----------------------------------------------------------------


def _title(raw: dict[str, Any] | None) -> MediaTitle:
    raw = raw or {}
    return MediaTitle(
        romaji=raw.get("romaji"),
        english=raw.get("english"),
        native=raw.get("native"),
    )


def _cover(raw: dict[str, Any] | None) -> str | None:
    raw = raw or {}
    value = raw.get("extraLarge") or raw.get("large")
    return str(value) if value else None


def _large_cover(raw: dict[str, Any] | None) -> str | None:
    """``coverImage.extraLarge`` alone, with no fall back to ``large``.

    ``cover_large_url`` is a promise about the *size* of the image — the
    redesigned shelves render 2:3 artwork at 172 px and the browse grid wider
    still — so falling back to the 230 px ``large`` here would put the soft
    image behind the column whose whole purpose is to be the sharp one. A null
    is the honest answer, and the client falls back to ``cover_url`` itself.
    """
    value = (raw or {}).get("extraLarge")
    return str(value) if value else None


def _airing(nodes: list[dict[str, Any]]) -> list[AiringEntry]:
    """AniList's schedule nodes. Never estimated: these are published times."""
    out: list[AiringEntry] = []
    for node in nodes:
        episode = node.get("episode")
        at = node.get("airingAt")
        if episode is None or at is None:
            continue
        out.append(
            AiringEntry(
                episode=int(episode),
                at=datetime.fromtimestamp(int(at), UTC),
                estimated=False,
            )
        )
    out.sort(key=lambda entry: entry.episode)
    return out


def _has_next_page(connection: dict[str, Any] | None) -> bool:
    """Whether a schedule connection says there is another page after this one."""
    info = (connection or {}).get("pageInfo") or {}
    return bool(info.get("hasNextPage"))


def _relations(raw: dict[str, Any] | None) -> list[MediaRelation]:
    """Anime relations only.

    AniList's ``relations`` mixes in the source manga and light novels. Arc's
    client turns each of these into a link to a show page, and a manga id in
    that position is a dead end, so non-anime nodes are dropped here rather
    than filtered in three places downstream.
    """
    edges = (raw or {}).get("edges") or []
    out: list[MediaRelation] = []
    for edge in edges:
        node = edge.get("node") or {}
        if node.get("type") != "ANIME" or node.get("id") is None:
            continue
        out.append(
            MediaRelation(
                anilist_id=int(node["id"]),
                mal_id=node.get("idMal"),
                relation_type=str(edge.get("relationType") or "OTHER"),
                title=_title(node.get("title")),
                format=node.get("format"),
            )
        )
    return out


def parse_media(raw: dict[str, Any], *, full: bool) -> CatalogMedia:
    """Turn one AniList ``Media`` object into a :class:`CatalogMedia`."""
    if not full:
        return CatalogMedia(
            source="anilist",
            anilist_id=int(raw["id"]),
            mal_id=raw.get("idMal"),
            title=_title(raw.get("title")),
            format=raw.get("format"),
            episodes=raw.get("episodes"),
            status=raw.get("status"),
            season=raw.get("season"),
            season_year=raw.get("seasonYear"),
            cover_url=_cover(raw.get("coverImage")),
            cover_large_url=_large_cover(raw.get("coverImage")),
            popularity=raw.get("popularity"),
            average_score=raw.get("averageScore"),
            # Only the season query asks for this; a search result leaves it
            # null, and the cache is careful never to write a null summary
            # ``next_airing`` over a cached one (FR-C3).
            next_airing=raw.get("nextAiringEpisode"),
        )

    studios = ((raw.get("studios") or {}).get("nodes")) or []
    studio = studios[0].get("name") if studios else None
    schedule = _airing(((raw.get("aired") or {}).get("nodes")) or []) + _airing(
        ((raw.get("upcoming") or {}).get("nodes")) or []
    )
    schedule.sort(key=lambda entry: entry.episode)
    return CatalogMedia(
        source="anilist",
        anilist_id=int(raw["id"]),
        mal_id=raw.get("idMal"),
        title=_title(raw.get("title")),
        format=raw.get("format"),
        episodes=raw.get("episodes"),
        status=raw.get("status"),
        season=raw.get("season"),
        season_year=raw.get("seasonYear"),
        cover_url=_cover(raw.get("coverImage")),
        cover_large_url=_large_cover(raw.get("coverImage")),
        popularity=raw.get("popularity"),
        average_score=raw.get("averageScore"),
        banner_url=raw.get("bannerImage"),
        description=strip_html(raw.get("description")),
        genres=[str(genre) for genre in (raw.get("genres") or [])],
        synonyms=[str(name) for name in (raw.get("synonyms") or [])],
        tags=[
            {"name": tag.get("name"), "rank": tag.get("rank")} for tag in (raw.get("tags") or [])
        ],
        studio=studio,
        credits=credits_from(studio, staff_credits(raw.get("staff"))),
        relations=_relations(raw.get("relations")),
        next_airing=raw.get("nextAiringEpisode"),
        airing=schedule,
        # Placed against the *source's* episode count rather than the schedule,
        # because the positional fallback is only safe when the list is known
        # to be the whole show, and the schedule can be longer (a split cour
        # counted as one entry) or shorter (a premiere with no per-episode
        # slot) than the count AniList publishes.
        episode_extras=parse_streaming_episodes(
            raw.get("streamingEpisodes"), episodes=raw.get("episodes")
        ),
        start_date=(schedule[0].at.date() if schedule else None),
        full=True,
    )


# --- Description → plain text -----------------------------------------------

_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLANK_RUN = re.compile(r"\n{3,}")


def strip_html(text: str | None) -> str | None:
    """AniList's HTML description as plain text.

    ``<br>`` becomes a newline (it is how AniList writes a paragraph break),
    every other tag is dropped, and entities are unescaped. A regex rather
    than a parser on purpose: the input is AniList's own small, well-formed
    subset of HTML, and a dependency for it would be out of proportion.
    """
    if text is None:
        return None
    out = _BR.sub("\n", text)
    out = _TAG.sub("", out)
    out = unescape(out)
    out = _BLANK_RUN.sub("\n\n", out)
    out = "\n".join(line.rstrip() for line in out.splitlines())
    out = out.strip()
    return out or None


# --- The client -------------------------------------------------------------


class AniListClient:
    """A paced, retrying GraphQL client for AniList.

    Owns an :class:`httpx.AsyncClient`; close it with :meth:`aclose` or use
    the object as an async context manager. One instance is meant to be shared
    (inside ``app.state.catalog``) so that the pacing applies across callers —
    two clients in one process pace independently and can double the real rate.
    """

    def __init__(
        self,
        *,
        url: str = ANILIST_URL,
        min_interval: float = 0.7,
        concurrency: int = CONCURRENCY,
        timeout: float = TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        wait_on_rate_limit: bool = True,
    ) -> None:
        self.url = url
        self.min_interval = max(min_interval, 0.0)
        #: Whether a 429 is slept off (a job) or raised (a request handler).
        #: See the module docstring, "Who may wait".
        self.wait_on_rate_limit = wait_on_rate_limit
        self._sem = asyncio.Semaphore(concurrency)
        self._pace_lock = asyncio.Lock()
        #: Monotonic time before which the next request must not be sent.
        self._next_at = 0.0
        #: Monotonic time until which AniList has said it will refuse. Only
        #: written when ``wait_on_rate_limit`` is false; zero means "no reason
        #: to think so".
        self._rate_limited_until = 0.0
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "arc/0.1 (self-hosted anime server)",
            },
        )

    @classmethod
    def from_settings(cls, settings: Settings, *, wait_on_rate_limit: bool = True) -> Self:
        """Build a client from configuration (``ANILIST_*``)."""
        return cls(
            url=settings.anilist_url,
            min_interval=settings.anilist_min_interval_ms / 1000.0,
            wait_on_rate_limit=wait_on_rate_limit,
        )

    @property
    def rate_limited_until(self) -> float:
        """Monotonic instant before which :meth:`query` will refuse to ask.

        Always zero on a client that waits out its own 429s: it has no window
        to skip, because it never returns while one is open.
        """
        return self._rate_limited_until

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

    async def _note_budget(self, response: httpx.Response) -> None:
        """Widen the spacing when AniList says the minute's budget is nearly out.

        Takes ``_pace_lock`` for the write: ``_next_at`` is read-modify-written
        here and in :meth:`_wait_turn`, and with ``concurrency`` requests in
        flight an unlocked update can land between another task's read and its
        own write — losing exactly the pause this method exists to impose.
        """
        raw = response.headers.get("X-RateLimit-Remaining")
        if raw is None:
            return
        try:
            remaining = int(raw)
        except ValueError:
            return
        if remaining > LOW_BUDGET_THRESHOLD:
            return
        async with self._pace_lock:
            self._next_at = max(self._next_at, time.monotonic() + LOW_BUDGET_PAUSE)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        """How long to sleep off a 429.

        A header that was sent is honoured up to :data:`MAX_RETRY_AFTER`; a
        missing or unparseable one falls back to :data:`DEFAULT_RETRY_AFTER`
        rather than to the cap (see the module docstring).
        """
        raw = response.headers.get("Retry-After")
        if raw is None:
            return DEFAULT_RETRY_AFTER
        try:
            seconds = float(raw)
        except ValueError:
            return DEFAULT_RETRY_AFTER
        return min(max(seconds, 0.0), MAX_RETRY_AFTER)

    def _check_rate_limit_window(self, name: str) -> None:
        """Fail before the request when AniList has already said it will refuse.

        The window only ever exists on an interactive client (see the module
        docstring). Cheap on purpose: one comparison, no lock. A race that
        lets one extra request out during the window costs a request, and
        contending a lock on every query to save it would cost more.
        """
        if self.wait_on_rate_limit:
            return
        remaining = self._rate_limited_until - time.monotonic()
        if remaining <= 0:
            return
        raise AniListRateLimited(
            f"anilist {name}: rate limited, retry in {remaining:.1f}s",
            retry_after=remaining,
        )

    def _rate_limited(self, name: str, pause: float) -> AniListRateLimited:
        """Note the window this 429 opened, and describe it for the caller.

        ``max`` rather than a plain assignment: two interactive calls can be
        in flight and the later response can carry the shorter header, and
        shortening a window that another 429 has already justified would send
        the next request straight back into it.
        """
        self._rate_limited_until = max(self._rate_limited_until, time.monotonic() + pause)
        log.warning(
            "anilist rate limited; not waiting",
            extra={"operation": name, "retry_after_s": pause},
        )
        return AniListRateLimited(
            f"anilist {name}: rate limited, retry in {pause:.1f}s", retry_after=pause
        )

    # --- The one request method ---

    async def query(
        self,
        document: str,
        variables: dict[str, Any],
        *,
        name: str = "query",
    ) -> dict[str, Any]:
        """Run ``document`` and return its ``data`` object.

        Raises :class:`AniListNotFound` when AniList answers "not found" for
        the thing that was asked for, :class:`AniListRateLimited` when this
        client does not wait out 429s and is inside one, and
        :class:`AniListError` for everything else that went wrong.
        """
        self._check_rate_limit_window(name)
        body = {"query": document, "variables": variables}
        started = time.monotonic()
        attempt = 0
        rate_limited_once = False
        last_error: str = "unknown error"

        while True:
            attempt += 1
            await self._wait_turn()
            async with self._sem:
                try:
                    response = await self._http.post(self.url, json=body)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < SERVER_ERROR_ATTEMPTS:
                        await _sleep(BACKOFF_BASE * 2 ** (attempt - 1))
                        continue
                    raise AniListError(f"anilist {name} failed: {last_error}") from exc

            await self._note_budget(response)

            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                pause = self._retry_after(response)
                if not self.wait_on_rate_limit:
                    raise self._rate_limited(name, pause)
                if rate_limited_once:
                    raise AniListError(f"anilist {name}: rate limited twice, giving up")
                rate_limited_once = True
                log.warning(
                    "anilist rate limited", extra={"operation": name, "retry_after_s": pause}
                )
                await _sleep(pause)
                continue

            if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                last_error = f"HTTP {response.status_code}"
                if attempt < SERVER_ERROR_ATTEMPTS:
                    await _sleep(BACKOFF_BASE * 2 ** (attempt - 1))
                    continue
                raise AniListError(f"anilist {name} failed: {last_error}")

            payload = self._decode(response, name)
            errors = payload.get("errors")
            if errors:
                message = "; ".join(str(err.get("message", err)) for err in errors)
                lowered = message.lower()
                if any(marker in lowered for marker in DISABLED_MARKERS):
                    raise AniListDisabled(f"anilist {name}: {message}")
                if response.status_code == httpx.codes.NOT_FOUND or "not found" in lowered:
                    raise AniListNotFound(f"anilist {name}: {message}")
                raise AniListError(f"anilist {name}: {message}")

            data = payload.get("data")
            if not isinstance(data, dict):
                raise AniListError(f"anilist {name}: response had no data object")

            log.info(
                "anilist query",
                extra={
                    "operation": name,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "attempts": attempt,
                    "status": response.status_code,
                },
            )
            return data

    @staticmethod
    def _decode(response: httpx.Response, name: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise AniListError(
                f"anilist {name}: HTTP {response.status_code} with a non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise AniListError(f"anilist {name}: response was not a JSON object")
        return payload

    # --- Typed wrappers ---

    async def search(self, term: str, *, page: int = 1) -> SearchPage:
        """Title search, one page of :data:`SEARCH_PER_PAGE` (FR-C1)."""
        data = await self.query(
            SEARCH,
            {"search": term, "page": page, "perPage": SEARCH_PER_PAGE},
            name="search",
        )
        block = data.get("Page") or {}
        info = block.get("pageInfo") or {}
        media = [
            parse_media(raw, full=False) for raw in (block.get("media") or []) if raw is not None
        ]
        return SearchPage(
            results=media,
            page=int(info.get("currentPage") or page),
            has_next=bool(info.get("hasNextPage")),
        )

    async def season(self, year: int, season: str) -> list[CatalogMedia]:
        """Every reasonably popular title of one season (FR-C7).

        Pages until AniList says there is no more, capped at
        :data:`MAX_SEASON_PAGES`. The cap is not a limitation so much as a
        statement of intent: the pre-cache exists so the schedule survives an
        outage, and the two hundred most popular shows of a season are that
        schedule. The long tail of one-episode ONAs is not worth four more
        requests against a 30/min budget every night.
        """
        collected: list[CatalogMedia] = []
        for page in range(1, MAX_SEASON_PAGES + 1):
            data = await self.query(
                SEASON,
                {
                    "season": season.upper(),
                    "seasonYear": int(year),
                    "page": page,
                    "perPage": SEASON_PER_PAGE,
                },
                name="season",
            )
            block = data.get("Page") or {}
            collected.extend(
                parse_media(raw, full=False)
                for raw in (block.get("media") or [])
                if raw is not None
            )
            if not (block.get("pageInfo") or {}).get("hasNextPage"):
                break
        return collected

    async def media_by_mal_id(self, mal_id: int) -> CatalogMedia:
        """The same detail record, found by MyAnimeList id (FR-C6).

        AniList maps only the MAL ids it happens to know, so a miss here is
        common and means "ask MAL", not "no such show" — which is why
        :class:`CatalogService` does not treat a not-found from this call as
        final the way it does for an AniList id.
        """
        data = await self.query(MEDIA_BY_MAL_ID, {"idMal": mal_id}, name="media.by_mal")
        raw = data.get("Media")
        if not raw:
            raise AniListNotFound(f"anilist has no anime with mal id {mal_id}")
        return await self._with_full_schedule(parse_media(raw, full=True), raw)

    async def media(self, anime_id: int) -> CatalogMedia:
        """One title with everything the cache stores.

        Raises :class:`AniListNotFound` for an id AniList does not know —
        including the case where it answers 200 with ``{"Media": null}``.

        One round trip for anything up to 100 aired episodes, plus one per
        further :data:`SCHEDULE_PER_PAGE` after that: a show longer than a
        single schedule page would otherwise come back with air times for its
        first hundred episodes and nulls for the rest, which reads on the show
        page as "episode 101 has not aired".
        """
        data = await self.query(MEDIA_BY_ID, {"id": anime_id}, name="media")
        raw = data.get("Media")
        if not raw:
            raise AniListNotFound(f"anilist has no anime with id {anime_id}")
        return await self._with_full_schedule(parse_media(raw, full=True), raw)

    async def _with_full_schedule(self, media: CatalogMedia, raw: dict[str, Any]) -> CatalogMedia:
        """``media`` plus any schedule pages the first response did not carry.

        The follow-up query is keyed on the AniList id, which is why this takes
        the parsed record rather than an id: a by-MAL-id fetch has to page the
        same way, and the only id AniList's schedule query accepts is the one
        that came back in the response.
        """
        if media.anilist_id is None or not _has_next_page(raw.get("aired")):
            return media
        rest = await self._rest_of_schedule(media.anilist_id, media.airing)
        return replace(media, airing=rest, start_date=(rest[0].at.date() if rest else None))

    async def _rest_of_schedule(
        self, anime_id: int, first_page: list[AiringEntry]
    ) -> list[AiringEntry]:
        """``first_page`` plus every further page of the aired schedule.

        Stops at :data:`MAX_SCHEDULE_PAGES` and logs when it does, so that a
        show past the cap is a line in the log rather than a mystery gap in an
        episode list. A page that fails is not fatal either: what has been
        collected so far is better than losing the whole refresh over the tail
        of a back catalogue.
        """
        collected = list(first_page)
        page = 1
        while page < MAX_SCHEDULE_PAGES:
            page += 1
            try:
                data = await self.query(
                    AIRED_SCHEDULE_PAGE,
                    {"id": anime_id, "page": page},
                    name="media.schedule",
                )
            except AniListError as exc:
                # The title itself came back fine; only its back catalogue is
                # short. Failing the whole fetch would send ``ensure_anime``
                # to the cached row and throw away a good detail response.
                log.warning(
                    "anilist airing schedule page failed; keeping what arrived",
                    extra={"anime_id": anime_id, "page": page, "error": str(exc)},
                )
                break
            raw = data.get("Media") or {}
            connection = raw.get("aired") or {}
            collected.extend(_airing(connection.get("nodes") or []))
            if not _has_next_page(connection):
                break
        else:
            log.warning(
                "anilist airing schedule truncated at the page cap",
                extra={
                    "anime_id": anime_id,
                    "pages": MAX_SCHEDULE_PAGES,
                    "episodes": len(collected),
                },
            )

        collected.sort(key=lambda entry: entry.episode)
        return collected


__all__ = [
    "ANILIST_URL",
    "DEFAULT_RETRY_AFTER",
    "DISABLED_MARKERS",
    "MAX_SCHEDULE_PAGES",
    "MAX_SEASON_PAGES",
    "SCHEDULE_PER_PAGE",
    "SEARCH_PER_PAGE",
    "SEASON_PER_PAGE",
    "AniListClient",
    "AniListDisabled",
    "AniListError",
    "AniListNotFound",
    "parse_media",
    "strip_html",
]
