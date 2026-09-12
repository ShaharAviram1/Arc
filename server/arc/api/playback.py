"""The player's three endpoints (FR-S2, FR-S3, FR-S4, FR-W3).

* ``GET  /api/episodes/{id}/play`` — everything the player opens with.
* ``POST /api/progress`` — where the user has got to, every ten seconds.
* ``POST``/``DELETE /api/episodes/{id}/watched`` — the manual mark and its undo.

Thin, as routers here are: the rules — what "watched" means, what it costs a
list entry, which episodes are worth resuming — all live in
:mod:`arc.services.playback.progress`, and this module turns them into status
codes and JSON.

**The progress body may not arrive as JSON.** The last report of a session is
sent on ``pagehide`` with ``navigator.sendBeacon``, which is the only way to
get a request out of a page that is closing — and a beacon sends whatever
content type the ``Blob`` carried, in practice ``text/plain``, because a
JSON one would make it a preflighted request that a closing page never
completes. So the body is read raw and parsed as JSON whatever the header
says.

**What that costs, stated plainly.** Accepting ``text/plain`` is what makes
``POST /api/progress`` a *CORS-simple* request: a cross-site page can send it
with no preflight, so the browser will not ask this server for permission
first and will not report the answer either — but the request still arrives,
and it still carries the session cookie unless something stops it. Two things
do, and they are the only two:

* :class:`arc.api.csrf.OriginCheckMiddleware`, which refuses any unsafe
  ``/api/`` request that does not carry an ``Origin`` (or ``Referer``) Arc
  serves. Browsers send ``Origin`` on beacons and on every cross-origin POST,
  so a forged one is refused with 403 while Arc's own client is unaffected.
* ``SameSite=Lax`` on the session cookie, which keeps the cookie off a
  cross-site POST in the first place.

Neither is defence in depth by accident: this route has no CSRF token and no
preflight to hide behind, so **it must never be exempted from the Origin
check** — not for beacons, not for "the browser is closing", not for a
convenience in a test. An exemption here is a cross-site write to a user's
watch history and, through the list advance below, to their MyAnimeList
progress.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from arc.api.anime_schemas import AnimeSummary, EpisodeOut
from arc.api.deps import CurrentUser, EpisodeId, SessionDep
from arc.api.episode_extras import episode_extras, renditions_for
from arc.api.media_stream import playlist_url
from arc.api.playback_schemas import EpisodeRef, PlayInfo, ProgressIn, ProgressOut
from arc.models import Anime, Episode, EpisodeState, Rendition, WatchProgress
from arc.services.catalog import episodes_for
from arc.services.catalog.airing import aired_through, out_of_order
from arc.services.playback.progress import (
    ProgressOutcome,
    record_progress,
    resume_position,
    unmark_watched,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["playback"])

EPISODE_NOT_FOUND = "episode not found"
NOT_PLAYABLE = "this episode is not ready to play"
BAD_JSON = "body is not valid JSON"


def now() -> datetime:
    """The clock ``aired`` and the completion timestamp are measured against.

    A function so a test can pin it, the same way :mod:`arc.api.home` does.
    """
    return datetime.now(UTC)


async def _episode(session: SessionDep, episode_id: int) -> Episode:
    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=EPISODE_NOT_FOUND)
    return episode


def _neighbours(
    episodes: list[Episode], episode: Episode
) -> tuple[EpisodeRef | None, EpisodeRef | None]:
    """The rows either side of ``episode`` in number order (FR-S5).

    By position in the list rather than by ``number ± 1``: episode numbering
    has gaps — recaps, a 5.5, a season that starts at 13 — and "the next row"
    is what a person means by the next episode.
    """
    numbers = [row.number for row in episodes]
    try:
        index = numbers.index(episode.number)
    except ValueError:  # pragma: no cover - the episode came from this list
        return None, None
    previous = EpisodeRef.from_episode(episodes[index - 1]) if index > 0 else None
    following = EpisodeRef.from_episode(episodes[index + 1]) if index + 1 < len(episodes) else None
    return previous, following


@router.get(
    "/api/episodes/{episode_id}/play",
    response_model=PlayInfo,
    summary="Everything the player needs to open one episode (FR-S1, FR-S2)",
    responses={404: {"description": NOT_PLAYABLE}},
)
async def play(episode_id: EpisodeId, user: CurrentUser, session: SessionDep) -> PlayInfo:
    """404 unless the episode is ``ready``.

    "Not ready" and "no such episode" are one answer on purpose: an episode
    that is still downloading has nothing to play, and the show page is where
    a user learns why — this endpoint's job is to open a player or not.
    """
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.state is not EpisodeState.READY:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_PLAYABLE)

    anime = await session.get(Anime, episode.anime_id)
    if anime is None:  # pragma: no cover - a foreign key guarantees this
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_PLAYABLE)

    siblings = await episodes_for(session, episode.anime_id)
    extras = await episode_extras(session, [episode.id])
    rendition = extras.renditions.get(episode.id)
    progress = await session.get(WatchProgress, (user.id, episode.id))
    duration = rendition.duration if rendition is not None and rendition.duration else 0.0

    at = now()
    previous, following = _neighbours(siblings, episode)
    return PlayInfo(
        episode=EpisodeOut.from_episode(
            episode,
            now=at,
            anime_status=anime.status,
            boundary=aired_through(
                siblings, now=at, anime_status=anime.status, next_airing=anime.next_airing
            ),
            out_of_order=episode.number in out_of_order(siblings),
            watched=progress is not None and progress.completed,
            torrent=extras.torrents.get(episode.id),
            rendition=rendition,
            transcode_job=extras.transcode_jobs.get(episode.id),
        ),
        anime=AnimeSummary.from_anime(anime),
        playlist_url=playlist_url(episode.id),
        duration=duration,
        resume_position=resume_position(progress, duration or None),
        previous=previous,
        next=following,
    )


def _reject_constant(literal: str) -> Any:
    """Refuse the ``NaN``, ``Infinity`` and ``-Infinity`` literals.

    Python's :mod:`json` accepts all three by default and none of them is
    JSON: RFC 8259 has no way to write them, no browser produces them, and
    each poisons the fraction FR-S4 is computed from.
    """
    raise ValueError(f"{literal} is not a number JSON can carry")


def _finite_float(literal: str) -> float:
    """Every number in the body, refused if it does not survive the conversion.

    ``1e400`` is not one of the literals :func:`_reject_constant` sees — it is
    an ordinary decimal that happens to be larger than a double, so Python
    rounds it to ``inf`` on the way in. Catching it *here* rather than leaving
    it to the field's ``allow_inf_nan=False`` matters: a validation error
    carries the offending input, and FastAPI serialises that error with
    :func:`json.dumps`, which refuses to write ``inf`` — so the 422 would
    itself fail and the caller would get a 500 for a body Arc had correctly
    decided to reject.
    """
    value = float(literal)
    if value in (float("inf"), float("-inf")):
        raise ValueError(f"{literal} is out of range for a number")
    return value


async def _body(request: Request) -> Any:
    """The request body as JSON, whatever content type it claimed.

    ``navigator.sendBeacon`` cannot send ``application/json`` without turning
    the request into a preflighted one, and a page being closed does not live
    long enough to answer a preflight. So the header is ignored and the bytes
    are parsed; a body that is not JSON is a 422 in FastAPI's own shape rather
    than a 400 the client would have to special-case.
    """
    raw = await request.body()
    try:
        return json.loads(raw, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, UnicodeDecodeError) as exc:
        # ``json.JSONDecodeError`` is a ``ValueError``, and so is what
        # :func:`_reject_constant` raises; both mean the same thing to a
        # caller — this body is not a progress report.
        raise RequestValidationError(
            [{"type": "json_invalid", "loc": ("body",), "msg": BAD_JSON, "input": None}]
        ) from exc


@router.post(
    "/api/progress",
    response_model=ProgressOut,
    summary="Report where the player has got to (FR-S3, FR-S4)",
    responses={404: {"description": EPISODE_NOT_FOUND}},
)
async def report_progress(request: Request, user: CurrentUser, session: SessionDep) -> ProgressOut:
    """Upsert the row, and advance the list if this report finished it.

    The episode does not have to be ``ready``: retention may have swept the
    rendition out from under a player that is still open, and the last report
    of that session is exactly the one worth keeping.
    """
    payload = await _body(request)
    try:
        body = ProgressIn.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc

    episode = await _episode(session, body.episode_id)
    outcome = await record_progress(
        session,
        user_id=user.id,
        episode=episode,
        position_s=body.position_s,
        duration_s=body.duration_s,
        now=now(),
    )
    await session.commit()
    if outcome.newly_completed:
        log.info(
            "episode watched",
            extra={
                "user_id": user.id,
                "episode_id": episode.id,
                "list_progress": outcome.list_progress,
            },
        )
    return _out(outcome)


def _out(outcome: ProgressOutcome) -> ProgressOut:
    return ProgressOut(
        completed=outcome.completed,
        newly_completed=outcome.newly_completed,
        list_progress=outcome.list_progress,
    )


@router.post(
    "/api/episodes/{episode_id}/watched",
    response_model=ProgressOut,
    summary="Mark an episode watched by hand (FR-W3)",
    responses={404: {"description": EPISODE_NOT_FOUND}},
)
async def mark_watched(
    episode_id: EpisodeId, user: CurrentUser, session: SessionDep
) -> ProgressOut:
    """ "I watched this elsewhere", treated exactly as reaching 90 % is.

    Works on an episode Arc has never prepared — which is the point of the
    requirement — so there may be no duration to record. Such a row stores
    ``0``/``0``: the flag is what carries the meaning, and a fabricated
    duration would be a number the player might later try to seek inside.
    """
    episode = await _episode(session, episode_id)
    rendition: Rendition | None = (await renditions_for(session, [episode_id])).get(episode_id)
    duration = rendition.duration if rendition is not None and rendition.duration else 0.0
    outcome = await record_progress(
        session,
        user_id=user.id,
        episode=episode,
        position_s=duration,
        duration_s=duration,
        now=now(),
        force_complete=True,
    )
    await session.commit()
    log.info(
        "episode marked watched by hand",
        extra={
            "user_id": user.id,
            "episode_id": episode.id,
            "newly_completed": outcome.newly_completed,
        },
    )
    return _out(outcome)


@router.delete(
    "/api/episodes/{episode_id}/watched",
    response_model=ProgressOut,
    summary="Take back a watched mark (FR-W3)",
    responses={404: {"description": EPISODE_NOT_FOUND}},
)
async def unmark(episode_id: EpisodeId, user: CurrentUser, session: SessionDep) -> ProgressOut:
    """Clear the flag, keep the position, leave the list and MAL alone.

    Idempotent: un-marking an episode that was never marked is a 200 saying it
    is not completed, because that is a true description of the state the
    caller asked for.
    """
    episode = await _episode(session, episode_id)
    await unmark_watched(session, user_id=user.id, episode_id=episode.id, now=now())
    await session.commit()
    return ProgressOut(completed=False, newly_completed=False, list_progress=None)


__all__ = ["BAD_JSON", "EPISODE_NOT_FOUND", "NOT_PLAYABLE", "now", "router"]
