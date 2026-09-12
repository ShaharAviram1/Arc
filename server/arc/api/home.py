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
rows. Nothing here calls a catalogue source — it *queues* two pieces of work
and waits for neither. The page's hero is the season's shows, and a season show
with no artwork at all is the "hero posters are still bad" the owner reported
on 2026-09-12 (:func:`~arc.services.tmdb.jobs.enqueue_hero_art`); the shelves
under it are 16:9 episode cards, and an episode with no still is a card framed
around the show's poster rather than the scene it is for
(:func:`~arc.services.tmdb.jobs.enqueue_episode_stills`).
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
from arc.services.tmdb.jobs import enqueue_episode_stills, enqueue_hero_art

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
    # Only "new this week" needs this: a continue-watching row carries its own
    # ``completed`` out of the query that found it (a rewatch left half-way is
    # on that shelf and is watched), so asking again would be a second query
    # for an answer already in hand.
    watched = await completed_episode_ids(
        session, user_id=user.id, episode_ids=[row.episode.id for row in fresh]
    )

    # Nothing on this response waits on either of these: the queue rows are
    # written here, a worker fetches the pictures, and the next visit to the
    # page finds them. Two SELECTs, each returning nothing once the art is in.
    #
    # The stills first, so that where both want the same show the full
    # enrichment is the job that stands: one dedupe key per show, and the first
    # caller wins. Every 16:9 card on this page whose episode has no still of
    # its own — Continue watching and Ready to watch — is a card framed around
    # the show's poster instead (``client/src/pages/Home.tsx``), which is
    # honest but is not the picture the card is for (owner, 2026-09-12).
    no_still = [row.anime.id for row in started if row.episode.still_url is None]
    no_still += [row.anime.id for row in fresh if row.episode.still_url is None]
    queued = await enqueue_episode_stills(session, no_still)
    # One SELECT that returns nothing once the season's art is in.
    queued += await enqueue_hero_art(session, now=at)
    if queued:
        await session.commit()

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
