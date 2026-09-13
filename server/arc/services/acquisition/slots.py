"""The per-user slot cap: at most K shows fetching at once (FR-A10).

The third brake on acquisition, and the only one that is per *user*. The pause
is an admin's decision, the storage hold is the disk's, and this one is the
answer to a shape neither of them covers: one person with a large list, every
show of it legitimately activated, asking Arc for the next N episodes of forty
things on the same afternoon. Dormant imports (FR-A9) stop the *untouched* half
of that list; a cap stops the rest from arriving all at once, which is what
qBittorrent's three download slots and one disk actually allow.

**What occupies a slot.** A show the user has at least one live want on whose
episode is actually *in flight* — being searched for, downloaded or prepared.
Three endings are excluded, and each of them is a slot that would otherwise
never be given back:

* ``ready``. The episode has arrived; the show is waiting to be *watched*, not
  fetched. Holding a slot open for it would mean a user who lets three episodes
  pile up stops fetching everything else.
* ``unavailable``. FR-A6 has given up on finding a release and is retrying
  once a day. Five shows with no seeders left would otherwise freeze the whole
  list for ever, which is precisely the production shape dormancy was the
  first half of the answer to. The retry keeps running: it needs no slot,
  because it is not competing for the disk or the client's download queue.
* ``failed``. A transcode that broke needs an admin, not a slot.

A **sample** (FR-A8) never occupies or counts against a slot either — one
episode somebody asked to try is the smallest thing Arc does, and refusing it
because five shows are downloading would make "try episode 1" a button that
sometimes does nothing. So a sample may add one episode beyond the cap, which
is the one deliberate hole in it.

**Who gets the free slots.** Occupants keep theirs, always: a cap is a limit on
*starting* things, and cancelling a download halfway through to make room for a
show whose turn it now is would throw away bytes to obey an ordering. That also
means lowering K never cancels anything — the shows over the new cap simply
keep going and nothing new is admitted until they finish. What is left is
filled from the shows that actually want something fetched, **currently airing
first** (the weekly episode is the thing a person notices missing) and then by
``list_entries.updated_at`` descending (the show they touched most recently is
the one they are most likely watching tonight). Ties keep the caller's order,
which is stable — the reconciler reads entries ordered by (user, anime).

**Who competes at all.** Only a show with something to fetch: one whose window
holds an episode the user has no live want on yet (``hungry``). A show whose
window is already covered — caught up, not yet broadcasting, or fully ready —
needs no slot and is neither admitted nor waiting, because giving it one would
hold a slot open for a show that will not use it. That is the difference
between this and a count of rows in ``list_entries``, and it is what makes "one
of the five finished, so the sixth starts" true on the next tick.

**What waiting means.** The show contributes **nothing at all** to the
reconciliation — no want is created for it, and every row it already has,
live or dropped, is left exactly as it was found. Not "its live wants are kept"
but *every* row, because the ones a waiting show can be holding are exactly the
ones with something to lose: a ``ready`` episode's want that retention would
delete the files of, a sample mid-download, and — the case that took a review
to find — a want FR-T2 dropped for going unwatched, whose ``dropped_at`` is the
only record of when retention's grace period started and whose revival is
supposed to need an Arc-side touch. Reviving or deleting any of those because
a cap moved would be a cap deciding something about retention.

The user is told on the show page rather than left to wonder (FR-A10's "the
rest wait visibly").

:func:`assign_slots` is pure, so the whole rule is a table in the tests. The
queries that fill in ``fetching`` and ``hungry`` are the reconciler's, in
:mod:`arc.services.acquisition.wants`, which is also the only place the window
is known.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from arc.models import EpisodeState

#: Episode states a live want does **not** occupy a slot from: the episode has
#: arrived, or nothing is going to happen to it without a retry schedule or an
#: admin. See the module docstring for why each one is here — between them they
#: are every way a slot could be held for ever by a show that is not fetching.
SETTLED: frozenset[EpisodeState] = frozenset(
    {EpisodeState.READY, EpisodeState.UNAVAILABLE, EpisodeState.FAILED}
)


@dataclass(frozen=True, slots=True)
class SlotShow:
    """One (user, show) pair as the cap sees it.

    Deliberately not the ``ListEntry`` row: the rule reads four facts, two of
    which are not on the row at all (whether anything is in flight for this
    user, and whether the window has anything left to ask for), and a rule that
    took the row would have to be handed the wants and the episodes as well.
    """

    anime_id: int
    #: Whether the show is currently broadcasting (``RELEASING``). First claim
    #: on a free slot.
    airing: bool
    #: ``list_entries.updated_at`` — the tie-break after airing.
    updated_at: datetime
    #: A live, non-sample want of this user on an episode that is in flight —
    #: not one of :data:`SETTLED`. The show is occupying a slot right now.
    fetching: bool = False
    #: The window holds an episode this user has no live want on, so the show
    #: has something to ask for. A show that is not hungry needs no slot.
    hungry: bool = True


def assign_slots(shows: list[SlotShow], k: int) -> tuple[list[SlotShow], list[SlotShow]]:
    """``(admitted, waiting)`` for one user's shows under a cap of ``k``.

    ``admitted`` is every show allowed to create wants this run: the current
    occupants (whatever ``k`` says — see the module docstring) followed by as
    many hungry shows as the remaining slots allow, in the order the rule
    ranks them. ``waiting`` is the hungry shows there was no room for.

    A show that is neither fetching nor hungry appears in **neither** list. It
    is not being held back — it has nothing it wants — and counting it as
    waiting would put "waiting for a slot" on a show that is simply up to date.

    ``k <= 0`` is **no cap**: everything with something to do is admitted and
    nothing waits. That is the opposite of what 0 means for N (FR-A1's "fetch
    nothing"), and the two sit next to each other in the rules editor — a cap
    of nothing is no cap, while a window of nothing is no fetching.
    """
    occupants = [show for show in shows if show.fetching]
    candidates = [show for show in shows if not show.fetching and show.hungry]
    if k <= 0:
        return occupants + candidates, []

    # ``sorted`` is stable, so equal keys keep the caller's order — which is
    # the reconciler's (user, anime) ordering rather than whatever the planner
    # happened to build.
    ranked = sorted(candidates, key=_rank)
    free = max(0, k - len(occupants))
    return occupants + ranked[:free], ranked[free:]


def _rank(show: SlotShow) -> tuple[int, float]:
    """Airing first, then most recently touched. Lower sorts earlier."""
    return (0 if show.airing else 1, -show.updated_at.timestamp())


__all__ = ["SETTLED", "SlotShow", "assign_slots"]
