"""Which episodes have aired, read off the cache alone.

Three callers need the same answer and must not disagree about it: the show
page's per-episode ``aired`` flag, the home page's "behind by N" (FR-C4), and
the schedule's weekday placement. So the rule lives here, as pure functions
over rows and a clock, and every one of them reads it from this module.

The rule itself is the interesting part. A null ``air_at`` is *missing data*,
not a statement about the future: AniList only keeps ``airingSchedule`` for
reasonably recent seasons, MAL synthesises nothing for a show with a partial
start date, and a fetch can simply have failed. Reading each null as "not yet"
would mark episode 300 of a running show unaired while 301 airs on Sunday, and
would tell a user they are behind by nothing on a show they have not started.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from arc.models import Episode

#: A show that has finished airing but has no schedule rows still has aired
#: episodes; this is the status that says so.
FINISHED = "FINISHED"

#: A show that is still airing. Its ``nextAiringEpisode`` is the sharpest
#: statement either source makes about where the aired/unaired line is.
RELEASING = "RELEASING"


def _fields(blob: dict[str, Any] | None) -> dict[str, Any]:
    """The blob as a mapping, or an empty one.

    ``anime.next_airing`` is JSONB: the column's type says "some JSON value",
    not "an object", and a source that one day answers with a list or a bare
    string would otherwise take every reader of it down with an
    ``AttributeError``. A shape nobody recognises is treated as no slot at all,
    which is the same answer a null gives.
    """
    return blob if isinstance(blob, dict) else {}


def next_airing_episode(blob: dict[str, Any] | None) -> int | None:
    """The episode number inside a stored ``next_airing`` blob, if it has one.

    MAL publishes no per-episode schedule, so a slot synthesised from its
    broadcast time knows *when* the next episode airs and not *which* one; the
    number is null there and the client renders the time without it (FR-C6).
    """
    episode = _fields(blob).get("episode")
    return int(episode) if isinstance(episode, int) else None


def next_airing_at(blob: dict[str, Any] | None) -> datetime | None:
    """When the next episode airs, from a stored ``next_airing`` blob.

    ``airingAt`` is epoch seconds in both sources' blobs — AniList's own field
    and the one :mod:`arc.services.mal.catalog` synthesises to match it.
    """
    at = _fields(blob).get("airingAt")
    if not isinstance(at, int | float):
        return None
    return datetime.fromtimestamp(int(at), UTC)


def next_airing_estimated(blob: dict[str, Any] | None) -> bool:
    """Whether the slot is Arc's own arithmetic rather than a published time.

    AniList publishes ``nextAiringEpisode`` and its blob carries no flag; the
    one :mod:`arc.services.mal.catalog` synthesises from a broadcast time sets
    ``estimated: true`` (FR-C6). So a missing flag means "published", which is
    what makes an AniList blob outrank a MAL one in the cache and what the
    schedule badges.
    """
    return bool(_fields(blob).get("estimated"))


def aired_through(
    episodes: list[Episode],
    *,
    now: datetime,
    anime_status: str | None,
    next_airing: dict[str, Any] | None,
) -> int:
    """The highest episode number known to have aired, from any evidence.

    The boundary is drawn once for the whole list, from the two things that
    *are* statements: the highest episode with a past air time, and (while the
    show is releasing) the episode before the one the source says is next.
    Everything at or below it counts as aired even with no date of its own.

    Note the asymmetry with :data:`arc.services.catalog.schedule.STALE_NEXT_AIRING`:
    the schedule drops a ``next_airing`` nobody has refreshed for a week, and
    this function checks its age at all. Both are right, because the two
    callers ask the blob different questions. The schedule reads it as a
    position in the *future*, where a stale blob is an active lie ("episode 6
    is next" three weeks after episode 6 aired). Here it only ever feeds a
    ``max``, so a stale blob can fail to raise the boundary but can never lower
    it — and the episodes it would have covered have real air times by then
    anyway. Ageing it out would cost the one case it exists for: a running show
    whose back catalogue has no dates at all.
    """
    highest = max(
        (
            episode.number
            for episode in episodes
            if episode.air_at is not None and episode.air_at <= now
        ),
        default=0,
    )
    if anime_status == RELEASING:
        upcoming = next_airing_episode(next_airing)
        if upcoming is not None:
            highest = max(highest, upcoming - 1)
    return highest


def is_aired(
    episode: Episode,
    *,
    now: datetime,
    anime_status: str | None,
    boundary: int = 0,
) -> bool:
    """Whether one episode has aired, given the list's :func:`aired_through`.

    A published time decides it on its own. Without one, a finished show's
    episodes have all aired — AniList simply does not keep dates that far
    back — and anything else is aired if the rest of the list places it behind
    the boundary.
    """
    if episode.air_at is not None:
        return episode.air_at <= now
    return anime_status == FINISHED or episode.number <= boundary


def aired_episodes(
    episodes: list[Episode],
    *,
    now: datetime,
    anime_status: str | None,
    next_airing: dict[str, Any] | None,
) -> list[Episode]:
    """The subset of ``episodes`` that has aired, in the order given."""
    boundary = aired_through(episodes, now=now, anime_status=anime_status, next_airing=next_airing)
    return [
        episode
        for episode in episodes
        if is_aired(episode, now=now, anime_status=anime_status, boundary=boundary)
    ]


__all__ = [
    "FINISHED",
    "RELEASING",
    "aired_episodes",
    "aired_through",
    "is_aired",
    "next_airing_at",
    "next_airing_episode",
    "next_airing_estimated",
]
