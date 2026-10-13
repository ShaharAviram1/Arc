"""Serving a progressive remux while its source downloads (M20 T6, FR-P8).

Handler-free, like :mod:`arc.services.media.names`: the media router and
``/play`` import this, and neither may pull ffmpeg's world or the job
registry into a request.

Two things live here.

**Whether ``live/<id>/`` may be served** (:func:`live_servable`). An episode
that is not ``ready`` is served from its live directory only while all of
these hold: the newest ``remux_live`` job stands behind the directory
(:func:`~arc.services.media.names.live_job_servable`); the episode is between
"downloading" and "preparing" (:data:`LIVE_EPISODE_STATES` — after the match
the remux adopts the directory, and until it has, the player keeps playing
what it was playing); and the downloaded file has **not** gone to review. A
file the ordinary matcher was not sure of stops being playable the moment it
says so (FR-L4); the pre-match gate let it start, it does not let it continue.

**What the player is given** (:func:`live_media_playlist`). ffmpeg's own
``video.m3u8`` is never passed through. It is re-read and re-emitted with a
fixed ``#EXT-X-TARGETDURATION:8`` (ffmpeg raises its own when a longer GOP
arrives, which an EVENT playlist may not do; and native Safari keeps its
seekable range three target durations behind the playlist's end, so the
target is kept close to the six seconds ffmpeg cuts), ``#EXT-X-PLAYLIST-TYPE:EVENT``,
``#EXT-X-START:TIME-OFFSET=0,PRECISE=YES`` (so neither Safari nor hls.js
starts at the live edge), ``#EXT-X-INDEPENDENT-SEGMENTS``, only the segments
whose files exist (a contiguous prefix: the first missing one ends it), and
``#EXT-X-ENDLIST`` only when ffmpeg wrote it and every segment is there.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, Job, MediaFile, ReviewState
from arc.services.media.names import (
    EPISODE_KEY,
    REMUX_LIVE,
    latest_live_job,
    live_dir_for,
    live_job_servable,
)
from arc.services.media.plan import INIT_NAME, VIDEO_PLAYLIST_NAME
from arc.services.media.subtitles import Cue, ass_to_cues, srt_to_cues

#: Episode states in which a live directory may be served: from the download
#: to the moment the remux has adopted it (``ready``).
LIVE_EPISODE_STATES: Final[frozenset[EpisodeState]] = frozenset(
    {
        EpisodeState.DOWNLOADING,
        EpisodeState.DOWNLOADED,
        EpisodeState.MATCHING,
        EpisodeState.MATCHED,
        EpisodeState.PREPARING,
    }
)

#: The constant target duration of every live playlist and its subtitle
#: playlist. ffmpeg cuts segments at about six seconds (``HLS_SEGMENT_SECONDS``),
#: and native Safari keeps its seekable range ``3 × TARGETDURATION`` behind the
#: playlist's end: at 20 an iPad could seek only to a minute behind the
#: download (T7, WebKit). A longer segment is logged by the live remux.
LIVE_TARGET_DURATION: Final[int] = 8

_SEGMENT_NAME: Final = re.compile(r"\Aseg_\d{5}\.m4s\Z")
_EXTINF: Final = re.compile(r"\A#EXTINF:\s*([0-9]*\.?[0-9]+)\s*,?.*\Z")


@dataclass(frozen=True, slots=True)
class LivePlaylist:
    """The re-emitted media playlist and what the subtitle side needs of it."""

    text: str
    #: ``(start, end)`` seconds of each served segment, in order.
    bounds: tuple[tuple[float, float], ...]
    ended: bool

    @property
    def front_seconds(self) -> float:
        """How far the served segments reach."""
        return self.bounds[-1][1] if self.bounds else 0.0

    @property
    def longest(self) -> float:
        """The longest served segment, seconds (above the target is worth a warning)."""
        return max((end - start for start, end in self.bounds), default=0.0)


#: The most of ffmpeg's playlist that is read: far more than any episode's.
MAX_LIVE_PLAYLIST_BYTES: Final[int] = 1 << 20


def served_playlist(directory: Path) -> LivePlaylist:
    """What the router would serve from ``directory`` now (blocking).

    The directory is opened without following a link and listed through its
    descriptor; ``video.m3u8`` is opened relative to it with ``O_NOFOLLOW``.
    Anything unreadable is an empty playlist.
    """
    empty = live_media_playlist("", frozenset())
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return empty
    try:
        try:
            fd = os.open(
                VIDEO_PLAYLIST_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
            )
        except OSError:
            return empty
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return empty
            text = handle.read(MAX_LIVE_PLAYLIST_BYTES).decode("utf-8", "replace")
        present: set[str] = set()
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    present.add(entry.name)
    except OSError:
        return empty
    finally:
        os.close(dir_fd)
    return live_media_playlist(text, present)


def live_media_playlist(ffmpeg_text: str, present: frozenset[str] | set[str]) -> LivePlaylist:
    """Re-emit ffmpeg's EVENT playlist for the player (pure).

    ``present`` is the set of file names that exist, as plain files, in the
    live directory. A segment not in it ends the list there; no ``init.mp4``
    means no segments at all.
    """
    segments: list[tuple[float, str]] = []
    pending: float | None = None
    wrote_end = False
    complete = True
    for raw in ffmpeg_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "#EXT-X-ENDLIST":
            wrote_end = True
            continue
        found = _EXTINF.match(line)
        if found is not None:
            pending = float(found.group(1))
            continue
        if line.startswith("#"):
            continue
        if pending is None or not _SEGMENT_NAME.match(line):
            pending = None
            continue
        if not complete or line not in present:
            complete = False
            pending = None
            continue
        segments.append((pending, line))
        pending = None
    if INIT_NAME not in present:
        segments = []
        complete = False

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{LIVE_TARGET_DURATION}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    bounds: list[tuple[float, float]] = []
    if segments:
        lines.append(f'#EXT-X-MAP:URI="{INIT_NAME}"')
    position = 0.0
    for duration, name in segments:
        lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(name)
        bounds.append((position, position + duration))
        position += duration
    ended = wrote_end and complete and bool(segments)
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return LivePlaylist(text="\n".join(lines) + "\n", bounds=tuple(bounds), ended=ended)


def growing_cues(raw: bytes, *, ass: bool) -> list[Cue]:
    """The cues of a subtitle file ffmpeg is still writing (pure).

    Only up to the last complete line: a ``Dialogue:`` line cut off mid-write
    would otherwise parse with half its text.
    """
    text = raw.decode("utf-8", "replace").lstrip("﻿")
    cut = text.rfind("\n")
    text = text[: cut + 1] if cut >= 0 else ""
    if not ass:
        # An SRT block is only complete once the blank line after it is in.
        blank = text.rfind("\n\n")
        text = text[: blank + 2] if blank >= 0 else ""
    return ass_to_cues(text) if ass else srt_to_cues(text)


def held_back(cues: list[Cue], front_s: float) -> list[Cue]:
    """The cues a player may be given while the video reaches ``front_s``.

    A cue that starts past the last finished video segment waits: its segment
    does not exist yet, and putting it in the last one would show it early.
    """
    return [cue for cue in cues if cue.start_s < front_s]


async def review_pending(session: AsyncSession, settings: Settings, episode_id: int) -> bool:
    """Whether the matcher disowned a file of this episode's own download.

    A file under ``downloads/<id>/`` waiting in the review queue, or linked to
    another episode: either way the ordinary match did not confirm what the
    pre-match gate let play, and the live stream stops (FR-L4).
    """
    prefix = str(settings.downloads_dir / str(episode_id)) + "/"
    found = await session.scalar(
        select(MediaFile.id)
        .where(
            MediaFile.path.startswith(prefix, autoescape=True),
            (MediaFile.review_state == ReviewState.PENDING)
            | (MediaFile.episode_id.is_not(None) & (MediaFile.episode_id != episode_id)),
        )
        .limit(1)
    )
    return found is not None


@dataclass(frozen=True, slots=True)
class LiveServed:
    """A live directory that is being served, its job, and what it serves now."""

    directory: Path
    job: Job
    playlist: LivePlaylist


async def live_servable(
    session: AsyncSession, settings: Settings, episode: Episode
) -> LiveServed | None:
    """The live directory, its job and playlist, when ``episode`` plays from it.

    ``None`` for a ``ready`` episode (the rendition is served instead), for one
    outside :data:`LIVE_EPISODE_STATES`, with no live job standing behind the
    directory, with no directory, **with no segment to play yet** (a job still
    waiting for the head hands out nothing), or whose file the matcher
    disowned. The stage of the newest job is not consulted beyond that: during
    an idle restart the new job cuts into ``live/<id>.new`` while the old
    directory, with its segments, is what is served. The caller still applies
    the router's link discipline to the directory itself.
    """
    if episode.state not in LIVE_EPISODE_STATES:
        return None
    job = await latest_live_job(session, episode.id)
    if not live_job_servable(job) or job is None:
        return None
    directory = live_dir_for(settings, episode.id)
    if not directory.is_dir() or directory.is_symlink():
        return None
    playlist = await asyncio.to_thread(served_playlist, directory)
    if not playlist.bounds:
        return None
    if await review_pending(session, settings, episode.id):
        return None
    return LiveServed(directory=directory, job=job, playlist=playlist)


async def live_playable_ids(
    session: AsyncSession, settings: Settings, episode_ids: Sequence[int]
) -> frozenset[int]:
    """Which of ``episode_ids`` play from their download right now (FR-A7, FR-P8).

    "downloading 43 % · playable" on the show page. Two queries for the page
    — the episodes in a live state, and the newest live job of each — and a
    directory read only for those few.
    """
    if not episode_ids:
        return frozenset()
    live = list(
        (
            await session.scalars(
                select(Episode).where(
                    Episode.id.in_(list(episode_ids)),
                    Episode.state.in_(sorted(LIVE_EPISODE_STATES)),
                )
            )
        ).all()
    )
    if not live:
        return frozenset()
    key = cast(ColumnElement[str], Job.payload[EPISODE_KEY].astext)
    with_jobs = set(
        (
            await session.scalars(
                select(key)
                .where(Job.type == REMUX_LIVE, key.in_([str(row.id) for row in live]))
                .distinct()
            )
        ).all()
    )
    found: set[int] = set()
    for episode in live:
        if str(episode.id) in with_jobs and await live_servable(session, settings, episode):
            found.add(episode.id)
    return frozenset(found)


#: Where a restart cuts while the old directory is still served, and where
#: the old one is parked for the moment of the swap.
NEXT_SUFFIX: Final[str] = ".new"
OLD_SUFFIX: Final[str] = ".old"


def _remove(path: Path) -> None:
    """Remove a file, a link (never followed) or a tree. Never raises."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return
    except OSError:  # pragma: no cover - housekeeping
        return
    shutil.rmtree(path, ignore_errors=True)


def remove_live_dir(settings: Settings, episode_id: int) -> None:
    """Remove ``live/<id>`` and its restart siblings. A link is unlinked, never followed."""
    directory = live_dir_for(settings, episode_id)
    for path in (
        directory,
        directory.with_name(directory.name + NEXT_SUFFIX),
        directory.with_name(directory.name + OLD_SUFFIX),
    ):
        _remove(path)


__all__ = [
    "LIVE_EPISODE_STATES",
    "LIVE_TARGET_DURATION",
    "MAX_LIVE_PLAYLIST_BYTES",
    "NEXT_SUFFIX",
    "OLD_SUFFIX",
    "LivePlaylist",
    "LiveServed",
    "growing_cues",
    "held_back",
    "live_media_playlist",
    "live_playable_ids",
    "live_servable",
    "served_playlist",
    "remove_live_dir",
    "review_pending",
]
