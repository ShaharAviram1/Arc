"""The home page (FR-W1).

``GET /api/home`` — one call for the three rows the client renders: continue
watching, behind on, new this week. One call rather than three because they
are one screen and a home page that paints in three stages is worse than one
that paints once; the queries behind them are small and share the caller.

The episodes on this page are the *same shape* as the ones on a show page, and
are filled in the same way: one batched lookup each for the torrents, the
transcode jobs and the renditions of every episode on the page
(:mod:`arc.api.episode_extras`), plus one for which of them the caller has
finished, so a card can say "downloading, 62 %" or "preparing, 40 %" or why the
last attempt failed. Four queries for the whole page, not four per row — the
home page of a user following thirty shows is the one place an N+1 would
actually hurt.

Like the schedule, this endpoint reads the local cache and the caller's own
rows. Nothing here calls a catalogue source.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from arc.api.deps import CurrentUser, SessionDep
from arc.api.episode_extras import episode_extras
from arc.api.schedule_schemas import (
    BehindEntry,
    ContinueWatchingEntry,
    HomePage,
    NewEpisodeEntry,
)
from arc.services.catalog import list_status_for
from arc.services.catalog.progress import behind_for_user, new_this_week
from arc.services.playback.progress import completed_episode_ids, continue_watching

router = APIRouter(prefix="/api/home", tags=["home"])


def now() -> datetime:
    """The clock "aired", "behind by N" and "this week" are measured against.

    A function so a test can pin it: every number on this page is a comparison
    against the present, and asserting them needs a fixed one.
    """
    return datetime.now(UTC)


@router.get("", response_model=HomePage, summary="Continue watching, behind on, new this week")
async def home(user: CurrentUser, session: SessionDep) -> HomePage:
    at = now()
    behind = await behind_for_user(session, user, now=at)
    fresh = await new_this_week(session, user, now=at)
    started = await continue_watching(session, user_id=user.id)

    anime_ids = [row.anime.id for row in fresh] + [row.anime.id for row in started]
    episode_ids = [row.episode.id for row in fresh] + [row.episode.id for row in started]
    # One lookup for the list badges on the cards: those rows are all on the
    # caller's list by construction, but which state they are in is what the
    # badge says.
    statuses = await list_status_for(session, user_id=user.id, anime_ids=anime_ids)
    extras = await episode_extras(session, episode_ids)
    # Only "new this week" needs this: a continue-watching row is unfinished by
    # definition, and asking about it would be asking a question with a known
    # answer.
    watched = await completed_episode_ids(
        session, user_id=user.id, episode_ids=[row.episode.id for row in fresh]
    )

    return HomePage(
        continue_watching=[
            ContinueWatchingEntry.from_row(
                row,
                now=at,
                list_status=statuses.get(row.anime.id),
                torrent=extras.torrents.get(row.episode.id),
                rendition=extras.renditions.get(row.episode.id),
                transcode_job=extras.transcode_jobs.get(row.episode.id),
            )
            for row in started
        ],
        behind=[BehindEntry.from_row(row) for row in behind],
        new_this_week=[
            NewEpisodeEntry.from_row(
                row,
                now=at,
                list_status=statuses.get(row.anime.id),
                watched=row.episode.id in watched,
                torrent=extras.torrents.get(row.episode.id),
                rendition=extras.renditions.get(row.episode.id),
                transcode_job=extras.transcode_jobs.get(row.episode.id),
            )
            for row in fresh
        ],
    )


__all__ = ["now", "router"]
