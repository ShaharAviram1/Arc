#!/usr/bin/env python
"""Refresh the AniList test fixtures from the live API.

    cd server && uv run python ../scripts/capture_anilist.py

Writes ``server/tests/fixtures/anilist/{search_frieren,media_154587}.json``
using the same GraphQL documents the application sends, so a change to a query
is one command away from a matching fixture. It deliberately does not touch
``media_999001_releasing.json``: that one is synthetic, anchored to the frozen
clock the tests use, and has no upstream to capture.

Be gentle — this is a handful of unauthenticated requests against a service
with a 30/min limit, so it sleeps between them.
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

from arc.services.anilist.queries import MEDIA_BY_ID, SEARCH  # noqa: E402

OUT = REPO / "server" / "tests" / "fixtures" / "anilist"
URL = "https://graphql.anilist.co"
FRIEREN_ID = 154587
PAUSE = 2.0


async def post(client: httpx.AsyncClient, document: str, variables: dict[str, Any]) -> Any:
    response = await client.post(URL, json={"query": document, "variables": variables})
    response.raise_for_status()
    return response.json()


def write(name: str, payload: Any) -> None:
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path.relative_to(REPO)} ({path.stat().st_size} bytes)")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "application/json", "User-Agent": "arc/0.1 (fixture capture)"}
    search_vars = {"search": "frieren", "page": 1, "perPage": 20}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        write("search_frieren", await post(client, SEARCH, search_vars))
        await asyncio.sleep(PAUSE)
        write("media_154587", await post(client, MEDIA_BY_ID, {"id": FRIEREN_ID}))


if __name__ == "__main__":
    asyncio.run(main())
