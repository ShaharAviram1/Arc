"""Running ffmpeg: fonts out, subtitles out, HLS in (FR-P1, architecture.md §5.3).

:mod:`arc.services.media.plan` decides *what*; this module does it. Four steps,
in this order, all with the rendition directory as the working directory:

1. **fonts** — every attachment in the container is dumped into ``_work/fonts``
   under a name Arc chose. A fansub MKV carries twenty-odd TTFs, and an ASS
   script that asks for a font libass cannot find is rendered in the fallback
   face at the wrong size, which is worse than no typesetting at all.
2. **subtitle** — the chosen track is extracted to ``_work/sub.ass`` (or
   ``.srt``). See :data:`~arc.services.media.plan.WORK_DIR` for why this is not
   done with ``subtitles=<source>:si=<n>``.
3. **encode** — one ffmpeg producing ``index.m3u8``, ``init.mp4`` and
   ``seg_00000.m4s`` onwards, with progress on stdout and the last forty lines
   of stderr kept for the failure message.
4. **package** — the output is checked (playlist, init segment, at least one
   segment, and an ffprobe that can read the playlist) before anything is
   written to the database. An episode marked ``ready`` whose playlist does not
   parse is a black player, and the check costs a hundred milliseconds.

Two failure rules.

**A missing font or an unextractable subtitle is not a failed transcode.** Both
degrade: the encode runs without the filter and the rendition records
``subtitle_lang = null``, which is exactly what FR-P2 asks for when there is no
usable track. A failed *encode* is a failed job, retried by the runner with
backoff and shown to the user with the tail of ffmpeg's own complaint (FR-P4).

**Concurrency is capped per process, not per queue.** ``MAX_TRANSCODES`` is a
statement about the host's cores; the worker's own ``WORKER_CONCURRENCY`` is
about the queue. A job that cannot get a slot *waits* rather than failing, and
its row stays ``running`` while it does. Nothing in this module keeps that row
alive, though: :func:`transcode_semaphore` hands out a plain
``asyncio.Semaphore`` and a coroutine parked on it produces no output to
report. Keeping ``locked_at`` warm through the wait is the caller's job, and
:mod:`arc.services.media.jobs` does it with a task of its own — without which
a queue two deep in twenty-minute encodes would have the stale sweep requeue
the third one underneath itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter
from typing import Final

from arc.services.media.plan import (
    FONTS_DIR,
    INIT_NAME,
    PLAYLIST_NAME,
    SEGMENT_GLOB,
    WORK_DIR,
    EncodeOptions,
    TranscodePlan,
    encode_args,
    font_extract_args,
    subtitle_extract_args,
)
from arc.services.media.probe import ffprobe_json

log = logging.getLogger(__name__)

#: Lines of ffmpeg's stderr kept for the failure message. Forty is enough for
#: the complaint and the stream layout that led to it, and short enough to sit
#: in a job payload and be read on a phone.
STDERR_TAIL_LINES: Final[int] = 40

#: Cap on the stored tail, in characters. ffmpeg can emit long lines.
MAX_TAIL_CHARS: Final[int] = 4000

#: Stream reader buffer. The default 64 KiB is plenty for ffmpeg's line-based
#: output, but a `ValueError` from one overlong line would lose the rest of the
#: stream, and losing the *end* of stderr is losing the error message.
STREAM_LIMIT: Final[int] = 1 << 20

#: Seconds to let a killed ffmpeg actually die before giving up on it.
KILL_GRACE: Final[float] = 10.0

#: How long the two short helper runs may take. Dumping twenty fonts and
#: demuxing one subtitle track are both seconds; ten minutes is "something is
#: badly wrong", not "this file is big".
HELPER_TIMEOUT: Final[float] = 600.0

#: Stage names written to the job payload, in the order they happen.
STAGE_PROBE: Final[str] = "probe"
STAGE_FONTS: Final[str] = "fonts"
STAGE_ENCODE: Final[str] = "encode"
STAGE_PACKAGE: Final[str] = "package"

#: ``stage, fraction`` → somewhere the user can see it. Async because the only
#: implementation writes a row.
type ProgressCallback = Callable[[str, float], Awaitable[None]]


class TranscodeError(RuntimeError):
    """ffmpeg could not produce a rendition.

    Carries ``error_tail`` — the last lines ffmpeg wrote — because that is the
    one thing a person needs to see and the traceback of a subprocess wrapper
    is not it (FR-P4).
    """

    def __init__(self, message: str, *, error_tail: str = "") -> None:
        super().__init__(message)
        self.error_tail = error_tail


@dataclass(frozen=True, slots=True)
class TranscodeResult:
    """What the handler writes to ``renditions`` (architecture.md §4)."""

    output_dir: Path
    playlist: Path
    duration: float | None
    width: int | None
    height: int | None
    subtitle_lang: str | None
    audio_lang: str | None
    segments: int
    fonts: int
    seconds: float
    notes: tuple[str, ...] = ()


# --- The per-process ffmpeg cap ---------------------------------------------
#
# Keyed by event loop rather than kept in a module-level variable, because the
# test suite runs each test in a loop of its own and an ``asyncio.Semaphore``
# with waiters belongs to the loop it was first awaited in. The worker has one
# loop for its whole life, so in production this is the single shared cap that
# ``MAX_TRANSCODES`` describes.
_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def transcode_semaphore(limit: int) -> asyncio.Semaphore:
    """The process's ffmpeg slot semaphore, created on first use."""
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(max(int(limit), 1))
        _semaphores[loop] = semaphore
    return semaphore


def reset_semaphore() -> None:
    """Forget every cached semaphore. For tests only."""
    _semaphores.clear()


# --- Progress ---------------------------------------------------------------


def parse_progress_line(line: str) -> tuple[str, str] | None:
    """One ``key=value`` line of ``-progress`` output, or ``None``.

    ffmpeg writes a block of these every half second and terminates each block
    with ``progress=continue`` (or ``progress=end``). Anything that is not a
    single ``=``-separated pair is not ours.
    """
    text = line.strip()
    if not text or "=" not in text:
        return None
    key, _, value = text.partition("=")
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    return key, value


def _timecode_seconds(value: str) -> float | None:
    """``HH:MM:SS.micros`` → seconds. ffmpeg writes ``N/A`` before the first frame."""
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (float(part) for part in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def progress_seconds(fields: dict[str, str]) -> float | None:
    """How far into the output ffmpeg has got, in seconds.

    ``out_time_us`` first because it is unambiguous. ``out_time_ms`` is read
    as microseconds too — ffmpeg has emitted microseconds under that name
    since 2017 and fixing it would break every parser — but only as a fallback
    behind the human-readable ``out_time``, so the ambiguity never decides.
    """
    raw = fields.get("out_time_us")
    if raw and raw != "N/A":
        try:
            return int(raw) / 1_000_000
        except ValueError:
            pass
    raw = fields.get("out_time")
    if raw and raw != "N/A":
        seconds = _timecode_seconds(raw)
        if seconds is not None:
            return seconds
    raw = fields.get("out_time_ms")
    if raw and raw != "N/A":
        try:
            return int(raw) / 1_000_000
        except ValueError:
            pass
    return None


class ProgressReader:
    """``-progress`` lines in, a 0..1 fraction out.

    Fed one line at a time; answers only when a block completes
    (``progress=…``), which is once every half second rather than once per
    field. The fraction never goes backwards and never exceeds 1: a duration
    read off the container is an estimate, and a progress bar that reaches
    103 % and then drops is a bug report.

    A source whose duration ffprobe would not state still answers — with a
    flat ``0.0``. The caller uses these answers as its heartbeat as well as
    its percentage (:mod:`arc.services.media.jobs`), and a file with no
    duration must not be a file whose job looks abandoned.
    """

    def __init__(self, duration: float | None) -> None:
        self.duration = duration if duration and duration > 0 else None
        self.fields: dict[str, str] = {}
        self.seconds: float = 0.0
        self.fraction: float = 0.0
        self.finished = False

    def feed(self, line: str) -> float | None:
        pair = parse_progress_line(line)
        if pair is None:
            return None
        key, value = pair
        self.fields[key] = value
        if key != "progress":
            return None

        if value == "end":
            self.finished = True
        seconds = progress_seconds(self.fields)
        self.fields.clear()
        if seconds is not None:
            self.seconds = max(self.seconds, seconds)
        if self.duration is None:
            return self.fraction
        fraction = min(max(self.seconds / self.duration, 0.0), 1.0)
        self.fraction = max(self.fraction, fraction)
        return self.fraction


# --- Running the binary -----------------------------------------------------


def _tail(lines: deque[str]) -> str:
    text = "\n".join(lines)
    return text[-MAX_TAIL_CHARS:]


async def _drain(reader: asyncio.StreamReader | None, sink: deque[str]) -> None:
    """Keep the last :data:`STDERR_TAIL_LINES` lines of a stream."""
    if reader is None:
        return
    while True:
        try:
            raw = await reader.readline()
        except ValueError, asyncio.LimitOverrunError:  # pragma: no cover - absurd line
            continue
        if not raw:
            return
        text = raw.decode("utf-8", "replace").strip()
        if text:
            sink.append(text)


async def _read_progress(
    reader: asyncio.StreamReader | None,
    tracker: ProgressReader,
    on_progress: ProgressCallback | None,
    stage: str,
) -> None:
    if reader is None:
        return
    while True:
        try:
            raw = await reader.readline()
        except ValueError, asyncio.LimitOverrunError:  # pragma: no cover - absurd line
            continue
        if not raw:
            return
        fraction = tracker.feed(raw.decode("utf-8", "replace"))
        if fraction is not None and on_progress is not None:
            await on_progress(stage, fraction)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    """``SIGKILL`` the whole process group ffmpeg was started in.

    ``process.kill()`` signals ffmpeg alone, which is not enough: it is started
    with ``start_new_session=True`` precisely so that its children can be
    reached, and an encoder helper that outlives its parent keeps a core busy
    for the rest of the run. Falls back to the single process if the group has
    already gone (the ``pid`` would then be reused, so ``killpg`` is only ever
    called while the process object still owns it).
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except ProcessLookupError, PermissionError, OSError:  # pragma: no cover - already gone
        with suppress(ProcessLookupError):
            process.kill()


async def run_ffmpeg(
    args: list[str],
    *,
    binary: str,
    cwd: Path,
    timeout: float,
    duration: float | None = None,
    on_progress: ProgressCallback | None = None,
    stage: str = STAGE_ENCODE,
    check: bool = True,
) -> str:
    """Run ffmpeg to completion and return the tail of its stderr.

    Raises :class:`TranscodeError` on a non-zero exit (unless ``check`` is
    false), on a timeout, and when the binary is not there at all. Never runs
    through a shell: ``args`` is an argument vector and the source path in it
    is the only string Arc did not write.
    """
    executable = shutil.which(binary)
    if executable is None:
        raise TranscodeError(f"{binary} is not installed or not on PATH")

    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    tracker = ProgressReader(duration)
    started = perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
            # Its own process group, so a timeout can kill everything ffmpeg
            # started rather than only ffmpeg. x264 and the hardware encoders
            # fork helpers, and a killed parent leaves those holding the cores
            # this cap exists to ration — and the pipes, so the reader would
            # never see EOF either.
            start_new_session=True,
        )
    except OSError as exc:
        raise TranscodeError(f"could not start {binary}: {exc}") from exc

    readers = asyncio.gather(
        _read_progress(process.stdout, tracker, on_progress, stage),
        _drain(process.stderr, tail),
    )
    try:
        await asyncio.wait_for(readers, timeout=timeout)
        code = await asyncio.wait_for(process.wait(), timeout=KILL_GRACE)
    except (TimeoutError, asyncio.CancelledError) as exc:
        readers.cancel()
        with_kill = "timed out" if isinstance(exc, TimeoutError) else "was cancelled"
        _kill_group(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=KILL_GRACE)
        except TimeoutError:  # pragma: no cover - an unkillable process
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise TranscodeError(
            f"{binary} {with_kill} after {timeout:.0f}s", error_tail=_tail(tail)
        ) from exc

    elapsed = perf_counter() - started
    log.debug(
        "ffmpeg finished",
        extra={"stage": stage, "code": code, "seconds": round(elapsed, 1), "binary": binary},
    )
    if check and code != 0:
        raise TranscodeError(f"{binary} exited {code}", error_tail=_tail(tail))
    return _tail(tail)


# --- What this ffmpeg can do ------------------------------------------------
#
# Burning subtitles in needs the ``ass`` (or ``subtitles``) filter, which needs
# ffmpeg to have been built ``--enable-libass``. Debian's package is, and so is
# Arc's image; Homebrew's plain ``ffmpeg`` formula is **not** (its ``ffmpeg
# -full`` is), and neither are most of the static builds people download. Left
# unchecked, that produces "Error parsing filterchain" on every episode, which
# reads like a bug in Arc rather than a missing build flag. Asked once per
# process per binary, because ``ffmpeg -filters`` is a fork and this answer
# cannot change while the process runs.
_filters: dict[str, frozenset[str]] = {}


async def available_filters(binary: str) -> frozenset[str]:
    """Every filter this ffmpeg has, from ``ffmpeg -filters``. Cached."""
    cached = _filters.get(binary)
    if cached is not None:
        return cached
    executable = shutil.which(binary)
    if executable is None:
        raise TranscodeError(f"{binary} is not installed or not on PATH")
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-hide_banner",
            "-filters",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=HELPER_TIMEOUT)
    except (OSError, TimeoutError) as exc:  # pragma: no cover - a broken binary
        raise TranscodeError(f"could not ask {binary} what it supports: {exc}") from exc

    names: set[str] = set()
    for line in stdout.decode("utf-8", "replace").splitlines():
        # " T.. ass              V->V       Render ASS subtitles onto input."
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) <= 4 and not line.startswith("Filters:"):
            names.add(parts[1])
    found = frozenset(names)
    _filters[binary] = found
    return found


def reset_filters() -> None:
    """Forget what each binary supports. For tests only."""
    _filters.clear()


NO_LIBASS = (
    "{binary} has no {filter!r} filter: it was built without libass, so subtitles "
    "cannot be burned in. Install a full build (Debian's ffmpeg package, or "
    "'brew install ffmpeg-full') and point FFMPEG_BIN at it."
)


async def require_subtitle_filter(binary: str, subtitle_file: str) -> None:
    """Raise unless this ffmpeg can render ``subtitle_file`` into the picture."""
    name = "ass" if subtitle_file.endswith(".ass") else "subtitles"
    if name not in await available_filters(binary):
        raise TranscodeError(NO_LIBASS.format(binary=binary, filter=name))


# --- The four steps ---------------------------------------------------------


def prepare_directories(plan: TranscodePlan) -> Path:
    """Empty the rendition directory and make the work directory inside it.

    Emptied rather than reused: a retry after a half-written encode would
    otherwise leave the previous run's segments beside the new ones, and the
    playlist names them by index, so a shorter second encode would leave stale
    segments that the player never asks for and retention never deletes.
    """
    output = plan.output_dir
    if output.exists():
        shutil.rmtree(output)
    work = output / WORK_DIR
    (work / "fonts").mkdir(parents=True, exist_ok=True)
    return work


def clean_work(plan: TranscodePlan) -> None:
    """Remove the work directory. Never raises: it is housekeeping."""
    try:
        shutil.rmtree(plan.output_dir / WORK_DIR, ignore_errors=True)
    except OSError:  # pragma: no cover - ignore_errors already covers this
        log.debug("could not remove the work directory", extra={"dir": str(plan.output_dir)})


async def extract_fonts(plan: TranscodePlan, *, binary: str) -> int:
    """Dump the container's attachments; return how many landed.

    Best effort by design (see the module docstring): a container with no
    attachments, an ffmpeg that dislikes one of them, or a name that sanitised
    to nothing all end the same way — a count, a log line, and an encode that
    goes ahead with whatever fonts are on the host.
    """
    args = font_extract_args(plan)
    if args is None:
        return 0
    fonts = plan.output_dir / FONTS_DIR
    try:
        await run_ffmpeg(
            args,
            binary=binary,
            cwd=plan.output_dir,
            timeout=HELPER_TIMEOUT,
            stage=STAGE_FONTS,
            # ffmpeg reports "Output file is empty" on a zero-length null
            # output as a warning on some builds and an error on others; the
            # attachments are already written either way, so the files on disk
            # are the result, not the exit code.
            check=False,
        )
    except TranscodeError as exc:
        log.warning(
            "font extraction failed; encoding with the host's fonts",
            extra={"source": str(plan.source), "error": str(exc)},
        )
        return 0
    found = sorted(path for path in fonts.glob("*") if path.is_file())
    return len(found)


async def extract_subtitle(plan: TranscodePlan, *, binary: str) -> str | None:
    """Write the chosen subtitle track to its own file; return its path.

    ``None`` when there was no track to take, or when taking it failed — the
    caller then encodes without the burn-in filter rather than failing the
    episode (FR-P2).
    """
    args = subtitle_extract_args(plan)
    target = plan.subtitle_file
    if args is None or target is None:
        return None
    try:
        await run_ffmpeg(
            args,
            binary=binary,
            cwd=plan.output_dir,
            timeout=HELPER_TIMEOUT,
            stage=STAGE_FONTS,
        )
    except TranscodeError as exc:
        log.warning(
            "subtitle extraction failed; encoding without subtitles",
            extra={"source": str(plan.source), "error": str(exc), "tail": exc.error_tail},
        )
        return None
    written = plan.output_dir / target
    if not written.exists() or written.stat().st_size == 0:
        log.warning(
            "the extracted subtitle file is empty; encoding without subtitles",
            extra={"source": str(plan.source)},
        )
        return None
    return target


def output_files(output_dir: Path) -> tuple[Path, Path, list[Path]]:
    """``(playlist, init segment, segments)`` as they should be on disk."""
    return (
        output_dir / PLAYLIST_NAME,
        output_dir / INIT_NAME,
        sorted(output_dir.glob(SEGMENT_GLOB)),
    )


async def validate_output(output_dir: Path, *, ffprobe: str = "ffprobe") -> int:
    """Check a finished rendition; return the segment count, or 0 if it is bad.

    Four things, cheapest first: the playlist exists and is not empty, the
    initialisation segment exists, there is at least one media segment, and
    ffprobe can open the playlist and find a video stream in it. The last one
    is what catches an encode killed between writing the playlist and
    finishing the last segment.
    """
    playlist, init, segments = output_files(output_dir)
    if not playlist.exists() or playlist.stat().st_size == 0:
        return 0
    if not init.exists() or init.stat().st_size == 0:
        return 0
    if not segments:
        return 0
    payload = await ffprobe_json(playlist, binary=ffprobe)
    if not payload:
        return 0
    streams = payload.get("streams") or []
    if not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams
    ):
        return 0
    return len(segments)


async def transcode(
    plan: TranscodePlan,
    *,
    options: EncodeOptions,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    timeout: float = 10800.0,
    on_progress: ProgressCallback | None = None,
) -> TranscodeResult:
    """Fonts, subtitle, encode, check. The whole of a transcode.

    The caller holds the semaphore and owns the job row; this function owns
    the directory and the subprocesses, and leaves the rendition directory
    either valid or unusable-but-obvious — never valid-looking and wrong.
    """
    started = perf_counter()
    prepare_directories(plan)

    if on_progress is not None:
        await on_progress(STAGE_FONTS, 0.0)
    fonts = await extract_fonts(plan, binary=ffmpeg)
    subtitle_file = await extract_subtitle(plan, binary=ffmpeg)
    if subtitle_file is not None:
        # Before the twenty minutes, not after: an ffmpeg without libass is a
        # deployment mistake, and finding out at the end of the encode wastes
        # the encode and hides the reason in a filter-graph parse error.
        await require_subtitle_filter(ffmpeg, subtitle_file)
    notes = list(plan.notes)
    if plan.subtitle is not None and subtitle_file is None:
        notes.append("the subtitle track could not be extracted; nothing was burned in")

    if on_progress is not None:
        await on_progress(STAGE_ENCODE, 0.0)
    # ``replace`` rather than a fresh ``EncodeOptions(...)`` listing every
    # field: the two the caller could not know — where the subtitle landed and
    # whether there are fonts to point libass at — are the only ones this layer
    # decides, and a hand-written copy is how a new knob (``tune``, the VBV
    # ceiling) silently stops reaching ffmpeg.
    args = encode_args(
        plan,
        replace(options, subtitle_file=subtitle_file, fonts_dir=FONTS_DIR if fonts else None),
    )
    log.info(
        "transcode starting",
        extra={
            **plan.as_dict(),
            "fonts": fonts,
            "burned_in": subtitle_file is not None,
            "encoder": options.video_encoder,
        },
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
    segments = await validate_output(plan.output_dir, ffprobe=ffprobe)
    if not segments:
        raise TranscodeError(
            f"ffmpeg finished but {plan.output_dir / PLAYLIST_NAME} is not a playable playlist"
        )

    duration = await _output_duration(plan, ffprobe=ffprobe)
    return TranscodeResult(
        output_dir=plan.output_dir,
        playlist=plan.output_dir / PLAYLIST_NAME,
        duration=duration,
        width=plan.width,
        height=plan.height,
        subtitle_lang=plan.subtitle_lang if subtitle_file else None,
        audio_lang=plan.audio_lang,
        segments=segments,
        fonts=fonts,
        seconds=perf_counter() - started,
        notes=tuple(notes),
    )


async def _output_duration(plan: TranscodePlan, *, ffprobe: str) -> float | None:
    """The rendition's own duration, falling back to the source's.

    The *output* is what the player seeks in and what FR-S4's 90 % is measured
    against, and it can differ from the source by a segment's rounding. Read
    from the playlist when ffprobe will say, from the plan when it will not.
    """
    payload = await ffprobe_json(plan.output_dir / PLAYLIST_NAME, binary=ffprobe)
    container = (payload or {}).get("format") or {}
    raw = container.get("duration") if isinstance(container, dict) else None
    try:
        duration = float(raw)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return plan.duration
    return duration if duration > 0 else plan.duration


__all__ = [
    "HELPER_TIMEOUT",
    "NO_LIBASS",
    "KILL_GRACE",
    "MAX_TAIL_CHARS",
    "STAGE_ENCODE",
    "STAGE_FONTS",
    "STAGE_PACKAGE",
    "STAGE_PROBE",
    "STDERR_TAIL_LINES",
    "ProgressCallback",
    "ProgressReader",
    "available_filters",
    "TranscodeError",
    "TranscodeResult",
    "clean_work",
    "extract_fonts",
    "extract_subtitle",
    "output_files",
    "parse_progress_line",
    "prepare_directories",
    "progress_seconds",
    "require_subtitle_filter",
    "reset_filters",
    "reset_semaphore",
    "run_ffmpeg",
    "transcode",
    "transcode_semaphore",
    "validate_output",
]
