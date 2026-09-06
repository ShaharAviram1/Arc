#!/usr/bin/env python
"""Refresh the MyAnimeList test fixtures from the live API.

    cd server && MAL_CLIENT_ID=... uv run python ../scripts/capture_mal.py

Writes ``server/tests/fixtures/mal/{search_frieren,anime_52991,
season_2023_fall}.json`` using the same field sets the application asks for
(:mod:`arc.services.mal.catalog`), so a change to those is one command away
from matching fixtures.

Two edits are made on the way out, both so the fixtures describe exactly one
page: the lists are trimmed to a handful of entries, and ``paging`` is emptied.
``tests/mal_mock.py`` sets ``paging`` itself when a test wants more.

Reads only. This script cannot write to anybody's list — MAL's read endpoints
take a client id and nothing else, which is the whole reason the catalogue
fallback needs no user to authorise it (FR-C6).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from arc.services.mal.catalog import DETAIL_FIELDS, SUMMARY_FIELDS  # noqa: E402

OUT = REPO / "server" / "tests" / "fixtures" / "mal"
URL = "https://api.myanimelist.net/v2"
FRIEREN_MAL_ID = 52991
SEASON = (2023, "fall")
#: How many entries of a list response to keep. Enough to prove the parsing and
#: the ordering; not so many that a fixture is a data dump.
KEEP = 5
PAUSE = 1.0


async def get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
    response = await client.get(f"{URL}{path}", params=params)
    response.raise_for_status()
    return response.json()


def write(name: str, payload: Any) -> None:
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path.relative_to(REPO)} ({path.stat().st_size} bytes)")


def trim(payload: dict[str, Any], *, first: int | None = None) -> dict[str, Any]:
    """Keep ``KEEP`` entries — ``first`` pinned to the front if given."""
    entries = payload.get("data") or []
    if first is not None:
        head = [entry for entry in entries if entry["node"]["id"] == first]
        rest = [entry for entry in entries if entry["node"]["id"] != first]
        entries = head + rest
    payload["data"] = entries[:KEEP]
    payload["paging"] = {}
    return payload


async def main() -> None:
    client_id = os.environ.get("MAL_CLIENT_ID")
    if not client_id:
        raise SystemExit("MAL_CLIENT_ID is not set; register one at myanimelist.net/apiconfig")

    OUT.mkdir(parents=True, exist_ok=True)
    headers = {
        "Accept": "application/json",
        "X-MAL-CLIENT-ID": client_id,
        "User-Agent": "arc/0.1 (fixture capture)",
    }
    year, season = SEASON
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        found = await get(client, "/anime", {"q": "frieren", "limit": 20, "fields": SUMMARY_FIELDS})
        write("search_frieren", trim(found, first=FRIEREN_MAL_ID))
        await asyncio.sleep(PAUSE)

        write(
            f"anime_{FRIEREN_MAL_ID}",
            await get(client, f"/anime/{FRIEREN_MAL_ID}", {"fields": DETAIL_FIELDS}),
        )
        await asyncio.sleep(PAUSE)

        listing = await get(
            client, f"/anime/season/{year}/{season}", {"limit": 100, "fields": SUMMARY_FIELDS}
        )
        write(f"season_{year}_{season}", trim(listing, first=FRIEREN_MAL_ID))


if __name__ == "__main__":
    asyncio.run(main())
