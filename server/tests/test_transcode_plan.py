"""Track selection and argument building, over captured probe payloads (FR-P2).

Everything in :mod:`arc.services.media.plan` is pure, so these tests are the
whole of "which track would Arc pick?" — no ffmpeg, no filesystem, no database.
The payloads are trimmed copies of real ``ffprobe -show_streams`` output: the
sixteen-language Crunchyroll WEB-DL that M7 was built against, a dual-audio
fansub MKV, and the two shapes that have no usable subtitles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arc.services.media.plan import (
    ASS_FILE,
    FONTS_DIR,
    SRT_FILE,
    EncodeOptions,
    PlanError,
    attachment_name,
    build_plan,
    encode_args,
    font_extract_args,
    language_matches,
    normalise_language,
    subtitle_extract_args,
)

#: A source path with everything a filter graph would choke on: brackets, a
#: colon, an apostrophe, spaces and a comma. Every test builds against this one
#: so that "no release name reaches the filter" is asserted everywhere.
NASTY_SOURCE = Path("/data/downloads/697/[Erai-raws] Show: it's here, 11 [1080p][ABC].mkv")
OUTPUT = Path("/data/renditions/697")


def stream(index: int, kind: str, **extra: Any) -> dict[str, Any]:
    """One ffprobe stream entry: ``tags`` and ``disposition`` spelled out."""
    tags: dict[str, Any] = {}
    for key in ("language", "title", "filename"):
        if key in extra:
            tags[key] = extra.pop(key)
    disposition = {
        "default": int(extra.pop("default", 0)),
        "forced": int(extra.pop("forced", 0)),
        "attached_pic": int(extra.pop("attached_pic", 0)),
    }
    entry: dict[str, Any] = {"index": index, "codec_type": kind, "disposition": disposition}
    entry.update(extra)
    if tags:
        entry["tags"] = tags
    return entry


def payload(*streams: dict[str, Any], duration: str = "1420.23") -> dict[str, Any]:
    return {
        "format": {"format_name": "matroska,webm", "duration": duration},
        "streams": list(streams),
    }


def plan_for(*streams: dict[str, Any], sub_lang: str = "en", audio_lang: str = "ja", **kwargs: Any):
    return build_plan(
        payload(*streams, **kwargs),
        source=NASTY_SOURCE,
        output_dir=OUTPUT,
        sub_lang=sub_lang,
        audio_lang=audio_lang,
    )


#: The real thing, trimmed: episode 697's MultiSub release.
MULTISUB = (
    stream(
        0,
        "video",
        codec_name="h264",
        language="jpn",
        title="[Erai-raws]_AVC_CR",
        width=1920,
        height=1080,
        default=1,
    ),
    stream(1, "audio", codec_name="aac", language="jpn", title="[Erai-raws]_AAC_CR", default=1),
    stream(2, "subtitle", codec_name="ass", language="eng", title="CR_English", default=1),
    stream(3, "subtitle", codec_name="ass", language="por", title="CR_Portuguese(Brazil)"),
    stream(4, "subtitle", codec_name="ass", language="spa", title="CR_Spanish"),
    stream(5, "subtitle", codec_name="ass", language="ger", title="CR_German"),
    stream(6, "attachment", filename="Arial_2.ttf"),
    stream(7, "attachment", filename="timesbd_3.ttf"),
)


# --- Language tags ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "wanted", "expected"),
    [
        ("eng", "en", True),
        ("en", "en", True),
        ("en-US", "en", True),
        ("ENG ", "en", True),
        ("jpn", "ja", True),
        ("ger", "de", True),
        ("deu", "de", True),
        ("por", "en", False),
        ("und", "en", False),
        (None, "en", False),
        ("eng", "eng", True),
        ("en", "eng", True),
        # An exotic tag Arc has no alias table for still compares literally.
        ("swa", "swa", True),
        ("swa", "en", False),
    ],
)
def test_language_tags_are_compared_across_iso_families(
    tag: str | None, wanted: str, expected: bool
) -> None:
    assert language_matches(tag, wanted) is expected


def test_undetermined_is_not_a_language() -> None:
    assert normalise_language("und") is None
    assert normalise_language("") is None
    assert normalise_language("zxx") is None


# --- Track selection --------------------------------------------------------


def test_the_multisub_release_picks_english_and_japanese() -> None:
    plan = plan_for(*MULTISUB)
    assert plan.subtitle is not None and plan.subtitle.index == 2
    assert plan.subtitle.type_index == 0
    assert plan.subtitle_lang == "en"
    assert plan.audio is not None and plan.audio.index == 1
    assert plan.audio_lang == "ja"
    assert plan.video.index == 0
    assert (plan.width, plan.height) == (1920, 1080)
    assert plan.duration == pytest.approx(1420.23)
    assert plan.notes == ()


def test_the_subtitle_language_preference_beats_stream_order() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264", width=1920, height=1080),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="ass", language="por"),
        stream(3, "subtitle", codec_name="ass", language="eng"),
    )
    assert plan.subtitle is not None and plan.subtitle.index == 3
    assert plan.subtitle.type_index == 1


def test_ass_is_preferred_over_srt_in_the_same_language() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="subrip", language="eng", title="English"),
        stream(3, "subtitle", codec_name="ass", language="eng", title="English"),
    )
    assert plan.subtitle is not None and plan.subtitle.codec == "ass"
    assert plan.subtitle_file == ASS_FILE


def test_a_signs_and_songs_track_loses_to_the_dialogue_track() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="ass", language="eng", title="Signs & Songs", default=1),
        stream(3, "subtitle", codec_name="ass", language="eng", title="Full Subtitles"),
    )
    assert plan.subtitle is not None and plan.subtitle.index == 3


def test_signs_wins_only_when_it_is_the_only_english_track() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="ass", language="eng", title="Signs & Songs"),
    )
    assert plan.subtitle is not None and plan.subtitle.index == 2
    assert plan.subtitle_lang == "en"


def test_a_forced_track_loses_to_an_unforced_one() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="ass", language="eng", forced=1, default=1),
        stream(3, "subtitle", codec_name="ass", language="eng"),
    )
    assert plan.subtitle is not None and plan.subtitle.index == 3


def test_the_language_beats_every_other_preference() -> None:
    """An SRT dialogue track in English beats a default forced ASS in Spanish."""
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="ass", language="spa", title="Full", default=1),
        stream(3, "subtitle", codec_name="subrip", language="eng", forced=1),
    )
    assert plan.subtitle is not None and plan.subtitle.index == 3


def test_bitmap_subtitles_count_as_no_subtitles() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),
    )
    assert plan.subtitle is None
    assert plan.subtitle_lang is None
    assert plan.subtitle_file is None
    assert any("bitmap" in note and "hdmv_pgs_subtitle" in note for note in plan.notes)


def test_no_subtitle_track_at_all_is_a_note_not_a_failure() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
    )
    assert plan.subtitle is None
    assert any("no subtitle track" in note for note in plan.notes)


def test_a_subtitle_in_the_wrong_language_is_used_and_flagged() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="ass", language="por"),
    )
    assert plan.subtitle is not None and plan.subtitle_lang == "pt"
    assert any("no en subtitle track" in note for note in plan.notes)


def test_no_audio_in_the_wanted_language_falls_back_to_the_first() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="eng", title="English Dub"),
        stream(2, "audio", codec_name="ac3", language="spa"),
    )
    assert plan.audio is not None and plan.audio.index == 1
    assert plan.audio_lang == "en"
    assert any("no ja audio track" in note for note in plan.notes)


def test_the_configured_audio_language_is_honoured() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="eng"),
        stream(2, "audio", codec_name="aac", language="jpn"),
        audio_lang="ja",
    )
    assert plan.audio is not None and plan.audio.index == 2
    assert plan.audio.type_index == 1


def test_an_attached_cover_is_not_the_video_stream() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="mjpeg", attached_pic=1, width=600, height=800),
        stream(1, "video", codec_name="h264", width=1920, height=1080),
        stream(2, "audio", codec_name="aac", language="jpn"),
    )
    assert plan.video.index == 1
    assert plan.video.type_index == 1
    assert (plan.width, plan.height) == (1920, 1080)


def test_a_file_with_only_a_cover_cannot_be_planned() -> None:
    with pytest.raises(PlanError, match="no playable video stream"):
        plan_for(
            stream(0, "video", codec_name="mjpeg", attached_pic=1),
            stream(1, "audio", codec_name="aac"),
        )


def test_no_probe_at_all_cannot_be_planned() -> None:
    with pytest.raises(PlanError, match="ffprobe returned nothing"):
        build_plan(None, source=NASTY_SOURCE, output_dir=OUTPUT)


def test_a_file_with_no_audio_is_planned_without_one() -> None:
    plan = plan_for(stream(0, "video", codec_name="h264"))
    assert plan.audio is None
    assert plan.audio_lang is None
    assert "-an" in encode_args(plan)


def test_a_missing_duration_is_not_a_failure() -> None:
    plan = build_plan(
        {"format": {}, "streams": [stream(0, "video", codec_name="h264")]},
        source=NASTY_SOURCE,
        output_dir=OUTPUT,
    )
    assert plan.duration is None


# --- Attachment names -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Arial_2.ttf", "003_Arial_2.ttf"),
        ("../../../etc/passwd", "003_passwd"),
        ("..\\..\\windows\\system32\\a.ttf", "003_a.ttf"),
        ("/absolute/font.otf", "003_font.otf"),
        ("...", "003_font"),
        ("", "003_font"),
        (None, "003_font"),
        ("a b;rm -rf /.ttf", "003_ttf"),
    ],
)
def test_attachment_names_cannot_escape_the_fonts_directory(raw: str | None, expected: str) -> None:
    assert attachment_name(raw, 3) == expected


@pytest.mark.parametrize(
    ("first_raw", "second_raw"),
    [
        # The same name twice — a container muxed from two directories.
        ("font.ttf", "font.ttf"),
        # And two names that sanitising collapses into one.
        ("a b.ttf", "a;b.ttf"),
        # Including the pair that both sanitise away to nothing.
        ("...", ".."),
    ],
)
def test_attachments_that_would_share_a_name_still_get_two_files(
    first_raw: str, second_raw: str
) -> None:
    """The index prefix is what keeps two attachments apart.

    ffmpeg dumps every attachment in one command, so without it the second
    would land on top of the first and half the typesetting would render in
    the fallback face — the exact failure dumping the fonts exists to avoid.
    """
    assert attachment_name(first_raw, 0) != attachment_name(second_raw, 1)


def test_attachments_are_numbered_within_their_own_kind() -> None:
    plan = plan_for(*MULTISUB)
    assert plan.attachments == ((0, "000_Arial_2.ttf"), (1, "001_timesbd_3.ttf"))
    args = font_extract_args(plan)
    assert args is not None
    assert "-dump_attachment:t:0" in args
    assert f"{FONTS_DIR}/000_Arial_2.ttf" in args
    assert f"{FONTS_DIR}/001_timesbd_3.ttf" in args


def test_a_container_with_no_attachments_dumps_nothing() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
    )
    assert font_extract_args(plan) is None


# --- Argument building ------------------------------------------------------

#: Characters that would mean something to a shell. ffmpeg is never run through
#: one, so this is belt and braces — but it is also how the "no release name in
#: the filter graph" rule is asserted, since the fixture source has three of
#: them in its name.
SHELL_CHARS = ";|&`$><\n\r\\"


def test_the_encode_arguments_are_a_list_of_plain_strings() -> None:
    plan = plan_for(*MULTISUB)
    args = encode_args(plan, EncodeOptions(subtitle_file=ASS_FILE))
    assert isinstance(args, list)
    assert all(isinstance(arg, str) for arg in args)
    # The source is the one argument that carries the release name, and it is
    # passed whole, as one argv entry, never interpolated into anything.
    assert args.count(str(NASTY_SOURCE)) == 1
    for arg in args:
        if arg == str(NASTY_SOURCE):
            continue
        assert not any(char in arg for char in SHELL_CHARS), arg


def test_the_subtitle_filter_never_contains_the_source_name() -> None:
    plan = plan_for(*MULTISUB)
    args = encode_args(plan, EncodeOptions(subtitle_file=ASS_FILE))
    filter_arg = args[args.index("-vf") + 1]
    assert filter_arg == f"ass={ASS_FILE}:fontsdir={FONTS_DIR}"
    assert "Erai-raws" not in filter_arg
    assert "[" not in filter_arg


def test_an_srt_track_uses_the_subtitles_filter() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="subrip", language="eng"),
    )
    assert plan.subtitle_file == SRT_FILE
    args = encode_args(plan, EncodeOptions(subtitle_file=SRT_FILE))
    assert args[args.index("-vf") + 1].startswith(f"subtitles={SRT_FILE}")


def test_no_subtitle_file_means_no_filter() -> None:
    plan = plan_for(*MULTISUB)
    assert "-vf" not in encode_args(plan, EncodeOptions(subtitle_file=None))


def test_the_encode_maps_exactly_the_chosen_streams() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="mjpeg", attached_pic=1),
        stream(1, "video", codec_name="h264"),
        stream(2, "audio", codec_name="aac", language="eng"),
        stream(3, "audio", codec_name="aac", language="jpn"),
        stream(4, "subtitle", codec_name="ass", language="eng"),
    )
    args = encode_args(plan)
    assert args[args.index("-map") : args.index("-map") + 2] == ["-map", "0:v:1"]
    assert "0:a:1" in args
    # No subtitle stream reaches the output: it is in the picture now.
    assert "-sn" in args
    assert not any(arg.startswith("0:s:") for arg in args)


def test_the_hls_output_is_fmp4_vod_with_aligned_keyframes() -> None:
    plan = plan_for(*MULTISUB)
    args = encode_args(plan, EncodeOptions(segment_seconds=6))
    pairs = dict(zip(args, args[1:], strict=False))
    assert pairs["-f"] == "hls"
    assert pairs["-hls_segment_type"] == "fmp4"
    assert pairs["-hls_time"] == "6"
    assert pairs["-hls_playlist_type"] == "vod"
    assert pairs["-hls_flags"] == "independent_segments"
    assert pairs["-hls_fmp4_init_filename"] == "init.mp4"
    assert pairs["-hls_segment_filename"] == "seg_%05d.m4s"
    assert pairs["-force_key_frames"] == "expr:gte(t,n_forced*6)"
    assert args[-1] == "index.m3u8"


def test_the_encoder_settings_come_from_the_options() -> None:
    plan = plan_for(*MULTISUB)
    args = encode_args(
        plan, EncodeOptions(video_encoder="h264_vaapi", preset="fast", crf=23, segment_seconds=4)
    )
    pairs = dict(zip(args, args[1:], strict=False))
    assert pairs["-c:v"] == "h264_vaapi"
    assert pairs["-preset"] == "fast"
    assert pairs["-crf"] == "23"
    assert pairs["-hls_time"] == "4"
    assert pairs["-c:a"] == "aac"
    assert pairs["-b:a"] == "160k"
    assert pairs["-ac"] == "2"
    assert pairs["-pix_fmt"] == "yuv420p"


def test_the_defaults_are_the_quality_settings_the_owner_asked_for() -> None:
    """M15: ``veryfast``/CRF 20 looked soft, so the defaults moved."""
    args = encode_args(plan_for(*MULTISUB))
    pairs = dict(zip(args, args[1:], strict=False))
    assert pairs["-preset"] == "fast"
    assert pairs["-crf"] == "19"
    assert pairs["-tune"] == "animation"
    # Unchanged, and asserted here so a profile the browser cannot decode
    # cannot slip in behind a quality change.
    assert pairs["-profile:v"] == "high"
    assert pairs["-level"] == "4.1"
    assert pairs["-pix_fmt"] == "yuv420p"


def test_no_tune_is_passed_when_it_is_turned_off() -> None:
    """An empty ``-tune`` is an unknown tune, and ffmpeg exits on it."""
    assert "-tune" not in encode_args(plan_for(*MULTISUB), EncodeOptions(tune=None))
    assert "-tune" not in encode_args(plan_for(*MULTISUB), EncodeOptions(tune=""))


def test_nothing_in_the_encode_scales_the_picture() -> None:
    """One rendition at the source's size, so swscale is never asked to resize.

    Which is why there is no ``-sws_flags``: it would describe a resize that
    does not happen. The burn-in filter is the whole graph.
    """
    args = encode_args(plan_for(*MULTISUB), EncodeOptions(subtitle_file=ASS_FILE))
    assert "-sws_flags" not in args
    assert "-s" not in args
    assert args[args.index("-vf") + 1] == f"ass={ASS_FILE}:fontsdir={FONTS_DIR}"


def test_a_bitrate_ceiling_is_passed_only_when_it_is_set() -> None:
    plain = encode_args(plan_for(*MULTISUB))
    assert "-maxrate" not in plain
    assert "-bufsize" not in plain

    # A bufsize on its own means nothing to x264 without a ceiling.
    assert "-bufsize" not in encode_args(plan_for(*MULTISUB), EncodeOptions(bufsize_kbps=9000))

    capped = encode_args(plan_for(*MULTISUB), EncodeOptions(maxrate_kbps=6000))
    pairs = dict(zip(capped, capped[1:], strict=False))
    assert pairs["-maxrate"] == "6000k"
    # Twice the ceiling when the buffer is not stated.
    assert pairs["-bufsize"] == "12000k"

    both = encode_args(plan_for(*MULTISUB), EncodeOptions(maxrate_kbps=6000, bufsize_kbps=9000))
    pairs = dict(zip(both, both[1:], strict=False))
    assert pairs["-maxrate"] == "6000k"
    assert pairs["-bufsize"] == "9000k"


def test_the_subtitle_extraction_copies_ass_and_converts_the_rest() -> None:
    ass = plan_for(*MULTISUB)
    args = subtitle_extract_args(ass)
    assert args is not None
    assert args[args.index("-map") + 1] == "0:s:0"
    assert args[args.index("-c:s") + 1] == "copy"
    assert args[-1] == ASS_FILE

    srt = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
        stream(2, "subtitle", codec_name="mov_text", language="eng"),
    )
    args = subtitle_extract_args(srt)
    assert args is not None
    assert args[args.index("-c:s") + 1] == "srt"
    assert args[-1] == SRT_FILE


def test_nothing_to_extract_when_there_is_no_subtitle() -> None:
    plan = plan_for(
        stream(0, "video", codec_name="h264"),
        stream(1, "audio", codec_name="aac", language="jpn"),
    )
    assert subtitle_extract_args(plan) is None


def test_the_plan_logs_as_one_flat_dict() -> None:
    fields = plan_for(*MULTISUB).as_dict()
    assert fields["subtitle_stream"] == 2
    assert fields["subtitle_lang"] == "en"
    assert fields["audio_lang"] == "ja"
    assert fields["attachments"] == 2
    assert all(not isinstance(value, Path) for value in fields.values())
