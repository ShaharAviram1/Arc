"""Attaching a file to an episode (spec §6, FR-L4, FR-L6).

One function does the linking and both callers use it — the ``match_file``
handler when it is confident enough, and ``POST /api/review/{id}/confirm``
when a person is. Two code paths that both had to find-or-create an episode
row and both had to get the state transition right is exactly how the two
would come to disagree.

Three rules live here.

* **The episode row is created if it has to be.** A manual drop is often a
  show whose episodes Arc has never listed — nobody has opened the show page,
  so the airing schedule was never synced. The row is created with a null
  ``air_at``: Arc does not know when it aired, and inventing a date would put
  a fiction on the show page. The next catalogue refresh fills it in
  (:func:`arc.services.catalog.cache.sync_episodes` back-fills rather than
  replaces).
* **The transition never goes backwards.** Linking sets the episode to
  ``matched``, which is right from every earlier state, and would be a
  downgrade from ``preparing`` or ``ready`` — a re-linked file must not send a
  playable episode back to the start of the pipeline (FR-P5: the source is
  kept precisely so a rendition can outlive it).
* **Linking is idempotent.** Running the same match twice writes the same row
  twice with the same values, which is what lets the job be retried.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Anime, Episode, EpisodeState, MediaFile, ReviewState

log = logging.getLogger(__name__)

#: States a link must not pull an episode out of. ``ready`` is the obvious
#: one; ``preparing`` is the same argument one step earlier — a transcode in
#: flight is not restarted because the file it is reading got re-matched.
TERMINAL_STATES: frozenset[EpisodeState] = frozenset({EpisodeState.PREPARING, EpisodeState.READY})


class LinkError(RuntimeError):
    """The link cannot be made. Carries the reason the API answers with."""


class UnknownAnime(LinkError):
    """No ``anime`` row with that internal id."""


async def ensure_episode(session: AsyncSession, anime_id: int, number: int) -> Episode:
    """The ``episodes`` row for ``(anime_id, number)``, created if missing.

    ``air_at`` is left null on a row created here — see the module docstring.
    ``UniqueConstraint(anime_id, number)`` is what makes this safe to call
    from two jobs at once; the flush is where a race would surface, and the
    caller's retry is what resolves it.
    """
    if number < 1:
        raise LinkError(f"episode number must be positive, got {number}")
    anime = await session.get(Anime, anime_id)
    if anime is None:
        raise UnknownAnime(f"no anime with id {anime_id}")

    episode = await session.scalar(
        select(Episode).where(Episode.anime_id == anime_id, Episode.number == number)
    )
    if episode is not None:
        return episode

    episode = Episode(
        anime_id=anime_id,
        number=number,
        state=EpisodeState.MATCHED,
        state_changed_at=datetime.now(UTC),
    )
    session.add(episode)
    await session.flush()
    log.info(
        "episode row created for a matched file", extra={"anime_id": anime_id, "number": number}
    )
    return episode


def advance_to_matched(episode: Episode) -> bool:
    """Move ``episode`` to ``matched`` unless that would be a downgrade."""
    if episode.state in TERMINAL_STATES or episode.state is EpisodeState.MATCHED:
        return False
    episode.state = EpisodeState.MATCHED
    episode.state_changed_at = datetime.now(UTC)
    return True


async def link(
    session: AsyncSession,
    media_file: MediaFile,
    *,
    anime_id: int,
    episode_number: int,
    review_state: ReviewState,
    confidence: float | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> Episode:
    """Point ``media_file`` at an episode and record how that was decided.

    ``review_state`` says who decided: :attr:`ReviewState.AUTO` for the
    matcher, :attr:`ReviewState.CONFIRMED` for a person. The candidate list is
    kept either way — it is the evidence behind an automatic link, and the
    review UI shows it when somebody reopens the decision.
    """
    episode = await ensure_episode(session, anime_id, episode_number)
    media_file.episode_id = episode.id
    media_file.review_state = review_state
    if confidence is not None:
        media_file.match_confidence = confidence
    if candidates is not None:
        media_file.match_candidates = candidates
    moved = advance_to_matched(episode)
    await session.flush()
    log.info(
        "media file linked",
        extra={
            "media_file_id": media_file.id,
            "episode_id": episode.id,
            "anime_id": anime_id,
            "number": episode_number,
            "review_state": review_state.value,
            "confidence": confidence,
            "episode_state_changed": moved,
        },
    )
    return episode


__all__ = [
    "TERMINAL_STATES",
    "LinkError",
    "UnknownAnime",
    "advance_to_matched",
    "ensure_episode",
    "link",
]
