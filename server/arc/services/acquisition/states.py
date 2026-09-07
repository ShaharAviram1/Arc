"""The episode state machine (spec §6).

``episodes.state`` is the one column six different jobs write, and before this
module each of them wrote it with a bare assignment. That is how an episode
ends up ``downloading`` after it was ``ready``, or ``wanted`` while a transcode
is running: every writer is individually reasonable and no single place knows
what the sequence is supposed to be.

So there is one writer, :func:`transition`, and it enforces the table below.

.. code-block:: text

    not_wanted → wanted → searching → downloading → downloaded
       → matching → (review) → matched → preparing → ready
    ready → (retention) → not_wanted
    ready → preparing                       (a deliberate re-encode, FR-P5)
    searching | downloading | downloaded | matching → unavailable
    unavailable → wanted                    (something wants it again)
    preparing → failed → preparing          (a transcode blew up, retry)

Four edges are in :data:`TRANSITIONS` without being drawn in the spec's
diagram, and all four are real.

* **→ matched from anywhere before it.** A file dropped into the manual
  directory belongs to an episode Arc never fetched, and confirming a review
  item does the same thing from the other side (FR-L6). Neither goes through
  the download half of the machine, and refusing them would mean a link that
  cannot record itself.
* **→ not_wanted from wanted / searching / unavailable.** The mirror of the
  first arrow: when the last want on an episode Arc has no bytes for goes
  away, the episode goes back to where it began. ``searching`` is included
  because the alternative is a dead end — the search job returns without
  starting a download, nothing else writes that row, and the episode sits in
  ``searching`` for ever, telling the show page it is being looked for when
  nobody is looking. Anything with bytes behind it — ``downloading``,
  ``downloaded``, ``matched`` — is left alone; retention (FR-T1, M10) is what
  unwinds those.
* **→ unavailable from downloaded / matching.** The file arrived and turned
  out not to be this episode: the torrent vanished from the client before its
  file was readable, or the matcher sent it to review and a person said "not
  this". Either way the episode has nothing, and ``unavailable`` is the state
  the daily retry (:data:`~arc.services.acquisition.wants.UNAVAILABLE_RETRY`)
  picks up and the show page explains (FR-A7). Leaving it at ``matching``
  would strand it exactly as ``searching`` used to be stranded.
* **ready → preparing.** A re-encode (M7): the admin ``force`` path, and the
  day the subtitle language or the encoder settings change. FR-P5 keeps the
  source file *precisely* so that a rendition can be redone, which is a
  promise the state machine has to be able to keep. Only the transcode job
  takes this edge; a link never does (:func:`advance_to_matched`), so a
  re-matched file still cannot pull a playable episode backwards.

Everything else raises :class:`IllegalTransition`. That is deliberate: an
impossible transition is a bug in the caller, and a state machine that
silently repairs its input is one that never tells anybody.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from arc.models import Episode, EpisodeState

log = logging.getLogger(__name__)

_S = EpisodeState

#: Every state a link may pull an episode *into* ``matched`` from — that is,
#: everything before ``matched`` in the pipeline plus the two dead ends a new
#: file legitimately rescues.
_TO_MATCHED: Final[frozenset[EpisodeState]] = frozenset({_S.MATCHED})

#: ``state → the states it may move to``. The single source of truth; the
#: docstring above is its prose form and the tests read this table directly.
TRANSITIONS: Final[Mapping[EpisodeState, frozenset[EpisodeState]]] = {
    _S.NOT_WANTED: frozenset({_S.WANTED}) | _TO_MATCHED,
    _S.WANTED: frozenset({_S.SEARCHING, _S.NOT_WANTED}) | _TO_MATCHED,
    _S.SEARCHING: frozenset({_S.DOWNLOADING, _S.UNAVAILABLE, _S.NOT_WANTED}) | _TO_MATCHED,
    _S.DOWNLOADING: frozenset({_S.DOWNLOADED, _S.UNAVAILABLE}) | _TO_MATCHED,
    _S.DOWNLOADED: frozenset({_S.MATCHING, _S.UNAVAILABLE}) | _TO_MATCHED,
    _S.MATCHING: frozenset({_S.UNAVAILABLE}) | _TO_MATCHED,
    _S.MATCHED: frozenset({_S.PREPARING}),
    _S.PREPARING: frozenset({_S.READY, _S.FAILED}),
    _S.FAILED: frozenset({_S.PREPARING}) | _TO_MATCHED,
    _S.READY: frozenset({_S.NOT_WANTED, _S.PREPARING}),
    _S.UNAVAILABLE: frozenset({_S.WANTED, _S.NOT_WANTED}) | _TO_MATCHED,
}

#: States a link must not pull an episode out of. ``ready`` is the obvious
#: one; ``preparing`` is the same argument one step earlier — a transcode in
#: flight is not restarted because the file it is reading got re-matched
#: (FR-P5: the source is kept precisely so a rendition can outlive it).
TERMINAL_STATES: Final[frozenset[EpisodeState]] = frozenset({_S.PREPARING, _S.READY})


class IllegalTransition(RuntimeError):
    """The requested state change is not in :data:`TRANSITIONS`."""

    def __init__(self, episode_id: int | None, current: EpisodeState, requested: EpisodeState):
        self.episode_id = episode_id
        self.current = current
        self.requested = requested
        allowed = ", ".join(sorted(state.value for state in TRANSITIONS.get(current, frozenset())))
        super().__init__(
            f"episode {episode_id}: {current.value} → {requested.value} is not a legal "
            f"transition (allowed from {current.value}: {allowed or 'nothing'})"
        )


def can_transition(current: EpisodeState, requested: EpisodeState) -> bool:
    """Whether ``current → requested`` is legal. A no-op counts as legal."""
    return requested is current or requested in TRANSITIONS.get(current, frozenset())


def transition(
    episode: Episode,
    new_state: EpisodeState,
    *,
    reason: str | None = None,
) -> bool:
    """Move ``episode`` to ``new_state``, or raise. Returns whether it moved.

    Re-requesting the state an episode is already in is a **no-op**, not an
    error: every handler that calls this may be run twice (CLAUDE.md), and the
    second run asking for the state the first run reached is the normal case,
    not a bug.

    ``reason`` is logged with every transition and is *stored* for exactly one
    of them: ``unavailable`` is the state a user is shown a sentence about
    (FR-A6, FR-A7), so it is written to ``unavailable_reason`` — and cleared on
    the way out of that state, because a stale explanation on an episode that
    is now downloading is worse than none.
    """
    current = episode.state
    if new_state is current:
        return False
    if new_state not in TRANSITIONS.get(current, frozenset()):
        raise IllegalTransition(episode.id, current, new_state)

    episode.state = new_state
    episode.state_changed_at = datetime.now(UTC)
    if new_state is _S.UNAVAILABLE:
        episode.unavailable_reason = reason
    elif current is _S.UNAVAILABLE:
        episode.unavailable_reason = None

    log.info(
        "episode state changed",
        extra={
            "episode_id": episode.id,
            "anime_id": episode.anime_id,
            "number": episode.number,
            "from": current.value,
            "to": new_state.value,
            "reason": reason,
        },
    )
    return True


def advance_to_matched(episode: Episode, *, reason: str | None = None) -> bool:
    """Move ``episode`` to ``matched`` unless that would be a downgrade.

    The no-downgrade rule is the caller's, not the machine's: ``preparing`` and
    ``ready`` have no legal edge to ``matched``, so :func:`transition` would
    raise — and a re-matched file must not *fail* the job that re-matched it.
    It is not an error for a linked file to belong to an episode that is
    already playable; it is the ordinary case (FR-P5).
    """
    if episode.state in TERMINAL_STATES:
        log.info(
            "link left an episode where it was",
            extra={"episode_id": episode.id, "state": episode.state.value},
        )
        return False
    return transition(episode, _S.MATCHED, reason=reason)


__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "IllegalTransition",
    "advance_to_matched",
    "can_transition",
    "transition",
]
