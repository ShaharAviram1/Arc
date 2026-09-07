"""Nyaa: search it, read it, discard most of it, rank what is left (FR-A3, FR-A4).

Four things live here and they are deliberately separable, because only the
first of them talks to the network:

* :class:`NyaaClient` — the RSS search feed, politely (architecture.md §6).
* :func:`queries` — what to ask for, given an anime and an episode number.
* :func:`acceptable` — whether one result really is that episode of that show.
* :func:`rank` — which of the survivors to take, and why (FR-A3).

**The filter is the interesting half.** A Nyaa search for
``"Sousou no Frieren - 07"`` returns the episode, the same episode from four
other groups at three resolutions, the *second season*'s episode 7, an English
dub of episode 12 that happens to have ``07`` in its hash, and two batches of
episodes 1–7. Every one of those matches the query; one of them is the file
Arc asked for. So each result title is parsed with the **same parser the
library uses** (FR-A4) and has to agree about all of: it is a single episode,
the number is the one being fetched, the title is that show's, and the season
is that show's. A release Nyaa has flagged as a ``remake`` is dropped outright:
it is a re-encode of somebody else's work, and the original is in the same
list.

The season check reads a missing marker as **season 1**, on both sides. A
catalogue entry with no marker is season 1 of that entry — AniList files each
season as its own row, so ``Sousou no Frieren`` and ``Sousou no Frieren 2nd
Season`` are two shows with two episode 7s — and a release with no marker is
season 1 of the show it names. Reading either as "no opinion" is how episode 7
of the wrong season ends up on disk, and a wrong file is worse than no file:
the matcher's own prior would then link it to the episode Arc asked for and it
would play as though it were right.

**Ranking is FR-A3's four rules in order**: preferred group, then resolution,
then seeders, then Nyaa's trusted flag. Each pick records the reasons it won,
because "why did it choose that release?" is the first question anybody asks
of an acquisition system and a score with no explanation cannot answer it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

import httpx
from rapidfuzz import fuzz

from arc.models import Anime
from arc.services.acquisition.rules import Rules
from arc.services.library.parser import ParsedName, parse, strip_season, title_key

log = logging.getLogger(__name__)

#: Patched by the tests, exactly as the AniList client's pause is: the pacing
#: below is a *duration* worth asserting on and not worth paying. ``_now`` is
#: patched alongside it, because a test that does not pay the pause still has
#: to see the clock move for the pacing to mean anything.
_sleep = asyncio.sleep
_now = time.monotonic

#: Nyaa's XML namespace, as the feed declares it.
NYAA_NS: Final[str] = "https://nyaa.si/xmlns/nyaa"

#: ``1_2`` = Anime - English-translated (architecture.md §6). ``f=0`` is "no
#: filter": trusted-only would be ``f=2``, and FR-A3 wants the flag as a
#: tie-break rather than as a gate.
CATEGORY: Final[str] = "1_2"
FILTER: Final[str] = "0"

#: Nyaa's own tracker list, the one its magnet links carry. The RSS feed gives
#: a ``.torrent`` URL and an info hash but no magnet, and a magnet with no
#: trackers relies entirely on DHT — which works, eventually, and "eventually"
#: is not what FR-A5 is for.
TRACKERS: Final[tuple[str, ...]] = (
    "http://nyaa.tracker.wf:7777/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.coppersurfer.tk:6969/announce",
    "udp://exodus.desync.com:6969/announce",
)

#: ≤ 1 request every 2 s (architecture.md §6). Nyaa is a volunteer-run site
#: with no API and no key; the politeness here is the whole contract.
MIN_INTERVAL: Final[float] = 2.0

#: How long an answer is reused. Ten minutes, because the queries a single
#: search makes overlap heavily with the ones the *next* episode's search
#: makes — the broad forms are identical but for the number — and because a
#: release that appeared 90 seconds ago will still be there in nine minutes.
CACHE_TTL: Final[float] = 600.0

TIMEOUT_SECONDS: Final[float] = 20.0

#: How long to wait before the single retry after a 5xx or a timeout.
RETRY_AFTER: Final[float] = 3.0

#: How close a parsed release title must be to one of the show's titles
#: (FR-A4). High, because the episode number and season have already been
#: checked by the time this is asked and the only thing left to get wrong is
#: the show itself.
TITLE_THRESHOLD: Final[float] = 0.90

#: Most queries a single search makes, however many the builder produced.
#: **Every one of them runs**, so this is the budget one episode's search may
#: spend: five × 2 s of spacing, and rather less in practice because the cache
#: is shared and the broad forms repeat from episode to episode.
MAX_QUERIES: Final[int] = 5

#: Season numbers as a release group writes them in a title. Only up to 5:
#: past that nobody uses numerals, and ``I`` is never written at all.
ROMAN_SEASONS: Final[dict[int, str]] = {2: "II", 3: "III", 4: "IV", 5: "V"}

#: Episode numbers are padded to two digits, or to three once a show is long
#: enough that groups start writing ``- 105``.
LONG_SHOW_EPISODES: Final[int] = 100


class NyaaUnavailable(RuntimeError):
    """Nyaa could not be reached, or answered with something unusable."""


@dataclass(frozen=True, slots=True)
class NyaaItem:
    """One ``<item>`` of the RSS feed."""

    title: str
    #: The ``.torrent`` URL. Kept for debugging and for a future fallback to
    #: adding the file rather than the magnet.
    link: str
    info_hash: str
    seeders: int = 0
    leechers: int = 0
    downloads: int = 0
    #: As Nyaa writes it: ``"585.1 MiB"``. Not parsed — it is shown, not
    #: compared, and every parse of it would be a new way to be wrong.
    size: str | None = None
    trusted: bool = False
    remake: bool = False
    category_id: str | None = None

    @property
    def magnet(self) -> str:
        """A magnet for this info hash, with Nyaa's trackers attached."""
        query = f"magnet:?xt=urn:btih:{self.info_hash}&dn={quote(self.title)}"
        return query + "".join(f"&tr={quote(tracker, safe='')}" for tracker in TRACKERS)


def _text(item: ElementTree.Element, tag: str) -> str | None:
    found = item.find(tag)
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _int(item: ElementTree.Element, tag: str) -> int:
    raw = _text(item, tag)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _flag(item: ElementTree.Element, tag: str) -> bool:
    return (_text(item, tag) or "").casefold() == "yes"


def parse_feed(xml: str) -> list[NyaaItem]:
    """Every usable ``<item>`` of a Nyaa RSS document.

    stdlib ``xml.etree`` rather than a dependency: the document is a flat list
    of a dozen fields and Nyaa is not going to change it. Items without a
    title or an info hash are dropped — there is nothing to do with one — and
    a document that is not XML at all raises :class:`NyaaUnavailable`, because
    that is what a Cloudflare interstitial looks like.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise NyaaUnavailable(f"nyaa did not answer with XML: {exc}") from exc

    items: list[NyaaItem] = []
    for element in root.iter("item"):
        title = _text(element, "title")
        info_hash = _text(element, f"{{{NYAA_NS}}}infoHash")
        if not title or not info_hash:
            continue
        items.append(
            NyaaItem(
                title=title,
                link=_text(element, "link") or "",
                info_hash=info_hash.lower(),
                seeders=_int(element, f"{{{NYAA_NS}}}seeders"),
                leechers=_int(element, f"{{{NYAA_NS}}}leechers"),
                downloads=_int(element, f"{{{NYAA_NS}}}downloads"),
                size=_text(element, f"{{{NYAA_NS}}}size"),
                trusted=_flag(element, f"{{{NYAA_NS}}}trusted"),
                remake=_flag(element, f"{{{NYAA_NS}}}remake"),
                category_id=_text(element, f"{{{NYAA_NS}}}categoryId"),
            )
        )
    return items


class NyaaClient:
    """The RSS search feed, paced and cached.

    **One instance per process**, handed out by :func:`shared_client`. The
    pacing lock and the cache belong to the instance, so the scope of the
    instance *is* the scope of the politeness: a client per search job would
    space that job's own queries two seconds apart and let three concurrent
    jobs leave together anyway, which is exactly the burst
    :data:`MIN_INTERVAL` exists to prevent. Sharing one client also shares the
    ten-minute cache, and it is worth sharing — the queries one episode makes
    overlap heavily with the ones the next episode of the same show makes.
    """

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = MIN_INTERVAL,
        cache_ttl: float = CACHE_TTL,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._min_interval = min_interval
        self._cache_ttl = cache_ttl
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache: dict[str, tuple[float, list[NyaaItem]]] = {}
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Arc/0.1 (self-hosted anime server)"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def url_for(self, query: str) -> str:
        """The RSS URL for ``query`` (architecture.md §6)."""
        params = urlencode({"page": "rss", "q": query, "c": CATEGORY, "f": FILTER})
        return f"{self._base_url}/?{params}"

    async def _pace(self) -> None:
        """Hold the gap between two requests open."""
        elapsed = _now() - self._last_request
        if self._last_request and elapsed < self._min_interval:
            await _sleep(self._min_interval - elapsed)
        self._last_request = _now()

    async def _fetch(self, url: str) -> str:
        """One GET, retried once on a 5xx or a transport failure."""
        for attempt in (1, 2):
            await self._pace()
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise NyaaUnavailable(f"nyaa request failed: {exc!r}") from exc
                log.warning("nyaa request failed, retrying", extra={"error": repr(exc)})
                await _sleep(RETRY_AFTER)
                continue
            if response.status_code >= 500:
                if attempt == 2:
                    raise NyaaUnavailable(f"nyaa answered {response.status_code}")
                log.warning("nyaa answered 5xx, retrying", extra={"status": response.status_code})
                await _sleep(RETRY_AFTER)
                continue
            if response.status_code >= 400:
                # A 4xx is final: asking again changes nothing.
                raise NyaaUnavailable(f"nyaa answered {response.status_code}")
            return response.text
        raise NyaaUnavailable("nyaa could not be reached")  # pragma: no cover - unreachable

    async def search(self, query: str) -> list[NyaaItem]:
        """The feed for ``query``, from the cache when it is fresh enough."""
        async with self._lock:
            cached = self._cache.get(query)
            now = _now()
            if cached is not None and now - cached[0] < self._cache_ttl:
                log.debug("nyaa cache hit", extra={"query": query})
                return cached[1]

            started = _now()
            items = parse_feed(await self._fetch(self.url_for(query)))
            self._cache[query] = (_now(), items)
            log.info(
                "nyaa search",
                extra={
                    "query": query,
                    "results": len(items),
                    "ms": round((_now() - started) * 1000),
                },
            )
            return items


#: The process-wide client and the URL it was built for. Module state rather
#: than a parameter on every caller: what it protects — the pacing gap and the
#: cache — is a property of the *process*, and a second instance would silently
#: double the request rate Nyaa sees.
_shared: NyaaClient | None = None
_shared_url: str | None = None


def shared_client(base_url: str) -> NyaaClient:
    """The one :class:`NyaaClient` this process uses, built on first use.

    Built lazily rather than at import: the API imports this module's filter
    without ever making a request, and a client built at import time would be
    an ``httpx.AsyncClient`` bound to whichever event loop happened to import
    it. There is no lock because there is nothing to await — construction is
    synchronous, so no other task can run in the middle of it.

    A different ``base_url`` replaces the instance, which only happens in
    tests; the old one is dropped rather than closed, because closing needs an
    ``await`` this signature does not have.
    """
    global _shared, _shared_url
    if _shared is None or _shared_url != base_url:
        _shared = NyaaClient(base_url)
        _shared_url = base_url
    return _shared


def reset_shared_client() -> NyaaClient | None:
    """Forget the shared client and hand it back, so a caller may close it."""
    global _shared, _shared_url
    client, _shared, _shared_url = _shared, None, None
    return client


async def close_shared_client() -> None:
    """Close the shared client, if one was ever built (worker shutdown)."""
    client = reset_shared_client()
    if client is not None:
        await client.aclose()


# --- Query building ---------------------------------------------------------


def pad(number: int, *, total_episodes: int | None = None) -> str:
    """``7`` → ``"07"``, and ``"007"`` once a show has 100 episodes or more."""
    width = 3 if total_episodes is not None and total_episodes >= LONG_SHOW_EPISODES else 2
    return str(number).zfill(width)


def anime_titles(anime: Anime) -> tuple[str, ...]:
    """Published titles then synonyms, deduplicated, blanks dropped.

    The order matters: :func:`queries` builds from the front of this and
    romaji is what release groups write.
    """
    names = [anime.title_romaji, anime.title_english, anime.title_native]
    names.extend(anime.synonyms or [])
    seen: dict[str, None] = {}
    for name in names:
        if isinstance(name, str) and name.strip():
            seen.setdefault(name.strip(), None)
    return tuple(seen)


def anime_season(anime: Anime) -> int | None:
    """The season this catalogue entry's own titles name, if any of them do."""
    for name in (anime.title_romaji, anime.title_english):
        if not name:
            continue
        marked = strip_season(name)
        if marked.season is not None:
            return marked.season
    return None


def _short_forms(name: str, season: int, padded: str) -> list[str]:
    """The three ways a group abbreviates a later season of ``name``.

    Nyaa ANDs every word of a query, so the *catalogue's* title is the worst
    possible thing to ask for: ``Mushoku Tensei III: Isekai Ittara Honki Dasu``
    matches only the releases that spell the whole subtitle out, and the most
    seeded 1080p file of that very episode — ``[SubsPlease] Mushoku Tensei S3 -
    11`` — is not one of them. So the franchise name is taken on its own
    (:attr:`~arc.services.library.parser.SeasonMark.base`) and the season is
    re-attached the three ways groups write it: ``S3``, ``III``, and not at
    all. The last is the broadest query of the set and the one that finds the
    most: it is a subset of every other release's words.
    """
    base = strip_season(name).base
    if not base:
        return []
    built = [f"{base} S{season} - {padded}"]
    roman = ROMAN_SEASONS.get(season)
    if roman:
        built.append(f"{base} {roman} - {padded}")
    built.append(f"{base} - {padded}")
    return built


def queries(anime: Anime, number: int) -> list[str]:
    """What to ask Nyaa for, best first, at most :data:`MAX_QUERIES`.

    ``"<title> - 07"`` first because that is how almost every group writes a
    weekly release, and because the dash is what keeps the query from matching
    a batch. Then the same for the english title, and then — for an entry whose
    own title names a season — the short forms of :func:`_short_forms`, because
    a later season is exactly where the catalogue's title and the release's
    name diverge. A show with no season marker needs none of them: its base
    *is* its title, and ``"<title> - 07"`` is already the broad form, so the
    bare ``"<title> 07"`` is the only fallback left for the groups that write
    no dash.

    All of these run (:func:`search_for_episode` merges them); the order is
    what decides which survive the cap, not which are worth asking.
    """
    romaji = anime.title_romaji
    english = anime.title_english
    padded = pad(number, total_episodes=anime.episodes)
    season = anime_season(anime)

    built: list[str] = []
    if romaji:
        built.append(f"{romaji} - {padded}")
    if english and english != romaji:
        built.append(f"{english} - {padded}")
    if season is None:
        if romaji:
            built.append(f"{romaji} {padded}")
    else:
        for name in (romaji, english):
            if name:
                built.extend(_short_forms(name, season, padded))

    seen: dict[str, None] = {}
    for query in built:
        seen.setdefault(" ".join(query.split()), None)
    return list(seen)[:MAX_QUERIES]


# --- Filtering --------------------------------------------------------------


def title_score(parsed_key: str, titles: Iterable[str]) -> float:
    """How close a parsed release title is to one of a show's titles, 0..1.

    Both sides have their season markers stripped and their punctuation
    flattened first (:func:`~arc.services.library.parser.title_key`), so
    ``Frieren: Beyond Journey's End`` and ``Frieren Beyond Journeys End`` are
    one name and ``Overlord IV`` and ``Overlord`` are one name with the season
    accounted for separately.

    **The comparison is asymmetric, and that is the whole point.**
    ``token_set_ratio`` scores 100 whenever one side's tokens contain the
    other's, and the two directions do not mean the same thing at all:

    * The release name being *shorter* is normal. Groups shorten official
      titles — ``[SubsPlease] Mushoku Tensei - 07`` is episode 7 of *Mushoku
      Tensei: Isekai Ittara Honki Dasu* — so a release whose tokens are a
      subset of the catalogue title's keeps ``token_set_ratio``, and keeps
      scoring 1.0.
    * The release name being *longer* is how the wrong show gets downloaded.
      ``Kimetsu no Yaiba - Yuukaku-hen`` contains every token of the
      season-one entry ``Kimetsu no Yaiba``, and under a symmetric
      ``token_set_ratio`` it scored 100 against it — so episode 7 of the
      *Entertainment District* sequel was a perfect match for episode 7 of the
      first season. A strict superset is therefore scored with
      ``token_sort_ratio``, which the extra words do lower.

    The one exception is a release that is longer because it wrote *more of
    this show's own names*: ``Kimetsu no Yaiba - Demon Slayer`` is the romaji
    title followed by the english one. So the leftover tokens are checked
    against the entry's own titles and synonyms first, and if one of them
    accounts for all of them the release is this show after all.
    """
    if not parsed_key:
        return 0.0
    keys = [key for key in (title_key(strip_season(name).title) for name in titles) if key]
    parsed_tokens = set(parsed_key.split())
    own_tokens = [set(key.split()) for key in keys]

    best = 0.0
    for other, other_tokens in zip(keys, own_tokens, strict=True):
        if other == parsed_key:
            return 1.0
        if other_tokens < parsed_tokens and not any(
            (parsed_tokens - other_tokens) <= tokens for tokens in own_tokens
        ):
            score = fuzz.token_sort_ratio(parsed_key, other)
        else:
            score = fuzz.token_set_ratio(parsed_key, other)
        best = max(best, score / 100.0)
    return best


@dataclass(frozen=True, slots=True)
class Candidate:
    """A release that survived the filter, with what the parser made of it."""

    item: NyaaItem
    parsed: ParsedName
    title_similarity: float

    @property
    def group(self) -> str | None:
        return self.parsed.group

    @property
    def resolution(self) -> str | None:
        return self.parsed.resolution


def acceptable(
    item: NyaaItem,
    *,
    titles: Sequence[str],
    number: int,
    season: int | None,
    threshold: float = TITLE_THRESHOLD,
) -> Candidate | None:
    """``item`` as a :class:`Candidate`, or ``None`` with a reason logged.

    Absolute numbering is deliberately **not** resolved here: episode 40 of a
    two-season franchise really is episode 15 of the sequel, but working that
    out needs the relation graph the matcher walks, and getting it wrong here
    means a wrong download rather than a review item. The exact number only —
    the matcher still has the offset rule for files that arrive anyway.
    """
    if item.remake:
        return None
    parsed = parse(item.title)
    if parsed.kind != "episode" or parsed.episode != number:
        return None
    if parsed.episode_end is not None:
        return None
    # Season 1 is what *both* sides mean when neither says otherwise, and the
    # comparison is symmetric because of it. A catalogue entry with no marker
    # is season 1 of that entry — AniList files each season as its own row —
    # and a release with no marker is season 1 of the show it names. So
    # ``Sousou no Frieren - 07`` is not episode 7 of *2nd Season*, any more
    # than ``… S2 - 07`` is episode 7 of the first.
    #
    # The cost is a later season whose groups do not label their releases at
    # all: nothing passes, and the episode ends up ``unavailable`` after the
    # retry window. That is the right way round. An episode Arc did not fetch
    # is visible and fixable; the wrong season on disk is linked by the
    # matcher's own prior and plays as if it were right.
    if (parsed.season or 1) != (season or 1):
        return None
    similarity = title_score(parsed.title_key, titles)
    if similarity < threshold:
        return None
    return Candidate(item=item, parsed=parsed, title_similarity=similarity)


def filter_items(
    items: Iterable[NyaaItem],
    *,
    titles: Sequence[str],
    number: int,
    season: int | None,
    threshold: float = TITLE_THRESHOLD,
) -> list[Candidate]:
    """Every acceptable item, in the order the feed gave them."""
    kept = []
    for item in items:
        candidate = acceptable(
            item, titles=titles, number=number, season=season, threshold=threshold
        )
        if candidate is not None:
            kept.append(candidate)
    return kept


# --- Ranking ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ranked:
    """One candidate, its sort key, and the sentences behind it (FR-A3)."""

    candidate: Candidate
    group_rank: int
    resolution_rank: int
    seeders: int
    trusted: bool
    reasons: tuple[str, ...] = field(default=())

    @property
    def item(self) -> NyaaItem:
        return self.candidate.item

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        """FR-A3's four rules, in the order the spec lists them."""
        return (self.group_rank, self.resolution_rank, -self.seeders, 0 if self.trusted else 1)


def _reasons(
    candidate: Candidate, rules: Rules, group_rank: int, resolution_rank: int
) -> list[str]:
    reasons: list[str] = []
    group = candidate.group
    if group and group_rank < len(rules.preferred_groups):
        reasons.append(f"group {group} is preference #{group_rank + 1}")
    elif group:
        reasons.append(f"group {group} is not on the preferred list")
    else:
        reasons.append("no release group in the name")

    resolution = candidate.resolution or "unknown resolution"
    if resolution_rank == 0:
        reasons.append(f"{resolution} is the preferred resolution")
    elif resolution_rank == 1:
        reasons.append(f"{resolution} is the fallback resolution")
    else:
        reasons.append(f"{resolution} is neither preferred nor fallback")

    reasons.append(f"{candidate.item.seeders} seeders")
    if candidate.item.trusted:
        reasons.append("trusted uploader")
    if rules.overridden:
        reasons.append("per-show rule override applied")
    return reasons


def rank(candidates: Iterable[Candidate], rules: Rules) -> list[Ranked]:
    """Order candidates by FR-A3's rules, best first, each with its reasons."""
    ranked = []
    for candidate in candidates:
        group_rank = rules.group_rank(candidate.group)
        resolution_rank = rules.resolution_rank(candidate.resolution)
        ranked.append(
            Ranked(
                candidate=candidate,
                group_rank=group_rank,
                resolution_rank=resolution_rank,
                seeders=candidate.item.seeders,
                trusted=candidate.item.trusted,
                reasons=tuple(_reasons(candidate, rules, group_rank, resolution_rank)),
            )
        )
    ranked.sort(key=lambda entry: entry.sort_key)
    return ranked


async def search_for_episode(
    client: NyaaClient,
    anime: Anime,
    number: int,
    rules: Rules,
    *,
    threshold: float = TITLE_THRESHOLD,
) -> list[Ranked]:
    """Run **every** :func:`queries` form, merge by info hash, filter and rank.

    This used to stop at the first query that produced any candidate, on the
    theory that the romaji ``- 07`` form is what the weekly release is named
    and a second request would only find the same files. That is true of a
    first season and false of every later one. Nyaa ANDs the words of a query,
    so ``Mushoku Tensei III: Isekai Ittara Honki Dasu - 11`` returns the eight
    releases that write the subtitle out — a real, plausible, *non-empty*
    answer — and the ranking then picks the best of those eight without ever
    seeing ``[SubsPlease] Mushoku Tensei S3 - 11``, which had three times the
    seeders and was simply not in the pool. A correct ranking over an
    incomplete pool looks exactly like a correct answer, which is what made it
    worth two seconds each to close.

    Merging is by ``info_hash`` because the queries overlap heavily by design
    and the same torrent comes back under several of them; the first feed to
    mention one wins, which keeps the best query's ordering at the front of the
    pool for anything the ranker leaves tied.
    """
    titles = anime_titles(anime)
    season = anime_season(anime)

    merged: dict[str, NyaaItem] = {}
    counts: list[int] = []
    for query in queries(anime, number):
        items = await client.search(query)
        before = len(merged)
        for item in items:
            merged.setdefault(item.info_hash, item)
        counts.append(len(items))
        log.debug(
            "nyaa query",
            extra={
                "query": query,
                "anime_id": anime.id,
                "number": number,
                "seen": len(items),
                "new": len(merged) - before,
            },
        )

    candidates = filter_items(
        merged.values(), titles=titles, number=number, season=season, threshold=threshold
    )
    ranked = rank(candidates, rules)
    log.info(
        "nyaa candidates",
        extra={
            "anime_id": anime.id,
            "number": number,
            "queries": len(counts),
            "per_query": counts,
            "merged": len(merged),
            "kept": len(candidates),
            "top": ranked[0].item.title if ranked else None,
            "reasons": list(ranked[0].reasons) if ranked else [],
        },
    )
    return ranked


def as_dict(ranked: Ranked) -> dict[str, Any]:
    """The chosen release, flattened for a log line or a job payload."""
    return {
        "title": ranked.item.title,
        "info_hash": ranked.item.info_hash,
        "group": ranked.candidate.group,
        "resolution": ranked.candidate.resolution,
        "seeders": ranked.item.seeders,
        "trusted": ranked.item.trusted,
        "size": ranked.item.size,
        "reasons": list(ranked.reasons),
    }


__all__ = [
    "CACHE_TTL",
    "CATEGORY",
    "MAX_QUERIES",
    "MIN_INTERVAL",
    "NYAA_NS",
    "ROMAN_SEASONS",
    "TITLE_THRESHOLD",
    "TRACKERS",
    "Candidate",
    "NyaaClient",
    "NyaaItem",
    "NyaaUnavailable",
    "Ranked",
    "acceptable",
    "anime_season",
    "anime_titles",
    "as_dict",
    "close_shared_client",
    "filter_items",
    "pad",
    "parse_feed",
    "queries",
    "rank",
    "reset_shared_client",
    "search_for_episode",
    "shared_client",
    "title_score",
]
