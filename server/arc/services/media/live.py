"""The progressive remux: playing an episode while it downloads (M20 T6, FR-P8).

Somebody pressed play on an episode that is downloading. Its torrent was added
sequentially, first and last pieces first (FR-A5), so its file fills from the
front. This job feeds those bytes — and only those bytes — to the same ffmpeg
remux T2 runs on a finished file, reading Matroska from stdin and writing an
EVENT playlist into ``DATA_DIR/live/<episode id>/``, which the media router
serves until the episode is ready (architecture.md §5.3d).

**Which downloads qualify.** A ``kind=single`` torrent for an episode a user
has demanded, whose largest video file is ``.mkv`` (an MP4 may keep its index
at the end, and a pipe cannot seek) and starts where its piece range says
(a torrent with hidden pad files would put the front in the wrong place),
whose head ffprobes as copyable (:func:`~arc.services.media.plan.copyable`)
**and states the episode's duration** (the completion guard, FR-S4, needs
it), and whose **file name pre-matches this episode at the auto-link
threshold** with the download's prior (:mod:`arc.services.library.prematch`,
FR-L4's stated exception). Anything else waits for the download and the
ordinary match; the 17 s remux then makes it ready. Nothing is indexed here.

**How far the download is** is never the file's size or mtime — qBittorrent
pre-allocates the whole file up front (the FR-L1 incident). It is
:func:`contiguous_bytes`: the hash-checked pieces in a row from the one that
holds the file's first byte, converted to bytes and clamped to the file. The
last pieces, fetched early by first/last priority, do not count until the
pieces before them are in.

**Never past the front.** :class:`FrontGatedReader` opens the file with
``O_NOFOLLOW`` under ``downloads/<episode id>/`` (the path built from the id and
the name qBittorrent reports, confined again after the open), writes at most
``front − position`` bytes at a time to ffmpeg's stdin, and when it has caught
up polls the front every two seconds. When the front is the whole file (every
piece hash-checked) it closes stdin; ffmpeg flushes and writes
``#EXT-X-ENDLIST``. An ffmpeg that stops reading for a minute, or exits, ends
the run.

**Idempotent by restarting.** Every start feeds the file again from byte 0: a
``-c:v copy`` of what is already downloaded runs at disk speed, and the output
is deterministic (same bytes in, same keyframes, same ``seg_%05d`` names and
bytes). A first start cuts straight into ``live/<id>/``. A restart that finds a
playlist being served there cuts into ``live/<id>.new/`` instead and swaps it
in once it has caught up, so a player never sees the list shrink. No progress
for ``LIVE_IDLE_MINUTES`` stops ffmpeg (the playlist stays servable up to
there) and queues the job again five minutes later, at most
:data:`MAX_IDLE_REQUEUES` times. A torrent cancelled, stalled or gone ends the
job and removes the directory.

**Finishing** is the ordinary path's job: the download completes, the file is
matched and linked, and the ``remux`` handler calls :func:`adopt_live` before
running ffmpeg itself. A live directory that is complete and was cut from the
very file that was linked becomes the rendition by one ``os.replace``.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import shutil
import stat
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    Torrent,
    TorrentKind,
)
from arc.services.acquisition.qbit import (
    DECIDED_STATES,
    QBIT_MISSING,
    FileInfo,
    QbitClient,
    QbitError,
    TorrentInfo,
    host_path,
    save_path_for,
)
from arc.services.jobs.registry import JobContext, register
from arc.services.library.prematch import prematch
from arc.services.media.jobs import (
    KEY_EPISODE,
    KEY_ERROR,
    MAX_ERROR_TAIL,
    _Reporter,
    episode_lock,
    language_rules,
    lock_key,
    validate_output,
)
from arc.services.media.live_serve import (
    LIVE_EPISODE_STATES,
    LIVE_TARGET_DURATION,
    NEXT_SUFFIX,
    OLD_SUFFIX,
    LivePlaylist,
    live_servable,
    remove_live_dir,
    served_playlist,
)
from arc.services.media.names import (
    KEY_DL_SPEED,
    KEY_DOWNLOAD_PROGRESS,
    KEY_EXPECTED_DURATION,
    KEY_FILE_SIZE,
    KEY_FRONT_BYTES,
    KEY_FRONT_SECONDS,
    KEY_LIVE_REASON,
    KEY_REQUEUES,
    KEY_SEEDERS,
    KEY_SEGMENTS,
    LIVE_OWNER_NAME,
    REMUX_INFO_NAME,
    REMUX_LIVE,
    STAGE_LIVE_DONE,
    STAGE_LIVE_GONE,
    STAGE_LIVE_IDLE,
    STAGE_LIVE_REFUSED,
    STAGE_LIVE_REMUX,
    STAGE_LIVE_WAITING,
    SUBTITLES_VTT_NAME,
    RemuxInfo,
    enqueue_remux_live,
    episode_demanded,
    latest_live_job,
    live_dir_for,
    live_job_servable,
    output_dir_for,
)
from arc.services.media.plan import (
    INIT_NAME,
    VIDEO_PLAYLIST_NAME,
    WORK_DIR,
    PlanError,
    RemuxOptions,
    TranscodePlan,
    _raw_stream,
    build_plan,
    copyable,
    remux_args,
)
from arc.services.media.probe import FFPROBE_ARGS, PROBE_TIMEOUT, ffprobe_json, ffprobe_path
from arc.services.media.remux import codecs_string, peak_bandwidth, read_cues, subtitle_label
from arc.services.media.subtitles import Cue, cues_to_vtt
from arc.services.media.transcode import (
    KILL_GRACE,
    STDERR_TAIL_LINES,
    STREAM_LIMIT,
    TranscodeError,
    _drain,
    _kill_group,
    _tail,
)

log = logging.getLogger(__name__)

#: The advisory lock's first half: ``('remux_live', episode_id)``.
LIVE_LOCK_KEY: Final[int] = lock_key(REMUX_LIVE)

#: How often the front is read again once the reader has caught up with it.
POLL_SECONDS: float = 2.0

#: The most one write to ffmpeg's stdin carries.
CHUNK_BYTES: Final[int] = 1 << 20

#: How long one write to ffmpeg's stdin may take to drain before ffmpeg is
#: taken to be hung: a stuck ffmpeg must not hold the only live slot forever.
DRAIN_TIMEOUT_SECONDS: float = 60.0

#: How often the payload (front, segments, seeders) is written, and how often
#: ``locked_at`` is pushed forward.
PAYLOAD_SECONDS: float = 5.0
HEARTBEAT_SECONDS: Final[float] = 60.0

#: How long after an idle exit the job runs again, and how many times in a row.
REQUEUE_AFTER: Final[timedelta] = timedelta(minutes=5)
MAX_IDLE_REQUEUES: Final[int] = 12

#: How far the head may grow while ffprobe cannot read it yet: an MKV with
#: embedded fonts carries them before its first cluster, often tens of MiB.
MAX_HEAD_BYTES: Final[int] = 64 << 20

#: The one extension a live remux reads: Matroska puts its header first.
LIVE_EXTENSION: Final[str] = "mkv"

#: The hash-checked piece state of ``torrents/pieceStates``.
PIECE_DONE: Final[int] = 2

#: Torrent row states that end a live remux: the download is not going to
#: arrive (FR-A6's ``stalled``, the reconciler's ``cancelled``, retention's
#: ``evicted``, a review's ``rejected``) or is gone from the client.
GONE_STATES: Final[frozenset[str]] = DECIDED_STATES | frozenset({QBIT_MISSING})

REFUSED_NOT_DOWNLOADING = "the episode is not downloading"
REFUSED_NOT_SINGLE = "the episode is downloading from a batch; it plays when its file is in"
REFUSED_NOT_DEMANDED = "nobody pressed play on this episode"
REFUSED_NOT_MKV = "the file is not Matroska; it plays when the download finishes"
REFUSED_NO_VIDEO = "the torrent has no video file"
REFUSED_UNREADABLE = "the head of the file could not be probed"
REFUSED_PATH = "the file is not inside this episode's download directory"
REFUSED_PADDED = "the torrent has pad files; it plays when the download finishes"
REFUSED_NO_DURATION = "the file does not state its duration; it plays when the download finishes"
GAVE_UP = "the download made no progress after {count} tries"


class LiveIdle(Exception):  # noqa: N818 - a signal, not an error
    """The front has not moved for ``LIVE_IDLE_MINUTES``."""


class LiveGone(Exception):  # noqa: N818 - a signal, not an error
    """The download behind the live remux was cancelled, stalled or removed."""


class LiveRefused(Exception):  # noqa: N818 - a signal, not an error
    """The download does not qualify for a live remux; ``args[0]`` says why."""


# --- The front -------------------------------------------------------------------


def contiguous_bytes(
    states: Sequence[int], piece_size: int, file_offset: int, file_size: int
) -> int:
    """How many bytes of the file, from its first, are downloaded and checked (pure).

    ``states`` is ``torrents/pieceStates`` for the whole torrent;
    ``file_offset`` is where the file starts in the torrent's byte stream (the
    sizes of the files before it). Consecutive state-2 pieces are counted from
    the one holding the file's first byte; the answer is clamped to
    ``[0, file_size]``. Pieces past the first gap count for nothing.
    """
    if piece_size <= 0 or file_size <= 0 or file_offset < 0:
        return 0
    index = file_offset // piece_size
    while index < len(states) and states[index] == PIECE_DONE:
        index += 1
    front = index * piece_size - file_offset
    return max(0, min(front, file_size))


def offset_agrees(offset: int, piece_size: int, piece_range: tuple[int, int] | None) -> bool:
    """Whether the summed file sizes put the file where the client says it is (pure).

    qBittorrent does not list BEP 47 pad files (nor a v2 torrent's implied
    padding), so the sizes of the files before ours can understate where it
    starts. The client's ``piece_range`` is the check: the piece holding the
    first byte by our arithmetic must be the first piece it names. A file
    that is not the first in its torrent with no range to check is refused.
    """
    if piece_size <= 0:
        return False
    if piece_range is None:
        return offset == 0
    return offset // piece_size == piece_range[0]


@dataclass(frozen=True, slots=True)
class Front:
    """Where the download is: contiguous checked bytes, the file's size, the client's word."""

    bytes: int
    file_size: int
    complete: bool = False

    @property
    def done(self) -> bool:
        """Every byte of the file is in a hash-checked piece: nothing more will come."""
        return self.bytes >= self.file_size


class Sink(Protocol):
    """ffmpeg's stdin, as :class:`asyncio.StreamWriter` presents it."""

    def write(self, data: bytes) -> object: ...

    async def drain(self) -> None: ...

    def close(self) -> object: ...


class FrontGatedReader:
    """Feed a growing file to ``sink``, never past the download front.

    ``poll`` reads the front again (and may raise :class:`LiveGone`); it is
    called only once the reader has caught up. ``idle_seconds`` without the
    front moving raises :class:`LiveIdle` — also when the file turns out
    shorter than the front says. A ``drain`` that takes longer than
    ``drain_timeout`` raises :class:`TranscodeError` (ffmpeg stopped reading);
    ``alive`` returning false while caught up raises :class:`BrokenPipeError`
    (ffmpeg exited). ``clock`` and ``sleep`` are parameters so a test can run
    hours of download in no time.
    """

    def __init__(
        self,
        fd: int,
        poll: Callable[[], Awaitable[Front]],
        *,
        idle_seconds: float,
        poll_seconds: float | None = None,
        chunk: int = CHUNK_BYTES,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        alive: Callable[[], bool] | None = None,
        drain_timeout: float | None = None,
    ) -> None:
        self.fd = fd
        self.poll = poll
        self.idle_seconds = idle_seconds
        self.poll_seconds = POLL_SECONDS if poll_seconds is None else poll_seconds
        self.chunk = max(int(chunk), 1)
        self.clock = clock
        self.sleep = sleep
        self.alive = alive
        self.drain_timeout = DRAIN_TIMEOUT_SECONDS if drain_timeout is None else drain_timeout
        #: Bytes handed to the sink so far: the next offset read.
        self.position = 0
        self._best = -1
        self._moved_at = 0.0

    async def run(self, sink: Sink, first: Front | None = None) -> int:
        """Feed until the file is whole; close ``sink`` then. Returns bytes fed."""
        front = first if first is not None else await self.poll()
        self._best = front.bytes
        self._moved_at = self.clock()
        while True:
            limit = min(front.bytes, front.file_size)
            if self.position < limit:
                size = min(self.chunk, limit - self.position)
                data = await asyncio.to_thread(os.pread, self.fd, size, self.position)
                if data:
                    sink.write(data)
                    try:
                        await asyncio.wait_for(sink.drain(), timeout=self.drain_timeout)
                    except TimeoutError as exc:
                        raise TranscodeError(
                            f"ffmpeg stopped reading its input for {self.drain_timeout:.0f}s"
                        ) from exc
                    self.position += len(data)
                    continue
                # The front says the bytes are there and the file is shorter
                # (a temporary name, a truncated file): wait, on the idle clock.
            elif front.done:
                sink.close()
                return self.position
            front = await self._wait()

    async def _wait(self) -> Front:
        """Sleep, read the front again, and judge idleness and ffmpeg's health."""
        if self.alive is not None and not self.alive():
            raise BrokenPipeError("ffmpeg exited while waiting for the download")
        await self.sleep(self.poll_seconds)
        if self.alive is not None and not self.alive():
            raise BrokenPipeError("ffmpeg exited while waiting for the download")
        front = await self.poll()
        if front.bytes > self._best:
            self._best = front.bytes
            self._moved_at = self.clock()
        elif self.clock() - self._moved_at >= self.idle_seconds:
            raise LiveIdle(f"no download progress for {self.idle_seconds:.0f}s")
        return front


# --- The source file -------------------------------------------------------------


def source_path(settings: Settings, episode_id: int, file_name: str) -> Path:
    """The file qBittorrent is writing, as the worker opens it; confined or raises.

    Built from the episode id's save path and the name qBittorrent reports
    (``torrents/files``), mapped to the worker's view of the downloads root,
    resolved, and refused unless it lands inside ``downloads/<episode id>/``.
    :func:`open_confined` checks the same of what was actually opened.
    """
    container = str(
        PurePosixPath(save_path_for(episode_id, downloads_path=settings.qbit_downloads_path))
        / file_name
    )
    try:
        path = host_path(
            container,
            downloads_path=settings.qbit_downloads_path,
            host_downloads=settings.downloads_dir,
        )
    except QbitError as exc:
        raise LiveRefused(REFUSED_PATH) from exc
    root = (settings.downloads_dir / str(episode_id)).resolve()
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root) or resolved == root:
        raise LiveRefused(REFUSED_PATH)
    return resolved


def open_source(path: Path) -> int:
    """``O_RDONLY | O_NOFOLLOW`` on a regular file, or :class:`FileNotFoundError`/``OSError``."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path.name} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def fd_path(fd: int) -> str | None:
    """Where an open descriptor really is: ``/proc/self/fd`` on Linux, ``F_GETPATH`` on macOS."""
    if os.path.isdir("/proc/self/fd"):
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return None
    getpath: int | None = getattr(fcntl, "F_GETPATH", None)
    if getpath is not None:
        try:
            raw = fcntl.fcntl(fd, getpath, bytes(1024))
        except OSError:
            return None
        return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape") or None
    return None


def open_confined(path: Path, root: Path) -> int:
    """:func:`open_source`, then refuse unless the opened file is inside ``root``.

    The name was confined before the open (:func:`source_path`); this checks
    the file that was actually opened, so a directory swapped for a link in
    between cannot redirect the read. Where the platform cannot say, the
    pre-open check and ``O_NOFOLLOW`` stand alone.
    """
    fd = open_source(path)
    real = fd_path(fd)
    if real is not None and not Path(real).is_relative_to(root.resolve()):
        os.close(fd)
        raise LiveRefused(REFUSED_PATH)
    return fd


def largest_video_file(files: Sequence[FileInfo], extensions: frozenset[str]) -> FileInfo | None:
    """The biggest file with a video extension: the episode (the single's rule)."""
    videos = [
        row
        for row in files
        if PurePosixPath(row.name).suffix.lower().lstrip(".") in extensions
        and not PurePosixPath(row.name).name.startswith(".")
    ]
    return max(videos, key=lambda row: (row.size, -row.index), default=None)


def file_offset(files: Sequence[FileInfo], chosen: FileInfo) -> int:
    """Where ``chosen`` starts in the torrent's byte stream: the listed files before it."""
    return sum(row.size for row in files if row.index < chosen.index)


async def probe_head(binary: str, head: bytes) -> dict[str, Any] | None:
    """ffprobe over the first bytes of a Matroska file, through a pipe.

    The MKV header carries the tracks and the Segment duration, which is all
    the plan and ``expected_duration`` need. ``None`` on any failure, like
    :func:`~arc.services.media.probe.ffprobe_json`.
    """
    executable = ffprobe_path(binary)
    if executable is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *FFPROBE_ARGS,
            "-f",
            "matroska",
            "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(head), timeout=PROBE_TIMEOUT)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return None
    except BrokenPipeError, ConnectionResetError:
        stdout = await process.stdout.read() if process.stdout is not None else b""
        await process.wait()
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _has_video(probe: dict[str, Any] | None) -> bool:
    streams = (probe or {}).get("streams") or []
    return any(isinstance(s, dict) and s.get("codec_type") == "video" for s in streams)


# --- The owner record ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveOwner:
    """``live/<id>/owner.json``: what the directory was cut from, and the plan's facts."""

    job_id: int
    info_hash: str
    file_index: int
    file_size: int
    path: str
    width: int | None = None
    height: int | None = None
    subtitle_lang: str | None = None
    audio_lang: str | None = None
    audio_copy: bool = False
    expected_duration: float | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "info_hash": self.info_hash,
                "file_index": self.file_index,
                "file_size": self.file_size,
                "path": self.path,
                "width": self.width,
                "height": self.height,
                "subtitle_lang": self.subtitle_lang,
                "audio_lang": self.audio_lang,
                "audio_copy": self.audio_copy,
                "expected_duration": self.expected_duration,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> LiveOwner | None:
        try:
            raw = json.loads(text)
        except ValueError:
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                job_id=int(raw["job_id"]),
                info_hash=str(raw["info_hash"]),
                file_index=int(raw["file_index"]),
                file_size=int(raw["file_size"]),
                path=str(raw["path"]),
                width=_optional_int(raw.get("width")),
                height=_optional_int(raw.get("height")),
                subtitle_lang=_optional_str(raw.get("subtitle_lang")),
                audio_lang=_optional_str(raw.get("audio_lang")),
                audio_copy=raw.get("audio_copy") is True,
                expected_duration=_optional_float(raw.get("expected_duration")),
            )
        except KeyError, TypeError, ValueError:
            return None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _write_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` by rename, so a reader never sees half of it."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _remove(path: Path) -> None:
    """Remove a file, a link (never followed) or a tree. Never raises."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return
    except OSError:  # pragma: no cover - housekeeping
        return
    shutil.rmtree(path, ignore_errors=True)


def _sibling(directory: Path, suffix: str) -> Path:
    return directory.with_name(directory.name + suffix)


# --- The handler -----------------------------------------------------------------


@register(REMUX_LIVE)
async def remux_live(ctx: JobContext) -> None:
    """Remux one downloading episode as its bytes arrive (FR-P8).

    The lock is taken first, as every prepare job does: a second claim of the
    same episode does nothing at all.
    """
    episode_id = int(ctx.payload[KEY_EPISODE])
    async with episode_lock(ctx.session, episode_id, key=LIVE_LOCK_KEY) as owned:
        if not owned:
            ctx.log.info(
                "another claim is already remuxing this download; leaving it to that one",
                extra={"episode_id": episode_id, "job_id": ctx.job.id},
            )
            return
        await _live(ctx, episode_id)


async def _live_torrent(session: AsyncSession, episode_id: int) -> Torrent | None:
    """The newest single of this episode that nothing has decided against."""
    found: Torrent | None = await session.scalar(
        select(Torrent)
        .where(
            Torrent.episode_id == episode_id,
            Torrent.kind == TorrentKind.SINGLE,
            (Torrent.qbit_state.is_(None)) | (Torrent.qbit_state.not_in(sorted(GONE_STATES))),
        )
        .order_by(Torrent.id.desc())
        .limit(1)
    )
    return found


class _Poller:
    """Reads the front, keeps the payload current, notices a cancelled download,
    and swaps a restart's directory in once it has caught up."""

    def __init__(
        self,
        ctx: JobContext,
        reporter: _Reporter,
        qbit: QbitClient,
        *,
        episode_id: int,
        torrent_id: int,
        info_hash: str,
        offset: int,
        piece_size: int,
        file: FileInfo,
        directory: Path,
        target: Path,
        old_segments: int = 0,
    ) -> None:
        self.ctx = ctx
        self.reporter = reporter
        self.qbit = qbit
        self.episode_id = episode_id
        self.torrent_id = torrent_id
        self.info_hash = info_hash
        self.offset = offset
        self.piece_size = piece_size
        self.file = file
        #: Where ffmpeg writes: ``live/<id>`` or, on a restart over a served
        #: playlist, ``live/<id>.new`` until :meth:`swap`.
        self.directory = directory
        self.target = target
        self.old_segments = old_segments
        self.stage = STAGE_LIVE_WAITING
        self.last_write = 0.0
        self.last_beat = monotonic()
        self.mpegts_known = False
        self.warned_long = False
        self.front: Front = Front(0, file.size)
        self.info: TorrentInfo | None = None

    async def _check_rows(self) -> None:
        row = (
            await self.ctx.session.execute(
                select(Torrent.id, Torrent.qbit_state).where(Torrent.id == self.torrent_id)
            )
        ).first()
        if row is None or (row.qbit_state is not None and row.qbit_state in GONE_STATES):
            raise LiveGone(f"the torrent is {row.qbit_state if row is not None else 'gone'}")
        episode_state = await self.ctx.session.scalar(
            select(Episode.state).where(Episode.id == self.episode_id)
        )
        if episode_state is None or episode_state not in LIVE_EPISODE_STATES:
            raise LiveGone(f"the episode is {episode_state.value if episode_state else 'gone'}")
        # The reads above open a transaction; ending it here means the next
        # poll sees what other transactions committed in the meantime.
        await self.ctx.session.commit()

    async def __call__(self) -> Front:
        await self._check_rows()
        info = await self.qbit.info(self.info_hash)
        if info is None:
            raise LiveGone("the torrent is gone from the client")
        states = await self.qbit.piece_states(self.info_hash)
        front = contiguous_bytes(states, self.piece_size, self.offset, self.file.size)
        self.info = info
        self.front = Front(front, self.file.size, complete=info.complete)
        await self.report()
        return self.front

    def served(self) -> LivePlaylist:
        """What the router would serve from the directory ffmpeg writes now."""
        return served_playlist(self.directory)

    def swap(self, *, force: bool = False) -> bool:
        """Move a restart's directory into place once it has caught up.

        Caught up is as many served segments as the old directory had, or its
        end; ``force`` swaps regardless. Two renames with the old directory
        parked aside, so ``live/<id>`` is never half one and half the other.
        ffmpeg keeps writing: its working directory follows the rename.
        """
        if self.directory == self.target:
            return False
        playlist = self.served()
        if not (force or playlist.ended or len(playlist.bounds) >= self.old_segments):
            return False
        parked = _sibling(self.target, OLD_SUFFIX)
        _remove(parked)
        if self.target.exists() or self.target.is_symlink():
            os.replace(self.target, parked)
        os.replace(self.directory, self.target)
        _remove(parked)
        self.directory = self.target
        log.info(
            "live restart caught up and swapped in",
            extra={"episode_id": self.episode_id, "segments": len(playlist.bounds)},
        )
        return True

    async def report(self, *, force: bool = False) -> None:
        now = monotonic()
        if not force and now - self.last_write < PAYLOAD_SECONDS:
            return
        self.last_write = now
        beat = now - self.last_beat >= HEARTBEAT_SECONDS
        if beat:
            self.last_beat = now
        self.swap()
        playlist = self.served()
        if playlist.bounds and not self.mpegts_known:
            await self._record_mpegts()
        if playlist.longest > LIVE_TARGET_DURATION and not self.warned_long:
            self.warned_long = True
            log.warning(
                "a live segment is longer than the playlist's target duration",
                extra={
                    "episode_id": self.episode_id,
                    "seconds": round(playlist.longest, 3),
                    "target": LIVE_TARGET_DURATION,
                },
            )
        info = self.info
        await self.reporter.write(
            stage=self.stage,
            heartbeat=beat or force,
            extra={
                KEY_FRONT_BYTES: self.front.bytes,
                KEY_FILE_SIZE: self.file.size,
                KEY_FRONT_SECONDS: round(playlist.front_seconds, 3),
                KEY_SEGMENTS: len(playlist.bounds),
                KEY_DOWNLOAD_PROGRESS: round(info.progress, 4) if info is not None else None,
                KEY_SEEDERS: (
                    info.swarm_seeds
                    if info is not None and info.swarm_seeds is not None
                    else (info.num_seeds if info is not None else None)
                ),
                KEY_DL_SPEED: info.dlspeed if info is not None else None,
            },
        )

    async def _record_mpegts(self) -> None:
        """Write the first video frame's 90 kHz time into ``remux.json`` once.

        From ``init.mp4`` followed by ``seg_00000.m4s`` — the first segment
        itself — never from the growing playlist, which libavformat opens near
        its live end and would answer for some later segment.
        """
        directory = self.directory
        probe_file = directory / WORK_DIR / "first.mp4"
        try:
            with probe_file.open("wb") as out:
                for name in (INIT_NAME, "seg_00000.m4s"):
                    fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                    with os.fdopen(fd, "rb") as part:
                        shutil.copyfileobj(part, out)
        except OSError:
            _remove(probe_file)
            return
        try:
            out_probe = await ffprobe_json(probe_file, binary=self.ctx.settings.ffprobe_bin)
        finally:
            _remove(probe_file)
        streams = [s for s in (out_probe or {}).get("streams") or [] if isinstance(s, dict)]
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            return
        try:
            first = max(float(video.get("start_time") or 0.0), 0.0)
        except TypeError, ValueError:
            first = 0.0
        info_path = directory / REMUX_INFO_NAME
        try:
            current = RemuxInfo.from_json(info_path.read_text(encoding="utf-8"))
        except OSError:
            current = RemuxInfo()
        updated = RemuxInfo(
            mpegts=round(first * 90_000),
            bandwidth=current.bandwidth,
            codecs=current.codecs,
            subtitle_name=current.subtitle_name,
        )
        _write_atomically(info_path, updated.to_json())
        self.mpegts_known = True

    async def wait_for(self, wanted: int, *, idle_seconds: float) -> Front:
        """Poll until the front reaches ``wanted`` bytes (or the whole file)."""
        moved_at = monotonic()
        best = -1
        while True:
            front = await self()
            if front.bytes >= min(wanted, front.file_size) or front.done:
                return front
            if front.bytes > best:
                best = front.bytes
                moved_at = monotonic()
            elif monotonic() - moved_at >= idle_seconds:
                raise LiveIdle("no download progress before the head arrived")
            await asyncio.sleep(POLL_SECONDS)


async def _metadata(
    ctx: JobContext, qbit: QbitClient, info_hash: str, *, idle_seconds: float
) -> tuple[list[FileInfo], int]:
    """The torrent's file list and piece size, waiting for its metadata if need be."""
    started = monotonic()
    while True:
        files = await qbit.files(info_hash)
        properties = await qbit.properties(info_hash) if files else None
        if files and properties is not None and properties.piece_size > 0:
            return files, properties.piece_size
        if monotonic() - started >= idle_seconds:
            raise LiveIdle("the torrent's metadata did not arrive")
        await asyncio.sleep(POLL_SECONDS)


async def _finish(
    ctx: JobContext,
    reporter: _Reporter,
    stage: str,
    *,
    reason: str | None = None,
    remove: bool = False,
) -> None:
    """Write a terminal stage (and why), removing the directory if asked."""
    if remove:
        remove_live_dir(ctx.settings, int(reporter.payload[KEY_EPISODE]))
    await reporter.write(stage=stage, heartbeat=True, extra={KEY_LIVE_REASON: reason})
    ctx.log.info(
        "live remux finished",
        extra={
            "episode_id": reporter.payload[KEY_EPISODE],
            "job_id": ctx.job.id,
            "stage": stage,
            "reason": reason,
        },
    )


def _requeues(ctx: JobContext) -> int:
    raw: Any = ctx.payload.get(KEY_REQUEUES)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


async def _live(ctx: JobContext, episode_id: int) -> None:
    """The body of :func:`remux_live`, under the lock."""
    settings = ctx.settings
    reporter = _Reporter(ctx, {**ctx.payload, KEY_EPISODE: episode_id})
    target = live_dir_for(settings, episode_id)

    episode = await ctx.session.get(Episode, episode_id)
    if episode is None or episode.state not in LIVE_EPISODE_STATES:
        await _finish(ctx, reporter, STAGE_LIVE_GONE, reason=REFUSED_NOT_DOWNLOADING, remove=True)
        return
    if episode.state is not EpisodeState.DOWNLOADING:
        # The download finished while this job waited (an idle requeue): the
        # ordinary match and remux take it from here, and whatever the last
        # run left stays servable until they do.
        await _finish(ctx, reporter, STAGE_LIVE_IDLE, reason=REFUSED_NOT_DOWNLOADING)
        return
    torrent = await _live_torrent(ctx.session, episode_id)
    if torrent is None:
        await _finish(ctx, reporter, STAGE_LIVE_REFUSED, reason=REFUSED_NOT_SINGLE, remove=True)
        return
    if not await episode_demanded(ctx.session, episode_id):
        await _finish(ctx, reporter, STAGE_LIVE_REFUSED, reason=REFUSED_NOT_DEMANDED, remove=True)
        return
    torrent_id, info_hash = torrent.id, torrent.info_hash.lower()
    sub_lang, audio_lang = await language_rules(ctx.session)

    # Every start is from byte 0 into an empty directory (deterministic
    # output). Over a playlist somebody may be watching, into ``.new`` first.
    old = await asyncio.to_thread(served_playlist, target)
    workdir = _sibling(target, NEXT_SUFFIX) if old.bounds else target
    _remove(_sibling(target, NEXT_SUFFIX))
    _remove(_sibling(target, OLD_SUFFIX))
    if workdir == target:
        _remove(target)
    (workdir / WORK_DIR).mkdir(parents=True, exist_ok=True)
    await reporter.write(
        stage=STAGE_LIVE_WAITING, heartbeat=True, extra={KEY_ERROR: None, KEY_LIVE_REASON: None}
    )
    idle_seconds = float(settings.live_idle_minutes) * 60.0
    poller: _Poller | None = None
    try:
        async with QbitClient.from_settings(settings) as qbit:
            files, piece_size = await _metadata(ctx, qbit, info_hash, idle_seconds=idle_seconds)
            chosen = largest_video_file(files, settings.video_extensions_set)
            if chosen is None:
                raise LiveRefused(REFUSED_NO_VIDEO)
            if PurePosixPath(chosen.name).suffix.lower().lstrip(".") != LIVE_EXTENSION:
                raise LiveRefused(REFUSED_NOT_MKV)
            offset = file_offset(files, chosen)
            if not offset_agrees(offset, piece_size, chosen.piece_range):
                raise LiveRefused(REFUSED_PADDED)
            path = source_path(settings, episode_id, chosen.name)
            gate = await prematch(
                ctx.session,
                settings,
                PurePosixPath(chosen.name).name,
                anime_id=episode.anime_id,
                number=episode.number,
            )
            if not gate.ok:
                raise LiveRefused(gate.reason or "the file name did not pre-match")
            poller = _Poller(
                ctx,
                reporter,
                qbit,
                episode_id=episode_id,
                torrent_id=torrent_id,
                info_hash=info_hash,
                offset=offset,
                piece_size=piece_size,
                file=chosen,
                directory=workdir,
                target=target,
                old_segments=len(old.bounds),
            )
            front = await poller.wait_for(settings.live_head_bytes, idle_seconds=idle_seconds)
            root = settings.downloads_dir / str(episode_id)
            fd = await _open_when_there(path, root, poller, idle_seconds=idle_seconds)
            try:
                probe, front = await _probe_growing(ctx, fd, poller, front, idle_seconds)
                plan, audio_copy = _plan_from_probe(
                    probe, front, path, workdir, sub_lang=sub_lang, audio_lang=audio_lang
                )
                owner = LiveOwner(
                    job_id=ctx.job.id,
                    info_hash=info_hash,
                    file_index=chosen.index,
                    file_size=chosen.size,
                    path=str(path),
                    width=plan.width,
                    height=plan.height,
                    subtitle_lang=plan.subtitle_lang,
                    audio_lang=plan.audio_lang,
                    audio_copy=audio_copy,
                    expected_duration=plan.duration,
                )
                _write_atomically(workdir / LIVE_OWNER_NAME, owner.to_json())
                poller.stage = STAGE_LIVE_REMUX
                await reporter.write(
                    stage=STAGE_LIVE_REMUX,
                    heartbeat=True,
                    extra={
                        KEY_EXPECTED_DURATION: plan.duration,
                        KEY_FILE_SIZE: chosen.size,
                        "width": plan.width,
                        "height": plan.height,
                        "subtitle_lang": plan.subtitle_lang,
                        "info_hash": info_hash,
                        "file_index": chosen.index,
                    },
                )
                ctx.log.info(
                    "live remux started",
                    extra={
                        "episode_id": episode_id,
                        "job_id": ctx.job.id,
                        "hash": info_hash,
                        "file_size": chosen.size,
                        "front_bytes": front.bytes,
                        "expected_duration": plan.duration,
                        "confidence": gate.confidence,
                        "restart": workdir != target,
                    },
                )
                await _run_ffmpeg(ctx, plan, audio_copy, fd, poller, front)
            finally:
                os.close(fd)
            poller.swap(force=True)
            await poller.report(force=True)
    except LiveRefused as exc:
        await _finish(ctx, reporter, STAGE_LIVE_REFUSED, reason=str(exc), remove=True)
        return
    except LiveGone as exc:
        await _finish(ctx, reporter, STAGE_LIVE_GONE, reason=str(exc), remove=True)
        return
    except LiveIdle as exc:
        # What a restart cut so far replaces the served playlist only when it
        # has caught up with it; otherwise the old one stays.
        if poller is not None:
            with suppress(OSError):
                poller.swap()
        _remove(_sibling(target, NEXT_SUFFIX))
        count = _requeues(ctx) + 1
        if count > MAX_IDLE_REQUEUES:
            await _finish(
                ctx,
                reporter,
                STAGE_LIVE_GONE,
                reason=GAVE_UP.format(count=MAX_IDLE_REQUEUES),
                remove=True,
            )
            return
        await enqueue_remux_live(
            ctx.session,
            episode_id,
            run_after=datetime.now(UTC) + REQUEUE_AFTER,
            exclude_job_id=ctx.job.id,
            requeues=count,
        )
        await ctx.session.commit()
        await _finish(ctx, reporter, STAGE_LIVE_IDLE, reason=str(exc))
        return
    except TranscodeError as exc:
        remove_live_dir(settings, episode_id)
        await reporter.write(
            stage=STAGE_LIVE_REMUX,
            extra={KEY_ERROR: f"{exc}\n{exc.error_tail}"[:MAX_ERROR_TAIL]},
        )
        raise
    await _finish(ctx, reporter, STAGE_LIVE_DONE)


async def _open_when_there(path: Path, root: Path, poller: _Poller, *, idle_seconds: float) -> int:
    """Open the source, waiting (and polling the front) while it does not exist yet."""
    started = monotonic()
    while True:
        try:
            return open_confined(path, root)
        except FileNotFoundError:
            if monotonic() - started >= idle_seconds:
                raise LiveIdle("the file did not appear") from None
            await asyncio.sleep(POLL_SECONDS)
            await poller()
        except OSError as exc:
            raise LiveRefused(REFUSED_PATH) from exc


async def _probe_growing(
    ctx: JobContext, fd: int, poller: _Poller, front: Front, idle_seconds: float
) -> tuple[dict[str, Any], Front]:
    """ffprobe the head; while it cannot be read, wait for twice as much of it.

    From ``LIVE_HEAD_BYTES`` up to :data:`MAX_HEAD_BYTES`: fonts attached to
    an MKV sit before its first cluster. Refused as unreadable once the head
    is that large or the whole file is in.
    """
    head = ctx.settings.live_head_bytes
    while True:
        data = await asyncio.to_thread(os.pread, fd, min(head, front.bytes), 0)
        probe = await probe_head(ctx.settings.ffprobe_bin, data)
        if probe is not None and _has_video(probe):
            return probe, front
        if front.done or head >= MAX_HEAD_BYTES:
            raise LiveRefused(REFUSED_UNREADABLE)
        head = min(head * 2, MAX_HEAD_BYTES)
        ctx.log.info(
            "the head did not probe yet; waiting for more of it",
            extra={"job_id": ctx.job.id, "head_bytes": head},
        )
        front = await poller.wait_for(head, idle_seconds=idle_seconds)


def _plan_from_probe(
    probe: dict[str, Any],
    front: Front,
    path: Path,
    directory: Path,
    *,
    sub_lang: str,
    audio_lang: str,
) -> tuple[TranscodePlan, bool]:
    """The plan from the head's probe, or :class:`LiveRefused`; writes ``remux.json``.

    A head that states no duration is refused: the completion guard (FR-S4)
    compares every reported duration against it, and a live stream without
    one could let 90 % of what has downloaded count as watched.
    """
    try:
        plan = build_plan(
            probe, source=path, output_dir=directory, sub_lang=sub_lang, audio_lang=audio_lang
        )
    except PlanError as exc:
        raise LiveRefused(str(exc)) from exc
    if plan.duration is None or plan.duration <= 0:
        raise LiveRefused(REFUSED_NO_DURATION)
    verdict = copyable(probe, plan)
    if not verdict.video:
        raise LiveRefused(f"the source cannot be copied ({verdict.reason})")
    raw_video = dict(_raw_stream(probe, "video", plan.video))
    raw_audio = dict(_raw_stream(probe, "audio", plan.audio)) if plan.audio else None
    # The source's average rate with headroom; replaced by the measured peak
    # when the remux adopts the directory.
    bandwidth = max(int(front.file_size * 8 / plan.duration * 1.5), 1)
    info = RemuxInfo(
        mpegts=0,
        bandwidth=bandwidth,
        codecs=codecs_string(raw_video, raw_audio, audio_copy=verdict.audio_copy),
        subtitle_name=subtitle_label(plan.subtitle_lang) if plan.subtitle is not None else None,
    )
    _write_atomically(directory / REMUX_INFO_NAME, info.to_json())
    return plan, verdict.audio_copy


async def _run_ffmpeg(
    ctx: JobContext,
    plan: TranscodePlan,
    audio_copy: bool,
    fd: int,
    poller: _Poller,
    front: Front,
) -> None:
    """ffmpeg reading stdin, fed by the gated reader; raises on a bad exit."""
    settings = ctx.settings
    options = RemuxOptions(
        segment_seconds=settings.hls_segment_seconds,
        audio_bitrate=settings.remux_audio_bitrate,
        audio_copy=audio_copy,
    )
    args = remux_args(plan, options, live=True)
    executable = shutil.which(settings.ffmpeg_bin)
    if executable is None:
        raise TranscodeError(f"{settings.ffmpeg_bin} is not installed or not on PATH")
    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            cwd=str(poller.directory),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
            start_new_session=True,
        )
    except OSError as exc:
        raise TranscodeError(f"could not start {settings.ffmpeg_bin}: {exc}") from exc
    drain = asyncio.create_task(_drain(process.stderr, tail))
    reader = FrontGatedReader(
        fd,
        poller,
        idle_seconds=float(settings.live_idle_minutes) * 60.0,
        alive=lambda: process.returncode is None,
    )
    stdin = process.stdin
    assert stdin is not None  # PIPE was asked for
    try:
        try:
            await reader.run(stdin, first=front)
        except BrokenPipeError, ConnectionResetError:
            # ffmpeg went away; its exit code below says why.
            pass
        code = await process.wait()
    except BaseException:
        _kill_group(process)
        with suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=KILL_GRACE)
        drain.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await drain
        raise
    with suppress(TimeoutError):
        await asyncio.wait_for(drain, timeout=KILL_GRACE)
    if code != 0:
        raise TranscodeError(f"{settings.ffmpeg_bin} exited {code}", error_tail=_tail(tail))
    if reader.position < front.file_size:
        raise TranscodeError(
            f"{settings.ffmpeg_bin} exited before the whole file was fed",
            error_tail=_tail(tail),
        )
    if not poller.served().ended:
        raise TranscodeError(
            f"{settings.ffmpeg_bin} finished but {VIDEO_PLAYLIST_NAME} has no end",
            error_tail=_tail(tail),
        )


# --- Adoption by the remux -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Adopted:
    """What :func:`adopt_live` published, for the ``renditions`` row."""

    output_dir: Path
    duration: float | None
    width: int | None
    height: int | None
    subtitle_lang: str | None
    audio_lang: str | None
    text_subtitles: bool
    segments: int
    audio_copy: bool


def _read_owner(directory: Path) -> LiveOwner | None:
    try:
        fd = os.open(directory / LIVE_OWNER_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        return LiveOwner.from_json(handle.read(1 << 16).decode("utf-8", "replace"))


async def adopt_live(
    session: AsyncSession, settings: Settings, episode_id: int, media_file: MediaFile
) -> Adopted | None:
    """Make ``live/<id>`` the episode's rendition, without a second ffmpeg run.

    Only when ``owner.json`` names the very file that was linked (path and
    size), the playlist has ``ENDLIST`` and :func:`validate_output` passes.
    Then: ``EVENT`` becomes ``VOD``, ``subs.vtt`` is written from
    ``_work/sub.*``, ``remux.json`` gets the measured first frame and peak
    bandwidth and the working files go. The directory is **not** moved yet:
    the caller commits the ``renditions`` row and ``ready`` first and then
    calls :func:`publish_adopted`, so there is no moment at which the episode
    is neither served live nor ready (the ready route falls back to the live
    directory for the instant between the two). ``None`` otherwise — and the
    caller runs the ordinary remux.
    """
    directory = live_dir_for(settings, episode_id)
    if directory.is_symlink() or not directory.is_dir():
        return None
    owner = _read_owner(directory)
    if owner is None:
        return None
    source = Path(media_file.path)
    try:
        same_path = source.resolve() == Path(owner.path).resolve()
        actual = source.stat().st_size
    except OSError:
        return None
    if not same_path or owner.file_size != (media_file.size or actual) or actual != owner.file_size:
        return None
    playlist = directory / VIDEO_PLAYLIST_NAME
    if playlist.is_symlink() or not playlist.is_file():
        return None
    text = playlist.read_text(encoding="utf-8")
    if "#EXT-X-ENDLIST" not in text:
        return None
    segments = await validate_output(directory, ffprobe=settings.ffprobe_bin)
    if not segments:
        return None

    vod = text.replace("#EXT-X-PLAYLIST-TYPE:EVENT", "#EXT-X-PLAYLIST-TYPE:VOD")
    _write_atomically(playlist, vod)

    cues: list[Cue] = []
    for name in ("sub.ass", "sub.srt"):
        extracted = directory / WORK_DIR / name
        if extracted.is_file() and not extracted.is_symlink():
            with suppress(OSError):
                cues = read_cues(extracted)
            break
    vtt = directory / SUBTITLES_VTT_NAME
    if cues:
        vtt.write_text(cues_to_vtt(cues), encoding="utf-8")
    elif vtt.exists():
        vtt.unlink()

    out = await ffprobe_json(playlist, binary=settings.ffprobe_bin)
    streams = [s for s in (out or {}).get("streams") or [] if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    try:
        first = max(float((video or {}).get("start_time") or 0.0), 0.0)
    except TypeError, ValueError:
        first = 0.0
    container: Any = (out or {}).get("format") or {}
    try:
        duration: float | None = float(container.get("duration"))
    except TypeError, ValueError, AttributeError:
        duration = owner.expected_duration
    if duration is not None and duration <= 0:
        duration = owner.expected_duration
    sizes = [
        (directory / name).stat().st_size
        for name in (line.strip() for line in vod.splitlines())
        if name and not name.startswith("#") and (directory / name).is_file()
    ]
    try:
        current = RemuxInfo.from_json((directory / REMUX_INFO_NAME).read_text(encoding="utf-8"))
    except OSError:
        current = RemuxInfo()
    info = RemuxInfo(
        mpegts=round(first * 90_000),
        bandwidth=peak_bandwidth(vod, sizes),
        codecs=current.codecs,
        subtitle_name=subtitle_label(owner.subtitle_lang) if cues else None,
    )
    _write_atomically(directory / REMUX_INFO_NAME, info.to_json())
    shutil.rmtree(directory / WORK_DIR, ignore_errors=True)
    with suppress(OSError):
        (directory / LIVE_OWNER_NAME).unlink()

    output_dir = output_dir_for(settings, episode_id)
    log.info(
        "live remux adopted as the rendition",
        extra={"episode_id": episode_id, "segments": segments, "job_id": owner.job_id},
    )
    return Adopted(
        output_dir=output_dir,
        duration=duration,
        width=owner.width,
        height=owner.height,
        subtitle_lang=owner.subtitle_lang if cues else None,
        audio_lang=owner.audio_lang,
        text_subtitles=bool(cues),
        segments=segments,
        audio_copy=owner.audio_copy,
    )


def publish_adopted(settings: Settings, episode_id: int) -> Path:
    """Rename an adopted ``live/<id>`` into ``renditions/<id>`` (same filesystem)."""
    directory = live_dir_for(settings, episode_id)
    output_dir = output_dir_for(settings, episode_id)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(output_dir, ignore_errors=True)
    os.replace(directory, output_dir)
    return output_dir


def live_has_ended(settings: Settings, episode_id: int) -> bool:
    """Whether ``live/<id>``'s playlist is whole (ffmpeg wrote ``ENDLIST``)."""
    return served_playlist(live_dir_for(settings, episode_id)).ended


async def running_live_job(session: AsyncSession, episode_id: int) -> int | None:
    """The id of a ``remux_live`` of this episode that is running right now.

    Columns, not the ORM row: a caller polling this must see the status other
    transactions committed, not the one its identity map remembers.
    """
    found: int | None = await session.scalar(
        select(Job.id)
        .where(
            Job.type == REMUX_LIVE,
            Job.status == JobStatus.RUNNING,
            Job.payload[KEY_EPISODE].astext == str(episode_id),
        )
        .order_by(Job.id.desc())
        .limit(1)
    )
    return found


# --- Leftovers --------------------------------------------------------------------


async def sweep_live_leftovers(session: AsyncSession, settings: Settings) -> int:
    """Remove live directories nothing stands behind any more. Returns how many.

    At worker start (after orphaned jobs are reclaimed) and from the hourly
    retention sweep. ``live/<id>`` stays while :func:`~arc.services.media.
    live_serve.live_servable` would serve it — or while its job is running
    and filling it — and ``live/<id>.new`` only while its job is running.
    Anything else under ``live/`` goes, a link unlinked rather than followed.
    """
    root = settings.live_dir
    if not root.is_dir() or root.is_symlink():
        return 0
    removed = 0
    for entry in sorted(root.iterdir()):
        keep = False
        stem = entry.name.removesuffix(NEXT_SUFFIX)
        restart = stem != entry.name
        if stem.isdigit() and entry.is_dir() and not entry.is_symlink():
            episode = await session.get(Episode, int(stem))
            if episode is not None:
                job = await latest_live_job(session, episode.id)
                running = (
                    job is not None
                    and job.status is JobStatus.RUNNING
                    and live_job_servable(job)
                    and episode.state in LIVE_EPISODE_STATES
                )
                keep = running or (
                    not restart and await live_servable(session, settings, episode) is not None
                )
        if keep:
            continue
        _remove(entry)
        removed += 1
        log.info("live directory removed", extra={"path": entry.name})
    return removed


__all__ = [
    "CHUNK_BYTES",
    "DRAIN_TIMEOUT_SECONDS",
    "GONE_STATES",
    "LIVE_LOCK_KEY",
    "MAX_HEAD_BYTES",
    "MAX_IDLE_REQUEUES",
    "POLL_SECONDS",
    "Adopted",
    "Front",
    "FrontGatedReader",
    "LiveGone",
    "LiveIdle",
    "LiveOwner",
    "LiveRefused",
    "adopt_live",
    "contiguous_bytes",
    "fd_path",
    "file_offset",
    "largest_video_file",
    "offset_agrees",
    "open_confined",
    "open_source",
    "probe_head",
    "publish_adopted",
    "live_has_ended",
    "remove_live_dir",
    "remux_live",
    "running_live_job",
    "source_path",
    "sweep_live_leftovers",
]
