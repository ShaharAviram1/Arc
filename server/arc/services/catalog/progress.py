"""What the home page says about a user (FR-C4, FR-W1).

Two questions, both asked of the local cache and the user's own rows and
neither of them of a catalogue source:

* **Behind on** — shows the user is watching that have aired episodes past
  their progress, with the count. This is FR-C4's "behind by N", and it is
  what the acquisition window of M6 will be computed from.
* **New this week** — episodes of followed shows that aired in the last seven
  days, newest first.

"Aired" means the same thing here as it does on the show page: the rule lives
in :mod:`arc.services.catalog.airing` and is read from there rather than
re-derived, because a home page that counts an episode as aired while the show
page renders it as upcoming is worse than either answer on its own.

Dropped and completed shows are absent from both by design. They generate no
wants (FR-W4), and "behind on a show I dropped" is not information.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, Episode, ListEntry, ListStatus, User
from arc.services.catalog.airing import aired_episodes, effective_air_at

#: How far back "new this week" looks (FR-W1).
NEW_WINDOW = timedelta(days=7)

#: And how many episodes it will name. A user following forty airing shows
#: would otherwise get a fortnight's broadcasting on one page; fifty is more
#: than a week of any real list and keeps the response small.
NEW_LIMIT = 50

#: The states "new this week" reports on. On-hold is deliberately absent:
#: nothing is being watched, and a paused show filling the list is noise. It
#: still counts as *followed* for the schedule's highlight (FR-C3).
NEW_STATUSES = (ListStatus.WATCHING, ListStatus.PLANNED)

#: Sort position of a "behind on" row whose episodes carry no air dates at
#: all. Older than any real air time, so those rows land at the bottom of a
#: newest-first list instead of raising on a ``None`` comparison.
UNDATED = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class BehindRow:
    """One show the user has unwatched aired episodes of (FR-C4)."""

    anime: Anime
    entry: ListEntry
    #: How many episodes of the show have aired.
    aired: int
    #: How many of those the user has not watched — ``behind by N``.
    behind: int
    #: When the most recent aired episode aired, when any of them has a date.
    #: Null for a finished show whose dates neither source keeps; those sort
    #: last rather than first.
    latest_aired_at: datetime | None


@dataclass(frozen=True, slots=True)
class NewEpisodeRow:
    """One episode that aired in the last week, on a show the user follows."""

    anime: Anime
    episode: Episode


async def _episodes_by_anime(
    session: AsyncSession, anime_ids: list[int]
) -> dict[int, list[Episode]]:
    """Every episode of these shows, grouped, in number order.

    One query for the whole list rather than one per show: a user watching
    thirty shows is a home page, not thirty round trips.
    """
    if not anime_ids:
        return {}
    rows = await session.scalars(
        select(Episode)
        .where(Episode.anime_id.in_(anime_ids))
        .order_by(Episode.anime_id, Episode.number)
        .execution_options(populate_existing=True)
    )
    grouped: dict[int, list[Episode]] = {}
    for episode in rows.all():
        grouped.setdefault(episode.anime_id, []).append(episode)
    return grouped


async def behind_for_user(session: AsyncSession, user: User, *, now: datetime) -> list[BehindRow]:
    """Shows in ``watching`` with aired episodes past the user's progress.

    Sorted by the most recent aired episode, newest first, so the show that
    aired last night is at the top of the page.
    """
    rows = await session.execute(
        select(Anime, ListEntry)
        .join(ListEntry, ListEntry.anime_id == Anime.id)
        .where(ListEntry.user_id == user.id, ListEntry.status == ListStatus.WATCHING)
        .order_by(Anime.id)
    )
    pairs = list(rows.all())
    episodes = await _episodes_by_anime(session, [anime.id for anime, _ in pairs])

    behind: list[BehindRow] = []
    for anime, entry in pairs:
        aired = aired_episodes(
            episodes.get(anime.id, []),
            now=now,
            anime_status=anime.status,
            next_airing=anime.next_airing,
        )
        unwatched = [episode for episode in aired if episode.number > entry.progress]
        if not unwatched:
            continue
        # Effective times, not stored ones: a source that dates episode 3 after
        # episode 7 would otherwise put "aired 27 Sep" — a date in the future —
        # at the top of a shelf sorted by exactly this value.
        times = effective_air_at(aired)
        latest = max(
            (at for at in (times.get(episode.number) for episode in aired) if at is not None),
            default=None,
        )
        behind.append(
            BehindRow(
                anime=anime,
                entry=entry,
                aired=len(aired),
                behind=len(unwatched),
                latest_aired_at=latest,
            )
        )

    # Newest first, and a show with no dates at all sorts last: it is a back
    # catalogue the user is working through, not something that aired tonight.
    behind.sort(key=lambda row: row.latest_aired_at or UNDATED, reverse=True)
    return behind


async def new_this_week(session: AsyncSession, user: User, *, now: datetime) -> list[NewEpisodeRow]:
    """Episodes of followed shows that aired in the last seven days (FR-W1).

    Answered in SQL rather than by the aired-boundary rule above, because the
    question itself needs a date: an episode with no ``air_at`` cannot be shown
    to have aired *this week* however certain we are that it has aired.
    """
    rows = await session.execute(
        select(Anime, Episode)
        .join(Episode, Episode.anime_id == Anime.id)
        .join(ListEntry, ListEntry.anime_id == Anime.id)
        .where(
            ListEntry.user_id == user.id,
            ListEntry.status.in_(NEW_STATUSES),
            Episode.air_at.isnot(None),
            Episode.air_at <= now,
            Episode.air_at >= now - NEW_WINDOW,
        )
        .order_by(Episode.air_at.desc(), Episode.id.desc())
        .limit(NEW_LIMIT)
    )
    return [NewEpisodeRow(anime=anime, episode=episode) for anime, episode in rows.all()]


__all__ = [
    "NEW_LIMIT",
    "NEW_STATUSES",
    "NEW_WINDOW",
    "BehindRow",
    "NewEpisodeRow",
    "behind_for_user",
    "new_this_week",
]
