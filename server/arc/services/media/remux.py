"""The ``remux`` handler: an episode made playable in seconds (M20, FR-P1, FR-P7).

Since M20 this is how a matched episode is prepared. The source's H.264 is
**copied**, not encoded; the audio is copied when it is AAC already and
converted to stereo AAC otherwise; the chosen subtitle track is extracted by
the same ffmpeg pass and turned into WebVTT, which the media router serves as
an HLS subtitle rendition (architecture.md §5.3c). On a 1080p SubsPlease
episode that is about 20 seconds of one core, against twenty minutes of every
core for the burn-in encode it replaces.

**A source that cannot be copied is handed over, not handled here.**
:func:`~arc.services.media.plan.copyable` says whether Safari and Chrome play
the video as it is (H.264, 8-bit 4:2:0, Baseline/Main/High, at most 1080p,
progressive). When it says no — 10-bit, HEVC, 4K, interlaced — this job queues
the ``transcode`` (deduplicated), records the reason on its own payload with
``stage=fallback``, and finishes. The episode stays where it was; the
transcode's own path moves it to ``preparing``. A hand-off rather than an
in-process call so that ``MAX_TRANSCODES`` still counts real encodes and a
cheap remux never holds an encoder slot (FR-P3, :func:`arc.services.jobs.loop.
process_caps`).

Everything else is the transcode handler's discipline, reused rather than
re-derived (:mod:`arc.services.media.jobs`): the per-episode advisory lock
taken before anything is read (on ``('remux', episode_id)``), the idempotent
short-circuit when a valid rendition is already there, the staging directory
``renditions/<id>.tmp-<job id>`` renamed into place only after validation
(:func:`~arc.services.media.jobs._publish`), the committed ``failed`` state and
stderr tail before the exception the runner retries on (two attempts).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from time import perf_counter
from typing import Any, Final

from arc.config import Settings
from arc.core.text import keep_head_and_tail
from arc.models import Episode, EpisodeState, JobStatus, MediaFile
from arc.models.media import RENDITION_REMUX
from arc.services.acquisition.states import transition
from arc.services.jobs.registry import JobContext, register
from arc.services.media.jobs import (
    KEY_EPISODE,
    KEY_ERROR,
    KEY_FORCE,
    KEY_NOTES,
    MAX_ERROR_TAIL,
    NO_SOURCE,
    STAGE_DONE,
    STAGING_SUFFIX,
    TRANSCODABLE,
    _discard,
    _fail,
    _publish,
    _rendition_for,
    _Reporter,
    _save_rendition,
    _source_for,
    discard_stale_staging,
    episode_lock,
    language_rules,
    lock_key,
    running_prepare_job,
    validate_output,
)
from arc.services.media.live_serve import remove_live_dir
from arc.services.media.names import (
    REMUX,
    REMUX_INFO_NAME,
    STAGE_DEFERRED,
    SUBTITLES_VTT_NAME,
    TRANSCODE,
    RemuxInfo,
    enqueue_transcode,
    output_dir_for,
)
from arc.services.media.plan import (
    VIDEO_PLAYLIST_NAME,
    WORK_DIR,
    Copyability,
    PlanError,
    RemuxOptions,
    TranscodePlan,
    _raw_stream,
    build_plan,
    copyable,
    remux_args,
)
from arc.services.media.probe import ffprobe_json
from arc.services.media.subtitles import (
    Cue,
    ass_to_cues,
    cues_to_vtt,
    segment_bounds,
    srt_to_cues,
)
from arc.services.media.transcode import TranscodeError, TranscodeResult, run_ffmpeg

#: The advisory lock's first half for a remux: ``('remux', episode_id)``.
#: Not the transcode's, deliberately — the fallback queues a transcode while
#: this lock is still held, and a shared key would make that transcode's claim
#: find the episode "taken" and give up. A running transcode is checked for
#: separately (:func:`~arc.services.media.jobs.running_prepare_job`): the remux
#: steps aside for it, and a transcode defers to a running remux.
REMUX_LOCK_KEY: Final[int] = lock_key(REMUX)

#: Stages a remux writes to its payload. ``fallback`` is terminal: the job
#: handed the episode over to the burn-in encode.
STAGE_PROBE = "probe"
STAGE_REMUX = "remux"
STAGE_FALLBACK = "fallback"

#: Payload keys a remux adds to the transcode's (:mod:`arc.services.media.jobs`).
#: ``fallback_reason`` is :attr:`Copyability.reason` ("10-bit", "HEVC"), shown on
#: the episode while the encode runs; ``transcode_job_id`` is the job it queued.
KEY_FALLBACK_REASON = "fallback_reason"
KEY_TRANSCODE_JOB = "transcode_job_id"
KEY_AUDIO = "audio"
#: Set on a remux that adopted a live directory instead of running ffmpeg.
KEY_ADOPTED = "adopted"

#: The 90 kHz clock of MPEG-TS timestamps, which ``X-TIMESTAMP-MAP`` speaks.
MPEGTS_HZ: Final[int] = 90_000

#: States a remux acts on: the transcode's, for the transcode's reasons.
REMUXABLE: frozenset[EpisodeState] = TRANSCODABLE

#: The label players show for the subtitle track, by ISO 639-1 code. Anything
#: unlisted is shown by its code; a track with no language as "Subtitles".
LANGUAGE_NAMES: Final[dict[str, str]] = {
    "en": "English",
    "ja": "Japanese",
    "es": "Español",
    "pt": "Português",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "ru": "Русский",
    "ar": "العربية",
    "zh": "中文",
    "ko": "한국어",
    "pl": "Polski",
    "id": "Bahasa Indonesia",
    "th": "ไทย",
    "vi": "Tiếng Việt",
    "ms": "Bahasa Melayu",
    "nl": "Nederlands",
    "tr": "Türkçe",
    "he": "עברית",
    "hi": "हिन्दी",
}

#: H.264 profile → the six hex digits of ``profile_idc`` and the constraint
#: flags in an RFC 6381 ``avc1.PPCCLL`` string. The level is appended.
_AVC_PROFILES: Final[dict[str, str]] = {
    "Constrained Baseline": "42E0",
    "Baseline": "4200",
    "Main": "4D40",
    "High": "6400",
}

#: AAC profile → its RFC 6381 object type. Converted audio is always LC.
_AAC_OBJECT: Final[dict[str, str]] = {"HE-AAC": "mp4a.40.5", "HE-AACv2": "mp4a.40.29"}
_AAC_LC: Final[str] = "mp4a.40.2"

NOTE_NO_DIALOGUE = (
    "the subtitle track had no dialogue left once signs and songs were dropped; "
    "the episode plays without subtitles"
)
NOTE_SUBS_UNREADABLE = (
    "the subtitle track could not be extracted; the episode plays without subtitles"
)
NO_PROBE = "ffprobe could not read the source file"


def subtitle_label(language: str | None) -> str:
    """The name a player shows for a track in ``language`` (ISO 639-1)."""
    if not language:
        return "Subtitles"
    return LANGUAGE_NAMES.get(language, language)


def codecs_string(video: dict[str, Any], audio: dict[str, Any] | None, *, audio_copy: bool) -> str:
    """The ``CODECS`` attribute of the master playlist, from the source's probe.

    The video is copied, so its profile and level are the source's. The audio
    is the source's AAC object type when copied, AAC-LC when converted.
    """
    profile = _AVC_PROFILES.get(str(video.get("profile") or ""), "6400")
    try:
        level = int(video.get("level") or 40)
    except TypeError, ValueError:
        level = 40
    parts = [f"avc1.{profile}{max(min(level, 255), 0):02X}"]
    if audio is not None:
        parts.append(
            _AAC_OBJECT.get(str(audio.get("profile") or ""), _AAC_LC) if audio_copy else _AAC_LC
        )
    return ",".join(parts)


def peak_bandwidth(playlist_text: str, sizes: list[int]) -> int:
    """Peak bits per second over the segments: ``BANDWIDTH`` (RFC 8216 §4.3.4.2).

    ``sizes`` are the segments' byte counts in playlist order. A segment with
    no stated duration is skipped; with none at all the answer is 1.
    """
    peak = 0.0
    for (start, end), size in zip(segment_bounds(playlist_text), sizes, strict=False):
        length = end - start
        if length > 0:
            peak = max(peak, size * 8 / length)
    return max(int(peak), 1)


def read_cues(path: Path) -> list[Cue]:
    """The cues of the extracted ``_work/sub.ass`` or ``_work/sub.srt``.

    Decoded leniently (a byte-order mark stripped, undecodable bytes replaced):
    a fansub file in a stray encoding should lose a character, not the track.
    """
    text = path.read_bytes().decode("utf-8", "replace").lstrip("﻿")
    return ass_to_cues(text) if path.suffix == ".ass" else srt_to_cues(text)


async def _probe(
    settings: Settings, source: Path, staging: Path, *, sub_lang: str, audio_lang: str
) -> tuple[dict[str, Any], TranscodePlan]:
    payload = await ffprobe_json(source, binary=settings.ffprobe_bin)
    if not payload:
        raise PlanError(f"{NO_PROBE}: {source.name}")
    plan = build_plan(
        payload, source=source, output_dir=staging, sub_lang=sub_lang, audio_lang=audio_lang
    )
    return payload, plan


def remux_options(settings: Settings, verdict: Copyability) -> RemuxOptions:
    """The remux's knobs from ``arc/config.py`` and the copyability verdict."""
    return RemuxOptions(
        segment_seconds=settings.hls_segment_seconds,
        audio_bitrate=settings.remux_audio_bitrate,
        audio_copy=verdict.audio_copy,
    )


@register(REMUX)
async def remux_episode(ctx: JobContext) -> None:
    """Prepare one episode by remuxing it (FR-P1), or hand it to the encode (FR-P7).

    Idempotent: a rendition already on disk that validates, with its row, is
    left alone unless the payload says ``force``. The lock is taken before
    anything is read, for the reason the transcode gives
    (:func:`arc.services.media.jobs.transcode_episode`).
    """
    episode_id = int(ctx.payload[KEY_EPISODE])
    async with episode_lock(ctx.session, episode_id, key=REMUX_LOCK_KEY) as owned:
        if not owned:
            payload = {**ctx.payload, KEY_EPISODE: episode_id}
            await _defer(ctx, _Reporter(ctx, payload), episode_id)
            return
        await _remux(ctx, episode_id)


async def _defer(
    ctx: JobContext, reporter: _Reporter, episode_id: int, *, transcode_job_id: int | None = None
) -> None:
    """Step aside for another prepare job, and say so on this job's row.

    ``stage=deferred`` is what :func:`~arc.services.media.names.
    latest_transcode_jobs` skips, so the episode keeps showing the job that is
    actually working rather than a finished remux with no progress.
    """
    extra: dict[str, Any] = {KEY_ERROR: None}
    if transcode_job_id is not None:
        extra[KEY_TRANSCODE_JOB] = transcode_job_id
    await reporter.write(stage=STAGE_DEFERRED, extra=extra)
    ctx.log.info(
        "another prepare job is working on this episode; stepping aside",
        extra={
            "episode_id": episode_id,
            "job_id": ctx.job.id,
            "transcode_job_id": transcode_job_id,
        },
    )


async def _rendition_intact(ctx: JobContext, episode_id: int) -> bool:
    """A rendition row and a directory that validates: the episode still plays."""
    if await _rendition_for(ctx.session, episode_id) is None:
        return False
    output_dir = output_dir_for(ctx.settings, episode_id)
    return bool(await validate_output(output_dir, ffprobe=ctx.settings.ffprobe_bin))


async def _fail_early(
    ctx: JobContext, episode: Episode, reporter: _Reporter, message: str, *, tail: str = ""
) -> TranscodeError:
    """Record a failure that happened before the episode moved to ``preparing``.

    ``failed`` is only reachable from ``preparing`` (:mod:`arc.services.
    acquisition.states`), so the episode passes through it first, in the same
    commit as the failure. Except for a ``ready`` episode whose rendition is
    still intact (a forced remux whose source is gone): it still plays, so
    the failure goes on the job only and the episode stays ``ready``.
    """
    if episode.state is EpisodeState.READY and await _rendition_intact(ctx, episode.id):
        await reporter.write(
            stage=str(reporter.payload.get("stage") or STAGE_PROBE),
            extra={KEY_ERROR: keep_head_and_tail(message, tail, limit=MAX_ERROR_TAIL)},
        )
        ctx.log.error(
            "remux failed; the existing rendition is kept",
            extra={"episode_id": episode.id, "error": message},
        )
        return TranscodeError(message, error_tail=tail)
    if episode.state is not EpisodeState.PREPARING:
        transition(episode, EpisodeState.PREPARING, reason=f"remux job {ctx.job.id}")
    await _fail(ctx, episode, reporter, message, tail=tail)
    return TranscodeError(message, error_tail=tail)


async def _remux(ctx: JobContext, episode_id: int) -> None:
    """The body of :func:`remux_episode`, under the episode's lock."""
    force = bool(ctx.payload.get(KEY_FORCE))
    settings = ctx.settings
    output_dir = output_dir_for(settings, episode_id)

    episode = await ctx.session.get(Episode, episode_id)
    if episode is None:
        ctx.log.info("episode went away before it was remuxed", extra={"id": episode_id})
        return
    if episode.state not in REMUXABLE:
        ctx.log.info(
            "episode is not in a state to be remuxed",
            extra={"episode_id": episode_id, "state": episode.state.value},
        )
        return
    payload = dict(ctx.payload)
    payload[KEY_EPISODE] = episode_id
    reporter = _Reporter(ctx, payload)

    transcode = await running_prepare_job(ctx.session, TRANSCODE, episode_id)
    if transcode is not None:
        # The burn-in encode is writing this episode's rendition; it was
        # asked for deliberately (a fallback, an admin), so it is left to it.
        await _defer(ctx, reporter, episode_id, transcode_job_id=transcode.id)
        return

    if not force:
        # The transcode's short-circuit, for the transcode's reason: a job with
        # nothing to do must not flick a playable episode through ``preparing``.
        rendition = await _rendition_for(ctx.session, episode_id)
        segments = await validate_output(output_dir, ffprobe=settings.ffprobe_bin)
        if rendition is not None and segments:
            if episode.state is not EpisodeState.READY:
                transition(episode, EpisodeState.PREPARING, reason="rendition already on disk")
                transition(episode, EpisodeState.READY, reason="rendition already on disk")
                await ctx.session.commit()
            await reporter.write(stage=STAGE_DONE, progress=1.0, extra={KEY_ERROR: None})
            ctx.log.info(
                "rendition already present; nothing to remux",
                extra={"episode_id": episode_id, "segments": segments},
            )
            return

    await reporter.write(stage=STAGE_PROBE, progress=0.0, extra={KEY_ERROR: None})
    media_file = await _source_for(ctx.session, episode_id)
    if media_file is None:
        raise await _fail_early(ctx, episode, reporter, NO_SOURCE)
    source = Path(media_file.path)
    if not source.exists():
        raise await _fail_early(ctx, episode, reporter, f"the source file is missing: {source}")

    # A progressive remux (M20 T6) may already have cut this very file while
    # it downloaded: adopt it rather than run ffmpeg a second time.
    if not force and await _adopt(ctx, episode, reporter, media_file):
        return
    # The ordinary remux runs; a live directory is not the rendition. Not
    # while a live remux is still writing it: that job sees the episode leave
    # its states and removes its own directory.
    from arc.services.media.live import running_live_job

    if await running_live_job(ctx.session, episode_id) is None:
        remove_live_dir(settings, episode_id)

    await discard_stale_staging(ctx.session, output_dir, episode_id, ctx.job.id)
    staging = output_dir.with_suffix(STAGING_SUFFIX.format(job_id=ctx.job.id))

    sub_lang, audio_lang = await language_rules(ctx.session)
    try:
        probe, plan = await _probe(
            settings, source, staging, sub_lang=sub_lang, audio_lang=audio_lang
        )
    except PlanError as exc:
        raise await _fail_early(ctx, episode, reporter, str(exc)) from exc

    verdict = copyable(probe, plan)
    if not verdict.video:
        await _hand_over(ctx, episode, reporter, verdict, force=force)
        return

    # The remux will run: from here on the episode is ``preparing`` and the
    # old rendition (on ``force``) is gone, as FR-P5 asks.
    if episode.state is not EpisodeState.PREPARING:
        transition(episode, EpisodeState.PREPARING, reason=f"remux job {ctx.job.id}")
    if force:
        existing = await _rendition_for(ctx.session, episode_id)
        if existing is not None:
            await ctx.session.delete(existing)
        shutil.rmtree(output_dir, ignore_errors=True)
        ctx.log.info("remuxing again on request", extra={"episode_id": episode_id})
    await ctx.session.commit()

    ctx.log.info(
        "remux planned",
        extra={"episode_id": episode_id, "audio_copy": verdict.audio_copy, **plan.as_dict()},
    )
    published = False
    try:
        await reporter.write(stage=STAGE_REMUX, progress=0.0, heartbeat=True)
        result, text_subtitles, info, notes = await _run(
            ctx, reporter, plan, probe, verdict, staging
        )
        result = _publish(staging, output_dir, result)
        published = True
    except TranscodeError as exc:
        await _fail(ctx, episode, reporter, str(exc), tail=exc.error_tail)
        raise
    except Exception as exc:  # pragma: no cover - defensive; the runner logs it
        await _fail(ctx, episode, reporter, repr(exc))
        raise
    finally:
        if not published:
            _discard(staging)

    await _save_rendition(
        ctx.session,
        episode_id,
        output_dir=result.output_dir,
        duration=result.duration,
        width=result.width,
        height=result.height,
        subtitle_lang=result.subtitle_lang,
        audio_lang=result.audio_lang,
        kind=RENDITION_REMUX,
        text_subtitles=text_subtitles,
        playlist_name=VIDEO_PLAYLIST_NAME,
    )
    transition(episode, EpisodeState.READY, reason="remux finished")
    await ctx.session.commit()
    await reporter.write(
        stage=STAGE_DONE,
        progress=1.0,
        extra={
            KEY_ERROR: None,
            KEY_NOTES: notes,
            KEY_AUDIO: "copy" if verdict.audio_copy else "aac",
        },
    )
    ctx.log.info(
        "episode ready (remux)",
        extra={
            "episode_id": episode_id,
            "seconds": round(result.seconds, 1),
            "segments": result.segments,
            "duration": result.duration,
            "text_subtitles": text_subtitles,
            "subtitle_lang": result.subtitle_lang,
            "audio_lang": result.audio_lang,
            "audio_copy": verdict.audio_copy,
            "notes": notes,
        },
    )


#: How long a remux waits for a running live remux of the same episode to
#: write its ``ENDLIST`` before it remuxes the file itself. The live job
#: closes ffmpeg's input as soon as the last piece is checked, so this is
#: normally seconds; a live directory that already has its ``ENDLIST`` is
#: adopted at once, whatever the job is still doing (it notices the episode
#: is ready and stops).
LIVE_ADOPT_WAIT_SECONDS: float = 15.0
LIVE_ADOPT_POLL_SECONDS: float = 2.0


async def _adopt(
    ctx: JobContext, episode: Episode, reporter: _Reporter, media_file: MediaFile
) -> bool:
    """Make the live directory the rendition (:func:`~arc.services.media.live.adopt_live`).

    Waits up to :data:`LIVE_ADOPT_WAIT_SECONDS` for a live remux of this
    episode that is still running. ``True`` when the episode is ``ready``.
    """
    from arc.services.media.live import (
        adopt_live,
        live_has_ended,
        publish_adopted,
        running_live_job,
    )

    waited = 0.0
    while not live_has_ended(ctx.settings, episode.id):
        if await running_live_job(ctx.session, episode.id) is None:
            break
        if waited >= LIVE_ADOPT_WAIT_SECONDS:
            # Still being cut: never adopted from under it.
            return False
        await ctx.session.commit()
        await asyncio.sleep(LIVE_ADOPT_POLL_SECONDS)
        waited += LIVE_ADOPT_POLL_SECONDS
    adopted = await adopt_live(ctx.session, ctx.settings, episode.id, media_file)
    if adopted is None:
        return False
    if episode.state is not EpisodeState.PREPARING:
        transition(episode, EpisodeState.PREPARING, reason=f"remux job {ctx.job.id}")
    await _save_rendition(
        ctx.session,
        episode.id,
        output_dir=adopted.output_dir,
        duration=adopted.duration,
        width=adopted.width,
        height=adopted.height,
        subtitle_lang=adopted.subtitle_lang,
        audio_lang=adopted.audio_lang,
        kind=RENDITION_REMUX,
        text_subtitles=adopted.text_subtitles,
        playlist_name=VIDEO_PLAYLIST_NAME,
    )
    transition(episode, EpisodeState.READY, reason="live remux adopted")
    # Ready first, then the rename: a request in between finds the episode
    # ready and the ready route falls back to the live directory, so the
    # player's URLs never 404 across the adoption.
    await ctx.session.commit()
    publish_adopted(ctx.settings, episode.id)
    await reporter.write(
        stage=STAGE_DONE,
        progress=1.0,
        extra={
            KEY_ERROR: None,
            KEY_ADOPTED: True,
            KEY_AUDIO: "copy" if adopted.audio_copy else "aac",
        },
    )
    ctx.log.info(
        "episode ready (live remux adopted)",
        extra={
            "episode_id": episode.id,
            "segments": adopted.segments,
            "duration": adopted.duration,
            "text_subtitles": adopted.text_subtitles,
        },
    )
    return True


async def _hand_over(
    ctx: JobContext,
    episode: Episode,
    reporter: _Reporter,
    verdict: Copyability,
    *,
    force: bool,
) -> None:
    """Queue the burn-in encode for a source the remux cannot copy (FR-P7).

    The episode is left as it is — ``matched`` normally — and the transcode's
    own handler moves it on. ``force`` is passed through, so a forced remux of
    a ready episode becomes a forced re-encode rather than nothing.
    """
    job = await enqueue_transcode(ctx.session, episode.id, force=force)
    if job.status is JobStatus.PENDING:
        # The reason travels with the encode, so the episode can say why it is
        # a full encode (``EpisodeOut.prepare_note``) while that job is the
        # newest. And on a dedupe hit a pending non-force transcode would
        # otherwise swallow this ``force``: the encode would find the old
        # rendition valid and do nothing.
        extra: dict[str, Any] = {KEY_FALLBACK_REASON: verdict.reason}
        if force:
            extra[KEY_FORCE] = True
        job.payload = {**(job.payload or {}), **extra}
    await ctx.session.commit()
    await reporter.write(
        stage=STAGE_FALLBACK,
        extra={
            KEY_ERROR: None,
            KEY_FALLBACK_REASON: verdict.reason,
            KEY_TRANSCODE_JOB: job.id,
        },
    )
    ctx.log.info(
        "source cannot be copied; handed to the burn-in encode",
        extra={"episode_id": episode.id, "reason": verdict.reason, "transcode_job_id": job.id},
    )


async def _run(
    ctx: JobContext,
    reporter: _Reporter,
    plan: TranscodePlan,
    probe: dict[str, Any],
    verdict: Copyability,
    staging: Path,
) -> tuple[TranscodeResult, bool, RemuxInfo, list[str]]:
    """ffmpeg into ``staging``, then subtitles, validation and the sidecar's facts.

    Returns the result (located in ``staging``), whether a WebVTT track was
    written, the :class:`RemuxInfo` to store, and the notes for the payload.
    Raises :class:`TranscodeError` for anything that leaves no playable
    rendition.
    """
    settings = ctx.settings
    started = perf_counter()
    if staging.exists():
        shutil.rmtree(staging)
    # ffmpeg creates neither the directory it runs in nor ``_work/`` for the
    # subtitle output, and fails "No such file or directory" without them.
    (staging / WORK_DIR).mkdir(parents=True, exist_ok=True)

    args = remux_args(plan, remux_options(settings, verdict), live=False)
    await run_ffmpeg(
        args,
        binary=settings.ffmpeg_bin,
        cwd=staging,
        timeout=settings.remux_timeout_seconds,
        duration=plan.duration,
        on_progress=reporter,
        stage=STAGE_REMUX,
    )

    notes = list(plan.notes)
    cues: list[Cue] = []
    if plan.subtitle is not None and plan.subtitle_file is not None:
        extracted = staging / plan.subtitle_file
        try:
            cues = read_cues(extracted) if extracted.exists() else []
        except OSError:
            cues = []
        if not extracted.exists() or extracted.stat().st_size == 0:
            notes.append(NOTE_SUBS_UNREADABLE)
        elif not cues:
            notes.append(NOTE_NO_DIALOGUE)
    if cues:
        (staging / SUBTITLES_VTT_NAME).write_text(cues_to_vtt(cues), encoding="utf-8")
    shutil.rmtree(staging / WORK_DIR, ignore_errors=True)

    segments = await validate_output(staging, ffprobe=settings.ffprobe_bin)
    playlist = staging / VIDEO_PLAYLIST_NAME
    out = await ffprobe_json(playlist, binary=settings.ffprobe_bin) if segments else None
    streams = [s for s in (out or {}).get("streams") or [] if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not segments or video is None or str(video.get("codec_name")) != "h264":
        raise TranscodeError(
            f"ffmpeg finished but {playlist.name} is not a playable H.264 playlist"
        )
    if plan.audio is not None and (audio is None or str(audio.get("codec_name")) != "aac"):
        raise TranscodeError(f"ffmpeg finished but {playlist.name} has no AAC audio")

    try:
        first_pts = max(float(video.get("start_time") or 0.0), 0.0)
    except TypeError, ValueError:
        first_pts = 0.0
    container: Any = (out or {}).get("format") or {}
    try:
        duration: float | None = float(container.get("duration"))
    except TypeError, ValueError, AttributeError:
        duration = plan.duration
    if duration is not None and duration <= 0:
        duration = plan.duration

    text = playlist.read_text()
    sizes = [
        (staging / name).stat().st_size
        for name in (line.strip() for line in text.splitlines())
        if name and not name.startswith("#") and (staging / name).is_file()
    ]
    raw_video = dict(_raw_stream(probe, "video", plan.video))
    raw_audio = dict(_raw_stream(probe, "audio", plan.audio)) if plan.audio else None
    info = RemuxInfo(
        mpegts=round(first_pts * MPEGTS_HZ),
        bandwidth=peak_bandwidth(text, sizes),
        codecs=codecs_string(raw_video, raw_audio, audio_copy=verdict.audio_copy),
        subtitle_name=subtitle_label(plan.subtitle_lang) if cues else None,
    )
    result = TranscodeResult(
        output_dir=staging,
        playlist=playlist,
        duration=duration,
        width=plan.width,
        height=plan.height,
        subtitle_lang=plan.subtitle_lang if cues else None,
        audio_lang=plan.audio_lang,
        segments=segments,
        fonts=0,
        seconds=perf_counter() - started,
        notes=tuple(notes),
    )
    # Inside the staging directory, so it is published with the rest or not
    # at all: a rendition is never in place without the facts its generated
    # playlists need.
    (staging / REMUX_INFO_NAME).write_text(info.to_json(), encoding="utf-8")
    return result, bool(cues), info, notes


__all__ = [
    "KEY_ADOPTED",
    "KEY_AUDIO",
    "LIVE_ADOPT_WAIT_SECONDS",
    "KEY_FALLBACK_REASON",
    "KEY_TRANSCODE_JOB",
    "LANGUAGE_NAMES",
    "MPEGTS_HZ",
    "REMUXABLE",
    "REMUX_LOCK_KEY",
    "STAGE_FALLBACK",
    "STAGE_PROBE",
    "STAGE_REMUX",
    "codecs_string",
    "peak_bandwidth",
    "read_cues",
    "remux_episode",
    "remux_options",
    "subtitle_label",
]
