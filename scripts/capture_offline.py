#!/usr/bin/env python
"""Refresh the offline-catalogue test fixtures from the live files.

    cd server && uv run python ../scripts/capture_offline.py

Writes ``server/tests/fixtures/offline/manami-slice.jsonl`` (the real header
line plus one line per selected anime) and ``server/tests/fixtures/offline/
fribb-slice.json`` (the matching entries of Fribb's id map). The parser tests
run against real records, not invented ones, because every interesting thing
about these two files is a shape somebody else chose: a missing
``animeSeason.year``, an entry with no MyAnimeList source at all, an
``imdb_id`` that is a list this week and a string the next.

Selection is by **MAL id** (:data:`MAL_IDS`) plus a few entries picked by exact
title (:data:`EXTRA_TITLES`), because the most useful edge case in the manami
file — a duplicate-ish entry that carries no MAL id — cannot be selected by one.
``--mal-id`` adds more from the command line.

The Fribb slice is the entries whose ``mal_id`` or ``anilist_id`` matches the
manami selection, and then whatever else is needed so the three shapes the
parser has to tolerate are all represented: a film (``themoviedb_id.movie``),
a list-valued ``imdb_id``, and an entry with no TMDB id at all.

Downloads about 14 MB. Both files are public and need no key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from arc.services.catalog.offline.parse import ids_from_sources  # noqa: E402

OUT = REPO / "server" / "tests" / "fixtures" / "offline"

MANAMI_URL = (
    "https://github.com/manami-project/anime-offline-database/releases/latest/download/"
    "anime-offline-database.jsonl.zst"
)
FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"

#: The shows the fixture covers, by MyAnimeList id. Chosen for coverage rather
#: than taste: the Mushoku Tensei family (five entries that a filename matcher
#: has to tell apart, including the season 3 the roadmap names), Frieren
#: (AniList 154587, the id the rest of the suite's fixtures use), three films,
#: one entry with no ``animeSeason.year`` at all, and a spread of long-running
#: and recent titles so the trigram column has something to search.
MAL_IDS: tuple[int, ...] = (
    39535,  # Mushoku Tensei
    45576,  # Mushoku Tensei Part 2
    51179,  # Mushoku Tensei II
    55888,  # Mushoku Tensei II Part 2
    59193,  # Mushoku Tensei III — roadmap M15.5's worked example
    52991,  # Sousou no Frieren (AniList 154587)
    32281,  # Kimi no Na wa. — MOVIE
    199,  # Sen to Chihiro no Kamikakushi — MOVIE
    28851,  # Koe no Katachi — MOVIE
    63794,  # [Oshi no Ko] 4th Season — UPCOMING, no season year
    37521,  # Vinland Saga
    49387,  # Vinland Saga Season 2
    16498,  # Shingeki no Kyojin
    21,  # One Piece — the longest synonym list in the file
    1535,  # Death Note
    5114,  # Fullmetal Alchemist: Brotherhood
    30276,  # One Punch Man
    38000,  # Kimetsu no Yaiba
    40748,  # Jujutsu Kaisen
    51009,  # Jujutsu Kaisen 2nd Season
    44511,  # Chainsaw Man
    11061,  # Hunter x Hunter (2011)
    9253,  # Steins;Gate
    20,  # Naruto
    31964,  # Boku no Hero Academia
    918,  # Gintama
    47778,  # Kimetsu no Yaiba: Yuukaku-hen
    41467,  # Bleach: Sennen Kessen-hen
)

#: Entries picked by exact title because they have no MAL id to pick them by.
#: manami keeps several such records — a title known to Anime-Planet and
#: anisearch but not (yet) to MyAnimeList — and they are exactly the case the
#: id parser has to leave null rather than guess at.
EXTRA_TITLES: tuple[str, ...] = ("Mushoku Tensei: Jobless Reincarnation Season 3",)


def anime_ids(entry: dict[str, Any]) -> dict[str, int | None]:
    return ids_from_sources(entry.get("sources"))


async def fetch(client: httpx.AsyncClient, url: str) -> tuple[bytes, httpx.Headers]:
    response = await client.get(url)
    response.raise_for_status()
    print(f"fetched {url} ({len(response.content)} bytes)")
    return response.content, response.headers


def select_manami(raw: bytes, wanted: set[int], titles: set[str]) -> tuple[str, list[str]]:
    """``(header line, selected lines)`` from the decompressed JSONL."""
    from compression import zstd

    text = zstd.decompress(raw).decode("utf-8")
    lines = text.splitlines()
    header, body = lines[0], lines[1:]

    chosen: dict[int, str] = {}
    extra: list[str] = []
    seen_titles: set[str] = set()
    for line in body:
        entry = json.loads(line)
        mal_id = anime_ids(entry)["mal_id"]
        if mal_id in wanted and mal_id not in chosen:
            chosen[mal_id] = line
        elif entry.get("title") in titles and entry.get("title") not in seen_titles:
            seen_titles.add(str(entry.get("title")))
            extra.append(line)

    missing = sorted(wanted - set(chosen))
    if missing:
        print(f"! not in this release, skipped: {missing}", file=sys.stderr)
    for title in sorted(titles - seen_titles):
        print(f"! title not found, skipped: {title!r}", file=sys.stderr)

    ordered = [chosen[mal_id] for mal_id in MAL_IDS if mal_id in chosen]
    return header, ordered + extra


def select_fribb(
    payload: list[dict[str, Any]], mal_ids: set[int], anilist_ids: set[int]
) -> list[dict[str, Any]]:
    """The matching id-map entries, plus one of each shape the parser tolerates."""
    picked = [
        entry
        for entry in payload
        if entry.get("mal_id") in mal_ids or entry.get("anilist_id") in anilist_ids
    ]

    def covered(predicate: Any) -> bool:
        return any(predicate(entry) for entry in picked)

    wants = {
        "a film (themoviedb_id.movie)": lambda e: (
            isinstance(e.get("themoviedb_id"), dict) and "movie" in e["themoviedb_id"]
        ),
        "a list-valued imdb_id": lambda e: isinstance(e.get("imdb_id"), list),
        "no TMDB id at all": lambda e: e.get("themoviedb_id") in (None, {}),
    }
    for description, predicate in wants.items():
        if covered(predicate):
            continue
        found = next((entry for entry in payload if predicate(entry)), None)
        if found is None:
            print(f"! nothing in the file has {description}", file=sys.stderr)
            continue
        print(f"+ added one entry for coverage: {description}")
        picked.append(found)
    return picked


def write(name: str, text: str) -> None:
    path = OUT / name
    path.write_text(text)
    print(f"wrote {path.relative_to(REPO)} ({path.stat().st_size} bytes)")


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mal-id",
        type=int,
        action="append",
        default=[],
        metavar="ID",
        help="an extra MyAnimeList id to include; repeat for several",
    )
    args = parser.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    wanted = set(MAL_IDS) | set(args.mal_id)

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        manami_raw, _ = await fetch(client, MANAMI_URL)
        fribb_raw, fribb_headers = await fetch(client, FRIBB_URL)

    header, lines = select_manami(manami_raw, wanted, set(EXTRA_TITLES))
    write("manami-slice.jsonl", "\n".join([header, *lines]) + "\n")

    anilist_ids = {
        anime_ids(json.loads(line))["anilist_id"]
        for line in lines
        if anime_ids(json.loads(line))["anilist_id"] is not None
    }
    entries = select_fribb(json.loads(fribb_raw), wanted, {i for i in anilist_ids if i})
    write("fribb-slice.json", json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
    print(f"fribb etag: {fribb_headers.get('etag')}")
    print(f"{len(lines)} manami lines, {len(entries)} fribb entries")


if __name__ == "__main__":
    asyncio.run(main())
