"""The player's three endpoints (FR-S2, FR-S3, FR-S4, FR-W3).

* ``GET  /api/episodes/{id}/play`` — everything the player opens with.
* ``POST /api/progress`` — where the user has got to, every ten seconds.
* ``POST``/``DELETE /api/episodes/{id}/watched`` — the manual mark and its undo.
* ``GET  /api/progress/completed`` — which of some episodes the caller has completed.

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
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import Field, ValidationError
from sqlalchemy import select

from arc.api.anime_schemas import AnimeSummary, EpisodeOut
from arc.api.deps import MAX_ID, MIN_ID, CurrentUser, EpisodeId, SessionDep, SettingsDep
from arc.api.episode_extras import episode_extras, renditions_for
from arc.api.media_stream import playlist_url
from arc.api.playback_schemas import (
    MAX_COMPLETED_IDS,
    CompletedEpisode,
    CompletedOut,
    EpisodeRef,
    LiveOut,
    NextSeasonRef,
    PendingOut,
    PlayInfo,
    ProgressIn,
    ProgressOut,
    SubtitlesOut,
)
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    Rendition,
    Want,
    WatchProgress,
)
from arc.services.acquisition.wants import coverable
from arc.services.catalog import episodes_for
from arc.services.catalog.airing import aired_through, out_of_order
from arc.services.media.live_serve import LiveServed, live_servable
from arc.services.media.names import (
    KEY_DOWNLOAD_PROGRESS,
    KEY_EXPECTED_DURATION,
    KEY_SEEDERS,
    latest_live_with,
    payload_float,
)
from arc.services.playback.next_season import next_season
from arc.services.playback.progress import (
    ProgressOutcome,
    record_progress,
    resume_position,
    unmark_watched,
)
from arc.services.trips.rules import holds_trip_episode

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
    episodes: list[Episode], episode: Episode, *, fetchable: Collection[int] = frozenset()
) -> tuple[EpisodeRef | None, EpisodeRef | None]:
    """The rows either side of ``episode`` in number order (FR-S5).

    By position in the list rather than by ``number ± 1``: episode numbering
    has gaps — recaps, a 5.5, a season that starts at 13 — and "the next row"
    is what a person means by the next episode. ``fetchable`` is the caller's
    on-demand set (FR-A14), for :attr:`EpisodeRef.fetchable`.
    """
    numbers = [row.number for row in episodes]
    try:
        index = numbers.index(episode.number)
    except ValueError:  # pragma: no cover - the episode came from this list
        return None, None

    def ref(row: Episode) -> EpisodeRef:
        return EpisodeRef.from_episode(
            row, fetchable=row.state is not EpisodeState.READY and row.id in fetchable
        )

    previous = ref(episodes[index - 1]) if index > 0 else None
    following = ref(episodes[index + 1]) if index + 1 < len(episodes) else None
    return previous, following


async def _live_want(session: SessionDep, user_id: int, episode_id: int) -> Want | None:
    """The caller's live want on this episode, if any (FR-A14's pending answer)."""
    found = await session.execute(
        select(Want).where(
            Want.user_id == user_id, Want.episode_id == episode_id, Want.dropped_at.is_(None)
        )
    )
    return found.scalar_one_or_none()


@router.get(
    "/api/episodes/{episode_id}/play",
    response_model=PlayInfo,
    summary="Everything the player needs to open one episode (FR-S1, FR-S2)",
    responses={404: {"description": NOT_PLAYABLE}},
)
async def play(
    episode_id: EpisodeId, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> PlayInfo:
    """404 unless the episode is ``ready``, the caller's trip episode, or wanted by the caller.

    "Not ready" and "no such episode" are one answer on purpose: an episode
    that nobody asked for has nothing to play, and the show page is where a
    user learns why — this endpoint's job is to open a player or not.

    The first exception is a trip episode (FR-A12, M19 T4): an episode that is
    not ready but on which the caller holds a ``pending`` or ``delivered``
    trip row lives on their device. It is answered with ``offline_only`` and
    no ``playlist_url``, the resume position and the neighbours as usual, so
    the player can open the device's copy.

    The second is play on demand (FR-A14, M20): an episode that is not ready
    but on which the caller has a live want — the window's, or one they made
    by pressing play (``POST /api/episodes/{id}/demand``) — is answered 200
    with ``pending`` (state, download progress, seeders) and no
    ``playlist_url``. The player polls until the URL arrives. Everyone else
    keeps the 404.

    A demanded want outranks the trip branch: an episode the caller pressed
    play on is answered ``pending`` even when a device of theirs holds its trip
    copy (the "watch it on the Mac" case).

    ``next.fetchable`` says whether the caller could demand the next episode
    **now**, on their real progress — exactly what ``demand`` would accept, so
    the client never offers a fetch it would be refused. Before the halfway
    mark the next one is usually not yet covered; the player re-reads this
    route, so it flips once the user passes 50 %. It is false for a ready
    neighbour (nothing to fetch) and is not worked out while this episode is
    itself pending.

    ``next_season`` (FR-S5 item 3, owner 2026-10-18) is the sequel's first
    episode when the season ends here — no following row and the show finished
    — and the sequel is a show Arc has; with the viewer's list state for it and
    a ``fetchable`` that holds once the press has touched that entry. The rule
    is :mod:`arc.services.playback.next_season`.
    """
    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_PLAYABLE)
    not_ready = episode.state is not EpisodeState.READY
    offline_only = False
    waiting = False
    if not_ready:
        want = await _live_want(session, user.id, episode.id)
        if want is not None and want.demanded_at is not None:
            # The user pressed play on this one (FR-A14) — typically on the Mac
            # while the iPad holds the trip copy — so the wait is the answer,
            # not "offline only".
            waiting = True
        elif await holds_trip_episode(session, user.id, episode.id):
            offline_only = True
        elif want is not None:
            waiting = True
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_PLAYABLE)

    anime = await session.get(Anime, episode.anime_id)
    if anime is None:  # pragma: no cover - a foreign key guarantees this
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_PLAYABLE)

    siblings = await episodes_for(session, episode.anime_id)
    extras = await episode_extras(
        session, [episode.id], offline_codec=settings.offline_codec, offline_demo=user.is_demo
    )
    rendition = extras.renditions.get(episode.id)
    progress = await session.get(WatchProgress, (user.id, episode.id))
    # FR-W5's other half: an episode at or below the caller's list progress is
    # watched whether or not Arc has a completion row for it. One primary-key
    # read, on a route that already does four.
    entry = await session.get(ListEntry, (user.id, episode.anime_id))
    duration = rendition.duration if rendition is not None and rendition.duration else 0.0
    if not_ready and progress is not None and progress.duration_s:
        # No rendition to measure: the length a player last reported.
        duration = progress.duration_s

    at = now()
    previous, following = _neighbours(siblings, episode)
    # ``fetchable`` costs a one-user plan, and the pending player polls this
    # route every few seconds: so it is worked out only when a neighbour exists
    # that is not ready, and not while this episode is itself still pending.
    # Asked for at most once, by the neighbours or by the next season.
    covered: frozenset[int] | None = None

    async def covered_now() -> frozenset[int]:
        nonlocal covered
        if waiting:
            return frozenset()
        if covered is None:
            covered = await coverable(session, user.id, now=at)
        return covered

    if not waiting and any(ref is not None and not ref.ready for ref in (previous, following)):
        previous, following = _neighbours(siblings, episode, fetchable=await covered_now())
    # The sequel's first episode, at a finished season's end (FR-S5 item 3).
    # Not while this episode is pending or playing from its download (FR-P8):
    # that player polls this route, so a last episode watched while it still
    # downloads gets the offer only once the poll stops (it is ready then).
    sequel = (
        None
        if waiting
        else await next_season(
            session,
            user_id=user.id,
            anime=anime,
            episode=episode,
            has_following=following is not None,
            covered=covered_now,
            now=at,
        )
    )
    pending: PendingOut | None = None
    live: LiveOut | None = None
    subtitles = None if not_ready else SubtitlesOut.from_rendition(rendition)
    if waiting:
        # Playing while downloading (FR-P8, M20 T6): the same playlist URL,
        # served from the live directory, in place of the pending wait.
        found = await live_servable(session, settings, episode)
        if found is not None:
            # During an idle restart the newest job has not probed yet: the
            # facts come from the newest one that did.
            probed = await latest_live_with(session, episode.id, KEY_EXPECTED_DURATION)
            live = _live_out(found, probed)
            if live.expected_duration > 0:
                duration = live.expected_duration
            language: Any = ((probed or found.job).payload or {}).get("subtitle_lang")
            if isinstance(language, str) and language:
                subtitles = SubtitlesOut(lang=language)
    if waiting and live is None:
        release = extras.torrents.get(episode.id)
        pending = PendingOut(
            state=episode.state,
            progress=release.progress if release is not None else None,
            seeders=release.torrent.seeders if release is not None else None,
        )
    return PlayInfo(
        episode=EpisodeOut.from_episode(
            episode,
            now=at,
            anime_status=anime.status,
            boundary=aired_through(
                siblings, now=at, anime_status=anime.status, next_airing=anime.next_airing
            ),
            out_of_order=episode.number in out_of_order(siblings),
            completed=progress is not None and progress.completed,
            list_progress=entry.progress if entry is not None else 0,
            release=extras.torrents.get(episode.id),
            rendition=rendition,
            transcode_job=extras.transcode_jobs.get(episode.id),
            offline=extras.offline.out(episode),
            trip_only=episode.id in extras.trip_only,
            min_seeders_warn=extras.min_seeders_warn,
            live_playable=live is not None,
        ),
        anime=AnimeSummary.from_anime(anime),
        playlist_url=playlist_url(episode.id) if not not_ready or live is not None else None,
        offline_only=offline_only,
        pending=pending,
        live=live,
        duration=duration,
        resume_position=resume_position(progress, duration or None),
        resume_at=progress.updated_at if progress is not None else None,
        previous=previous,
        next=following,
        next_season=NextSeasonRef.build(sequel) if sequel is not None else None,
        subtitles=subtitles,
    )


def _live_out(served: LiveServed, probed: Job | None) -> LiveOut:
    """The live numbers ``/play`` reports.

    How far it plays and whether it has ended come from the playlist being
    served; the download's numbers from the newest job's payload (else the
    probed one's); the expected length from the newest job that probed.
    """
    numbers = served.job if served.job.payload.get(KEY_DOWNLOAD_PROGRESS) is not None else probed
    raw_seeders: Any = ((numbers.payload if numbers is not None else None) or {}).get(KEY_SEEDERS)
    progress = payload_float(numbers, KEY_DOWNLOAD_PROGRESS) or 0.0
    return LiveOut(
        front_seconds=round(served.playlist.front_seconds, 3),
        expected_duration=payload_float(probed, KEY_EXPECTED_DURATION) or 0.0,
        download_progress=min(max(progress, 0.0), 1.0),
        seeders=raw_seeders
        if isinstance(raw_seeders, int) and not isinstance(raw_seeders, bool)
        else None,
        ended=served.playlist.ended,
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
    """Clear the flag, keep the position, and lower the list by one if it stood here.

    The one path on which Arc lowers MyAnimeList's progress, and it does so
    only because somebody pressed it (FR-S4 as revised by the owner on
    2026-09-13; the rules and the reasoning are in
    :func:`~arc.services.playback.progress.unmark_watched`). ``list_progress``
    comes back when the number moved, so the client can render the new one
    without waiting for the refetch.

    Idempotent: un-marking an episode that was never marked is a 200 saying it
    is not completed, because that is a true description of the state the
    caller asked for. It is *not* a no-op in that case — an episode the list
    vouches for with no completion row of its own is exactly the mark this
    route exists to take back.
    """
    episode = await _episode(session, episode_id)
    outcome = await unmark_watched(session, user_id=user.id, episode=episode, now=now())
    await session.commit()
    if outcome.list_progress is not None:
        log.info(
            "watched mark taken back",
            extra={
                "user_id": user.id,
                "episode_id": episode.id,
                "list_progress": outcome.list_progress,
            },
        )
    return ProgressOut(completed=False, newly_completed=False, list_progress=outcome.list_progress)


@router.get(
    "/api/progress/completed",
    response_model=CompletedOut,
    summary="Which of these episodes the caller has completed (FR-S9 item 9)",
)
async def completed_episodes(
    user: CurrentUser,
    session: SessionDep,
    episode_ids: Annotated[
        list[Annotated[int, Field(ge=MIN_ID, le=MAX_ID)]],
        Query(min_length=1, max_length=MAX_COMPLETED_IDS),
    ],
) -> CompletedOut:
    """The caller's own completion rows among ``episode_ids``, and when each was made.

    A read and nothing else: no row is written, no list entry touched. An id
    the caller has no completed row for (or that names no episode) is simply
    absent. The device marks its downloaded copies watched from this answer
    when a completion was never announced to it live (FR-S9 item 9).
    """
    rows = await session.execute(
        select(WatchProgress.episode_id, WatchProgress.completed_at, WatchProgress.updated_at)
        .where(
            WatchProgress.user_id == user.id,
            WatchProgress.episode_id.in_(set(episode_ids)),
            WatchProgress.completed.is_(True),
        )
        .order_by(WatchProgress.episode_id)
    )
    return CompletedOut(
        completed=[
            CompletedEpisode(episode_id=episode_id, completed_at=completed_at or updated_at)
            for episode_id, completed_at, updated_at in rows.all()
        ]
    )


__all__ = ["BAD_JSON", "EPISODE_NOT_FOUND", "NOT_PLAYABLE", "now", "router"]
