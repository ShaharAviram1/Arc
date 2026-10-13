"""Text subtitles for the remuxed rendition: ASS/SRT → WebVTT, and the HLS
playlists that carry them (M20, architecture.md §5.3c).

Everything here is **pure**: strings in, strings out. The remux job extracts
the chosen track to ``_work/sub.ass`` or ``_work/sub.srt`` (:func:`~arc.
services.media.plan.remux_args`), reads it, and hands the text to this module;
the media routes call the playlist builders with what is already on disk.

The conversion is deliberately lossy. WebVTT cannot position or style the way
ASS does, so the rule is "dialogue only, readable": an event that exists to be
*placed* — a sign, a song lyric, karaoke, a vector drawing — is dropped rather
than shown at the bottom of the screen out of context; what is left keeps its
italics and, when it was meant to sit at the top, goes to the top.

A cue goes into exactly one HLS subtitle segment, the one it **starts** in.
Repeating a long cue in each segment it overlaps is what the HLS spec allows,
but Safari then shows it twice.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from arc.services.media.plan import VIDEO_PLAYLIST_NAME

#: The two names the master playlist points at, and the subtitle segment name.
SUBTITLES_PLAYLIST_NAME: Final[str] = "subs.m3u8"
SUBTITLE_SEGMENT_TEMPLATE: Final[str] = "sub_{index:05d}.vtt"
SUBTITLE_GROUP: Final[str] = "subs"

#: The cue setting a top-aligned line gets.
TOP_LINE: Final[str] = "line:5%"

#: Styles whose events are signs, songs or typesetting, not dialogue. Matched
#: as whole words after ``_`` and ``-`` become spaces, so ``Sign_Basic``,
#: ``OP-Romaji`` and ``Episode Title`` all match and ``Default`` does not.
TYPESET_STYLE: Final[re.Pattern[str]] = re.compile(
    r"\b(sign|signs|song|songs|op|ed|kara|karaoke|kfx|ts|title)\b", re.IGNORECASE
)

#: Override tags that mean the event is placed or animated: positioning,
#: movement, karaoke timing, and drawings (``\p1`` and up).
_TYPESET_TAG: Final[re.Pattern[str]] = re.compile(r"\\(?:pos\(|move\(|(?:k|K|kf|ko)\d|p[1-9])")

_OVERRIDE_BLOCK: Final[re.Pattern[str]] = re.compile(r"\{([^}]*)\}")
_ITALIC_TAG: Final[re.Pattern[str]] = re.compile(r"\\i(\d*)(?=\\|$)")
#: ``\an7``–``\an9`` (numpad layout) and the legacy ``\a5``–``\a7`` (SSA).
_TOP_TAG: Final[re.Pattern[str]] = re.compile(r"\\(?:an[789]|a[567])(?!\d)")
_ASS_TIME: Final[re.Pattern[str]] = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})(?:[.,](\d+))?\s*$")
_SRT_TIME: Final[re.Pattern[str]] = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
#: Tag-like only: a letter right after ``<`` (or ``</``), so "x < y and y > z"
#: is text, not a tag.
_HTML_TAG: Final[re.Pattern[str]] = re.compile(r"</?[a-zA-Z][^<>]*>")
_ITALIC_HTML: Final[re.Pattern[str]] = re.compile(r"<\s*(/?)\s*i\s*>", re.IGNORECASE)
_EXTINF: Final[re.Pattern[str]] = re.compile(r"^#EXTINF:\s*([0-9]*\.?[0-9]+)")

_ITALIC_ON: Final[str] = "\x01"
_ITALIC_OFF: Final[str] = "\x02"


@dataclass(frozen=True, slots=True)
class Cue:
    """One WebVTT cue. ``text`` is already escaped and may hold ``<i>`` tags
    and ``\\n`` line breaks; ``line`` is a cue setting such as ``line:5%``."""

    start_s: float
    end_s: float
    text: str
    line: str | None = None


# --- Shared text handling ---------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render(marked: str) -> str:
    """Escape text carrying italic markers and turn the markers into tags.

    Line by line, so every line's tags are balanced on that line: an italic
    open at a line break is closed there and reopened on the next line.
    Unbalanced markers are tolerated (a second "on" is ignored, a stray "off"
    dropped). Blank lines are removed, because a blank line ends a WebVTT cue,
    and an ``<i></i>`` that wraps nothing disappears.
    """
    rendered: list[str] = []
    italic = False
    for raw_line in marked.split("\n"):
        out: list[str] = ["<i>"] if italic else []
        for char in raw_line:
            if char == _ITALIC_ON:
                if not italic:
                    out.append("<i>")
                    italic = True
            elif char == _ITALIC_OFF:
                if italic:
                    out.append("</i>")
                    italic = False
            else:
                out.append(_escape(char))
        if italic:
            out.append("</i>")
        line = "".join(out)
        while "<i></i>" in line:
            line = line.replace("<i></i>", "")
        line = re.sub(r"\s+</i>$", "</i>", re.sub(r"^<i>\s+", "<i>", line.strip()))
        if line.removeprefix("<i>").removesuffix("</i>").strip():
            rendered.append(line)
    return "\n".join(rendered)


def _finish(cues: Iterable[Cue]) -> list[Cue]:
    """Drop empty cues and exact duplicates; sort by start (then end)."""
    seen: set[tuple[float, float, str]] = set()
    kept: list[Cue] = []
    for cue in cues:
        key = (cue.start_s, cue.end_s, cue.text)
        if not cue.text or cue.end_s <= cue.start_s or key in seen:
            continue
        seen.add(key)
        kept.append(cue)
    kept.sort(key=lambda cue: (cue.start_s, cue.end_s))
    return kept


# --- ASS ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Style:
    italic: bool
    top: bool


def _ass_time(value: str) -> float | None:
    match = _ASS_TIME.match(value)
    if match is None:
        return None
    hours, minutes, seconds, fraction = match.groups()
    frac = float(f"0.{fraction}") if fraction else 0.0
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + frac


def _fields(line: str) -> list[str]:
    return [part.strip().lower() for part in line.split(":", 1)[1].split(",")]


def _ass_bool(value: str) -> bool:
    try:
        return int(value.strip()) != 0
    except ValueError:
        return False


def _parse_styles(lines: Sequence[str]) -> dict[str, _Style]:
    """Style name (lowercased) → italic / top-aligned, from the styles section."""
    styles: dict[str, _Style] = {}
    section = ""
    fmt: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
            fmt = []
            continue
        if section not in {"[v4+ styles]", "[v4 styles]"}:
            continue
        if line.lower().startswith("format:"):
            fmt = _fields(line)
        elif line.lower().startswith("style:") and fmt:
            values = [part.strip() for part in line.split(":", 1)[1].split(",", len(fmt) - 1)]
            record = dict(zip(fmt, values, strict=False))
            try:
                alignment = int(record.get("alignment", "2"))
            except ValueError:
                alignment = 2
            # V4+ uses the numpad layout (7–9 top); SSA's V4 uses 1–3 bottom,
            # +4 top, +8 middle.
            top = alignment in {7, 8, 9} if section == "[v4+ styles]" else alignment in {5, 6, 7}
            name = record.get("name", "").lower()
            styles[name] = _Style(italic=_ass_bool(record.get("italic", "0")), top=top)
    return styles


def _is_typeset_style(style: str) -> bool:
    return TYPESET_STYLE.search(style.replace("_", " ").replace("-", " ")) is not None


def _ass_text(text: str, *, italic: bool) -> str:
    """ASS event text → escaped WebVTT cue text with ``<i>`` tags."""
    marked: list[str] = [_ITALIC_ON] if italic else []
    position = 0
    for block in _OVERRIDE_BLOCK.finditer(text):
        marked.append(text[position : block.start()])
        for match in _ITALIC_TAG.finditer(block.group(1)):
            # ``\i1`` on; ``\i0`` off; a bare ``\i`` resets to the style.
            value = match.group(1)
            on = value != "0" if value else italic
            marked.append(_ITALIC_ON if on else _ITALIC_OFF)
        position = block.end()
    marked.append(text[position:])
    plain = "".join(marked)
    plain = plain.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", "\u00a0")
    return _render(plain)


def ass_to_cues(text: str) -> list[Cue]:
    """The dialogue of an ASS/SSA script as WebVTT cues.

    Reads the ``[Events]`` ``Format:`` line for the field order, skips
    ``Comment:`` lines, drops typesetting (a style named like a sign or song,
    or text with ``\\pos``/``\\move``/karaoke/drawing tags), keeps italics, puts
    top-aligned lines at the top, removes exact duplicates and sorts by start.
    """
    lines = text.lstrip("\ufeff").splitlines()
    styles = _parse_styles(lines)
    cues: list[Cue] = []
    section = ""
    fmt: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
            fmt = []
            continue
        if section != "[events]":
            continue
        lowered = line.lower()
        if lowered.startswith("format:"):
            fmt = _fields(line)
            continue
        if not lowered.startswith("dialogue:") or not fmt or "text" not in fmt:
            continue
        values = line.split(":", 1)[1].split(",", len(fmt) - 1)
        if len(values) != len(fmt):
            continue
        record = dict(zip(fmt, values, strict=True))
        start = _ass_time(record.get("start", ""))
        end = _ass_time(record.get("end", ""))
        if start is None or end is None:
            continue
        style_name = record.get("style", "").strip()
        body = record["text"]
        blocks = "".join(match.group(1) for match in _OVERRIDE_BLOCK.finditer(body))
        if _is_typeset_style(style_name) or _TYPESET_TAG.search(blocks):
            continue
        style = styles.get(style_name.lower(), styles.get(style_name.lstrip("*").lower()))
        top = bool(_TOP_TAG.search(blocks)) or (style is not None and style.top)
        cue_text = _ass_text(body, italic=style is not None and style.italic)
        cues.append(Cue(start, end, cue_text, TOP_LINE if top else None))
    return _finish(cues)


# --- SRT ----------------------------------------------------------------------


def _srt_text(lines: Sequence[str]) -> tuple[str, bool]:
    """SRT cue lines → (escaped text with ``<i>`` only, top-aligned?)."""
    joined = "\n".join(lines)
    blocks = "".join(match.group(1) for match in _OVERRIDE_BLOCK.finditer(joined))
    top = bool(_TOP_TAG.search(blocks))
    joined = _OVERRIDE_BLOCK.sub("", joined)
    joined = _ITALIC_HTML.sub(lambda m: _ITALIC_OFF if m.group(1) else _ITALIC_ON, joined)
    joined = _HTML_TAG.sub("", joined)
    return _render(html.unescape(joined)), top


def srt_to_cues(text: str) -> list[Cue]:
    """An SRT file as WebVTT cues: HTML stripped except ``<i>``, escaped."""
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = block.strip("\n").split("\n")
        for at, line in enumerate(lines):
            match = _SRT_TIME.search(line)
            if match is None:
                continue
            h1, m1, s1, f1, h2, m2, s2, f2 = match.groups()
            start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(f1.ljust(3, "0")) / 1000
            end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(f2.ljust(3, "0")) / 1000
            cue_text, top = _srt_text(lines[at + 1 :])
            cues.append(Cue(start, end, cue_text, TOP_LINE if top else None))
            break
    return _finish(cues)


# --- WebVTT -------------------------------------------------------------------


def vtt_timestamp(seconds: float) -> str:
    """``HH:MM:SS.mmm``, rounded to the millisecond."""
    millis = max(round(seconds * 1000), 0)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _cue_block(cue: Cue) -> str:
    timing = f"{vtt_timestamp(cue.start_s)} --> {vtt_timestamp(cue.end_s)}"
    if cue.line:
        timing += f" {cue.line}"
    return f"{timing}\n{cue.text}\n"


def cues_to_vtt(cues: Iterable[Cue]) -> str:
    """A whole WebVTT file."""
    return "WEBVTT\n\n" + "\n".join(_cue_block(cue) for cue in cues)


#: The timing line :func:`_cue_block` writes, read back by :func:`vtt_to_cues`.
_VTT_TIMING: Final[re.Pattern[str]] = re.compile(
    r"^(\d+):(\d{2}):(\d{2})\.(\d{3})\s+-->\s+(\d+):(\d{2}):(\d{2})\.(\d{3})(?:\s+(.*))?$"
)


def _vtt_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def vtt_to_cues(text: str) -> list[Cue]:
    """The cues of a WebVTT file :func:`cues_to_vtt` wrote, read back.

    The remux stores one ``subs.vtt`` per rendition and the media route cuts
    it into segments on request (:func:`vtt_segment`), so it has to read its
    own output; it is not a general WebVTT parser (no ``NOTE``, ``STYLE`` or
    cue identifiers, which :func:`cues_to_vtt` never writes). A block whose
    timing line does not parse is skipped. Added in M20 T2.
    """
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip("\n").split("\n")
        if not lines:
            continue
        match = _VTT_TIMING.match(lines[0].strip())
        if match is None:
            continue
        groups = match.groups()
        start = _vtt_seconds(*groups[0:4])
        end = _vtt_seconds(*groups[4:8])
        body = "\n".join(line for line in lines[1:] if line.strip())
        if body:
            cues.append(Cue(start, end, body, groups[8] or None))
    return cues


def vtt_segment(cues: Iterable[Cue], start_s: float, end_s: float, mpegts: int) -> str:
    """One HLS subtitle segment: the cues that **start** in ``[start_s, end_s)``.

    ``mpegts`` is the 90 kHz timestamp of the rendition's first video frame;
    the ``X-TIMESTAMP-MAP`` header ties the cue times (which are from 0) to
    it, so the cues line up with video whose first PTS is not zero.
    """
    header = f"WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:{int(mpegts)},LOCAL:00:00:00.000\n\n"
    inside = [cue for cue in cues if start_s <= cue.start_s < end_s]
    return header + "\n".join(_cue_block(cue) for cue in inside)


# --- Playlists ----------------------------------------------------------------


def segment_bounds(video_playlist_text: str) -> list[tuple[float, float]]:
    """``(start, end)`` in seconds of each segment of a media playlist,
    accumulated from its ``#EXTINF`` durations."""
    bounds: list[tuple[float, float]] = []
    position = 0.0
    for line in video_playlist_text.splitlines():
        match = _EXTINF.match(line.strip())
        if match is None:
            continue
        duration = float(match.group(1))
        bounds.append((position, position + duration))
        position += duration
    return bounds


def subtitles_playlist(
    bounds: Sequence[tuple[float, float]], *, ended: bool, target_duration: int = 20
) -> str:
    """The subtitle media playlist: one ``sub_NNNNN.vtt`` per video segment.

    The same boundaries as the video, so segment *n* of each covers the same
    time. VOD with ``#EXT-X-ENDLIST`` once the video is complete; EVENT, open,
    while it is still being written.

    ``#EXT-X-TARGETDURATION`` is ``target_duration`` as given, never derived
    from the segments: an EVENT playlist may not change it as it grows. The
    caller chooses it to match the video playlist (20 in the M20 plan, §2.1)
    and is responsible for it being at least the longest segment.
    """
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max(int(target_duration), 1)}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        f"#EXT-X-PLAYLIST-TYPE:{'VOD' if ended else 'EVENT'}",
    ]
    for index, (start, end) in enumerate(bounds):
        lines.append(f"#EXTINF:{end - start:.6f},")
        lines.append(SUBTITLE_SEGMENT_TEMPLATE.format(index=index))
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _attribute(value: str) -> str:
    """A quoted-string attribute value: no quotes, commas are fine, no newlines."""
    return "".join(char for char in value if char not in '"\r\n')


def master_playlist(
    *,
    bandwidth: int,
    codecs: str,
    resolution: tuple[int, int] | None,
    subtitle_name: str | None,
    subtitle_lang: str | None = None,
) -> str:
    """The master playlist of a remuxed rendition.

    One video variant (``video.m3u8``) and, when ``subtitle_name`` is given,
    one subtitle rendition on by default (``subs.m3u8``). ``subtitle_name`` is
    the label players show ("English"); ``subtitle_lang`` the ISO 639-1 code.
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    stream = [f"BANDWIDTH={max(int(bandwidth), 1)}", f'CODECS="{_attribute(codecs)}"']
    if resolution is not None:
        stream.append(f"RESOLUTION={int(resolution[0])}x{int(resolution[1])}")
    if subtitle_name is not None:
        media = [
            "TYPE=SUBTITLES",
            f'GROUP-ID="{SUBTITLE_GROUP}"',
        ]
        if subtitle_lang:
            media.append(f'LANGUAGE="{_attribute(subtitle_lang)}"')
        media += [
            f'NAME="{_attribute(subtitle_name) or "Subtitles"}"',
            "DEFAULT=YES",
            "AUTOSELECT=YES",
            f'URI="{SUBTITLES_PLAYLIST_NAME}"',
        ]
        lines.append("#EXT-X-MEDIA:" + ",".join(media))
        stream.append(f'SUBTITLES="{SUBTITLE_GROUP}"')
    lines.append("#EXT-X-STREAM-INF:" + ",".join(stream))
    lines.append(VIDEO_PLAYLIST_NAME)
    return "\n".join(lines) + "\n"


__all__ = [
    "SUBTITLES_PLAYLIST_NAME",
    "SUBTITLE_GROUP",
    "SUBTITLE_SEGMENT_TEMPLATE",
    "TOP_LINE",
    "TYPESET_STYLE",
    "Cue",
    "ass_to_cues",
    "cues_to_vtt",
    "master_playlist",
    "segment_bounds",
    "srt_to_cues",
    "subtitles_playlist",
    "vtt_segment",
    "vtt_timestamp",
    "vtt_to_cues",
]
