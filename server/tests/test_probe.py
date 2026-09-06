"""``ffprobe`` when it exists and when it does not (architecture.md §5.2).

The machine the suite runs on may or may not have ffmpeg installed — Arc's
Docker image does, a laptop often does not — so nothing here depends on the
real binary. A fake ``ffprobe`` shell script is written into a temporary
directory and put on ``PATH``, which exercises the actual subprocess call, the
actual JSON decode and the actual failure paths.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from arc.services.media import probe as probe_module
from arc.services.media.probe import ffprobe_json, ffprobe_path, probe_summary, summarise

#: What a real ffprobe says about a small MKV, trimmed to the keys Arc reads.
SAMPLE = {
    "format": {"format_name": "matroska,webm", "duration": "1421.984000"},
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "bit_rate": "4500000",
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "tags": {"language": "jpn", "title": "Japanese"},
        },
        {
            "index": 2,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "eng", "title": "Full Subtitles"},
        },
    ],
}


def install_fake_ffprobe(
    directory: Path, monkeypatch: pytest.MonkeyPatch, *, body: str, exit_code: int = 0
) -> Path:
    """Write an executable ``ffprobe`` into ``directory`` and put it first on PATH."""
    script = directory / "ffprobe"
    script.write_text(f"#!/bin/sh\n{body}\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(directory), prepend=os.pathsep)
    return script


@pytest.fixture(autouse=True)
def fresh_warning() -> None:
    """The "no ffprobe" line is written once per process; reset it per test."""
    probe_module.reset_warning()


class TestWithAnFfprobe:
    async def test_json_is_returned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps(SAMPLE).replace("'", "")
        install_fake_ffprobe(tmp_path, monkeypatch, body=f"cat <<'EOF'\n{payload}\nEOF")
        assert await ffprobe_json(tmp_path / "episode.mkv") == SAMPLE

    async def test_a_nonzero_exit_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_ffprobe(tmp_path, monkeypatch, body="echo broken >&2", exit_code=1)
        assert await ffprobe_json(tmp_path / "episode.mkv") is None

    async def test_output_that_is_not_json_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_ffprobe(tmp_path, monkeypatch, body="echo 'not json at all'")
        assert await ffprobe_json(tmp_path / "episode.mkv") is None

    async def test_the_summary_is_what_ingest_stores(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_ffprobe(tmp_path, monkeypatch, body=f"cat <<'EOF'\n{json.dumps(SAMPLE)}\nEOF")
        summary = await probe_summary(tmp_path / "episode.mkv")
        assert summary is not None
        assert summary["duration"] == pytest.approx(1421.984)
        assert summary["format"] == "matroska,webm"
        assert [stream["type"] for stream in summary["streams"]] == [
            "video",
            "audio",
            "subtitle",
        ]

    async def test_the_arguments_are_the_documented_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fake echoes its argv, so the call itself is under test."""
        install_fake_ffprobe(tmp_path, monkeypatch, body='printf \'{"argv": "%s"}\' "$*"')
        payload = await ffprobe_json(tmp_path / "episode.mkv")
        assert payload is not None
        argv = payload["argv"]
        assert "-print_format json" in argv
        assert "-show_streams" in argv
        assert "-show_format" in argv
        assert argv.endswith("episode.mkv")


class TestWithoutAnFfprobe:
    @pytest.fixture(autouse=True)
    def no_ffprobe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty PATH: nothing at all is installed."""
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    def test_it_is_not_found(self) -> None:
        assert ffprobe_path() is None

    async def test_probing_is_none_not_an_error(self, tmp_path: Path) -> None:
        assert await ffprobe_json(tmp_path / "episode.mkv") is None
        assert await probe_summary(tmp_path / "episode.mkv") is None

    async def test_the_warning_is_written_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A scan of a hundred files must not be a hundred identical lines."""
        with caplog.at_level("INFO", logger="arc.services.media.probe"):
            for index in range(5):
                await ffprobe_json(tmp_path / f"episode-{index}.mkv")
        lines = [
            record for record in caplog.records if "ffprobe is not installed" in record.message
        ]
        assert len(lines) == 1


class TestSummarise:
    def test_nothing_in_nothing_out(self) -> None:
        assert summarise(None) is None
        assert summarise({}) is None

    def test_a_duration_that_is_not_a_number_is_dropped(self) -> None:
        summary = summarise({"format": {"duration": "N/A"}, "streams": []})
        assert summary is not None
        assert summary["duration"] is None

    def test_bitrates_and_dispositions_are_not_stored(self) -> None:
        summary = summarise(SAMPLE)
        assert summary is not None
        assert "bit_rate" not in summary["streams"][0]

    def test_only_video_streams_carry_dimensions(self) -> None:
        summary = summarise(SAMPLE)
        assert summary is not None
        video, audio, _ = summary["streams"]
        assert (video["width"], video["height"]) == (1920, 1080)
        assert "width" not in audio

    def test_tags_become_language_and_title(self) -> None:
        summary = summarise(SAMPLE)
        assert summary is not None
        assert summary["streams"][2]["language"] == "eng"
        assert summary["streams"][2]["title"] == "Full Subtitles"

    def test_a_stream_with_no_tags_is_fine(self) -> None:
        summary = summarise({"format": {}, "streams": [{"index": 0, "codec_type": "video"}]})
        assert summary is not None
        assert summary["streams"][0]["language"] is None

    def test_the_summary_is_json_safe(self) -> None:
        summary = summarise(SAMPLE)
        assert json.loads(json.dumps(summary)) == summary
