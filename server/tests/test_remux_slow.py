"""The real ffmpeg remuxing a real SubsPlease episode, and the router serving it (M20, slow).

Everything else in the remux suite runs against a fake binary. This takes the
first two minutes of a real 1080p SubsPlease MKV from the developer's library
(read only: the source is opened by ffprobe and ffmpeg and nothing else), runs
the ``remux`` handler over it with the real ffmpeg and ffprobe, and asks the
media router for the result: the master playlist, the subtitle playlist and
the WebVTT segment a known cue should land in.

Two minutes rather than the whole episode only to keep the suite quick; the
cut is an input option (``-t 120`` before ``-i``), so it applies to the video,
the audio and the subtitle output alike. The whole test has to finish in
under a minute of wall time (the M20 plan's acceptance).

Skipped when there is no such file — a fresh checkout or CI has no library.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    RENDITION_REMUX,
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    Rendition,
    ReviewState,
    User,
)
from arc.services.media import remux as remux_module
from arc.services.media.names import REMUX, RemuxInfo, output_dir_for
from arc.services.media.subtitles import segment_bounds, vtt_to_cues
from tests.conftest import add_user, api_transport, login
from tests.test_transcode_jobs import context

pytestmark = [pytest.mark.slow, pytest.mark.pg]

#: Where the developer's library is, relative to this checkout or to the main
#: one when the suite runs from a git worktree under ``.claude/worktrees``.
_HERE = Path(__file__).resolve()
CANDIDATE_ROOTS = [parent / "data" / "downloads" for parent in _HERE.parents]
SUBSPLEASE = re.compile(r"\[SubsPlease\] .* \(1080p\) \[[0-9A-F]{8}\]\.mkv\Z")

EXCERPT_SECONDS = 120
WALL_LIMIT_SECONDS = 60.0

USER_EMAIL = "remux-slow@arc.test"
USER_PASSWORD = "viewer-password"


def _source() -> Path | None:
    """The first SubsPlease 1080p episode in the library with an English text track."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return None
    for root in CANDIDATE_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/*.mkv")):
            if SUBSPLEASE.search(path.name):
                return path
    return None


SOURCE = _source()


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    """``DATA_DIR`` in this test's own tree, the real binaries from ``PATH``."""
    return Settings(  # type: ignore[call-arg]
        env="test",
        database_url=test_database_url,
        data_dir=tmp_path,
        _env_file=None,
    )


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


def _excerpt(original: Any) -> Any:
    """``remux_args`` with ``-t 120`` as an input option."""

    def wrapped(*args: Any, **kwargs: Any) -> list[str]:
        argv: list[str] = original(*args, **kwargs)
        at = argv.index("-i")
        return [*argv[:at], "-t", str(EXCERPT_SECONDS), *argv[at:]]

    return wrapped


async def _ffprobe(path: Path) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    payload: dict[str, Any] = json.loads(stdout)
    return payload


async def _decodes(path: Path, seconds: int) -> None:
    """ffmpeg decodes the first ``seconds`` of the rendition without an error."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-t",
        str(seconds),
        "-i",
        str(path),
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert stderr.decode().strip() == "", stderr.decode()


@pytest.mark.skipif(SOURCE is None, reason="no SubsPlease 1080p MKV in data/downloads")
async def test_a_real_episode_is_remuxed_and_its_cue_lands_in_the_right_segment(
    api_factory: SessionFactory,
    settings: Settings,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert SOURCE is not None
    started = perf_counter()
    monkeypatch.setattr(remux_module, "remux_args", _excerpt(remux_module.remux_args))

    async with api_factory() as session:
        anime = Anime(
            anilist_id=977001,
            summary_source="anilist",
            detail_source="anilist",
            title_romaji="Slow Remux Show",
            format="TV",
            status="FINISHED",
            episodes=12,
        )
        session.add(anime)
        await session.flush()
        episode = Episode(anime_id=anime.id, number=10, state=EpisodeState.MATCHED)
        session.add(episode)
        await session.flush()
        session.add(
            MediaFile(
                episode_id=episode.id,
                path=str(SOURCE),
                size=SOURCE.stat().st_size,
                review_state=ReviewState.AUTO,
            )
        )
        job = Job(
            type=REMUX,
            payload={"episode_id": episode.id},
            status=JobStatus.RUNNING,
            attempts=1,
            locked_by="test:1",
            locked_at=datetime.now(UTC),
        )
        session.add(job)
        await session.commit()

        await remux_module.remux_episode(context(session, settings, job))

        await session.refresh(episode)
        assert episode.state is EpisodeState.READY
        rendition = await session.scalar(
            select(Rendition).where(Rendition.episode_id == episode.id)
        )
        assert rendition is not None
        assert rendition.kind == RENDITION_REMUX
        assert rendition.text_subtitles is True
        assert rendition.subtitle_lang == "en"
        episode_id = episode.id

    directory = output_dir_for(settings, episode_id)
    playlist = directory / "video.m3u8"

    # The rendition plays: H.264 copied, AAC, two minutes long, decodable.
    probe = await _ffprobe(playlist)
    codecs = {stream["codec_type"]: stream["codec_name"] for stream in probe["streams"]}
    assert codecs == {"video": "h264", "audio": "aac"}
    assert float(probe["format"]["duration"]) == pytest.approx(EXCERPT_SECONDS, abs=7)
    await _decodes(playlist, 8)

    # The master names the video and a default English subtitle rendition.
    master = await client.get(f"/media/{episode_id}/index.m3u8")
    assert master.status_code == 200
    assert "#EXT-X-MEDIA:TYPE=SUBTITLES" in master.text
    assert "DEFAULT=YES,AUTOSELECT=YES" in master.text
    assert "\nvideo.m3u8\n" in master.text

    # As many subtitle segments as video segments.
    video_text = playlist.read_text()
    bounds = segment_bounds(video_text)
    subs = await client.get(f"/media/{episode_id}/subs.m3u8")
    assert len(re.findall(r"^sub_\d{5}\.vtt$", subs.text, re.MULTILINE)) == len(bounds)

    # A known cue — the first one that starts 30 s in or later — is in the
    # segment whose span contains its start, and in no neighbour.
    cues = vtt_to_cues((directory / "subs.vtt").read_text())
    cue = next(item for item in cues if item.start_s >= 30.0)
    index = next(n for n, (start, end) in enumerate(bounds) if start <= cue.start_s < end)
    line = next(text for text in cue.text.splitlines() if text.strip())
    segment = await client.get(f"/media/{episode_id}/sub_{index:05d}.vtt")
    assert segment.status_code == 200
    assert line in segment.text
    # The offset is the first video frame's PTS: what remux.json recorded,
    # and what ffprobe says of init.mp4 + seg_00000.m4s read as one file.
    mapped = re.search(r"X-TIMESTAMP-MAP=MPEGTS:(\d+),LOCAL:00:00:00\.000", segment.text)
    assert mapped is not None
    info = RemuxInfo.from_json((directory / "remux.json").read_text())
    first = tmp_path / "first.mp4"
    first.write_bytes(
        (directory / "init.mp4").read_bytes() + (directory / "seg_00000.m4s").read_bytes()
    )
    head = await _ffprobe(first)
    video = next(stream for stream in head["streams"] if stream["codec_type"] == "video")
    assert int(mapped.group(1)) == info.mpegts == round(float(video["start_time"]) * 90_000)
    for neighbour in (index - 1, index + 1):
        if 0 <= neighbour < len(bounds):
            other = await client.get(f"/media/{episode_id}/sub_{neighbour:05d}.vtt")
            assert (cue.start_s, cue.text) not in {
                (item.start_s, item.text) for item in vtt_to_cues(other.text)
            }

    elapsed = perf_counter() - started
    assert elapsed < WALL_LIMIT_SECONDS, f"took {elapsed:.1f}s"
