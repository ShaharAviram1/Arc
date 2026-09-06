#!/usr/bin/env python
"""Capture the AniList catalogue the matcher acceptance test runs against.

    cd server && uv run python ../scripts/capture_match_catalogue.py

Writes ``server/tests/fixtures/anilist/match_catalogue.json``: one entry per
title, holding only what :mod:`arc.services.library.matcher` scores against —
ids, the three titles, synonyms, format, episode count, airing status, season
and year, and the relation edges the absolute-numbering rule follows. No
synopsis, no tags, no cover: the file is a *catalogue*, not a cache dump, and
keeping it to the scored fields is what keeps it reviewable.

Two passes, because AniList answers them with two different queries and the
second is far cheaper per title:

1. one search per name in :data:`SEEDS`, which is also how the fixture ends up
   with the near-misses a real search returns — the sequels, the side stories
   and the same franchise under three names, which are exactly the candidates
   the matcher has to *not* pick;
2. one batched detail fetch per 50 collected ids, for the synonyms and
   relations a search result does not carry.

The batched document lives here rather than in
:mod:`arc.services.anilist.queries` on purpose: Arc itself never fetches fifty
titles by id at once, and a query the application does not send does not belong
in the module that documents what it sends.

Be gentle — unauthenticated requests against a service that enforces about
30/min, so it sleeps between them.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from arc.services.anilist.queries import SEARCH  # noqa: E402

OUT = REPO / "server" / "tests" / "fixtures" / "anilist" / "match_catalogue.json"
URL = "https://graphql.anilist.co"
PAUSE = 2.2
BATCH = 50

#: The titles the acceptance corpus draws on. Deliberately full of traps:
#: numbered titles (86, Steins;Gate 0, Mob Psycho 100), long franchises with
#: many seasons under slightly different names, absolute-numbered shounen, and
#: several pairs whose romaji and English differ enough to matter.
SEEDS: tuple[str, ...] = (
    "Sousou no Frieren",
    "Mushishi",
    "Mob Psycho 100",
    "Steins;Gate",
    "Steins;Gate 0",
    "86 Eighty Six",
    "Re:Zero kara Hajimeru Isekai Seikatsu",
    "Kaguya-sama wa Kokurasetai",
    "Boku no Hero Academia",
    "One Piece",
    "Vinland Saga",
    "Jujutsu Kaisen",
    "Spy x Family",
    "Shingeki no Kyojin",
    "Kimetsu no Yaiba",
    "Bocchi the Rock!",
    "Cowboy Bebop",
    "Dandadan",
    "Kusuriya no Hitorigoto",
    "Overlord",
    "Sword Art Online",
    "Hunter x Hunter",
    "Chihayafuru",
    "Nichijou",
    "Toradora!",
    "Clannad After Story",
    "Fullmetal Alchemist: Brotherhood",
    "Death Note",
    "Gurren Lagann",
    "Solo Leveling",
    "Kaiju No. 8",
    "Bakemonogatari",
    "Monogatari Series: Second Season",
    "Sound! Euphonium",
    "Ping Pong the Animation",
    "Violet Evergarden",
    "Made in Abyss",
    "Non Non Biyori",
    "Yuru Camp",
    "Chainsaw Man",
    "Cyberpunk: Edgerunners",
    "Serial Experiments Lain",
    "Neon Genesis Evangelion",
    "Monster",
    "Ergo Proxy",
    "Texhnolyze",
    "Haibane Renmei",
    "Kino no Tabi",
    "Planetes",
    "Hyouka",
    "Dungeon Meshi",
    "Shangri-La Frontier",
    "Oshi no Ko",
    "Tensei shitara Slime Datta Ken",
    "Dr. Stone",
    "One Punch Man",
    "Psycho-Pass",
    "Fate/Zero",
    "Code Geass",
    "Naruto Shippuuden",
    "Bleach",
    "Detective Conan",
    "Gintama",
    "Fairy Tail",
    "Mushoku Tensei",
    "Sono Bisque Doll wa Koi wo Suru",
    "Delicious in Dungeon",
    "Ranma 1/2",
    "Sakamoto Days",
    "Ao no Exorcist",
    "Higurashi no Naku Koro ni",
    "Shokugeki no Souma",
    "Kanojo, Okarishimasu",
    "Tate no Yuusha no Nariagari",
    "Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e",
    "Seishun Buta Yarou wa Bunny Girl Senpai no Yume wo Minai",
    "Nanatsu no Taizai",
    "Isekai Ojisan",
    "Mahoutsukai no Yome",
    "Yahari Ore no Seishun Love Comedy wa Machigatteiru.",
    "Kono Subarashii Sekai ni Shukufuku wo!",
    "Ijiranaide, Nagatoro-san",
    "3-gatsu no Lion",
    "Ghost in the Shell: SAC_2045",
    "K-On!",
    "xxxHOLiC",
    "Hibike! Euphonium",
    "Fate/stay night: Unlimited Blade Works",
    "Yofukashi no Uta",
    "Blue Lock",
    "Ore dake Level Up na Ken",
    "Shouwa Genroku Rakugo Shinjuu",
    "Tearmoon Teikoku Monogatari",
    "Sengoku Youko",
)

#: The scored fields, in bulk. Not in ``arc.services.anilist.queries`` because
#: the application has no bulk-by-id query and never will.
CATALOGUE_BY_IDS = """
query ArcMatchCatalogue($ids: [Int], $page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { currentPage hasNextPage }
    media(id_in: $ids, type: ANIME, sort: ID) {
      id
      idMal
      title { romaji english native }
      synonyms
      format
      episodes
      status
      season
      seasonYear
      coverImage { extraLarge large }
      relations {
        edges {
          relationType
          node { id idMal type format title { romaji english native } }
        }
      }
    }
  }
}
"""


#: How many times one request is retried, and how long it waits first. A
#: ninety-request run against a rate-limited public API times out or gets a
#: 429 sooner or later, and losing four minutes of capture to one blip is not
#: worth the two lines this costs.
RETRIES = 5
RETRY_PAUSE = 8.0


async def post(client: httpx.AsyncClient, document: str, variables: dict[str, Any]) -> Any:
    for attempt in range(1, RETRIES + 1):
        try:
            response = await client.post(URL, json={"query": document, "variables": variables})
            if response.status_code in {429, 500, 502, 503, 504}:
                raise httpx.HTTPError(f"http {response.status_code}")
            response.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            if attempt == RETRIES:
                raise
            wait = RETRY_PAUSE * attempt
            print(f"  retry {attempt}/{RETRIES - 1} after {exc!r}; sleeping {wait:.0f}s")
            await asyncio.sleep(wait)
            continue
        body = response.json()
        if body.get("errors"):
            raise RuntimeError(f"anilist: {body['errors']}")
        return body
    raise RuntimeError("unreachable")


async def collect_ids(client: httpx.AsyncClient) -> list[int]:
    """Search every seed and keep every id any of them returned."""
    found: dict[int, None] = {}
    for index, term in enumerate(SEEDS, 1):
        body = await post(client, SEARCH, {"search": term, "page": 1, "perPage": 8})
        media = body["data"]["Page"]["media"]
        for node in media:
            found.setdefault(int(node["id"]), None)
        print(f"[{index:3}/{len(SEEDS)}] {term}: {len(media)} hits, {len(found)} ids so far")
        await asyncio.sleep(PAUSE)
    return sorted(found)


async def fetch_details(client: httpx.AsyncClient, ids: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for start in range(0, len(ids), BATCH):
        chunk = ids[start : start + BATCH]
        body = await post(client, CATALOGUE_BY_IDS, {"ids": chunk, "page": 1, "perPage": BATCH})
        rows.extend(body["data"]["Page"]["media"])
        print(f"details {start + len(chunk)}/{len(ids)}")
        await asyncio.sleep(PAUSE)
    return rows


#: Relation types worth keeping. Only ``SEQUEL`` is read — it is what the
#: absolute-numbering rule follows — and ``PREQUEL`` comes along so a chain is
#: legible to a human reading the fixture. Dropping the other seven types
#: (CHARACTER, SUMMARY, SPIN_OFF, …) halves the file and removes nothing any
#: test looks at.
KEPT_RELATIONS = frozenset({"SEQUEL", "PREQUEL"})


def trim(node: dict[str, Any]) -> dict[str, Any]:
    """Keep the scored fields and drop everything else."""
    edges = ((node.get("relations") or {}).get("edges")) or []
    return {
        "id": node["id"],
        "idMal": node.get("idMal"),
        "title": node.get("title") or {},
        "synonyms": node.get("synonyms") or [],
        "format": node.get("format"),
        "episodes": node.get("episodes"),
        "status": node.get("status"),
        "season": node.get("season"),
        "seasonYear": node.get("seasonYear"),
        "coverImage": node.get("coverImage") or {"extraLarge": None, "large": None},
        "relations": {
            "edges": [
                {
                    "relationType": edge.get("relationType"),
                    "node": {
                        "id": (edge.get("node") or {}).get("id"),
                        "idMal": (edge.get("node") or {}).get("idMal"),
                        "type": (edge.get("node") or {}).get("type"),
                        "format": (edge.get("node") or {}).get("format"),
                        "title": (edge.get("node") or {}).get("title") or {},
                    },
                }
                for edge in edges
                if (edge.get("node") or {}).get("type") == "ANIME"
                and str(edge.get("relationType") or "").upper() in KEPT_RELATIONS
            ]
        },
    }


async def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "application/json", "User-Agent": "arc/0.1 (fixture capture)"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        ids = await collect_ids(client)
        rows = await fetch_details(client, ids)
    trimmed = sorted((trim(row) for row in rows), key=lambda row: int(row["id"]))
    OUT.write_text(json.dumps({"media": trimmed}, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(REPO)} ({len(trimmed)} titles, {OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
