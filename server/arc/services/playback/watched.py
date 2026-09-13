"""What "watched" means, for one episode and for a whole show (FR-W5).

Two pure functions and a type, in a module that imports **nothing from Arc**.
That is the whole reason it exists rather than living beside the rest of the
playback rules in :mod:`arc.services.playback.progress`: the acquisition
reconciler needs :func:`watched_through` and ``progress`` needs acquisition's
dormancy stamp, so one of the two directions has to be free of the other, and a
leaf holding the definition is what stops the rule being copied to both sides.

The owner's decision of 2026-09-13 is the rule; these are its only
implementation.
"""

from __future__ import annotations

from typing import Literal

#: Where an episode's watched mark came from — and, folded into the same value,
#: whether the viewer can take it back (FR-W5, owner 2026-09-13).
#:
#: ``arc`` is a mark **Arc can undo**: the un-mark either clears a completion
#: row of its own or lowers the list's progress past this episode, and either
#: way the viewer sees the tick go. ``progress`` is an episode *under* the
#: line — watched, on the list's word, with nothing here to undo, because the
#: un-mark only ever moves the line by one.
#:
#: One value rather than a second ``unwatchable`` flag beside it: the client's
#: only question is "is this pill a button?", the two would never legitimately
#: disagree, and a boolean that always tracks another field is a boolean that
#: eventually does not.
WatchedSource = Literal["arc", "progress"]

#: The two values, spelled once so a router and a schema cannot disagree.
WATCHED_BY_ARC: WatchedSource = "arc"
WATCHED_BY_PROGRESS: WatchedSource = "progress"


def watched_source(number: int, *, completed: bool, list_progress: int) -> WatchedSource | None:
    """Whether this episode is watched by this user, and on whose word (FR-W5).

    The owner's rule of 2026-09-13: *at or below the list's progress, or
    completed in Arc*. The answer says which of the two it rests on, because
    that decides whether the show page's pill is a button (see
    :data:`WatchedSource`).

    Three cases, in the order they are asked:

    * **above the progress** — watched only if Arc holds a completion row for
      it, which happens when progress was lowered under a completion by a
      MyAnimeList import or by hand. ``arc``: the un-mark clears that row.
    * **exactly at the progress** — the latest watched episode, and ``arc``
      *whether or not* a completion row exists, because the owner's revision of
      2026-09-13 makes the explicit un-mark lower progress to ``N-1``. A list
      imported at 9 therefore has one episode the viewer can take back, which
      is what makes "I marked one too many" fixable at all.
    * **below the progress** — ``progress``. Watched, and not the episode the
      un-mark would move: taking back episode 4 of a list that says 9 would
      have to claim something about 5…9 that the viewer never said. The client
      says so rather than offering a button ("Unwatch from the latest watched
      episode down").

    A progress of zero means nothing is watched, whatever the numbering: a
    show whose catalogue entry starts at episode 0 would otherwise read its
    first episode as watched by an untouched list.
    """
    if list_progress <= 0 or number > list_progress:
        return WATCHED_BY_ARC if completed else None
    if number == list_progress:
        return WATCHED_BY_ARC
    return WATCHED_BY_PROGRESS


def watched_through(list_progress: int, highest_completion: int = 0) -> int:
    """How far one user has got in one show, as a single number (FR-W5).

    The acquisition window starts after this
    (:func:`arc.services.acquisition.wants.window`): the next unwatched episode
    is the one after whichever of the two facts is further on, so a list
    imported at 9 and a completion of 11 both mean "fetch 12".

    **It is deliberately not the same shape as** :func:`watched_source`, and
    the difference is worth naming because the two look interchangeable.
    ``watched_source`` answers a question about *one* episode and needs that
    episode's own completion; this collapses a whole show to a boundary, so an
    episode below the furthest completion is behind the line even with no row
    of its own. They disagree in exactly one state — progress below a
    completion, which only a MyAnimeList import or a hand-edited progress can
    produce, since every completion raises progress
    (``progress._advance_list``) — and there the right answers really are
    different: acquisition must not re-fetch an episode 4 that sits below
    somebody's episode 11, and a show page must not claim they watched it.
    """
    return max(list_progress, highest_completion)


__all__ = [
    "WATCHED_BY_ARC",
    "WATCHED_BY_PROGRESS",
    "WatchedSource",
    "watched_source",
    "watched_through",
]
