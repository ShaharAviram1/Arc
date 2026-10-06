"""The small offline copy: the rules the request path needs (FR-P6, §5.3b).

Handler-free, like :mod:`arc.services.media.names`, because the API asks every
question here — *may this user fetch this copy?*, *what does the show page say
about it?*, *queue one* — and must not import ffmpeg's runner or the job
registry to ask. The encode itself is :mod:`arc.services.media.offline`.

**What a copy is.** One MP4 per episode at ``DATA_DIR/offline/<id>.mp4``,
720p at most, H.264 by default, the rendition's own subtitle and audio choice
burned in, ``moov`` first. A device that keeps an episode offline downloads it
instead of the full-size ``episode.mp4`` (FR-S7), which stays as the fallback
for when the source a copy would be made from has already gone.

**The six states a client sees** (:data:`OfflineState`), and only on a
``ready`` episode — anything else has no copy to offer (M19 T3/T4 widen that
for a trip's episodes):

* ``none`` — no copy yet, and the source is still here to make one from;
* ``queued`` / ``preparing`` — asked for; ``preparing`` carries a percentage;
* ``available`` — a validated file is waiting at ``url``;
* ``failed`` — the last encode did not produce one, and it can be asked again;
* ``unavailable`` — no copy, and no source left to make one from: the device
  takes the full-size download instead.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    User,
)
from arc.services.events import offline_copy_event, publish
from arc.services.jobs.queue import ACTIVE_STATUSES
from arc.services.media.download import stat_etag
from arc.services.media.names import (
    EPISODE_KEY,
    OFFLINE_ENCODE,
    OFFLINE_WHY_REQUEST,
    USER_KEY,
    WHY_KEY,
    enqueue_offline_encode,
    latest_offline_jobs,
    offline_path_for,
)
from arc.services.media.plan import OfflineOptions
from arc.services.trips.rules import holds_trip_episode

log = logging.getLogger(__name__)

#: What a client is told about an episode's copy (module docstring).
type OfflineState = Literal["none", "queued", "preparing", "available", "failed", "unavailable"]

#: RFC 6381 codec strings, so a client can ask ``canPlayType`` *before* it
#: downloads a hundred megabytes it cannot play. H.264 High at level 4.0
#: (``-profile:v high -level 4.0``) is ``avc1.640028``; HEVC Main at level 3.1,
#: which is what x265 picks for 720p at anime frame rates, is
#: ``hvc1.1.6.L93.B0``. The video codec only: the audio is AAC-LC either way,
#: which every client Arc serves already plays.
CODEC_STRINGS: Final[dict[str, str]] = {
    "h264": "avc1.640028",
    "hevc": "hvc1.1.6.L93.B0",
}

#: The detail a ready episode whose source is gone answers ``POST`` with: the
#: device falls back to the full-size ``episode.mp4`` on it (FR-S7).
SOURCE_GONE: Final[str] = "source_gone"

#: The details a refused *new* request answers with. ``copy_queue_full`` (429)
#: when the caller already has :data:`MAX_USER_REQUEST_COPIES` copies queued or
#: being made, or the host has :data:`MAX_HOST_REQUEST_COPIES`; ``storage_held``
#: (409) while the data volume is under its floor (FR-T6). The device takes the
#: full-size file on either (FR-S7). A trip's copies (``why="trip"``) are not
#: counted here — a trip has its own cap.
COPY_QUEUE_FULL: Final[str] = "copy_queue_full"
STORAGE_HELD: Final[str] = "storage_held"
MAX_USER_REQUEST_COPIES: Final[int] = 10
MAX_HOST_REQUEST_COPIES: Final[int] = 30

#: How often ``last_served_at`` may be written. Hourly: the idle rule counts in
#: days, and a resuming download makes dozens of range requests a minute.
SERVED_TOUCH_SECONDS: Final[float] = 3600.0


class CopyUnavailable(LookupError):
    """There is no copy to make: the episode is unknown or not ``ready``."""


class SourceGone(RuntimeError):
    """The episode is ready but the source a copy would be made from is gone."""


class CopyQueueFull(RuntimeError):
    """Too many request copies are already queued (:data:`COPY_QUEUE_FULL`)."""


class StorageHeld(RuntimeError):
    """The data volume is under its floor; no new copy is started (FR-T6)."""


def offline_options(settings: Settings) -> OfflineOptions:
    """The ``OFFLINE_*`` settings, in the shape :func:`offline_encode_args` takes."""
    return OfflineOptions(
        codec=settings.offline_codec,
        height=settings.offline_height,
        crf=settings.offline_crf,
        preset=settings.offline_preset,
        audio_bitrate=settings.offline_audio_bitrate,
    )


def settings_key(options: OfflineOptions, *, sub_lang: str, audio_lang: str) -> str:
    """A short hash over everything that changes a copy's bytes.

    Stored on the row so that "was this copy made the way the host makes them
    now?" is a comparison rather than a guess. Recorded only: nothing re-makes
    a ready copy because the settings changed. The languages are in it because
    they choose the tracks; the preset is, because it changes the picture.
    """
    raw = "|".join(
        (
            options.codec,
            str(options.height),
            str(options.crf),
            options.preset,
            options.audio_bitrate,
            sub_lang,
            audio_lang,
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def codec_string(codec: str | None) -> str | None:
    """:data:`CODEC_STRINGS` for ``codec``, or ``None`` for one Arc never writes."""
    return CODEC_STRINGS.get(codec or "")


def offline_state(
    *, copy_state: OfflineCopyState | None, has_source: bool, job_alive: bool = True
) -> OfflineState:
    """What a client is told about a **ready** episode's copy. Pure.

    A ``failed`` copy with no source left is ``unavailable`` rather than
    ``failed``: asking again could only fail again, and the useful answer is
    "take the full-size file".

    ``job_alive`` is whether a pending or running ``offline_encode`` stands
    behind a ``queued``/``preparing`` row. Without one the encode is dead — it
    used up its attempts, or its job row went — and the row would otherwise
    read "preparing" for ever; it reads ``failed`` instead, and a later
    request queues it again.
    """
    if copy_state is OfflineCopyState.READY:
        return "available"
    in_flight = copy_state in (OfflineCopyState.QUEUED, OfflineCopyState.PREPARING)
    if in_flight and job_alive:
        return "queued" if copy_state is OfflineCopyState.QUEUED else "preparing"
    if not has_source:
        return "unavailable"
    if copy_state is OfflineCopyState.FAILED or in_flight:
        return "failed"
    return "none"


def job_progress(job: Job | None) -> float:
    """The 0..1 progress an ``offline_encode`` job has written, clamped."""
    raw = job.payload.get("progress") if job is not None else None
    value = float(raw) if isinstance(raw, int | float) else 0.0
    return min(max(value, 0.0), 1.0)


def file_matches(path: Path, copy: OfflineCopy) -> bool:
    """Whether the file on disk is the one the row describes.

    A plain file (never a link), of the row's size, answering the row's ETag.
    This is the encode's idempotency check: a ready row whose file still
    matches needs no second encode.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    return info.st_size == copy.size and stat_etag(info.st_size, info.st_mtime_ns) == copy.etag


async def newest_source(session: AsyncSession, episode_id: int) -> MediaFile | None:
    """The file a copy is made from: the newest one linked to the episode.

    The same choice the transcode makes, so the copy and the rendition come
    from one file.
    """
    found: MediaFile | None = await session.scalar(
        select(MediaFile)
        .where(MediaFile.episode_id == episode_id)
        .order_by(MediaFile.id.desc())
        .limit(1)
    )
    return found


async def source_on_disk(session: AsyncSession, episode_id: int) -> MediaFile | None:
    """:func:`newest_source`, but only if its file is actually there."""
    source = await newest_source(session, episode_id)
    if source is None or not Path(source.path).is_file():
        return None
    return source


async def episodes_with_sources(session: AsyncSession, episode_ids: list[int]) -> set[int]:
    """Which of ``episode_ids`` have a linked source row. One query.

    The row rather than the file, deliberately: this is asked for a whole page
    of episodes, and every way Arc removes a source removes its row with it.
    ``POST`` checks the file itself before it promises an encode.
    """
    if not episode_ids:
        return set()
    rows = await session.scalars(
        select(MediaFile.episode_id).where(MediaFile.episode_id.in_(set(episode_ids))).distinct()
    )
    return {episode_id for episode_id in rows.all() if episode_id is not None}


def _offline_job_key() -> ColumnElement[str]:
    return cast(ColumnElement[str], Job.payload[EPISODE_KEY].astext)


async def encoding_episode_ids(
    session: AsyncSession, episode_ids: list[int] | None = None
) -> set[int]:
    """Episodes with a pending or running ``offline_encode`` job.

    Retention asks this before it deletes a source (FR-P6): a copy being made
    is reading that file. A ``preparing`` row with **no** live job behind it is
    an encode whose worker died, and it does not hold the source — otherwise a
    crash could pin a file to the disk for ever. ``None`` asks about every
    episode.
    """
    key = _offline_job_key()
    statement = select(key).where(Job.type == OFFLINE_ENCODE, Job.status.in_(ACTIVE_STATUSES))
    if episode_ids is not None:
        if not episode_ids:
            return set()
        statement = statement.where(key.in_({str(episode_id) for episode_id in episode_ids}))
    found: set[int] = set()
    for raw in (await session.scalars(statement.distinct())).all():
        try:
            found.add(int(raw))
        except TypeError, ValueError:  # pragma: no cover - a hand-written row
            continue
    return found


async def may_fetch_copy(
    session: AsyncSession, user: User, episode: Episode, copy: OfflineCopy
) -> bool:
    """Whether ``user`` may download ``copy`` (FR-P6, FR-A12). The media route asks this.

    Only a ``ready`` copy, and never for the demo account (the route refuses
    it with a 403 before anything is looked up; this says no as well). Then:

    * the copy of a ``ready`` episode — any account;
    * the copy of an episode that is **not** ready (a trip's: its source is
      gone and it has no rendition) — only a user holding a ``pending`` or
      ``delivered`` trip row on it (:func:`~arc.services.trips.rules.
      holds_trip_episode`). ``delivered`` so a second device of the same user
      can fetch it in the hour before the settle deletes it. An ``expired``
      or ``cancelled`` row, another user, or no row at all is the route's
      ordinary 404 — never a 403, which would say the file exists.
    """
    if user.is_demo or copy.state is not OfflineCopyState.READY:
        return False
    if episode.state is EpisodeState.READY:
        return True
    return await holds_trip_episode(session, user.id, episode.id)


def publish_copy(session: AsyncSession, episode: Episode, copy: OfflineCopy) -> None:
    """Stage the live ``offline_copy`` event for this copy's current state."""
    publish(
        session,
        offline_copy_event(
            anime_id=episode.anime_id, episode_id=episode.id, state=copy.state.value
        ),
    )


@dataclass(frozen=True, slots=True)
class CopyRequest:
    """What :func:`request_copy` did: the row, and the live job behind it if any."""

    copy: OfflineCopy | None
    job: Job | None
    has_source: bool


async def ensure_copy_row(session: AsyncSession, episode_id: int) -> OfflineCopy:
    """The episode's ``offline_copies`` row, inserted ``queued`` if there is none.

    ``INSERT … ON CONFLICT DO NOTHING`` and a re-read rather than ``add()``:
    two requests (or a request and the encode) creating the row at once must
    both end up holding *the* row, not one of them a primary-key error.
    """
    await session.execute(
        insert(OfflineCopy)
        .values(episode_id=episode_id, state=OfflineCopyState.QUEUED)
        .on_conflict_do_nothing(index_elements=[OfflineCopy.episode_id])
    )
    copy = await session.get(OfflineCopy, episode_id, populate_existing=True)
    assert copy is not None  # inserted above, or already there
    return copy


async def _queue_is_full(session: AsyncSession, user_id: int | None) -> bool:
    """Whether a new *request* copy would exceed the per-user or the host cap.

    Counts live ``offline_encode`` jobs with ``why = request`` only: a trip's
    copies are bounded by the trip itself (M19 T3), and are not this cap's.
    """
    why = cast(ColumnElement[str], Job.payload[WHY_KEY].astext)
    live = select(func.count()).where(
        Job.type == OFFLINE_ENCODE,
        Job.status.in_(ACTIVE_STATUSES),
        why == OFFLINE_WHY_REQUEST,
    )
    host = int(await session.scalar(live) or 0)
    if host >= MAX_HOST_REQUEST_COPIES:
        return True
    if user_id is None:
        return False
    who = cast(ColumnElement[str], Job.payload[USER_KEY].astext)
    mine = int(await session.scalar(live.where(who == str(user_id))) or 0)
    return mine >= MAX_USER_REQUEST_COPIES


async def request_copy(
    session: AsyncSession,
    settings: Settings,
    episode_id: int,
    *,
    user_id: int | None = None,
    storage_held: Callable[[], Awaitable[bool]] | None = None,
) -> CopyRequest:
    """ "Keep offline" on a ready episode: queue its copy unless there is one.

    Flushed, not committed. Raises :class:`CopyUnavailable` for an unknown or
    not-ready episode and :class:`SourceGone` for a ready one whose source is no
    longer on disk and that has no copy on its way either. Idempotent: an
    available copy is returned as it is, a queued or preparing one keeps its
    live job, and a failed or dead one is queued again.

    Only a request that would start a **new** encode is refused for space or
    queue length: :class:`StorageHeld` while ``storage_held()`` says the volume
    is under its floor (FR-T6), :class:`CopyQueueFull` past the caps
    (:data:`MAX_USER_REQUEST_COPIES`, :data:`MAX_HOST_REQUEST_COPIES`).

    The episode row is locked ``FOR UPDATE`` for the rest of the transaction,
    so this and retention's :func:`~arc.services.retention.delete.
    delete_episode_files` (which takes the same lock) cannot interleave: a
    deletion either sees this request's live job and leaves the source alone,
    or finishes first and this request finds the episode no longer ``ready``.
    """
    episode = await session.get(Episode, episode_id, with_for_update=True, populate_existing=True)
    if episode is None or episode.state is not EpisodeState.READY:
        raise CopyUnavailable(episode_id)

    copy = await session.get(OfflineCopy, episode_id)
    if (
        copy is not None
        and copy.state is OfflineCopyState.READY
        and file_matches(offline_path_for(settings, episode_id), copy)
    ):
        return CopyRequest(copy=copy, job=None, has_source=True)

    live = (await latest_offline_jobs(session, [episode_id])).get(episode_id)
    source = await source_on_disk(session, episode_id)
    if source is None:
        if (
            live is not None
            and copy is not None
            and copy.state in (OfflineCopyState.QUEUED, OfflineCopyState.PREPARING)
        ):
            # Already on its way: the job will find the source gone and say so.
            return CopyRequest(copy=copy, job=live, has_source=False)
        raise SourceGone(episode_id)

    if live is None:
        if storage_held is not None and await storage_held():
            raise StorageHeld(episode_id)
        if await _queue_is_full(session, user_id):
            raise CopyQueueFull(episode_id)
    job = live or await enqueue_offline_encode(
        session, episode_id, why=OFFLINE_WHY_REQUEST, user_id=user_id
    )
    copy = await ensure_copy_row(session, episode_id)
    if live is None or copy.state not in (OfflineCopyState.QUEUED, OfflineCopyState.PREPARING):
        # A new row, a ready row whose file has gone, a failed one, or one whose
        # encode died and has just been queued afresh: all are queued. A row
        # with its live job behind it is left as it stands.
        copy.state = OfflineCopyState.QUEUED
        copy.error = None
    copy.media_file_id = source.id
    publish_copy(session, episode, copy)
    await session.flush()
    log.info(
        "offline copy requested",
        extra={
            "episode_id": episode_id,
            "job_id": job.id,
            "user_id": user_id,
            "state": copy.state.value,
        },
    )
    return CopyRequest(copy=copy, job=job, has_source=True)


#: On (owner, 2026-10-06; kept a constant so it can be turned off): touch a
#: **non-ready** (trip) copy's ``last_served_at`` every five minutes rather
#: than hourly, so the trip settle can tell a download still running
#: (:data:`~arc.services.trips.settle.SETTLE_WAITS_FOR_FETCH`).
TRIP_TOUCH_ENABLED: bool = True
TRIP_TOUCH_SECONDS: Final[float] = 300.0


def touch_interval(episode: Episode) -> float:
    """Seconds between two ``last_served_at`` writes for this episode's copy."""
    if TRIP_TOUCH_ENABLED and episode.state is not EpisodeState.READY:
        return TRIP_TOUCH_SECONDS
    return SERVED_TOUCH_SECONDS


def served_recently(
    copy: OfflineCopy, *, now: datetime | None = None, interval: float = SERVED_TOUCH_SECONDS
) -> bool:
    """Whether ``last_served_at`` is fresh enough not to be written again."""
    if copy.last_served_at is None:
        return False
    moment = now or datetime.now(UTC)
    return (moment - copy.last_served_at).total_seconds() < interval


async def copy_count(session: AsyncSession) -> int:
    """How many copy rows there are. For the admin view and tests."""
    return int(await session.scalar(select(func.count()).select_from(OfflineCopy)) or 0)


__all__ = [
    "CODEC_STRINGS",
    "COPY_QUEUE_FULL",
    "MAX_HOST_REQUEST_COPIES",
    "MAX_USER_REQUEST_COPIES",
    "SERVED_TOUCH_SECONDS",
    "SOURCE_GONE",
    "STORAGE_HELD",
    "TRIP_TOUCH_ENABLED",
    "TRIP_TOUCH_SECONDS",
    "CopyQueueFull",
    "CopyRequest",
    "CopyUnavailable",
    "OfflineState",
    "SourceGone",
    "StorageHeld",
    "codec_string",
    "copy_count",
    "encoding_episode_ids",
    "ensure_copy_row",
    "episodes_with_sources",
    "file_matches",
    "job_progress",
    "may_fetch_copy",
    "newest_source",
    "offline_options",
    "offline_state",
    "publish_copy",
    "request_copy",
    "served_recently",
    "settings_key",
    "source_on_disk",
    "touch_interval",
]
