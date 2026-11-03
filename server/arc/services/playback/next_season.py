"""The "Next season" offer at the end of a finished season (FR-S5 item 3, 2026-10-18).

The player's ordinary Next knows only the same show's rows. At the last
episode of a finished season it now offers the sequel's first episode, when
the sequel is a show Arc has (:mod:`arc.services.catalog.sequels`).

Pressing it is a touch (FR-A9): the client first makes the sequel's entry
active — adds it as Watching when it is absent, re-sends it when it is dormant
— through the ordinary list write, and then demands the episode (FR-A14). So
the offer carries the viewer's **list state** for the sequel. Its
``fetchable`` is what the demand will accept (M21): aired, not ready, and
nothing on the page would refuse the press (the cap, a pause, a hold —
:class:`~arc.services.acquisition.demand_gate.DemandGate`), whatever the
entry; M20 judged it on the window the press would give the entry, and that
coverage helper is gone. Nothing here fetches, wants or writes anything.

``dormant`` is reported **only for a watching or planned entry**
(:data:`~arc.services.acquisition.wants.WANTING_STATUSES`). The client's
touch of a dormant entry is a ``PUT {}``, and for a completed entry that write
raises progress to the episode count and queues a MyAnimeList write the user
never asked for. An entry in any other status is reported ``active``: a ready
episode is then a plain link, and a not-ready one is simply demanded — no list
write either way (CLAUDE.md's MAL non-negotiable).

**During a rewatch pass** (FR-W7 item 5, owner-accepted 2026-11-03) the press
carries the rewatch on to the sequel when the sequel's list entry has progress
above 0 or is completed: :attr:`NextSeason.rewatch_carry` is then true, and
the client opens a pass on the sequel (``PUT /api/anime/{id}/rewatch``) instead
of any list write — whatever ``list_state`` says, so a dormant watching entry
at 5 is not touched either. Otherwise the press is the ordinary one above, in
normal mode (an absent sequel added as Watching). This module only says which;
it writes nothing in either case.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, Episode, EpisodeState, ListEntry, ListStatus
from arc.services.acquisition.dormancy import is_dormant
from arc.services.acquisition.wants import WANTING_STATUSES
from arc.services.catalog.airing import RELEASING, aired_through, is_aired
from arc.services.catalog.cache import episodes_for, preferred_title
from arc.services.catalog.schedule import NOT_YET_RELEASED
from arc.services.catalog.sequels import find_sequel, first_regular_episode, season_ends_here

#: The viewer's list entry for the sequel: there and needing no touch
#: (``active`` — including any status that is neither watching nor planned),
#: a watching/planned untouched import (``dormant``, FR-A9), or not there.
ListState = Literal["active", "dormant", "absent"]


@dataclass(frozen=True, slots=True)
class NextSeason:
    """The sequel's first episode, as the player offers it."""

    anime: Anime
    title: str
    episode: Episode
    fetchable: bool
    list_state: ListState
    #: Whether the episode has aired (and the sequel is not announced-only),
    #: so the player can say "hasn't aired yet" rather than "isn't ready yet".
    aired: bool
    #: The press carries an open rewatch pass on to the sequel (FR-W7 item 5):
    #: the client opens the sequel's pass and makes no list write.
    rewatch_carry: bool = False


def list_state(entry: ListEntry | None, *, airing: bool) -> ListState:
    """``active``, ``dormant`` or ``absent`` — FR-A9's rule on the viewer's entry.

    ``dormant`` only for a watching/planned entry: it is the state the client
    answers with a ``PUT {}``, and that write must never land on a completed,
    dropped or on-hold entry (it would move progress and write to MAL).
    """
    if entry is None:
        return "absent"
    if entry.status in WANTING_STATUSES and is_dormant(entry, airing=airing):
        return "dormant"
    return "active"


def carries_rewatch(entry: ListEntry | None, *, rewatching: bool) -> bool:
    """FR-W7 item 5: a pass carries on to a sequel with progress above 0 or completed.

    Only while the season being finished is in an open pass; a sequel not on
    the list, or on it at 0 and not completed, gets the ordinary press.
    """
    if not rewatching or entry is None:
        return False
    return entry.progress > 0 or entry.status is ListStatus.COMPLETED


def has_aired(sequel: Anime, episodes: list[Episode], episode: Episode, *, now: datetime) -> bool:
    """Whether ``episode`` of ``sequel`` has aired: never for an announced-only show."""
    if sequel.status == NOT_YET_RELEASED:
        return False
    boundary = aired_through(
        episodes, now=now, anime_status=sequel.status, next_airing=sequel.next_airing
    )
    return is_aired(episode, now=now, anime_status=sequel.status, boundary=boundary)


async def next_season(
    session: AsyncSession,
    *,
    user_id: int,
    anime: Anime,
    episode: Episode,
    has_following: bool,
    fetchable: Callable[[Episode], Awaitable[bool]],
    rewatching: bool = False,
    now: datetime,
) -> NextSeason | None:
    """The sequel's first episode, or ``None`` when there is nothing to offer.

    ``None`` unless the season ends at ``episode``
    (:func:`~arc.services.catalog.sequels.season_ends_here`), a sequel has a
    row in Arc, and that row has a regular episode. ``fetchable`` is the
    caller's :meth:`~arc.services.acquisition.demand_gate.DemandGate.fetchable`
    for an aired episode — asked only when the sequel's episode has aired and
    is not ready. ``rewatching`` is whether ``anime`` is in an open pass of
    the viewer's (:func:`carries_rewatch`).

    The caller does not ask at all while the opened episode is itself pending
    or playing from its download (FR-A14, FR-P8): that player polls ``/play``
    every few seconds, and the reads per poll are the cost this avoids. So a
    last episode watched while it still downloads gets no offer until the poll
    stops — once the episode is adopted as ready, the next read carries it.
    """
    if not season_ends_here(anime, episode, has_following=has_following):
        return None
    sequel = await find_sequel(session, anime)
    if sequel is None:
        return None
    rows = await episodes_for(session, sequel.id)
    first = first_regular_episode(rows)
    if first is None:
        return None
    entry = await session.get(ListEntry, (user_id, sequel.id))
    aired = has_aired(sequel, rows, first, now=now)
    return NextSeason(
        anime=sequel,
        title=preferred_title(sequel),
        episode=first,
        fetchable=aired and first.state is not EpisodeState.READY and await fetchable(first),
        list_state=list_state(entry, airing=sequel.status == RELEASING),
        aired=aired,
        rewatch_carry=carries_rewatch(entry, rewatching=rewatching),
    )


__all__ = [
    "ListState",
    "NextSeason",
    "carries_rewatch",
    "has_aired",
    "list_state",
    "next_season",
]
