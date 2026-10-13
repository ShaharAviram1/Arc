"""M20 T1: ASS/SRT → WebVTT and the HLS subtitle playlists (pure)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from arc.services.media.subtitles import (
    TOP_LINE,
    Cue,
    ass_to_cues,
    cues_to_vtt,
    master_playlist,
    segment_bounds,
    srt_to_cues,
    subtitles_playlist,
    vtt_segment,
)

FIXTURES = Path(__file__).parent / "fixtures"
EVENTS = (
    "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)

_TIMING = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2}):(\d{2}):(\d{2})\.(\d{3})(.*)$")


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _parse_vtt(text: str) -> tuple[list[str], list[Cue]]:
    """A tiny WebVTT reader: the header lines and the cues, strictly."""
    blocks = text.split("\n\n")
    header = blocks[0].split("\n")
    assert header[0] == "WEBVTT"
    cues: list[Cue] = []
    for block in blocks[1:]:
        if not block.strip():
            continue
        lines = block.strip("\n").split("\n")
        match = _TIMING.match(lines[0])
        assert match is not None, lines[0]
        g = match.groups()
        settings = g[8].strip() or None
        body = "\n".join(lines[1:])
        assert body and "-->" not in body
        cues.append(Cue(_seconds(*g[0:4]), _seconds(*g[4:8]), body, settings))
    return header, cues


def _ass_cues() -> list[Cue]:
    return ass_to_cues((FIXTURES / "sample.ass").read_text())


def test_ass_keeps_only_dialogue_with_italics() -> None:
    cues = _ass_cues()
    assert [cue.text for cue in cues] == [
        "Where is Miss Philly?",
        "<i>A teleportation</i> circle &amp; more &lt;here&gt;.",
        "<i>Which means...</i>",
        "Second line, with a comma\nand a break.",
        "Up there.",
        "Also up there, friend.",
    ]
    texts = " ".join(cue.text for cue in cues)
    for dropped in ("comment", "ROYAL", "Guild", "ke", "Sora", "m 0 0"):
        assert dropped not in texts


def test_ass_sorted_deduplicated_and_timed() -> None:
    cues = _ass_cues()
    assert [cue.start_s for cue in cues] == sorted(cue.start_s for cue in cues)
    assert (cues[0].start_s, cues[0].end_s) == (1.5, 3.25)
    assert sum(1 for cue in cues if cue.text == "Where is Miss Philly?") == 1


def test_ass_top_alignment() -> None:
    by_text = {cue.text: cue for cue in _ass_cues()}
    assert by_text["Up there."].line == TOP_LINE  # \an8
    assert by_text["Also up there, friend."].line == TOP_LINE  # Alignment 8 style
    assert by_text["Where is Miss Philly?"].line is None


@pytest.mark.parametrize(
    "style",
    ["Signs", "sign", "Song", "OP", "ED_Romaji", "Kara-FX", "KFX", "TS", "Episode Title"],
)
def test_ass_typeset_styles_dropped(style: str) -> None:
    script = f"{EVENTS}Dialogue: 0,0:00:01.00,0:00:02.00,{style},,0,0,0,,text\n"
    assert ass_to_cues(script) == []


@pytest.mark.parametrize("style", ["Default", "Main", "Italics", "Opening Narration", "Edward"])
def test_ass_dialogue_styles_kept(style: str) -> None:
    script = f"{EVENTS}Dialogue: 0,0:00:01.00,0:00:02.00,{style},,0,0,0,,text\n"
    assert [cue.text for cue in ass_to_cues(script)] == ["text"]


def test_ass_respects_the_format_line_order() -> None:
    script = (
        "[Events]\nFormat: Start, End, Style, Text\n"
        "Dialogue: 0:00:03.00,0:00:04.00,Default,third, and fourth\n"
    )
    assert ass_to_cues(script) == [Cue(3.0, 4.0, "third, and fourth")]


def test_ass_italic_across_a_line_break_is_balanced() -> None:
    script = (
        f"{EVENTS}"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}one\\Ntwo{\\i0} three\n"
    )
    assert [cue.text for cue in ass_to_cues(script)] == ["<i>one</i>\n<i>two</i> three"]


def test_srt_strips_html_except_italics() -> None:
    cues = srt_to_cues((FIXTURES / "sample.srt").read_text())
    assert [(cue.start_s, cue.text, cue.line) for cue in cues] == [
        (1.5, "Where is Miss Philly?", None),
        (2.0, "Line one\n<i>line two</i>", None),
        (4.0, "<i>A teleportation circle</i> &amp; Tom &amp; Jerry", None),
        (7.0, "Up there.", TOP_LINE),
    ]


def test_vtt_round_trips() -> None:
    cues = _ass_cues()
    header, parsed = _parse_vtt(cues_to_vtt(cues))
    assert header == ["WEBVTT"]
    assert parsed == cues


VIDEO_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:7
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.006000,
seg_00000.m4s
#EXTINF:6.006000,
seg_00001.m4s
#EXTINF:4.170833,
seg_00002.m4s
#EXT-X-ENDLIST
"""


def test_segment_bounds_matches_extinf_sums() -> None:
    bounds = segment_bounds(VIDEO_PLAYLIST)
    assert len(bounds) == 3
    assert bounds[0] == (0.0, 6.006)
    assert bounds[1][0] == pytest.approx(6.006) and bounds[1][1] == pytest.approx(12.012)
    assert bounds[2][1] == pytest.approx(6.006 + 6.006 + 4.170833)
    for (_, end), (start, _) in zip(bounds, bounds[1:], strict=False):
        assert end == start


def test_vtt_segment_places_each_cue_once() -> None:
    cues = _ass_cues()
    bounds = segment_bounds(VIDEO_PLAYLIST)
    seen: list[Cue] = []
    for start, end in bounds:
        segment = vtt_segment(cues, start, end, 7470)
        header, parsed = _parse_vtt(segment)
        assert header == ["WEBVTT", "X-TIMESTAMP-MAP=MPEGTS:7470,LOCAL:00:00:00.000"]
        assert all(start <= cue.start_s < end for cue in parsed)
        seen += parsed
    assert seen == cues
    # The cue at 4.0–6.0 straddles nothing; a long one starting in segment 0
    # and ending in segment 1 is only in segment 0.
    long = [Cue(5.0, 9.0, "long")]
    assert "long" in vtt_segment(long, *bounds[0], 0)
    assert "long" not in vtt_segment(long, *bounds[1], 0)


def test_subtitles_playlist_vod_and_event() -> None:
    bounds = segment_bounds(VIDEO_PLAYLIST)
    vod = subtitles_playlist(bounds, ended=True)
    assert vod.startswith("#EXTM3U\n")
    assert "#EXT-X-TARGETDURATION:20" in vod
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in vod and vod.rstrip().endswith("#EXT-X-ENDLIST")
    assert re.findall(r"sub_\d{5}\.vtt", vod) == ["sub_00000.vtt", "sub_00001.vtt", "sub_00002.vtt"]
    assert vod.count("#EXTINF:") == len(segment_bounds(VIDEO_PLAYLIST))
    assert segment_bounds(vod) == pytest.approx(bounds)

    event = subtitles_playlist(bounds[:2], ended=False)
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in event and "#EXT-X-ENDLIST" not in event
    assert event.count("#EXTINF:") == 2


def test_master_playlist_with_subtitles() -> None:
    text = master_playlist(
        bandwidth=8_160_156,
        codecs="avc1.640028,mp4a.40.2",
        resolution=(1920, 1080),
        subtitle_name="English",
        subtitle_lang="en",
    )
    lines = text.strip().split("\n")
    assert lines[0] == "#EXTM3U"
    media = next(line for line in lines if line.startswith("#EXT-X-MEDIA:"))
    for part in (
        "TYPE=SUBTITLES",
        'GROUP-ID="subs"',
        'LANGUAGE="en"',
        'NAME="English"',
        "DEFAULT=YES",
        "AUTOSELECT=YES",
        'URI="subs.m3u8"',
    ):
        assert part in media
    assert lines[-2] == (
        '#EXT-X-STREAM-INF:BANDWIDTH=8160156,CODECS="avc1.640028,mp4a.40.2",'
        'RESOLUTION=1920x1080,SUBTITLES="subs"'
    )
    assert lines[-1] == "video.m3u8"


def test_master_playlist_without_subtitles() -> None:
    text = master_playlist(
        bandwidth=5_000_000, codecs="avc1.640028,mp4a.40.2", resolution=None, subtitle_name=None
    )
    assert "#EXT-X-MEDIA" not in text and "SUBTITLES=" not in text
    assert text.strip().split("\n")[-2:] == [
        '#EXT-X-STREAM-INF:BANDWIDTH=5000000,CODECS="avc1.640028,mp4a.40.2"',
        "video.m3u8",
    ]


def test_subtitles_playlist_event_target_duration_is_fixed() -> None:
    growing = [(0.0, 6.0), (6.0, 15.0), (15.0, 21.0)]
    targets = {
        re.search(
            r"#EXT-X-TARGETDURATION:(\d+)", subtitles_playlist(growing[:n], ended=False)
        ).group(1)  # type: ignore[union-attr]
        for n in (1, 2, 3)
    }
    assert targets == {"20"}
    # A segment longer than the target does not move it: the caller chooses it.
    text = subtitles_playlist([(0.0, 30.0)], ended=False, target_duration=12)
    assert "#EXT-X-TARGETDURATION:12" in text


def test_srt_keeps_literal_angle_brackets() -> None:
    srt = "1\n00:00:01,000 --> 00:00:02,000\nx < y and y > z, <b>bold</b> <3\n"
    assert [cue.text for cue in srt_to_cues(srt)] == ["x &lt; y and y &gt; z, bold &lt;3"]
