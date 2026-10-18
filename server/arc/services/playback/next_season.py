"""The "Next season" offer at the end of a finished season (FR-S5 item 3, 2026-10-18).

The player's ordinary Next knows only the same show's rows. At the last
episode of a finished season it now offers the sequel's first episode, when
the sequel is a show Arc has (:mod:`arc.services.catalog.sequels`).

Pressing it is a touch (FR-A9): the client first makes the sequel's entry
active — adds it as Watching when it is absent, re-sends it when it is dormant
— through the ordinary list write, and then demands the episode (FR-A14). So
the offer carries the viewer's **list state** for the sequel, and its
``fetchable`` is the answer the demand will get *after* that touch
(:func:`~arc.services.acquisition.wants.coverable_once_active`, or what is
already covered — a live "try episode 1" sample, FR-A8) — for an active entry
that is simply today's covered set, the same one the ordinary ``next`` reads.
Nothing here fetches, wants or writes anything.

``dormant`` is reported **only for a watching or planned entry**
(:data:`~arc.services.acquisition.wants.WANTING_STATUSES`). The client's
touch of a dormant entry is a ``PUT {}``, and for a completed entry that write
raises progress to the episode count and queues a MyAnimeList write the user
never asked for. An entry in any other status is reported ``active``: a ready
episode is then a plain link, and a not-ready one is fetchable only if it is
already covered — no list write either way (CLAUDE.md's MAL non-negotiable).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, Episode, EpisodeState, ListEntry
from arc.services.acquisition.dormancy import is_dormant
from arc.services.acquisition.wants import WANTING_STATUSES, coverable_once_active
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
    covered: Callable[[], Awaitable[frozenset[int]]],
    now: datetime,
) -> NextSeason | None:
    """The sequel's first episode, or ``None`` when there is nothing to offer.

    ``None`` unless the season ends at ``episode``
    (:func:`~arc.services.catalog.sequels.season_ends_here`), a sequel has a
    row in Arc, and that row has a regular episode. ``covered`` is the
    caller's :func:`~arc.services.acquisition.wants.coverable` set, asked for
    only when the episode is not ready.

    The caller does not ask at all while the opened episode is itself pending
    or playing from its download (FR-A14, FR-P8): that player polls ``/play``
    every few seconds, and a plan per poll is the cost this avoids. So a last
    episode watched while it still downloads gets no offer until the poll
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
    state = list_state(entry, airing=sequel.status == RELEASING)
    fetchable = False
    if first.state is not EpisodeState.READY:
        if state != "active":
            reach = await coverable_once_active(session, user_id, sequel, rows, entry, now=now)
            fetchable = first.id in reach
        # What is covered already counts either way: for an active entry it is
        # the whole answer, and on a dormant one a live sample want (FR-A8)
        # the demand would accept although the window does not reach it.
        fetchable = fetchable or first.id in await covered()
    return NextSeason(
        anime=sequel,
        title=preferred_title(sequel),
        episode=first,
        fetchable=fetchable,
        list_state=state,
        aired=has_aired(sequel, rows, first, now=now),
    )


__all__ = ["ListState", "NextSeason", "has_aired", "list_state", "next_season"]
