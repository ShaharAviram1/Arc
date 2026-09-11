"""Fake ``ffmpeg`` and ``ffprobe`` on ``PATH``, for the M7 tests.

The transcode code path is almost entirely *about* running a subprocess:
argument order, working directory, which stream on which pipe, what a non-zero
exit means. Mocking ``asyncio.create_subprocess_exec`` would test none of it.
So these write real executable scripts into a temporary directory and put it
first on ``PATH`` — the same trick :mod:`tests.test_probe` uses for ffprobe,
one step further.

The fake ffmpeg reads its own arguments and behaves like the real one for the
three invocations Arc makes: dumping attachments, extracting a subtitle track,
and encoding to HLS with ``-progress`` on stdout. It can also be told to fail,
to be slow, or to produce a broken playlist, which are the three interesting
failures.

The real binary is used, on a five-second fixture, by ``test_transcode_slow``.
"""

from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path
from typing import Any

import pytest

from arc.services.media.transcode import reset_filters

#: What the fake ffprobe says about a source file: one video, one Japanese
#: audio, an English ASS track and a Portuguese one, and two attachments.
SOURCE_PROBE: dict[str, Any] = {
    "format": {"format_name": "matroska,webm", "duration": "12.0"},
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "disposition": {"attached_pic": 0},
        },
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "jpn"}},
        {
            "index": 2,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "eng", "title": "English"},
        },
        {
            "index": 3,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "por", "title": "Portuguese"},
        },
        {"index": 4, "codec_type": "attachment", "tags": {"filename": "Arial.ttf"}},
        {"index": 5, "codec_type": "attachment", "tags": {"filename": "Comic.ttf"}},
    ],
}

#: And about a finished playlist.
PLAYLIST_PROBE: dict[str, Any] = {
    "format": {"format_name": "hls", "duration": "12.0"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    ],
}

_FFPROBE = """\
import json, sys
SOURCE = {source}
PLAYLIST = {playlist}
target = sys.argv[-1]
print(json.dumps(PLAYLIST if target.endswith(".m3u8") else SOURCE))
"""

_FFMPEG = """\
import os, sys, time
args = sys.argv[1:]
FAIL = {fail!r}
SLEEP = {sleep!r}
BROKEN = {broken!r}
SEGMENTS = {segments!r}
DURATION = {duration!r}
MARKER = {marker!r}
FILTERS = {filters!r}

def note(kind, argv=None):
    if MARKER:
        with open(MARKER, "a") as handle:
            handle.write(kind + "\\n")
        if argv is not None:
            with open(MARKER + ".argv", "a") as handle:
                handle.write("\\0".join(argv) + "\\n")

if "-filters" in args:
    print("Filters:")
    print("  T.. = Timeline support")
    for name in FILTERS:
        print(" .. %s V->V Render subtitles onto input video." % name)
    sys.exit(0)

if any(a.startswith("-dump_attachment") for a in args):
    note("fonts")
    for index, arg in enumerate(args):
        if arg.startswith("-dump_attachment"):
            target = args[index + 1]
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(b"font")
    sys.exit(0)

if "-c:s" in args:
    note("subtitle")
    target = args[-1]
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w") as handle:
        handle.write("[Script Info]\\nTitle: stub\\n")
    sys.exit(0)

note("encode", args)
if FAIL:
    for line in range(60):
        print("[libx264 @ 0x1] stub complaint line %d" % line, file=sys.stderr)
    print("Error opening output file index.m3u8.", file=sys.stderr)
    sys.exit(1)

playlist = args[-1]
step = DURATION / max(SEGMENTS, 1)
for index in range(SEGMENTS):
    if SLEEP:
        time.sleep(SLEEP)
    if not BROKEN:
        with open("init.mp4", "wb") as handle:
            handle.write(b"init")
        with open("seg_%05d.m4s" % index, "wb") as handle:
            handle.write(b"segment")
    print("frame=%d" % (index * 100))
    print("out_time_us=%d" % int((index + 1) * step * 1_000_000))
    print("progress=continue")
    sys.stdout.flush()
print("out_time_us=%d" % int(DURATION * 1_000_000))
print("progress=end")
with open(playlist, "w") as handle:
    if BROKEN:
        handle.write("")
    else:
        handle.write("#EXTM3U\\n#EXT-X-VERSION:7\\n")
sys.exit(0)
"""


def write_script(directory: Path, name: str, body: str) -> Path:
    """Write an executable Python script called ``name`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!/usr/bin/env python3\n{textwrap.dedent(body)}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def install_fake_ffmpeg(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
    sleep: float = 0.0,
    broken: bool = False,
    segments: int = 3,
    duration: float = 12.0,
    marker: Path | None = None,
    source_probe: dict[str, Any] | None = None,
    filters: tuple[str, ...] = ("ass", "subtitles"),
) -> Path:
    """Put a fake ``ffmpeg`` and ``ffprobe`` first on ``PATH``.

    ``marker`` is a file each invocation appends its kind to, so a test can
    assert that the fonts were dumped before the encode without parsing a log.
    The encode also writes its whole argument vector, NUL-separated, to
    ``<marker>.argv`` — which is how "the configured preset actually reached
    ffmpeg" is asserted without stubbing the subprocess away.
    ``filters`` is what it claims to support: pass ``()`` for the Homebrew-style
    build with no libass.
    """
    write_script(
        directory,
        "ffprobe",
        _FFPROBE.format(
            source=json.dumps(source_probe or SOURCE_PROBE),
            playlist=json.dumps(PLAYLIST_PROBE),
        ),
    )
    write_script(
        directory,
        "ffmpeg",
        _FFMPEG.format(
            fail=fail,
            sleep=sleep,
            broken=broken,
            segments=segments,
            duration=duration,
            marker=str(marker) if marker else "",
            filters=filters,
        ),
    )
    monkeypatch.setenv("PATH", str(directory), prepend=os.pathsep)
    # "what can this ffmpeg do?" is cached per binary name for the life of the
    # process, and every test installs a different binary under the same name.
    reset_filters()
    return directory


__all__ = [
    "PLAYLIST_PROBE",
    "SOURCE_PROBE",
    "install_fake_ffmpeg",
    "write_script",
]
