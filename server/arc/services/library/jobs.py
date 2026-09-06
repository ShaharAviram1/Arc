"""The two library handlers: ``library_scan`` and ``match_file`` (FR-L1, FR-L3).

``library_scan`` walks the directories and creates rows;
``match_file`` decides what one row is. They are separate jobs on purpose: a
scan is filesystem work that must finish quickly and never block on a network,
and a match is a catalogue request that must be retried with backoff when
AniList is down. Putting them in one handler would mean a slow catalogue makes
new downloads invisible.

Both are idempotent. A second scan finds no new paths; a second match
recomputes the same answer and writes the same columns. The one thing a
re-match must not do is undo a decision, so a row a human has already
confirmed or ignored is left alone (FR-L6) — and a row that is *linked* is
never unlinked here either, whatever the new answer is (see :func:`_review`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from arc.models import MediaFile, ReviewState
from arc.services.catalog.factory import catalog_for
from arc.services.jobs.registry import JobContext, register
from arc.services.library import ingest
from arc.services.library.link import LinkError, link
from arc.services.library.matcher import MatchResult, match
from arc.services.library.names import LIBRARY_SCAN, MATCH_FILE
from arc.services.library.parser import ParsedName, parse

log = logging.getLogger(__name__)

#: Review states a person has set. A re-run of ``match_file`` reads one of
#: these and stops: the queue's job is to be resolved by a human once, not to
#: keep re-proposing an answer somebody has already rejected.
DECIDED: frozenset[ReviewState] = frozenset({ReviewState.CONFIRMED, ReviewState.IGNORED})

#: Why a file was ignored or sent to review, stored in ``match_candidates``'s
#: place when there is nothing to show. Short strings, rendered by the client.
REASON_NC = "creditless opening/ending, preview or trailer — not an episode"
REASON_BATCH = "a batch or multi-episode file; link the episodes individually"
REASON_NO_EPISODE = "no episode number in the filename"
REASON_NO_CANDIDATES = "no good candidates"
REASON_LOW_CONFIDENCE = "below the auto-link threshold"
REASON_WEAK_TITLE = "title match is not close enough to link automatically"


@register(LIBRARY_SCAN)
async def library_scan(ctx: JobContext) -> None:
    """Walk the library directories and index anything new (FR-L1)."""
    result = await ingest.scan(ctx.session, ctx.settings)
    ctx.log.info(
        "library scan",
        extra={
            "job_id": ctx.job.id,
            **result.as_dict(),
            # Absolute: ``DATA_DIR`` is relative in dev (``./data``), and
            # "which ./data" depends on where the worker was started from.
            "roots": [str(root.resolve()) for root in ctx.settings.library_dirs],
        },
    )


def _parsed_of(media_file: MediaFile) -> ParsedName:
    """Parse the filename again rather than trusting the stored blob.

    ``media_files.parsed`` is what the API renders; it is not the input to a
    match. Re-parsing costs microseconds, is pure, and means a row indexed by
    last month's parser is matched by this month's — which matters, because
    the parser changes every time the corpus grows a case it got wrong.

    The **basename**, not the stored absolute path, so that this and the parse
    ingest stored produce the same :class:`ParsedName` — including its ``raw``
    field, which would otherwise carry the whole path here and the filename
    there.
    """
    return parse(Path(media_file.path).name)


def _ignore(media_file: MediaFile, reason: str) -> None:
    media_file.review_state = ReviewState.IGNORED
    media_file.match_confidence = None
    media_file.match_candidates = [{"reason": reason}]


def _review(media_file: MediaFile, result: MatchResult, reason: str) -> None:
    """Send a file to the queue, with whatever the matcher had and why.

    The reason is always the **last** entry of ``match_candidates``, next to
    the candidates rather than instead of them: "here are three shows it could
    be, and here is why I would not choose between them" is a more useful
    review item than either half alone, and the client renders a reason-only
    entry as a sentence (:class:`arc.api.review_schemas.CandidateOut`).

    **A file that is already linked is left exactly as it is.** A re-match runs
    for reasons that have nothing to do with the file — the parser changed, the
    catalogue row was refreshed, the job was retried — and a run that happens to
    be less sure than the last one must not take an episode away from a show
    page, from a transcode in flight, or from somebody's progress. Unlinking is
    a deliberate act with consequences (:func:`arc.api.review.reopen` says the
    same thing from the other side), so it belongs to a person and to the admin
    tools, never to a background job.
    """
    if media_file.episode_id is not None:
        log.info(
            "a linked file was re-matched less confidently and was left linked",
            extra={
                "media_file_id": media_file.id,
                "episode_id": media_file.episode_id,
                "confidence": result.confidence,
                "reason": reason,
            },
        )
        return
    media_file.review_state = ReviewState.PENDING
    media_file.match_confidence = result.confidence
    media_file.match_candidates = [*result.top(), {"reason": reason}]


@register(MATCH_FILE)
async def match_file(ctx: JobContext) -> None:
    """Match one ``media_files`` row, and link it only if it is sure (FR-L4).

    The ``expected`` prior is not read from anywhere yet: M6 is what knows
    which episode Arc was downloading, and it will pass it in through the job
    payload. The plumbing is here so that landing it is one line.
    """
    media_file_id = int(ctx.payload["media_file_id"])
    media_file = await ctx.session.get(MediaFile, media_file_id)
    if media_file is None:
        ctx.log.info("media file went away before it was matched", extra={"id": media_file_id})
        return
    if media_file.review_state in DECIDED:
        ctx.log.info(
            "media file already resolved by a person",
            extra={"media_file_id": media_file_id, "review_state": media_file.review_state.value},
        )
        return

    parsed = _parsed_of(media_file)

    if parsed.kind == "nc":
        _ignore(media_file, REASON_NC)
        ctx.log.info("creditless file ignored", extra={"media_file_id": media_file_id})
        return
    if parsed.kind == "batch":
        _review(media_file, MatchResult(), REASON_BATCH)
        ctx.log.info("batch file sent to review", extra={"media_file_id": media_file_id})
        return

    raw_expected = ctx.payload.get("expected")
    expected: tuple[int, int] | None = None
    if isinstance(raw_expected, list | tuple) and len(raw_expected) == 2:
        expected = (int(raw_expected[0]), int(raw_expected[1]))

    async with catalog_for(ctx.settings) as catalog:
        result = await match(
            ctx.session,
            catalog,
            parsed,
            expected=expected,
            minimum=ctx.settings.match_min_candidate,
        )

    best = result.best
    if best is None:
        _review(media_file, result, REASON_NO_CANDIDATES)
        ctx.log.info("no candidates for media file", extra={"media_file_id": media_file_id})
        return
    if best.episode_number is None:
        _review(media_file, result, REASON_NO_EPISODE)
        ctx.log.info("no episode number for media file", extra={"media_file_id": media_file_id})
        return
    if not result.auto_links(
        ctx.settings.match_auto_threshold, min_title=ctx.settings.match_min_title_for_auto
    ):
        # Two bars, two reasons: "I am not sure enough" and "the name is not
        # close enough" are different things to tell a person, and the second
        # is the one whose top candidate is usually a sibling show.
        weak_title = result.confidence >= ctx.settings.match_auto_threshold
        _review(media_file, result, REASON_WEAK_TITLE if weak_title else REASON_LOW_CONFIDENCE)
        ctx.log.info(
            "media file sent to review",
            extra={
                "media_file_id": media_file_id,
                "confidence": result.confidence,
                "threshold": ctx.settings.match_auto_threshold,
                "title": best.title,
                "min_title": ctx.settings.match_min_title_for_auto,
                "best_anime_id": best.anime_id,
            },
        )
        return

    try:
        await link(
            ctx.session,
            media_file,
            anime_id=best.anime_id,
            episode_number=best.episode_number,
            review_state=ReviewState.AUTO,
            confidence=result.confidence,
            candidates=result.top(),
        )
    except LinkError as exc:
        # The candidate named a show Arc cannot link to. That is a review
        # item, not a failed job: the file is fine and a person can pick a
        # different title.
        _review(media_file, result, str(exc))
        ctx.log.warning(
            "could not link a confident match",
            extra={"media_file_id": media_file_id, "error": str(exc)},
        )


__all__ = [
    "DECIDED",
    "LIBRARY_SCAN",
    "MATCH_FILE",
    "REASON_BATCH",
    "REASON_LOW_CONFIDENCE",
    "REASON_NC",
    "REASON_NO_CANDIDATES",
    "REASON_NO_EPISODE",
    "REASON_WEAK_TITLE",
    "library_scan",
    "match_file",
]
