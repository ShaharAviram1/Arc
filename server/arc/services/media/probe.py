"""``ffprobe`` over a file, when there is an ffprobe (architecture.md §5.2).

Two rules shape this module, and they are the same rule twice.

**Probing is optional.** Arc's Docker image ships ffmpeg; a developer's laptop
often does not, and the ingest pipeline must not stop at the first file on a
machine with no ffprobe. So a missing binary is *not* an error: it is logged
once — once per process, not once per file, or a scan of a hundred episodes is
a hundred identical lines — and every call returns ``None`` from then on.

**A probe never fails a job.** A truncated download, a file still being
written, a container ffprobe does not understand: all of them are ``None`` and
a debug line. The parser has already extracted everything the matcher needs
from the *name*; a probe adds duration and stream detail, which M7 wants for
its transcode plan and the review UI shows as a courtesy.

:func:`summarise` reduces ffprobe's very large JSON to the handful of fields
worth putting in ``media_files.parsed["probe"]``. The raw output of a probe on
a two-hour MKV with forty subtitle tracks is tens of kilobytes of JSONB per
row, and nothing reads more than this.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The binary, and the arguments Arc always sends. ``-v quiet`` because the
#: only output that matters is on stdout, as JSON.
FFPROBE = "ffprobe"
FFPROBE_ARGS = ("-v", "quiet", "-print_format", "json", "-show_format", "-show_streams")

#: Seconds before a probe is abandoned. Generous: ffprobe on a large MKV over
#: a slow disk is seconds, not milliseconds, but it is never a minute.
PROBE_TIMEOUT = 30.0

#: Set once the "no ffprobe on this machine" line has been written.
_warned = False


def ffprobe_path(binary: str = FFPROBE) -> str | None:
    """Where ffprobe is, or ``None`` if it is not on ``PATH``.

    ``binary`` is ``FFPROBE_BIN`` from the settings when a caller has them —
    M7's transcode does, ingest does not, and neither should have to care
    which. An absolute path is returned unchanged if it is executable.
    """
    return shutil.which(binary)


def _warn_once() -> None:
    global _warned
    if not _warned:
        _warned = True
        log.info(
            "ffprobe is not installed; media files will be indexed without stream details",
            extra={"binary": FFPROBE},
        )


def reset_warning() -> None:
    """Forget that the warning was written. For tests only."""
    global _warned
    _warned = False


async def ffprobe_json(path: Path | str, *, binary: str = FFPROBE) -> dict[str, Any] | None:
    """Run ffprobe over ``path`` and return its parsed JSON, or ``None``.

    ``None`` covers every way this can not produce an answer: no ffprobe
    installed, a non-zero exit, a timeout, output that is not JSON. None of
    them is raised, because none of them is a reason to fail an ingest.
    """
    binary = ffprobe_path(binary) or ""
    if not binary:
        _warn_once()
        return None

    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            *FFPROBE_ARGS,
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=PROBE_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            log.warning("ffprobe timed out", extra={"path": str(path)})
            return None
    except OSError as exc:
        log.warning("ffprobe could not be run", extra={"path": str(path), "error": str(exc)})
        return None

    if process.returncode != 0:
        log.debug(
            "ffprobe returned an error", extra={"path": str(path), "code": process.returncode}
        )
        return None
    try:
        payload: dict[str, Any] = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError, UnicodeDecodeError:
        log.debug("ffprobe output was not json", extra={"path": str(path)})
        return None
    return payload


def summarise(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """ffprobe's JSON reduced to what Arc stores.

    Duration and container from ``format``; per stream the codec, the type,
    the language and title tags, and — for video — the dimensions. Everything
    else (bitrates, disposition flags, chapter lists, the codec's private
    data) is dropped: M7 re-probes with its own question when it plans a
    transcode, so keeping it here would be a copy that goes stale.
    """
    if not payload:
        return None
    container = payload.get("format") or {}
    duration: float | None = None
    raw_duration = container.get("duration")
    if raw_duration is not None:
        try:
            duration = float(raw_duration)
        except TypeError, ValueError:
            duration = None

    streams: list[dict[str, Any]] = []
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        tags = stream.get("tags") or {}
        entry: dict[str, Any] = {
            "index": stream.get("index"),
            "type": stream.get("codec_type"),
            "codec": stream.get("codec_name"),
            "language": tags.get("language") if isinstance(tags, dict) else None,
            "title": tags.get("title") if isinstance(tags, dict) else None,
        }
        if stream.get("codec_type") == "video":
            entry["width"] = stream.get("width")
            entry["height"] = stream.get("height")
        streams.append(entry)

    return {
        "duration": duration,
        "format": container.get("format_name"),
        "streams": streams,
    }


async def probe_summary(path: Path | str) -> dict[str, Any] | None:
    """:func:`ffprobe_json` then :func:`summarise` — what ingest calls."""
    return summarise(await ffprobe_json(path))


__all__ = [
    "FFPROBE",
    "FFPROBE_ARGS",
    "PROBE_TIMEOUT",
    "ffprobe_json",
    "ffprobe_path",
    "probe_summary",
    "reset_warning",
    "summarise",
]
