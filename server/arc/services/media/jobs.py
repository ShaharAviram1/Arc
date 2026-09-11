"""The ``transcode`` handler and the sweep that catches what it missed (M7).

One handler, and it is the longest-running thing Arc does: a 24-minute 1080p
episode is roughly twenty minutes of x264 at ``veryfast`` on two cores. Almost
everything here follows from that one fact.

**Progress lives in the job's payload, not in a column.** A transcode has to
say how far it has got (FR-P4), and the obvious place — a ``progress`` column
on ``episodes`` — would be a migration for a number that is only meaningful
while one job runs, is meaningless the moment it stops, and would have to be
cleared by every other writer of that row. The job row already exists, already
has a JSONB payload, already has a lifetime exactly as long as the work, and
is already what the admin queue view reads. So the payload carries
``{"episode_id", "progress", "stage", "error_tail"}``, and
:meth:`arc.api.anime_schemas.PrepareState.from_job` reads the latest transcode
job for an episode to render the percentage and the failure.

**The job keeps its own lock warm, waiting included.** ``requeue_stale``
returns a job to the queue when its worker has held it for longer than
``WORKER_STALE_AFTER``, which is how a crashed worker's rows are recovered —
and, without care, also how a *working* worker's twenty-minute encode gets a
second copy of itself started underneath it. So this handler pushes
``locked_at`` forward every :data:`HEARTBEAT_SECONDS` and the sweep only ever
sees a transcode that has genuinely stopped talking. Two things do the pushing,
because there are two ways to be busy: while ffmpeg runs the beat rides on the
progress reports it is already writing (:class:`_Reporter`), and while the job
is merely *queued behind* other encodes :func:`_keep_lock_warm` does it from a
task of its own. The second is not an optimisation — a third job on a two-slot
host waits for as long as the two ahead of it take, which is longer than
``WORKER_STALE_AFTER``, and without it the sweep would requeue the one job that
is doing nothing wrong.

**One claim per episode, enforced by Postgres.** Idempotent is not the same as
safe to run twice *at the same time*: two claims of the same episode — a stale
sweep that fired a moment early, an admin pressing retry on another host —
would write into one rendition directory with two ffmpegs. So the encoding half
runs under a transaction-level advisory lock on ``('transcode', episode_id)``,
taken in a session of its own (the handler's commits as it goes would drop a
lock held there). A claim that cannot get the lock returns without touching
anything: the other one owns the episode, and duplicating its work is the whole
thing being avoided.

**Nothing half-written is ever visible.** The encode goes into a sibling
``<id>.tmp-<job id>`` directory and is renamed over the real one only once the
playlist has been validated, so a reader sees a finished rendition or none at
all and never a directory being filled in. Every exit that is not a success —
a failed encode, a timeout, the worker being cancelled at shutdown — removes
that directory on the way out, so exhausting the attempts leaves the disk as it
found it.

**The commits are deliberate and frequent.** The handler commits its own
session as it goes rather than leaving one transaction open for twenty
minutes: the ``preparing`` state has to be visible to the show page
immediately, each progress write has to be visible while the next one is being
computed, and — the important one — the ``failed`` state and the stderr tail
have to be *written* before the exception is re-raised, because the runner
rolls the handler's session back on the way out.

**The semaphore is taken around ffmpeg only.** Probing, planning and the
database work happen outside it, so ``MAX_TRANSCODES`` counts encoders rather
than jobs. A job waiting for a slot sits in ``running`` with its heartbeat
going; it does not fail, and it does not give the slot up to a later, lower
priority episode.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from arc.config import Settings
from arc.core.text import keep_head_and_tail
from arc.models import (
    DEFAULT_SETTINGS,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    Rendition,
    Setting,
)
from arc.services.acquisition.states import transition
from arc.services.jobs.queue import ACTIVE_STATUSES
from arc.services.jobs.registry import JobContext, register
from arc.services.media.names import (
    EPISODE_KEY as KEY_EPISODE,
)
from arc.services.media.names import (
    TRANSCODE,
    enqueue_transcode,
    latest_transcode_jobs,
    output_dir_for,
    transcode_dedupe_key,
)
from arc.services.media.plan import (
    PLAYLIST_NAME,
    EncodeOptions,
    PlanError,
    TranscodePlan,
    build_plan,
)
from arc.services.media.probe import ffprobe_json
from arc.services.media.transcode import (
    STAGE_ENCODE,
    STAGE_PROBE,
    TranscodeError,
    TranscodeResult,
    transcode,
    transcode_semaphore,
    validate_output,
)

log = logging.getLogger(__name__)

#: How often the running job pushes ``jobs.locked_at`` forward. A minute: far
#: inside the two-hour ``WORKER_STALE_AFTER``, and cheap — one indexed UPDATE
#: against a row nothing else is touching.
HEARTBEAT_SECONDS = 60.0

#: How often the progress fraction is written, and how much it has to have
#: moved. Five seconds is finer than anybody watching a percentage needs; the
#: 2 % rule is what stops a short file writing forty rows in ten seconds.
PROGRESS_INTERVAL_SECONDS = 5.0
PROGRESS_STEP = 0.02

#: Payload keys. Named because the API reads them and a test asserts on them.
#: ``KEY_EPISODE`` is :data:`arc.services.media.names.EPISODE_KEY`, imported
#: rather than repeated: the enqueue side writes it and this side reads it.
KEY_PROGRESS = "progress"
KEY_STAGE = "stage"
KEY_ERROR = "error_tail"
KEY_FORCE = "force"
KEY_NOTES = "notes"

#: Stage written once the rendition row exists. Not one of the four working
#: stages: it means "there is nothing left to do", which the others do not.
STAGE_DONE = "done"

#: Cap on the stored failure detail. The API trims further for display; this
#: is the bound on what goes in the row. Both trims keep the head and drop the
#: middle (:mod:`arc.core.text`), so whatever survives still begins with the
#: sentence that names the failure.
MAX_ERROR_TAIL = 4000

#: First half of the advisory lock key, standing for "a transcode of". Postgres
#: advisory locks are a pair of 32-bit integers in one process-wide namespace,
#: so the pair has to be unique across everything Arc might ever lock: a
#: checksum of the job type is a stable, collision-shy way to say
#: ``('transcode', episode_id)`` in the two integers available. Folded into
#: ``int4``'s signed range, which is what ``pg_advisory_xact_lock`` takes.
TRANSCODE_LOCK_KEY: Final[int] = (zlib.crc32(TRANSCODE.encode()) + (1 << 31)) % (1 << 32) - (
    1 << 31
)

#: Suffix of the directory an encode is built in before it is renamed into
#: place. The job id is in it so that two claims cannot even collide by
#: accident, and a retry — which reuses the job row — reuses the directory and
#: starts it empty.
STAGING_SUFFIX = ".tmp-{job_id}"

#: What a stale ``.tmp-*`` directory is matched by when one is swept up. Only
#: a hard-killed worker leaves one; every ordinary exit removes its own.
STAGING_GLOB = "{episode_id}.tmp-*"

#: States a transcode will act on. ``matched`` is the ordinary one (FR-P1),
#: ``failed`` is a retry (FR-P4), ``preparing`` is a worker that died and came
#: back, and ``ready`` is a deliberate re-encode (FR-P5).
TRANSCODABLE: frozenset[EpisodeState] = frozenset(
    {
        EpisodeState.MATCHED,
        EpisodeState.PREPARING,
        EpisodeState.FAILED,
        EpisodeState.READY,
    }
)

#: States the startup sweep considers unfinished business.
SWEEPABLE: frozenset[EpisodeState] = frozenset(
    {EpisodeState.MATCHED, EpisodeState.PREPARING, EpisodeState.FAILED}
)

NO_SOURCE = "no media file is linked to this episode"

#: The two admin-editable language rules, in the order
#: :func:`language_rules` returns them.
LANGUAGE_KEYS: tuple[str, str] = ("sub_lang", "audio_lang")


def encode_options(settings: Settings) -> EncodeOptions:
    """The ffmpeg knobs from ``arc/config.py``, in the plan's shape."""
    return EncodeOptions(
        video_encoder=settings.ffmpeg_video_encoder,
        preset=settings.transcode_preset,
        crf=settings.transcode_crf,
        # An empty ``TRANSCODE_TUNE`` is how an operator turns tuning off, and
        # the dataclass says so with ``None`` rather than with "".
        tune=settings.transcode_tune or None,
        segment_seconds=settings.hls_segment_seconds,
        maxrate_kbps=settings.transcode_maxrate_kbps,
        bufsize_kbps=settings.transcode_bufsize_kbps,
    )


class _Reporter:
    """Writes progress and keeps the job's lock warm, in one place.

    Called from the ffmpeg output reader roughly twice a second. It throttles,
    so the database sees one write every :data:`PROGRESS_INTERVAL_SECONDS` or
    every :data:`PROGRESS_STEP` of movement, and it piggybacks the heartbeat on
    the same call: while ffmpeg is running, its own output is a perfectly good
    clock and no second timer is needed.

    :func:`_keep_lock_warm` is the *other* caller, and the only one, and it runs
    only while the handler is parked on ``Semaphore.acquire`` — where the
    handler itself touches neither the session nor this object. It is cancelled
    and awaited to a stop the instant the slot is taken, before anything else
    writes, because two coroutines writing through one ``AsyncSession`` really
    is a race; they simply never overlap.
    """

    def __init__(self, ctx: JobContext, payload: dict[str, Any]) -> None:
        self.ctx = ctx
        self.payload = payload
        self.last_write = 0.0
        self.last_fraction = -1.0
        self.last_heartbeat = monotonic()

    async def __call__(self, stage: str, fraction: float) -> None:
        now = monotonic()
        moved = abs(fraction - self.last_fraction) >= PROGRESS_STEP
        due = (now - self.last_write) >= PROGRESS_INTERVAL_SECONDS
        changed_stage = self.payload.get(KEY_STAGE) != stage
        if not (moved or due or changed_stage):
            return
        self.last_write = now
        self.last_fraction = fraction
        beat = (now - self.last_heartbeat) >= HEARTBEAT_SECONDS
        if beat:
            self.last_heartbeat = now
        await self.write(stage=stage, progress=fraction, heartbeat=beat)

    async def write(
        self,
        *,
        stage: str,
        progress: float | None = None,
        heartbeat: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Write the payload (and optionally the lock) in a committed step."""
        self.payload[KEY_STAGE] = stage
        if progress is not None:
            self.payload[KEY_PROGRESS] = round(progress, 4)
        if extra:
            self.payload.update(extra)
        values: dict[str, Any] = {"payload": dict(self.payload)}
        if heartbeat:
            values["locked_at"] = datetime.now(UTC)
        await self.ctx.session.execute(
            update(Job).where(Job.id == self.ctx.job.id).values(**values)
        )
        await self.ctx.session.commit()


async def _source_for(session: AsyncSession, episode_id: int) -> MediaFile | None:
    """The file this episode's rendition is made from.

    The newest linked row wins: a re-download replaces the file rather than
    the episode, and the last one linked is the one a person confirmed.
    """
    found: MediaFile | None = await session.scalar(
        select(MediaFile)
        .where(MediaFile.episode_id == episode_id)
        .order_by(MediaFile.id.desc())
        .limit(1)
    )
    return found


async def _rendition_for(session: AsyncSession, episode_id: int) -> Rendition | None:
    found: Rendition | None = await session.scalar(
        select(Rendition).where(Rendition.episode_id == episode_id)
    )
    return found


async def _save_rendition(
    session: AsyncSession,
    episode_id: int,
    *,
    output_dir: Path,
    duration: float | None,
    width: int | None,
    height: int | None,
    subtitle_lang: str | None,
    audio_lang: str | None,
) -> Rendition:
    """Create or update the one ``renditions`` row for this episode.

    Written only on success, and only after the playlist has been checked: the
    row is Arc's statement that the episode is playable, and a row for an
    episode that is not is worse than no row at all.
    """
    rendition = await _rendition_for(session, episode_id)
    if rendition is None:
        rendition = Rendition(episode_id=episode_id, dir="", playlist_path="")
        session.add(rendition)
    rendition.dir = str(output_dir)
    rendition.playlist_path = str(output_dir / PLAYLIST_NAME)
    rendition.duration = duration
    rendition.width = width
    rendition.height = height
    rendition.subtitle_lang = subtitle_lang
    rendition.audio_lang = audio_lang
    rendition.ready_at = datetime.now(UTC)
    await session.flush()
    return rendition


async def _plan_for(
    settings: Settings, source: Path, output_dir: Path, *, sub_lang: str, audio_lang: str
) -> TranscodePlan:
    payload = await ffprobe_json(source, binary=settings.ffprobe_bin)
    return build_plan(
        payload,
        source=source,
        output_dir=output_dir,
        sub_lang=sub_lang,
        audio_lang=audio_lang,
    )


async def language_rules(session: AsyncSession) -> tuple[str, str]:
    """``(sub_lang, audio_lang)`` from the ``settings`` table (FR-P2, FR-D2).

    Admin-editable, so it is read per job rather than cached: an admin who
    switches the subtitle language expects the next episode to follow, not the
    next restart. A row of the wrong JSON type is ignored with a warning and
    the default is used — the same rule
    :mod:`arc.services.acquisition.rules` applies to its own keys, for the same
    reason: one bad hand-edited row must not stop every transcode.
    """
    rows = await session.execute(
        select(Setting.key, Setting.value).where(Setting.key.in_(LANGUAGE_KEYS))
    )
    stored = {key: value for key, value in rows.all()}
    chosen: list[str] = []
    for key in LANGUAGE_KEYS:
        value = stored.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            log.warning("setting is not a language string, using the default", extra={"key": key})
            value = None
        chosen.append(str(value or DEFAULT_SETTINGS[key]))
    return chosen[0], chosen[1]


def _engine_of(session: AsyncSession) -> AsyncEngine | None:
    """The engine ``session`` was built from, when there is one.

    ``AsyncSession.bind`` is set when the session came from an engine, which
    is how the worker and the API both make theirs. A session bound to a
    *connection* — the rolled-back one some unit tests use — has no engine to
    open a second connection on, and an advisory lock taken on that same
    connection would be no lock at all, so those callers get ``None`` and
    :func:`episode_lock` waves them through.
    """
    bind = getattr(session, "bind", None)
    return bind if isinstance(bind, AsyncEngine) else None


@asynccontextmanager
async def episode_lock(session: AsyncSession, episode_id: int) -> AsyncIterator[bool]:
    """Hold ``('transcode', episode_id)`` for the length of the block.

    Yields whether the lock was taken. ``False`` means another claim of this
    episode is running *right now* — on this worker or another — and the caller
    must do nothing at all rather than wait: waiting would hold a queue slot
    for twenty minutes to then find the work already done.

    Transaction-level (``pg_advisory_xact_lock``) rather than session-level, so
    that a worker killed mid-encode releases it when its connection dies rather
    than leaving the episode locked until somebody notices. Held in a session
    of its own for the opposite reason: the handler's session commits every few
    seconds to publish progress, and each of those commits would end the
    transaction the lock is attached to.
    """
    engine = _engine_of(session)
    if engine is None:
        yield True
        return
    async with AsyncSession(engine) as guard:
        held = bool(
            await guard.scalar(
                select(func.pg_try_advisory_xact_lock(TRANSCODE_LOCK_KEY, episode_id))
            )
        )
        try:
            yield held
        finally:
            # Ends the transaction, and with it the lock. Never a commit: this
            # session exists to hold a lock and writes nothing.
            await guard.rollback()


async def _keep_lock_warm(reporter: _Reporter) -> None:
    """Push ``locked_at`` forward while the job waits for an encode slot.

    Runs only between "planned" and "encoding", which on a busy host is the
    longest the job is ever quiet: nothing is being written, ffmpeg has not
    started, and the stale sweep cannot tell that from a dead worker. Cancelled
    by :func:`_stop` the moment the slot is taken, after which the reporter's
    own beats take over.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        await reporter.write(stage=STAGE_ENCODE, heartbeat=True)


async def _stop(task: asyncio.Task[None]) -> None:
    """Cancel ``task`` and wait until it has actually stopped.

    The waiting is the point. ``cancel()`` only schedules the cancellation, and
    returning while the task is still inside ``_Reporter.write`` would put two
    coroutines on one session — exactly what the reporter's docstring says must
    not happen. Idempotent, so the success path and the ``finally`` can both
    call it.
    """
    task.cancel()
    # Both, deliberately: ``CancelledError`` is a ``BaseException`` and is the
    # ordinary outcome here, and anything else the task managed to raise is
    # already lost work that must not replace the caller's own exception.
    with suppress(asyncio.CancelledError, Exception):
        await task


def _discard(path: Path) -> None:
    """Remove a half-written rendition. Never raises: it is housekeeping."""
    shutil.rmtree(path, ignore_errors=True)


def _publish(staging: Path, output_dir: Path, result: TranscodeResult) -> TranscodeResult:
    """Rename a validated encode into place; return the result relocated.

    The rename is what makes the swap atomic from a reader's point of view.
    Nothing ever opens a rendition directory that is being filled in: a request
    for ``<id>/index.m3u8`` finds the previous encode's playlist or this one's,
    with no moment in between. (A ``force`` re-encode has already thrown the
    previous one away by the time it gets here — FR-P5 asks for exactly that —
    so what it replaces is usually nothing at all.)

    ``rename`` will not replace a non-empty directory, so the old one goes
    first. That is safe here and nowhere else, because the advisory lock means
    nothing is going to put it back between the two calls.
    """
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    _discard(output_dir)
    os.replace(staging, output_dir)
    return replace(result, output_dir=output_dir, playlist=output_dir / PLAYLIST_NAME)


@register(TRANSCODE)
async def transcode_episode(ctx: JobContext) -> None:
    """Prepare one episode for playback (FR-P1 … FR-P5).

    Idempotent in the way that matters for a job this expensive: a rendition
    that is already on disk and already parses is *not* re-encoded, whatever
    the episode's state says, unless the payload asks for it. A worker that
    was killed at 95 % has nothing to reuse and starts again — segment-level
    resume would mean trusting a half-written playlist, which is the one thing
    the validation exists to refuse.

    The lock is taken before anything is read, not after: every check below —
    "is there a rendition already?", "what state is the episode in?" — is only
    worth making if this claim is the one that gets to act on the answer.
    """
    episode_id = int(ctx.payload[KEY_EPISODE])
    async with episode_lock(ctx.session, episode_id) as owned:
        if not owned:
            ctx.log.info(
                "another claim is already transcoding this episode; leaving it to that one",
                extra={"episode_id": episode_id, "job_id": ctx.job.id},
            )
            return
        await _prepare(ctx, episode_id)


async def _prepare(ctx: JobContext, episode_id: int) -> None:
    """The body of :func:`transcode_episode`, under the episode's lock."""
    force = bool(ctx.payload.get(KEY_FORCE))
    settings = ctx.settings
    output_dir = output_dir_for(settings, episode_id)

    episode = await ctx.session.get(Episode, episode_id)
    if episode is None:
        ctx.log.info("episode went away before it was transcoded", extra={"id": episode_id})
        return
    if episode.state not in TRANSCODABLE:
        ctx.log.info(
            "episode is not in a state to be transcoded",
            extra={"episode_id": episode_id, "state": episode.state.value},
        )
        return

    payload = dict(ctx.payload)
    payload[KEY_EPISODE] = episode_id
    reporter = _Reporter(ctx, payload)

    if force:
        existing = await _rendition_for(ctx.session, episode_id)
        if existing is not None:
            await ctx.session.delete(existing)
        shutil.rmtree(output_dir, ignore_errors=True)
        await ctx.session.commit()
        ctx.log.info("re-encoding on request", extra={"episode_id": episode_id})
    else:
        # Asked *before* the state moves, not after: a job that turns out to
        # have nothing to do must not flick a playable episode through
        # ``preparing`` on its way back to ``ready``. Somebody is looking at
        # that badge.
        rendition = await _rendition_for(ctx.session, episode_id)
        segments = await validate_output(output_dir, ffprobe=settings.ffprobe_bin)
        if rendition is not None and segments:
            if episode.state is not EpisodeState.READY:
                transition(episode, EpisodeState.PREPARING, reason="rendition already on disk")
                transition(episode, EpisodeState.READY, reason="rendition already on disk")
                await ctx.session.commit()
            await reporter.write(stage=STAGE_DONE, progress=1.0, extra={KEY_ERROR: None})
            ctx.log.info(
                "rendition already present; nothing to encode",
                extra={"episode_id": episode_id, "segments": segments},
            )
            return

    transition(episode, EpisodeState.PREPARING, reason=f"transcode job {ctx.job.id}")
    await ctx.session.commit()
    await reporter.write(stage=STAGE_PROBE, progress=0.0, extra={KEY_ERROR: None})

    media_file = await _source_for(ctx.session, episode_id)
    if media_file is None:
        await _fail(ctx, episode, reporter, NO_SOURCE)
        raise TranscodeError(NO_SOURCE)

    source = Path(media_file.path)
    if not source.exists():
        message = f"the source file is missing: {source}"
        await _fail(ctx, episode, reporter, message)
        raise TranscodeError(message)

    # Anything a hard-killed worker left behind for this episode. Ordinary
    # exits clean up after themselves in the ``finally`` below; a ``SIGKILL``
    # cannot, and the directory would then sit there until somebody looked.
    for stale in output_dir.parent.glob(STAGING_GLOB.format(episode_id=episode_id)):
        _discard(stale)
    staging = output_dir.with_suffix(STAGING_SUFFIX.format(job_id=ctx.job.id))

    sub_lang, audio_lang = await language_rules(ctx.session)
    try:
        plan = await _plan_for(settings, source, staging, sub_lang=sub_lang, audio_lang=audio_lang)
    except PlanError as exc:
        await _fail(ctx, episode, reporter, str(exc))
        raise TranscodeError(str(exc)) from exc

    ctx.log.info("transcode planned", extra={"episode_id": episode_id, **plan.as_dict()})
    semaphore = transcode_semaphore(settings.max_transcodes)
    # One beat before the wait and a task beating through it: acquiring a slot
    # can take as long as every encode ahead of it, and a queued job that says
    # nothing looks exactly like a dead one to ``requeue_stale``.
    await reporter.write(stage=STAGE_ENCODE, progress=0.0, heartbeat=True)
    warm = asyncio.create_task(_keep_lock_warm(reporter))
    published = False
    try:
        async with semaphore:
            await _stop(warm)
            result = await transcode(
                plan,
                options=encode_options(settings),
                ffmpeg=settings.ffmpeg_bin,
                ffprobe=settings.ffprobe_bin,
                timeout=settings.transcode_timeout_seconds,
                on_progress=reporter,
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
        # Every exit that is not a published rendition — a failed encode, a
        # timeout, the worker being cancelled at shutdown — takes the work
        # directory and the half-written output with it, so a job that
        # exhausts its attempts leaves no disused segments behind.
        await _stop(warm)
        if not published:
            _discard(staging)

    await _succeed(ctx, episode, reporter, result)


async def _fail(
    ctx: JobContext,
    episode: Episode,
    reporter: _Reporter,
    message: str,
    *,
    tail: str = "",
) -> None:
    """Record the failure on the episode and the job, and commit it.

    Committed here rather than left to the runner because the runner rolls
    this session back: the exception that follows is what schedules the retry,
    and the state a user is shown while they wait has to survive it (FR-P4).

    ``message`` leads and survives the cap whatever the tail does
    (:mod:`arc.core.text`): it is the half that says *what* failed, and the
    stored string is trimmed again, harder, before it reaches a show page.
    """
    detail = keep_head_and_tail(message, tail, limit=MAX_ERROR_TAIL)
    try:
        transition(episode, EpisodeState.FAILED, reason=message)
        await ctx.session.commit()
    except Exception:  # pragma: no cover - a rolled-back session, mid-shutdown
        ctx.log.exception("could not mark the episode failed", extra={"episode_id": episode.id})
        await ctx.session.rollback()
    await reporter.write(
        stage=str(reporter.payload.get(KEY_STAGE) or STAGE_ENCODE),
        extra={KEY_ERROR: detail},
    )
    ctx.log.error(
        "transcode failed",
        extra={"episode_id": episode.id, "error": message, "tail": tail[-500:]},
    )


async def _succeed(
    ctx: JobContext, episode: Episode, reporter: _Reporter, result: TranscodeResult
) -> None:
    await _save_rendition(
        ctx.session,
        episode.id,
        output_dir=result.output_dir,
        duration=result.duration,
        width=result.width,
        height=result.height,
        subtitle_lang=result.subtitle_lang,
        audio_lang=result.audio_lang,
    )
    transition(episode, EpisodeState.READY, reason="transcode finished")
    await ctx.session.commit()
    await reporter.write(
        stage=STAGE_DONE,
        progress=1.0,
        extra={KEY_ERROR: None, KEY_NOTES: list(result.notes)},
    )
    ctx.log.info(
        "episode ready",
        extra={
            "episode_id": episode.id,
            "seconds": round(result.seconds, 1),
            "segments": result.segments,
            "fonts": result.fonts,
            "duration": result.duration,
            "width": result.width,
            "height": result.height,
            "subtitle_lang": result.subtitle_lang,
            "audio_lang": result.audio_lang,
            "notes": list(result.notes),
        },
    )


# --- The startup sweep ------------------------------------------------------


async def sweep_transcodes(session: AsyncSession) -> int:
    """Queue the transcodes a restart would otherwise lose. Returns the count.

    Three cases, all of which are "the worker was not running when it should
    have been":

    * an episode ``matched`` with no rendition and no queued transcode — the
      link happened while the worker was down, or its job row was lost;
    * one stuck in ``preparing`` with nothing running — the worker was killed
      mid-encode and its job row was failed by the stale sweep;
    * one in ``failed`` whose job still has attempts left — the retry was due
      when the process went away.

    An episode whose transcode has genuinely run out of attempts is **not**
    requeued: that is a job for a person (FR-P4's "with retry"), and a sweep
    that revived it every restart would hide the failure instead of showing it.
    """
    episodes = (
        await session.scalars(
            select(Episode)
            .outerjoin(Rendition, Rendition.episode_id == Episode.id)
            .where(Episode.state.in_(SWEEPABLE), Rendition.id.is_(None))
            .order_by(Episode.id)
        )
    ).all()
    if not episodes:
        return 0

    jobs = await latest_transcode_jobs(session, [episode.id for episode in episodes])
    queued = 0
    for episode in episodes:
        job = jobs.get(episode.id)
        if job is not None:
            if job.status in ACTIVE_STATUSES:
                continue
            if job.status is JobStatus.FAILED and job.attempts >= job.max_attempts:
                log.info(
                    "transcode has used up its attempts; not requeued",
                    extra={"episode_id": episode.id, "job_id": job.id},
                )
                continue
        await enqueue_transcode(session, episode.id)
        queued += 1
    if queued:
        await session.commit()
        log.info("transcodes queued at startup", extra={"count": queued})
    return queued


__all__ = [
    "HEARTBEAT_SECONDS",
    "KEY_EPISODE",
    "KEY_ERROR",
    "KEY_FORCE",
    "KEY_NOTES",
    "KEY_PROGRESS",
    "KEY_STAGE",
    "MAX_ERROR_TAIL",
    "NO_SOURCE",
    "PROGRESS_INTERVAL_SECONDS",
    "PROGRESS_STEP",
    "STAGE_DONE",
    "STAGING_GLOB",
    "STAGING_SUFFIX",
    "SWEEPABLE",
    "TRANSCODABLE",
    "TRANSCODE",
    "TRANSCODE_LOCK_KEY",
    "LANGUAGE_KEYS",
    "encode_options",
    "enqueue_transcode",
    "episode_lock",
    "language_rules",
    "output_dir_for",
    "sweep_transcodes",
    "transcode_dedupe_key",
    "transcode_episode",
]
