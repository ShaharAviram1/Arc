"""The small offline copy's pure half (FR-P6): arguments, validation, states.

No database, no subprocess. ``offline_encode_args`` is asserted as an argument
vector because the order *is* the behaviour here — the scale before the
subtitles, ``-movflags`` before the output name — and the validation helpers
are fed captured shapes of what ffprobe says about a good and a bad copy.
"""

from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from arc.api.offline_schemas import OfflineOut
from arc.config import Settings
from arc.models import OfflineCopy, OfflineCopyState
from arc.services.media.copies import (
    CODEC_STRINGS,
    codec_string,
    made_otherwise,
    offline_options,
    offline_state,
    settings_key,
)
from arc.services.media.offline import (
    NO_SOURCE_DURATION,
    SHORTFALL,
    CopyProblem,
    check_probe,
    moov_before_mdat,
    probe_problem,
)
from arc.services.media.plan import (
    OFFLINE_OUTPUT,
    OfflineOptions,
    TranscodePlan,
    build_plan,
    offline_encode_args,
    offline_scale_filter,
)
from tests.media_helpers import OFFLINE_PROBE, SOURCE_PROBE


def _plan(
    probe: dict[str, Any] | None = None, tmp: Path = Path("/data/offline/7.tmp-1")
) -> TranscodePlan:
    return build_plan(
        probe or SOURCE_PROBE,
        source=Path("/data/downloads/7/[Group] Show - 03 [1080p].mkv"),
        output_dir=tmp,
    )


def _after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


# --- Arguments ----------------------------------------------------------------


def test_h264_scales_first_then_burns_the_subtitles() -> None:
    args = offline_encode_args(_plan(), OfflineOptions(subtitle_file="_work/sub.ass"))

    # One chain, in this order: libass draws on the 720p frame, so the glyphs
    # are rendered at the size they are shown at rather than scaled down.
    assert _after(args, "-vf") == (
        "scale=-2:'min(720,ih)':flags=bicubic,ass=_work/sub.ass:fontsdir=_work/fonts"
    )


def test_the_scale_never_upscales_a_480p_source() -> None:
    """``min(720,ih)``: a 480-line source keeps its 480 lines."""
    small = {
        **SOURCE_PROBE,
        "streams": [
            {**SOURCE_PROBE["streams"][0], "width": 854, "height": 480},
            *SOURCE_PROBE["streams"][1:],
        ],
    }
    args = offline_encode_args(_plan(small))

    assert "min(720,ih)" in _after(args, "-vf")
    assert offline_scale_filter(720) == "scale=-2:'min(720,ih)':flags=bicubic"
    # The expression, evaluated as ffmpeg would for ih = 480 and ih = 1080.
    assert min(720, 480) == 480 and min(720, 1080) == 720


def test_the_height_cap_follows_the_setting() -> None:
    args = offline_encode_args(_plan(), OfflineOptions(height=540))

    assert "min(540,ih)" in _after(args, "-vf")


def test_without_a_subtitle_only_the_scale_is_in_the_graph() -> None:
    args = offline_encode_args(_plan(), OfflineOptions(subtitle_file=None))

    assert _after(args, "-vf") == "scale=-2:'min(720,ih)':flags=bicubic"


def test_h264_is_high_profile_level_4_with_animation_tuning() -> None:
    args = offline_encode_args(_plan(), OfflineOptions(codec="h264", crf=26, preset="fast"))

    assert _after(args, "-c:v") == "libx264"
    assert _after(args, "-crf") == "26"
    assert _after(args, "-preset") == "fast"
    assert _after(args, "-tune") == "animation"
    assert _after(args, "-profile:v") == "high"
    assert _after(args, "-level") == "4.0"
    assert _after(args, "-pix_fmt") == "yuv420p"
    assert "-tag:v" not in args


def test_h264_level_follows_the_height_as_the_codec_string_does() -> None:
    """Both count the picture the copy has: ``min(cap, source height)``."""
    source = _plan()
    assert source.height == 1080
    at_1080 = offline_encode_args(source, OfflineOptions(codec="h264", height=1080))
    # A 1440 cap over a 1080p source still makes a 1080p copy: level 4.0.
    capped = offline_encode_args(source, OfflineOptions(codec="h264", height=1440))
    tall_source = replace(source, video=replace(source.video, height=2160))
    taller = offline_encode_args(tall_source, OfflineOptions(codec="h264", height=1440))

    assert _after(at_1080, "-level") == "4.0"
    assert _after(capped, "-level") == "4.0"
    assert codec_string("h264", min(1440, 1080)) == "avc1.640028"  # level 4.0 = 0x28
    assert _after(taller, "-level") == "5.1"
    assert codec_string("h264", min(1440, 2160)) == "avc1.640033"  # level 5.1 = 0x33


def test_hevc_is_tagged_hvc1_for_safari() -> None:
    args = offline_encode_args(_plan(), OfflineOptions(codec="hevc", crf=28, preset="medium"))

    assert _after(args, "-c:v") == "libx265"
    assert _after(args, "-tag:v") == "hvc1"
    assert _after(args, "-profile:v") == "main"
    assert _after(args, "-pix_fmt") == "yuv420p"
    assert _after(args, "-crf") == "28"
    assert _after(args, "-preset") == "medium"
    assert "-tune" not in args


def test_an_unknown_codec_is_refused() -> None:
    with pytest.raises(ValueError):
        offline_encode_args(_plan(), OfflineOptions(codec="av1"))


@pytest.mark.parametrize("codec", ["h264", "hevc"])
def test_audio_faststart_and_nothing_else_reaches_the_file(codec: str) -> None:
    args = offline_encode_args(_plan(), OfflineOptions(codec=codec, audio_bitrate="96k"))

    assert _after(args, "-c:a") == "aac"
    assert _after(args, "-b:a") == "96k"
    assert _after(args, "-ac") == "2"
    assert _after(args, "-movflags") == "+faststart"
    assert "-sn" in args and "-dn" in args
    assert _after(args, "-map_chapters") == "-1"
    assert _after(args, "-map_metadata") == "-1"
    assert _after(args, "-progress") == "pipe:1"
    # One relative output name, last, so ffmpeg's working directory decides
    # where it lands and the release name never reaches an output path.
    assert args[-1] == OFFLINE_OUTPUT
    assert "-f" not in args, "no HLS muxer: one progressive file"


def test_the_tracks_are_the_renditions_tracks() -> None:
    """The same plan: the Japanese audio and the English full subtitle track."""
    plan = _plan()
    args = offline_encode_args(plan)

    maps = [args[i + 1] for i, arg in enumerate(args) if arg == "-map"]
    assert maps == [f"0:v:{plan.video.type_index}", f"0:a:{plan.audio.type_index}"]  # type: ignore[union-attr]
    assert plan.audio_lang == "ja" and plan.subtitle_lang == "en"


def test_a_source_with_no_audio_gets_no_audio() -> None:
    silent = {
        **SOURCE_PROBE,
        "streams": [s for s in SOURCE_PROBE["streams"] if s["codec_type"] != "audio"],
    }
    args = offline_encode_args(_plan(silent))

    assert "-an" in args and "-c:a" not in args


def test_the_settings_become_the_options(test_database_url: str) -> None:
    settings = Settings(  # type: ignore[call-arg]
        env="test",
        database_url=test_database_url,
        offline_codec="hevc",
        offline_height=540,
        offline_crf=30,
        offline_preset="slow",
        offline_audio_bitrate="128k",
        _env_file=None,
    )
    options = offline_options(settings)

    assert (options.codec, options.height, options.crf, options.preset, options.audio_bitrate) == (
        "hevc",
        540,
        30,
        "slow",
        "128k",
    )


def test_the_defaults_are_the_owners(test_database_url: str) -> None:
    settings = Settings(env="test", database_url=test_database_url, _env_file=None)  # type: ignore[call-arg]

    assert offline_options(settings) == OfflineOptions()
    assert OfflineOptions() == OfflineOptions(
        codec="h264", height=720, crf=26, preset="fast", audio_bitrate="96k"
    )
    assert settings.offline_dir == (settings.data_dir / "offline").resolve()


def test_the_settings_key_moves_with_anything_that_changes_the_bytes() -> None:
    base = settings_key(OfflineOptions(), sub_lang="en", audio_lang="ja")

    assert base == settings_key(OfflineOptions(), sub_lang="en", audio_lang="ja")
    assert base != settings_key(OfflineOptions(crf=27), sub_lang="en", audio_lang="ja")
    assert base != settings_key(OfflineOptions(codec="hevc"), sub_lang="en", audio_lang="ja")
    assert base != settings_key(OfflineOptions(), sub_lang="pt", audio_lang="ja")
    assert len(base) == 16


@pytest.mark.parametrize(
    ("codec", "height", "expected"),
    [
        ("h264", 480, "avc1.640028"),
        ("h264", 720, "avc1.640028"),
        ("h264", 1080, "avc1.640028"),
        ("h264", 1440, "avc1.640033"),
        ("h264", 2160, "avc1.640033"),
        ("hevc", 540, "hvc1.1.6.L93.B0"),
        ("hevc", 720, "hvc1.1.6.L93.B0"),
        ("hevc", 721, "hvc1.1.6.L120.B0"),
        ("hevc", 1080, "hvc1.1.6.L120.B0"),
        ("hevc", 1081, "hvc1.1.6.L153.B0"),
        ("hevc", 2160, "hvc1.1.6.L153.B0"),
        # An unknown height reads as the smallest tier.
        ("h264", None, "avc1.640028"),
        ("hevc", None, "hvc1.1.6.L93.B0"),
        (None, 1080, None),
        ("vp9", 1080, None),
    ],
)
def test_the_codec_strings_are_rfc_6381_by_height(
    codec: str | None, height: int | None, expected: str | None
) -> None:
    assert codec_string(codec, height) == expected


def test_the_codec_table_covers_both_codecs() -> None:
    assert set(CODEC_STRINGS) == {"h264", "hevc"}
    for tiers in CODEC_STRINGS.values():
        assert tiers[-1][0] == 0


def _made(
    codec: str | None = "h264", height: int | None = 720, crf: int | None = 26
) -> OfflineCopy:
    return OfflineCopy(
        episode_id=1, codec=codec, height=height, crf=crf, audio_bitrate="96k" if codec else None
    )


def test_a_copy_made_otherwise_is_told_by_its_recorded_settings() -> None:
    old = OfflineOptions(codec="h264", height=720, crf=26)
    new = OfflineOptions(codec="hevc", height=1080, crf=28)

    assert not made_otherwise(_made(), old)
    assert made_otherwise(_made(), new), "the trip-4 case: codec and CRF differ"
    assert made_otherwise(_made(crf=27), old)
    assert made_otherwise(_made(), replace(old, audio_bitrate="128k"))
    # Never started: nothing recorded, not stale.
    assert not made_otherwise(_made(codec=None, height=None, crf=None), new)


def test_the_height_is_compared_like_with_like() -> None:
    cap_1080 = OfflineOptions(codec="h264", height=1080, crf=26)

    # Taller than the cap allows: stale.
    assert made_otherwise(_made(height=1080), OfflineOptions(codec="h264", height=720, crf=26))
    # A 720p copy under a 1080 cap: stale only if the source is known taller.
    assert made_otherwise(_made(height=720), cap_1080, source_height=1080)
    assert not made_otherwise(_made(height=720), cap_1080, source_height=720)
    assert not made_otherwise(_made(height=720), cap_1080, source_height=None)
    assert not made_otherwise(_made(height=1080), cap_1080, source_height=2160)


def test_offline_out_names_the_copys_own_level_once_it_exists() -> None:
    """The copy row's codec and height decide, not the host's current settings."""
    tall = OfflineCopy(
        episode_id=1, state=OfflineCopyState.READY, codec="hevc", height=1080, size=9
    )
    short = OfflineCopy(episode_id=2, state=OfflineCopyState.READY, codec="h264", height=720)

    made = OfflineOut.build(1, copy=tall, job=None, has_source=True, codec="h264", height=720)
    older = OfflineOut.build(2, copy=short, job=None, has_source=True, codec="hevc", height=1080)

    assert made.codecs == "hvc1.1.6.L120.B0"
    assert older.codecs == "avc1.640028"


def test_offline_out_names_the_hosts_level_for_a_copy_not_made_yet() -> None:
    out = OfflineOut.build(1, copy=None, job=None, has_source=True, codec="hevc", height=1080)

    assert out.state == "none"
    assert out.codecs == "hvc1.1.6.L120.B0"


# --- Validating a finished copy -------------------------------------------------


def _write_boxes(path: Path, *kinds: bytes) -> Path:
    path.write_bytes(b"".join(struct.pack(">I", 16) + kind + b"x" * 8 for kind in kinds))
    return path


def test_moov_before_mdat_is_faststart(tmp_path: Path) -> None:
    assert moov_before_mdat(_write_boxes(tmp_path / "a.mp4", b"ftyp", b"moov", b"mdat"))
    assert not moov_before_mdat(_write_boxes(tmp_path / "b.mp4", b"ftyp", b"mdat", b"moov"))
    assert not moov_before_mdat(_write_boxes(tmp_path / "c.mp4", b"ftyp", b"free"))


def test_a_64_bit_box_size_is_followed(tmp_path: Path) -> None:
    big = struct.pack(">I", 1) + b"free" + struct.pack(">Q", 24) + b"y" * 8
    path = tmp_path / "d.mp4"
    path.write_bytes(big + struct.pack(">I", 8) + b"moov")

    assert moov_before_mdat(path)


def test_garbage_is_not_faststart(tmp_path: Path) -> None:
    path = tmp_path / "e.mp4"
    path.write_bytes(b"#EXTM3U\n")
    assert not moov_before_mdat(path)
    assert not moov_before_mdat(tmp_path / "missing.mp4")
    zero = tmp_path / "f.mp4"
    zero.write_bytes(struct.pack(">I", 0) + b"ftyp")
    assert not moov_before_mdat(zero)


def _probe(**video: Any) -> dict[str, Any]:
    streams = [{**OFFLINE_PROBE["streams"][0], **video}, OFFLINE_PROBE["streams"][1]]
    return {**OFFLINE_PROBE, "streams": streams}


def test_a_good_copy_passes() -> None:
    assert (
        check_probe(OFFLINE_PROBE, codec="h264", max_height=720, duration=12.0, has_audio=True)
        is None
    )


@pytest.mark.parametrize(
    ("payload", "codec", "says"),
    [
        (_probe(codec_name="hevc"), "h264", "not 'h264'"),
        (_probe(height=1080), "h264", "1080 lines"),
        (_probe(codec_name="hevc", codec_tag_string="hev1"), "hevc", "'hvc1'"),
        ({**OFFLINE_PROBE, "format": {"duration": "4.0"}}, "h264", "4.0s long"),
        ({**OFFLINE_PROBE, "format": {}}, "h264", "no duration"),
        (
            {**OFFLINE_PROBE, "streams": [OFFLINE_PROBE["streams"][1]]},
            "h264",
            "one video stream, found 0",
        ),
        ({**OFFLINE_PROBE, "streams": [OFFLINE_PROBE["streams"][0]]}, "h264", "one audio"),
        (None, "h264", "could not read"),
    ],
)
def test_a_wrong_copy_is_refused(payload: dict[str, Any] | None, codec: str, says: str) -> None:
    problem = check_probe(payload, codec=codec, max_height=720, duration=12.0, has_audio=True)

    assert problem is not None and says in problem


def test_a_duration_within_two_seconds_is_fine() -> None:
    near = {**OFFLINE_PROBE, "format": {"duration": "13.5"}}
    assert check_probe(near, codec="h264", max_height=720, duration=12.0, has_audio=True) is None


def test_a_source_with_no_duration_is_refused() -> None:
    """A real source always states a length; ``None`` is a broken file (owner, 2026-10-29)."""
    near = {**OFFLINE_PROBE, "format": {"duration": "13.5"}}

    problem = probe_problem(near, codec="h264", max_height=720, duration=None, has_audio=True)

    assert problem == CopyProblem(NO_SOURCE_DURATION, source=True)
    assert check_probe(near, codec="h264", max_height=720, duration=None, has_audio=True) == (
        NO_SOURCE_DURATION
    )


def test_a_short_copy_is_the_sources_fault() -> None:
    short = {**OFFLINE_PROBE, "format": {"duration": "1454.8"}}

    problem = probe_problem(short, codec="h264", max_height=720, duration=1500.1, has_audio=True)

    assert problem == CopyProblem(SHORTFALL.format(actual=1454.8, duration=1500.1), source=True)
    assert problem.message == "it is 1454.8s long; the source is 1500.1s"


@pytest.mark.parametrize(
    "payload",
    [
        _probe(codec_name="hevc"),
        _probe(height=1080),
        {**OFFLINE_PROBE, "format": {}},
        {**OFFLINE_PROBE, "streams": [OFFLINE_PROBE["streams"][0]]},
        None,
    ],
    ids=["codec", "height", "copy-without-duration", "no-audio", "unreadable-copy"],
)
def test_every_other_refusal_is_the_encoders(payload: dict[str, Any] | None) -> None:
    problem = probe_problem(payload, codec="h264", max_height=720, duration=12.0, has_audio=True)

    assert problem is not None and problem.source is False


def test_hevc_tagged_hvc1_passes() -> None:
    hevc = _probe(codec_name="hevc", codec_tag_string="hvc1")
    assert check_probe(hevc, codec="hevc", max_height=720, duration=12.0, has_audio=True) is None


# --- What a client is told ------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "has_source", "expected"),
    [
        (None, True, "none"),
        (None, False, "unavailable"),
        (OfflineCopyState.QUEUED, True, "queued"),
        (OfflineCopyState.QUEUED, False, "queued"),
        (OfflineCopyState.PREPARING, True, "preparing"),
        (OfflineCopyState.READY, False, "available"),
        (OfflineCopyState.FAILED, True, "failed"),
        (OfflineCopyState.FAILED, False, "unavailable"),
    ],
)
def test_the_six_states(state: OfflineCopyState | None, has_source: bool, expected: str) -> None:
    assert offline_state(copy_state=state, has_source=has_source) == expected


def test_the_live_event_carries_ids_and_the_copys_state() -> None:
    """``offline_copy`` (FR-P6): ids and a state only, like every other event."""
    import json

    from arc.services.events import OFFLINE_COPY, offline_copy_event

    payload = json.loads(offline_copy_event(anime_id=3, episode_id=9, state="ready").to_json())

    assert payload["kind"] == OFFLINE_COPY == "offline_copy"
    assert (payload["anime_id"], payload["episode_id"], payload["state"]) == (3, 9, "ready")
