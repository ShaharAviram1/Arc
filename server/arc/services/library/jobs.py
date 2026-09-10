"""The three library handlers: ``library_scan``, ``match_file``,
``llm_suggest_match`` (FR-L1, FR-L3, FR-L5).

``library_scan`` walks the directories and creates rows;
``match_file`` decides what one row is. They are separate jobs on purpose: a
scan is filesystem work that must finish quickly and never block on a network,
and a match is a catalogue request that must be retried with backoff when
AniList is down. Putting them in one handler would mean a slow catalogue makes
new downloads invisible.

``llm_suggest_match`` is a third for the same reason twice over: it talks to a
third party with a daily quota, it is worth nothing to anybody until somebody
opens the review page, and — the point — it must not be able to affect the
match. It runs *after* the file is in the queue, writes one column nothing
else reads, and never links (FR-L5, and CLAUDE.md: suggestions are shown,
never applied).

All three are idempotent. A second scan finds no new paths; a second match
recomputes the same answer and writes the same columns; a second suggestion
sees the one already stored and stops. The one thing a re-match must not do is
undo a decision, so a row a human has already confirmed or ignored is left
alone (FR-L6) — and a row that is *linked* is never unlinked here either,
whatever the new answer is (see :func:`_review`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Anime, Episode, MediaFile, ReviewState
from arc.services.acquisition.reject import episode_id_of
from arc.services.catalog.factory import catalog_for
from arc.services.jobs.registry import JobContext, register
from arc.services.library import ingest, suggest
from arc.services.library.link import LinkError, link
from arc.services.library.matcher import MatchResult, match
from arc.services.library.names import (
    LIBRARY_SCAN,
    LLM_SUGGEST_MATCH,
    MATCH_FILE,
    enqueue_suggestion,
)
from arc.services.library.parser import ParsedName, parse
from arc.services.recs.base import RecsFailed, RecsRefused, RecsUnavailable
from arc.services.recs.factory import shared_model

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


async def _review(ctx: JobContext, media_file: MediaFile, result: MatchResult, reason: str) -> None:
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

    **This is also the only place a suggestion is asked for automatically**
    (FR-L5). It has to be here rather than at the end of ``match_file``,
    because "the file went to the queue" is precisely the condition — an
    auto-linked file has nothing to suggest and an ignored one has nobody to
    suggest to — and there are five ways to reach the queue. The enqueue is
    flushed into the handler's own transaction, so a file that reaches the
    queue and a job that will explain it land together or not at all.

    …**and only when there is a shortlist to choose from**. A batch file and a
    "no good candidates" item both reach the queue with an empty
    :meth:`~arc.services.library.matcher.MatchResult.top`, and the question
    "which of these" has no meaning for either; the job would spend a queue
    slot to store an error. A person can still ask by hand from the review
    page, which is the right place for "try anyway".
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
    shortlist = result.top()
    media_file.review_state = ReviewState.PENDING
    media_file.match_confidence = result.confidence
    media_file.match_candidates = [*shortlist, {"reason": reason}]

    if shortlist and suggest.suggestions_enabled(ctx.settings):
        job = await enqueue_suggestion(ctx.session, media_file.id)
        ctx.log.info(
            "match suggestion queued",
            extra={"media_file_id": media_file.id, "job_id": job.id},
        )


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
        await _review(ctx, media_file, MatchResult(), REASON_BATCH)
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
        await _review(ctx, media_file, result, REASON_NO_CANDIDATES)
        ctx.log.info("no candidates for media file", extra={"media_file_id": media_file_id})
        return
    if best.episode_number is None:
        await _review(ctx, media_file, result, REASON_NO_EPISODE)
        ctx.log.info("no episode number for media file", extra={"media_file_id": media_file_id})
        return
    if not result.auto_links(
        ctx.settings.match_auto_threshold, min_title=ctx.settings.match_min_title_for_auto
    ):
        # Two bars, two reasons: "I am not sure enough" and "the name is not
        # close enough" are different things to tell a person, and the second
        # is the one whose top candidate is usually a sibling show.
        weak_title = result.confidence >= ctx.settings.match_auto_threshold
        await _review(
            ctx, media_file, result, REASON_WEAK_TITLE if weak_title else REASON_LOW_CONFIDENCE
        )
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
        await _review(ctx, media_file, result, str(exc))
        ctx.log.warning(
            "could not link a confident match",
            extra={"media_file_id": media_file_id, "error": str(exc)},
        )


# --- Match suggestions (FR-L5) ----------------------------------------------


#: Stored in ``llm_suggestion.error`` in place of an answer. Each is a
#: sentence the review page renders as "no suggestion: …", so each has to be
#: readable by whoever is looking at the queue rather than at the log.
SUGGEST_NOT_CONFIGURED = "not configured"
SUGGEST_NO_CANDIDATES = "the matcher found no candidates to choose between"
SUGGEST_REFUSED = "the model declined this request"


def _now() -> datetime:
    """The clock a suggestion is stamped with. A function so a test can pin it."""
    return datetime.now(UTC)


#: The key an answer always has and an error never does, so "is there a real
#: suggestion here" is one lookup rather than a guess about which fields a
#: past version of this job happened to write.
ANSWER_MARKER = "confidence"


def has_answer(suggestion: dict[str, Any] | None) -> bool:
    """Whether ``llm_suggestion`` holds a real answer rather than a failure."""
    return bool(suggestion) and ANSWER_MARKER in (suggestion or {})


def _store_error(media_file: MediaFile, error: str, *, model: str = "") -> None:
    """Record why there is no *new* suggestion, without destroying an old one.

    A stored failure rather than a failed job, because these are all answers:
    the feature is off, there was nothing to choose from, the model declined,
    the model wrote something unusable. None of them gets better on a retry,
    and a ``failed`` row in the queue view for each would be noise an operator
    learns to ignore (FR-D3). Unavailability is the opposite and is raised —
    see :func:`llm_suggest_match`.

    **A failure never overwrites a good suggestion.** ``force`` exists so a
    person can ask again, and "ask again" that trades a usable answer for
    "the model declined" would make the button dangerous to press. So a
    failure that lands on a row which already has an answer goes into
    ``last_error`` beside it and the answer stands; only a row with nothing
    worth keeping is replaced outright. A later success drops ``last_error``
    with the rest of the old blob, which is right: it is a note about an
    attempt, not about the file.
    """
    entry = {
        "error": error,
        "model": model or None,
        "created_at": _now().isoformat(),
    }
    existing = media_file.llm_suggestion
    if has_answer(existing):
        media_file.llm_suggestion = {**(existing or {}), "last_error": entry}
    else:
        media_file.llm_suggestion = entry


async def _expected_episode(
    session: AsyncSession, media_file: MediaFile, settings: Settings
) -> suggest.ExpectedEpisode | None:
    """The episode Arc asked for, when Arc is what downloaded this file.

    Derived from the save path rather than from the job payload
    (:func:`arc.services.acquisition.reject.episode_id_of`: every download
    lands in ``downloads/<episode id>/`` and nothing else does), so the prior
    survives a suggestion asked for from the review page days later, where no
    payload could carry it. ``None`` for a manual drop, which claims nothing.
    """
    episode_id = episode_id_of(media_file.path, downloads_dir=settings.downloads_dir)
    if episode_id is None:
        return None
    episode = await session.get(Episode, episode_id)
    if episode is None:
        return None
    anime = await session.get(Anime, episode.anime_id)
    if anime is None:
        return None
    title = anime.title_romaji or anime.title_english or anime.title_native or ""
    return suggest.ExpectedEpisode(anime_id=anime.id, title=title, episode_number=episode.number)


async def _suggest_candidates(
    session: AsyncSession, media_file: MediaFile
) -> list[suggest.SuggestCandidate]:
    """The stored candidates, resolved to catalogue rows, in stored order.

    The shortlist the *matcher* produced, not a fresh search: the question put
    to the model is "which of these", and re-deriving it would be asking about
    a file the review page is not showing. Entries with no ``anime_id`` (the
    trailing reason sentence) and ids whose row has since gone are dropped.
    """
    raw = [
        blob
        for blob in (media_file.match_candidates or [])
        if isinstance(blob, dict) and blob.get("anime_id") is not None
    ][: suggest.MAX_CANDIDATES]
    if not raw:
        return []
    wanted = {int(blob["anime_id"]) for blob in raw}
    found = await session.scalars(select(Anime).where(Anime.id.in_(wanted)))
    rows = {row.id: row for row in found.all()}

    candidates: list[suggest.SuggestCandidate] = []
    for blob in raw:
        row = rows.get(int(blob["anime_id"]))
        if row is None:
            continue
        score: Any = blob.get("score")
        candidates.append(
            suggest.SuggestCandidate(
                anime_id=row.id,
                romaji=row.title_romaji,
                english=row.title_english,
                format=row.format,
                episodes=row.episodes,
                season=row.season,
                season_year=row.season_year,
                score=float(score) if isinstance(score, int | float) else None,
                reasons=tuple(str(item) for item in (blob.get("reasons") or [])),
            )
        )
    return candidates


@register(LLM_SUGGEST_MATCH)
async def llm_suggest_match(ctx: JobContext) -> None:
    """Ask a model which candidate an unsure file is, and store the answer.

    **It never links.** There is no call to
    :func:`arc.services.library.link.link` in this function and there must
    never be one: FR-L5 says the proposal is shown in the review queue and
    never auto-applied, and the only write here is to
    ``media_files.llm_suggestion``.

    Idempotent three ways, all of them "the answer is already known or no
    longer wanted":

    * the row is gone, or a person has confirmed, ignored or auto-linked it
      since the job was queued — nothing to suggest about;
    * it already has a suggestion, and the payload did not say ``force`` —
      re-asking would spend a request from a daily quota to produce the same
      column;
    * the feature is off or no provider is configured — recorded as an error
      so the page can say why rather than showing an empty space.

    Only :class:`RecsUnavailable` escapes, so the queue's retry-with-backoff
    is what handles a provider that is down. A refusal, an unusable answer and
    an empty shortlist are all *stored*: retrying them is asking the same
    question again.
    """
    media_file_id = int(ctx.payload["media_file_id"])
    force = bool(ctx.payload.get("force"))

    media_file = await ctx.session.get(MediaFile, media_file_id)
    if media_file is None:
        ctx.log.info("media file went away before it was suggested", extra={"id": media_file_id})
        return
    if media_file.review_state is not ReviewState.PENDING:
        ctx.log.info(
            "media file is no longer waiting for review; no suggestion needed",
            extra={"media_file_id": media_file_id, "review_state": media_file.review_state.value},
        )
        return
    if media_file.llm_suggestion is not None and not force:
        ctx.log.info("media file already has a suggestion", extra={"media_file_id": media_file_id})
        return
    if not suggest.suggestions_enabled(ctx.settings):
        _store_error(media_file, SUGGEST_NOT_CONFIGURED)
        ctx.log.info("match suggestions are not configured", extra={"media_file_id": media_file_id})
        return

    candidates = await _suggest_candidates(ctx.session, media_file)
    if not candidates:
        _store_error(media_file, SUGGEST_NO_CANDIDATES)
        ctx.log.info(
            "nothing to suggest between for media file", extra={"media_file_id": media_file_id}
        )
        return

    message = suggest.build_user_message(
        _parsed_of(media_file),
        candidates,
        expected=await _expected_episode(ctx.session, media_file, ctx.settings),
    )

    # The process's chain, not one built for this job: it carries the
    # daily-quota cooldowns, and a queue of thirty review files must not
    # rediscover a spent model thirty times (arc/services/recs/factory.py).
    model = shared_model(ctx.settings)
    if model is None:  # pragma: no cover - suggestions_enabled already said otherwise
        _store_error(media_file, SUGGEST_NOT_CONFIGURED)
        return
    try:
        answer = await model.complete(
            system=suggest.SYSTEM_PROMPT,
            user=message,
            schema=suggest.SUGGESTION_SCHEMA,
            name=suggest.SCHEMA_NAME,
        )
    except RecsUnavailable:
        # The one outcome worth another attempt, and the queue already knows
        # how to space them out. Raising also rolls the session back, so
        # nothing half-written is left on the row.
        ctx.log.warning(
            "no model could answer a match suggestion",
            extra={"media_file_id": media_file_id},
        )
        raise
    except RecsRefused as exc:
        _store_error(media_file, SUGGEST_REFUSED)
        ctx.log.warning(
            "a match suggestion was declined",
            extra={"media_file_id": media_file_id, "stop_details": str(exc.stop_details)},
        )
        return
    except RecsFailed as exc:
        _store_error(media_file, str(exc)[: suggest.MAX_REASON_CHARS])
        ctx.log.warning(
            "a match suggestion could not be used",
            extra={"media_file_id": media_file_id, "error": str(exc)[:200]},
        )
        return

    try:
        suggestion = suggest.validate(answer.data, candidates)
    except suggest.SuggestionInvalid as exc:
        _store_error(media_file, str(exc)[: suggest.MAX_REASON_CHARS], model=answer.model)
        ctx.log.warning(
            "a match suggestion was not shaped like an answer",
            extra={"media_file_id": media_file_id, "error": str(exc)[:200]},
        )
        return

    media_file.llm_suggestion = {
        **suggestion.as_dict(),
        "model": answer.model or None,
        "provider": answer.provider or None,
        "created_at": _now().isoformat(),
    }
    ctx.log.info(
        "match suggestion stored",
        extra={
            "media_file_id": media_file_id,
            "anime_id": suggestion.anime_id,
            "episode_number": suggestion.episode_number,
            "confidence": suggestion.confidence,
            "model": answer.model,
            "provider": answer.provider,
        },
    )


__all__ = [
    "DECIDED",
    "LIBRARY_SCAN",
    "LLM_SUGGEST_MATCH",
    "MATCH_FILE",
    "REASON_BATCH",
    "REASON_LOW_CONFIDENCE",
    "REASON_NC",
    "REASON_NO_CANDIDATES",
    "REASON_NO_EPISODE",
    "REASON_WEAK_TITLE",
    "SUGGEST_NOT_CONFIGURED",
    "SUGGEST_NO_CANDIDATES",
    "SUGGEST_REFUSED",
    "library_scan",
    "llm_suggest_match",
    "match_file",
]
