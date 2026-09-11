"""MyAnimeList's official API as a read-only catalogue source (FR-C6).

Reads on MAL API v2 need nothing but a registered client id in a header
(``X-MAL-CLIENT-ID``), so this is the one fallback Arc can use without asking
each user to authorise anything. The OAuth half — list import and the writes of
FR-M4 — belongs to M9 and lives elsewhere; nothing here can write.

Three things make MAL's answers different from AniList's, and all three are
handled here so nothing above has to know:

* **Vocabulary.** MAL says ``finished_airing`` and ``tv``; Arc stores AniList's
  ``FINISHED`` and ``TV``, because that is what the columns already hold and
  what the client already renders. The maps are at the top of the file.
* **No airing schedule.** MAL publishes a weekly broadcast slot, not per-episode
  air times, so :func:`synthesise_airing` works them out from ``start_date``
  plus that slot and marks every one of them estimated. The client badges them
  and AniList's real times replace them the moment AniList is back. The same
  slot gives :func:`synthesise_next_airing` the next broadcast of a currently
  airing show, which is what puts a MAL-sourced row on a weekday of the
  schedule (FR-C3).
* **Partial dates.** ``start_date`` can be ``2023``, ``2023-09`` or
  ``2023-09-29``. Only the full form is a date; the others are dropped rather
  than guessed, because a wrong first-episode date propagates to every episode.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Self
from zoneinfo import ZoneInfo

import httpx

from arc.config import Settings
from arc.services.catalog.credits import credits_from
from arc.services.catalog.source import (
    AiringEntry,
    CatalogMedia,
    MediaRelation,
    MediaTitle,
    SearchPage,
    SourceName,
    SourceNotFound,
    SourceUnavailable,
)

log = logging.getLogger(__name__)

#: The public endpoint. Overridden by ``MAL_API_URL``.
MAL_API_URL = "https://api.myanimelist.net/v2"

#: How long a single request may take. The same 15 s as AniList: long enough
#: that a slow answer is not thrown away, short enough that a hung fallback
#: does not hold a request handler open past any sensible page load.
TIMEOUT_SECONDS = 15.0

#: One retry for a 429 or a 5xx. MAL is the *fallback*: it is being asked
#: because something else already failed, so the budget for patience is small.
RETRY_PAUSE_SECONDS = 1.0

#: Results per search page. Matches AniList's page size so the two sources
#: paginate identically and the client cannot tell them apart.
SEARCH_LIMIT = 20

#: Season pages. MAL's ceiling for this endpoint is 500; 100 in one request is
#: plenty for a schedule and keeps the response under a megabyte.
SEASON_LIMIT = 100
MAX_SEASON_PAGES = 4

#: Broadcast times are Japanese local time, always.
JST = ZoneInfo("Asia/Tokyo")

#: The slot assumed when MAL gives a broadcast weekday but no time, or no
#: broadcast at all. Late-night (23:00–01:30 JST) is where most TV anime airs,
#: and 23:00 is the modal hour; more importantly it is late enough to be the
#: same calendar day in UTC, which midnight is not.
DEFAULT_BROADCAST_TIME = time(23, 0)

#: Refuse to synthesise a schedule longer than this. A bad ``num_episodes``
#: would otherwise write tens of thousands of episode rows.
MAX_SYNTHESISED_EPISODES = 2000

#: The AniList status value that means "still airing". ``STATUS_MAP`` maps
#: MAL's ``currently_airing`` onto it, and a synthesised broadcast slot is only
#: written for shows in that state.
RELEASING = "RELEASING"

#: MAL's airing status → the AniList vocabulary ``anime.status`` holds.
STATUS_MAP = {
    "finished_airing": "FINISHED",
    "currently_airing": "RELEASING",
    "not_yet_aired": "NOT_YET_RELEASED",
}

#: MAL's ``media_type`` → the AniList ``format`` vocabulary. ``tv_special`` has
#: no AniList equivalent and is folded into ``SPECIAL``, which is what AniList
#: itself calls those entries.
FORMAT_MAP = {
    "tv": "TV",
    "tv_special": "SPECIAL",
    "ova": "OVA",
    "movie": "MOVIE",
    "special": "SPECIAL",
    "ona": "ONA",
    "music": "MUSIC",
}

#: Monday is 0, matching :meth:`datetime.date.weekday`.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: Fields a search result and a season entry carry: enough for a card, plus the
#: broadcast slot, because a summary is the only thing the season pre-cache
#: ever sees and the schedule page needs a weekday (FR-C7).
SUMMARY_FIELDS = (
    "id,title,alternative_titles{synonyms,en,ja},main_picture{large},"
    "num_episodes,status,start_date,end_date,start_season{year,season},"
    "broadcast{day_of_the_week,start_time},media_type,mean,num_list_users"
)

#: Everything a show page renders. ``related_anime`` nodes carry no ``idMal``
#: of their own — the node *is* a MAL id — so relations arrive MAL-keyed and
#: the API resolves them against the local rows.
DETAIL_FIELDS = SUMMARY_FIELDS + (
    ",synopsis,genres{name},studios{name},"
    "related_anime{node{id,title,media_type},relation_type},"
    "average_episode_duration"
)


def _mean_to_score(mean: float | int | str | None) -> int | None:
    """MAL's 0–10 ``mean`` as AniList's 0–100 ``averageScore``.

    One column holds both, so the two sources have to agree on a scale. MAL's
    is the odd one out, so it is the one converted.
    """
    if mean is None:
        return None
    try:
        return round(float(mean) * 10)
    except TypeError, ValueError:
        return None


def _titles(raw: dict[str, Any]) -> MediaTitle:
    """MAL's ``title`` is the romaji one; the rest live in alternative titles."""
    alternatives = raw.get("alternative_titles") or {}
    return MediaTitle(
        romaji=raw.get("title"),
        english=alternatives.get("en") or None,
        native=alternatives.get("ja") or None,
    )


def _synonyms(raw: dict[str, Any]) -> list[str]:
    alternatives = raw.get("alternative_titles") or {}
    return [str(name) for name in (alternatives.get("synonyms") or [])]


def parse_date(value: str | None) -> date | None:
    """``2023-09-29`` → a date; ``2023`` and ``2023-09`` → ``None``.

    MAL is honest about not knowing a full date and says so by sending a
    shorter string. Arc treats that as "no date": every synthesised air time is
    an offset from this one, so a guessed day becomes a wrong day for every
    episode of the show.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_broadcast(raw: dict[str, Any] | None) -> tuple[int, time] | None:
    """``{"day_of_the_week": "friday", "start_time": "23:00"}`` → ``(4, 23:00)``.

    Both halves are required: MAL sometimes gives a weekday with no time (a
    show whose slot moves), and a broadcast with no time tells us nothing an
    air *time* can be built from.
    """
    raw = raw or {}
    day = str(raw.get("day_of_the_week") or "").lower()
    start = raw.get("start_time")
    if day not in WEEKDAYS or not start:
        return None
    try:
        at = time.fromisoformat(str(start))
    except ValueError:
        return None
    return (WEEKDAYS.index(day), at)


def synthesise_airing(
    *,
    start_date: date | None,
    broadcast: tuple[int, time] | None,
    episodes: int | None,
) -> list[AiringEntry]:
    """Weekly air times from a start date and a broadcast slot (FR-C6).

    Episode 1 airs on ``start_date`` at the broadcast time in Tokyo; every
    later episode is a week after the one before. The broadcast *weekday* is
    deliberately not used to move episode 1: MAL's ``start_date`` is the day
    the show actually premiered, and a premiere in a different slot from the
    weekly one (a late-night double bill, a Sunday special) is common enough
    that trusting the weekday would shift the whole run.

    When MAL publishes no broadcast time at all the slot falls back to
    :data:`DEFAULT_BROADCAST_TIME` rather than to midnight. Midnight JST is the
    *previous* calendar day everywhere west of Japan — 15:00 UTC on the 28th
    for a show that premiered on the 29th — so a missing time would silently
    move every episode of the show back a day for most of the world. 23:00 is
    the modal late-night anime slot and keeps the premiere on its own date in
    UTC and the Americas.

    Every entry is marked estimated. None of this is published data, and the
    client says so until AniList replaces it.
    """
    if start_date is None or not episodes or episodes < 1:
        return []
    count = min(int(episodes), MAX_SYNTHESISED_EPISODES)
    at_local = DEFAULT_BROADCAST_TIME if broadcast is None else broadcast[1]
    first = datetime.combine(start_date, at_local, tzinfo=JST).astimezone(UTC)
    return [
        AiringEntry(episode=number, at=first + timedelta(weeks=number - 1), estimated=True)
        for number in range(1, count + 1)
    ]


def next_broadcast(broadcast: tuple[int, time], *, now: datetime) -> datetime:
    """The next occurrence of a weekly JST slot, at or after ``now``.

    Wholly a Tokyo-local calculation before it is converted back: "Friday
    23:00" is a Japanese weekday, and working the next Friday out in UTC puts
    a late-night slot on Saturday for half the year.
    """
    weekday, at = broadcast
    local = now.astimezone(JST)
    candidate = datetime.combine(local.date(), at, tzinfo=JST)
    candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
    if candidate < local:
        candidate += timedelta(weeks=1)
    return candidate.astimezone(UTC)


def synthesise_next_airing(
    *, status: str | None, broadcast: tuple[int, time] | None, now: datetime
) -> dict[str, Any] | None:
    """A ``next_airing`` blob for a currently-airing show (FR-C3, FR-C6).

    MAL publishes no ``nextAiringEpisode``, and without one a MAL-sourced
    season row lands on no weekday at all — which is exactly the state the
    schedule has to survive, since the pre-cache runs through MAL precisely
    when AniList is down. The broadcast slot is enough to say *when* the next
    episode airs, so that is what this says.

    What it deliberately does not say is *which* episode. MAL's ``num_episodes``
    is the total, not the count aired, and counting weeks from ``start_date``
    would guess through every break and recap week a show ever takes. The
    number is null, ``estimated`` marks the blob as Arc's own arithmetic, and
    the show page renders no "next episode" line for it.
    """
    if status != RELEASING or broadcast is None:
        return None
    at = next_broadcast(broadcast, now=now)
    return {"episode": None, "airingAt": int(at.timestamp()), "estimated": True}


def _relations(raw: dict[str, Any]) -> list[MediaRelation]:
    out: list[MediaRelation] = []
    for edge in raw.get("related_anime") or []:
        node = edge.get("node") or {}
        node_id = node.get("id")
        if node_id is None:
            continue
        out.append(
            MediaRelation(
                # MAL's ``relation_type`` is lower_snake; the column already
                # holds AniList's SCREAMING_SNAKE, and one vocabulary means the
                # client renders a relation the same whichever source found it.
                relation_type=str(edge.get("relation_type") or "other").upper(),
                title=MediaTitle(romaji=node.get("title")),
                mal_id=int(node_id),
                format=FORMAT_MAP.get(str(node.get("media_type") or "").lower()),
            )
        )
    return out


def parse_anime(raw: dict[str, Any], *, full: bool, now: datetime | None = None) -> CatalogMedia:
    """One MAL ``anime`` object as a :class:`CatalogMedia`.

    ``now`` is the instant the synthesised broadcast slot is measured from;
    it defaults to the present and exists so a test can pin it.
    """
    start_date = parse_date(raw.get("start_date"))
    broadcast = parse_broadcast(raw.get("broadcast"))
    episodes = raw.get("num_episodes") or None
    start_season = raw.get("start_season") or {}
    season = start_season.get("season")
    status = STATUS_MAP.get(str(raw.get("status") or "").lower())
    summary = {
        "source": "mal",
        "mal_id": int(raw["id"]),
        "title": _titles(raw),
        "format": FORMAT_MAP.get(str(raw.get("media_type") or "").lower()),
        "episodes": episodes,
        "status": status,
        "season": str(season).upper() if season else None,
        "season_year": start_season.get("year"),
        "cover_url": (raw.get("main_picture") or {}).get("large"),
        # AniList's two ranking numbers, in MAL's currencies. ``num_list_users``
        # is the same idea as ``popularity`` — how many people have it on a
        # list — and ``mean`` is a 0–10 score, so it is scaled to AniList's
        # 0–100 to keep one comparable column (§5.6). Both are null when MAL
        # has no rating yet, which is normal for an unaired show.
        "popularity": raw.get("num_list_users"),
        "average_score": _mean_to_score(raw.get("mean")),
        "broadcast": broadcast,
        "start_date": start_date,
        # Carried by a summary as well as a detail record: a season row with no
        # next broadcast has no weekday on the schedule (FR-C3).
        "next_airing": synthesise_next_airing(
            status=status, broadcast=broadcast, now=now or datetime.now(UTC)
        ),
    }
    if not full:
        return CatalogMedia(**summary)  # type: ignore[arg-type]

    synopsis = raw.get("synopsis")
    studios = raw.get("studios") or []
    studio = studios[0].get("name") if studios else None
    return CatalogMedia(
        **summary,  # type: ignore[arg-type]
        # MAL's synopsis is plain text with real newlines — no HTML to strip,
        # unlike AniList's.
        description=(str(synopsis).strip() or None) if synopsis else None,
        synonyms=_synonyms(raw),
        genres=[str(genre.get("name")) for genre in (raw.get("genres") or []) if genre.get("name")],
        studio=studio,
        # MAL publishes no staff at all through the official API — no director,
        # no composer, nothing (M15). So a MAL-filled row's credits block is
        # the studio row on its own, which is honest and still renders; the
        # rest arrives the first time AniList answers for this show, because
        # ``_apply``'s fill-only rule lets AniList overwrite what MAL wrote.
        credits=credits_from(studio),
        relations=_relations(raw),
        airing=synthesise_airing(start_date=start_date, broadcast=broadcast, episodes=episodes),
        full=True,
    )


class MalSource:
    """MyAnimeList as the fallback catalogue source.

    Unconfigured is not an error to raise at import or startup: a deployment
    without a client id simply has no fallback, and everything must keep
    working while AniList is up. So :attr:`configured` is false and every call
    raises :class:`SourceUnavailable` with reason ``unconfigured``, which the
    admin status view shows and the service treats like any other outage.
    """

    name: SourceName = "mal"

    def __init__(
        self,
        *,
        url: str = MAL_API_URL,
        client_id: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.client_id = client_id or None
        headers = {
            "Accept": "application/json",
            "User-Agent": "arc/0.1 (self-hosted anime server)",
        }
        if self.client_id:
            headers["X-MAL-CLIENT-ID"] = self.client_id
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, headers=headers)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(url=settings.mal_api_url, client_id=settings.mal_client_id)

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- One request ---

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One GET, retried once for a 429 or a 5xx.

        A 404 is :class:`SourceNotFound`; everything else that goes wrong is
        :class:`SourceUnavailable`, including a 401/403, which for a read
        endpoint means the client id is wrong rather than that the caller is.
        """
        if not self.configured:
            raise SourceUnavailable(self.name, "unconfigured")

        target = f"{self.url}{path}"
        for attempt in (1, 2):
            try:
                response = await self._http.get(target, params=params)
            except httpx.HTTPError as exc:
                if attempt == 1:
                    await _sleep(RETRY_PAUSE_SECONDS)
                    continue
                raise SourceUnavailable(self.name, f"{type(exc).__name__}: {exc}") from exc

            if response.status_code == httpx.codes.NOT_FOUND:
                raise SourceNotFound(f"mal has nothing at {path}")
            retryable = (
                response.status_code == httpx.codes.TOO_MANY_REQUESTS
                or response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
            )
            if retryable and attempt == 1:
                await _sleep(RETRY_PAUSE_SECONDS)
                continue
            if response.status_code >= httpx.codes.BAD_REQUEST:
                raise SourceUnavailable(self.name, f"HTTP {response.status_code}")

            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceUnavailable(self.name, "non-JSON body") from exc
            if not isinstance(payload, dict):
                raise SourceUnavailable(self.name, "response was not a JSON object")
            return payload

        raise SourceUnavailable(self.name, "exhausted retries")  # pragma: no cover

    @staticmethod
    def _nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """``{"data": [{"node": {...}}]}`` → the list of anime objects."""
        return [
            entry["node"]
            for entry in (payload.get("data") or [])
            if isinstance(entry, dict) and isinstance(entry.get("node"), dict)
        ]

    # --- The interface ---

    async def search(self, term: str, *, page: int = 1) -> SearchPage:
        """Title search (FR-C1). MAL pages by offset, not page number."""
        offset = max(page - 1, 0) * SEARCH_LIMIT
        payload = await self._get(
            "/anime",
            {"q": term, "limit": SEARCH_LIMIT, "offset": offset, "fields": SUMMARY_FIELDS},
        )
        results = [parse_anime(node, full=False) for node in self._nodes(payload)]
        return SearchPage(
            results=results,
            page=page,
            has_next=bool((payload.get("paging") or {}).get("next")),
        )

    async def by_anilist_id(self, anilist_id: int) -> CatalogMedia | None:
        """MAL knows nothing about AniList ids, and says so by answering ``None``.

        Not an error: the service reads ``None`` as "this source cannot help"
        and moves on, which is different from "there is no such show".
        """
        return None

    async def by_mal_id(self, mal_id: int) -> CatalogMedia | None:
        payload = await self._get(f"/anime/{mal_id}", {"fields": DETAIL_FIELDS})
        return parse_anime(payload, full=True)

    async def season(self, year: int, season: str) -> list[CatalogMedia]:
        collected: list[CatalogMedia] = []
        for page in range(MAX_SEASON_PAGES):
            payload = await self._get(
                f"/anime/season/{year}/{season.lower()}",
                {"limit": SEASON_LIMIT, "offset": page * SEASON_LIMIT, "fields": SUMMARY_FIELDS},
            )
            collected.extend(parse_anime(node, full=False) for node in self._nodes(payload))
            if not (payload.get("paging") or {}).get("next"):
                break
        return collected


async def _sleep(seconds: float) -> None:
    """Indirection so a test can skip the retry pause. See the AniList client."""
    import asyncio

    await asyncio.sleep(seconds)


__all__ = [
    "DEFAULT_BROADCAST_TIME",
    "RELEASING",
    "DETAIL_FIELDS",
    "FORMAT_MAP",
    "JST",
    "MAL_API_URL",
    "MAX_SEASON_PAGES",
    "SEARCH_LIMIT",
    "SEASON_LIMIT",
    "STATUS_MAP",
    "SUMMARY_FIELDS",
    "WEEKDAYS",
    "MalSource",
    "parse_anime",
    "next_broadcast",
    "parse_broadcast",
    "parse_date",
    "synthesise_airing",
    "synthesise_next_airing",
]
