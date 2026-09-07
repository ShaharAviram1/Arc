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
    CONTINUE_LIMIT,
    CONTINUE_MIN_POSITION_S,
    RESUME_MAX_FRACTION,
    RESUME_MIN_S,
    ContinueRow,
    ProgressOutcome,
    completed_episode_ids,
    continue_watching,
    is_completed,
    record_progress,
    resume_position,
    unmark_watched,
)

__all__ = [
    "COMPLETION_FRACTION",
    "CONTINUE_LIMIT",
    "CONTINUE_MIN_POSITION_S",
    "RESUME_MAX_FRACTION",
    "RESUME_MIN_S",
    "ContinueRow",
    "ProgressOutcome",
    "completed_episode_ids",
    "continue_watching",
    "is_completed",
    "record_progress",
    "resume_position",
    "unmark_watched",
]
