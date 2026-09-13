"""Dormant imports: a list entry nobody has touched in Arc (FR-A9).

A MyAnimeList import is a baseline, not a request (FR-M2). The owner's list
brought 414 wants into existence in one afternoon: shows planned in 2018 whose
releases have no seeders left, holding every download slot qBittorrent had and
filling the disk with what did arrive. Nobody had asked Arc for any of it — the
rows were simply *there*, and FR-A1 read "watching or planned" literally.

So the rule (owner, 2026-09-13): a ``watching``/``planned`` entry generates no
wants until the user has done something to that show **in Arc** — a status,
progress or score change, an episode watched, a "try episode 1". That moment is
stamped on ``list_entries.activated_at`` and never cleared: activation does not
expire, because a show somebody asked for in March is still a show they asked
for. The exception is a show that is **currently airing**, which keeps fetching
whether or not it has been touched: the weekly use of Arc is "the episode is
there on the morning it airs", and a user whose whole list arrived by import
should not have to press something to get that.

Two functions and a sentence, in a module with no imports of its own beyond the
model. That is deliberate: :mod:`arc.services.catalog.lists`,
:mod:`arc.services.playback.progress` and :mod:`arc.api.mal`'s revert all write
the stamp, :mod:`arc.services.acquisition.wants` and the API schemas read it —
and ``catalog.lists`` cannot import ``acquisition.wants`` without closing an
import cycle through ``catalog.airing``. A leaf every side can reach is the
price of one rule with one implementation.

**"Try episode 1" is the one user action that does not activate** (FR-A8). It
writes its own want for exactly one episode, which is what was asked for;
activating would hand the show the whole N-episode window, on a list the user
may not have curated. The show starts fetching properly when they say so — a
status, a finished episode, or the Show page's "Fetch this show".

:func:`is_dormant` takes *whether the show is airing* rather than the show,
for the same reason: "is this airing?" is the catalogue's question
(:data:`arc.services.catalog.airing.RELEASING`), and the answer is a boolean by
the time this rule cares.
"""

from __future__ import annotations

from datetime import datetime

from arc.models import ListEntry

#: What :func:`arc.services.acquisition.wants.compute_wants` writes into
#: ``wants.drop_reason`` for a row whose entry is dormant. Distinct from
#: :data:`~arc.services.acquisition.wants.REASON_NOT_WANTING` because it is a
#: different fact about a different row — the show has not been dropped, it has
#: never been picked up — and an admin reading the wants table after an import
#: needs to see which of the two it is. Both revive unconditionally, so the
#: difference is in what it says, not in what it does.
REASON_DORMANT = "imported; not touched in Arc yet"


def activate(entry: ListEntry, *, now: datetime) -> bool:
    """Stamp this entry as touched in Arc. ``True`` if this was the first time.

    Write-once: a second touch leaves the original moment alone, because
    ``activated_at`` answers "has the user ever engaged with this show here?"
    and not "when did they last?" — ``updated_at`` is the second question and
    already exists. It is also never *cleared*, here or anywhere: FR-A9's
    "activation never expires".

    Called from the Arc-side writes of a list entry: every ``PUT`` through
    :func:`arc.services.catalog.lists.set_list_entry`, every progress report
    for a show with an entry
    (:func:`arc.services.playback.progress._activate_entry`) plus the entry
    FR-S4 creates on a completion, and a write-log revert
    (``arc.api.mal.revert``, FR-M7's third user-originated event). From none of
    the MyAnimeList-driven ones — an import, a push or a conflict resolution
    leaves it exactly as it found it — and not from ``request_sample`` (above).
    """
    if entry.activated_at is not None:
        return False
    entry.activated_at = now
    return True


def is_dormant(entry: ListEntry, *, airing: bool) -> bool:
    """Whether this entry generates no wants (FR-A9).

    Never touched in Arc, and the show is not airing. An airing show is the
    documented exception; everything else waits for the user.
    """
    return entry.activated_at is None and not airing


__all__ = ["REASON_DORMANT", "activate", "is_dormant"]
