"""What a press of play would get right now: the read side of the demand (FR-A14, M21).

:func:`~arc.services.acquisition.demand.demand` decides a press under the want
lock. The show page and the player have to say *before* the press whether it
will be accepted — a Play chip that leads to a refusal is worse than a
sentence in place — so this module reads the same four facts the demand reads,
without the lock and without writing anything:

* the caller's open demands (:func:`~arc.services.acquisition.demand.open_demands`)
  and ``demand_max_open`` — the cap (0 = no cap);
* whether acquisition is paused, and whether it is held for disk space (FR-T6).

and applies them **in the demand's own order** (:meth:`DemandGate.refuses`): a
ready episode is never refused (there is nothing to fetch); a new demand beyond
the cap is refused :data:`~arc.services.acquisition.demand.DEMAND_CAP` (a press
on an episode already demanded only restamps it and never counts); a pause or
a hold refuses only an episode that still needs fetching
(:data:`~arc.services.acquisition.demand.NEEDS_FETCH`). "Not aired" is the
caller's half: the gate is told whether the episode has aired, because the
caller already holds the show's rows the rule needs.

M21 T4 replaced M20's coverage helpers (``wants.coverable`` and its once-active
twin) with this: the demand no longer asks whether a window covers the episode,
so the flags that predict it no longer plan a window either. Nothing here plans
wants — the cost is three small reads and, while a disk floor is set, one
``statvfs``.

Nothing here touches a list entry or MyAnimeList.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState
from arc.services.acquisition.demand import (
    ACQUISITION_PAUSED,
    ACQUISITION_PAUSED_MESSAGE,
    DEMAND_CAP,
    NEEDS_FETCH,
    STORAGE_HELD,
    STORAGE_HELD_MESSAGE,
    OpenDemand,
    demand_cap_message,
    open_demands,
)
from arc.services.acquisition.rules import demand_max_open, is_paused, is_storage_held


@dataclass(frozen=True, slots=True)
class DemandRefusal:
    """A refusal a press would get: the demand's code and its sentence."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DemandGate:
    """One user's standing with the demand right now. Pure once built.

    ``open`` is their open demands (oldest press first), ``max_open`` the cap
    (0 = none), ``paused`` and ``held`` the two acquisition stops.
    """

    open: tuple[OpenDemand, ...] = ()
    max_open: int = 0
    paused: bool = False
    held: bool = False

    @property
    def open_ids(self) -> frozenset[int]:
        """The episode ids of the open demands (``EpisodeOut.demanded``)."""
        return frozenset(found.episode_id for found in self.open)

    @property
    def at_cap(self) -> bool:
        """Whether a press on an episode not yet demanded would be refused for the cap."""
        return self.max_open > 0 and len(self.open) >= self.max_open

    def refusal(self) -> DemandRefusal | None:
        """The refusal a click on the page would get now, or ``None`` (``AnimeDetail.demand``).

        **One** refusal, in the demand's order: the cap first (it applies to
        any row not already demanded), then the pause, then the hold (each
        only for a row that still needs fetching — the client reads the row's
        state). So with the cap reached *and* acquisition paused the page
        carries ``demand_cap``: an already-demanded row that still needs
        fetching then reads as pressable, and the press gets 409
        ``acquisition_paused``. The client must handle the demand's 409
        whatever this says (M21 T6); :meth:`refuses` is the exact per-episode
        answer.
        """
        if self.at_cap:
            return DemandRefusal(DEMAND_CAP, demand_cap_message(self.max_open))
        if self.paused:
            return DemandRefusal(ACQUISITION_PAUSED, ACQUISITION_PAUSED_MESSAGE)
        if self.held:
            return DemandRefusal(STORAGE_HELD, STORAGE_HELD_MESSAGE)
        return None

    def refuses(self, episode: Episode) -> DemandRefusal | None:
        """What a press on this (aired) episode would be refused with, or ``None``.

        Exactly :func:`~arc.services.acquisition.demand.demand`'s steps 1, 3–5.
        """
        if episode.state is EpisodeState.READY:
            return None
        if episode.id not in self.open_ids and self.at_cap:
            return DemandRefusal(DEMAND_CAP, demand_cap_message(self.max_open))
        if episode.state in NEEDS_FETCH:
            if self.paused:
                return DemandRefusal(ACQUISITION_PAUSED, ACQUISITION_PAUSED_MESSAGE)
            if self.held:
                return DemandRefusal(STORAGE_HELD, STORAGE_HELD_MESSAGE)
        return None

    def fetchable(self, episode: Episode, *, aired: bool) -> bool:
        """Aired ∧ not ready ∧ nothing would refuse the press (``next.fetchable``)."""
        return aired and episode.state is not EpisodeState.READY and self.refuses(episode) is None


async def demand_gate(
    session: AsyncSession, user_id: int, *, settings: Settings | None
) -> DemandGate:
    """Read the four facts for one user. Read-only.

    ``settings`` is what makes the disk measurable; without one there is no
    measurement, which is never held — the demand's own rule.
    """
    return DemandGate(
        open=tuple(await open_demands(session, user_id)),
        max_open=await demand_max_open(session),
        paused=await is_paused(session),
        held=settings is not None and await is_storage_held(session, settings),
    )


__all__ = ["DemandGate", "DemandRefusal", "demand_gate"]
