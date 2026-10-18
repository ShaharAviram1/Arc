"""The "Next season" offer at the end of a finished season (FR-S5 item 3, 2026-10-18).

The player's ordinary Next knows only the same show's rows. At the last
episode of a finished season it now offers the sequel's first episode, when
the sequel is a show Arc has (:mod:`arc.services.catalog.sequels`).

Pressing it is a touch (FR-A9): the client first makes the sequel's entry
active — adds it as Watching when it is absent, re-sends it when it is dormant
— through the ordinary list write, and then demands the episode (FR-A14). So
the offer carries the viewer's **list state** for the sequel, and its
``fetchable`` is the answer the demand will get *after* that touch
(:func:`~arc.services.acquisition.wants.coverable_once_active`) — for an
active entry that is simply today's covered set, the same one the ordinary
``next`` reads. Nothing here fetches, wants or writes anything.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, Episode, EpisodeState, ListEntry
from arc.services.acquisition.dormancy import is_dormant
from arc.services.acquisition.wants import coverable_once_active
from arc.services.catalog.airing import RELEASING
from arc.services.catalog.cache import episodes_for, preferred_title
from arc.services.catalog.sequels import find_sequel, first_regular_episode, season_ends_here

#: The viewer's list entry for the sequel: there and fetching (``active``),
#: there but an untouched import (``dormant``, FR-A9), or not there.
ListState = Literal["active", "dormant", "absent"]


@dataclass(frozen=True, slots=True)
class NextSeason:
    """The sequel's first episode, as the player offers it."""

    anime: Anime
    title: str
    episode: Episode
    fetchable: bool
    list_state: ListState


def list_state(entry: ListEntry | None, *, airing: bool) -> ListState:
    """``active``, ``dormant`` or ``absent`` — FR-A9's rule on the viewer's entry."""
    if entry is None:
        return "absent"
    return "dormant" if is_dormant(entry, airing=airing) else "active"


async def next_season(
    session: AsyncSession,
    *,
    user_id: int,
    anime: Anime,
    episode: Episode,
    has_following: bool,
    covered: Callable[[], Awaitable[frozenset[int]]],
    now: datetime,
) -> NextSeason | None:
    """The sequel's first episode, or ``None`` when there is nothing to offer.

    ``None`` unless the season ends at ``episode``
    (:func:`~arc.services.catalog.sequels.season_ends_here`), a sequel has a
    row in Arc, and that row has a regular episode. ``covered`` is the
    caller's :func:`~arc.services.acquisition.wants.coverable` set, asked for
    only when the answer needs it (an active entry, an episode not ready).
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
    state = list_state(entry, airing=sequel.status == RELEASING)
    fetchable = False
    if first.state is not EpisodeState.READY:
        if state == "active":
            fetchable = first.id in await covered()
        else:
            reach = await coverable_once_active(session, user_id, sequel, rows, entry, now=now)
            fetchable = first.id in reach
    return NextSeason(
        anime=sequel,
        title=preferred_title(sequel),
        episode=first,
        fetchable=fetchable,
        list_state=state,
    )


__all__ = ["ListState", "NextSeason", "list_state", "next_season"]
