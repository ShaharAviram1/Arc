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
list. So is a release with **no seeders** (:data:`MIN_SEEDERS`): it is not a
worse candidate but a file that cannot be fetched at all, and taking one puts a
magnet into qBittorrent that sits there asking for its metadata until the stall
rule removes it hours later.

The season check reads a missing marker as **season 1**, on both sides. A
catalogue entry with no marker is season 1 of that entry — AniList files each
season as its own row, so ``Sousou no Frieren`` and ``Sousou no Frieren 2nd
Season`` are two shows with two episode 7s — and a release with no marker is
season 1 of the show it names. Reading either as "no opinion" is how episode 7
of the wrong season ends up on disk, and a wrong file is worse than no file:
the matcher's own prior would then link it to the episode Arc asked for and it
would play as though it were right.

**A film is a different question entirely** (:func:`is_single`). A ``MOVIE``
entry, or an OVA/ONA the catalogue gives one episode, has no episode number for
a query to carry and none for the filter to compare: the query is the bare
title and the check is "is this one release of this title", with the year as
the tie-break. Until 2026-09-14 Arc asked Nyaa for ``Servamp Movie: Alice in
the Garden - 01``.

**Some groups never restart the count** (:func:`absolute_offset`, 2026-09-17).
SubsPlease released *Jujutsu Kaisen* season 2 as ``- 25`` … ``- 47`` while the
catalogue numbers that entry 1–23, so ``Jujutsu Kaisen S2 - 01`` found nothing
and ``Jujutsu Kaisen - 01`` was season one's first episode. The offset is
**read off the catalogue, never guessed**: the episode counts of the entry's
``PREQUEL`` chain, walked through cached rows, and the whole feature declines
for an entry whose chain Arc cannot add up. Then ``- 25`` is asked for as well
as ``- 01``, and accepted as episode 1 — but only from a release that names no
season at all, and never while an explicitly season-marked release for the same
episode is in the same pool.

**A finished show that finds nothing is asked again by group**
(:func:`group_queries`, 2026-09-18). The feed answers the newest 75 matches of
a query and **cannot be paged at all** — ``&p=2`` and ``&p=3`` return byte for
byte what ``&p=1`` returns, verified against the live feed, and only the HTML
listing paginates. For an old show whose franchise kept going those 75 are the
franchise: episode 10 of *Kimetsu no Yaiba* (2019) came back under seven forms
as 91 merged results with not one season-one single among them — season 4,
season 5, an Infinity Castle rip, remakes and batches, every one correctly
rejected. The filter was right; the pool was three years too young. Since Nyaa
ANDs the words of a query, the way to reach what is behind those 75 is to ask
something **narrower**: ``Kimetsu no Yaiba - 10 HorribleSubs`` returns the 2019
singles. So a ``FINISHED`` entry whose ordinary forms left the pool under
:data:`ENOUGH_CANDIDATES` asks its top forms again with a group's name on the
end — the admin's :attr:`~arc.services.acquisition.rules.Rules.preferred_groups`
first, then :data:`DEEP_SEARCH_GROUPS` — stopping the moment there is enough to
rank and never spending more than :data:`MAX_REQUESTS` requests on one episode.

**A finished show with no single at all may be offered a batch**
(``acceptable(..., batches=True)``, FR-A11, 2026-09-18). For a show that
finished years ago the only seeded releases are often complete-season packs:
the same *Kimetsu no Yaiba* pool held several, and the filter threw every one
of them away before comparing the episode number, because FR-A4's "never fetch
whole seasons" is a non-negotiable. It is a statement about **bytes** rather
than about torrents, though, and a torrent whose contents are read before a
single one of them is fetched can be asked for one file. So a batch is a
*candidate* — never by default, only for a :func:`deep_search` entry, and only
when no acceptable single exists at all — carrying the episodes its name claims
in :attr:`Candidate.covers`, or nothing at all where its name claims none.
Which of its files are fetched is settled elsewhere (FR-A11); nothing in this
module downloads anything.

**Ranking is FR-A3's four rules in order**: preferred group, then resolution,
then seeders, then Nyaa's trusted flag — behind one rule that comes first, an
**English dub ranks below every subbed candidate** and is only ever chosen for
lack of anything else. Each pick records the reasons it won, because "why did
it choose that release?" is the first question anybody asks of an acquisition
system and a score with no explanation cannot answer it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
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

#: Most bytes a ``.torrent`` may be (:meth:`NyaaClient.torrent_file`). One
#: mebibyte: a file list is a few kilobytes per file and the piece hashes are
#: twenty bytes per piece, so a 50 GB batch of 26 files is well under a
#: hundred kilobytes. The ceiling is not about disk — the blob is posted
#: straight to qBittorrent and never stored — it is about what a feed's
#: ``<link>`` might point at if Nyaa ever answers something other than a
#: torrent, and about not holding an unbounded body in memory for it.
MAX_TORRENT_BYTES: Final[int] = 1024 * 1024

#: What a bencoded ``.torrent`` starts with: ``d``, the opening of the
#: top-level dictionary. A Cloudflare interstitial, an HTML error page and a
#: redirect to a login form all start with ``<`` or whitespace, and none of
#: them is something to hand a torrent client. This is a *shape* check and not
#: a parse: Arc never decodes the file (D3 of the plan — qBittorrent's own
#: ``torrents/files`` is authoritative about the indices ``filePrio`` takes),
#: so the only thing worth asserting here is that it is a bencoded dict at all.
BENCODE_DICT_PREFIX: Final[bytes] = b"d"

#: Fewest seeders a release may have and still be a candidate. One: a torrent
#: with nobody holding it cannot be downloaded, however well its name matches,
#: and the seeder count is a *filter* at zero and a ranking input above it
#: (FR-A3). Nyaa reports the figure in the feed, so this is a decision taken
#: before anything is asked of qBittorrent.
MIN_SEEDERS: Final[int] = 1

#: How close a parsed release title must be to one of the show's titles
#: (FR-A4). High, because the episode number and season have already been
#: checked by the time this is asked and the only thing left to get wrong is
#: the show itself.
TITLE_THRESHOLD: Final[float] = 0.90

#: Most queries a single search makes, however many the builder produced.
#: **Every one of them runs**, so this is the budget one episode's search may
#: spend: ten × 2 s of spacing, and rather less in practice because the cache
#: is shared and the broad forms repeat from episode to episode. Five
#: originally; six so that a title with a subtitle and no season marker keeps
#: its bare form *behind* the two head forms; eight so that the two ``SxxEyy``
#: forms (:func:`queries`) do not push the season short forms off the end of a
#: marked entry's list; ten for the symbol-stripped variants
#: (:func:`strip_symbols`), which sit behind the form they are derived from and
#: would otherwise push a marked entry's english short forms off the end. The
#: dedupe keeps a typical show at three or four regardless.
MAX_QUERIES: Final[int] = 10

#: How many acceptable releases in the merged pool are enough to stop looking.
#: Three: the ranker's job is to compare sources, and three of them give it
#: something to compare — a fourth costs two seconds of a volunteer-run site's
#: patience and changes the answer rarely. Counted with the real
#: :func:`filter_items`, so "acceptable" here means exactly what it means to
#: the pick.
ENOUGH_CANDIDATES: Final[int] = 3

#: Most requests one episode's search may make, title forms and narrowed forms
#: together. Ten title forms plus three forms × up to six groups is 28, which
#: is nearly a minute of pacing for one episode; 20 is 38 seconds of it, and
#: the narrowing is ordered so that the requests most likely to answer are the
#: ones inside the ceiling. It is only reached by a show where nothing has been
#: found yet — :data:`ENOUGH_CANDIDATES` stops the narrowing long before it on
#: anything that is working.
MAX_REQUESTS: Final[int] = 20

#: The airing status that earns the narrowed forms (:func:`deep_search`). Every
#: source normalises into AniList's vocabulary on the way in, so there is one
#: spelling to check. A null status reads as "not known to be finished" and
#: gets the ordinary forms only: that is the airing show's behaviour, and the
#: airing show's behaviour is the one that must not change.
DEEP_SEARCH_STATUS: Final[str] = "FINISHED"

#: The groups a **finished** show is asked for by name when its ordinary forms
#: find nothing (:func:`group_queries`, 2026-09-18). Between them these three
#: have subtitled nearly every simulcast since 2013 — HorribleSubs until 2020,
#: SubsPlease after it, Erai-raws throughout — so for an old show whose own
#: uploads are buried they are the names likeliest to be attached to the
#: episode that does exist: ``Kimetsu no Yaiba - 10 HorribleSubs`` returns the
#: 2019 singles that ``Kimetsu no Yaiba - 10`` cannot reach.
#:
#: A **constant and not a setting**, and it is the one call here worth
#: reviewing: three names hard-coded into the search are three claims about who
#: uploads anime, and they will age. They sit *behind* the admin's own
#: ``preferred_groups`` (which is where a fourth name belongs) and they are
#: de-duplicated against it, so an admin who prefers Judas asks Judas first and
#: never asks it twice.
DEEP_SEARCH_GROUPS: Final[tuple[str, ...]] = ("HorribleSubs", "SubsPlease", "Erai-raws")

#: The word a batch uploader writes, and the only one worth a query
#: (:func:`batch_queries`, 2026-09-18). ``[Erai-raws] … [BATCH]``,
#: ``[Judas] … [BATCH]``: it is in the name of most packs and in the name of
#: almost no single. ``Complete`` and ``Season Pack`` are the other two
#: spellings and neither earns a request — the *bare* form above it already
#: reaches those uploads, and each extra word is two more seconds of a
#: volunteer-run site's patience spent on a guess about wording.
BATCH_WORD: Final[str] = "BATCH"

#: Most forms one search may spend on finding a pack: the bare title, the
#: title with :data:`BATCH_WORD`, and the english title with it. Three,
#: because these are asked **only** when everything else has failed — a
#: finished show with no acceptable single at all — and they are asked after
#: the narrowed forms have had their turn.
#:
#: It is also the **reservation**: the group narrowing stops this many
#: requests short of :data:`MAX_REQUESTS` for any entry that could still ask
#: for a pack (:func:`search_for_episode`, owner 2026-09-18). A show with ten
#: title forms and eighteen narrowed ones would otherwise spend the entire
#: budget looking for a single, and that is exactly the show whose only
#: seeded releases are packs.
MAX_BATCH_QUERIES: Final[int] = 3

#: Punctuation a release group drops and a catalogue keeps. Every one of these
#: either glues two words into one token (``Yarichin☆Bitch-bu``) or hangs off
#: the end of one (``Love Live! Superstar!!``), and Nyaa's search ANDs *tokens*
#: — so ``Yarichin☆Bitch-bu - 01`` finds nothing while ``Yarichin Bitch-bu -
#: 01`` finds the show. The ordinary hyphen is **not** here: it is what
#: separates the episode number from the title in almost every query form.
#: The slash is, because a franchise written with one is written both ways —
#: *Fate/Zero* and *Fate Zero* are the same show on Nyaa.
#:
#: **Not** the same set as the parser's
#: :data:`~arc.services.library.parser._PUNCT_RE`, and deliberately so: that
#: one flattens *every* non-alphanumeric character on both sides of a
#: comparison, which is right for a comparison and wrong for a query. This set
#: is the punctuation a release group is known to drop from a name it types,
#: and each character here costs a real request to Nyaa — so it is a short,
#: evidenced list rather than "everything that is not a letter". A title whose
#: punctuation differs only in ways *this* set does not cover is still matched
#: by the filter, because the filter compares ``title_key``s.
SYMBOLS: Final[str] = "☆★♪♥!?:;~〜～·・—/"
_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(f"[{re.escape(SYMBOLS)}]+")

#: Formats that have **one** thing to fetch and no episode number to ask for.
#: ``MOVIE`` whatever its episode count (a two-part film is still asked for by
#: name), and the short forms only when the catalogue says there is exactly one
#: episode: an OVA *series* of four is four numbered releases like any other.
SINGLE_FORMATS: Final[frozenset[str]] = frozenset({"OVA", "ONA", "SPECIAL"})

#: The parser kinds a **single** may be (:func:`acceptable`). A film names
#: itself one (``Movie``, ``Gekijouban``) and a bonus episode names itself the
#: other; a release with *no episode number and no marker at all* is neither,
#: and that is the point of the list. ``[Judas] Sword Art Online [BD 1080p]``,
#: ``[Coalgirls] Servamp (1920x1080 Blu-ray FLAC)`` and ``[Coalgirls]
#: Kizumonogatari [BD 1080p]`` are whole-series Blu-ray packs: they parse as
#: ``kind=unknown`` with no number, no range and no batch marker, so before
#: 2026-09-14 each of them was "an episode-less single" — 20 GB of a franchise
#: offered as one film, with a *shorter* title than the entry's and therefore a
#: perfect score under the ordinary asymmetric comparison.
SINGLE_KINDS: Final[frozenset[str]] = frozenset({"movie", "special"})

#: The only words a release may drop from a single's title and still be it
#: (:func:`title_score`'s ``strict`` mode). The parser takes a trailing type
#: marker off the title it reports — ``[Moozzi2] The Royal Tutor Movie`` parses
#: as ``the royal tutor`` — while the catalogue keeps it, so the *type* word is
#: a difference that means nothing. Every other missing word means the release
#: named a different film of the franchise.
TYPE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "movie",
        "movies",
        "gekijouban",
        "gekijoban",
        "film",
        "ova",
        "oav",
        "ona",
        "special",
        "specials",
        "the",
    }
)

#: How far a release's year may sit from the entry's ``season_year`` and still
#: be the same thing (:func:`acceptable`, singles only). One: a film that
#: premiered in December is a BD in January, and a group writes whichever year
#: it has in mind.
YEAR_SLACK: Final[int] = 1

#: The floor a synonym has to clear to earn a query of its own: two words, or
#: six characters. The list is somebody else's free-text field and it holds
#: entries like ``"2"`` — which is not a query for this show but a query for a
#: quarter of Nyaa, and it would cost a slot one of the title forms could have
#: had.
MIN_SYNONYM_WORDS: Final[int] = 2
MIN_SYNONYM_CHARS: Final[int] = 6

#: What a synonym may not consist of: a season marker and nothing else.
#: Matched against the whole of the synonym, so ``"Season 2"``, ``"Part 2"``
#: and ``"S2"`` are dropped while ``"Overlord Season 2"`` is not.
_SEASON_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:season|series|part|cour|s|ep|episode)\b|\d+(?:st|nd|rd|th)?|\b[IVX]+\b",
    re.IGNORECASE,
)

#: Most synonyms that may earn a query of their own (FR-A4). Two, and they are
#: counted against :data:`MAX_QUERIES` **last**: a synonym is a name somebody
#: once wrote for this show, and the manami vocabulary carries five or six of
#: them for a popular franchise.
MAX_SYNONYM_QUERIES: Final[int] = 2

#: Season numbers as a release group writes them in a title. Only up to 5:
#: past that nobody uses numerals, and ``I`` is never written at all.
ROMAN_SEASONS: Final[dict[int, str]] = {2: "II", 3: "III", 4: "IV", 5: "V"}

#: Episode numbers are padded to two digits, or to three once a show is long
#: enough that groups start writing ``- 105``.
LONG_SHOW_EPISODES: Final[int] = 100

#: What separates a title from its subtitle: a colon, an en/em dash or a tilde
#: (both widths). **Whitespace on one side is part of the definition.** The
#: colon of ``Rakudai Kenja no Gakuin Musou: Nidome no Tensei`` is glued to the
#: word in front of it and followed by a space, and it does separate; the colon
#: of ``Re:Zero kara Hajimeru Isekai Seikatsu`` has whitespace on neither side
#: and is part of the name, which matters because the head it would otherwise
#: produce is ``Re`` — a query worth two seconds of Nyaa's patience and nothing
#: else. ``re.search`` finds the leftmost match whichever alternative it is, so
#: this is the *first* separator of the title.
SUBTITLE_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"\s[-–—]\s|\s[:~〜]|[:~〜]\s")

#: Trailing punctuation a head is trimmed of, the same set
#: :func:`~arc.services.library.parser.strip_season` uses, plus the wide
#: dashes and tilde.
_TRIM: Final[str] = " \t._-~:–—〜"

#: The relation type that disqualifies an entry from the head forms. Stored in
#: ``anime.relations`` blobs under ``relation_type`` in AniList's vocabulary —
#: MAL's ``lower_snake`` is upper-cased on the way in — and read the same way
#: the matcher and the recommender read it.
PREQUEL_RELATION: Final[str] = "PREQUEL"

#: Prequel formats whose episodes a release group **counts** when it numbers a
#: sequel absolutely (:func:`absolute_offset`). The assumption, written down
#: because it is an assumption: a group continuing the count of *Jujutsu
#: Kaisen* counts the television seasons and nothing else.
ABSOLUTE_COUNTED_FORMATS: Final[frozenset[str]] = frozenset({"TV", "ONA"})

#: And the ones it **skips**: a film, an OVA and a bonus special sit beside the
#: run rather than inside it. ``[SubsPlease] Jujutsu Kaisen - 25`` is episode 1
#: of season 2 after season one's 24 — *Jujutsu Kaisen 0*, a ``PREQUEL`` edge of
#: the same entry, is not one of them.
ABSOLUTE_SKIPPED_FORMATS: Final[frozenset[str]] = frozenset({"MOVIE", "OVA", "SPECIAL", "MUSIC"})

#: The only airing status a prequel may carry and still be counted. Every
#: source normalises into AniList's vocabulary on the way in (MAL's
#: ``finished_airing`` and manami's own word both become this), so there is one
#: spelling to check. A ``RELEASING`` prequel's ``episodes`` is an **announced**
#: total, which is the number likeliest to be wrong — a twelve-episode cour
#: that runs to thirteen, a split season counted whole — and being one out here
#: lands the absolute number inside the prequel's own band, where the season
#: check cannot catch it. A null status is no evidence and declines too.
ABSOLUTE_FINISHED_STATUS: Final[str] = "FINISHED"

#: How far back an offset may be walked before the chain is called a franchise
#: and abandoned. Ten is far more than any real series and exists only so a
#: relation graph that loops cannot spin: the cycle check below catches the
#: ordinary loop, and this catches the pathological one.
MAX_PREQUEL_HOPS: Final[int] = 10

#: How a prequel row is fetched: by AniList id, then by MAL id, answering
#: ``None`` when Arc has never cached it. Injected rather than imported,
#: because everything else in this module is a pure function of an ``anime``
#: row and a release name — which is what lets the query corpus run offline.
PrequelResolver = Callable[[int | None, int | None], Awaitable["Anime | None"]]


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
        """The RSS URL for ``query`` (architecture.md §6).

        One URL per query and no ``p``: **the RSS feed does not paginate**
        (verified 2026-09-18). ``&p=2`` and ``&p=3`` answer byte for byte what
        ``&p=1`` answers, and so do ``s=``/``o=`` — only the HTML listing pages
        and sorts. What gets past the first 75 results is therefore a *narrower
        query* (:func:`group_queries`), never a deeper one.
        """
        params = urlencode({"page": "rss", "q": query, "c": CATEGORY, "f": FILTER})
        return f"{self._base_url}/?{params}"

    async def _pace(self) -> None:
        """Hold the gap between two requests open."""
        elapsed = _now() - self._last_request
        if self._last_request and elapsed < self._min_interval:
            await _sleep(self._min_interval - elapsed)
        self._last_request = _now()

    async def _fetch(self, url: str) -> httpx.Response:
        """One GET, retried once on a 5xx or a transport failure.

        The **response** rather than its text, because two callers want two
        different halves of it: :meth:`search` reads ``.text`` (an RSS
        document, whose encoding httpx works out) and :meth:`torrent_file`
        reads ``.content`` (a bencoded blob, where a decode would be damage).
        """
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
            return response
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
            items = parse_feed((await self._fetch(self.url_for(query))).text)
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

    async def torrent_file(self, url: str) -> bytes:
        """The ``.torrent`` behind one RSS item. Paced like a search, never cached.

        The one request in this module that is not a search, and it exists for
        the byte guarantee behind FR-A11 (plan §4): qBittorrent refuses
        ``torrents/filePrio`` while it has no metadata, so a batch added as a
        *magnet* has an unavoidable window in which unwanted files are fetched.
        Handing the client the file itself means the metadata is there at add
        time and the priorities can be written before a single byte moves.

        Five guards, and each of them closes a way for a feed to point Arc
        somewhere it should not go:

        * the URL must be on **this client's own host** — ``link`` is a string
          out of somebody else's XML, and a request Arc makes because a feed
          told it to is a request to an arbitrary address otherwise. The
          trailing slash is part of the check, so ``https://nyaa.si.example``
          does not pass for ``https://nyaa.si``;
        * and the URL the answer **came from** must be on that host too. The
          client follows redirects (it has to: Nyaa moves ``/download`` about),
          so the first check is a statement about where Arc *asked* and this
          one is a statement about who *answered* — a 302 to anywhere is
          otherwise the same request to an arbitrary address with an extra hop;
        * ``Content-Length``, where the answer carries one, must be within
          :data:`MAX_TORRENT_BYTES`, so a body Arc will refuse anyway is
          refused on its header rather than after it has been held in memory;
        * the body must be no larger than :data:`MAX_TORRENT_BYTES` — the
          header is a claim and this is the measurement;
        * and it must begin with :data:`BENCODE_DICT_PREFIX`, which an HTML
          interstitial does not.

        Not cached, unlike :meth:`search`: a blob is asked for exactly once,
        immediately before it is added, and the cache exists to spare Nyaa the
        *repeated* question rather than to hold megabytes of payload. It does
        take the same lock, so the two-second gap covers it like everything
        else — a ``.torrent`` fetch is counted in ``Search.requests``' budget
        by the caller.
        """
        if not self._is_mine(url):
            raise NyaaUnavailable(f"refusing a torrent URL off nyaa's own host: {url!r}")
        async with self._lock:
            response = await self._fetch(url)
        if not self._is_mine(str(response.url)):
            raise NyaaUnavailable(
                f"a torrent URL redirected off nyaa's own host: {str(response.url)!r}"
            )
        claimed = response.headers.get("content-length")
        if claimed is not None and claimed.strip().isdigit() and int(claimed) > MAX_TORRENT_BYTES:
            raise NyaaUnavailable(f"torrent file claims {claimed} bytes, over {MAX_TORRENT_BYTES}")
        blob = response.content
        if len(blob) > MAX_TORRENT_BYTES:
            raise NyaaUnavailable(f"torrent file is {len(blob)} bytes, over {MAX_TORRENT_BYTES}")
        if not blob.startswith(BENCODE_DICT_PREFIX):
            raise NyaaUnavailable("nyaa did not answer with a bencoded torrent")
        log.info("nyaa torrent file", extra={"url": url, "bytes": len(blob)})
        return blob

    def _is_mine(self, url: str) -> bool:
        """Whether ``url`` is on the host this client was built for."""
        return url.startswith(f"{self._base_url}/")


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


def strip_symbols(text: str) -> str:
    """``"Yarichin☆Bitch-bu - 01"`` → ``"Yarichin Bitch-bu - 01"``.

    Every character of :data:`SYMBOLS` becomes a space and the runs collapse,
    which is the right answer for both shapes the problem takes: a symbol
    *between* two words has to become a separator (``Yarichin☆Bitch-bu``, and
    the same for ``Re:Zero``), and one *after* a word simply goes (``Love
    Live! Superstar!!``). Nyaa ANDs the tokens of a query, so a title Arc asks
    for with the star in it matches only the uploads that wrote the star.
    """
    return " ".join(_SYMBOL_RE.sub(" ", text).split())


def is_single(anime: Anime) -> bool:
    """Whether this entry is **one** release with no episode number (FR-A4).

    A film, an OVA or an ONA that the catalogue gives exactly one episode. It
    is the question the query builder and the filter both have to ask, because
    every form and every check below them is written for a numbered weekly
    release and none of them is true of a film:
    ``Servamp Movie: Alice in the Garden - 01`` is a query with no answer, and
    *Servamp Movie: Alice in the Garden* is one with nine. Three entries sat in
    ``searching`` for a day on production for exactly that reason.

    ``episodes == 1`` guards the OVA half only. A ``MOVIE`` entry is a single
    whatever its count — a two-part film is two rows in the catalogue and each
    of them is asked for by name — while an OVA *series* of four is four
    numbered releases like any other show.
    """
    fmt = (anime.format or "").strip().upper()
    if fmt == "MOVIE":
        return True
    return fmt in SINGLE_FORMATS and anime.episodes == 1


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


def _sxxexx_form(name: str, season: int, number: int) -> str:
    """``"One-Room TA S01E01"`` — the way a Western-style group writes it.

    The third thing Nyaa's word-ANDing breaks, after the season marker
    (:func:`_short_forms`) and the subtitle (:func:`head_of`), and the one that
    is not a *title* problem at all: ToonsHub, geckyzz, the dub groups and
    Erai-raws' alternate naming write the number as ``S01E02`` rather than as
    ``- 02``, and ``01`` is not a word of ``S01E02``. So ``One-Room TA - 01``
    and ``One-Room TA 01`` each returned nothing on 2026-09-13 while
    ``One-Room TA`` returned nine releases, seven of them the episodes Arc was
    looking for.

    Built from the season-stripped base for the same reason the short forms
    are — ``Mushoku Tensei III: Isekai Ittara Honki Dasu`` asks ``Mushoku
    Tensei S03E11`` — and the season is the one the entry's own title names, or
    1 when it names none, which is the same reading :func:`acceptable` applies
    on both sides.

    The episode is padded to **two** digits here rather than through
    :func:`pad`: ``SxxEyy`` is a scene convention with its own width, and a
    long show writes ``S01E1089`` (One Piece), never ``S01E089``.

    Unlike the head forms this needs no :func:`has_prequel` gate, and for two
    reasons. It is built from the *base* rather than the head, so a subtitled
    sequel is asked for by its whole name (``Made in Abyss: Retsujitsu no
    Ougonkyou S01E07``) and cannot reach the first season's releases at all;
    and where the base *is* bare — a marked entry, ``Sousou no Frieren
    S02E07`` — the form carries the season explicitly, which is exactly what
    :func:`acceptable`'s season check reads.
    """
    base = strip_season(name).base
    if not base:
        return ""
    return f"{base} S{season:02d}E{number:02d}"


def has_prequel(anime: Anime) -> bool:
    """Whether the catalogue says something comes *before* this entry.

    The gate on the head forms, and the reason is the one thing a head query
    cannot defend itself against. A release named by the bare head — ``[Group]
    Made in Abyss - 07`` — is almost always the **first** season, and for a
    sequel whose own title marks nothing but the arc (*Made in Abyss:
    Retsujitsu no Ougonkyou*, *Kimetsu no Yaiba: Yuukaku-hen*) there is no
    season marker for :func:`acceptable` to disagree with, while
    :func:`title_score` reads the shorter name as the ordinary case of a group
    abbreviating an official title. So the two checks that make every other
    broad form safe both pass, and episode 7 of season 1 lands as episode 7 of
    the sequel. A marked sequel (*Sousou no Frieren 2nd Season*) is not at
    risk, and does not reach the head forms anyway.

    **No relations is not evidence.** An offline-sourced row and a row nobody
    has opened the detail of both carry ``None`` here, and refusing them the
    head forms would withhold the fix from exactly the shows that need it —
    the ones Arc knows least about. They keep it; the 0.90 filter and the
    season check still apply.
    """
    return any(
        isinstance(relation, dict)
        and str(relation.get("relation_type") or "").upper() == PREQUEL_RELATION
        for relation in anime.relations or ()
    )


def _prequel_edges(anime: Anime) -> list[dict[str, Any]]:
    """Every ``PREQUEL`` blob of this entry, in the order the source wrote them."""
    return [
        relation
        for relation in anime.relations or ()
        if isinstance(relation, dict)
        and str(relation.get("relation_type") or "").upper() == PREQUEL_RELATION
    ]


def _row_key(anime: Anime) -> tuple[int | None, int | None]:
    """What identifies a row while the chain is being walked."""
    return (anime.anilist_id, anime.mal_id)


async def absolute_offset(anime: Anime, resolve: PrequelResolver) -> int | None:
    """How many episodes ran **before** this entry, or ``None`` to decline.

    Some groups never restart the count. SubsPlease released *Jujutsu Kaisen*
    season 2 as ``[SubsPlease] Jujutsu Kaisen - 25 (1080p)`` through ``- 47``,
    while the catalogue entry for that season numbers its episodes 1–23. So
    ``Jujutsu Kaisen S2 - 01`` matched nothing on Nyaa and ``Jujutsu Kaisen -
    01`` was season *one's* first episode: the episode was either not fetched
    or fetched wrong, and the second of those is the outcome CLAUDE.md forbids.

    The offset is the sum of the episode counts of the entry's ``PREQUEL``
    chain, **taken from the catalogue and never guessed**, which is the whole
    difference between this and the arithmetic a person does in their head.
    Every hop must be a row Arc has cached, with a format that says the group
    counted it and an episode count to add; anything else returns ``None`` and
    the entry simply keeps the behaviour it had before 2026-09-17. A feature
    that declines is a missing file, and a missing file is visible and fixable.

    Declined, therefore, when:

    * the entry is a **single** (:func:`is_single`) — a film has no count to
      continue;
    * a prequel edge is not one of :data:`ABSOLUTE_SKIPPED_FORMATS` and Arc has
      **no cached row** for it, so its length is unknown;
    * that row's ``episodes`` is null or zero, or its format is neither counted
      nor skipped — a ``TV_SHORT``, a ``MUSIC`` video, a row with no format at
      all: guessing which side of the line it falls on is guessing the offset;
    * that row has **not finished airing** (:data:`ABSOLUTE_FINISHED_STATUS`),
      because a ``RELEASING`` season's episode count is an announcement rather
      than a fact, and an announcement that turns out one short puts the
      absolute number inside the prequel's own run — where the season check
      cannot see it and the file that lands is the wrong episode;
    * **two** prequel edges survive the format filter at one hop, because
      which of them the group was counting is exactly the thing that must not
      be inferred;
    * the chain revisits a row (AniList's graph does contain loops) or runs
      past :data:`MAX_PREQUEL_HOPS`.

    Films, OVAs and specials in the chain are **skipped rather than declined**
    (:data:`ABSOLUTE_SKIPPED_FORMATS`): *Jujutsu Kaisen 0* is a ``PREQUEL`` edge
    of season 2 and no group has ever counted it, so an entry whose only other
    prequel is season one is answered rather than abandoned. The walk stops
    there — it does not reach around a film for whatever precedes it — which is
    the conservative half of the same decision.

    Returns a positive number of episodes, or ``None``. Zero is never returned:
    "nothing came before" and "do not use this" are the same answer here.
    """
    if is_single(anime):
        return None

    total = 0
    hops = 0
    current = anime
    seen: set[tuple[int | None, int | None]] = {_row_key(anime)}

    while True:
        edges = [
            edge
            for edge in _prequel_edges(current)
            if str(edge.get("format") or "").upper() not in ABSOLUTE_SKIPPED_FORMATS
        ]
        if not edges:
            return total or None
        if len(edges) > 1:
            log.debug(
                "absolute numbering declined: two prequels at one hop",
                extra={"anime_id": anime.id, "edges": len(edges)},
            )
            return None
        hops += 1
        if hops > MAX_PREQUEL_HOPS:
            log.debug(
                "absolute numbering declined: the prequel chain is too long",
                extra={"anime_id": anime.id, "hops": MAX_PREQUEL_HOPS},
            )
            return None

        row = await resolve(edges[0].get("anilist_id"), edges[0].get("mal_id"))
        if row is None:
            log.debug(
                "absolute numbering declined: a prequel is not cached",
                extra={"anime_id": anime.id, "relation": edges[0].get("anilist_id")},
            )
            return None

        fmt = str(row.format or edges[0].get("format") or "").upper()
        if fmt in ABSOLUTE_SKIPPED_FORMATS:
            # The edge carried no format and the row turns out to be a film.
            return total or None
        if fmt not in ABSOLUTE_COUNTED_FORMATS or not row.episodes or row.episodes <= 0:
            log.debug(
                "absolute numbering declined: a prequel cannot be counted",
                extra={"anime_id": anime.id, "format": fmt, "episodes": row.episodes},
            )
            return None
        if str(row.status or "").upper() != ABSOLUTE_FINISHED_STATUS:
            # An airing prequel's count is a promise rather than a fact
            # (:data:`ABSOLUTE_FINISHED_STATUS`), and one episode out here is a
            # number that lands *inside* the prequel's own run.
            log.debug(
                "absolute numbering declined: a prequel has not finished airing",
                extra={"anime_id": anime.id, "status": row.status},
            )
            return None
        if _row_key(row) in seen:
            log.debug(
                "absolute numbering declined: the prequel chain loops",
                extra={"anime_id": anime.id},
            )
            return None

        seen.add(_row_key(row))
        total += row.episodes
        current = row


def head_of(name: str) -> str:
    """The title in front of its subtitle, or ``""`` when there is no subtitle.

    The other half of :func:`_short_forms`' problem, and the half a season
    marker does not cause. Nyaa ANDs every word, and a release group writes the
    *head* of a title and stops: *Rakudai Kenja no Gakuin Musou: Nidome no
    Tensei, S-Rank Cheat Majutsushi Bouken-roku* is nine words in the catalogue
    and ``[SubsPlease] Rakudai Kenja no Gakuin Musou - 01`` on Nyaa, so asking
    for the catalogue's title returns nothing at all — and so does asking for
    the english one, whose head nobody writes either.

    Returned only when it is non-empty and strictly shorter than what was
    handed in, so a title with no subtitle produces no second query for the
    dedupe to drop.
    """
    found = SUBTITLE_SEPARATOR.search(name)
    if found is None:
        return ""
    head = name[: found.start()].strip(_TRIM)
    return head if len(head) < len(name) else ""


def _synonym_forms(anime: Anime) -> list[str]:
    """Up to :data:`MAX_SYNONYM_QUERIES` synonyms worth a query of their own.

    AniList's ``synonyms`` are where a show's *other* real names live — the
    Japanese-market abbreviation, the streaming service's title, the name the
    manga was licensed under — and a release group writes one of those as often
    as it writes the catalogue's romaji. They come last and they are few,
    because the list is also where a dozen transliterations of the same three
    words live, and each one costs two seconds of Nyaa's patience.

    A synonym is only kept when it says something the title forms have not
    already said: its season-stripped name and its head
    (:func:`head_of`) are both compared against the same two forms of the
    romaji and english titles, so *Mushoku Tensei: Isekai Ittara Honki Dasu 3rd
    Season* — whose head is the ``Mushoku Tensei`` the short forms already ask
    for — earns nothing.

    And it has to be a **name** (:data:`MIN_SYNONYM_WORDS`,
    :data:`MIN_SYNONYM_CHARS`). The synonym list is a free-text field in
    somebody else's database and it carries entries like ``"Season 2"``,
    ``"2"`` and ``"Part 2"`` — a query for none of which is a query for this
    show, and one of which is a query for a quarter of Nyaa. Two words, or six
    characters, and never something :func:`~arc.services.library.parser.
    strip_season` reads as nothing but a season marker. The floor also drops a
    short native-script name (``進撃の巨人`` is five characters), which is the
    same decision :func:`queries` already makes about ``title_native``: it is
    one of the names the *filter* compares against (:func:`anime_titles`) and
    not one of the names Arc asks the english-translated category for.
    """
    covered: set[str] = set()
    for name in (anime.title_romaji, anime.title_english):
        if not name:
            continue
        base = strip_season(name).base or name
        for form in (name, base, head_of(base)):
            key = title_key(form)
            if key:
                covered.add(key)

    kept: list[str] = []
    for synonym in anime.synonyms or ():
        if not isinstance(synonym, str) or not synonym.strip():
            continue
        marked = strip_season(synonym.strip())
        base = marked.base or marked.title
        if not base:
            # Nothing but a season marker: ``strip_season("Season 2")`` keeps
            # the words (there is no head for the marker to hang off) but
            # ``"2"`` and ``"II"`` reduce to nothing at all.
            continue
        if len(base.split()) < MIN_SYNONYM_WORDS and len(base) < MIN_SYNONYM_CHARS:
            continue
        if not title_key(_SEASON_ONLY_RE.sub(" ", base)):
            # ``"Season 2"``, ``"Part 2"``, ``"S2"``: a season and nothing to
            # attach it to.
            continue
        head = head_of(base)
        keys = {title_key(base)} | ({title_key(head)} if head else set())
        if not any(keys) or keys & covered:
            continue
        covered |= keys
        kept.append(base)
        if len(kept) == MAX_SYNONYM_QUERIES:
            break
    return kept


def queries(anime: Anime, number: int, *, offset: int | None = None) -> list[str]:
    """What to ask Nyaa for, best first, at most :data:`MAX_QUERIES`.

    ``offset`` is :func:`absolute_offset`'s answer — the episodes that ran
    before this entry — and when there is one, two more forms are built from
    the season-stripped base with the **absolute** number on them: ``Jujutsu
    Kaisen - 25`` and ``Jujutsu Kaisen 25`` for episode 1 of a second season
    that follows 24. They sit directly behind the romaji short forms, because
    that is the shape of the name the group that numbers this way writes
    (SubsPlease writes exactly ``[SubsPlease] Jujutsu Kaisen - 25 (1080p)``),
    and the number is padded against the *combined* length of the franchise so
    that a show which passes 100 episodes across its seasons is asked for the
    way a 100-episode show is written. They are built **only where stripping
    the season marker actually shortened the title**: an unmarked sequel has no
    marker to strip, so its "base" is its whole nine-word name, and nine words
    followed by an absolute number is a form nobody writes.

    **A film, an OVA or an ONA with one episode** (:func:`is_single`) is a
    different list and a short one: the bare titles, their symbol-stripped
    variants and up to two synonyms, with no number attached to any of them.
    Nothing on Nyaa writes ``Servamp Movie: Alice in the Garden - 01``, so
    every numbered form was a query with no answer and three films sat in
    ``searching`` for a day on production because of it.

    Otherwise, in order — and **the order is what the cap cuts**, so it runs
    from the form a group is likeliest to have written to the most speculative:

    1. ``"<romaji> - 07"``, because that is how almost every group writes a
       weekly release, and because the dash is what keeps the query from
       matching a batch.
    2. ``"<english> - 07"``, the same for the english title.
    3. ``"<romaji> S01E07"`` (:func:`_sxxexx_form`), the same title with the
       number written the Western way — which is what ToonsHub, geckyzz, the
       dub groups and Erai-raws' alternate naming write, and which the ``- 07``
       forms cannot find because Nyaa ANDs the words of a query and ``07`` is
       not a word of ``S01E07``. Third rather than last: a show whose groups
       name it this way has *nothing* under the two forms in front of it.
    4. For an entry whose own title names a season, the **romaji** short forms
       of :func:`_short_forms` — a later season is where the catalogue's title
       and the release's name diverge most, and romaji is what groups write.
    5. ``"<head of romaji> - 07"`` and ``"<head of english> - 07"``, where
       :func:`head_of` found a subtitle to drop **and** :func:`has_prequel`
       found nothing in front of the entry. A group names a show by its head:
       nothing on Nyaa spells *… Musou: Nidome no Tensei, S-Rank Cheat
       Majutsushi Bouken-roku* out, so every query built from the whole title
       returns zero results while nine releases sit there under the head.
    6. ``"<romaji> 07"``, the bare form, for the groups that write no dash.
       Only for an entry with no season marker: a marked one has spent its
       budget on the short forms, which are broader and likelier.
    7. Then the **english** ``SxxEyy`` form and the english short forms. Same
       two ideas as 3 and 4 in the language a group writes second.
    8. Then the **symbol-stripped variants of the two full titles** (
       :func:`strip_symbols`): ``Yarichin☆Bitch-bu - 01`` is followed by
       ``Yarichin Bitch-bu - 01``, ``Love Live! Superstar!! - 03`` by ``Love
       Live Superstar - 03`` and ``Fate/Zero - 12`` by ``Fate Zero - 12``,
       because a symbol glued between two words makes one token out of both and
       Nyaa matches tokens. Only the *full* forms earn one: a variant of an
       abbreviation is a guess about a guess, and those slots are better spent
       on the short forms above — which is exactly what the first version of
       this got wrong, putting each variant beside its own form and cutting
       *Kimetsu no Yaiba: Katanakaji no Sato-hen 2nd Season*'s short forms off
       the end of the list.
    9. ``"<synonym> - 07"`` for up to :data:`MAX_SYNONYM_QUERIES` of the
       entry's other names (:func:`_synonym_forms`), last because a synonym is
       a name somebody once wrote rather than a name a group writes.

    A broad head query is safe because of what happens *after* it: the release
    name still has to parse as this episode, agree about the season, and reach
    :data:`TITLE_THRESHOLD` against one of the entry's own titles under the
    asymmetric :func:`title_score`. A release that merely shares the head
    carries tokens the entry never wrote and is rejected there. The one thing
    those two cannot catch is an *unmarked sequel* being offered its first
    season, which is what :func:`has_prequel` is for.

    All of these run (:func:`search_for_episode` merges them); the cap is the
    politeness budget, not an opinion about which are worth asking.
    """
    romaji = anime.title_romaji
    english = anime.title_english
    padded = pad(number, total_episodes=anime.episodes)
    season = anime_season(anime)
    single = is_single(anime)

    built: list[str] = []

    def add(query: str) -> None:
        """One form, normalised, in the order it was asked for."""
        normalised = " ".join(query.split())
        if normalised:
            built.append(normalised)

    def add_variant(query: str) -> None:
        """The same form with its symbols taken out, when that changes it."""
        stripped = strip_symbols(" ".join(query.split()))
        if stripped and stripped != " ".join(query.split()):
            add(stripped)

    if single:
        # No cap pressure here — two titles and two synonyms — so the variant
        # stays beside the form it comes from, which is also where it does the
        # most good: ``Servamp Movie Alice in the Garden`` is the query that
        # finds the film, and the colon is the only reason the first one did
        # not.
        for name in (romaji, english):
            if name:
                add(name)
                add_variant(name)
        for synonym in _synonym_forms(anime):
            add(synonym)
            add_variant(synonym)
    else:
        # **The order is the cap**, so the forms that a group is likeliest to
        # have written come first and the speculative ones are what falls off
        # the end (2026-09-14). Romaji leads every pair: it is what release
        # groups write. A marked, subtitled title — *Kimetsu no Yaiba:
        # Katanakaji no Sato-hen 2nd Season* — has fourteen forms and ten
        # slots, and the four it must not lose are the romaji short forms.
        if romaji:
            add(f"{romaji} - {padded}")
        if english and english != romaji:
            add(f"{english} - {padded}")
        if romaji:
            add(_sxxexx_form(romaji, season or 1, number))
        if season is not None and romaji:
            for form in _short_forms(romaji, season, padded):
                add(form)
        if offset:
            # Right behind the romaji short forms, because a group that
            # numbers absolutely writes the franchise name and the running
            # number and nothing else (:func:`absolute_offset`). The base is
            # romaji's where there is one: it is what release groups write, and
            # a second language's absolute form would cost two of ten slots to
            # say the same thing twice.
            #
            # **Only where the base is shorter than the title**: an unmarked
            # sequel whose title names an arc (*Made in Abyss: Retsujitsu no
            # Ougonkyou*) has no marker to strip, so the "base" is the whole
            # nine-word name — and nobody writes nine words followed by an
            # absolute number. Those two slots buy nothing, and the acceptance
            # route still applies if such a release turns up under another
            # form.
            full = " ".join((romaji or english or "").split())
            base = strip_season(full).base
            if base and " ".join(base.split()) != full:
                combined = anime.episodes + offset if anime.episodes else None
                running = pad(number + offset, total_episodes=combined)
                add(f"{base} - {running}")
                add(f"{base} {running}")
        if not has_prequel(anime):
            for name in (romaji, english):
                if not name:
                    continue
                head = head_of(strip_season(name).base)
                if head:
                    add(f"{head} - {padded}")
        if season is None and romaji:
            add(f"{romaji} {padded}")
        if english and english != romaji:
            add(_sxxexx_form(english, season or 1, number))
            if season is not None:
                for form in _short_forms(english, season, padded):
                    add(form)
        # Then the symbol-stripped variants of the two full titles, which is
        # the one speculative form worth a slot: a star or a colon glued
        # between two words makes one token out of both and Nyaa matches
        # tokens. Only the *full* forms get one — a variant of a variant of an
        # abbreviation is a guess about a guess, and the cap is better spent on
        # the short forms above.
        if romaji:
            add_variant(f"{romaji} - {padded}")
        if english and english != romaji:
            add_variant(f"{english} - {padded}")
        for synonym in _synonym_forms(anime):
            add(f"{synonym} - {padded}")
            add_variant(f"{synonym} - {padded}")

    seen: dict[str, None] = {}
    for query in built:
        seen.setdefault(query, None)
    return list(seen)[:MAX_QUERIES]


# --- Filtering --------------------------------------------------------------


def title_score(parsed_key: str, titles: Iterable[str], *, strict: bool = False) -> float:
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

    ``strict`` closes the *other* direction, and is used for a single
    (:func:`acceptable`, 2026-09-14). A shorter release name is normal for an
    episode — the episode number is what pins it down — and it is how the wrong
    *film* gets downloaded, because a film has no number at all: ``servamp`` is
    a subset of every token of *Servamp Movie: Alice in the Garden*, and
    ``kizumonogatari`` is a subset of all three parts of *Kizumonogatari*. So
    in strict mode a release that names **less** than the entry is scored by
    ``token_sort_ratio`` too, and only the type marker itself
    (:data:`TYPE_WORDS`) is forgiven — the parser strips a trailing ``Movie``
    from the title it reports and the catalogue keeps it, which is a difference
    that means nothing, unlike a missing ``Alice in the Garden``.
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
        elif (
            strict
            and parsed_tokens < other_tokens
            and not ((other_tokens - parsed_tokens) <= TYPE_WORDS)
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
    #: The prequel total subtracted to read this release's number as the
    #: entry's own (:func:`absolute_offset`). Zero for every candidate whose
    #: number needed no arithmetic at all, which is nearly all of them.
    offset: int = 0
    #: Every episode number a **batch** claims to hold, as its name gives them
    #: — :attr:`~arc.services.library.parser.ParsedName.episode_span`, so in
    #: the release's own numbering (absolute where :attr:`absolute` is true).
    #: Empty for every single, and empty for a batch that names **no** range at
    #: all: a ``[BATCH]`` or ``Complete Series`` marker with no numbers is a
    #: candidate whose coverage is unknowable from its name and is settled from
    #: its file list at pick time (FR-A11, plan §3).
    covers: tuple[int, ...] = ()

    @property
    def absolute(self) -> bool:
        """Whether this release was accepted by the absolute-numbering rule."""
        return self.offset > 0

    @property
    def is_batch(self) -> bool:
        """Whether this candidate is a batch rather than one episode's file.

        Only ever true where the caller asked for batches
        (``acceptable(..., batches=True)``); the default filter rejects every
        one of them before a :class:`Candidate` exists, which is what keeps
        FR-A4's "never fetch whole seasons" true of every path written before
        FR-A11.
        """
        return self.parsed.is_batch

    @property
    def torrent_url(self) -> str:
        """The ``.torrent`` URL, for a batch that is added as a file.

        Nyaa's RSS ``<link>`` *is* the direct download
        (``https://nyaa.si/download/2088261.torrent``), which is the whole
        reason :meth:`NyaaClient.torrent_file` needs nothing but the item.
        """
        return self.item.link

    @property
    def group(self) -> str | None:
        return self.parsed.group

    @property
    def resolution(self) -> str | None:
        return self.parsed.resolution

    @property
    def dubbed(self) -> bool:
        """Whether the release carries an English dub instead of the original."""
        return self.parsed.dubbed


def _rejected(item: NyaaItem, reason: str) -> None:
    """Log why one result is not the file.

    Every branch of :func:`acceptable` goes through here, because the filter
    throws away nineteen results out of twenty and "why was nothing found?" is
    unanswerable without the sentence each of them was thrown away by.
    """
    log.debug("nyaa rejected", extra={"title": item.title, "reason": reason})


def _acceptable_batch(
    item: NyaaItem,
    parsed: ParsedName,
    *,
    titles: Sequence[str],
    number: int,
    season: int | None,
    threshold: float,
    offset: int | None,
    covered: str,
) -> Candidate | None:
    """The batch half of :func:`acceptable` (FR-A11), reached only with ``batches=True``.

    Split out because it is a different question with the same three
    ingredients, and because keeping it beside the single-episode path made
    that path harder to read than either of them is on its own. The checks and
    their order are the episode path's, with the coverage test standing where
    the episode-number test stands:

    1. the range, where the name gives one, must **cover** the episode — or
       cover ``number + offset``, the absolute reading, which as always is only
       offered to a release that names no season at all (:func:`acceptable`);
    2. the season must agree, unless the absolute reading is what accepted it,
       in which case the release named no season by construction;
    3. the title must reach ``threshold`` against the entry's own names.

    A batch that names **no** range is exempt from (1) only: it still has to be
    this show's, and this season's, and :attr:`Candidate.covers` is empty so
    that the pick can see that its coverage was never claimed.
    """
    span = parsed.episode_span
    running = number + offset if offset else None
    absolute = running is not None and parsed.season is None and running in span
    if span and number not in span and not absolute:
        wanted = f"{number} or {running}" if running is not None else f"{number}"
        _rejected(item, f"batch release{covered}, and this is episode {wanted}")
        return None
    if not absolute and (parsed.season or 1) != (season or 1):
        _rejected(item, f"batch release of season {parsed.season or 1}, not {season or 1}")
        return None
    similarity = title_score(parsed.title_key, titles)
    if similarity < threshold:
        _rejected(item, f"title {parsed.title_key!r} scored {similarity:.2f} < {threshold}")
        return None
    return Candidate(
        item=item,
        parsed=parsed,
        title_similarity=similarity,
        offset=(offset or 0) if absolute else 0,
        covers=span,
    )


def acceptable(
    item: NyaaItem,
    *,
    titles: Sequence[str],
    number: int,
    season: int | None,
    threshold: float = TITLE_THRESHOLD,
    single: bool = False,
    year: int | None = None,
    offset: int | None = None,
    batches: bool = False,
) -> Candidate | None:
    """``item`` as a :class:`Candidate`, or ``None`` with a reason logged.

    **A single** (``single=True``: a film, or an OVA/ONA the catalogue gives
    one episode — :func:`is_single`) is a different question, because the two
    things this function is otherwise built on are both absent. A film's
    release carries no episode number at all (``[Erai-raws] Servamp Movie -
    Alice in the Garden [1080p]``) and no season. So three other things are
    asked instead, and the *order* of them is the fix of 2026-09-14:

    * The release must **say what it is**: :data:`SINGLE_KINDS`, which is the
      parser's ``movie`` or ``special``. "No episode number" was the first
      version of this test and it let three whole-series Blu-ray packs through
      — ``[Judas] Sword Art Online [BD 1080p]`` names no episode, no range and
      no batch marker, so it was "an episode-less single" and it is 20 GB of
      the franchise. A release with no number *and* no marker is not a film.
    * Its title must reach the threshold under the **strict** comparison
      (:func:`title_score`), which is the half the kind check cannot do: a
      release that names *less* than the entry is the ordinary case for an
      episode and the wrong film for a single, because ``kizumonogatari`` is a
      subset of all three parts of *Kizumonogatari* and scored 1.00 against
      every one of them. Only the type marker is forgiven
      (:data:`TYPE_WORDS`), since the parser takes a trailing ``Movie`` off the
      title it reports and the catalogue keeps it.
    * And the **year**, when both sides have one: a franchise reboot carries
      the original's name exactly, and a December premiere is a January disc,
      so they must agree within :data:`YEAR_SLACK`. A missing year on either
      side is **no evidence, not agreement** — most releases carry none, which
      is exactly why the strict title rule above is unconditional rather than a
      fallback for the ones that do.

    It is then episode 1, which is the row the catalogue holds for it. A batch
    is still a batch (a franchise's three films in one torrent is exactly the
    6 GB download FR-A4 forbids), a creditless opening is still ignored, and a
    *numbered* release is refused unless the parser read it as the film itself:
    ``[SubsPlease] Yuru Camp - 01`` under a one-episode *special* entry is the
    television series' first episode, which is the wrong file with a very
    plausible name.

    A **batch is rejected first**, before the episode number is even compared,
    because it is the rejection that matters most and the one whose reason has
    to be legible: ``[Erai-raws] Dagashi Kashi 2 - 01 ~ 12 [1080p]`` is an
    entire 6 GB season whose first episode number is the one Arc asked for, and
    "only the next N unwatched episodes, never a whole season" (FR-A4) is a
    non-negotiable. The parser is what knows this — a range or a ``BATCH``
    marker makes :attr:`~arc.services.library.parser.ParsedName.kind` ``batch``
    — and the filter's job is only to refuse to look past it.

    ``batches`` (FR-A11, 2026-09-18) is the **only** way past that rejection,
    and it defaults to ``False`` so that every caller written before it, the
    whole query corpus and every current rejection are unchanged. With
    ``batches=True`` the rejection becomes a *classification* instead, in this
    order:

    * ``remake`` and the seeder floor still come first, because neither has
      anything to do with what the torrent holds;
    * where the name gives a **range**, it must cover ``number`` — or
      ``number + offset`` under the absolute rule, which needs the same four
      conditions a single's does, the release naming no season among them. A
      pack of episodes 1–12 asked for episode 20 is not this episode's batch
      any more than ``- 12`` is episode 20;
    * where the name gives **no range at all** (``[Judas] Kimetsu no Yaiba [BD
      1080p][BATCH]``, ``Complete Series``) it is a candidate with
      :attr:`Candidate.covers` empty: its coverage is unknowable from its name
      and is settled from its **file list** before anything is fetched, which
      is the one thing FR-A4 could not do before and the reason this is a
      candidate rather than a guess;
    * the season must agree and the title must reach ``threshold``, exactly as
      for a single, and a dub is **ranked** rather than filtered (FR-A3);
    * and a ``single=True`` entry never gets one: a film is one file, and a
      franchise's three films in one torrent is the download FR-A4 forbids
      with no episode for the file plan to identify.

    Nothing here decides that a batch *will* be fetched. It decides that one
    may be **considered**, and only :func:`search_for_episode` asks — for a
    finished show, and only when no acceptable single exists at all.

    **Absolute numbering** (``offset``, 2026-09-17) is the one way a release
    whose number is *not* ``number`` may still be this episode: with an offset
    of 24, ``[SubsPlease] Jujutsu Kaisen - 25 (1080p)`` is episode 1 of season
    two. Four things have to hold at once, and each of them closes a way of
    being wrong:

    * the offset came from :func:`absolute_offset`, which read it off the
      catalogue rather than inferring it from the number in front of it;
    * the release **names no season**. One that does is judged by the ordinary
      per-season rule and nothing else: a group that writes ``S2`` has told Arc
      which season it means, and arithmetic cannot improve on that;
    * the number is exactly ``number + offset`` — which is above the prequel
      total by construction, so an absolute match can never collide with the
      prequel's own numbering (``- 24``, season one's last, is episode 24 of
      season one and is rejected here);
    * and the title still reaches ``threshold`` against the entry's own names,
      season markers stripped from both sides as always.

    The rule that this function cannot enforce, because it sees one release at
    a time, lives in :func:`filter_items`: an absolute match is dropped
    outright when a season-marked release for the same episode came back in the
    same search. An explicit answer beats an inferred one every time.
    """
    if item.remake:
        _rejected(item, "nyaa flagged it a remake")
        return None
    if item.seeders < MIN_SEEDERS:
        # Rejected, not ranked last. Seeders are the third of FR-A3's rules and
        # a tie-break between releases that *could* be downloaded; zero is not
        # a worse release, it is no release — a torrent nobody is holding never
        # gets past asking for its metadata, and until 2026-09-13 Arc chose
        # those anyway whenever they happened to be the only thing a group had
        # uploaded, then held one of the client's download slots on them for as
        # long as nobody looked. Nyaa's own RSS carries the count
        # (``nyaa:seeders``), so this costs nothing but an ``if``.
        _rejected(item, "no seeders")
        return None
    # ``path=False``: this is a Nyaa title, so a slash in it is part of the
    # name (``Fate/Zero``) and never a directory separator.
    parsed = parse(item.title)
    if parsed.is_batch or parsed.episode_end is not None:
        span = parsed.episode_span
        if len(span) > 1:
            covered = f" covering episodes {span[0]}-{span[-1]}"
        elif span:
            # A marker-only batch: it says BATCH and names one number.
            covered = f" covering episode {span[0]}"
        else:
            covered = ""
        if batches and parsed.is_batch and not single:
            return _acceptable_batch(
                item,
                parsed,
                titles=titles,
                number=number,
                season=season,
                threshold=threshold,
                offset=offset,
                covered=covered,
            )
        if batches and single:
            # A film is one file: there is no episode for a file plan to
            # identify, and a franchise's three films in one torrent is
            # exactly the download FR-A4 forbids.
            _rejected(item, f"batch release{covered}, and this entry is one release")
            return None
        # Not asked for, or a range inside something that is not episodes at
        # all — a creditless opening outranks the range in the parser, so
        # ``kind`` is ``nc`` and there is no batch of episodes here.
        _rejected(item, f"batch release{covered}, not a single episode")
        return None
    if single and number == 1:
        if parsed.kind not in SINGLE_KINDS:
            # The release has to *say* it is one thing (:data:`SINGLE_KINDS`).
            # "No episode number" is not the same claim: a whole-series Blu-ray
            # pack says nothing at all, and three of them were accepted as
            # films before 2026-09-14.
            _rejected(item, f"parsed as {parsed.kind}, not a film or a one-off")
            return None
        if parsed.kind != "movie" and parsed.episode not in (None, number):
            _rejected(item, f"episode {parsed.episode}, and this entry is one release")
            return None
        if year is not None and parsed.year is not None and abs(parsed.year - year) > YEAR_SLACK:
            _rejected(item, f"year {parsed.year}, not {year}")
            return None
        # ``strict``: a release that names *less* of this film's title than the
        # catalogue does is another film of the franchise, not an abbreviation
        # (:func:`title_score`). This is what the year check cannot do — most
        # releases carry no year, and a missing year on either side is no
        # evidence rather than agreement.
        similarity = title_score(parsed.title_key, titles, strict=True)
        if similarity < threshold:
            _rejected(item, f"title {parsed.title_key!r} scored {similarity:.2f} < {threshold}")
            return None
        return Candidate(item=item, parsed=parsed, title_similarity=similarity)
    if parsed.kind != "episode":
        _rejected(item, f"parsed as {parsed.kind}, not a single episode")
        return None
    # The absolute reading, and only where the release itself offers no season:
    # one that names a season has said which episode it is, and the ordinary
    # rule below is a better answer than any arithmetic.
    running = number + offset if offset else None
    absolute = running is not None and parsed.season is None and parsed.episode == running
    if parsed.episode != number and not absolute:
        wanted = f"{number} or {running}" if running is not None else f"{number}"
        _rejected(item, f"episode {parsed.episode}, not {wanted}")
        return None
    if absolute:
        similarity = title_score(parsed.title_key, titles)
        if similarity < threshold:
            _rejected(item, f"title {parsed.title_key!r} scored {similarity:.2f} < {threshold}")
            return None
        return Candidate(item=item, parsed=parsed, title_similarity=similarity, offset=offset or 0)
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
        _rejected(item, f"season {parsed.season or 1}, not {season or 1}")
        return None
    similarity = title_score(parsed.title_key, titles)
    if similarity < threshold:
        _rejected(item, f"title {parsed.title_key!r} scored {similarity:.2f} < {threshold}")
        return None
    return Candidate(item=item, parsed=parsed, title_similarity=similarity)


def filter_items(
    items: Iterable[NyaaItem],
    *,
    titles: Sequence[str],
    number: int,
    season: int | None,
    threshold: float = TITLE_THRESHOLD,
    single: bool = False,
    year: int | None = None,
    offset: int | None = None,
    batches: bool = False,
    only_batches: bool = False,
) -> list[Candidate]:
    """Every acceptable item, in the order the feed gave them.

    With one rule that only a whole result set can state (2026-09-17): where a
    release **names its season** and is this episode, every
    absolute-numbered candidate is dropped. The two readings cannot both be
    right about the same episode, one of them is a group's own statement of
    which season it uploaded and the other is Arc's arithmetic, and a pool
    holding both would let the ranker settle it on seeders. Explicit wins.

    ``batches`` is :func:`acceptable`'s and means what it means there.
    ``only_batches`` drops every **single** from the answer, which is how
    :func:`search_for_episode` asks its second question of the pool it already
    has (FR-A11): the two lists are ranked apart and a single always wins, so
    mixing them would only give the ranker a choice it must not make. The
    explicit-season rule above is then applied to the batch list **on its own
    terms** — an absolute pack loses to a pack that names the season, and to
    nothing else.
    """
    kept = []
    for item in items:
        candidate = acceptable(
            item,
            titles=titles,
            number=number,
            season=season,
            threshold=threshold,
            single=single,
            year=year,
            offset=offset,
            batches=batches,
        )
        if candidate is not None and (not only_batches or candidate.is_batch):
            kept.append(candidate)
    explicit = [
        candidate
        for candidate in kept
        if not candidate.absolute and candidate.parsed.season is not None
    ]
    if not explicit or not any(candidate.absolute for candidate in kept):
        return kept
    for candidate in kept:
        if candidate.absolute:
            _rejected(
                candidate.item,
                f"absolute numbering, and {explicit[0].item.title!r} names the season outright",
            )
    return [candidate for candidate in kept if not candidate.absolute]


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
    #: How many of the show's currently wanted episodes this **batch** covers
    #: (:func:`rank`). Zero for every single, where it is not part of the sort
    #: key at all, and at least one for every batch.
    covered_wanted: int = 0

    @property
    def item(self) -> NyaaItem:
        return self.candidate.item

    @property
    def dubbed(self) -> bool:
        return self.candidate.dubbed

    @property
    def is_batch(self) -> bool:
        return self.candidate.is_batch

    @property
    def torrent_url(self) -> str:
        """The ``.torrent`` URL, for a batch that is added as a file (FR-A11)."""
        return self.candidate.torrent_url

    @property
    def sort_key(self) -> tuple[int, ...]:
        """A dub last, then FR-A3's four rules in the order the spec lists them.

        **A batch gains one term and only a batch** (FR-A11, 2026-09-18):
        ``-covered_wanted`` directly behind the dub, so of two packs that both
        hold the episode the one that also holds the *other* episodes somebody
        is waiting for is taken — one torrent then serves several wants and the
        second of them costs no Nyaa request and no new bytes of overhead. It
        sits behind the dub for the same reason everything does, and in front
        of the group because which episodes a pack holds is a fact about the
        download and group, resolution, seeders and trusted are preferences
        about the copy. A single's key is the five-tuple it has always been,
        untouched, which is asserted rather than assumed.

        **The dub comes before the group**, which is to say it outranks every
        other preference: a dubbed release is not a worse copy of the episode,
        it is the episode in the wrong language, and no ordering of groups or
        resolutions should be able to promote one over a subbed file that
        exists. Production picked ``[Yameii] … [English Dub]`` for *Sword Art
        Online* and ``[KaiDubs] …`` for *BOFURI* on 2026-09-13 because both
        were the most seeded upload of their episode, which is exactly what
        FR-A3's third rule says to do and exactly the wrong answer.

        It is a ranking and not a filter, because a dub is still the episode:
        when nothing else was found, a file somebody can watch beats fourteen
        days of ``searching`` (``_pick`` logs the choice when it happens).
        """
        coverage = (-self.covered_wanted,) if self.is_batch else ()
        return (
            1 if self.dubbed else 0,
            *coverage,
            self.group_rank,
            self.resolution_rank,
            -self.seeders,
            0 if self.trusted else 1,
        )


def covered_wanted(candidate: Candidate, wanted_numbers: Collection[int]) -> int:
    """How many of ``wanted_numbers`` this batch covers — at least one (FR-A11).

    Zero for a single, which does not carry the term at all. For a batch:

    * the numbers are the **entry's** episode numbers and
      :attr:`Candidate.covers` is the *release's*, so the offset an absolute
      candidate was accepted under is applied here too — a ``25 ~ 47`` pack of
      a second season that follows 24 covers that entry's episodes 1–23;
    * a pack that names **no** range covered nothing it can be counted on, and
      scores the floor;
    * and the floor is one, for every batch, because a batch is only a
      candidate at all if it may hold the episode being searched for. That is
      what makes an empty ``wanted_numbers`` — the default, and what a caller
      with no live wants to hand over passes — score every batch alike, so the
      term drops out of the key and FR-A3's four rules decide, rather than
      quietly promoting the packs whose coverage is unknown.
    """
    if not candidate.is_batch:
        return 0
    span = set(candidate.covers)
    if not span:
        return 1
    return max(1, sum(1 for number in set(wanted_numbers) if number + candidate.offset in span))


def _reasons(
    candidate: Candidate,
    rules: Rules,
    group_rank: int,
    resolution_rank: int,
    covered: int = 0,
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
    if candidate.is_batch:
        # The one reason that is about *what the torrent holds* rather than
        # which copy of the episode it is, and the sentence FR-A11 is judged
        # by: a chosen batch has to say how much of it Arc asked for.
        span = (
            f"episodes {candidate.covers[0]}-{candidate.covers[-1]}"
            if len(candidate.covers) > 1
            else f"episode {candidate.covers[0]}"
            if candidate.covers
            else "an unnamed range"
        )
        reasons.append(f"batch covering {span}, {covered} of them wanted")
    if candidate.absolute and candidate.parsed.episode is not None:
        # The one reason that is about *which episode this is* rather than
        # which copy of it: a chosen release whose name says 25 lands in an
        # episode row numbered 1, and the log line has to say why.
        reasons.append(
            f"absolute numbering: release {candidate.parsed.episode}"
            f" = episode {candidate.parsed.episode - candidate.offset}"
        )
    if candidate.dubbed:
        reasons.append("english dub, so ranked below every subbed release")
    if candidate.item.trusted:
        reasons.append("trusted uploader")
    if rules.overridden:
        reasons.append("per-show rule override applied")
    return reasons


def rank(
    candidates: Iterable[Candidate],
    rules: Rules,
    *,
    wanted_numbers: Collection[int] = (),
) -> list[Ranked]:
    """Order candidates by FR-A3's rules, best first, each with its reasons.

    ``wanted_numbers`` are the episodes of this show somebody is waiting for
    right now, and they matter to **batches only** (:func:`covered_wanted`,
    :attr:`Ranked.sort_key`): of two packs that both hold the episode being
    searched for, the one that also holds the others is one torrent instead of
    two. It is empty by default and empty is neutral — every batch then scores
    the same and FR-A3's four rules decide, which is exactly what a caller with
    nothing to say about the rest of the show should get.
    """
    ranked = []
    for candidate in candidates:
        group_rank = rules.group_rank(candidate.group)
        resolution_rank = rules.resolution_rank(candidate.resolution)
        covered = covered_wanted(candidate, wanted_numbers)
        ranked.append(
            Ranked(
                candidate=candidate,
                group_rank=group_rank,
                resolution_rank=resolution_rank,
                seeders=candidate.item.seeders,
                trusted=candidate.item.trusted,
                reasons=tuple(_reasons(candidate, rules, group_rank, resolution_rank, covered)),
                covered_wanted=covered,
            )
        )
    ranked.sort(key=lambda entry: entry.sort_key)
    return ranked


def deep_search(anime: Anime) -> bool:
    """Whether this entry may be asked the narrowed forms (:func:`group_queries`).

    A show the catalogue calls :data:`DEEP_SEARCH_STATUS`, and nothing else —
    the asymmetry is the whole decision (owner, 2026-09-18). The newest 75
    matches of a *finished* show's query are its franchise's later seasons,
    because the episode being looked for was uploaded years ago and everything
    since sits in front of it. The newest 75 of an *airing* show's query
    contain this week's release, which is the thing being looked for: there is
    nothing behind them worth six more requests, and Nyaa's patience is the
    budget.

    Null status reads as not-finished. It is the airing show's answer, and the
    airing show's behaviour is the one that must not change.
    """
    return str(anime.status or "").strip().upper() == DEEP_SEARCH_STATUS


def group_queries(anime: Anime, number: int, rules: Rules) -> list[str]:
    """``"<form> <group>"`` for the top forms × the groups worth naming.

    The fix of 2026-09-18, and the shape of it follows from what Nyaa does
    with a query: it ANDs the words, so adding a group's name to a form does
    not *sort* the 75 newest matches, it asks a different and much smaller
    question — one whose whole answer fits. ``Kimetsu no Yaiba - 10`` is 75
    results of a franchise that ran five more seasons; ``Kimetsu no Yaiba - 10
    HorribleSubs`` is 30 results, one of which is the episode.

    Only the **top three ordinary forms** earn a narrowed version: the romaji
    ``- NN``, the english ``- NN``, and the romaji ``SxxEyy``. Those are the
    three shapes a group's own upload actually has, and every further form
    multiplied by every group is a request spent on a spelling nobody wrote.
    The order is **form-major** — all the groups of the romaji form, then all
    the groups of the english one — because the form is the stronger signal:
    romaji is what groups write, and a wrong-language form narrowed by the
    right group still finds nothing.

    Groups come from the rules first (``preferred_groups``, per-show override
    already merged by
    :func:`~arc.services.acquisition.rules.load_rules`) and then from
    :data:`DEEP_SEARCH_GROUPS`, de-duplicated case-insensitively so an admin
    who already prefers SubsPlease does not ask for it twice. Preferring a
    group is a statement about who to ask, and the admin's statement outranks
    the module's.

    Pure, like :func:`queries`, so the list is a claim that can be read and
    argued with rather than a behaviour that has to be watched.
    """
    padded = pad(number, total_episodes=anime.episodes)
    season = anime_season(anime)
    romaji = anime.title_romaji
    english = anime.title_english

    forms: list[str] = []
    if romaji:
        forms.append(f"{romaji} - {padded}")
    if english and english != romaji:
        forms.append(f"{english} - {padded}")
    if romaji:
        forms.append(_sxxexx_form(romaji, season or 1, number))

    groups: list[str] = []
    seen_groups: set[str] = set()
    for group in (*rules.preferred_groups, *DEEP_SEARCH_GROUPS):
        folded = group.strip().casefold()
        if not folded or folded in seen_groups:
            continue
        seen_groups.add(folded)
        groups.append(group.strip())

    built: dict[str, None] = {}
    for form in forms:
        normalised = " ".join(form.split())
        if not normalised:
            continue
        for group in groups:
            built.setdefault(f"{normalised} {group}", None)
    return list(built)


def batch_queries(anime: Anime) -> list[str]:
    """``"<base>"`` and ``"<base> BATCH"`` — the forms a **pack** is named by.

    The correction of 2026-09-18, and the reason the first version of the
    batch fallback could never fire. **Every form :func:`queries` builds
    carries the episode number**, and a batch release name does not carry one:
    ``[Erai-raws] Dagashi Kashi 2 - 01 ~ 12 [1080p]`` holds no token ``03``,
    and Nyaa ANDs the words of a query. So the merged pool of a numbered
    search contains **no batches at all** — verified against the live feed on
    2026-09-18: *One Week Friends* episode 3 returned 16 results and 0
    batches, *Chivalry of a Failed Knight* 13 and 0, *Dagashi Kashi 2* 10 and
    0. The one case that appeared to work, *Kimetsu no Yaiba* episode 10, did
    so by accident: ``10`` is a token of ``1080p``, which is in every batch's
    name. The plan's "no extra request, the batches were in the pool all
    along" was true of that accident and of nothing else.

    So a batch has to be **asked for**, and asked for by the shape a pack's
    name actually has: the title with no number on it, and the title with the
    word every batch uploader writes.

    Three forms at most, best first:

    1. ``"<romaji base>"`` — the season-stripped romaji title, bare. The
       broadest of the three and the one that finds a pack whose name says
       ``Complete Series``, ``Seasons 1-2`` or nothing at all. Season-stripped
       for the same reason :func:`_short_forms` is: *Dagashi Kashi 2*'s packs
       are named ``Dagashi Kashi 2`` and ``Dagashi Kashi S2`` and the query
       has to be a subset of both, and the season check on the way back in
       (:func:`_acceptable_batch`) is what keeps season one's pack out.
    2. ``"<romaji base> BATCH"`` — narrower, and it is the *word* Erai-raws
       and most others put in the name.
    3. ``"<english base> BATCH"``, where the english base says something the
       romaji one did not.

    Deliberately **not** ``Complete`` or ``Season Pack``: each extra word is
    another paced request for a spelling, the bare form already reaches those
    uploads, and the budget is the thing being spent.

    Pure, like :func:`queries` and :func:`group_queries`, and asked only where
    :func:`search_for_episode` asks them — a finished show, not a film, with
    no acceptable single at all.
    """
    bases: list[str] = []
    for name in (anime.title_romaji, anime.title_english):
        if not name:
            continue
        base = " ".join((strip_season(name).base or name).split())
        if base and base not in bases:
            bases.append(base)
    if not bases:
        return []
    # The bare form is built from the **first** base there is, which is romaji
    # where the entry has one: it is what release groups write, and a second
    # bare form in another language would spend a slot to ask the same
    # question twice.
    forms = [bases[0], f"{bases[0]} {BATCH_WORD}"]
    forms.extend(f"{base} {BATCH_WORD}" for base in bases[1:])
    return forms[:MAX_BATCH_QUERIES]


@dataclass(frozen=True, slots=True)
class Search:
    """What one episode's search asked, saw and kept (FR-A7).

    ``ranked`` is the answer; the other three are the *diagnostic*, and they
    are here because ``forms`` and ``results`` are the two numbers that tell an
    owner looking at a stuck row which half is broken. "Six forms, zero
    results" is a query problem — Nyaa has never heard of any name Arc asked
    by, which is what every case in ``tests/fixtures/query_corpus.txt`` was
    before it was fixed. "Two forms, forty results, nothing kept" is a filter
    problem, and the rejection log names the sentence behind each one. Without
    the pair, a row that says ``Searching`` for six hours says nothing at all.
    ``requests`` is the third and the newest (2026-09-18): above ``forms`` it
    says the search fell back on the group-narrowed forms
    (:func:`group_queries`), which is the sentence behind a search that took
    forty seconds.
    """

    ranked: list[Ranked]
    #: How many **title** query forms ran (:func:`queries`, after the dedupe
    #: and the cap). Deliberately still only those: it is the number on the
    #: episode row, and "7 forms" has meant "seven ways of writing the title"
    #: since M6.
    forms: int
    #: Distinct releases every form returned between them, before the filter.
    results: int
    #: Requests made: title forms, narrowed forms and batch forms together
    #: (some possibly answered from the ten-minute cache). Equal to ``forms``
    #: for an airing show and for a finished one that found what it needed, so
    #: a larger number is itself the statement that this search had to go
    #: narrower (:func:`group_queries`) or had to ask for a pack
    #: (:func:`batch_queries`).
    requests: int
    #: Batch releases worth considering, ranked, and **only** where ``ranked``
    #: is empty: a finished show with no acceptable single at all (FR-A11,
    #: 2026-09-18). Always empty for an airing show, for a film and for any
    #: search that found a single, however poorly seeded. It costs no extra
    #: request — these come out of the pool the title and narrowed forms had
    #: already merged — and it is a *list of candidates*, not a decision:
    #: whether one is taken is ``search_release``'s, which reads the torrent's
    #: contents before it fetches anything.
    batches: list[Ranked] = field(default_factory=list)

    @property
    def kept(self) -> int:
        """How many **singles** were kept, which is what FR-A7's row reports."""
        return len(self.ranked)


async def search_for_episode(
    client: NyaaClient,
    anime: Anime,
    number: int,
    rules: Rules,
    *,
    threshold: float = TITLE_THRESHOLD,
    offset: int | None = None,
    wanted_numbers: Collection[int] = (),
) -> Search:
    """Run **every** :func:`queries` form, merge by info hash, filter and rank.

    ``offset`` is :func:`absolute_offset`'s answer for this entry, computed by
    the caller because it is the caller that has a database session
    (``jobs._prequel_offset``). ``None`` — the default, and what every search
    did before 2026-09-17 — asks and accepts exactly what it always did.

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

    **A finished show that found too little is asked again by group**
    (:func:`group_queries`, 2026-09-18), and the rule is exactly this:

    * every title form runs first, exactly as before;
    * then, **only for a** :func:`deep_search` **entry** and **only while the
      merged pool holds fewer than** :data:`ENOUGH_CANDIDATES` **acceptable
      releases**, the narrowed forms run in order, each one an ordinary paced
      and cached request;
    * the narrowing stops the moment there are three to compare, because that
      is what the ranker was short of;
    * and :data:`MAX_REQUESTS` is the ceiling on the lot — title forms
      included — so one episode's search cannot cost more than 20 requests
      however the forms and groups multiply out; where a pack could still be
      asked for, the narrowing stops :data:`MAX_BATCH_QUERIES` short of it and
      leaves those three to :func:`batch_queries`.

    The narrowed forms are **not** counted against :data:`MAX_QUERIES`: that
    cap is the budget for ways of writing the *title*, and these are not that.
    They are counted in ``requests`` and not in ``forms``, for the same reason.

    Nothing about *which* releases are acceptable changes: a narrowed form's
    results merge by info hash like everything else and go through the same
    filter into the same ranking, so a group that was asked for by name still
    wins or loses on preferred group, resolution, seeders and trusted. The pool
    was the bug.

    **And a finished show with no single at all is offered batches**
    (``Search.batches``, FR-A11, 2026-09-18) — the last question this function
    asks, of the pool it already has:

    * ``not ranked``, literally "no acceptable single exists". Stricter than
      the narrowing's "fewer than three": a single, however poorly seeded,
      always wins, because it is the episode and nothing else;
    * :func:`deep_search`, so ``FINISHED`` only. An airing show never sees a
      batch — its own week's release is inside the newest 75 — and a null
      status reads as not-finished, which is the airing show's answer;
    * not a :func:`is_single` entry, which has no episode for a file plan to
      identify;
    * and **up to three more requests** (:func:`batch_queries`, corrected
      2026-09-18). This was written as "no new request, the batches were in
      the pool all along", and that was true of exactly one case and wrong in
      general: every form above carries the episode number, no batch name
      carries one, and Nyaa ANDs the words of a query, so a numbered search's
      pool holds **no batches at all** — 16 results and 0 batches for *One
      Week Friends* episode 3, 13 and 0 for *Chivalry of a Failed Knight*, 10
      and 0 for *Dagashi Kashi 2*, against the live feed. *Kimetsu no Yaiba*
      episode 10 looked like it worked because ``10`` is a token of
      ``1080p``. So the pack is asked for by name, with no number and with
      the word ``BATCH``, and those requests are the **last** this search
      makes: they are counted in ``requests`` (never in ``forms``) and they
      are inside :data:`MAX_REQUESTS` like everything else. The narrowing
      above **stops** :data:`MAX_BATCH_QUERIES` **requests short of that
      ceiling** so the three are always reachable (owner, 2026-09-18): a show
      with ten title forms and eighteen narrowed ones would otherwise spend
      the whole budget looking for a single, and it is exactly the show
      likeliest to have nothing but packs. The narrowing still goes first —
      it is looking for a *single*, and a single beats every pack.

    ``wanted_numbers`` are that show's currently wanted episodes, supplied by
    the caller because it is the caller that has a session, and they only
    order the batch list (:func:`covered_wanted`). Empty is neutral.
    """
    titles = anime_titles(anime)
    season = anime_season(anime)
    single = is_single(anime)

    merged: dict[str, NyaaItem] = {}
    counts: list[int] = []
    asked: set[str] = set()
    requests = 0

    def enough() -> bool:
        """Whether the pool already holds enough to compare (FR-A3)."""
        kept = filter_items(
            merged.values(),
            titles=titles,
            number=number,
            season=season,
            threshold=threshold,
            single=single,
            year=anime.season_year,
            offset=offset,
        )
        return len(kept) >= ENOUGH_CANDIDATES

    async def ask(query: str) -> int:
        """One form, merged into the pool; how many items it returned."""
        nonlocal requests
        items = await client.search(query)
        requests += 1
        asked.add(query)
        before = len(merged)
        for item in items:
            merged.setdefault(item.info_hash, item)
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
        return len(items)

    for query in queries(anime, number, offset=offset):
        if requests >= MAX_REQUESTS:
            log.warning(
                "nyaa request ceiling reached, forms left unasked",
                extra={"anime_id": anime.id, "number": number, "requests": requests},
            )
            break
        counts.append(await ask(query))

    if deep_search(anime):
        # Narrowed by group, and only for what the title forms could not
        # reach: the newest 75 matches of a finished show's query are its
        # franchise's later seasons, and the feed cannot be paged past them.
        #
        # **The narrowing stops three requests short of the ceiling** for an
        # entry that could still ask for a pack (owner, 2026-09-18). Without
        # the reservation, a show with ten title forms and eighteen narrowed
        # ones spends the whole budget looking for a single and the batch
        # forms are never reached — which is precisely the show most likely to
        # have nothing but packs. A film keeps the plain ceiling, because it
        # never asks for one, and an airing show never gets here at all.
        narrowing_ceiling = MAX_REQUESTS if single else MAX_REQUESTS - MAX_BATCH_QUERIES
        for query in group_queries(anime, number, rules):
            if requests >= narrowing_ceiling:
                log.warning(
                    "nyaa request ceiling reached, narrowed forms left unasked",
                    extra={
                        "anime_id": anime.id,
                        "number": number,
                        "requests": requests,
                        "ceiling": narrowing_ceiling,
                    },
                )
                break
            if enough():
                break
            log.debug(
                "nyaa narrowed query",
                extra={"query": query, "anime_id": anime.id, "number": number},
            )
            await ask(query)

    candidates = filter_items(
        merged.values(),
        titles=titles,
        number=number,
        season=season,
        threshold=threshold,
        single=single,
        year=anime.season_year,
        offset=offset,
    )
    ranked = rank(candidates, rules)

    batches: list[Ranked] = []
    narrowed = requests - len(counts)
    if not ranked and deep_search(anime) and not single:
        # A pack has to be **asked for** (:func:`batch_queries`, corrected
        # 2026-09-18): every form above carries the episode number, no batch
        # name carries one, and Nyaa ANDs the words of a query — so the pool
        # merged so far holds no batches at all. Up to three more forms, the
        # last requests this search will make, each of them paced, cached and
        # inside the same ceiling as everything else.
        for query in batch_queries(anime):
            if query in asked:
                continue
            if requests >= MAX_REQUESTS:
                log.warning(
                    "nyaa request ceiling reached, batch forms left unasked",
                    extra={"anime_id": anime.id, "number": number, "requests": requests},
                )
                break
            log.debug(
                "nyaa batch query",
                extra={"query": query, "anime_id": anime.id, "number": number},
            )
            await ask(query)
        # ``only_batches`` keeps the two lists apart: a single always wins, so
        # there is nothing for a mixed ranking to decide.
        batches = rank(
            filter_items(
                merged.values(),
                titles=titles,
                number=number,
                season=season,
                threshold=threshold,
                single=single,
                year=anime.season_year,
                offset=offset,
                batches=True,
                only_batches=True,
            ),
            rules,
            wanted_numbers=wanted_numbers,
        )

    log.info(
        "nyaa candidates",
        extra={
            "anime_id": anime.id,
            "number": number,
            "offset": offset,
            "queries": len(counts),
            "per_query": counts,
            "requests": requests,
            "narrowed": narrowed,
            "batch_forms": requests - len(counts) - narrowed,
            "merged": len(merged),
            "kept": len(candidates),
            "batches": len(batches),
            "top": ranked[0].item.title if ranked else None,
            "reasons": list(ranked[0].reasons) if ranked else [],
            "top_batch": batches[0].item.title if batches else None,
        },
    )
    return Search(
        ranked=ranked,
        forms=len(counts),
        results=len(merged),
        requests=requests,
        batches=batches,
    )


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
    "ABSOLUTE_COUNTED_FORMATS",
    "ABSOLUTE_SKIPPED_FORMATS",
    "BATCH_WORD",
    "BENCODE_DICT_PREFIX",
    "CACHE_TTL",
    "CATEGORY",
    "DEEP_SEARCH_GROUPS",
    "DEEP_SEARCH_STATUS",
    "ENOUGH_CANDIDATES",
    "MAX_PREQUEL_HOPS",
    "MAX_BATCH_QUERIES",
    "MAX_QUERIES",
    "MAX_REQUESTS",
    "MAX_SYNONYM_QUERIES",
    "MAX_TORRENT_BYTES",
    "MIN_INTERVAL",
    "MIN_SEEDERS",
    "NYAA_NS",
    "ROMAN_SEASONS",
    "SINGLE_FORMATS",
    "SYMBOLS",
    "TITLE_THRESHOLD",
    "TRACKERS",
    "YEAR_SLACK",
    "Candidate",
    "NyaaClient",
    "NyaaItem",
    "NyaaUnavailable",
    "PREQUEL_RELATION",
    "PrequelResolver",
    "Ranked",
    "SINGLE_KINDS",
    "Search",
    "TYPE_WORDS",
    "absolute_offset",
    "acceptable",
    "anime_season",
    "anime_titles",
    "as_dict",
    "batch_queries",
    "close_shared_client",
    "covered_wanted",
    "deep_search",
    "filter_items",
    "group_queries",
    "has_prequel",
    "head_of",
    "is_single",
    "pad",
    "parse_feed",
    "queries",
    "rank",
    "reset_shared_client",
    "search_for_episode",
    "shared_client",
    "strip_symbols",
    "title_score",
]
