"""Release filenames to a :class:`ParsedName` (FR-L2, architecture.md §5.2).

``anitopy`` does the hard part — it knows where a title ends and a tag begins
in ``[Erai-raws] Kaguya-sama wa Kokurasetai - Ultra Romantic - 05 [1080p]
[Multiple Subtitle][ABCD1234].mkv`` — and this module does the four things it
deliberately does not.

1. **It types the file.** anitopy reports what it saw; Arc needs to know
   whether the thing on disk is an episode, a batch, a movie, a special or a
   creditless opening, because each takes a different path (an ``nc`` file is
   ignored outright, a batch goes to review). That is :attr:`ParsedName.kind`.
2. **It normalises the season.** Releases write the second season of a show
   five ways — ``2nd Season``, ``Season 2``, ``S2``, ``II``, ``Part 2`` — and
   anitopy only recognises the first three, and only where the release put
   them somewhere it looks. Everything here ends up in :attr:`season`, and is
   *removed from the title*, so ``Mob Psycho 100 II`` and ``Mob Psycho 100 2nd
   Season`` produce the same title with the same season.
3. **It cleans the title.** Whatever tech tags anitopy left behind, trailing
   ``END`` markers, dangling separators and stray punctuation come off, and
   the result is offered twice: :attr:`title` keeps the original casing for
   display, :attr:`title_key` is lowercased with punctuation flattened to
   spaces for matching (``Re:Zero`` and ``Re Zero`` are one key).
4. **It gives one flat, JSON-safe shape.** ``media_files.parsed`` is JSONB and
   the review API renders straight out of it, so every field is a scalar and
   :meth:`ParsedName.as_dict` is the storage format.
5. **It says whether the audio is a dub** (:attr:`ParsedName.dubbed`), because
   acquisition ranks a dub below every subbed candidate (FR-A3) and "which
   release did Arc pick, and why" has to be able to name that reason.

``/`` is a **title character** as well as a path separator — *Fate/Zero*,
*Fate/strange Fake* — and which one it is depends on where the string came
from, not on what it looks like. So ``parse`` takes a ``path`` flag and the
caller says: the ingest scanner and the match job pass ``path=True`` and get
the last component; a Nyaa title is never split (:func:`basename`).

The module is **pure**: no I/O, no clock, no database. Same string in, same
dataclass out, which is what makes ``tests/fixtures/release_names.txt`` a
usable regression corpus.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, NamedTuple

import anitopy

#: What the file is. The matcher branches on this: ``nc`` is ignored without
#: asking anyone (FR-L4), ``batch`` goes to review because phase 1 links single
#: files only, and the rest are matched normally.
Kind = Literal["episode", "batch", "movie", "special", "nc", "unknown"]

#: Extensions Arc treats as video. Also the ingest scanner's filter, so a
#: ``.nfo`` beside a release never becomes a ``media_files`` row.
VIDEO_EXTENSIONS: frozenset[str] = frozenset({"mkv", "mp4", "avi", "ts", "webm"})

# --- Vocabulary -------------------------------------------------------------

#: Roman numerals that mean a season when they end a title. ``I`` is excluded
#: (a first season is never numbered) and so is ``V`` — it is a real word in
#: too many titles to be worth the two shows it would catch.
_ROMAN: dict[str, int] = {
    "II": 2,
    "III": 3,
    "IV": 4,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
}

#: Tokens that are technical rather than part of a title. Matched
#: case-insensitively against whole words left over in anitopy's title or in a
#: bracket it did not claim. ``END``/``FIN``/``FINAL`` are deliberately absent:
#: they belong to :data:`_END_RE`, which is case-sensitive, because *Frieren:
#: Beyond Journey's End* ends in one of them and means it.
_TECH_WORDS = frozenset(
    {
        "10bit",
        "10bits",
        "8bit",
        "aac",
        "ac3",
        "amzn",
        "atmos",
        "av1",
        "avc",
        "batch",
        "bd",
        "bdrip",
        "blu-ray",
        "bluray",
        "cr",
        "crf",
        "dts",
        "dual",
        "dual-audio",
        "dvd",
        "dvdrip",
        "eac3",
        "eng",
        "flac",
        "h264",
        "h265",
        "hdtv",
        "hevc",
        "hi10",
        "hi10p",
        "jpn",
        "multi",
        "multi-subs",
        "opus",
        "raw",
        "remux",
        "repack",
        "sub",
        "subbed",
        "subs",
        "tv",
        "uncensored",
        "vostfr",
        "web",
        "web-dl",
        "webdl",
        "webrip",
        "x264",
        "x265",
        "xvid",
    }
)

#: Bracketed things that are never a title: a CRC32, a resolution, a bit
#: depth, a subtitle note. Used together with :func:`_is_tech_word` to decide
#: whether a leftover ``[...]`` can be dropped wholesale.
_BRACKET_NOISE = re.compile(
    r"^(?:[0-9a-f]{8}|multiple\s+subtitle|multi[- ]?subs?|dual[- ]?audio|"
    r"english\s+(?:dub|sub)|complete(?:\s+series)?|batch|uncensored)$",
    re.IGNORECASE,
)

#: A resolution as a bare token: ``1080p``, ``1920x1080``, ``4K``.
_RESOLUTION_TOKEN = re.compile(r"^(?:\d{3,4}p|\d{3,4}[xX]\d{3,4}|4k|uhd)$", re.IGNORECASE)

#: A season *range* — ``S1-S4`` on a batch. It names no single season, so it
#: is removed from the title and leaves ``season`` null rather than claiming
#: the last one it saw.
_SEASON_RANGE_RE = re.compile(r"\s*[-:]?\s*\bS\d{1,2}\s*[-~]\s*S\d{1,2}\b\s*", re.IGNORECASE)

#: A bare four-digit year among dot-separated scene tokens.
_BARE_YEAR_RE = re.compile(r"(?<![\d])(19[5-9]\d|20\d\d)(?![\d])")

#: The window every year rule in this module uses: 1950 to 2099. The same one
#: :data:`_BARE_YEAR_RE` and :data:`_YEAR_RE` spell out, as numbers, for the
#: rules that have an ``int`` rather than a string in hand.
YEAR_RANGE: tuple[int, int] = (1950, 2099)

#: Creditless openings/endings, previews and trailers. These are real files in
#: a batch and must never be matched to an episode (FR-L4).
_NC_RE = re.compile(
    r"(?:^|[\s\-_.\[\(])(?:"
    r"nc(?:op|ed)\d*|creditless|clean(?:\s|_)?(?:opening|ending)|"
    r"op\d*|ed\d*|pv\d*|cm\d*|teaser|trailer|promo|preview|menu|textless"
    r")(?:[\s\-_.\]\)]|$)",
    re.IGNORECASE,
)

#: OVAs, ONAs, specials and the assorted names for a bonus episode.
_SPECIAL_RE = re.compile(
    r"(?:^|[\s\-_.\[\(])(?:ova|oav|ona|specials?|sp\d{1,2}|extras?|omake|bonus)"
    r"(?:[\s\-_.\]\)]|$)",
    re.IGNORECASE,
)

#: A theatrical release. ``Gekijouban`` is the Japanese marker and appears in
#: romaji titles, which is why it is here and not only in anitopy's own list.
_MOVIE_RE = re.compile(
    r"(?:^|[\s\-_.\[\(])(?:movie|gekijou?ban|gekijo?ban|theatrical|film)(?:[\s\-_.\]\)]|$)",
    re.IGNORECASE,
)

#: A whole season in one directory or file, said in words: ``[BATCH]``,
#: ``[Unofficial Batch]``, ``Season Pack``. **Enough on its own** (FR-A4): a
#: release that says ``BATCH`` is a batch whatever numbers it also carries,
#: because ``[Erai-raws] Shingeki no Kyojin Season 3 Part 2 - 01 ~ 10
#: [1080p][BATCH]`` is 10 episodes and 6 GB, and the one thing Arc must never
#: do is fetch a whole season. This used to be read only when the name gave no
#: episode number at all, which is precisely the case a batch *with* a range
#: never is.
_BATCH_MARKER_RE = re.compile(
    r"(?:^|[\s\-_.\[\(])(?:batch|seasons?[\s._-]+pack)(?:[\s\-_.\]\)]|$)",
    re.IGNORECASE,
)

#: The other half of the batch vocabulary — ``Complete``, ``Complete Series``,
#: ``S1 Complete`` — and the half that is **only read when the name names no
#: single episode**. The word means two things: on a run it is the whole show
#: (``[Kametsu] Cowboy Bebop Complete Series``) and on one file it is a fansub
#: shouting that this was the last one, which is the same claim ``END`` makes
#: and why :data:`_END_RE` carries it too. ``[Group] Show - 12 [COMPLETE]`` is
#: therefore episode 12 and not a season; nothing is lost by the gate, because
#: a ``Complete`` release that really is a batch either writes a range — which
#: has already decided the question by the time this is asked — or names no
#: episode at all.
_COMPLETE_MARKER_RE = re.compile(
    r"(?:^|[\s\-_.\[\(])complete(?:\s+series)?(?:[\s\-_.\]\)]|$)",
    re.IGNORECASE,
)

#: An episode **range**, as a release writes one at the place an episode number
#: goes: ``- 01 ~ 12 [1080p]``, ``01~12``, ``[01-23]``, ``E01-E12``,
#: ``S01E01-E12``, ``001-024``. Read *before* anitopy's own answer, because
#: anitopy reports only the first number of ``- 01 ~ 12`` and a release Arc
#: reads as episode 1 is a release Arc downloads: six gigabytes of *Dagashi
#: Kashi* season 2 arrived that way, and all twelve episodes were transcoded
#: for the sake of the two somebody wanted.
#:
#: Four details do all the work of keeping a *title* from reading as a range:
#:
#: * **Both numbers are padded** to at least two digits, unless one carries an
#:   ``E`` prefix. ``Ranma 1-2`` is a name, and no group writes a range that
#:   way; ``01-02`` is a two-episode pack, and every group writes one that way.
#: * **The separator may not be a hyphen glued to a digit**, which is what a
#:   date is: the ``05-10`` of ``Show.2024-05-10`` would otherwise be episodes
#:   5 to 10. Checked after the separator is consumed, so the two characters
#:   behind the cursor are the digit and the hyphen.
#: * **The range is the last thing before the tags**, exactly as a single
#:   episode number is (:data:`_DASH_EPISODE_RE`): a bracket, the extension or
#:   the end of the name has to follow it. ``[Commie] Chihayafuru - 01 - 100
#:   Poems [ABCD1234]`` writes an episode title after the number, and episodes
#:   1 to 100 is not what it says.
#: * **Plausibility is checked in code**, not here: the run has to count up
#:   from at least 1, and two numbers that are both plausible *years* are a
#:   year pair rather than a run — which is what stops ``Gundam 00 - 05`` (a
#:   title ending in a padded zero), ``Mob Psycho 100 - 07`` (counting down)
#:   and ``Cowboy Bebop 1998-1999`` being ranges.
#:
#: The separator is any dash or tilde a group might write, the wave dash
#: ``〜`` and the fullwidth ``～`` included: a Japanese raw writes ``01〜12``
#: and means exactly what ``01-12`` means.
#:
#: A leading ``S01`` on either end is allowed and ignored; the season is read
#: elsewhere and ``S1-S4`` names no episodes at all.
_EPISODE_RANGE_RE = re.compile(
    r"(?:^|[\s\-_.\[\(#])(?<!\d-)"
    r"(?:S\d{1,2}\s*)?(?:(?:E|EP|EPISODE)\s*(?P<low>\d{1,4})|(?P<lowpad>\d{2,4}))(?:v\d)?"
    r"\s*[-~〜～–—]\s*"
    r"(?:S\d{1,2}\s*)?(?:(?:E|EP|EPISODE)\s*(?P<high>\d{1,4})|(?P<highpad>\d{2,4}))(?:v\d)?"
    r"(?=\s*[\[({\])}]|\.[A-Za-z0-9]{2,4}$|\s*$)",
    re.IGNORECASE,
)

#: The same question asked *leniently*, and only ever of a pair anitopy has
#: already called a range: does that pair sit where a range sits — with nothing
#: but a tag, an extension or the end of the name behind it? The numbers need
#: no padding here, because whether this is a range is not what is being asked.
#: ``[Commie] Chihayafuru - 01 - 100 Poems [ABCD1234]`` is episode 1 of a show
#: whose episode title starts with a number, and anitopy reports the pair
#: ``(1, 100)`` for it.
_RANGE_BEFORE_TAG_RE = re.compile(
    r"(?<![\w])(\d{1,4})(?:v\d)?\s*[-~〜～–—]\s*(\d{1,4})(?:v\d)?"
    r"(?=\s*[\[({\])}]|\.[A-Za-z0-9]{2,4}$|\s*$)",
    re.IGNORECASE,
)

#: The dashes and tildes a range may be written with, as a character class for
#: :func:`_without_range` to build a pattern out of.
_RANGE_SEPARATORS = r"[-~〜～–—]"

_RANGE_RE = re.compile(r"(?<![\w])(\d{1,4})\s*[-~]\s*(\d{1,4})(?![\w])")

#: Shortest run the *unpadded* fallbacks count as a batch. Two, so that
#: ``Ranma 1-2`` — which anitopy reports as episodes 1 and 2 — is not read as a
#: two-episode batch of a show called Ranma: plenty of shows have a fraction or
#: a hyphenated number in their name, and none of them write it padded.
#: :data:`_EPISODE_RANGE_RE` is not held to this, because a padded ``01-02``
#: really is a two-episode pack (``[RH] Fukigen na Mononokean - 01-02``) and
#: fetching it is fetching an episode nobody asked for.
MIN_BATCH_SPAN = 2

#: The episode number as the last thing before the tags: ``- 03 [1080p]``,
#: ``- E03.mkv``, ``#03 (…)``. Only consulted when anitopy's own answer was
#: rejected, and the *last* match wins, because an earlier one is part of the
#: title (``Ranma 1-2 (2024) - 03``).
_DASH_EPISODE_RE = re.compile(
    r"[-_#]\s*(?:E|EP|EPISODE)?\s*(\d{1,4})(?:v\d)?\s*(?=[\[(]|\.[A-Za-z0-9]{2,4}$|$)",
    re.IGNORECASE,
)

#: ``v2`` / ``v3`` on the episode number, which anitopy misses when it sits
#: outside the number it recognised.
_VERSION_RE = re.compile(r"(?<=\d)v(\d)\b", re.IGNORECASE)

#: A fractional episode: ``- 12.5``. The convention is universal and means one
#: thing — a recap, a summary or a bonus that sits *between* two numbered
#: episodes — so the number in front of the dot is the episode it follows and
#: the file is a :data:`Kind` ``special``. Only consulted for anitopy's own
#: answer and for the dash form, because a bare ``1.5`` in a filename is far
#: more often a version, a ratio or part of a name.
_FRACTION_RE = re.compile(r"^(\d{1,4})\.(\d)$")
_DASH_FRACTION_RE = re.compile(
    r"[-_#]\s*(?:E|EP|EPISODE)?\s*(\d{1,4})\.(\d)\s*(?=[\[(]|\.[A-Za-z0-9]{2,4}$|$)",
    re.IGNORECASE,
)

#: ``第3話`` / ``第03話`` — "episode 3" as a Japanese raw release writes it.
#: Read for the number *and* stripped out of the title: leaving it in would
#: make every episode of a show a different title, and none of them the one
#: the catalogue carries. ``第3期`` (season) is deliberately not here; it is a
#: different claim and nothing yet reads it.
_JP_EPISODE_RE = re.compile(r"第\s*0*(\d{1,4})\s*話")

#: The half of that marker anitopy leaves behind when the release spaces it
#: out (``第 3 話``): it claims the ``3`` as the episode number and hands back
#: a title ending in a bare ``第``. Only a *standalone* character is dropped,
#: so a title whose last word genuinely ends in one (``昔話``) is untouched.
_JP_DANGLING_RE = re.compile(r"(?:(?<=\s)|^)[第話]\s*$")

#: A four-digit year in brackets or parentheses. Only 1950..2099 counts, so a
#: resolution or an episode number never reads as one.
_YEAR_RE = re.compile(r"[\[(](19[5-9]\d|20\d\d)[\])]")

#: Fallbacks for the three technical fields, read off the raw filename when
#: anitopy did not classify them. It has no vocabulary for ``4K`` or ``AV1``
#: at all, and it misses ``WEB-DL`` inside a dot-separated scene stem — all
#: three of which are ordinary things for a release to say.
_RESOLUTION_IN_NAME = re.compile(
    r"(?:^|[\s\-_.\[\(])(\d{3,4}p|\d{3,4}[xX]\d{3,4}|4K|UHD)(?:[\s\-_.\]\)]|$)"
)
_SOURCE_IN_NAME = re.compile(
    r"(?:^|[\s\-_.\[\(])(BDRip|BDMV|BD|Blu-?Ray|WEB-?DL|WEBRip|WEB|AMZN|HDTV|DVDRip|DVD)"
    r"(?:[\s\-_.\]\)]|$)",
    re.IGNORECASE,
)
_CODEC_IN_NAME = re.compile(
    r"(?:^|[\s\-_.\[\(])(AV1|HEVC|x265|H\.?265|x264|H\.?264|AVC|XviD)(?:[\s\-_.\]\)]|$)",
    re.IGNORECASE,
)

#: The scene convention: dot-separated stem, group after a trailing hyphen —
#: ``Show.Name.S02E05.1080p.WEB.H264-GROUP``.
_SCENE_GROUP_RE = re.compile(r"^(?=[^\s]+$).*\.[^.\s]*-([A-Za-z][A-Za-z0-9]{1,15})$")

#: Ordinals spelled out. AniList writes "Monogatari Series: Second Season"
#: and "Fate/Zero 2nd Season" for the same idea, and the matcher compares the
#: two, so both have to reduce to the number 2.
ORDINAL_WORDS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
}
_ORDINAL_ALTERNATION = "|".join(ORDINAL_WORDS)

#: Season written as words, at the end of a title. Ordered longest-first so
#: ``2nd Season`` is not consumed by the bare-``Season`` branch.
_SEASON_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\s*[-:]?\s*\b(\d{1,2})(?:st|nd|rd|th)\s+Season\b\s*$", re.IGNORECASE),
    re.compile(rf"\s*[-:]?\s*\b({_ORDINAL_ALTERNATION})\s+Season\b\s*$", re.IGNORECASE),
    re.compile(r"\s*[-:]?\s*\bSeason\s*(\d{1,2})\b\s*$", re.IGNORECASE),
    re.compile(r"\s*[-:]?\s*\bS(\d{1,2})\b\s*$"),
)

#: The *K-ON!!* convention: a title that ends in a run of exclamation marks
#: names its season by how many there are. ``K-ON!`` is season 1 and ``K-ON!!``
#: is season 2 — and they are two different shows with different episode
#: counts, which nothing else in a filename distinguishes once punctuation is
#: flattened for matching. Two or more marks only: a single ``!`` is just
#: enthusiasm (*Bocchi the Rock!*, *Yuru Camp*).
_SHOUT_SEASON_RE = re.compile(r"^(?P<head>.*[^\s!])(?P<marks>!{2,4})\s*$")

#: A season marker sitting *before* a subtitle rather than at the end:
#: ``Mushoku Tensei II - Isekai Ittara Honki Dasu``. Only the marker is
#: removed; the subtitle stays, because it is part of the name the catalogue
#: carries too.
_MID_SEASON_RE = re.compile(
    r"(?<=\S)\s+(?:(\d{1,2})(?:st|nd|rd|th)\s+Season|Season\s*(\d{1,2})|S(\d{1,2})"
    r"|(II|III|IV|VI|VII|VIII|IX))\s*(?=[-:]\s)",
    re.IGNORECASE,
)

#: ``Part 2`` / ``Cour 2`` anywhere in the filename. A release that names both
#: a season and a part — ``2nd Season Part 2`` — hands anitopy the part as an
#: "episode title" rather than as part of the show's name, so the title alone
#: never sees it. Sets :attr:`ParsedName.part` only, never the season: which
#: of the two a bare ``Part 2`` means is decided by the title rule below, and
#: this fallback has no more information than that one did.
_PART_IN_NAME = re.compile(r"\b(?:Part|Cour)\s*(\d{1,2})\b", re.IGNORECASE)

#: ``Part 2`` / ``Cour 2``. Read as a season only when nothing else named one;
#: releases use it both for "the second cour of season 1" and for "season 2",
#: and the second reading is the one that helps a match.
_PART_RE = re.compile(r"\s*[-:]?\s*\b(?:Part|Cour)\s*(\d{1,2})\b\s*$", re.IGNORECASE)

#: A trailing Roman numeral, with at least one word before it.
_ROMAN_RE = re.compile(r"^(?P<head>.*\S)\s+(?P<numeral>[IVX]{2,5})\s*$")

#: ``S02E05`` in any casing. anitopy reads this itself for two-digit episode
#: numbers, but gives up on ``S01E1089`` — which is precisely how the scene
#: names One Piece — so it is re-read here whenever anitopy found no number.
_SXXEXX_RE = re.compile(r"\bS(\d{1,2})\s*E(\d{1,4})\b", re.IGNORECASE)

#: A type marker left at the end of a title: ``Show Name - OVA 02`` names the
#: show ``Show Name``, not ``Show Name OVA``.
_TYPE_TAIL_RE = re.compile(
    r"\s*[-:]?\s*[\[(]?\b(?:ova|oav|ona|specials?|sp|extras?|omake|bonus|movie)\b[\])]?\s*$",
    re.IGNORECASE,
)

#: Terminal markers a fansub adds to the last episode of a run. **Case
#: sensitive**: groups shout them (``- 28 END``), and matching ``End`` too
#: would take the last word off *Frieren: Beyond Journey's End*.
_END_RE = re.compile(r"\s*[\[(]?\b(?:END|FIN|FINAL|COMPLETE)\b[\])]?\s*$")

#: A release that carries an **English dub** instead of the original audio:
#: ``[English Dub]``, ``[Eng Dub]``, ``[Dubbed]``, ``(Dub)``, ``[DUB]``,
#: ``[EN DUB]``. The word has to stand on its own, which is what keeps
#: ``[KaiDubs]`` (a group name, read separately) and any title containing
#: "Dubai" out of it.
#:
#: **``Dual Audio`` is deliberately not here.** A dual-audio release carries
#: the original track as well, so it is an ordinary candidate that happens to
#: be bigger; only a release with *nothing but* the dub is the one FR-A3 ranks
#: last. The distinction is the whole reason this is a pattern rather than a
#: search for the substring "dub".
_DUB_RE = re.compile(r"(?:^|[\s\-_.\[\(])dub(?:bed|s)?(?:[\s\-_.\]\)]|$)", re.IGNORECASE)

#: And the other way a release says it: the group's own name. ``[KaiDubs]``,
#: ``[AnimeDub]`` — a group whose name *ends* in "dub" or "dubs" uploads
#: nothing else, and two of the releases production picked yesterday said it
#: this way and no other. Only the ending counts: a name merely containing the
#: letters is not a claim about the audio.
_DUB_GROUP_RE = re.compile(r"dubs?$", re.IGNORECASE)

#: Everything that is not a letter, a digit or a space, for :func:`title_key`.
#: Flattened to spaces rather than deleted: ``Re:Zero`` and ``Re Zero`` must
#: produce one key, and deleting would make ``rezero`` out of one and
#: ``re zero`` out of the other. ``\W`` is Unicode-aware here, so kana and
#: kanji survive and only punctuation goes.
#:
#: This is **everything**, because both sides of every comparison go through
#: it and a difference that survives on neither side costs nothing. The
#: acquisition query builder's :data:`~arc.services.acquisition.nyaa.SYMBOLS`
#: is the short, evidenced subset of the same idea: each character it strips
#: costs an extra request to Nyaa, so it lists only the punctuation groups are
#: known to drop (``☆``, ``!``, ``:``, ``/``) rather than all of it.
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ParsedName:
    """What Arc made of one filename. Stored verbatim in ``media_files.parsed``."""

    #: The filename as given, including the extension.
    raw: str
    #: The show's title, cleaned but with its original casing, for display.
    title: str
    #: The same title lowercased with punctuation flattened, for matching.
    #: Every similarity the matcher computes is computed on this.
    title_key: str
    kind: Kind = "unknown"
    episode: int | None = None
    #: The last episode of a range (``[01-12]`` → 1 and 12). ``None`` for a
    #: single file, which is what makes ``episode_end is not None`` the test
    #: for "this is a batch".
    episode_end: int | None = None
    #: The fraction of a ``12.5``-style number: ``0.5``, and ``None`` for the
    #: ordinary case. A recap between episodes 12 and 13 is *not* episode 12,
    #: so the two are stored apart — :attr:`episode` places the file in the
    #: run and this says it sits after that episode rather than being it. The
    #: file's :attr:`kind` is ``special``, which is what keeps it from being
    #: linked over the real episode 12.
    episode_fraction: float | None = None
    season: int | None = None
    #: The cour, when a release names one: ``Part 2``, ``Cour 2``. Kept apart
    #: from :attr:`season` because the two are different claims — *Shingeki no
    #: Kyojin Season 3* and *Shingeki no Kyojin Season 3 Part 2* are two
    #: entries in the catalogue with the same season number — even though a
    #: release that names *only* a part usually means the season.
    part: int | None = None
    #: ``v2`` and friends. ``None``, never 1: an unversioned release says
    #: nothing, and storing 1 would make it look like it had.
    version: int | None = None
    group: str | None = None
    #: Normalised to ``1080p`` / ``720p`` / ``2160p`` / …, so ``1920x1080``
    #: and ``4K`` do not read as three different resolutions.
    resolution: str | None = None
    #: ``BD`` | ``WEB`` | ``TV`` | ``DVD``.
    source: str | None = None
    #: ``H264`` | ``H265`` | ``AV1`` | ``XVID``.
    codec: str | None = None
    year: int | None = None
    #: Lowercase, without the dot.
    extension: str | None = None
    #: Whether the release says it carries an **English dub** rather than the
    #: original audio with subtitles (:data:`_DUB_RE`). A ranking input for
    #: acquisition (FR-A3: a dub ranks below every subbed candidate) and a fact
    #: worth storing about a file that arrived anyway. ``Dual Audio`` is not a
    #: dub: it has the original track too, so it is an ordinary candidate.
    dubbed: bool = False

    @property
    def is_batch(self) -> bool:
        return self.kind == "batch"

    @property
    def episode_span(self) -> tuple[int, ...]:
        """Every episode number this file claims to hold."""
        if self.episode is None:
            return ()
        end = self.episode_end if self.episode_end is not None else self.episode
        if end < self.episode:
            return (self.episode,)
        return tuple(range(self.episode, end + 1))

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe mapping for ``media_files.parsed``."""
        return asdict(self)


def title_key(text: str) -> str:
    """The matching form of a title: lowercase, punctuation flattened.

    ``Kaguya-sama wa Kokurasetai: Ultra Romantic`` and ``Kaguya sama wa
    Kokurasetai Ultra Romantic`` are the same show written by two groups, and
    a matcher that scored them apart would send half the library to review.
    """
    return re.sub(r"\s+", " ", _PUNCT_RE.sub(" ", text.casefold())).strip()


def _as_list(value: Any) -> list[str]:
    """anitopy returns a str for one hit and a list for several."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _first_int(values: list[str]) -> int | None:
    for value in values:
        digits = re.sub(r"[^0-9]", "", value)
        if digits:
            return int(digits)
    return None


def _found(pattern: re.Pattern[str], text: str) -> str | None:
    """The first capture of ``pattern`` in ``text``, or ``None``."""
    matched = pattern.search(text)
    return matched.group(1) if matched else None


def _found_all(pattern: re.Pattern[str], text: str) -> list[str]:
    """Every capture of ``pattern`` in ``text``."""
    return [matched.group(1) for matched in pattern.finditer(text)]


def _normalise_resolution(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().lower()
    if value in {"4k", "uhd"}:
        return "2160p"
    matched = re.fullmatch(r"(\d{3,4})\s*[xX*]\s*(\d{3,4})", value)
    if matched:
        return f"{int(matched.group(2))}p"
    matched = re.fullmatch(r"(\d{3,4})p?", value)
    if matched:
        return f"{int(matched.group(1))}p"
    return raw.strip()


def _normalise_source(terms: list[str]) -> str | None:
    joined = " ".join(terms).lower()
    if not joined:
        return None
    if "blu" in joined or re.search(r"\bbd(?:rip|mv)?\b", joined):
        return "BD"
    if "web" in joined or "amzn" in joined or "cr" == joined.strip():
        return "WEB"
    if "dvd" in joined:
        return "DVD"
    if "tv" in joined or "hdtv" in joined:
        return "TV"
    return None


def _normalise_codec(terms: list[str]) -> str | None:
    joined = " ".join(terms).lower()
    if not joined:
        return None
    if "265" in joined or "hevc" in joined:
        return "H265"
    if "av1" in joined:
        return "AV1"
    if "264" in joined or "avc" in joined:
        return "H264"
    if "xvid" in joined:
        return "XVID"
    return None


def _is_tech_word(word: str) -> bool:
    """Whether one whitespace-delimited token is a tag rather than a word."""
    stripped = word.strip("[](){}.,-_")
    if not stripped:
        return True
    return stripped.lower() in _TECH_WORDS or bool(_RESOLUTION_TOKEN.match(stripped))


def _strip_brackets(title: str) -> str:
    """Drop leftover ``[...]`` / ``(...)`` groups that hold no words.

    A bracket that reads as a checksum, a resolution or a subtitle note is
    noise; one that holds real words — ``(Frieren: Beyond Journey's End)``,
    which is a title a source also carries — is left alone.
    """

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner or _BRACKET_NOISE.match(inner):
            return " "
        if all(_is_tech_word(word) for word in re.split(r"[\s_]+", inner) if word):
            return " "
        return match.group(0)

    title = re.sub(r"\[([^\]]*)\]", replace, title)
    return re.sub(r"\(([^)]*)\)", replace, title)


def _strip_tech_tail(title: str) -> str:
    """Peel technical words, ``END`` markers and separators off both ends."""
    previous = None
    while previous != title:
        previous = title
        title = _END_RE.sub("", title)
        title = re.sub(r"[\s.]*\b(?:\d{3,4}p|\d{3,4}[xX]\d{3,4}|4[Kk])\b[\s.]*$", "", title)
        tail = re.search(r"[\s._-]+([A-Za-z0-9][A-Za-z0-9-]*)\s*$", title)
        if tail and tail.group(1).lower() in _TECH_WORDS:
            title = title[: tail.start()]
        title = title.strip(" \t._-~")
    return title


def _clean_title(raw: str | None, *, group: str | None = None) -> str:
    """anitopy's title, with everything that is not the title taken off.

    ``group`` is passed in because a scene name puts it *inside* what anitopy
    calls the title (``A.Silent.Voice.2016.2160p.BluRay.HEVC-TERMINAL``), and
    the tail-peeler stops at the first token it does not recognise — which
    would be the group, leaving the four tags in front of it in place.
    """
    if not raw:
        return ""
    title = raw
    if group and title.endswith(f"-{group}"):
        title = title[: -len(group) - 1]
    title = title.replace("_", " ")
    title = _strip_brackets(title)
    title = _NC_RE.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip(" \t._-~")
    title = _strip_tech_tail(title)
    return re.sub(r"\s+", " ", title).strip(" \t._-~")


def _marker_value(marker: str) -> int:
    """``"2"`` → 2, ``"second"`` → 2."""
    text = marker.strip().lower()
    return ORDINAL_WORDS.get(text, 0) or int(re.sub(r"[^0-9]", "", text) or 0)


class SeasonMark(NamedTuple):
    """What a title said about its place in a franchise, and what is left."""

    title: str
    season: int | None = None
    part: int | None = None
    #: The franchise name the marker was attached to — everything *in front of*
    #: it. Usually the same string as :attr:`title`, and different exactly when
    #: the marker sat before a subtitle: ``Mushoku Tensei III: Isekai Ittara
    #: Honki Dasu`` leaves ``title`` as ``Mushoku Tensei : Isekai Ittara Honki
    #: Dasu`` — right for matching, useless as a search query — and ``base`` as
    #: ``Mushoku Tensei``, which is the name release groups actually write.
    base: str = ""


def _season_from_title(title: str) -> SeasonMark:
    """Split a trailing season marker off ``title``.

    Applied repeatedly, because ``… 2nd Season Part 2`` carries two of them
    and the outer (``Part 2``) must come off before the inner one is visible.
    The first *numbered season* wins; a ``Part``/``Cour`` only counts when no
    season was named, since it means "cour 2" as often as "season 2" and the
    latter is the reading that helps.
    """
    title = _SEASON_RANGE_RE.sub(" ", title).strip(" \t._-~:")
    season: int | None = None
    part: int | None = None
    base: str | None = None

    middle = _MID_SEASON_RE.search(title)
    if middle:
        marker = next(value for value in middle.groups() if value)
        season = _ROMAN.get(marker.upper()) or _marker_value(marker)
        base = title[: middle.start()].strip(" \t._-~:") or None
        title = (title[: middle.start()] + " " + title[middle.end() :]).strip(" \t._-~:")
        title = re.sub(r"\s+", " ", title)

    changed = True
    while changed and title:
        changed = False
        shout = _SHOUT_SEASON_RE.match(title)
        if shout:
            if season is None:
                season = len(shout.group("marks"))
            title, changed = shout.group("head").strip(" \t._-~:"), True
            continue
        for pattern in _SEASON_PATTERNS:
            matched = pattern.search(title)
            if matched:
                head = title[: matched.start()].strip(" \t._-~:")
                if not head:
                    continue
                if season is None:
                    season = _marker_value(matched.group(1))
                title, changed = head, True
                break
        if changed:
            continue
        matched = _PART_RE.search(title)
        if matched:
            head = title[: matched.start()].strip(" \t._-~:")
            if head:
                if part is None:
                    part = int(matched.group(1))
                title, changed = head, True
                continue
        roman = _ROMAN_RE.match(title)
        if roman:
            value = _ROMAN.get(roman.group("numeral").upper())
            head = roman.group("head").strip(" \t._-~:")
            if value is not None and head:
                if season is None:
                    season = value
                title, changed = head, True
    if season is None:
        season = part
    stripped = title.strip(" \t._-~:")
    return SeasonMark(stripped, season, part, base or stripped)


def strip_season(title: str) -> SeasonMark:
    """``"Overlord IV"`` → ``("Overlord", 4, None, "Overlord")``. The public form.

    Exported because the matcher runs *catalogue* titles through exactly this
    before comparing them with a filename's: "Vinland Saga Season 2" and a
    file parsed as season 2 of "Vinland Saga" have to reduce to the same key,
    or the season would be scored twice — once as a title difference and once
    as a season agreement — and the first would drown the second.
    """
    return _season_from_title(title)


def _kind_of(
    name: str,
    parsed: dict[str, Any],
    *,
    title: str,
    episode: int | None,
    episode_end: int | None,
    fraction: float | None = None,
) -> Kind:
    """Decide what the file is, most specific first.

    ``nc`` outranks everything: a creditless opening inside a movie release is
    still a creditless opening, and linking it to the movie would put an
    eighty-second file where a two-hour one belongs.
    """
    types = {value.lower() for value in _as_list(parsed.get("anime_type"))}
    haystack = f" {name} "
    if types & {"ncop", "nced", "op", "ed", "pv", "cm", "trailer", "preview", "teaser"}:
        return "nc"
    if _NC_RE.search(haystack):
        return "nc"
    if episode_end is not None:
        return "batch"
    if _BATCH_MARKER_RE.search(haystack):
        return "batch"
    if _COMPLETE_MARKER_RE.search(haystack) and episode is None:
        # A range has already returned above, so what is left here is either a
        # whole show with no numbers (a batch) or one file a group marked as
        # the last of its run (episode N, and ``END`` by another name).
        return "batch"
    if types & {"movie", "gekijouban"}:
        return "movie"
    if types & {"ova", "oav", "ona", "special", "sp", "specials", "extra", "omake", "bonus"}:
        return "special"
    if _MOVIE_RE.search(haystack) or _MOVIE_RE.search(f" {title} "):
        return "movie"
    if _SPECIAL_RE.search(haystack):
        return "special"
    if fraction is not None:
        # ``12.5`` is the convention for a recap or a bonus that sits between
        # two episodes. It is not episode 12, and linking it as one would put
        # a twelve-minute summary where the episode belongs.
        return "special"
    if episode is not None:
        return "episode"
    return "unknown"


class EpisodeNumbers(NamedTuple):
    """What a filename said about which episode it holds."""

    episode: int | None = None
    episode_end: int | None = None
    version: int | None = None
    fraction: float | None = None


def _fraction_of(numbers: list[str], name: str) -> tuple[int, float] | None:
    """``12.5`` as ``(12, 0.5)``, from anitopy's answer or from the name.

    Checked before anything else strips the dot: ``re.sub(r"[^0-9]", "", …)``
    turns ``"12.5"`` into ``125``, which is an episode number no show has and
    a match nothing would ever make.
    """
    for value in numbers:
        matched = _FRACTION_RE.match(value.strip())
        if matched:
            return int(matched.group(1)), int(matched.group(2)) / 10.0
    tail = list(_DASH_FRACTION_RE.finditer(name))
    if tail:
        return int(tail[-1].group(1)), int(tail[-1].group(2)) / 10.0
    return None


def _without_range(title: str, low: int, high: int) -> str:
    """``"Show - 01〜12"`` → ``"Show"``, and only for *this* file's own run.

    Needed because anitopy knows ``-`` and ``~`` and not ``〜``: given
    ``- 01 ~ 12`` it hands back the title ``Show``, and given ``- 01〜12`` it
    hands back ``Show - 01〜12`` — a title the matcher could never match, on
    exactly the file a person has to look at.

    The pattern is built from the two numbers rather than searched for
    generically, so nothing else that looks like a pair can be caught by it:
    the ``86`` of *86* and the ``07-`` of *07-Ghost* are parts of names, and a
    generic scan would also swallow ``86 - 01`` whole and never see the real
    range behind it. Leading zeros are allowed on either end, because the
    title writes ``01`` where this has the number 1.
    """
    pattern = re.compile(
        rf"(?<![\w])0*{low}\s*{_RANGE_SEPARATORS}\s*0*{high}(?![\w])",
    )
    return pattern.sub(" ", title)


def _is_year(value: int) -> bool:
    """Whether a number could be a release year rather than an episode."""
    return YEAR_RANGE[0] <= value <= YEAR_RANGE[1]


def _explicit_range(name: str, first: int | None) -> tuple[int, int] | None:
    """The episode range the release wrote out, ``(low, high)``, or ``None``.

    The first thing asked of a name, and the reason is that anitopy does not
    answer this question: ``[Erai-raws] Dagashi Kashi 2 - 01 ~ 12 [1080p]`` is
    reported as episode ``01``, full stop, and a release Arc reads as a single
    episode 1 is one it downloads and transcodes twelve times over. So
    :data:`_EPISODE_RANGE_RE` is read off the raw name before anitopy's answer
    is trusted, and what it finds wins.

    ``first`` is anitopy's own first number, and for a *bare* range it is a
    veto: one whose low end is not the number anitopy read is not the episode
    position at all but two numbers that happen to sit either side of a dash —
    the ``12 - 24`` of a title ending in a number, followed by episode 24. When
    anitopy read no number there is nothing to disagree with and the range
    stands on its own, which is the ``[01-23]``-in-a-noise-bracket case. An
    ``E``-prefixed range (``E01-E12``) skips the veto: nothing but an episode
    range is written that way, and anitopy reads the pair as the single number
    112.
    """
    for matched in _EPISODE_RANGE_RE.finditer(name):
        prefixed = matched.group("low") is not None or matched.group("high") is not None
        low_text = matched.group("low") or matched.group("lowpad")
        high_text = matched.group("high") or matched.group("highpad")
        low, high = int(low_text), int(high_text)
        # Counting up, from a real episode, to a number a show could reach.
        if not 0 < low < high <= 9999:
            continue
        if _is_year(low) and _is_year(high):
            # ``[Kametsu] Cowboy Bebop 1998-1999`` is when it aired, not what
            # it holds. Both ends, because episode 1998 of nothing exists but
            # ``0001-2000`` of One Piece is a run somebody really does upload.
            continue
        if first is not None and low != first and not prefixed:
            continue
        return low, high
    return None


def _range_sits_before_a_tag(name: str, low: int, high: int) -> bool:
    """Whether ``low``-``high`` appears in ``name`` with only a tag behind it.

    Asked of anitopy's own pair, which it reports for two numbers either side
    of a dash wherever they sit — including the episode number and the first
    word of an episode *title*: ``[Commie] Chihayafuru - 01 - 100 Poems`` is
    reported as ``(1, 100)`` and is episode 1.
    """
    return any(
        int(matched.group(1)) == low and int(matched.group(2)) == high
        for matched in _RANGE_BEFORE_TAG_RE.finditer(name)
    )


def _episode_numbers(name: str, parsed: dict[str, Any]) -> EpisodeNumbers:
    """anitopy's answer about the numbers, corrected.

    A range the release wrote out (:func:`_explicit_range`) is read first and
    settles the question. Failing that, anitopy reports a range as a two-element
    list, which is the same batch case by another route; ``[01-12]`` inside a
    bracket it treated as noise is neither, so the raw name is re-read for a
    loose range whenever it produced a single number and the name still holds
    one.

    Both of those looser routes are held to the same two conditions the strict
    pattern applies in code — the pair may not be two plausible years, and it
    must sit before a tag (:func:`_range_sits_before_a_tag`) — because anitopy
    pairs numbers wherever it finds them: ``- 01 - 100 Poems`` is episode 1 of
    a show whose episode title opens with a number, not episodes 1 to 100.
    """
    numbers = _as_list(parsed.get("episode_number"))
    version = _first_int(_as_list(parsed.get("release_version")))
    if version is None:
        matched = _VERSION_RE.search(name)
        if matched:
            version = int(matched.group(1))

    fractional = _fraction_of(numbers, name)
    if fractional is not None:
        return EpisodeNumbers(fractional[0], None, version, fractional[1])

    values = [int(re.sub(r"[^0-9]", "", value) or -1) for value in numbers]
    values = [value for value in values if value >= 0]

    explicit = _explicit_range(name, values[0] if values else None)
    if explicit is not None:
        return EpisodeNumbers(explicit[0], explicit[1], version, None)

    episode: int | None = None
    episode_end: int | None = None
    rejected_range = False
    if values:
        episode = values[0]
        if len(values) > 1 and values[-1] > episode:
            if values[-1] - episode < MIN_BATCH_SPAN:
                # anitopy read a hyphenated number in the title as a range.
                episode, rejected_range = None, True
            elif _range_sits_before_a_tag(name, episode, values[-1]):
                episode_end = values[-1]
            # Otherwise anitopy paired the episode number with a number that
            # opens the episode *title*. The episode stands; there is no span.

    if episode_end is None and not rejected_range:
        for matched in _RANGE_RE.finditer(name):
            low, high = int(matched.group(1)), int(matched.group(2))
            # A range is only a range if it counts up, stays plausible, is not
            # a pair of years, and has nothing but a tag behind it: a
            # resolution (1920-1080), a date (2023-10), an airing span
            # (1998-1999) and the ``- 01 - 100 Poems`` of an episode title that
            # opens with a number all look like one otherwise.
            if 0 < low < high <= 9999 and high - low >= MIN_BATCH_SPAN:
                if episode is not None and low != episode:
                    continue
                if _is_year(low) and _is_year(high):
                    continue
                if not _range_sits_before_a_tag(name, low, high):
                    continue
                episode, episode_end = low, high
                break

    if episode is None:
        tail = list(_DASH_EPISODE_RE.finditer(name))
        if tail:
            episode = int(tail[-1].group(1))
    if episode is None:
        japanese = _JP_EPISODE_RE.search(name)
        if japanese:
            episode = int(japanese.group(1))
    return EpisodeNumbers(episode, episode_end, version, None)


def basename(name: str, *, path: bool) -> str:
    """The last path component of ``name``, or ``name`` itself.

    ``/`` is **both** a path separator and a title character — *Fate/Zero*,
    *Fate/strange Fake*, *Kizumonogatari II/III* — and nothing about the string
    says which it is. So the **caller** says, because the caller is the only
    one that knows:

    * ``path=True`` for a string that came off the filesystem (the ingest
      scanner, the match job). It is split on its last separator and nowhere
      else, which is not a heuristic but a fact about filesystems: no path
      component can contain a slash, so the last one is always the one before
      the filename.
    * ``path=False`` for a release *name* — a Nyaa title, a corpus line, a
      name a person typed. It is never split, whatever it contains.

    Guessing was the previous version of this, and it guessed wrong in both
    directions: ``[SubsPlease] Fate/Zero - 12 (1080p)`` became a show called
    ``Zero`` with no release group (so the acquisition filter rejected every
    release of the show and the library sent every file of it to review), and a
    ``" / "`` inside a tag looked exactly like a directory. One boolean at
    three call sites replaces all of it.
    """
    if not path:
        return name
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _group_of(name: str, stem: str, parsed: dict[str, Any]) -> str | None:
    """The release group: anitopy's, or the scene's trailing ``-GROUP``."""
    group = parsed.get("release_group")
    if isinstance(group, list):
        group = group[0] if group else None
    if group:
        cleaned = str(group).strip(" []()_-")
        if cleaned:
            return cleaned
    matched = _SCENE_GROUP_RE.match(stem)
    if matched:
        return matched.group(1)
    return None


def parse(name: str, *, path: bool = False) -> ParsedName:
    """Parse one release filename. Deterministic, pure, never raises.

    ``path=True`` says ``name`` came off the filesystem, and only then is its
    last component taken (:func:`basename`); by default a slash is part of the
    title, because the strings that carry one are release names. A name anitopy
    cannot make anything of comes back with ``kind="unknown"`` and whatever
    title could be salvaged, which is a review item, not an error.
    """
    raw = name
    base = basename(name, path=path)
    stem, _, extension = base.rpartition(".")
    if not stem:  # no dot at all
        stem, extension = base, ""
    extension = extension.lower() if extension.lower() in VIDEO_EXTENSIONS else ""

    try:
        parsed: dict[str, Any] = anitopy.parse(base) or {}
    except Exception:  # pragma: no cover - anitopy is not documented to raise
        parsed = {}

    episode, episode_end, version, fraction = _episode_numbers(base, parsed)

    season = _first_int(_as_list(parsed.get("anime_season")))
    if episode is None:
        matched = _SXXEXX_RE.search(base)
        if matched:
            season = season if season is not None else int(matched.group(1))
            episode = int(matched.group(2))

    group = _group_of(base, stem, parsed)
    title = _clean_title(parsed.get("anime_title"), group=group)
    part: int | None = None
    if " " not in title and title.count(".") >= 2:
        # A scene stem anitopy handed back verbatim: the dots are its spaces.
        title = title.replace(".", " ").strip()
    title = _SXXEXX_RE.sub(" ", title).strip(" \t._-~:")
    if episode is not None and episode_end is not None:
        title = _without_range(title, episode, episode_end).strip(" \t._-~:")
    title = _JP_EPISODE_RE.sub(" ", title).strip(" \t._-~:")
    title = _JP_DANGLING_RE.sub("", title).strip(" \t._-~:")
    title = re.sub(r"\s+", " ", title)
    marked = _season_from_title(title)
    title, title_season, part = marked.title, marked.season, marked.part
    if part is None:
        loose_part = _PART_IN_NAME.search(base)
        part = int(loose_part.group(1)) if loose_part else None
    title = _TYPE_TAIL_RE.sub("", title).strip(" \t._-~:") or title
    if season is None:
        season = title_season

    # A scene release puts the year in the title with nothing around it —
    # ``Your.Name.2016.1080p.BluRay.x264-GROUP``. Only for a scene stem (no
    # spaces at all in the filename): a bare four-digit number at the end of a
    # spaced title is part of the name — *Ghost in the Shell SAC_2045*,
    # *Chihayafuru 3* — not a release year.
    scene_year: int | None = None
    if " " not in stem:
        trailing = _BARE_YEAR_RE.search(title)
        if trailing and trailing.end() == len(title) and trailing.start() > 0:
            scene_year = int(trailing.group(1))
            title = title[: trailing.start()].strip(" \t._-~:")

    kind = _kind_of(
        base, parsed, title=title, episode=episode, episode_end=episode_end, fraction=fraction
    )
    if kind in {"nc", "movie"}:
        # A creditless opening's "01" is an OP index and a movie's is a part
        # number; neither is an episode of anything.
        episode = episode if kind == "movie" and episode_end is not None else None
        episode_end = None
        fraction = None
        if kind == "nc":
            episode = None

    year = _first_int(_as_list(parsed.get("anime_year")))
    if year is None:
        matched = _YEAR_RE.search(base)
        year = int(matched.group(1)) if matched else scene_year

    dubbed = bool(_DUB_RE.search(f" {base} ")) or bool(
        group and _DUB_GROUP_RE.search(group.strip(" []()_-"))
    )

    return ParsedName(
        raw=raw,
        title=title,
        title_key=title_key(title),
        kind=kind,
        episode=episode,
        episode_end=episode_end,
        episode_fraction=fraction,
        season=season,
        part=part,
        version=version,
        group=group,
        resolution=_normalise_resolution(
            next(iter(_as_list(parsed.get("video_resolution"))), None)
            or _found(_RESOLUTION_IN_NAME, base)
        ),
        source=_normalise_source(
            _as_list(parsed.get("source")) or _found_all(_SOURCE_IN_NAME, base)
        ),
        codec=_normalise_codec(
            _as_list(parsed.get("video_term")) or _found_all(_CODEC_IN_NAME, base)
        ),
        year=year,
        extension=extension or None,
        dubbed=dubbed,
    )


__all__ = [
    "ORDINAL_WORDS",
    "EpisodeNumbers",
    "SeasonMark",
    "VIDEO_EXTENSIONS",
    "Kind",
    "ParsedName",
    "basename",
    "parse",
    "strip_season",
    "title_key",
]
