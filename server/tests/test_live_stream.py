"""Serving a progressive remux while the episode downloads (M20 T6, §5.4a).

Every test writes a live directory by hand under a ``DATA_DIR`` of its own —
what ffmpeg leaves mid-run: an EVENT ``video.m3u8`` that may name a segment
not renamed into place yet, ``init.mp4``, the segments, ``remux.json`` and a
subtitle file still being written under ``_work/`` — plus the ``remux_live``
job row that stands behind it, and asks the router and ``/play``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from arc.api.media_stream import LIVE_PLAYLIST_CACHE, LIVE_SEGMENT_CACHE, PLAYLIST_TYPE
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    RENDITION_REMUX,
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    Rendition,
    User,
    Want,
)
from arc.services.media.names import (
    REMUX_LIVE,
    STAGE_LIVE_DONE,
    STAGE_LIVE_REFUSED,
    STAGE_LIVE_REMUX,
    RemuxInfo,
    live_dir_for,
    output_dir_for,
)
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "live-viewer@arc.test"
USER_PASSWORD = "viewer-password"

FFMPEG_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:7
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.000000,
seg_00000.m4s
#EXTINF:6.000000,
seg_00001.m4s
#EXTINF:6.000000,
seg_00002.m4s
"""

#: The subtitle file ffmpeg is still writing: the last line is cut off, and
#: one whole cue starts past the last finished video segment (12 s).
GROWING_ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment
Style: Default,Arial,48,&H00FFFFFF,0,0,2

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,Hello there.
Dialogue: 0,0:00:07.00,0:00:08.00,Default,,0,0,0,,In the second.
Dialogue: 0,0:00:13.00,0:00:14.00,Default,,0,0,0,,Not yet.
Dialogue: 0,0:00:15.00,0:00:16.00,Default,,0,0,0,,Cut o"""

INFO = RemuxInfo(
    mpegts=7470, bandwidth=9_000_000, codecs="avc1.640028,mp4a.40.2", subtitle_name="English"
)


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
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


@pytest.fixture
async def anon(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield http


async def add_live(
    factory: SessionFactory,
    settings: Settings,
    *,
    anilist_id: int,
    stage: str = STAGE_LIVE_REMUX,
    status: JobStatus = JobStatus.RUNNING,
    state: EpisodeState = EpisodeState.DOWNLOADING,
    ended: bool = False,
    want_for: int | None = None,
) -> int:
    """A downloading episode with a live directory mid-run and its job."""
    async with factory() as session:
        anime = Anime(
            anilist_id=anilist_id,
            summary_source="anilist",
            detail_source="anilist",
            title_romaji="Live Show",
            format="TV",
            status="RELEASING",
            episodes=12,
        )
        session.add(anime)
        await session.flush()
        episode = Episode(anime_id=anime.id, number=1, state=state)
        session.add(episode)
        await session.flush()
        directory = live_dir_for(settings, episode.id)
        (directory / "_work").mkdir(parents=True)
        playlist = FFMPEG_PLAYLIST + ("#EXT-X-ENDLIST\n" if ended else "")
        (directory / "video.m3u8").write_text(playlist)
        (directory / "init.mp4").write_bytes(b"INIT" * 8)
        # seg_00002 is named by the playlist but not renamed into place yet,
        # unless the run has ended.
        for index in range(3 if ended else 2):
            (directory / f"seg_{index:05d}.m4s").write_bytes(bytes([65 + index]) * 64)
        (directory / "remux.json").write_text(INFO.to_json())
        (directory / "_work" / "sub.ass").write_text(GROWING_ASS)
        session.add(
            Job(
                type=REMUX_LIVE,
                payload={
                    "episode_id": episode.id,
                    "stage": stage,
                    "expected_duration": 1440.0,
                    "front_seconds": 12.0,
                    "front_bytes": 4_000_000,
                    "file_size": 1_400_000_000,
                    "download_progress": 0.43,
                    "seeders": 12,
                    "width": 1920,
                    "height": 1080,
                    "subtitle_lang": "en",
                },
                status=status,
                attempts=1,
                locked_by="test:1" if status is JobStatus.RUNNING else None,
                locked_at=datetime.now(UTC) if status is JobStatus.RUNNING else None,
            )
        )
        if want_for is not None:
            session.add(
                Want(user_id=want_for, episode_id=episode.id, demanded_at=datetime.now(UTC))
            )
        await session.commit()
        return episode.id


@pytest.fixture
async def live_id(api_factory: SessionFactory, settings: Settings, user: User) -> int:
    return await add_live(api_factory, settings, anilist_id=977001, want_for=user.id)


# --- Playlists -------------------------------------------------------------------------


async def test_the_live_master_is_served_while_downloading(
    client: AsyncClient, live_id: int
) -> None:
    response = await client.get(f"/media/{live_id}/index.m3u8")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(PLAYLIST_TYPE)
    assert response.headers["cache-control"] == LIVE_PLAYLIST_CACHE
    assert 'URI="subs.m3u8"' in response.text
    assert "RESOLUTION=1920x1080" in response.text
    assert response.text.rstrip().endswith("video.m3u8")


async def test_the_live_video_playlist_has_the_fixed_header_and_only_existing_segments(
    client: AsyncClient, live_id: int
) -> None:
    response = await client.get(f"/media/{live_id}/video.m3u8")
    assert response.status_code == 200
    assert response.headers["cache-control"] == LIVE_PLAYLIST_CACHE
    lines = response.text.splitlines()
    assert "#EXT-X-TARGETDURATION:8" in lines
    assert "#EXT-X-TARGETDURATION:7" not in lines
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in lines
    assert "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES" in lines
    assert "#EXT-X-INDEPENDENT-SEGMENTS" in lines
    assert [line for line in lines if line.startswith("seg_")] == [
        "seg_00000.m4s",
        "seg_00001.m4s",
    ]
    assert "#EXT-X-ENDLIST" not in lines


async def test_endlist_once_ffmpeg_wrote_it(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    found = await add_live(
        api_factory,
        settings,
        anilist_id=977002,
        ended=True,
        stage=STAGE_LIVE_DONE,
        status=JobStatus.DONE,
        state=EpisodeState.MATCHING,
    )
    response = await client.get(f"/media/{found}/video.m3u8")
    assert response.status_code == 200
    assert response.text.rstrip().endswith("#EXT-X-ENDLIST")
    assert "seg_00002.m4s" in response.text


async def test_live_segments_are_served_briefly_cached(client: AsyncClient, live_id: int) -> None:
    response = await client.get(f"/media/{live_id}/seg_00001.m4s")
    assert response.status_code == 200
    assert response.content == b"B" * 64
    assert response.headers["cache-control"] == LIVE_SEGMENT_CACHE
    assert (await client.get(f"/media/{live_id}/init.mp4")).status_code == 200
    assert (await client.get(f"/media/{live_id}/seg_00002.m4s")).status_code == 404


async def test_the_live_subtitles_mirror_the_video_and_hold_back_cues(
    client: AsyncClient, live_id: int
) -> None:
    subs = await client.get(f"/media/{live_id}/subs.m3u8")
    assert subs.status_code == 200
    assert subs.headers["cache-control"] == LIVE_PLAYLIST_CACHE
    assert [line for line in subs.text.splitlines() if line.startswith("sub_")] == [
        "sub_00000.vtt",
        "sub_00001.vtt",
    ]
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in subs.text
    assert "#EXT-X-TARGETDURATION:8" in subs.text

    first = await client.get(f"/media/{live_id}/sub_00000.vtt")
    second = await client.get(f"/media/{live_id}/sub_00001.vtt")
    assert first.status_code == 200 and "Hello there." in first.text
    assert "X-TIMESTAMP-MAP=MPEGTS:7470" in first.text
    assert "In the second." in second.text
    # Past the last finished segment, and the half-written line: not served.
    assert "Not yet." not in second.text and "Cut o" not in second.text
    assert (await client.get(f"/media/{live_id}/sub_00002.vtt")).status_code == 404


# --- When the live directory is not served ----------------------------------------------


async def test_unauthenticated_is_401(anon: AsyncClient, live_id: int) -> None:
    for name in ("index.m3u8", "video.m3u8", "seg_00000.m4s", "subs.m3u8"):
        assert (await anon.get(f"/media/{live_id}/{name}")).status_code == 401


async def test_a_symlink_under_live_is_404(
    client: AsyncClient, live_id: int, settings: Settings, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"secret")
    directory = live_dir_for(settings, live_id)
    (directory / "seg_00001.m4s").unlink()
    (directory / "seg_00001.m4s").symlink_to(secret)
    response = await client.get(f"/media/{live_id}/seg_00001.m4s")
    assert response.status_code == 404
    # And it is not listed either.
    playlist = await client.get(f"/media/{live_id}/video.m3u8")
    assert "seg_00001.m4s" not in playlist.text


async def test_a_symlinked_live_directory_is_404(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    found = await add_live(api_factory, settings, anilist_id=977003)
    directory = live_dir_for(settings, found)
    moved = directory.with_name("elsewhere")
    os.replace(directory, moved)
    directory.symlink_to(moved)
    for name in ("index.m3u8", "video.m3u8", "seg_00000.m4s"):
        assert (await client.get(f"/media/{found}/{name}")).status_code == 404


@pytest.mark.parametrize("name", ["..%2Fx.vtt", "owner.json", "remux.json", "_work%2Fsub.ass"])
async def test_names_outside_the_vocabulary_are_422(
    client: AsyncClient, live_id: int, name: str
) -> None:
    assert (await client.get(f"/media/{live_id}/{name}")).status_code == 422


async def test_a_refused_job_stands_behind_nothing(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    found = await add_live(
        api_factory, settings, anilist_id=977004, stage=STAGE_LIVE_REFUSED, status=JobStatus.DONE
    )
    assert (await client.get(f"/media/{found}/index.m3u8")).status_code == 404
    assert (await client.get(f"/media/{found}/seg_00000.m4s")).status_code == 404


async def test_a_ready_episode_is_served_from_its_rendition_not_live(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    found = await add_live(
        api_factory,
        settings,
        anilist_id=977005,
        state=EpisodeState.READY,
        stage=STAGE_LIVE_DONE,
        status=JobStatus.DONE,
        ended=True,
    )
    rendition = output_dir_for(settings, found)
    rendition.mkdir(parents=True)
    (rendition / "index.m3u8").write_text("#EXTM3U\n#EXT-X-VERSION:7\n#RENDITION\n")
    (rendition / "seg_00000.m4s").write_bytes(b"R" * 8)
    async with api_factory() as session:
        session.add(
            Rendition(
                episode_id=found,
                dir=str(rendition),
                playlist_path=str(rendition / "index.m3u8"),
                kind="transcode",
            )
        )
        await session.commit()
    playlist = await client.get(f"/media/{found}/index.m3u8")
    assert "#RENDITION" in playlist.text
    assert (await client.get(f"/media/{found}/seg_00000.m4s")).content == b"R" * 8
    assert RENDITION_REMUX != "transcode"


async def test_the_whole_episode_download_is_not_offered_from_live(
    client: AsyncClient, live_id: int
) -> None:
    assert (await client.get(f"/media/{live_id}/episode.mp4")).status_code == 404


# --- /play ----------------------------------------------------------------------------------


async def test_play_reports_the_live_stream(client: AsyncClient, live_id: int) -> None:
    response = await client.get(f"/api/episodes/{live_id}/play")
    assert response.status_code == 200
    body = response.json()
    assert body["playlist_url"] == f"/media/{live_id}/index.m3u8"
    assert body["pending"] is None
    assert body["duration"] == 1440.0
    assert body["live"] == {
        "front_seconds": 12.0,
        "expected_duration": 1440.0,
        "download_progress": 0.43,
        "seeders": 12,
        "ended": False,
    }
    assert body["subtitles"] == {"lang": "en"}


async def test_play_without_a_live_directory_is_pending(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    found = await add_live(
        api_factory,
        settings,
        anilist_id=977006,
        want_for=user.id,
        stage=STAGE_LIVE_REFUSED,
        status=JobStatus.DONE,
    )
    body = (await client.get(f"/api/episodes/{found}/play")).json()
    assert body["playlist_url"] is None
    assert body["live"] is None
    assert body["pending"]["state"] == "downloading"


async def test_play_stays_pending_until_the_first_segment(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    """A live remux still waiting for the head hands out no empty playlist."""
    found = await add_live(
        api_factory, settings, anilist_id=977007, want_for=user.id, stage="waiting"
    )
    directory = live_dir_for(settings, found)
    for segment in directory.glob("seg_*.m4s"):
        segment.unlink()
    body = (await client.get(f"/api/episodes/{found}/play")).json()
    assert body["live"] is None and body["playlist_url"] is None
    assert body["pending"]["state"] == "downloading"
    assert body["episode"]["live_playable"] is False
    assert (await client.get(f"/media/{found}/video.m3u8")).status_code == 404

    # The first segment lands: now it plays, and the episode says so.
    (directory / "seg_00000.m4s").write_bytes(b"A" * 64)
    body = (await client.get(f"/api/episodes/{found}/play")).json()
    assert body["live"] is not None and body["playlist_url"] == f"/media/{found}/index.m3u8"
    assert body["live"]["front_seconds"] == 6.0
    assert body["episode"]["live_playable"] is True


async def test_the_same_urls_play_across_the_adoption(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    """Live, then the instant between ``ready`` and the rename, then the rendition."""
    found = await add_live(api_factory, settings, anilist_id=977008, want_for=user.id, ended=True)
    urls = [f"/media/{found}/{name}" for name in ("index.m3u8", "video.m3u8", "seg_00001.m4s")]
    assert [(await client.get(url)).status_code for url in urls] == [200, 200, 200]

    # ``ready`` and the row are committed; ``live/<id>`` is not renamed yet.
    live = live_dir_for(settings, found)
    rendition = output_dir_for(settings, found)
    async with api_factory() as session:
        episode = await session.get(Episode, found)
        assert episode is not None
        episode.state = EpisodeState.READY
        session.add(
            Rendition(
                episode_id=found,
                dir=str(rendition),
                playlist_path=str(rendition / "video.m3u8"),
                kind=RENDITION_REMUX,
                text_subtitles=False,
                width=1920,
                height=1080,
            )
        )
        await session.commit()
    assert [(await client.get(url)).status_code for url in urls] == [200, 200, 200]
    assert (await client.get(urls[2])).content == b"B" * 64

    # Renamed: the very same URLs, now from the rendition, immutable.
    rendition.parent.mkdir(parents=True, exist_ok=True)
    os.replace(live, rendition)
    responses = [await client.get(url) for url in urls]
    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[2].content == b"B" * 64
    assert "immutable" in responses[2].headers["cache-control"]
