"""Where one episode of a trip stands, in a word the device can act on (FR-A12).

One pure function, :func:`trip_phase`, so the table in the tests *is* the rule
and one place to extend. ``delivered`` and ``expired`` are read straight off
the ``trip_episodes`` row: the device's confirmation writes the first
(:mod:`arc.services.trips.deliver`), the hourly sweep the second
(:mod:`arc.services.trips.sweep`).

The phases, in the order an episode passes through them:

* ``waiting_space`` — not started, because the disk is under the floor
  (FR-T6); it starts on its own once retention has made room;
* ``searching`` — looking for a release, or about to (the next
  reconciliation starts it);
* ``downloading`` — bytes arriving, with a fraction;
* ``preparing`` — the file is here and the small copy is being made, with a
  fraction once the encode reports one;
* ``available`` — the copy is waiting for the device to fetch it;
* ``unavailable`` — Arc has given up (FR-A6), or there is no copy and nothing
  left to make one from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from arc.models import EpisodeState, OfflineCopyState, TripEpisodeState

type TripPhase = Literal[
    "searching",
    "downloading",
    "preparing",
    "available",
    "delivered",
    "expired",
    "unavailable",
    "waiting_space",
]

#: States that mean "the file is here, or is being looked at": a copy is next.
_LANDED: frozenset[EpisodeState] = frozenset(
    {
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHING,
        EpisodeState.MATCHED,
        EpisodeState.PREPARING,
    }
)


@dataclass(frozen=True, slots=True)
class PhaseFacts:
    """Everything :func:`trip_phase` reads about one trip episode."""

    episode_state: EpisodeState
    row_state: TripEpisodeState = TripEpisodeState.PENDING
    copy_state: OfflineCopyState | None = None
    #: Whether a source file is still linked to the episode.
    has_source: bool = False
    #: Whether an ``offline_encode`` job for it is pending or running.
    encoding: bool = False
    #: Whether acquisition is holding itself for want of disk space (FR-T6).
    held: bool = False


def trip_phase(facts: PhaseFacts) -> TripPhase:
    """The phase of one trip episode. Pure.

    The copy decides first: a ready copy is ``available`` whatever the episode
    is doing (a trip-only episode's is ``not_wanted`` by then, its source
    deleted). Any other copy row is ``preparing`` while an ``offline_encode``
    is pending or running for it and ``unavailable`` otherwise (a failed copy
    is not retried, FR-P6; a queued row with no job is a dead encode). Then
    the episode's own state. A ``ready`` episode with no
    copy and no source is ``unavailable`` here — the device's fallback to the
    full-size file is M19 T6's to decide.

    The row's own ending answers before anything else: ``delivered`` and
    ``expired`` as themselves, and a ``cancelled``
    row as ``unavailable`` — the trip that held it is over.
    """
    if facts.row_state is TripEpisodeState.DELIVERED:
        return "delivered"
    if facts.row_state is TripEpisodeState.EXPIRED:
        return "expired"
    if facts.row_state is TripEpisodeState.CANCELLED:
        return "unavailable"
    if facts.copy_state is OfflineCopyState.READY:
        return "available"
    if facts.copy_state is not None:
        # Queued, preparing or failed: only an encode actually on its way
        # makes it "preparing". A failed copy is not retried automatically
        # (FR-P6), and a queued/preparing row with no live job is an encode
        # that died — both are given up on until somebody asks again.
        return "preparing" if facts.encoding else "unavailable"
    state = facts.episode_state
    if state is EpisodeState.READY:
        return "preparing" if facts.has_source else "unavailable"
    if state in _LANDED:
        return "preparing"
    if state is EpisodeState.DOWNLOADING:
        return "downloading"
    if state in (EpisodeState.WANTED, EpisodeState.SEARCHING):
        return "searching"
    if state in (EpisodeState.UNAVAILABLE, EpisodeState.FAILED):
        return "unavailable"
    # ``not_wanted`` with no copy: the next reconciliation starts it, unless
    # the disk is too full for it to.
    return "waiting_space" if facts.held else "searching"


__all__ = ["PhaseFacts", "TripPhase", "trip_phase"]
