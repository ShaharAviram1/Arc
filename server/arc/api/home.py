"""The home page (FR-W1).

``GET /api/home`` — one call for the three rows the client renders: continue
watching, behind on, new this week. One call rather than three because they
are one screen and a home page that paints in three stages is worse than one
that paints once; the queries behind them are small and share the caller.

``continue_watching`` is present and empty until M8: ``watch_progress`` is
only written by the player, and the field is in the contract from the start so
that its arrival is a change to the client's rendering rather than to its
types.

Like the schedule, this endpoint reads the local cache and the caller's own
rows. Nothing here calls a catalogue source.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from arc.api.deps import CurrentUser, SessionDep
from arc.api.schedule_schemas import BehindEntry, HomePage, NewEpisodeEntry
from arc.services.catalog import list_status_for
from arc.services.catalog.progress import behind_for_user, new_this_week

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

    # One lookup for the list badges on the "new this week" cards: those rows
    # are all on the caller's list by construction, but which state they are in
    # is what the badge says.
    statuses = await list_status_for(
        session, user_id=user.id, anime_ids=[row.anime.id for row in fresh]
    )
    return HomePage(
        continue_watching=[],
        behind=[BehindEntry.from_row(row) for row in behind],
        new_this_week=[
            NewEpisodeEntry.from_row(row, now=at, list_status=statuses.get(row.anime.id))
            for row in fresh
        ],
    )


__all__ = ["now", "router"]
