"""Playback: where a user got to, and what finishing an episode means.

One module for now (:mod:`arc.services.playback.progress`). It is its own
package rather than a file under ``catalog`` because the questions are
different in kind: the catalogue is about shows and the world's facts about
them, and this is about one person and one episode — the rules of FR-S2, FR-S4
and FR-W3, and the single point at which watching something changes a list
entry and therefore, eventually, MyAnimeList.
"""

from __future__ import annotations

from arc.services.playback.progress import (
    COMPLETION_FRACTION,
    CONTINUE_END_MARGIN_S,
    CONTINUE_LIMIT,
    CONTINUE_MIN_POSITION_S,
    RESUME_MAX_FRACTION,
    RESUME_MIN_S,
    ContinueRow,
    ProgressOutcome,
    UnmarkOutcome,
    completed_episode_ids,
    continue_watching,
    is_completed,
    record_progress,
    resume_position,
    unmark_watched,
)

# A leaf that imports nothing from Arc, which is the point of it: the
# acquisition reconciler reads ``watched_through`` from there while
# ``progress`` above reaches back into acquisition for FR-A9's stamp, and a
# definition either side could import is what keeps the rule in one place
# without either of them importing the other.
from arc.services.playback.watched import (
    WATCHED_BY_ARC,
    WATCHED_BY_PROGRESS,
    WatchedSource,
    watched_source,
    watched_through,
)

__all__ = [
    "COMPLETION_FRACTION",
    "CONTINUE_END_MARGIN_S",
    "CONTINUE_LIMIT",
    "CONTINUE_MIN_POSITION_S",
    "RESUME_MAX_FRACTION",
    "RESUME_MIN_S",
    "WATCHED_BY_ARC",
    "WATCHED_BY_PROGRESS",
    "ContinueRow",
    "ProgressOutcome",
    "UnmarkOutcome",
    "WatchedSource",
    "completed_episode_ids",
    "continue_watching",
    "is_completed",
    "record_progress",
    "resume_position",
    "unmark_watched",
    "watched_source",
    "watched_through",
]
