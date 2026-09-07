"""The real ffmpeg, on a five-second fixture built at test time (M7, slow).

Everything else in the M7 suite runs against a fake binary, which proves that
Arc *says* the right thing to ffmpeg. This proves ffmpeg *does* it: that the
arguments in :mod:`arc.services.media.plan` produce an HLS rendition a browser
can play, and — the assertion the milestone turns on — that the subtitles are
actually in the picture.

The burn-in check is a measurement, not an eyeball. The fixture is a black
320×180 video with one white ASS line near the bottom, so the mean brightness
of the bottom fifth of a frame is ~0 in the source and clearly not zero in the
rendition. Comparing the two frames at the same timestamp is the whole test:
if the filter were dropped, misspelled, or pointed at the wrong track, the two
numbers would match.

Marked ``slow`` (architecture.md §10) and kept under about fifteen seconds:
the fixture is five seconds of a single flat colour, which x264 encodes almost
instantly.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from arc.config import Settings
from arc.services.media.plan import EncodeOptions, build_plan
from arc.services.media.probe import ffprobe_json
from arc.services.media.transcode import available_filters, reset_filters, transcode

pytestmark = pytest.mark.slow

#: The line burned into the English track, and the one in the Japanese track
#: that must *not* be burned in.
ENGLISH_LINE = "ARC TEST SUBTITLE"
JAPANESE_LINE = "NIHONGO NO JIMAKU"

FIXTURE_SECONDS = 5
FIXTURE_SIZE = "320x180"

#: Fonts to attach if the host has one. Purely so the attachment path is
#: exercised on a real container; the test asserts the count either way.
CANDIDATE_FONTS = (
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
)

ASS_TEMPLATE = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, \
BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, \
BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,24,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,\
100,100,0,0,1,1,0,2,10,10,8,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{line}
"""


def settings_for(tmp_path: Path) -> Settings:
    """Test settings that still honour ``FFMPEG_BIN`` from the environment.

    Deliberately *not* ``_env_file=None`` like the rest of the suite: which
    ffmpeg to use is exactly the thing a developer overrides locally (a
    Homebrew ``ffmpeg`` is built without libass; ``ffmpeg-full`` is not), and a
    test that ignored the override would test a binary nobody deploys.
    """
    return Settings(data_dir=tmp_path, env="test")  # type: ignore[call-arg]


async def run(binary: str, *args: str, cwd: Path | None = None) -> None:
    """Run ffmpeg and raise with its stderr if it fails."""
    process = await asyncio.create_subprocess_exec(
        binary,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-y",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise AssertionError(f"ffmpeg failed: {stderr.decode('utf-8', 'replace')[-2000:]}")


async def make_fixture(binary: str, directory: Path) -> tuple[Path, int]:
    """A five-second MKV with two ASS tracks and (if possible) a font.

    Named like a release, brackets and all, so the run also proves that no
    part of the source name has to be escaped anywhere.
    """
    directory.mkdir(parents=True, exist_ok=True)
    english = directory / "eng.ass"
    japanese = directory / "jpn.ass"
    english.write_text(ASS_TEMPLATE.format(line=ENGLISH_LINE))
    japanese.write_text(ASS_TEMPLATE.format(line=JAPANESE_LINE))

    font = next((path for path in CANDIDATE_FONTS if path.exists()), None)
    source = directory / "[Arc-tests] Fixture: it's here - 01 [1080p][ABC123].mkv"
    attach = ["-attach", str(font), "-metadata:s:t:0", "mimetype=font/ttf"] if font else []
    await run(
        binary,
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={FIXTURE_SIZE}:r=24:d={FIXTURE_SECONDS}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={FIXTURE_SECONDS}",
        "-i",
        str(english),
        "-i",
        str(japanese),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2",
        "-map",
        "3",
        "-metadata:s:a:0",
        "language=jpn",
        "-metadata:s:s:0",
        "language=eng",
        "-metadata:s:s:0",
        "title=English",
        "-metadata:s:s:1",
        "language=jpn",
        "-metadata:s:s:1",
        "title=Japanese",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-c:s",
        "copy",
        *attach,
        str(source),
    )
    return source, 1 if font else 0


async def bottom_band_brightness(binary: str, target: Path, at: float, work: Path) -> float:
    """Mean luma of the bottom fifth of the frame at ``at`` seconds, 0..255.

    Raw greyscale bytes rather than a PNG and an image library: the number is
    the assertion, and adding Pillow to the server's dependencies to compute a
    mean would be a dependency for one test.
    """
    raw = work / f"band-{target.name}-{at}.gray"
    await run(
        binary,
        "-ss",
        str(at),
        "-i",
        str(target),
        "-frames:v",
        "1",
        "-vf",
        "crop=iw:ih/5:0:ih*4/5",
        "-pix_fmt",
        "gray",
        "-f",
        "rawvideo",
        str(raw),
    )
    pixels = raw.read_bytes()
    assert pixels, f"no frame at {at}s of {target}"
    return sum(pixels) / len(pixels)


async def test_a_real_five_second_file_becomes_a_playable_rendition_with_subtitles(
    tmp_path: Path,
) -> None:
    reset_filters()
    settings = settings_for(tmp_path)
    ffmpeg = settings.ffmpeg_bin
    if shutil.which(ffmpeg) is None:
        pytest.skip(f"{ffmpeg} is not installed")
    if "ass" not in await available_filters(ffmpeg):
        pytest.skip(
            f"{ffmpeg} was built without libass, so subtitles cannot be burned in. "
            "Install a full build (Debian's ffmpeg, or 'brew install ffmpeg-full') "
            "and set FFMPEG_BIN."
        )

    source, expected_fonts = await make_fixture(ffmpeg, tmp_path / "downloads")
    plan = build_plan(
        await ffprobe_json(source, binary=settings.ffprobe_bin),
        source=source,
        output_dir=tmp_path / "renditions" / "1",
        sub_lang="en",
        audio_lang="ja",
    )

    # The plan first: the English ASS track, the Japanese audio, the fonts.
    assert plan.subtitle is not None and plan.subtitle.type_index == 0
    assert plan.subtitle_lang == "en"
    assert plan.audio_lang == "ja"
    assert (plan.width, plan.height) == (320, 180)
    assert plan.duration == pytest.approx(FIXTURE_SECONDS, abs=0.5)
    assert len(plan.attachments) == expected_fonts

    seen: list[tuple[str, float]] = []

    async def record(stage: str, fraction: float) -> None:
        seen.append((stage, fraction))

    result = await transcode(
        plan,
        options=EncodeOptions(segment_seconds=2),
        ffmpeg=settings.ffmpeg_bin,
        ffprobe=settings.ffprobe_bin,
        timeout=120,
        on_progress=record,
    )

    # --- the output is what M8 will serve ---------------------------------
    assert result.playlist.exists()
    assert (plan.output_dir / "init.mp4").exists()
    assert result.segments >= 2
    assert result.fonts == expected_fonts
    assert result.subtitle_lang == "en"
    assert result.audio_lang == "ja"
    assert not (plan.output_dir / "_work").exists()
    playlist = result.playlist.read_text()
    assert '#EXT-X-MAP:URI="init.mp4"' in playlist
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in playlist
    assert "#EXT-X-INDEPENDENT-SEGMENTS" in playlist
    assert playlist.count("seg_") >= result.segments
    assert [stage for stage, _ in seen][-1] == "package"
    assert max(fraction for _, fraction in seen) == pytest.approx(1.0)

    # --- and it is H.264 + AAC of the right length ------------------------
    probed = await ffprobe_json(result.playlist, binary=settings.ffprobe_bin)
    assert probed is not None
    codecs = {stream["codec_type"]: stream["codec_name"] for stream in probed["streams"]}
    assert codecs == {"video": "h264", "audio": "aac"}
    assert float(probed["format"]["duration"]) == pytest.approx(FIXTURE_SECONDS, abs=1.0)
    assert result.duration == pytest.approx(FIXTURE_SECONDS, abs=1.0)

    # --- and the subtitles are in the picture, not in a track -------------
    assert not any(stream["codec_type"] == "subtitle" for stream in probed["streams"])
    before = await bottom_band_brightness(settings.ffmpeg_bin, source, 2.5, tmp_path)
    after = await bottom_band_brightness(settings.ffmpeg_bin, result.playlist, 2.5, tmp_path)
    assert before < 1.0, "the fixture's own bottom band should be black"
    assert after > before + 3.0, (
        f"the bottom band is as dark as the source ({after:.2f} vs {before:.2f}): "
        "the subtitle does not look burned in"
    )
