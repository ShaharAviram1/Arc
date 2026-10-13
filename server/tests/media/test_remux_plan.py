"""M20 T1: the copyability probe and the remux argument vector (pure).

The probes under ``fixtures/`` are real ``ffprobe -show_streams -show_format``
output from the dev library (SubsPlease and Erai-raws 1080p releases). The
ones whose name ends in ``_edited`` are a copy of the SubsPlease probe with
the fields of one stream hand-edited, for sources not in the library (Hi10P,
HEVC, 4K, interlaced, FLAC audio).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arc.services.media.plan import (
    ASS_FILE,
    INIT_NAME,
    SEGMENT_PATTERN,
    SRT_FILE,
    VIDEO_PLAYLIST_NAME,
    Copyability,
    RemuxOptions,
    Stream,
    TranscodePlan,
    build_plan,
    copyable,
    remux_args,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = Path("/data/downloads/7/[SubsPlease] Show - 01 (1080p) [ABCDEF01].mkv")
OUT = Path("/data/renditions/7.tmp-1")


def _probe(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def _plan(payload: dict[str, Any]) -> TranscodePlan:
    return build_plan(payload, source=SOURCE, output_dir=OUT)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("probe_subsplease_1080p_h264_high", Copyability(True, True, None)),
        ("probe_erai_1080p_h264_aac_lc", Copyability(True, True, None)),
        ("probe_hi10p_edited", Copyability(False, True, "10-bit")),
        ("probe_hevc_edited", Copyability(False, True, "HEVC")),
        ("probe_4k_edited", Copyability(False, True, "above 1080p")),
        ("probe_interlaced_edited", Copyability(False, True, "interlaced")),
        ("probe_flac_audio_edited", Copyability(True, False, None)),
    ],
)
def test_copyable_fixtures(fixture: str, expected: Copyability) -> None:
    payload = _probe(fixture)
    assert copyable(payload, _plan(payload)) == expected


def _edit_video(**fields: Any) -> dict[str, Any]:
    payload = _probe("probe_subsplease_1080p_h264_high")
    video = next(s for s in payload["streams"] if s["codec_type"] == "video")
    for key, value in fields.items():
        if value is None:
            video.pop(key, None)
        else:
            video[key] = value
    return payload


@pytest.mark.parametrize(
    ("fields", "video", "reason"),
    [
        ({"pix_fmt": "yuvj420p"}, True, None),
        ({"profile": "Main"}, True, None),
        ({"profile": "Constrained Baseline"}, True, None),
        ({"field_order": None}, True, None),
        ({"height": 1088}, True, None),
        ({"level": 51}, True, None),
        ({"level": 52}, False, "H.264 level 5.2"),
        ({"pix_fmt": "yuv444p", "profile": "High 4:4:4 Predictive"}, False, "4:4:4"),
        ({"pix_fmt": "yuv422p", "profile": "High 4:2:2"}, False, "yuv422p pixel format"),
        ({"profile": "Extended"}, False, "H.264 Extended profile"),
        ({"codec_name": "mpeg4"}, False, "mpeg4 video"),
        ({"field_order": "bb"}, False, "interlaced"),
        ({"width": 2560, "height": 1440}, False, "above 1080p"),
    ],
)
def test_copyable_rules(fields: dict[str, Any], video: bool, reason: str | None) -> None:
    payload = _edit_video(**fields)
    result = copyable(payload, _plan(payload))
    assert (result.video, result.reason) == (video, reason)


def test_copyable_audio_profiles() -> None:
    payload = _probe("probe_subsplease_1080p_h264_high")
    audio = next(s for s in payload["streams"] if s["codec_type"] == "audio")
    audio["profile"] = "HE-AAC"
    assert copyable(payload, _plan(payload)).audio_copy is True
    audio["profile"] = "Main"
    assert copyable(payload, _plan(payload)).audio_copy is False
    audio["codec_name"], audio["profile"] = "opus", None
    assert copyable(payload, _plan(payload)).audio_copy is False


def test_copyable_reads_the_chosen_audio_stream() -> None:
    """A dual-audio file: the plan picks the Japanese track; that one decides."""
    payload = _probe("probe_subsplease_1080p_h264_high")
    dub = json.loads(json.dumps(next(s for s in payload["streams"] if s["codec_type"] == "audio")))
    dub.update(index=99, codec_name="flac", tags={"language": "eng"})
    dub.pop("profile")
    payload["streams"].insert(1, dub)
    plan = _plan(payload)
    assert plan.audio is not None and plan.audio.type_index == 1
    assert copyable(payload, plan).audio_copy is True


def test_copyable_without_audio() -> None:
    payload = _probe("probe_subsplease_1080p_h264_high")
    payload["streams"] = [s for s in payload["streams"] if s["codec_type"] != "audio"]
    assert copyable(payload, _plan(payload)) == Copyability(True, False, None)


# --- remux_args -------------------------------------------------------------


def _value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_remux_args_vod_shape() -> None:
    plan = _plan(_probe("probe_subsplease_1080p_h264_high"))
    args = remux_args(plan, RemuxOptions(audio_copy=True), live=False)

    assert args[:3] == ["-nostdin", "-hide_banner", "-y"]
    assert "-vf" not in args and "-filter_complex" not in args
    assert args.count("-c:v") == 1 and _value(args, "-c:v") == "copy"
    assert _value(args, "-i") == str(SOURCE)
    assert "pipe:0" not in args
    assert _value(args, "-c:a") == "copy"
    assert "-b:a" not in args
    # Maps, in order: video, audio, then the subtitle output's track.
    maps = [args[i + 1] for i, arg in enumerate(args) if arg == "-map"]
    assert maps == ["0:v:0", "0:a:0", "0:s:0"]
    assert _value(args, "-f") == "hls"
    assert _value(args, "-hls_time") == "6"
    assert _value(args, "-hls_segment_type") == "fmp4"
    assert _value(args, "-hls_flags") == "independent_segments+temp_file"
    assert _value(args, "-hls_fmp4_init_filename") == INIT_NAME
    assert _value(args, "-hls_segment_filename") == SEGMENT_PATTERN
    assert _value(args, "-hls_playlist_type") == "vod"
    assert _value(args, "-progress") == "pipe:1"
    # The playlist is the first output; the extracted ASS track the second.
    playlist_at = args.index(VIDEO_PLAYLIST_NAME)
    assert args[playlist_at + 1 :] == ["-map", "0:s:0", "-c:s", "copy", "-f", "ass", ASS_FILE]


def test_remux_args_live_and_audio_encode() -> None:
    plan = _plan(_probe("probe_flac_audio_edited"))
    args = remux_args(plan, RemuxOptions(audio_bitrate="192k"), live=True)

    assert args[args.index("-i") - 2 : args.index("-i") + 2] == [
        "-f",
        "matroska",
        "-i",
        "pipe:0",
    ]
    assert str(SOURCE) not in args
    assert _value(args, "-hls_playlist_type") == "event"
    at = args.index("-c:a")
    assert args[at : at + 6] == ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    assert args.count("-c:v") == 1 and "-vf" not in args


def test_remux_args_live_flushes_the_subtitle_output_per_packet() -> None:
    """Live (M20 T6): the ASS file grows as events are muxed, not at exit."""
    plan = _plan(_probe("probe_subsplease_1080p_h264_high"))
    live = remux_args(plan, live=True)
    playlist_at = live.index(VIDEO_PLAYLIST_NAME)
    assert live[playlist_at + 1 :] == [
        "-map", "0:s:0", "-c:s", "copy", "-flush_packets", "1", "-f", "ass", ASS_FILE,
    ]  # fmt: skip
    # Only the subtitle output: the HLS output keeps its own buffering.
    assert live.index("-flush_packets") > playlist_at
    assert "-flush_packets" not in remux_args(plan, live=False)


def _with_subtitle(codec: str | None) -> TranscodePlan:
    plan = _plan(_probe("probe_subsplease_1080p_h264_high"))
    subtitle = None if codec is None else Stream(index=5, type_index=2, codec=codec)
    return TranscodePlan(
        source=plan.source,
        output_dir=plan.output_dir,
        video=plan.video,
        audio=plan.audio,
        subtitle=subtitle,
        duration=plan.duration,
    )


def test_remux_args_srt_and_converted_text_tracks() -> None:
    args = remux_args(_with_subtitle("subrip"), live=False)
    assert args[args.index(VIDEO_PLAYLIST_NAME) + 1 :] == [
        "-map",
        "0:s:2",
        "-c:s",
        "copy",
        "-f",
        "srt",
        SRT_FILE,
    ]
    mov = remux_args(_with_subtitle("mov_text"), live=False)
    assert mov[mov.index(VIDEO_PLAYLIST_NAME) + 1 :][3] == "srt"


def test_remux_args_without_subtitles_ends_at_the_playlist() -> None:
    args = remux_args(_with_subtitle(None), live=False)
    assert args[-1] == VIDEO_PLAYLIST_NAME
    assert "-c:s" not in args
    skipped = remux_args(
        _plan(_probe("probe_subsplease_1080p_h264_high")),
        RemuxOptions(extract_subtitles=False),
        live=False,
    )
    assert skipped[-1] == VIDEO_PLAYLIST_NAME


def test_remux_args_no_audio() -> None:
    payload = _probe("probe_subsplease_1080p_h264_high")
    payload["streams"] = [s for s in payload["streams"] if s["codec_type"] != "audio"]
    args = remux_args(_plan(payload), live=False)
    assert "-an" in args and "-c:a" not in args
