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

A published date is not a statement either, when the rest of the list
contradicts it. Two sanity rules therefore sit over the stored times, because
both sources publish dates that cannot be true (architecture.md §5, cache rule
3: estimated vs published):

1. **A ``FINISHED`` show has no future episodes.** The status is the source's
   own summary of the whole run; an episode of it dated next month is a typo,
   not a broadcast, and "will air 27 Sep" between two episodes that aired in
   August is the bug this rule exists for.
2. **Air times do not go backwards.** An episode dated *after* a
   higher-numbered one is non-monotonic, and the later sibling is the better
   evidence: the episode is treated as having aired by the earlier of the two
   dates and is flagged estimated, so the client badges it "est.".

Neither rule invents a date. The stored ``air_at`` is left exactly as the
source published it — only the derived aired-ness and the estimated flag
change — so the next refresh can still correct it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
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


def effective_air_at(episodes: Iterable[Episode]) -> dict[int, datetime]:
    """Stored air times with the non-monotonic ones pulled back into order.

    Keyed by episode number, and only for episodes that have a stored time at
    all. An episode's effective time is the earliest time anything from it
    onwards carries, so a date later than a higher-numbered episode's gives way
    to that sibling and every other row is returned untouched.

    Nothing is written: this is the reading of the stored column, not a
    replacement for it. The row keeps whatever the source published, which is
    what lets a later refresh fix the source's mistake rather than Arc's.
    """
    times: dict[int, datetime] = {}
    floor: datetime | None = None
    for episode in sorted(episodes, key=lambda row: row.number, reverse=True):
        at = episode.air_at
        if at is None:
            continue
        floor = at if floor is None else min(at, floor)
        times[episode.number] = floor
    return times


def out_of_order(episodes: Sequence[Episode]) -> frozenset[int]:
    """Episode numbers dated after a higher-numbered episode of the same show.

    These are the rows the client badges "est.": Arc is showing the date the
    source published while saying, in the same breath, that the list does not
    support it.
    """
    effective = effective_air_at(episodes)
    return frozenset(
        episode.number
        for episode in episodes
        if episode.air_at is not None and effective.get(episode.number) != episode.air_at
    )


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

    The list is asked before the episode's own date, which is what makes the
    two sanity rules in the module docstring one line each. A finished show's
    episodes have all aired, whatever date any single row carries. Anything the
    boundary already covers has aired too — the boundary is the highest
    *dated* episode in the past, so a row below it with a future date is
    exactly the non-monotonic case, and taking the sibling's date is what rule
    2 asks for. Only then does a published time speak for itself.
    """
    if anime_status == FINISHED or episode.number <= boundary:
        return True
    if episode.air_at is not None:
        return episode.air_at <= now
    return False


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
    "effective_air_at",
    "is_aired",
    "next_airing_at",
    "next_airing_episode",
    "next_airing_estimated",
    "out_of_order",
]
