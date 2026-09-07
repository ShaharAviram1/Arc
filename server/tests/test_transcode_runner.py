"""Running ffmpeg: progress, the stderr tail, the concurrency cap.

Against a fake ``ffmpeg`` on ``PATH`` (:mod:`tests.media_helpers`) rather than
a mocked ``create_subprocess_exec``, so the argument vector, the working
directory, both pipes and the exit code are all exercised for real. The one
test that needs the actual binary is in ``test_transcode_slow``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arc.services.media.plan import (
    ASS_FILE,
    FONTS_DIR,
    EncodeOptions,
    build_plan,
)
from arc.services.media.transcode import (
    MAX_TAIL_CHARS,
    STDERR_TAIL_LINES,
    ProgressReader,
    TranscodeError,
    available_filters,
    parse_progress_line,
    progress_seconds,
    reset_filters,
    reset_semaphore,
    run_ffmpeg,
    transcode,
    transcode_semaphore,
    validate_output,
)
from tests.media_helpers import SOURCE_PROBE, install_fake_ffmpeg, write_script


@pytest.fixture(autouse=True)
def _fresh_process_state() -> None:
    """Both process-wide caches: the ffmpeg cap and "what can this build do?"."""
    reset_semaphore()
    reset_filters()


def make_plan(tmp_path: Path, *, name: str = "[Group] Show - 01 [1080p].mkv"):
    source = tmp_path / "downloads" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"not really a video")
    return build_plan(SOURCE_PROBE, source=source, output_dir=tmp_path / "renditions" / "1")


# --- Progress parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("out_time_us=1500000\n", ("out_time_us", "1500000")),
        ("  progress=continue  ", ("progress", "continue")),
        ("bitrate= 1200.4kbits/s", ("bitrate", "1200.4kbits/s")),
        # A value containing '=' keeps everything after the first one.
        ("stream_0_0_q=-1.0=x", ("stream_0_0_q", "-1.0=x")),
        ("", None),
        ("   ", None),
        ("no equals sign here", None),
        ("=orphan", None),
    ],
)
def test_progress_lines_parse_to_pairs(line: str, expected: tuple[str, str] | None) -> None:
    assert parse_progress_line(line) == expected


def test_out_time_us_is_preferred_and_na_is_ignored() -> None:
    assert progress_seconds({"out_time_us": "2500000"}) == pytest.approx(2.5)
    assert progress_seconds({"out_time_us": "N/A", "out_time": "00:00:03.500000"}) == pytest.approx(
        3.5
    )
    assert progress_seconds({"out_time_ms": "4000000"}) == pytest.approx(4.0)
    assert progress_seconds({"out_time": "N/A"}) is None
    assert progress_seconds({}) is None
    assert progress_seconds({"out_time_us": "banana", "out_time": "01:02:03.0"}) == pytest.approx(
        3723.0
    )


def test_the_reader_answers_once_per_block_not_once_per_field() -> None:
    reader = ProgressReader(100.0)
    assert reader.feed("frame=24") is None
    assert reader.feed("out_time_us=25000000") is None
    assert reader.feed("progress=continue") == pytest.approx(0.25)
    assert reader.finished is False
    assert reader.feed("out_time_us=100000000") is None
    assert reader.feed("progress=end") == pytest.approx(1.0)
    assert reader.finished is True


def test_progress_never_goes_backwards_or_past_one() -> None:
    reader = ProgressReader(10.0)
    reader.feed("out_time_us=9000000")
    assert reader.feed("progress=continue") == pytest.approx(0.9)
    # A later block reporting an earlier time (ffmpeg does this at the very
    # end of some muxers) must not make the bar jump back.
    reader.feed("out_time_us=1000000")
    assert reader.feed("progress=continue") == pytest.approx(0.9)
    reader.feed("out_time_us=99000000")
    assert reader.feed("progress=end") == pytest.approx(1.0)


def test_a_source_with_no_duration_still_answers_so_the_heartbeat_beats() -> None:
    reader = ProgressReader(None)
    reader.feed("out_time_us=5000000")
    assert reader.feed("progress=continue") == 0.0
    assert reader.seconds == pytest.approx(5.0)


# --- Running the binary -----------------------------------------------------


async def test_a_missing_binary_is_a_transcode_error(tmp_path: Path) -> None:
    with pytest.raises(TranscodeError, match="not installed"):
        await run_ffmpeg(["-version"], binary="definitely-not-ffmpeg", cwd=tmp_path, timeout=5)


async def test_a_non_zero_exit_carries_the_tail_of_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_script(
        tmp_path / "bin",
        "ffmpeg",
        """
        import sys
        for line in range(200):
            print("noise line %d" % line, file=sys.stderr)
        print("Error opening output file.", file=sys.stderr)
        sys.exit(3)
        """,
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"), prepend=":")
    with pytest.raises(TranscodeError) as caught:
        await run_ffmpeg([], binary="ffmpeg", cwd=tmp_path, timeout=30)
    assert "exited 3" in str(caught.value)
    lines = caught.value.error_tail.splitlines()
    assert len(lines) <= STDERR_TAIL_LINES
    # The *end* is what is kept: the complaint, not the noise before it.
    assert lines[-1] == "Error opening output file."
    assert "noise line 0" not in caught.value.error_tail
    assert len(caught.value.error_tail) <= MAX_TAIL_CHARS


async def test_the_tail_is_capped_in_characters_as_well_as_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_script(
        tmp_path / "bin",
        "ffmpeg",
        """
        import sys
        for line in range(40):
            print("x" * 500, file=sys.stderr)
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"), prepend=":")
    with pytest.raises(TranscodeError) as caught:
        await run_ffmpeg([], binary="ffmpeg", cwd=tmp_path, timeout=30)
    assert len(caught.value.error_tail) == MAX_TAIL_CHARS


async def test_a_run_that_overruns_is_killed_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_script(
        tmp_path / "bin",
        "ffmpeg",
        """
        import sys, time
        print("about to hang", file=sys.stderr)
        sys.stderr.flush()
        time.sleep(30)
        """,
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"), prepend=":")
    with pytest.raises(TranscodeError, match="timed out"):
        await run_ffmpeg([], binary="ffmpeg", cwd=tmp_path, timeout=0.5)


async def test_progress_reaches_the_callback_while_ffmpeg_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=5, duration=10.0)
    seen: list[tuple[str, float]] = []

    async def record(stage: str, fraction: float) -> None:
        seen.append((stage, fraction))

    output = tmp_path / "out"
    output.mkdir()
    await run_ffmpeg(
        ["-f", "hls", "index.m3u8"],
        binary="ffmpeg",
        cwd=output,
        timeout=30,
        duration=10.0,
        on_progress=record,
        stage="encode",
    )
    assert [round(fraction, 2) for _, fraction in seen] == [0.2, 0.4, 0.6, 0.8, 1.0, 1.0]
    assert {stage for stage, _ in seen} == {"encode"}


# --- The concurrency cap ----------------------------------------------------


async def test_the_semaphore_is_one_per_process_and_sized_once() -> None:
    first = transcode_semaphore(2)
    second = transcode_semaphore(9)
    assert first is second


async def test_at_most_two_encodes_run_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MAX_TRANSCODES`` caps ffmpeg, not the queue (FR-P1, architecture §8)."""
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=4, sleep=0.05)
    live = 0
    peak = 0
    semaphore = transcode_semaphore(2)

    async def one(index: int) -> None:
        nonlocal live, peak
        plan = make_plan(tmp_path, name=f"show {index}.mkv")
        object.__setattr__(plan, "output_dir", tmp_path / "renditions" / str(index))
        async with semaphore:
            live += 1
            peak = max(peak, live)
            try:
                await transcode(plan, options=EncodeOptions(), timeout=60)
            finally:
                live -= 1

    await asyncio.gather(*(one(index) for index in range(5)))
    assert peak == 2


# --- The whole thing, with the fake binary ----------------------------------


async def test_a_transcode_dumps_fonts_extracts_the_track_then_encodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=3, marker=marker)
    plan = make_plan(tmp_path)

    stages: list[str] = []

    async def record(stage: str, fraction: float) -> None:
        stages.append(stage)

    result = await transcode(plan, options=EncodeOptions(), timeout=60, on_progress=record)

    assert marker.read_text().split() == ["fonts", "subtitle", "encode"]
    assert result.segments == 3
    assert result.fonts == 2
    assert result.subtitle_lang == "en"
    assert result.audio_lang == "ja"
    assert (result.width, result.height) == (1920, 1080)
    assert result.duration == pytest.approx(12.0)
    assert result.playlist.name == "index.m3u8"
    assert stages[0] == "fonts"
    assert "encode" in stages and stages[-1] == "package"
    # The work directory is cleaned up; the rendition is only the output.
    assert not (plan.output_dir / "_work").exists()
    assert sorted(path.name for path in plan.output_dir.iterdir()) == [
        "index.m3u8",
        "init.mp4",
        "seg_00000.m4s",
        "seg_00001.m4s",
        "seg_00002.m4s",
    ]


async def test_the_encode_is_told_where_the_subtitle_and_fonts_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = tmp_path / "argv.txt"
    write_script(
        tmp_path / "bin",
        "ffmpeg",
        f"""
        import os, sys
        args = sys.argv[1:]
        if "-filters" in args:
            print(" .. ass V->V Render ASS subtitles onto input video.")
            sys.exit(0)
        with open({str(argv)!r}, "a") as handle:
            handle.write("\\x00".join(args) + "\\n")
        if any(a.startswith("-dump_attachment") for a in args):
            for index, arg in enumerate(args):
                if arg.startswith("-dump_attachment"):
                    target = args[index + 1]
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    open(target, "wb").write(b"font")
            sys.exit(0)
        if "-c:s" in args:
            target = args[-1]
            os.makedirs(os.path.dirname(target), exist_ok=True)
            open(target, "w").write("[Script Info]")
            sys.exit(0)
        open("init.mp4", "wb").write(b"i")
        open("seg_00000.m4s", "wb").write(b"s")
        open(args[-1], "w").write("#EXTM3U")
        sys.exit(0)
        """,
    )
    write_script(
        tmp_path / "bin",
        "ffprobe",
        """
        import json, sys
        print(json.dumps({"format": {"duration": "12.0"},
                          "streams": [{"index": 0, "codec_type": "video",
                                       "codec_name": "h264"}]}))
        """,
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"), prepend=":")

    plan = make_plan(tmp_path)
    await transcode(plan, options=EncodeOptions(), timeout=60)

    calls = [line.split("\x00") for line in argv.read_text().splitlines()]
    encode = calls[-1]
    assert encode[encode.index("-vf") + 1] == f"ass={ASS_FILE}:fontsdir={FONTS_DIR}"
    # The source path is passed once, whole, and never inside the filter.
    assert encode[encode.index("-i") + 1] == str(plan.source)


async def test_a_failed_encode_raises_with_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, fail=True)
    plan = make_plan(tmp_path)
    with pytest.raises(TranscodeError) as caught:
        await transcode(plan, options=EncodeOptions(), timeout=60)
    assert "Error opening output file" in caught.value.error_tail


async def test_an_encode_that_writes_nothing_playable_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, broken=True)
    plan = make_plan(tmp_path)
    with pytest.raises(TranscodeError, match="not a playable playlist"):
        await transcode(plan, options=EncodeOptions(), timeout=60)


async def test_a_stale_rendition_is_cleared_before_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=2)
    plan = make_plan(tmp_path)
    plan.output_dir.mkdir(parents=True)
    for index in range(9):
        (plan.output_dir / f"seg_{index:05d}.m4s").write_bytes(b"old")

    result = await transcode(plan, options=EncodeOptions(), timeout=60)
    assert result.segments == 2
    assert sorted(path.name for path in plan.output_dir.glob("seg_*.m4s")) == [
        "seg_00000.m4s",
        "seg_00001.m4s",
    ]


async def test_a_source_with_no_text_subtitle_is_encoded_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = {
        "format": {"duration": "12.0"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "jpn"}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        ],
    }
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, segments=1, source_probe=probe)
    plan = build_plan(
        probe,
        source=tmp_path / "in.mkv",
        output_dir=tmp_path / "renditions" / "9",
    )
    result = await transcode(plan, options=EncodeOptions(), timeout=60)
    assert result.subtitle_lang is None
    assert result.fonts == 0
    assert any("bitmap" in note for note in result.notes)


# --- Output validation ------------------------------------------------------


async def test_validation_rejects_every_kind_of_half_written_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch)
    output = tmp_path / "out"
    output.mkdir()
    assert await validate_output(output) == 0

    (output / "index.m3u8").write_text("#EXTM3U")
    assert await validate_output(output) == 0

    (output / "init.mp4").write_bytes(b"init")
    assert await validate_output(output) == 0

    (output / "seg_00000.m4s").write_bytes(b"seg")
    assert await validate_output(output) == 1

    (output / "index.m3u8").write_text("")
    assert await validate_output(output) == 0


async def test_an_ffmpeg_without_libass_says_so_before_the_encode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Homebrew build. Twenty minutes of x264 must not be spent finding out."""
    marker = tmp_path / "calls.txt"
    install_fake_ffmpeg(tmp_path / "bin", monkeypatch, marker=marker, filters=())
    plan = make_plan(tmp_path)
    with pytest.raises(TranscodeError, match="without libass"):
        await transcode(plan, options=EncodeOptions(), timeout=60)
    assert "encode" not in marker.read_text()


async def test_the_filter_list_is_asked_for_once_per_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = tmp_path / "asked.txt"
    write_script(
        tmp_path / "bin",
        "ffmpeg",
        f"""
        import sys
        if "-filters" in sys.argv:
            with open({str(counter)!r}, "a") as handle:
                handle.write("x")
            print(" .. ass V->V Render ASS subtitles onto input video.")
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"), prepend=":")
    assert "ass" in await available_filters("ffmpeg")
    assert "ass" in await available_filters("ffmpeg")
    assert counter.read_text() == "x"
