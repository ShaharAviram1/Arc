#!/usr/bin/env python
"""Refresh the TMDB test fixtures from the live API (M15.5, FR-C6).

    cd server && uv run python ../scripts/capture_tmdb.py

Reads ``TMDB_API_KEY`` from the environment or from the repository ``.env``
(the key itself is never printed and never written into a fixture: TMDB's v3
key travels as a query parameter, and only the *responses* are saved).

Writes ``server/tests/fixtures/tmdb/`` — one file per endpoint the enrichment
job calls, for Frieren (``tmdb_tv_id`` 209867, season 1) plus one film, so the
movie branch of :mod:`arc.services.tmdb.client` has a recorded response too::

    tv_209867.json                     GET /tv/209867
    tv_209867_season_1.json            GET /tv/209867/season/1
    tv_209867_aggregate_credits.json   GET /tv/209867/aggregate_credits
    movie_378064.json                  GET /movie/378064
    movie_378064_credits.json          GET /movie/378064/credits

Two edits are made on the way out, both so a fixture stays a fixture rather
than a data dump: the season's episode list is trimmed to :data:`KEEP_EPISODES`
entries, and the crew is trimmed to the members whose job Arc maps
(:data:`arc.services.tmdb.enrich.CREW_JOBS`) plus :data:`KEEP_NOISE` it does
not — the ones the tests assert are dropped. ``cast`` is emptied: Arc renders
no cast, and it is two thirds of the payload.

Reads only. There is no TMDB write path in Arc at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from arc.services.tmdb.client import TMDB_API_URL  # noqa: E402
from arc.services.tmdb.enrich import CREW_JOBS  # noqa: E402

OUT = REPO / "server" / "tests" / "fixtures" / "tmdb"

#: Frieren, as the id map has it: ``mal_id`` 52991 → ``tmdb_tv_id`` 209867,
#: ``tmdb_season`` 1.
FRIEREN_TV_ID = 209867
FRIEREN_SEASON = 1

#: "A Silent Voice" (Koe no Katachi). A film, so it exercises the branch where
#: there is no season and the credits come back with a flat ``job`` per crew
#: member rather than a ``jobs`` list.
FILM_ID = 378064

#: How many episodes of the season to keep. Enough to prove the numbering and
#: the still URLs; not so many that the fixture is the whole show.
KEEP_EPISODES = 5

#: Per-episode fields nobody reads, dropped so the season fixture stays a
#: fixture: an episode's own crew and guest cast are most of its bytes.
EPISODE_DROP = frozenset({"crew", "guest_stars"})

#: How many *unmapped* crew members to keep, so a test can assert that a job
#: Arc has no row for is dropped rather than guessed at.
KEEP_NOISE = 6

#: Seconds between requests. Far more polite than TMDB asks for; this runs by
#: hand, a handful of times a year.
PAUSE = 0.5


def api_key() -> str:
    """The key, from the environment or from the repository ``.env``."""
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if key:
        return key
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("TMDB_API_KEY="):
                key = line.split("=", 1)[1].strip()
                if key:
                    return key
    raise SystemExit("TMDB_API_KEY is not set (environment or .env)")


async def get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(f"{TMDB_API_URL}{path}", params=params)
    response.raise_for_status()
    time.sleep(PAUSE)
    return response.json()


def write(name: str, payload: Any) -> None:
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path.relative_to(REPO)} ({path.stat().st_size} bytes)")


def _jobs_of(member: dict[str, Any]) -> list[str]:
    """Every job string on a crew member, in both shapes TMDB uses."""
    jobs = [str(entry.get("job")) for entry in member.get("jobs") or [] if entry.get("job")]
    if member.get("job"):
        jobs.append(str(member["job"]))
    return jobs


def trim_credits(payload: dict[str, Any]) -> dict[str, Any]:
    """Mapped crew, a little noise, and no cast at all."""
    crew = payload.get("crew") or []
    mapped = [member for member in crew if any(job in CREW_JOBS for job in _jobs_of(member))]
    noise = [member for member in crew if member not in mapped][:KEEP_NOISE]
    return {**payload, "cast": [], "crew": mapped + noise}


def trim_season(payload: dict[str, Any]) -> dict[str, Any]:
    """The first few episodes, without their own cast and crew.

    Every episode carries a ``crew`` and a ``guest_stars`` array — ninety
    kilobytes for five episodes, none of which Arc reads: the credits come from
    the series-level aggregate, not from an episode.
    """
    episodes = [
        {key: value for key, value in episode.items() if key not in EPISODE_DROP}
        for episode in (payload.get("episodes") or [])[:KEEP_EPISODES]
    ]
    return {**payload, "episodes": episodes}


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    key = api_key()
    async with httpx.AsyncClient(timeout=30.0, params={"api_key": key}) as client:
        show = await get(client, f"/tv/{FRIEREN_TV_ID}", language="en-US")
        write(f"tv_{FRIEREN_TV_ID}", show)

        season = await get(client, f"/tv/{FRIEREN_TV_ID}/season/{FRIEREN_SEASON}", language="en-US")
        write(f"tv_{FRIEREN_TV_ID}_season_{FRIEREN_SEASON}", trim_season(season))

        credits = await get(client, f"/tv/{FRIEREN_TV_ID}/aggregate_credits", language="en-US")
        write(f"tv_{FRIEREN_TV_ID}_aggregate_credits", trim_credits(credits))

        film = await get(client, f"/movie/{FILM_ID}", language="en-US")
        write(f"movie_{FILM_ID}", film)

        film_credits = await get(client, f"/movie/{FILM_ID}/credits", language="en-US")
        write(f"movie_{FILM_ID}_credits", trim_credits(film_credits))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
