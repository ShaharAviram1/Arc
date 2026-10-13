"""The pre-match gate: may a file still downloading be played? (M20 T6, FR-L4)

FR-L4 says a file is not played until the matcher linked it on its own or a
person confirmed it. Playing while downloading (FR-P8) has to start before
the file exists as a whole, so it cannot wait for ``match_file``. The owner's
answer (2026-10-13, plan Q6): play early **only when the in-flight file's name
pre-matches at the auto-link threshold**, with the download's prior, exactly as
the matcher would decide once the file is in — otherwise wait for the file and
the ordinary match.

So this runs the matcher's own steps over the *name* qBittorrent reports for
the file, in the order :func:`arc.services.library.jobs.match_file` runs them:
a creditless or batch name refuses; no candidate refuses; two season marks
that disagree refuse; a name naming another season than the download refuses
(:func:`~arc.services.library.matcher.prior_season_mismatch`); no episode
number refuses; below either auto-link bar refuses. And one more, which the
matcher has no need of: the best candidate must be **the episode being
downloaded**. A confident answer naming another episode is not a reason to
play this one.

**Nothing is indexed.** No ``media_files`` row, no review item, no link: the
gate answers a question and writes nothing (FR-L1 holds). The real match
still runs when the download completes, and it alone links.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Anime
from arc.services.catalog.factory import catalog_for
from arc.services.library.matcher import match, prior_season_mismatch, prior_season_reason
from arc.services.library.parser import parse, season_conflict_reason

log = logging.getLogger(__name__)

REFUSED_NOT_AN_EPISODE = "the file name is not a single episode"
REFUSED_NO_CANDIDATES = "the file name matches no show"
REFUSED_NO_EPISODE = "no episode number in the file name"
REFUSED_LOW_CONFIDENCE = "the file name does not match this episode surely enough"
REFUSED_ELSEWHERE = "the file name matches another episode"


@dataclass(frozen=True, slots=True)
class PreMatch:
    """The gate's answer. ``reason`` is set exactly when ``ok`` is false."""

    ok: bool
    reason: str | None = None
    confidence: float | None = None


async def prematch(
    session: AsyncSession,
    settings: Settings,
    file_name: str,
    *,
    anime_id: int,
    number: int,
) -> PreMatch:
    """Whether ``file_name`` would auto-link to ``(anime_id, number)`` (FR-L4's exception).

    ``file_name`` is the base name of the file inside the torrent. Reads the
    catalogue cache (and, if the offline catalogue has nothing, the live one,
    as the matcher does); writes nothing.
    """
    parsed = parse(file_name, path=True)
    if parsed.kind in {"nc", "batch"}:
        return PreMatch(False, REFUSED_NOT_AN_EPISODE)
    async with catalog_for(settings) as catalog:
        result = await match(
            session,
            catalog,
            parsed,
            expected=(anime_id, number),
            minimum=settings.match_min_candidate,
        )
    best = result.best
    if best is None:
        return PreMatch(False, REFUSED_NO_CANDIDATES, result.confidence)
    if parsed.season_conflict:
        return PreMatch(False, season_conflict_reason(parsed), result.confidence)
    prior_row = await session.get(Anime, anime_id)
    mismatch = prior_season_mismatch(parsed, prior_row) if prior_row is not None else None
    if mismatch is not None:
        return PreMatch(False, prior_season_reason(*mismatch), result.confidence)
    if best.episode_number is None:
        return PreMatch(False, REFUSED_NO_EPISODE, result.confidence)
    if not result.auto_links(
        settings.match_auto_threshold, min_title=settings.match_min_title_for_auto
    ):
        return PreMatch(False, REFUSED_LOW_CONFIDENCE, result.confidence)
    if best.anime_id != anime_id or best.episode_number != number:
        return PreMatch(False, REFUSED_ELSEWHERE, result.confidence)
    if parsed.episode is not None and parsed.episode != number:
        # The matcher scores the download's own candidate at the episode the
        # download was for (FR-L3), which is right for a finished file whose
        # name uses absolute numbering. Before the file is whole that benefit
        # of the doubt is not given: a name that says another number waits.
        return PreMatch(False, REFUSED_ELSEWHERE, result.confidence)
    return PreMatch(True, None, result.confidence)


__all__ = [
    "REFUSED_ELSEWHERE",
    "REFUSED_LOW_CONFIDENCE",
    "REFUSED_NOT_AN_EPISODE",
    "REFUSED_NO_CANDIDATES",
    "REFUSED_NO_EPISODE",
    "PreMatch",
    "prematch",
]
