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

#: The same source as a remux sees it (M20): the fields
#: :func:`arc.services.media.plan.copyable` reads, set to a SubsPlease-style
#: 1080p H.264 High 8-bit stream with AAC-LC audio, so the video and the audio
#: are both copied.
REMUX_SOURCE_PROBE: dict[str, Any] = {
    "format": SOURCE_PROBE["format"],
    "streams": [
        {
            **SOURCE_PROBE["streams"][0],
            "profile": "High",
            "level": 40,
            "pix_fmt": "yuv420p",
            "field_order": "progressive",
        },
        {**SOURCE_PROBE["streams"][1], "profile": "LC"},
        *SOURCE_PROBE["streams"][2:],
    ],
}

#: And a source the remux must refuse: 10-bit H.264 (Hi10P), the classic
#: fansub encode Safari will not play.
HI10P_SOURCE_PROBE: dict[str, Any] = {
    "format": SOURCE_PROBE["format"],
    "streams": [
        {**REMUX_SOURCE_PROBE["streams"][0], "profile": "High 10", "pix_fmt": "yuv420p10le"},
        *REMUX_SOURCE_PROBE["streams"][1:],
    ],
}

#: And about a finished playlist. ``start_time`` is the first video frame's,
#: which a remux records for the subtitle segments' ``X-TIMESTAMP-MAP``.
PLAYLIST_PROBE: dict[str, Any] = {
    "format": {"format_name": "hls", "duration": "12.0"},
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "start_time": "0.083000",
        },
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    ],
}

#: And about a finished small offline copy (FR-P6): 720p H.264 and AAC, as
#: long as the source.
OFFLINE_PROBE: dict[str, Any] = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.0"},
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "codec_tag_string": "avc1",
            "width": 1280,
            "height": 720,
        },
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    ],
}

#: The subtitle track the fake remux extracts: two lines of dialogue (one in
#: the first segment, one in the third of a 12 s, 3-segment rendition) and a
#: sign that the WebVTT conversion drops.
REMUX_ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment
Style: Default,Arial,48,&H00FFFFFF,0,0,2
Style: Sign,Arial,48,&H00FFFFFF,0,0,8

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,Hello there.
Dialogue: 0,0:00:02.00,0:00:04.00,Sign,,0,0,0,,{\\pos(100,100)}SHOP
Dialogue: 0,0:00:09.00,0:00:10.50,Default,,0,0,0,,{\\i1}See you{\\i0} later.
"""

_FFPROBE = """\
import json, sys
SOURCE = {source}
PLAYLIST = {playlist}
OFFLINE = {offline}
target = sys.argv[-1]
if target.endswith(".m3u8"):
    print(json.dumps(PLAYLIST))
elif target.endswith("offline.mp4"):
    print(json.dumps(OFFLINE))
else:
    print(json.dumps(SOURCE))
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
FASTSTART = {faststart!r}
SUBTITLE_ASS = {subtitle_ass!r}

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

if "-c:v" in args and args[args.index("-c:v") + 1] == "copy":
    # The remux (M20): fMP4 HLS into the working directory, a VOD
    # ``video.m3u8`` naming every segment, and — when the arguments carry a
    # second output — the subtitle track written to it.
    note("remux", args)
    if FAIL:
        for line in range(60):
            print("[hls @ 0x1] stub complaint line %d" % line, file=sys.stderr)
        print("Error opening output file video.m3u8.", file=sys.stderr)
        sys.exit(1)
    step = DURATION / max(SEGMENTS, 1)
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:%d" % max(int(step + 0.999), 1),
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS",
             '#EXT-X-MAP:URI="init.mp4"']
    for index in range(SEGMENTS):
        if SLEEP:
            time.sleep(SLEEP)
        if not BROKEN:
            with open("init.mp4", "wb") as handle:
                handle.write(b"init" * 4)
            with open("seg_%05d.m4s" % index, "wb") as handle:
                handle.write(b"segment-%05d" % index * 10)
        lines.append("#EXTINF:%.6f," % step)
        lines.append("seg_%05d.m4s" % index)
        print("out_time_us=%d" % int((index + 1) * step * 1_000_000))
        print("progress=continue")
        sys.stdout.flush()
    print("progress=end")
    lines.append("#EXT-X-ENDLIST")
    with open("video.m3u8", "w") as handle:
        handle.write("" if BROKEN else "\\n".join(lines) + "\\n")
    if "-c:s" in args:
        target = args[-1]
        with open(target, "w") as handle:
            if target.endswith(".ass"):
                handle.write(SUBTITLE_ASS)
            else:
                handle.write("1\\n00:00:01,500 --> 00:00:03,000\\nHello there.\\n\\n")
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

if "-movflags" in args:
    # The small offline copy: one progressive MP4, its boxes in faststart
    # order unless told otherwise.
    import struct
    def box(kind, body=b""):
        return struct.pack(">I", 8 + len(body)) + kind + body
    for index in range(SEGMENTS):
        if SLEEP:
            time.sleep(SLEEP)
        print("out_time_us=%d" % int((index + 1) * DURATION / max(SEGMENTS, 1) * 1_000_000))
        print("progress=continue")
        sys.stdout.flush()
    print("progress=end")
    parts = [box(b"ftyp", b"isom"), box(b"moov", b"m" * 32), box(b"mdat", b"v" * 256)]
    if not FASTSTART:
        parts = [parts[0], parts[2], parts[1]]
    with open(args[-1], "wb") as handle:
        handle.write(b"".join(parts))
    sys.exit(0)

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
    offline_probe: dict[str, Any] | None = None,
    faststart: bool = True,
    subtitle_ass: str | None = None,
) -> Path:
    """Put a fake ``ffmpeg`` and ``ffprobe`` first on ``PATH``.

    ``marker`` is a file each invocation appends its kind to, so a test can
    assert that the fonts were dumped before the encode without parsing a log.
    The encode also writes its whole argument vector, NUL-separated, to
    ``<marker>.argv`` — which is how "the configured preset actually reached
    ffmpeg" is asserted without stubbing the subprocess away.
    ``filters`` is what it claims to support: pass ``()`` for the Homebrew-style
    build with no libass. ``offline_probe`` is what ffprobe says about a
    finished small copy, and ``faststart=False`` makes the fake write ``mdat``
    before ``moov`` — the two ways a copy's validation is tested.
    ``subtitle_ass`` is the track a remux extracts (:data:`REMUX_ASS` by
    default).
    """
    subtitle_ass = REMUX_ASS if subtitle_ass is None else subtitle_ass
    write_script(
        directory,
        "ffprobe",
        _FFPROBE.format(
            source=json.dumps(source_probe or SOURCE_PROBE),
            playlist=json.dumps(PLAYLIST_PROBE),
            offline=json.dumps(offline_probe or OFFLINE_PROBE),
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
            faststart=faststart,
            subtitle_ass=subtitle_ass,
        ),
    )
    monkeypatch.setenv("PATH", str(directory), prepend=os.pathsep)
    # "what can this ffmpeg do?" is cached per binary name for the life of the
    # process, and every test installs a different binary under the same name.
    reset_filters()
    return directory


__all__ = [
    "HI10P_SOURCE_PROBE",
    "OFFLINE_PROBE",
    "REMUX_ASS",
    "REMUX_SOURCE_PROBE",
    "PLAYLIST_PROBE",
    "SOURCE_PROBE",
    "install_fake_ffmpeg",
    "write_script",
]
