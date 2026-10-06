"""The small offline copy with the real ffmpeg, on the five-second fixture (slow).

The counterpart of :mod:`tests.test_transcode_slow` for FR-P6: that the
arguments produce one faststart MP4 that passes Arc's own validation, that a
180-line source is *not* scaled up to 720, and that the English subtitle is in
the picture (the same bottom-band brightness measurement).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from arc.services.media.offline import encode_copy, moov_before_mdat
from arc.services.media.plan import OfflineOptions, build_plan
from arc.services.media.probe import ffprobe_json
from arc.services.media.transcode import available_filters, reset_filters
from tests.test_transcode_slow import (
    FIXTURE_SECONDS,
    bottom_band_brightness,
    make_fixture,
    settings_for,
)

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("codec", ["h264", "hevc"])
async def test_a_real_file_becomes_a_small_faststart_copy_with_subtitles(
    tmp_path: Path, codec: str
) -> None:
    reset_filters()
    settings = settings_for(tmp_path)
    ffmpeg = settings.ffmpeg_bin
    if shutil.which(ffmpeg) is None:
        pytest.skip(f"{ffmpeg} is not installed")
    if "ass" not in await available_filters(ffmpeg):
        pytest.skip(f"{ffmpeg} was built without libass")

    source, expected_fonts = await make_fixture(ffmpeg, tmp_path / "downloads")
    plan = build_plan(
        await ffprobe_json(source, binary=settings.ffprobe_bin),
        source=source,
        output_dir=tmp_path / "offline" / "1.tmp-1",
        sub_lang="en",
        audio_lang="ja",
    )

    result = await encode_copy(
        plan,
        options=OfflineOptions(codec=codec, preset="ultrafast"),
        ffmpeg=ffmpeg,
        ffprobe=settings.ffprobe_bin,
        timeout=120,
    )

    assert result.path.is_file()
    assert moov_before_mdat(result.path)
    assert result.fonts == expected_fonts
    assert result.subtitle_lang == "en" and result.audio_lang == "ja"
    assert not (plan.output_dir / "_work").exists()

    probed = await ffprobe_json(result.path, binary=settings.ffprobe_bin)
    assert probed is not None
    video = next(s for s in probed["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == codec
    if codec == "hevc":
        assert video["codec_tag_string"] == "hvc1"
    # Never upscaled: the fixture is 320x180 and the copy stays 180 lines.
    assert (video["width"], video["height"]) == (320, 180)
    assert not any(s["codec_type"] == "subtitle" for s in probed["streams"])
    assert float(probed["format"]["duration"]) == pytest.approx(FIXTURE_SECONDS, abs=1.0)

    before = await bottom_band_brightness(ffmpeg, source, 2.5, tmp_path)
    after = await bottom_band_brightness(ffmpeg, result.path, 2.5, tmp_path)
    assert after > before + 3.0, "the subtitle does not look burned in"
