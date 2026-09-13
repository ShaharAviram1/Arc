"""The Nyaa client, query builder, filter and ranker (FR-A3, FR-A4).

``tests/fixtures/nyaa/search_frieren_07.xml`` was captured from the real feed
(``?page=rss&q=Sousou no Frieren - 07&c=1_2&f=0``) and is left exactly as it
came back, because what makes it worth having is precisely the junk: episode 7
from five groups at three resolutions, the *second season*'s episode 7, a
remake, an English dub of episode 12, and two 01–07 batches. Every one of them
matched the query and only some of them are the file.

The two ``search_mushoku_s3_11_*.xml`` fixtures were captured the same way and
are here for the opposite reason: they do not overlap at all. Nyaa ANDs the
words of a query, so the catalogue's own title (``Mushoku Tensei III: Isekai
Ittara Honki Dasu - 11``) returns the eight releases that spell the subtitle
out, the short form (``Mushoku Tensei S3 - 11``) returns the six that do not,
and the two feeds share no info hash whatsoever. The most seeded 1080p file of
that episode is in the short feed alone, which is why stopping at the first
query that returned *something* was a correct ranking over the wrong pool.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from arc.models import Anime
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition.nyaa import (
    CATEGORY,
    MIN_INTERVAL,
    TRACKERS,
    Candidate,
    NyaaClient,
    NyaaItem,
    NyaaUnavailable,
    acceptable,
    anime_season,
    anime_titles,
    filter_items,
    has_prequel,
    head_of,
    is_single,
    pad,
    parse_feed,
    queries,
    rank,
    search_for_episode,
    shared_client,
    strip_symbols,
    title_score,
)
from arc.services.acquisition.rules import Rules
from arc.services.library.parser import parse, strip_season
from tests.acquisition_helpers import NyaaStub, force_transport, no_sleep, read_fixture


def relation(kind: str, *, anilist_id: int) -> dict[str, Any]:
    """One ``anime.relations`` blob, shaped as ``catalog.cache`` stores it."""
    return {
        "anilist_id": anilist_id,
        "mal_id": None,
        "relation_type": kind,
        "format": "TV",
        "title": {"romaji": None, "english": None, "native": None, "preferred": None},
    }


FRIEREN_S1 = Anime(
    anilist_id=154587,
    title_romaji="Sousou no Frieren",
    title_english="Frieren: Beyond Journey's End",
    episodes=28,
    #: A sequel and nothing in front of it — so the head forms apply.
    relations=[relation("SEQUEL", anilist_id=175482)],
)
FRIEREN_S2 = Anime(
    anilist_id=175482,
    title_romaji="Sousou no Frieren 2nd Season",
    title_english="Frieren: Beyond Journey's End Season 2",
    episodes=24,
)
#: AniList row 178789, copied field for field out of the dev catalogue. The
#: season marker sits *before* the subtitle, which is what makes the catalogue
#: title and the release name diverge.
MUSHOKU_S3 = Anime(
    anilist_id=178789,
    title_romaji="Mushoku Tensei III: Isekai Ittara Honki Dasu",
    title_english="Mushoku Tensei: Jobless Reincarnation Season 3",
    title_native="無職転生 III ～異世界行ったら本気だす～",
    synonyms=["Mushoku Tensei: Isekai Ittara Honki Dasu 3rd Season"],
    episodes=14,
)
#: Split at the colon the way the query builder splits it, because the heads
#: are what the assertions are about.
RAKUDAI_HEAD = "Rakudai Kenja no Gakuin Musou"
RAKUDAI_ENGLISH_HEAD = "From Overshadowed to Overpowered"
RAKUDAI_ROMAJI = f"{RAKUDAI_HEAD}: Nidome no Tensei, S-Rank Cheat Majutsushi Bouken-roku"
RAKUDAI_ENGLISH = f"{RAKUDAI_ENGLISH_HEAD}: Second Reincarnation of a Talentless Sage"
#: AniList row 190123, copied field for field out of the dev catalogue. No
#: season marker anywhere, and a subtitle in both languages — so every query
#: built from a whole title returns **zero** results, which is what the owner
#: watched happen three times on 2026-09-12 before the episode went onto the
#: six-hour retry schedule. ``Rakudai Kenja no Gakuin Musou 01`` had nine. No
#: relations either, and that is deliberate: nothing comes before it.
RAKUDAI = Anime(
    anilist_id=190123,
    title_romaji=RAKUDAI_ROMAJI,
    title_english=RAKUDAI_ENGLISH,
    episodes=12,
)
#: AniList row 97986. The sequel that carries **no season marker at all** — its
#: title names the arc — and therefore the one entry a bare head query must not
#: be asked for: ``[SubsPlease] Made in Abyss - 07`` is season one's episode 7,
#: there is no season on either side to disagree, and the release being shorter
#: than the catalogue title is what ``title_score`` calls normal. The
#: ``PREQUEL`` edge is the only thing that knows.
MADE_IN_ABYSS_S2 = Anime(
    anilist_id=97986,
    title_romaji="Made in Abyss: Retsujitsu no Ougonkyou",
    title_english="Made in Abyss - The Golden City of the Scorching Sun",
    episodes=12,
    relations=[
        relation("PREQUEL", anilist_id=34599),
        relation("SIDE_STORY", anilist_id=100643),
    ],
)
MADE_IN_ABYSS_TITLES = (
    "Made in Abyss: Retsujitsu no Ougonkyou",
    "Made in Abyss - The Golden City of the Scorching Sun",
)
#: AniList row 205068, copied field for field out of the production catalogue.
#: Seven episodes, ``FINISHED``, no season marker in either title — and every
#: release of it on Nyaa names the episode the **Western** way. Set to watching
#: on 2026-09-13; episodes 1 and 2 went ``searching``, all five query forms
#: returned zero, and the six-hour retry would have repeated that forever. By
#: hand the same day: ``One-Room TA - 01`` → 0, ``One-Room TA 01`` → 0,
#: ``One-Room TA`` → 9, seven of the nine being ``S01Exx`` singles.
ONE_ROOM = Anime(
    anilist_id=205068,
    title_romaji="Wollum Jogyonim",
    title_english="One-Room TA",
    status="FINISHED",
    episodes=7,
)
ONE_ROOM_TITLES = ("Wollum Jogyonim", "One-Room TA")
#: The two ToonsHub singles and the two batches, verbatim from nyaa.si. The
#: first single carries the entry's *romaji* title in a parenthetical, which is
#: the asymmetric ``title_score`` exception: extra tokens that are one of the
#: show's own other names do not lower the score.
TOONSHUB_EPISODE_2 = (
    "[ToonsHub] One-Room TA S01E02 1080p VIKI WEB-DL AAC2.0 H.264 (Wollum Jogyonim, Multi-Subs)"
)
TOONSHUB_EPISODE_7 = "[ToonsHub] One-Room TA S01E07 1080p VIKI WEB-DL AAC2.0 H.264 (Multi-Subs)"
DOOMDOS_BATCH = "[Doomdos] - One-Room TA - S01E01-03 [1080p VIKI WEB-DL BATCH]"
GECKYZZ_BATCH = (
    "[geckyzz] One-Room TA - S01 (원룸 조교님; One Room Jogyo-nim) "
    "[VIKI.WEB-DL 1080P AVC, AAC, M-SUB][BATCH]"
)

FEED = read_fixture("search_frieren_07.xml")
EMPTY = read_fixture("search_empty.xml")
#: ``q=Mushoku Tensei S3 - 11`` and ``q=Mushoku Tensei III: Isekai Ittara Honki
#: Dasu - 11``, captured the same afternoon. Disjoint by info hash.
MUSHOKU_SHORT = read_fixture("search_mushoku_s3_11_short.xml")
MUSHOKU_FULL = read_fixture("search_mushoku_s3_11_full.xml")

#: Twelve distinct forms are built for this entry and ten of them fit
#: :data:`MAX_QUERIES`. The two that fall off the end are the **symbol-stripped
#: variants of the full titles**, which is the ordering the cap is written for
#: (2026-09-14): every romaji form — including all three short forms, which is
#: what SubsPlease writes — comes first, then the english ones, and the
#: speculative variant last. The head forms add nothing: the romaji base is
#: ``Mushoku Tensei`` with no subtitle left in it, and the english head is
#: ``Mushoku Tensei`` too, which the third short form already asked for. The
#: synonym adds nothing either: its head is that same ``Mushoku Tensei``.
MUSHOKU_QUERIES = [
    "Mushoku Tensei III: Isekai Ittara Honki Dasu - 11",
    "Mushoku Tensei: Jobless Reincarnation Season 3 - 11",
    "Mushoku Tensei S03E11",
    "Mushoku Tensei S3 - 11",
    "Mushoku Tensei III - 11",
    "Mushoku Tensei - 11",
    "Mushoku Tensei: Jobless Reincarnation S03E11",
    "Mushoku Tensei: Jobless Reincarnation S3 - 11",
    "Mushoku Tensei: Jobless Reincarnation III - 11",
    "Mushoku Tensei: Jobless Reincarnation - 11",
]
#: The two forms the ``search_mushoku_s3_11_*`` fixtures were captured for,
#: named rather than indexed: the ``SxxEyy`` forms now sit between them.
MUSHOKU_FULL_QUERY = MUSHOKU_QUERIES[0]
MUSHOKU_SHORT_QUERY = "Mushoku Tensei S3 - 11"
SUBSPLEASE_1080P = "[SubsPlease] Mushoku Tensei S3 - 11 (1080p) [4492A492].mkv"


# --- Parsing the feed -------------------------------------------------------


def test_the_captured_feed_parses_into_every_item() -> None:
    items = parse_feed(FEED)

    assert len(items) == 20
    assert items[0].title.startswith("[Erai-raws] Sousou no Frieren 2nd Season - 07")


def test_an_item_carries_the_nyaa_namespaced_fields() -> None:
    first = parse_feed(FEED)[0]

    assert first.seeders == 128
    assert first.leechers == 0
    assert first.downloads == 6148
    assert first.info_hash == "e2d71232c3a51a6bbfe181861537f698aab01285"
    assert first.size == "585.1 MiB"
    assert first.trusted is True
    assert first.remake is False
    assert first.category_id == "1_2"
    assert first.link == "https://nyaa.si/download/2088261.torrent"


def test_a_remake_is_read_off_the_feed() -> None:
    raze = next(item for item in parse_feed(FEED) if item.title.startswith("[Raze]"))

    assert raze.remake is True
    assert raze.seeders == 4


def test_an_empty_feed_is_an_empty_list_not_an_error() -> None:
    assert parse_feed(EMPTY) == []


def test_a_page_that_is_not_xml_raises() -> None:
    with pytest.raises(NyaaUnavailable):
        parse_feed("<html>just a moment…</html>\x00")


def test_the_magnet_is_built_from_the_hash_and_nyaa_trackers() -> None:
    first = parse_feed(FEED)[0]

    magnet = first.magnet

    assert magnet.startswith("magnet:?xt=urn:btih:e2d71232c3a51a6bbfe181861537f698aab01285")
    assert "&dn=" in magnet
    assert magnet.count("&tr=") == len(TRACKERS)
    assert "nyaa.tracker.wf" in magnet


# --- Query building ---------------------------------------------------------


def test_episode_numbers_are_padded_to_two_digits() -> None:
    assert pad(7) == "07"
    assert pad(12) == "12"


def test_a_long_show_pads_to_three() -> None:
    assert pad(7, total_episodes=500) == "007"
    assert pad(105, total_episodes=500) == "105"


#: The order :func:`queries` documents: full romaji, full english, the two
#: ``SxxEyy`` forms, the season short forms, the head of romaji, the head of
#: english, bare romaji.
FRIEREN_S1_QUERIES = [
    "Sousou no Frieren - 07",
    "Frieren: Beyond Journey's End - 07",
    "Sousou no Frieren S01E07",
    "Frieren - 07",
    "Sousou no Frieren 07",
    "Frieren: Beyond Journey's End S01E07",
    "Frieren Beyond Journey's End - 07",
]


def test_queries_are_built_in_the_documented_order() -> None:
    """The romaji title has no subtitle; the english one does, so ``Frieren``.

    The two ``SxxEyy`` forms sit third and fourth — the entry names no season,
    so they say ``S01`` — and the head form stays in front of the bare one: a
    group writing ``Frieren - 07`` is likelier than one writing the romaji
    title with no dash.
    """
    assert queries(FRIEREN_S1, 7) == FRIEREN_S1_QUERIES


def test_a_show_whose_title_names_a_season_gets_the_short_forms_too() -> None:
    """Eight forms, and the head of the english title is not one of them.

    The two ``SxxEyy`` forms carry the season this entry names (``S02``) and
    the english short forms are built before the head forms, so there is still
    only room for the first of those and ``Frieren - 07`` falls off the end.
    That is the intended trade: ``Frieren: Beyond Journey's End S2`` names the
    season this entry is, and ``Frieren`` on its own does not.
    """
    built = queries(FRIEREN_S2, 7)

    assert built == [
        "Sousou no Frieren 2nd Season - 07",
        "Frieren: Beyond Journey's End Season 2 - 07",
        "Sousou no Frieren S02E07",
        "Sousou no Frieren S2 - 07",
        "Sousou no Frieren II - 07",
        "Sousou no Frieren - 07",
        "Frieren - 07",
        "Frieren: Beyond Journey's End S02E07",
        "Frieren: Beyond Journey's End S2 - 07",
        "Frieren: Beyond Journey's End II - 07",
    ]
    assert len(built) <= nyaa_module.MAX_QUERIES


def test_both_titles_are_asked_for_in_the_sxxeyy_form() -> None:
    """The bug One-Room TA found, in one assertion (FR-A4).

    Nyaa ANDs the words of a query and ``01`` is not a word of ``S01E01``, so
    every form that writes the number as ``- 01`` or as a bare ``01`` misses a
    group that writes it the Western way — which is all of ToonsHub, geckyzz
    and Doomdos for this show. The entry names no season, so both forms say
    ``S01``, and they sit third and fourth: the two in front of them are what
    returned zero.
    """
    assert anime_season(ONE_ROOM) is None
    assert queries(ONE_ROOM, 1) == [
        "Wollum Jogyonim - 01",
        "One-Room TA - 01",
        "Wollum Jogyonim S01E01",
        "Wollum Jogyonim 01",
        "One-Room TA S01E01",
    ]
    assert "One-Room TA S01E02" in queries(ONE_ROOM, 2)


def test_the_sxxeyy_form_names_the_season_the_entry_names() -> None:
    """``S03E11`` for a third season, off the season-stripped base.

    The same divergence :func:`_short_forms` covers, in the other convention:
    the catalogue writes *Mushoku Tensei III: Isekai Ittara Honki Dasu* and a
    scene-style release writes ``Mushoku Tensei S03E11``.
    """
    assert "Mushoku Tensei S03E11" in queries(MUSHOKU_S3, 11)
    assert "Sousou no Frieren S02E07" in queries(FRIEREN_S2, 7)
    assert "Sousou no Frieren S01E07" in queries(FRIEREN_S1, 7)


def test_the_sxxeyy_episode_is_not_padded_to_three_for_a_long_show() -> None:
    """``S01E1089`` is what the scene writes for One Piece, never ``S01E089``."""
    anime = Anime(anilist_id=21, title_romaji="One Piece", title_english=None, episodes=1100)

    assert queries(anime, 1089) == [
        "One Piece - 1089",
        "One Piece S01E1089",
        "One Piece 1089",
    ]


def test_a_subtitled_title_is_also_asked_for_by_its_head() -> None:
    """The bug, in one assertion (FR-A4).

    Nyaa ANDs every word, so nine words of catalogue title match nothing at
    all; ``Rakudai Kenja no Gakuin Musou - 01`` matched nine releases the same
    night. Both languages get a head form, because which of the two a group
    writes is not knowable in advance.
    """
    assert queries(RAKUDAI, 1) == [
        f"{RAKUDAI_ROMAJI} - 01",
        f"{RAKUDAI_ENGLISH} - 01",
        f"{RAKUDAI_ROMAJI} S01E01",
        "Rakudai Kenja no Gakuin Musou - 01",
        "From Overshadowed to Overpowered - 01",
        f"{RAKUDAI_ROMAJI} 01",
        f"{RAKUDAI_ENGLISH} S01E01",
        f"{strip_symbols(RAKUDAI_ROMAJI)} - 01",
        f"{strip_symbols(RAKUDAI_ENGLISH)} - 01",
    ]
    assert head_of(RAKUDAI_ROMAJI) == RAKUDAI_HEAD
    assert head_of(RAKUDAI_ENGLISH) == RAKUDAI_ENGLISH_HEAD
    assert has_prequel(RAKUDAI) is False, "no relations stored, so nothing is in front"


def test_a_dash_separated_subtitle_yields_a_head_too() -> None:
    """``" - "`` separates as a colon does; ``"-"`` inside a word does not.

    The same show as :data:`MADE_IN_ABYSS_S2` with its relations not yet
    fetched, which is deliberately the *opposite* outcome: an absent relation
    list is not evidence of a prequel, and the rows Arc knows least about are
    the ones that need the head form most.
    """
    anime = Anime(
        anilist_id=6,
        title_romaji="Made in Abyss - Retsujitsu no Ougonkyou",
        title_english=None,
        episodes=12,
    )

    assert has_prequel(anime) is False
    assert queries(anime, 7) == [
        "Made in Abyss - Retsujitsu no Ougonkyou - 07",
        "Made in Abyss - Retsujitsu no Ougonkyou S01E07",
        "Made in Abyss - 07",
        "Made in Abyss - Retsujitsu no Ougonkyou 07",
    ]


def test_an_entry_with_a_prequel_asks_no_head_query_at_all() -> None:
    """The guard, and the one case the filter cannot defend (FR-A4).

    ``Made in Abyss - 07`` would return season one's episode 7, and both
    checks that make every other broad form safe would pass it: no season is
    marked on either side, and a release name shorter than the catalogue title
    is the ordinary case of a group abbreviating an official name (see
    :func:`test_a_group_that_shortens_the_official_title_still_matches`). So
    the query is not asked.

    The two ``SxxEyy`` forms need no such gate: they are built from the
    season-stripped *base*, which for this entry is the whole subtitled title,
    so neither of them is a bare head either.
    """
    built = queries(MADE_IN_ABYSS_S2, 7)

    assert has_prequel(MADE_IN_ABYSS_S2) is True
    assert head_of("Made in Abyss: Retsujitsu no Ougonkyou") == "Made in Abyss"
    assert built == [
        "Made in Abyss: Retsujitsu no Ougonkyou - 07",
        "Made in Abyss - The Golden City of the Scorching Sun - 07",
        "Made in Abyss: Retsujitsu no Ougonkyou S01E07",
        "Made in Abyss: Retsujitsu no Ougonkyou 07",
        "Made in Abyss - The Golden City of the Scorching Sun S01E07",
        "Made in Abyss Retsujitsu no Ougonkyou - 07",
    ]
    assert "Made in Abyss - 07" not in built
    assert "Made in Abyss S01E07" not in built


def test_a_sequel_relation_does_not_withhold_the_head_form() -> None:
    """Only a ``PREQUEL`` disqualifies. Frieren S1 has a sequel, not a prequel."""
    assert has_prequel(FRIEREN_S1) is False
    assert queries(FRIEREN_S1, 7) == FRIEREN_S1_QUERIES
    assert "Frieren - 07" in FRIEREN_S1_QUERIES


def test_a_missing_relation_list_is_not_evidence_of_a_prequel() -> None:
    """``None`` and ``[]`` both keep the head forms; casing does not matter."""
    for relations in (None, []):
        anime = Anime(
            anilist_id=9,
            title_romaji="Kaguya-sama wa Kokurasetai: Tensai-tachi no Renai Zunousen",
            title_english=None,
            episodes=12,
            relations=relations,
        )

        assert has_prequel(anime) is False
        assert "Kaguya-sama wa Kokurasetai - 07" in queries(anime, 7)

    lowercase = Anime(
        anilist_id=10,
        title_romaji="Kaguya-sama wa Kokurasetai: Tensai-tachi no Renai Zunousen",
        title_english=None,
        episodes=12,
        relations=[relation("prequel", anilist_id=11), "not a dict"],  # type: ignore[list-item]
    )

    assert has_prequel(lowercase) is True
    assert "Kaguya-sama wa Kokurasetai - 07" not in queries(lowercase, 7)


def test_a_title_with_no_subtitle_adds_no_head_form() -> None:
    """Nothing to drop, so nothing extra to ask — not even for the dedupe."""
    assert head_of("Mushishi") == ""
    assert head_of("Sousou no Frieren") == ""
    assert queries(FRIEREN_S1, 7).count("Sousou no Frieren - 07") == 1


def test_a_colon_inside_a_word_is_not_a_subtitle_separator() -> None:
    """``Re:Zero`` is one name. ``Re`` is not a query worth two seconds.

    The english ``SxxEyy`` form loses its closing dash, which is not a typo:
    every form built from a season-stripped base is trimmed of the punctuation
    the strip cut at, and ``-Starting Life in Another World-`` was bracketed in
    dashes rather than ending in a word.
    """
    anime = Anime(
        anilist_id=7,
        title_romaji="Re:Zero kara Hajimeru Isekai Seikatsu",
        title_english="Re:ZERO -Starting Life in Another World-",
        episodes=25,
    )

    assert head_of("Re:Zero kara Hajimeru Isekai Seikatsu") == ""
    assert queries(anime, 7) == [
        "Re:Zero kara Hajimeru Isekai Seikatsu - 07",
        "Re:ZERO -Starting Life in Another World- - 07",
        "Re:Zero kara Hajimeru Isekai Seikatsu S01E07",
        "Re:Zero kara Hajimeru Isekai Seikatsu 07",
        "Re:ZERO -Starting Life in Another World S01E07",
        "Re Zero kara Hajimeru Isekai Seikatsu - 07",
        "Re ZERO -Starting Life in Another World- - 07",
    ]


def test_a_head_is_trimmed_of_the_punctuation_it_was_cut_at() -> None:
    assert head_of("Kaguya-sama wa Kokurasetai: Tensai-tachi no Renai Zunousen") == (
        "Kaguya-sama wa Kokurasetai"
    )
    assert head_of("Grisaia no Kajitsu ~Le Fruit de la Grisaia~") == "Grisaia no Kajitsu"
    assert head_of("Sword Art Online — Alicization") == "Sword Art Online"


def test_the_query_budget_caps_what_the_builder_produced() -> None:
    """Fourteen distinct forms built, ten asked for: the cap is the budget."""
    assert nyaa_module.MAX_QUERIES == 10
    assert len(queries(MUSHOKU_S3, 11)) == nyaa_module.MAX_QUERIES


def test_a_season_marker_before_a_subtitle_still_yields_the_short_name() -> None:
    """The bug, in one assertion.

    ``Mushoku Tensei III: Isekai Ittara Honki Dasu`` is what AniList calls
    season 3; ``Mushoku Tensei S3`` is what SubsPlease calls it. Nyaa ANDs the
    words, so the first query cannot return the second's releases, and every
    short form has to be asked for by name.
    """
    assert queries(MUSHOKU_S3, 11) == MUSHOKU_QUERIES


def test_the_base_title_is_the_franchise_name_in_front_of_the_marker() -> None:
    """The parser helper the short forms are built from."""
    mid = strip_season("Mushoku Tensei III: Isekai Ittara Honki Dasu")
    trailing = strip_season("Sousou no Frieren 2nd Season")
    plain = strip_season("Mushishi")

    assert (mid.season, mid.base) == (3, "Mushoku Tensei")
    assert mid.title == "Mushoku Tensei : Isekai Ittara Honki Dasu"
    assert (trailing.season, trailing.base) == (2, "Sousou no Frieren")
    assert (plain.season, plain.base) == (None, "Mushishi")


def test_a_show_with_only_a_romaji_title_still_builds_queries() -> None:
    anime = Anime(anilist_id=1, title_romaji="Mushishi", title_english=None)

    assert queries(anime, 3) == ["Mushishi - 03", "Mushishi S01E03", "Mushishi 03"]


def test_the_season_a_catalogue_entry_names_is_read_off_its_title() -> None:
    assert anime_season(FRIEREN_S1) is None
    assert anime_season(FRIEREN_S2) == 2


def test_titles_include_synonyms_and_drop_blanks() -> None:
    anime = Anime(
        anilist_id=2,
        title_romaji="Overlord IV",
        title_english=None,
        title_native="オーバーロードIV",
        synonyms=["Overlord Season 4", "  "],
    )

    assert anime_titles(anime) == ("Overlord IV", "オーバーロードIV", "Overlord Season 4")


# --- Symbols: a star a group does not write (2026-09-14) --------------------


def test_a_symbol_is_collapsed_to_a_single_space() -> None:
    """Between two words it separates; after one it simply goes."""
    assert strip_symbols("Yarichin☆Bitch-bu - 01") == "Yarichin Bitch-bu - 01"
    assert strip_symbols("Love Live! Superstar!! - 03") == "Love Live Superstar - 03"
    assert strip_symbols("Re:Zero kara Hajimeru") == "Re Zero kara Hajimeru"
    assert strip_symbols("Sousou no Frieren - 07") == "Sousou no Frieren - 07"


def test_the_full_titles_get_a_symbol_stripped_variant_at_the_end() -> None:
    """The bug, in one assertion (FR-A4).

    Nyaa ANDs the *tokens* of a query and ``Yarichin☆Bitch-bu`` is one token
    nothing on the site holds, so every form built from the romaji title
    returned zero. The variant is built for the **full titles only** and sits
    at the end of the list: it is a guess about how a group spells a name, and
    a marked entry's short forms — which are names the catalogue actually
    holds — must not fall off the end of the budget behind it.
    """
    anime = Anime(
        anilist_id=98789,
        title_romaji="Yarichin☆Bitch-bu",
        title_english="Yarichin Bitch Club",
        episodes=4,
    )

    assert queries(anime, 1) == [
        "Yarichin☆Bitch-bu - 01",
        "Yarichin Bitch Club - 01",
        "Yarichin☆Bitch-bu S01E01",
        "Yarichin☆Bitch-bu 01",
        "Yarichin Bitch Club S01E01",
        # Last, because it is a guess about how a group spells a name rather
        # than a name the catalogue holds — and the only form here that found
        # the show.
        "Yarichin Bitch-bu - 01",
    ]


def test_a_form_with_no_symbols_produces_no_variant() -> None:
    """Nothing to strip, so nothing extra to ask — and nothing to dedupe."""
    assert queries(FRIEREN_S1, 7).count("Sousou no Frieren - 07") == 1
    assert all("☆" not in query for query in queries(FRIEREN_S1, 7))


def test_the_exclamation_marks_of_a_shouted_title_are_stripped_too() -> None:
    """``Love Live! Superstar!!`` is two tokens on Nyaa and four in the cache."""
    anime = Anime(
        anilist_id=125708,
        title_romaji="Love Live! Superstar!!",
        title_english=None,
        episodes=12,
    )

    assert "Love Live Superstar - 03" in queries(anime, 3)


# --- Films, OVAs and ONAs: one release, no number (2026-09-14) --------------

#: Three production entries that sat in ``searching`` for a day on 2026-09-13,
#: because every query Arc built for them carried ``- 01`` and no film release
#: on Nyaa names an episode.
SAO_PROGRESSIVE = Anime(
    anilist_id=125367,
    title_romaji="Sword Art Online Movie: Progressive - Hoshi Naki Yoru no Aria",
    title_english="Sword Art Online the Movie: Progressive - Aria of a Starless Night",
    format="MOVIE",
    episodes=1,
    season_year=2021,
)
SERVAMP_MOVIE = Anime(
    anilist_id=101168,
    title_romaji="Servamp Movie: Alice in the Garden",
    title_english="Servamp Movie: Alice in the Garden",
    format="MOVIE",
    episodes=1,
    season_year=2018,
)
ROYAL_TUTOR_MOVIE = Anime(
    anilist_id=106578,
    title_romaji="Oushitsu Kyoushi Haine Movie",
    title_english="The Royal Tutor Movie",
    format="MOVIE",
    episodes=1,
    season_year=2019,
)
SERVAMP_RELEASE = "[Erai-raws] Servamp Movie - Alice in the Garden [1080p][Multiple Subtitle].mkv"


def test_a_movie_is_asked_for_by_name_with_no_episode_number() -> None:
    """The bug, in one assertion (FR-A4)."""
    assert is_single(SERVAMP_MOVIE) is True
    assert queries(SERVAMP_MOVIE, 1) == [
        "Servamp Movie: Alice in the Garden",
        "Servamp Movie Alice in the Garden",
    ]
    assert queries(ROYAL_TUTOR_MOVIE, 1) == [
        "Oushitsu Kyoushi Haine Movie",
        "The Royal Tutor Movie",
    ]


def test_a_movies_symbol_stripped_variant_is_what_finds_it() -> None:
    """``Movie:`` is a token; ``Movie`` is the word the group wrote."""
    built = queries(SAO_PROGRESSIVE, 1)

    assert built[1] == "Sword Art Online Movie Progressive - Hoshi Naki Yoru no Aria"
    assert built[3] == "Sword Art Online the Movie Progressive - Aria of a Starless Night"


def test_a_one_episode_ova_is_a_single_and_an_ova_series_is_not() -> None:
    """``episodes == 1`` is the whole of the OVA rule."""
    one_ova = Anime(anilist_id=3, title_romaji="Show OVA", title_english=None, format="OVA")
    four = Anime(
        anilist_id=4, title_romaji="Show OVA", title_english=None, format="OVA", episodes=4
    )
    one_ova.episodes = 1

    assert is_single(one_ova) is True
    assert is_single(four) is False
    assert queries(four, 2) == ["Show OVA - 02", "Show OVA S01E02", "Show OVA 02"]


def test_a_film_release_with_no_episode_number_is_accepted_as_episode_one() -> None:
    candidate = acceptable(
        one(SERVAMP_RELEASE),
        titles=anime_titles(SERVAMP_MOVIE),
        number=1,
        season=None,
        single=True,
        year=SERVAMP_MOVIE.season_year,
    )

    assert candidate is not None
    assert candidate.parsed.kind == "movie"
    assert candidate.parsed.episode is None


#: The three whole-series Blu-ray packs that were accepted as films until
#: 2026-09-14. Each names no episode, no range and no batch marker — so the
#: first version of the single rule read every one of them as "an episode-less
#: single" — and each has a *shorter* title than the entry's, which the
#: ordinary asymmetric comparison scores 1.00.
BLU_RAY_PACKS = [
    "[Coalgirls] Servamp (1920x1080 Blu-ray FLAC)",
    "[Judas] Servamp Movie [BD 1080p]",
    "[Judas] Sword Art Online [BD 1080p][HEVC x265]",
]


@pytest.mark.parametrize("title", BLU_RAY_PACKS, ids=lambda value: value[:28])
def test_a_whole_series_blu_ray_pack_is_not_the_film(title: str) -> None:
    """20 GB of a franchise, offered as one film (FR-A4).

    Two rules reject it and either would do: the release never *says* it is a
    film (:data:`~arc.services.acquisition.nyaa.SINGLE_KINDS`), and a single's
    title comparison forgives only the type word itself, so ``servamp`` cannot
    reach 0.90 against ``servamp movie alice in the garden``.
    """
    assert (
        acceptable(
            one(title),
            titles=anime_titles(SERVAMP_MOVIE) + anime_titles(SAO_PROGRESSIVE),
            number=1,
            season=None,
            single=True,
            year=None,
        )
        is None
    )


#: *Kizumonogatari* is three films with one name, no episode numbers and no
#: year on the rips — so the strict title rule is the entire margin between
#: them, and the year check has nothing to work with.
KIZUMONOGATARI_III = Anime(
    anilist_id=15689,
    title_romaji="Kizumonogatari III: Reiketsu-hen",
    title_english="Kizumonogatari Part 3: Reiketsu",
    format="MOVIE",
    episodes=1,
    season_year=2017,
)


def test_a_franchise_sibling_film_is_rejected_and_the_right_one_is_not() -> None:
    def check(title: str) -> object:
        return acceptable(
            one(title),
            titles=anime_titles(KIZUMONOGATARI_III),
            number=1,
            season=None,
            single=True,
            year=KIZUMONOGATARI_III.season_year,
        )

    assert check("[Moozzi2] Kizumonogatari III Reiketsu-hen Movie [BD 1920x1080 x264 FLAC]")
    assert check("[Coalgirls] Kizumonogatari I Tekketsu-hen Movie [BD 1080p FLAC]") is None
    assert check("[Coalgirls] Kizumonogatari II Nekketsu-hen Movie [BD 1080p FLAC]") is None
    # And the cost of the rule, stated rather than hidden: a rip that names
    # nothing but the franchise is not distinguishable from a series pack by
    # anything in its name, so it is refused and the episode says so (FR-A6).
    assert check("[Coalgirls] Kizumonogatari [BD 1080p]") is None


def test_a_films_batch_is_still_rejected() -> None:
    """Three films in one torrent is the 6 GB download FR-A4 forbids."""
    assert (
        acceptable(
            one("[Judas] Servamp Movie - Alice in the Garden [BD 1080p][BATCH]"),
            titles=anime_titles(SERVAMP_MOVIE),
            number=1,
            season=None,
            single=True,
        )
        is None
    )


def test_a_numbered_episode_is_not_the_one_release_a_special_entry_is() -> None:
    """A one-episode entry must not take the television series' episode 1.

    ``[SubsPlease] Yuru Camp - 01`` is not *Yuru Camp Specials*: the title
    scores 1.00 against it (the release name is a subset, which is the
    ordinary case of a group shortening an official title) and only the
    episode number says otherwise. A film is the exception — the parser calls
    it a ``movie`` and a number in a film's name is a part index, not an
    episode.
    """
    special = Anime(
        anilist_id=5,
        title_romaji="Yuru Camp Specials",
        title_english=None,
        format="SPECIAL",
        episodes=1,
    )

    assert (
        acceptable(
            one("[SubsPlease] Yuru Camp - 01 (1080p) [A1B2C3D4].mkv"),
            titles=anime_titles(special),
            number=1,
            season=None,
            single=True,
        )
        is None
    )


def test_a_film_of_the_wrong_year_is_rejected() -> None:
    """A franchise's other film carries the same name and a different year."""
    titles = anime_titles(SAO_PROGRESSIVE)
    older = one("[Judas] Sword Art Online the Movie Progressive [BD 1080p] (2016)")
    right = one("[Judas] Sword Art Online the Movie Progressive [BD 1080p] (2022)")

    assert acceptable(older, titles=titles, number=1, season=None, single=True, year=2021) is None
    assert acceptable(right, titles=titles, number=1, season=None, single=True, year=2021)


def test_a_film_that_names_no_year_is_not_held_against_it() -> None:
    """Most releases say nothing, and silence is not a disagreement."""
    assert acceptable(
        one(SERVAMP_RELEASE),
        titles=anime_titles(SERVAMP_MOVIE),
        number=1,
        season=None,
        single=True,
        year=2018,
    )


def test_a_creditless_opening_is_never_the_film() -> None:
    assert (
        acceptable(
            one("[Moozzi2] Servamp Movie Alice in the Garden NCOP [BD 1080p]"),
            titles=anime_titles(SERVAMP_MOVIE),
            number=1,
            season=None,
            single=True,
        )
        is None
    )


# --- Synonyms: the other names a group writes (2026-09-14) ------------------


def test_a_synonym_that_says_something_new_earns_a_query() -> None:
    """manami's vocabulary is what release groups write (FR-A4)."""
    anime = Anime(
        anilist_id=21234,
        title_romaji="Boku no Hero Academia",
        title_english="My Hero Academia",
        synonyms=["Izuku Midoriya: Origin", "Vigilantes"],
        episodes=13,
    )

    built = queries(anime, 3)

    assert "Izuku Midoriya: Origin - 03" in built
    assert "Izuku Midoriya Origin - 03" in built, "and its symbol-stripped variant"
    assert "Vigilantes - 03" in built


def test_at_most_two_synonyms_are_asked_for() -> None:
    anime = Anime(
        anilist_id=21235,
        title_romaji="Show",
        title_english=None,
        synonyms=["Alpha One", "Beta Two", "Gamma Three", "Delta Four"],
        episodes=13,
    )

    built = queries(anime, 3)

    assert [query for query in built if query.startswith(("Alpha", "Beta", "Gamma", "Delta"))] == [
        "Alpha One - 03",
        "Beta Two - 03",
    ]


def test_a_synonym_that_is_not_a_name_earns_nothing() -> None:
    """The synonym list is somebody else's free-text field (FR-A4).

    ``"Season 2"`` and ``"2"`` are in it, and a query for either is a query for
    a quarter of Nyaa. The floor is two words or six characters, which also
    drops a short native-script name — the same decision :func:`queries`
    already makes about ``title_native``, which it never asks for either: it is
    one of the names the *filter* compares against, not one Arc asks the
    english-translated category for.
    """
    anime = Anime(
        anilist_id=21236,
        title_romaji="Shingeki no Kyojin",
        title_english="Attack on Titan",
        synonyms=["Season 2", "進撃の巨人", "2"],
        episodes=25,
    )

    built = queries(anime, 3)

    assert built == [
        "Shingeki no Kyojin - 03",
        "Attack on Titan - 03",
        "Shingeki no Kyojin S01E03",
        "Shingeki no Kyojin 03",
        "Attack on Titan S01E03",
    ]


def test_a_synonym_the_head_rule_already_covers_earns_nothing() -> None:
    """*Mushoku Tensei: … 3rd Season*'s head is the ``Mushoku Tensei`` asked for."""
    assert MUSHOKU_S3.synonyms == ["Mushoku Tensei: Isekai Ittara Honki Dasu 3rd Season"]
    assert all("3rd Season" not in query for query in queries(MUSHOKU_S3, 11))


def test_a_single_asks_its_synonyms_by_name_too() -> None:
    anime = Anime(
        anilist_id=6,
        title_romaji="Kimi no Na wa.",
        title_english="Your Name.",
        synonyms=["Your Name"],
        format="MOVIE",
        episodes=1,
    )

    assert queries(anime, 1) == ["Kimi no Na wa.", "Your Name."]


# --- Filtering --------------------------------------------------------------


def keep_titles(number: int, *, anime: Anime) -> list[str]:
    kept = filter_items(
        parse_feed(FEED),
        titles=anime_titles(anime),
        number=number,
        season=anime_season(anime),
    )
    return [candidate.item.title for candidate in kept]


def test_season_one_keeps_only_the_unmarked_releases() -> None:
    """Six survivors out of twenty: two groups at three resolutions each.

    The ``[amZero]`` and ``[ASW]`` season-one uploads are in the feed too and
    are absent from this list because Nyaa flags both as ``remake``.
    """
    kept = keep_titles(7, anime=FRIEREN_S1)

    assert kept == [
        "[Erai-raws] Sousou no Frieren - 07 [480p][Multiple Subtitle]"
        " [ENG][POR-BR][SPA-LA][SPA][ARA][FRE][GER][ITA][RUS]",
        "[Erai-raws] Sousou no Frieren - 07 [720p][Multiple Subtitle]"
        " [ENG][POR-BR][SPA-LA][SPA][ARA][FRE][GER][ITA][RUS]",
        "[Erai-raws] Sousou no Frieren - 07 [1080p][Multiple Subtitle]"
        " [ENG][POR-BR][SPA-LA][SPA][ARA][FRE][GER][ITA][RUS]",
        "[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv",
        "[SubsPlease] Sousou no Frieren - 07 (720p) [24255A91].mkv",
        "[SubsPlease] Sousou no Frieren - 07 (480p) [69D5A270].mkv",
    ]


def test_season_two_keeps_only_the_season_two_releases() -> None:
    kept = keep_titles(7, anime=FRIEREN_S2)

    assert kept, "the feed does contain season two"
    assert all(("2nd Season" in title or " S2 " in title) for title in kept)
    assert "[SubsPlease] Sousou no Frieren S2 - 07 (1080p) [56170200].mkv" in kept


def test_an_unmarked_release_is_not_offered_to_a_later_season() -> None:
    """``Sousou no Frieren - 07`` is season 1's episode 7, not season 2's."""
    kept = keep_titles(7, anime=FRIEREN_S2)

    assert "[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv" not in kept


def test_a_marked_release_is_not_offered_to_the_first_season() -> None:
    kept = keep_titles(7, anime=FRIEREN_S1)

    assert not any("2nd Season" in title or " S2 " in title for title in kept)


def test_the_remake_is_discarded_even_though_it_is_the_right_season() -> None:
    kept = keep_titles(7, anime=FRIEREN_S2)

    assert not any(title.startswith("[Raze]") for title in kept)


def test_every_remake_in_the_feed_is_discarded() -> None:
    remakes = {item.title for item in parse_feed(FEED) if item.remake}
    kept = set(keep_titles(7, anime=FRIEREN_S1)) | set(keep_titles(7, anime=FRIEREN_S2))

    assert remakes, "the captured feed does contain remakes"
    assert not (remakes & kept)


def test_the_batches_are_discarded() -> None:
    kept = keep_titles(7, anime=FRIEREN_S1)

    assert not any("Episodes 01-07" in title for title in kept)


def test_a_different_episode_number_is_discarded() -> None:
    """The dub of S01E12 is in the feed only because ``07`` is in its hash."""
    kept = keep_titles(7, anime=FRIEREN_S1)

    assert not any("S01E12" in title for title in kept)


def test_episode_twelve_finds_the_dub_and_nothing_else() -> None:
    kept = keep_titles(12, anime=FRIEREN_S1)

    assert kept == [
        "[Yameii] Frieren - Beyond Journey's End - S01E12 [English Dub]"
        " [CR WEB-DL 720p] [07AE0439] (Sousou no Frieren)"
    ]


def mushoku_keep(feed: str) -> list[str]:
    kept = filter_items(
        parse_feed(feed),
        titles=anime_titles(MUSHOKU_S3),
        number=11,
        season=anime_season(MUSHOKU_S3),
    )
    return [candidate.item.title for candidate in kept]


def test_a_short_season_marker_is_accepted_for_the_entry_that_spells_it_out() -> None:
    """``S3`` on the release, ``III`` before a subtitle on the entry: one season.

    The parser reads both — a trailing/mid ``III`` is season 3 and so is ``S3``
    — so the filter needed no change for this. Only the query builder did.
    """
    assert anime_season(MUSHOKU_S3) == 3
    assert mushoku_keep(MUSHOKU_SHORT) == [
        "[SubsPlease] Mushoku Tensei S3 - 11 (1080p) [4492A492].mkv",
        "[SubsPlease] Mushoku Tensei S3 - 11 (720p) [0C3D876B].mkv",
        "[SubsPlease] Mushoku Tensei S3 - 11 (480p) [6D40AB16].mkv",
    ]


def test_the_neighbouring_episode_of_the_right_season_is_rejected() -> None:
    """``S3 - 10`` is in the feed because ``11`` is elsewhere in its row."""
    assert any("S3 - 10" in item.title for item in parse_feed(MUSHOKU_SHORT))
    assert not any("S3 - 10" in title for title in mushoku_keep(MUSHOKU_SHORT))


def test_the_asw_and_raze_encodes_are_dropped_as_remakes() -> None:
    """Both are Nyaa-flagged ``remake``: re-encodes of the SubsPlease file.

    ASW had 907 seeders and would otherwise have been a candidate; the original
    it re-encoded is three rows above it in the same feed.
    """
    remakes = {item.title for item in parse_feed(MUSHOKU_SHORT) if item.remake}

    assert any(title.startswith("[ASW]") for title in remakes)
    assert any(title.startswith("[Raze]") for title in remakes)
    assert not (remakes & set(mushoku_keep(MUSHOKU_SHORT)))


def test_the_full_title_feed_keeps_the_episode_and_drops_the_others() -> None:
    kept = mushoku_keep(MUSHOKU_FULL)

    assert len(kept) == 7
    assert sum(title.startswith("[Erai-raws]") for title in kept) == 3
    assert not any(" - 09 " in title for title in kept), "episode 9 shares the feed"


# --- Sequels: the asymmetry of the title comparison -------------------------

#: A sequel release, and the season-one catalogue entry it must **not** be
#: accepted for. Each of these was accepted before ``title_score`` became
#: asymmetric: every token of the entry's title is in the release's name, and
#: ``token_set_ratio`` scores that 100 whichever way round it is.
SEQUELS: list[tuple[str, tuple[str, ...]]] = [
    (
        "[SubsPlease] Made in Abyss - Retsujitsu no Ougonkyou - 07 (1080p) [A1B2C3D4].mkv",
        ("Made in Abyss",),
    ),
    (
        "[SubsPlease] Kimetsu no Yaiba - Yuukaku-hen - 07 (1080p) [A1B2C3D4].mkv",
        ("Kimetsu no Yaiba", "Demon Slayer: Kimetsu no Yaiba", "Demon Slayer"),
    ),
    (
        "[Erai-raws] Shingeki no Kyojin - The Final Season - 07 [1080p][Multiple Subtitle]",
        ("Shingeki no Kyojin", "Attack on Titan"),
    ),
    (
        "[SubsPlease] Mushoku Tensei Gaiden - 07 (1080p) [A1B2C3D4].mkv",
        ("Mushoku Tensei: Isekai Ittara Honki Dasu", "Mushoku Tensei: Jobless Reincarnation"),
    ),
]


def one(title: str) -> NyaaItem:
    """A feed item that is nothing but its title."""
    return NyaaItem(title=title, link="", info_hash="0" * 40, seeders=10)


@pytest.mark.parametrize(("title", "titles"), SEQUELS, ids=lambda value: str(value)[:40])
def test_a_sequel_is_not_offered_to_the_first_seasons_entry(
    title: str, titles: tuple[str, ...]
) -> None:
    """FR-A4: the wrong season on disk is worse than no file at all."""
    assert acceptable(one(title), titles=titles, number=7, season=None) is None


def test_a_group_that_shortens_the_official_title_still_matches() -> None:
    """The other direction, which is the ordinary case and must not change.

    ``[SubsPlease] Mushoku Tensei - 07`` is episode 7 of *Mushoku Tensei:
    Isekai Ittara Honki Dasu*; nobody writes the whole thing.
    """
    titles = ("Mushoku Tensei: Isekai Ittara Honki Dasu", "Mushoku Tensei: Jobless Reincarnation")

    assert title_score("mushoku tensei", titles) == 1.0
    assert acceptable(
        one("[SubsPlease] Mushoku Tensei - 07 (1080p) [A1B2C3D4].mkv"),
        titles=titles,
        number=7,
        season=None,
    )


def test_a_release_that_writes_two_of_the_shows_own_titles_matches() -> None:
    """The leftover tokens are the show's *own* english title, so it is it."""
    titles = ("Kimetsu no Yaiba", "Demon Slayer: Kimetsu no Yaiba")

    assert title_score("kimetsu no yaiba demon slayer", titles) == 1.0
    assert acceptable(
        one("[SubsPlease] Kimetsu no Yaiba - Demon Slayer - 07 (1080p) [A1B2C3D4].mkv"),
        titles=titles,
        number=7,
        season=None,
    )


def test_the_sequels_own_entry_still_takes_its_own_release() -> None:
    """Rejecting it for season one must not reject it for season two."""
    assert acceptable(
        one("[SubsPlease] Kimetsu no Yaiba - Yuukaku-hen - 07 (1080p) [A1B2C3D4].mkv"),
        titles=("Kimetsu no Yaiba: Yuukaku-hen", "Demon Slayer: Entertainment District Arc"),
        number=7,
        season=None,
    )


def test_a_head_query_cannot_admit_a_subtitled_sequel() -> None:
    """What makes the broad head form safe: the filter, not the query (FR-A4).

    The first season's entry now asks ``Demon Slayer - 07`` as well, the head
    of its english title, and a head form is by construction a subset of every
    subtitled sequel's words: the *Entertainment District* episode 7 comes back
    in that feed. The asymmetric :func:`title_score` is what rejects it —
    ``yuukaku hen`` is in none of the entry's own titles, so the extra tokens
    are scored by ``token_sort_ratio`` and fall under the 0.90 threshold. That
    filter is the safety of the broad query and must not be weakened.
    """
    s1_titles = ("Kimetsu no Yaiba", "Demon Slayer: Kimetsu no Yaiba", "Demon Slayer")

    assert queries(
        Anime(
            anilist_id=8,
            title_romaji="Kimetsu no Yaiba",
            title_english="Demon Slayer: Kimetsu no Yaiba",
            episodes=26,
        ),
        7,
    ) == [
        "Kimetsu no Yaiba - 07",
        "Demon Slayer: Kimetsu no Yaiba - 07",
        "Kimetsu no Yaiba S01E07",
        "Demon Slayer - 07",
        "Kimetsu no Yaiba 07",
        "Demon Slayer: Kimetsu no Yaiba S01E07",
        "Demon Slayer Kimetsu no Yaiba - 07",
    ]
    assert title_score("kimetsu no yaiba yuukaku hen", s1_titles) < 0.90
    assert (
        acceptable(
            one("[SubsPlease] Kimetsu no Yaiba - Yuukaku-hen - 07 (1080p) [A1B2C3D4].mkv"),
            titles=s1_titles,
            number=7,
            season=None,
        )
        is None
    )


def test_the_filter_cannot_save_an_unmarked_sequel_from_a_head_query() -> None:
    """Why :func:`has_prequel` exists rather than a stricter threshold.

    A sequel whose AniList title marks the season is safe without any help: the
    season check reads the unmarked first-season release as season 1 and
    rejects it. A sequel whose title marks nothing but the arc — *Made in
    Abyss: Retsujitsu no Ougonkyou*, *Kimetsu no Yaiba: Yuukaku-hen* — has no
    season to disagree about, and a release named by the head is *shorter* than
    the entry's title, which :func:`title_score` reads as the ordinary case of
    a group abbreviating an official name. Both checks pass and the file is
    season one's.

    Which is a statement about the *filter*, and the filter is right: it cannot
    tell this apart from ``[SubsPlease] Mushoku Tensei - 07`` without knowing
    that something comes before this entry. So the fix is upstream, in
    :func:`queries`, and it is not to weaken anything here — the query is
    simply never asked (see
    :func:`test_an_entry_with_a_prequel_asks_no_head_query_at_all`).
    """
    assert title_score("made in abyss", MADE_IN_ABYSS_TITLES) == 1.0
    assert (
        acceptable(
            one("[SubsPlease] Made in Abyss - 07 (1080p) [ABCD1234].mkv"),
            titles=MADE_IN_ABYSS_TITLES,
            number=7,
            season=None,
        )
        is not None
    )
    assert "Made in Abyss - 07" not in queries(MADE_IN_ABYSS_S2, 7)


def test_a_release_naming_a_different_show_is_discarded() -> None:
    other = Anime(anilist_id=3, title_romaji="Mushishi", title_english=None, episodes=26)

    assert keep_titles(7, anime=other) == []


def test_the_title_threshold_is_what_rejects_a_near_miss() -> None:
    item = parse_feed(FEED)[0]
    titles = ("Sousou no Frieren 2nd Season",)

    assert acceptable(item, titles=titles, number=7, season=2) is not None
    assert acceptable(item, titles=titles, number=7, season=2, threshold=1.01) is None


# --- One-Room TA: the Western naming, end to end -----------------------------


def test_the_toonshub_single_is_accepted_for_the_one_room_ta_entry() -> None:
    """What the ``SxxEyy`` query is for: the release it finds has to pass.

    The entry marks no season, so season 1 is what it means, and ToonsHub says
    ``S01`` — the two agree. The parenthetical that carries the entry's romaji
    title is the asymmetric-score exception and must not cost anything: the
    leftover tokens are one of this show's own names, so the score stays at
    1.00 rather than falling under the 0.90 threshold.
    """
    candidate = acceptable(one(TOONSHUB_EPISODE_2), titles=ONE_ROOM_TITLES, number=2, season=None)

    assert candidate is not None
    assert candidate.parsed.episode == 2
    assert candidate.parsed.season == 1
    assert candidate.parsed.title_key == "one room ta"
    assert candidate.parsed.group == "ToonsHub"
    assert candidate.title_similarity >= 0.90
    assert title_score("one room ta wollum jogyonim", ONE_ROOM_TITLES) >= 0.90

    seventh = acceptable(one(TOONSHUB_EPISODE_7), titles=ONE_ROOM_TITLES, number=7, season=None)

    assert seventh is not None
    assert seventh.parsed.episode == 7


def test_the_toonshub_single_is_still_only_its_own_episode() -> None:
    """``S01E02`` is episode 2 and nothing else — the number check still runs."""
    wrong_number = acceptable(
        one(TOONSHUB_EPISODE_2), titles=ONE_ROOM_TITLES, number=1, season=None
    )

    assert wrong_number is None


@pytest.mark.parametrize("title", [DOOMDOS_BATCH, GECKYZZ_BATCH], ids=["range", "season pack"])
def test_the_one_room_ta_batches_are_rejected(title: str) -> None:
    """FR-A4: a range and a season pack, both under the same Western naming.

    ``S01E01-03`` is three episodes whose low end is the number Arc asked for,
    and ``- S01 … [BATCH]`` names no episode at all. Neither may reach the
    ranker, whatever the new query form returns.
    """
    assert acceptable(one(title), titles=ONE_ROOM_TITLES, number=1, season=None) is None
    assert filter_items([one(title)], titles=ONE_ROOM_TITLES, number=1, season=None) == []


# --- Batches: never fetch a whole season (FR-A4) -----------------------------

#: The seven batches Arc actually picked, from one production acquisition run
#: on 2026-09-12, each paired with the **single episode of the same show, the
#: same season and the same number** from the same group. The pair is the
#: point: the two differ only in that one names a range or a batch marker, so
#: anything that rejects the batch and keeps the single can only be reading
#: that. The first of them cost 6.3 GB and twelve transcodes.
BATCHES: list[tuple[str, str, tuple[str, ...], int, int | None]] = [
    (
        "[Erai-raws] Dagashi Kashi 2 - 01 ~ 12 [1080p][Multiple Subtitle]",
        "[Erai-raws] Dagashi Kashi 2 - 01 [1080p][Multiple Subtitle]",
        ("Dagashi Kashi 2",),
        1,
        None,
    ),
    (
        "[Erai-raws] Shingeki no Kyojin Season 3 Part 2 - 01 ~ 10"
        " [1080p][BATCH][Multiple Subtitle] [ENG][POR-BR]",
        "[Erai-raws] Shingeki no Kyojin Season 3 Part 2 - 01 [1080p][Multiple Subtitle]",
        ("Shingeki no Kyojin Season 3 Part 2", "Attack on Titan Season 3 Part 2"),
        1,
        3,
    ),
    (
        "[Erai-raws] Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2 - 01 ~ 12"
        " [1080p][Multiple Subtitles][Unofficial Batch]",
        "[Erai-raws] Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2 - 01"
        " [1080p][Multiple Subtitle]",
        ("Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season Part 2",),
        1,
        2,
    ),
    (
        "[Erai-raws] Karakai Jouzu no Takagi-san 2 - 01 ~ 12 [1080p][Multiple Subtitle]",
        "[Erai-raws] Karakai Jouzu no Takagi-san 2 - 01 [1080p][Multiple Subtitle]",
        ("Karakai Jouzu no Takagi-san 2",),
        1,
        None,
    ),
    (
        "[Erai-raws] Sword Art Online - Alicization - War of Underworld Part 2 - 01 ~ 11"
        " [1080p][BATCH][Multiple Subtitle] [ENG][POR-BR][SPA-LA][ITA]",
        "[Erai-raws] Sword Art Online - Alicization - War of Underworld Part 2 - 01"
        " [1080p][Multiple Subtitle]",
        ("Sword Art Online: Alicization - War of Underworld Part 2",),
        1,
        2,
    ),
    (
        "[Erai-raws] Quanzhi Gaoshou 2 - 01 ~ 12 [1080p CR WEB-DL AVC AAC][MultiSub] [BATCH]",
        "[Erai-raws] Quanzhi Gaoshou 2 - 01 [1080p CR WEB-DL AVC AAC][MultiSub]",
        ("Quanzhi Gaoshou 2",),
        1,
        None,
    ),
    (
        "[RH] Fukigen na Mononokean - 01-02 (The Morose Mononokean)"
        " [English Dubbed] [uncut] [1080p]",
        "[RH] Fukigen na Mononokean - 01 (The Morose Mononokean) [English Dubbed] [uncut] [1080p]",
        ("Fukigen na Mononokean",),
        1,
        None,
    ),
]


@pytest.mark.parametrize(
    ("batch", "single", "titles", "number", "season"),
    BATCHES,
    ids=lambda value: str(value)[:40],
)
def test_a_batch_is_rejected_and_says_it_was_a_batch(
    batch: str,
    single: str,
    titles: tuple[str, ...],
    number: int,
    season: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FR-A4: only the next N unwatched episodes, never a whole season."""
    with caplog.at_level("DEBUG", logger=nyaa_module.__name__):
        rejected = acceptable(one(batch), titles=titles, number=number, season=season)

    assert rejected is None
    reasons = [getattr(record, "reason", "") for record in caplog.records]
    assert any("batch" in reason for reason in reasons), reasons


@pytest.mark.parametrize(
    ("batch", "single", "titles", "number", "season"),
    BATCHES,
    ids=lambda value: str(value)[:40],
)
def test_the_single_episode_of_the_same_show_is_still_accepted(
    batch: str,
    single: str,
    titles: tuple[str, ...],
    number: int,
    season: int | None,
) -> None:
    """The control. A filter that rejected these too would be no fix at all."""
    candidate = acceptable(one(single), titles=titles, number=number, season=season)

    assert candidate is not None
    assert candidate.parsed.kind == "episode"
    assert candidate.parsed.episode == number


def test_a_marker_only_batch_is_rejected_and_names_one_episode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``[Batch]`` with a single number holds one episode as far as Arc knows.

    So the reason says "episode 12", not "episodes 12-12".
    """
    title = "[Erai-raws] Dagashi Kashi 2 - 12 [1080p][Multiple Subtitle][Batch]"
    with caplog.at_level("DEBUG", logger=nyaa_module.__name__):
        rejected = acceptable(one(title), titles=("Dagashi Kashi 2",), number=12, season=None)

    assert rejected is None
    reasons = [getattr(record, "reason", "") for record in caplog.records]
    assert any("batch release covering episode 12," in reason for reason in reasons), reasons


def test_the_ranker_never_sees_a_batch() -> None:
    """Ranking is downstream of the filter, so it can only pick a survivor."""
    rules = Rules(
        preferred_groups=("Erai-raws",), preferred_resolution="1080p", fallback_resolution="720p"
    )
    batch, single, titles, number, season = BATCHES[0]
    feed = [one(batch), one(single)]

    candidates = filter_items(feed, titles=titles, number=number, season=season)
    ranked = rank(candidates, rules)

    assert [candidate.item.title for candidate in candidates] == [single]
    assert [entry.item.title for entry in ranked] == [single]


def test_every_batch_in_the_production_run_is_filtered_out() -> None:
    """All seven at once, against the show each of them belongs to."""
    for batch, _single, titles, number, season in BATCHES:
        assert filter_items([one(batch)], titles=titles, number=number, season=season) == []


# --- Seeders: zero is not "last", it is "no" (2026-09-13) -------------------

#: A release that is this show's episode 7 in every other respect, so the only
#: thing the tests below vary is the seeder count.
GOOD = "[SubsPlease] Sousou no Frieren - 07 (1080p) [24356E19].mkv"
GOOD_TITLES = ("Sousou no Frieren",)


def seeded(title: str, seeders: int) -> NyaaItem:
    return NyaaItem(title=title, link="", info_hash="0" * 40, seeders=seeders)


def test_a_release_with_no_seeders_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    """It cannot be downloaded at all, so it is not a candidate at any rank."""
    with caplog.at_level("DEBUG", logger=nyaa_module.__name__):
        rejected = acceptable(seeded(GOOD, 0), titles=GOOD_TITLES, number=7, season=None)

    assert rejected is None
    reasons = [getattr(record, "reason", "") for record in caplog.records]
    assert "no seeders" in reasons, reasons


def test_one_seeder_is_enough_to_be_a_candidate() -> None:
    """The rule is a floor at zero, not a quality bar: ranking does the rest."""
    candidate = acceptable(seeded(GOOD, 1), titles=GOOD_TITLES, number=7, season=None)

    assert candidate is not None
    assert candidate.item.seeders == 1


def test_the_ranker_never_sees_a_dead_release() -> None:
    """And the live one below it wins rather than the episode going unfetched."""
    dead = NyaaItem(title=GOOD, link="", info_hash="a" * 40, seeders=0)
    alive = NyaaItem(
        title="[Erai-raws] Sousou no Frieren - 07 [720p][Multiple Subtitle]",
        link="",
        info_hash="b" * 40,
        seeders=12,
    )

    ranked = rank(
        filter_items([dead, alive], titles=GOOD_TITLES, number=7, season=None),
        Rules(preferred_resolution="1080p", fallback_resolution="720p"),
    )

    assert [entry.item.info_hash for entry in ranked] == ["b" * 40]


# --- Ranking ----------------------------------------------------------------


def frieren_candidates() -> list:  # type: ignore[type-arg]
    return filter_items(
        parse_feed(FEED),
        titles=anime_titles(FRIEREN_S1),
        number=7,
        season=None,
    )


def test_the_preferred_group_wins_before_anything_else() -> None:
    rules = Rules(
        preferred_groups=("Erai-raws", "SubsPlease"),
        preferred_resolution="1080p",
        fallback_resolution="720p",
    )

    ranked = rank(frieren_candidates(), rules)

    assert ranked[0].candidate.group == "Erai-raws"
    assert ranked[0].candidate.resolution == "1080p"


def test_resolution_orders_within_one_group() -> None:
    rules = Rules(
        preferred_groups=("Erai-raws",),
        preferred_resolution="1080p",
        fallback_resolution="720p",
    )

    erai = [
        entry for entry in rank(frieren_candidates(), rules) if entry.candidate.group == "Erai-raws"
    ]

    assert [entry.candidate.resolution for entry in erai] == ["1080p", "720p", "480p"]


def test_with_no_group_preference_seeders_decide() -> None:
    rules = Rules(preferred_resolution="1080p", fallback_resolution="720p")

    ranked = rank(frieren_candidates(), rules)
    top = ranked[0]

    best_1080p = max(
        (entry for entry in ranked if entry.candidate.resolution == "1080p"),
        key=lambda entry: entry.seeders,
    )
    assert top.item.info_hash == best_1080p.item.info_hash
    assert top.candidate.resolution == "1080p"


def test_trusted_breaks_a_tie_that_seeders_do_not() -> None:
    def make(title: str, *, trusted: bool) -> Candidate:
        item = NyaaItem(
            title=title, link="", info_hash=title[:8].lower(), seeders=50, trusted=trusted
        )
        return Candidate(item=item, parsed=parse(title), title_similarity=1.0)

    plain = make("[AAA] Show - 07 [1080p].mkv", trusted=False)
    trusted = make("[AAA] Show - 07 [1080p] (x).mkv", trusted=True)

    ranked = rank([plain, trusted], Rules(preferred_resolution="1080p"))

    assert ranked[0].item.trusted is True


def test_a_per_show_override_moves_the_pick() -> None:
    """The same feed, ranked by a rule that names a different group."""
    default = rank(
        frieren_candidates(),
        Rules(preferred_groups=("Erai-raws",), preferred_resolution="1080p"),
    )
    overridden = rank(
        frieren_candidates(),
        Rules(
            preferred_groups=("SubsPlease",),
            preferred_resolution="720p",
            fallback_resolution="1080p",
            overridden=True,
        ),
    )

    assert default[0].candidate.group == "Erai-raws"
    assert overridden[0].candidate.group == "SubsPlease"
    assert overridden[0].candidate.resolution == "720p"
    assert "per-show rule override applied" in overridden[0].reasons


def test_the_reasons_explain_the_pick() -> None:
    rules = Rules(
        preferred_groups=("SubsPlease",),
        preferred_resolution="1080p",
        fallback_resolution="720p",
    )

    top = rank(frieren_candidates(), rules)[0]

    assert "group SubsPlease is preference #1" in top.reasons
    assert "1080p is the preferred resolution" in top.reasons
    assert any(reason.endswith("seeders") for reason in top.reasons)


# --- Dubs: below every subbed release (FR-A3, 2026-09-14) -------------------


def candidate(title: str, *, seeders: int = 50, trusted: bool = False) -> Candidate:
    """One filter-passing candidate, built straight from a release name."""
    item = NyaaItem(
        title=title,
        link="",
        info_hash=f"{abs(hash(title)):040x}"[:40],
        seeders=seeders,
        trusted=trusted,
    )
    return Candidate(item=item, parsed=parse(title), title_similarity=1.0)


#: The two releases production picked on 2026-09-13, both because they were
#: the most seeded upload of their episode — which is FR-A3's third rule doing
#: exactly what it says and exactly the wrong thing.
YAMEII_DUB = "[Yameii] Sword Art Online - S01E07 [English Dub] [CR WEB-DL 1080p] [F00DBABE]"
KAIDUBS_DUB = "[KaiDubs] Bofuri - 07 [1080p].mkv"


def test_a_dub_ranks_below_every_subbed_candidate() -> None:
    """Whatever the seeders, the group preference or the resolution say."""
    dub = candidate(YAMEII_DUB, seeders=4000, trusted=True)
    sub = candidate("[nobody] Sword Art Online - 07 [480p].mkv", seeders=1)

    ranked = rank([dub, sub], Rules(preferred_groups=("Yameii",), preferred_resolution="1080p"))

    assert [entry.item.title for entry in ranked] == [sub.item.title, dub.item.title]
    assert ranked[1].dubbed is True


def test_a_group_that_names_itself_a_dubber_is_read_as_one() -> None:
    """``[KaiDubs] Bofuri - 07`` says nothing else about its audio."""
    dub = candidate(KAIDUBS_DUB, seeders=900)
    sub = candidate("[SubsPlease] Bofuri - 07 (1080p) [A1B2C3D4].mkv", seeders=12)

    ranked = rank([dub, sub], Rules(preferred_resolution="1080p"))

    assert ranked[0].candidate.group == "SubsPlease"
    assert ranked[0].dubbed is False


def test_dual_audio_is_not_a_dub() -> None:
    """It carries the original track too, so it is an ordinary candidate."""
    dual = candidate("[Judas] Bofuri - 07 [BD 1080p][HEVC x265 10bit][Dual-Audio].mkv", seeders=90)
    sub = candidate("[SubsPlease] Bofuri - 07 (1080p) [A1B2C3D4].mkv", seeders=12)

    ranked = rank([dual, sub], Rules(preferred_resolution="1080p"))

    assert ranked[0].item.title == dual.item.title
    assert all(entry.dubbed is False for entry in ranked)


def test_a_dub_that_is_ranked_says_so_in_its_reasons() -> None:
    top = rank([candidate(YAMEII_DUB)], Rules())[0]

    assert "english dub, so ranked below every subbed release" in top.reasons


# --- The client -------------------------------------------------------------


async def test_the_url_carries_the_anime_category_and_no_filter() -> None:
    stub = NyaaStub()
    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        url = client.url_for("Sousou no Frieren - 07")

    assert f"c={CATEGORY}" in url
    assert "f=0" in url
    assert "page=rss" in url


async def test_a_search_parses_what_the_feed_returned() -> None:
    stub = NyaaStub({"Sousou no Frieren - 07": FEED})

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        items = await client.search("Sousou no Frieren - 07")

    assert len(items) == 20
    assert stub.queries == ["Sousou no Frieren - 07"]


async def test_a_repeat_query_is_answered_from_the_cache() -> None:
    stub = NyaaStub({"q": FEED})

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        first = await client.search("q")
        second = await client.search("q")

    assert first == second
    assert stub.queries == ["q"], "the second call must not reach the network"


async def test_the_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = NyaaStub({"q": FEED})

    async with NyaaClient("https://nyaa.test", transport=stub.transport(), cache_ttl=0.0) as client:
        await client.search("q")
        await client.search("q")

    assert stub.queries == ["q", "q"]


async def test_requests_are_spaced_by_the_polite_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep(slept))
    stub = NyaaStub()

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        await client.search("one")
        await client.search("two")
        await client.search("three")

    assert len(slept) == 2, "the first request waits for nothing"
    assert all(0 < pause <= MIN_INTERVAL for pause in slept)


async def test_a_5xx_is_retried_once_and_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep(slept))
    stub = NyaaStub()
    stub.status = 503

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        with pytest.raises(NyaaUnavailable):
            await client.search("q")

    assert len(stub.queries) == 2


async def test_a_5xx_that_recovers_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, text=FEED)

    async with NyaaClient("https://nyaa.test", transport=httpx.MockTransport(handler)) as client:
        items = await client.search("q")

    assert len(items) == 20
    assert attempts["n"] == 2


async def test_a_4xx_is_final(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub()
    stub.status = 404

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        with pytest.raises(NyaaUnavailable):
            await client.search("q")

    assert len(stub.queries) == 1, "asking a 404 again changes nothing"


async def test_a_transport_error_is_retried_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("nope", request=request)

    async with NyaaClient("https://nyaa.test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NyaaUnavailable):
            await client.search("q")

    assert calls["n"] == 2


# --- The whole search -------------------------------------------------------


async def test_the_search_runs_every_query_even_when_the_first_finds_plenty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix: a non-empty first answer is not evidence that it is the pool."""
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub({"Sousou no Frieren - 07": FEED})

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(
            client, FRIEREN_S1, 7, Rules(preferred_groups=("SubsPlease",))
        )

    assert stub.queries == FRIEREN_S1_QUERIES
    assert found.ranked[0].candidate.group == "SubsPlease"
    assert (found.forms, found.results) == (len(FRIEREN_S1_QUERIES), 20)


async def test_the_search_finds_what_only_a_later_query_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub({"Sousou no Frieren 07": FEED})

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(client, FRIEREN_S1, 7, Rules())

    assert stub.queries == FRIEREN_S1_QUERIES
    assert found.ranked


async def test_the_pool_is_the_union_of_every_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two disjoint feeds, one candidate pool, ranked across both.

    Neither feed alone contains both of these: the SubsPlease 1080p is only in
    the short one and the Erai-raws 1080p is only in the full one. Before the
    merge, the full-title query answered first and non-empty and the search
    never asked the other seven.
    """
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub({MUSHOKU_FULL_QUERY: MUSHOKU_FULL, MUSHOKU_SHORT_QUERY: MUSHOKU_SHORT})

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(client, MUSHOKU_S3, 11, Rules())

    assert stub.queries == MUSHOKU_QUERIES
    titles = [entry.item.title for entry in found.ranked]
    assert SUBSPLEASE_1080P in titles
    assert any(title.startswith("[Erai-raws]") and "1080p" in title for title in titles)


async def test_the_merge_keeps_one_row_per_info_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every query answers the same feed; the pool is that feed, not eight of it."""
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub(default=MUSHOKU_SHORT)

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(client, MUSHOKU_S3, 11, Rules())

    assert len(stub.queries) == len(MUSHOKU_QUERIES) == 10
    hashes = [entry.item.info_hash for entry in found.ranked]
    assert len(hashes) == len(set(hashes))


async def test_the_most_seeded_1080p_release_wins_across_the_merged_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-A3 with no group preference: resolution, then seeders — over the union.

    4836 seeders against the full-title feed's best of 4181. Ranking was never
    the bug; being asked to rank six of the fourteen was.
    """
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub({MUSHOKU_FULL_QUERY: MUSHOKU_FULL, MUSHOKU_SHORT_QUERY: MUSHOKU_SHORT})
    rules = Rules(preferred_resolution="1080p", fallback_resolution="720p")

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(client, MUSHOKU_S3, 11, rules)

    assert found.ranked[0].item.title == SUBSPLEASE_1080P
    assert found.ranked[0].item.seeders == 4836
    assert found.ranked[0].candidate.resolution == "1080p"


async def test_every_query_is_still_spaced_by_the_polite_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full budget costs nine pauses, not zero and not a burst."""
    slept: list[float] = []
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep(slept))
    stub = NyaaStub()

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        await search_for_episode(client, MUSHOKU_S3, 11, Rules())

    assert len(stub.queries) == nyaa_module.MAX_QUERIES == 10
    assert len(slept) == 9, "the first request waits for nothing"
    assert all(0 < pause <= MIN_INTERVAL for pause in slept)


async def test_a_search_that_finds_nothing_says_what_it_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty answer, and the two numbers a stuck row is rendered from."""
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep([]))
    stub = NyaaStub()

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        found = await search_for_episode(client, FRIEREN_S1, 99, Rules())

    assert found.ranked == []
    assert (found.forms, found.results, found.kept) == (len(FRIEREN_S1_QUERIES), 0, 0)


async def test_concurrent_searches_do_not_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock is what keeps two queries from leaving together."""
    slept: list[float] = []
    monkeypatch.setattr(nyaa_module, "_sleep", no_sleep(slept))
    stub = NyaaStub()

    async with NyaaClient("https://nyaa.test", transport=stub.transport()) as client:
        await asyncio.gather(client.search("a"), client.search("b"), client.search("c"))

    assert len(stub.queries) == 3
    assert len(slept) == 2


# --- The shared client ------------------------------------------------------


def test_the_shared_client_is_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        nyaa_module.NyaaClient, "__init__", force_transport(NyaaClient, NyaaStub().transport())
    )

    assert shared_client("https://nyaa.test") is shared_client("https://nyaa.test")


async def test_two_concurrent_searches_are_two_seconds_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of sharing one client: the gap survives concurrency.

    Two ``search_release`` jobs running at once used to build a client each,
    and a pacing gap that is per instance is no gap at all — both requests
    left together. The clock is faked so the test asserts the two seconds
    without spending them.
    """
    clock = {"at": 1000.0}

    async def sleep(seconds: float) -> None:
        clock["at"] += seconds

    monkeypatch.setattr(nyaa_module, "_sleep", sleep)
    monkeypatch.setattr(nyaa_module, "_now", lambda: clock["at"])

    sent: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(clock["at"])
        return httpx.Response(200, text=EMPTY)

    monkeypatch.setattr(
        nyaa_module.NyaaClient,
        "__init__",
        force_transport(NyaaClient, httpx.MockTransport(handler)),
    )

    first = shared_client("https://nyaa.test")
    second = shared_client("https://nyaa.test")
    assert first is second, "both jobs reach for the same instance"

    await asyncio.gather(first.search("one episode"), second.search("another episode"))

    assert len(sent) == 2
    assert sent[1] - sent[0] >= MIN_INTERVAL
