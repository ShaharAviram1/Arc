"""The ``offline_encode`` handler: the small offline copy (FR-P6, §5.3b).

One MP4 per episode, ``DATA_DIR/offline/<episode id>.mp4``: 720p at most, H.264
(or HEVC, ``OFFLINE_CODEC``), CRF 26, AAC stereo 96k, ``moov`` first, and the
rendition's own subtitle and audio choice burned in — the same
:func:`~arc.services.media.plan.build_plan`, the same extracted fonts and
subtitle file, a different output (:func:`~arc.services.media.plan.
offline_encode_args`). It exists because a device that keeps an episode offline
should not have to hold the 1080p rendition to do it: a 24-minute episode is
about 100 MB here against 300–600 MB there.

It follows the transcode handler (:mod:`arc.services.media.jobs`) point for
point, and borrows its machinery rather than copying it:

* **One claim per episode**, by a transaction-level advisory lock on
  ``('offline_encode', episode_id)`` (:data:`OFFLINE_LOCK_KEY`). A copy and a
  rendition of one episode may be made side by side — they only *read* the
  source — but never two copies.
* **Idempotent.** A ``ready`` row whose file is still on disk at the row's size
  and ETag is done; a re-run writes "done" to its payload and returns.
* **Nothing half-written is visible.** The encode runs in
  ``offline/<id>.tmp-<job id>/`` and is validated there — one video stream of
  the expected codec (``hvc1``-tagged for HEVC), no taller than
  ``OFFLINE_HEIGHT``, a duration within :data:`DURATION_TOLERANCE` of the
  source's, ``moov`` before ``mdat`` — and only then ``os.replace``-d onto
  ``offline/<id>.mp4``. Every other exit removes the staging directory.
* **Progress lives in the job's payload**, written and heartbeated by the
  transcode's own reporter; the wait for an encoder slot keeps the lock warm
  the same way.
* **Failure is committed, then re-raised** so the runner backs off and
  retries, and the person who asked sees the reason in the meantime. The one
  exception is a source that has gone: no retry can bring it back, so the copy
  is marked ``failed`` and the job ends there.
* **The encoder cap is shared.** ``transcode`` and ``offline_encode`` take the
  same semaphore and count against one ``MAX_TRANSCODES`` in the claim loop
  (:func:`arc.services.jobs.loop.process_caps`); a pending transcode always
  sorts in front of a pending copy (:mod:`arc.services.media.names`).

Two seams are left for the trips that follow (M19 T3/T4): the payload's
``why`` (``request`` or ``trip``), and :func:`on_copy_ready`, the single place
that reacts to a copy becoming ready.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.core.text import keep_head_and_tail
from arc.models import Episode, EpisodeState, OfflineCopy, OfflineCopyState, Rendition
from arc.services.jobs.registry import JobContext, register
from arc.services.media.copies import (
    encoding_episode_ids,
    ensure_copy_row,
    file_matches,
    offline_options,
    publish_copy,
    settings_key,
    source_on_disk,
)
from arc.services.media.download import stat_etag
from arc.services.media.jobs import (
    KEY_ERROR,
    KEY_NOTES,
    KEY_STAGE,
    MAX_ERROR_TAIL,
    STAGE_DONE,
    _keep_lock_warm,
    _plan_for,
    _Reporter,
    _stop,
    episode_lock,
    language_rules,
    lock_key,
)
from arc.services.media.names import (
    EPISODE_KEY,
    OFFLINE_ENCODE,
    OFFLINE_WHY_REQUEST,
    WHY_KEY,
    offline_path_for,
)
from arc.services.media.plan import (
    FONTS_DIR,
    OFFLINE_OUTPUT,
    OfflineOptions,
    PlanError,
    TranscodePlan,
    offline_encode_args,
)
from arc.services.media.probe import ffprobe_json
from arc.services.media.transcode import (
    STAGE_ENCODE,
    STAGE_FONTS,
    STAGE_PACKAGE,
    STAGE_PROBE,
    ProgressCallback,
    TranscodeError,
    clean_work,
    extract_fonts,
    extract_subtitle,
    prepare_directories,
    require_subtitle_filter,
    run_ffmpeg,
    transcode_semaphore,
)
from arc.services.trips.hooks import trip_copy_ready

log = logging.getLogger(__name__)

#: ``('offline_encode', episode_id)``'s first half (see :func:`episode_lock`).
OFFLINE_LOCK_KEY: Final[int] = lock_key(OFFLINE_ENCODE)

#: Staging directory for one claim, beside the finished file, and the glob a
#: hard-killed worker's leftovers are swept by.
STAGING_SUFFIX: Final[str] = ".tmp-{job_id}"
STAGING_GLOB: Final[str] = "{episode_id}.tmp-*"

#: How far the copy's duration may stray from the source's, in seconds. A
#: re-mux rounds to a frame or two; an encode that stopped early is minutes out.
DURATION_TOLERANCE: Final[float] = 2.0

#: The codec names ffprobe reports for the two codecs a copy may be made in.
PROBE_CODEC: Final[dict[str, str]] = {"h264": "h264", "hevc": "hevc"}

#: How many top-level boxes are read looking for ``moov`` and ``mdat``. An MP4
#: has a handful (``ftyp``, ``free``, ``moov``, ``mdat``); this is a bound on a
#: malformed file, not a real limit.
MAX_BOXES: Final[int] = 64

SOURCE_GONE_MESSAGE: Final[str] = (
    "the source file is gone, so no small copy can be made; download the full-size file instead"
)

#: Why a request copy was abandoned without a failure: the episode stopped
#: being ``ready`` (retention took it) before or while its copy was made.
NOT_READY_MESSAGE: Final[str] = "the episode is no longer ready; the copy was not kept"


class CopyRefused(TranscodeError):
    """A failure that no retry can change: the copy fails at once, unretried.

    A finished encode that does not validate, an ffmpeg with no libass, a
    source with no video stream — running the same ffmpeg over the same file
    again produces the same refusal, and each retry would cost a whole encode.
    Like a source that is gone, these end the job ``failed`` and say why.
    """


@dataclass(frozen=True, slots=True)
class CopyResult:
    """What one finished, validated encode produced, still in its staging dir."""

    path: Path
    height: int | None
    subtitle_lang: str | None
    audio_lang: str | None
    fonts: int
    seconds: float
    notes: tuple[str, ...] = ()


# --- Validation -------------------------------------------------------------


def moov_before_mdat(path: Path) -> bool:
    """Whether ``path``'s ``moov`` box comes before its ``mdat`` (faststart).

    Read from the top-level box headers alone — four bytes of size and four of
    type each, a 64-bit size when the first is 1 — so the check costs a few
    reads however large the file. A copy with ``moov`` at the end plays only
    once the whole file is on the device, which defeats the reason
    ``-movflags +faststart`` is passed.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            offset = 0
            for _ in range(MAX_BOXES):
                if offset + 8 > size:
                    return False
                handle.seek(offset)
                header = handle.read(8)
                if len(header) < 8:
                    return False
                length, kind = struct.unpack(">I4s", header)
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if length == 1:
                    extended = handle.read(8)
                    if len(extended) < 8:
                        return False
                    (length,) = struct.unpack(">Q", extended)
                elif length == 0:
                    return False
                if length < 8:
                    return False
                offset += length
    except OSError:
        return False
    return False


def check_probe(
    payload: dict[str, Any] | None,
    *,
    codec: str,
    max_height: int,
    duration: float | None,
    has_audio: bool,
) -> str | None:
    """Why an ffprobe of a finished copy is not acceptable, or ``None``. Pure."""
    if not payload:
        return "ffprobe could not read it"
    streams = [s for s in payload.get("streams") or [] if isinstance(s, dict)]
    videos = [
        s
        for s in streams
        if s.get("codec_type") == "video"
        and not (isinstance(s.get("disposition"), dict) and s["disposition"].get("attached_pic"))
    ]
    if len(videos) != 1:
        return f"expected one video stream, found {len(videos)}"
    video = videos[0]
    expected = PROBE_CODEC.get(codec, codec)
    if video.get("codec_name") != expected:
        return f"the video is {video.get('codec_name')!r}, not {expected!r}"
    if codec == "hevc" and video.get("codec_tag_string") != "hvc1":
        return f"the HEVC track is tagged {video.get('codec_tag_string')!r}, not 'hvc1'"
    try:
        height = int(video.get("height") or 0)
    except TypeError, ValueError:
        height = 0
    if height <= 0 or height > max_height:
        return f"the picture is {height} lines tall; the most a copy may be is {max_height}"
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if has_audio and len(audios) != 1:
        return f"expected one audio stream, found {len(audios)}"
    if duration is not None:
        container = payload.get("format")
        raw: Any = container.get("duration") if isinstance(container, dict) else None
        try:
            actual = float(raw)
        except TypeError, ValueError:
            return "it has no duration"
        # A shortfall only: ``duration`` is the chosen tracks' length, and a
        # copy a frame or two longer is a rounding, not a broken encode.
        if actual < duration - DURATION_TOLERANCE:
            return f"it is {actual:.1f}s long; the source is {duration:.1f}s"
    return None


async def validate_copy(
    path: Path,
    *,
    codec: str,
    max_height: int,
    duration: float | None,
    has_audio: bool,
    ffprobe: str,
) -> str | None:
    """Why the file at ``path`` is not a usable copy, or ``None`` when it is."""
    if not path.is_file() or path.stat().st_size == 0:
        return "ffmpeg wrote nothing"
    if not moov_before_mdat(path):
        return "the moov box is not before the media data (no faststart)"
    payload = await ffprobe_json(path, binary=ffprobe)
    return check_probe(
        payload, codec=codec, max_height=max_height, duration=duration, has_audio=has_audio
    )


# --- The encode ---------------------------------------------------------------


async def encode_copy(
    plan: TranscodePlan,
    *,
    options: OfflineOptions,
    ffmpeg: str,
    ffprobe: str,
    timeout: float,
    on_progress: ProgressCallback | None = None,
) -> CopyResult:
    """Fonts, subtitle, encode, check — into ``plan.output_dir``, the staging dir.

    The transcode's own helpers do the first two steps, so the copy burns in
    exactly what the rendition does (FR-P2): the same track, extracted to the
    same relative name, rendered with the same fonts.
    """
    started = perf_counter()
    prepare_directories(plan)
    if on_progress is not None:
        await on_progress(STAGE_FONTS, 0.0)
    fonts = await extract_fonts(plan, binary=ffmpeg)
    subtitle_file = await extract_subtitle(plan, binary=ffmpeg)
    if subtitle_file is not None:
        try:
            await require_subtitle_filter(ffmpeg, subtitle_file)
        except TranscodeError as exc:
            raise CopyRefused(str(exc), error_tail=exc.error_tail) from exc
    notes = list(plan.notes)
    if plan.subtitle is not None and subtitle_file is None:
        notes.append("the subtitle track could not be extracted; nothing was burned in")

    if on_progress is not None:
        await on_progress(STAGE_ENCODE, 0.0)
    args = offline_encode_args(
        plan,
        replace(options, subtitle_file=subtitle_file, fonts_dir=FONTS_DIR if fonts else None),
    )
    log.info(
        "offline encode starting",
        extra={**plan.as_dict(), "fonts": fonts, "codec": options.codec},
    )
    await run_ffmpeg(
        args,
        binary=ffmpeg,
        cwd=plan.output_dir,
        timeout=timeout,
        duration=plan.duration,
        on_progress=on_progress,
        stage=STAGE_ENCODE,
    )

    if on_progress is not None:
        await on_progress(STAGE_PACKAGE, 1.0)
    clean_work(plan)
    output = plan.output_dir / OFFLINE_OUTPUT
    problem = await validate_copy(
        output,
        codec=options.codec,
        max_height=options.height,
        duration=plan.chosen_duration,
        has_audio=plan.audio is not None,
        ffprobe=ffprobe,
    )
    if problem is not None:
        raise CopyRefused(f"ffmpeg finished but the offline copy is not usable: {problem}")
    height = plan.height if plan.height is not None else None
    return CopyResult(
        path=output,
        height=min(height, options.height) if height is not None else None,
        subtitle_lang=plan.subtitle_lang if subtitle_file else None,
        audio_lang=plan.audio_lang,
        fonts=fonts,
        seconds=perf_counter() - started,
        notes=tuple(notes),
    )


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _publish(result: CopyResult, target: Path) -> os.stat_result:
    """Move a validated copy onto its real name; return the new file's stat.

    ``os.replace`` is atomic on one filesystem, so the media route sees the
    previous copy or this one and never a file being written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(result.path, target)
    return os.stat(target)


# --- The seam the trips use (M19 T3/T4) ---------------------------------------


async def on_copy_ready(ctx: JobContext, episode: Episode, copy: OfflineCopy, *, why: str) -> None:
    """Called once, after a copy has been made, validated and committed ``ready``.

    The one place that reacts to a copy becoming ready; the live event went out
    with the commit. A trip's consequences hang here (M19 T3,
    :func:`~arc.services.trips.hooks.trip_copy_ready`): the trip rows' clock
    starts, and a trip-only episode's source is queued for deletion now that
    its copy is made (owner, 2026-10-05). Anything it writes it commits itself.
    """
    ctx.log.info(
        "offline copy ready",
        extra={
            "episode_id": episode.id,
            "why": why,
            "size": copy.size,
            "codec": copy.codec,
            "height": copy.height,
        },
    )
    await trip_copy_ready(ctx.session, episode)
    await ctx.session.commit()


# --- The handler --------------------------------------------------------------


@register(OFFLINE_ENCODE)
async def offline_encode(ctx: JobContext) -> None:
    """Make one episode's small offline copy (FR-P6)."""
    episode_id = int(ctx.payload[EPISODE_KEY])
    async with episode_lock(ctx.session, episode_id, key=OFFLINE_LOCK_KEY) as owned:
        if not owned:
            ctx.log.info(
                "another claim is already making this copy; leaving it to that one",
                extra={"episode_id": episode_id, "job_id": ctx.job.id},
            )
            return
        await _make(ctx, episode_id)


async def _make(ctx: JobContext, episode_id: int) -> None:
    """The body of :func:`offline_encode`, under the episode's lock."""
    settings = ctx.settings
    why = str(ctx.payload.get(WHY_KEY) or OFFLINE_WHY_REQUEST)
    target = offline_path_for(settings, episode_id)

    episode = await ctx.session.get(Episode, episode_id)
    if episode is None:
        ctx.log.info("episode went away before its copy was made", extra={"id": episode_id})
        return

    payload = dict(ctx.payload)
    payload[EPISODE_KEY] = episode_id
    reporter = _Reporter(ctx, payload)

    copy = await ctx.session.get(OfflineCopy, episode_id)
    if copy is not None and copy.state is OfflineCopyState.READY and file_matches(target, copy):
        await reporter.write(stage=STAGE_DONE, progress=1.0, extra={KEY_ERROR: None})
        ctx.log.info("offline copy already present", extra={"episode_id": episode_id})
        return
    if why == OFFLINE_WHY_REQUEST and episode.state is not EpisodeState.READY:
        # Retention took the episode between the request and this claim: a
        # copy of it now would be a file nobody can fetch.
        await _abandon(ctx, episode_id, reporter)
        return
    if copy is None:
        copy = await ensure_copy_row(ctx.session, episode_id)

    media_file = await source_on_disk(ctx.session, episode_id)
    if media_file is None:
        # No retry can bring a deleted source back: record why and stop.
        await _fail(ctx, episode, copy, reporter, SOURCE_GONE_MESSAGE)
        return

    sub_lang, audio_lang = await _languages(ctx.session, episode_id)
    options = offline_options(settings)
    copy.state = OfflineCopyState.PREPARING
    copy.media_file_id = media_file.id
    copy.codec = options.codec
    copy.height = options.height
    copy.crf = options.crf
    copy.audio_bitrate = options.audio_bitrate
    copy.settings_key = settings_key(options, sub_lang=sub_lang, audio_lang=audio_lang)
    copy.size = None
    copy.etag = None
    copy.ready_at = None
    copy.error = None
    publish_copy(ctx.session, episode, copy)
    await ctx.session.commit()
    await reporter.write(stage=STAGE_PROBE, progress=0.0, extra={KEY_ERROR: None})

    for stale in settings.offline_dir.glob(STAGING_GLOB.format(episode_id=episode_id)):
        _discard(stale)
    staging = settings.offline_dir / f"{episode_id}{STAGING_SUFFIX.format(job_id=ctx.job.id)}"

    try:
        plan = await _plan_for(
            settings, Path(media_file.path), staging, sub_lang=sub_lang, audio_lang=audio_lang
        )
    except PlanError as exc:
        # A source with no video stream stays one: no retry.
        await _fail(ctx, episode, copy, reporter, str(exc))
        return

    semaphore = transcode_semaphore(settings.max_transcodes)
    await reporter.write(stage=STAGE_ENCODE, progress=0.0, heartbeat=True)
    warm = asyncio.create_task(_keep_lock_warm(reporter))
    info: os.stat_result | None = None
    try:
        async with semaphore:
            await _stop(warm)
            result = await encode_copy(
                plan,
                options=options,
                ffmpeg=settings.ffmpeg_bin,
                ffprobe=settings.ffprobe_bin,
                timeout=settings.transcode_timeout_seconds,
                on_progress=reporter,
            )
        # Checked again, under the episode's row lock, immediately before the
        # file is put in place: retention takes the same lock, so either it has
        # already made the episode ``not_wanted`` (and the copy is dropped
        # here), or it waits for this commit and then deletes the copy with the
        # episode. Never a copy left behind on an episode that is gone.
        if why != OFFLINE_WHY_REQUEST or await _still_ready(ctx.session, episode):
            info = _publish(result, target)
    except CopyRefused as exc:
        await _fail(ctx, episode, copy, reporter, str(exc), tail=exc.error_tail)
        return
    except TranscodeError as exc:
        await _fail(ctx, episode, copy, reporter, str(exc), tail=exc.error_tail)
        raise
    except Exception as exc:  # pragma: no cover - defensive; the runner logs it
        await _fail(ctx, episode, copy, reporter, repr(exc))
        raise
    finally:
        await _stop(warm)
        # Published or not, nothing in the staging directory is wanted any
        # more: the copy has been moved out of it, or it is not a copy.
        _discard(staging)

    if info is None:
        await _abandon(ctx, episode_id, reporter)
        return
    copy.state = OfflineCopyState.READY
    copy.size = info.st_size
    copy.etag = stat_etag(info.st_size, info.st_mtime_ns)
    copy.height = result.height
    copy.ready_at = datetime.now(UTC)
    copy.error = None
    publish_copy(ctx.session, episode, copy)
    await ctx.session.commit()
    await reporter.write(
        stage=STAGE_DONE,
        progress=1.0,
        extra={KEY_ERROR: None, KEY_NOTES: list(result.notes)},
    )
    ctx.log.info(
        "offline copy made",
        extra={
            "episode_id": episode_id,
            "seconds": round(result.seconds, 1),
            "bytes": info.st_size,
            "subtitle_lang": result.subtitle_lang,
            "audio_lang": result.audio_lang,
            "fonts": result.fonts,
            "notes": list(result.notes),
        },
    )
    await on_copy_ready(ctx, episode, copy, why=why)


async def _languages(session: AsyncSession, episode_id: int) -> tuple[str, str]:
    """``(sub_lang, audio_lang)`` to plan the copy with.

    The rendition's own recorded choice when the episode has one, so the copy
    carries the same tracks as what is streamed even if an admin has changed
    the language rules since; the current rules where the rendition recorded
    none (no subtitles, an untagged track) or there is no rendition at all.
    """
    rules_sub, rules_audio = await language_rules(session)
    rendition = await session.scalar(select(Rendition).where(Rendition.episode_id == episode_id))
    if rendition is None:
        return rules_sub, rules_audio
    return rendition.subtitle_lang or rules_sub, rendition.audio_lang or rules_audio


async def _still_ready(session: AsyncSession, episode: Episode) -> bool:
    """Lock the episode row and read its state afresh; True if still ``ready``."""
    await session.refresh(episode, with_for_update=True)
    return episode.state is EpisodeState.READY


async def _abandon(ctx: JobContext, episode_id: int, reporter: _Reporter) -> None:
    """Drop a request copy whose episode is no longer ready: row gone, job done."""
    await ctx.session.execute(delete(OfflineCopy).where(OfflineCopy.episode_id == episode_id))
    await ctx.session.commit()
    await reporter.write(stage=STAGE_DONE, extra={KEY_ERROR: NOT_READY_MESSAGE})
    ctx.log.info(
        "episode left ready before its copy was kept; dropped the copy",
        extra={"episode_id": episode_id, "job_id": ctx.job.id},
    )


# --- Start-up housekeeping ----------------------------------------------------


async def sweep_offline_leftovers(session: AsyncSession, settings: Settings) -> int:
    """Remove what a hard-killed worker left in ``DATA_DIR/offline``. Returns a count.

    Two kinds of leftover, both of which only a ``SIGKILL`` can produce:
    a ``<id>.tmp-<job id>`` staging directory whose job is no longer live, and a
    ``<id>.mp4`` with no ``offline_copies`` row at all. Run once at worker
    start-up, after orphaned jobs have been reclaimed, so a staging directory
    belonging to a job another worker is still running is left alone. Anything
    whose name Arc did not write is left alone too, and links are unlinked,
    never followed.
    """
    root = settings.offline_dir
    if not root.is_dir():
        return 0
    entries = list(root.iterdir())
    live = await encoding_episode_ids(session)
    rows = set((await session.scalars(select(OfflineCopy.episode_id))).all())
    removed = 0
    for entry in entries:
        name = entry.name
        stem, dot, rest = name.partition(".")
        if not stem.isdigit():
            continue
        episode_id = int(stem)
        stale_staging = dot and rest.startswith("tmp-") and episode_id not in live
        orphan_copy = name == f"{episode_id}.mp4" and episode_id not in rows
        if not (stale_staging or orphan_copy):
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink(missing_ok=True)
        else:
            _discard(entry)
        removed += 1
    if removed:
        log.info("swept offline leftovers", extra={"count": removed})
    return removed


async def _fail(
    ctx: JobContext,
    episode: Episode,
    copy: OfflineCopy,
    reporter: _Reporter,
    message: str,
    *,
    tail: str = "",
) -> None:
    """Record the failure on the copy and the job, and commit it.

    Committed here rather than left to the runner, which rolls this session
    back: the reason has to survive the exception that schedules the retry.
    """
    detail = keep_head_and_tail(message, tail, limit=MAX_ERROR_TAIL)
    try:
        copy.state = OfflineCopyState.FAILED
        copy.error = detail
        publish_copy(ctx.session, episode, copy)
        await ctx.session.commit()
    except Exception:  # pragma: no cover - a rolled-back session, mid-shutdown
        ctx.log.exception("could not mark the copy failed", extra={"episode_id": episode.id})
        await ctx.session.rollback()
    await reporter.write(
        stage=str(reporter.payload.get(KEY_STAGE) or STAGE_ENCODE),
        extra={KEY_ERROR: detail},
    )
    ctx.log.error(
        "offline copy failed",
        extra={"episode_id": episode.id, "error": message, "tail": tail[-500:]},
    )


__all__ = [
    "DURATION_TOLERANCE",
    "NOT_READY_MESSAGE",
    "OFFLINE_LOCK_KEY",
    "SOURCE_GONE_MESSAGE",
    "STAGING_GLOB",
    "STAGING_SUFFIX",
    "CopyRefused",
    "CopyResult",
    "check_probe",
    "encode_copy",
    "moov_before_mdat",
    "offline_encode",
    "on_copy_ready",
    "sweep_offline_leftovers",
    "validate_copy",
]
