"""Serving a remuxed rendition: the generated master, subtitle playlist and segments (M20 T2).

A remux leaves ``video.m3u8``, ``init.mp4``, ``seg_NNNNN.m4s``, the whole
WebVTT track as ``subs.vtt`` and ``remux.json`` in the rendition directory;
``index.m3u8``, ``subs.m3u8`` and ``sub_NNNNN.vtt`` exist only as answers
(architecture.md §5.4a). Every test here writes those files for real under a
``DATA_DIR`` of its own and asks the router, as :mod:`tests.test_media_stream`
does for a transcode.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from arc.api.media_stream import PLAYLIST_TYPE, TEXT_SUBTITLES_REFUSED
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import RENDITION_REMUX, Anime, Episode, EpisodeState, Rendition, User
from arc.services.media.names import RemuxInfo, output_dir_for
from arc.services.media.subtitles import Cue, cues_to_vtt
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "remux-viewer@arc.test"
USER_PASSWORD = "viewer-password"

VIDEO_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.006000,
seg_00000.m4s
#EXTINF:6.006000,
seg_00001.m4s
#EXTINF:2.000000,
seg_00002.m4s
#EXT-X-ENDLIST
"""

#: One cue per segment, plus a long one that starts in the first and runs
#: into the second (it must appear once, in the first), and one that starts
#: after the video's stated end (it belongs to the last segment).
CUES = [
    Cue(1.5, 3.0, "First."),
    Cue(5.5, 8.0, "Across the boundary."),
    Cue(7.0, 8.5, "<i>Second.</i>", "line:5%"),
    Cue(13.0, 13.9, "Third."),
    Cue(14.2, 15.0, "After the end."),
]

INFO = RemuxInfo(
    mpegts=7470, bandwidth=8_200_000, codecs="avc1.640028,mp4a.40.2", subtitle_name="English"
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


async def add_remux(
    factory: SessionFactory,
    settings: Settings,
    *,
    anilist_id: int,
    text_subtitles: bool = True,
    kind: str = RENDITION_REMUX,
) -> int:
    """A ready episode with a remuxed rendition on disk and its row."""
    async with factory() as session:
        anime = Anime(
            anilist_id=anilist_id,
            summary_source="anilist",
            detail_source="anilist",
            title_romaji="Remux Show",
            format="TV",
            status="FINISHED",
            episodes=1,
        )
        session.add(anime)
        await session.flush()
        episode = Episode(anime_id=anime.id, number=1, state=EpisodeState.READY)
        session.add(episode)
        await session.flush()
        directory = output_dir_for(settings, episode.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "video.m3u8").write_text(VIDEO_PLAYLIST)
        (directory / "init.mp4").write_bytes(b"INIT" * 8)
        for index in range(3):
            (directory / f"seg_{index:05d}.m4s").write_bytes(bytes([65 + index]) * 64)
        (directory / "remux.json").write_text(INFO.to_json())
        if text_subtitles:
            (directory / "subs.vtt").write_text(cues_to_vtt(CUES))
        session.add(
            Rendition(
                episode_id=episode.id,
                dir=str(directory),
                playlist_path=str(directory / "video.m3u8"),
                duration=14.012,
                width=1920,
                height=1080,
                subtitle_lang="en" if text_subtitles else None,
                audio_lang="ja",
                kind=kind,
                text_subtitles=text_subtitles,
            )
        )
        await session.commit()
        return episode.id


@pytest.fixture
async def episode_id(api_factory: SessionFactory, settings: Settings) -> int:
    return await add_remux(api_factory, settings, anilist_id=976001)


# --- The master playlist ------------------------------------------------------


async def test_the_master_names_the_video_and_a_default_subtitle_track(
    client: AsyncClient, episode_id: int
) -> None:
    response = await client.get(f"/media/{episode_id}/index.m3u8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(PLAYLIST_TYPE)
    assert response.headers["cache-control"] == "private, no-cache"
    lines = response.text.splitlines()
    media = next(line for line in lines if line.startswith("#EXT-X-MEDIA:"))
    assert "TYPE=SUBTITLES" in media
    assert "DEFAULT=YES,AUTOSELECT=YES" in media
    assert 'LANGUAGE="en"' in media and 'NAME="English"' in media
    assert 'URI="subs.m3u8"' in media
    stream = lines.index(next(line for line in lines if line.startswith("#EXT-X-STREAM-INF:")))
    assert lines[stream + 1] == "video.m3u8"
    assert "BANDWIDTH=8200000" in lines[stream]
    assert 'CODECS="avc1.640028,mp4a.40.2"' in lines[stream]
    assert "RESOLUTION=1920x1080" in lines[stream]
    assert 'SUBTITLES="subs"' in lines[stream]


async def test_the_master_revalidates_with_a_304(client: AsyncClient, episode_id: int) -> None:
    first = await client.get(f"/media/{episode_id}/index.m3u8")
    again = await client.get(
        f"/media/{episode_id}/index.m3u8", headers={"If-None-Match": first.headers["etag"]}
    )

    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == first.headers["etag"]


async def test_a_remux_without_a_text_track_offers_none(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    found = await add_remux(api_factory, settings, anilist_id=976002, text_subtitles=False)

    master = await client.get(f"/media/{found}/index.m3u8")
    subs = await client.get(f"/media/{found}/subs.m3u8")
    segment = await client.get(f"/media/{found}/sub_00000.vtt")

    assert master.status_code == 200
    assert "#EXT-X-MEDIA" not in master.text
    assert "SUBTITLES=" not in master.text
    assert master.text.rstrip().endswith("video.m3u8")
    assert subs.status_code == 404
    assert segment.status_code == 404


async def test_a_row_with_a_text_track_but_no_subs_vtt_offers_none(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    """The file is the truth: no ``subs.vtt``, no subtitle rendition and no playlist."""
    (output_dir_for(settings, episode_id) / "subs.vtt").unlink()

    master = await client.get(f"/media/{episode_id}/index.m3u8")
    subs = await client.get(f"/media/{episode_id}/subs.m3u8")
    segment = await client.get(f"/media/{episode_id}/sub_00000.vtt")

    assert master.status_code == 200
    assert "#EXT-X-MEDIA" not in master.text
    assert "SUBTITLES=" not in master.text
    assert subs.status_code == 404
    assert segment.status_code == 404


async def test_the_media_playlist_is_served_as_a_playlist(
    client: AsyncClient, episode_id: int
) -> None:
    response = await client.get(f"/media/{episode_id}/video.m3u8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(PLAYLIST_TYPE)
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.text == VIDEO_PLAYLIST


# --- The subtitle playlist and segments -----------------------------------------


async def test_the_subtitle_playlist_has_one_segment_per_video_segment(
    client: AsyncClient, episode_id: int
) -> None:
    video = (await client.get(f"/media/{episode_id}/video.m3u8")).text
    response = await client.get(f"/media/{episode_id}/subs.m3u8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(PLAYLIST_TYPE)
    assert response.headers["cache-control"] == "private, no-cache"
    names = re.findall(r"^sub_\d{5}\.vtt$", response.text, re.MULTILINE)
    assert len(names) == len(re.findall(r"^seg_\d{5}\.m4s$", video, re.MULTILINE)) == 3
    assert names == ["sub_00000.vtt", "sub_00001.vtt", "sub_00002.vtt"]
    assert "#EXT-X-TARGETDURATION:6" in response.text
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in response.text
    assert response.text.rstrip().endswith("#EXT-X-ENDLIST")
    durations = re.findall(r"^#EXTINF:([0-9.]+),", response.text, re.MULTILINE)
    assert [float(value) for value in durations] == [6.006, 6.006, 2.0]


async def test_each_cue_is_in_the_segment_it_starts_in(
    client: AsyncClient, episode_id: int
) -> None:
    bodies = [(await client.get(f"/media/{episode_id}/sub_{index:05d}.vtt")) for index in range(3)]

    for response in bodies:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/vtt")
        assert response.text.startswith("WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:7470,LOCAL:00:00:00.000\n")
    first, second, third = (response.text for response in bodies)
    assert "First." in first and "Across the boundary." in first
    assert "Across the boundary." not in second
    assert "00:00:07.000 --> 00:00:08.500 line:5%" in second
    assert "<i>Second.</i>" in second
    assert "Third." in third and "After the end." in third
    assert "First." not in second + third


async def test_a_subtitle_segment_past_the_last_is_404(
    client: AsyncClient, episode_id: int
) -> None:
    assert (await client.get(f"/media/{episode_id}/sub_00003.vtt")).status_code == 404
    assert (await client.get(f"/media/{episode_id}/sub_99999.vtt")).status_code == 404


@pytest.mark.parametrize(
    "name", ["..%2Fx.vtt", "..%2F..%2Fsubs.vtt", "subs.vtt", "remux.json", "sub_0.vtt", "x.vtt"]
)
async def test_names_outside_the_vocabulary_are_422(
    client: AsyncClient, episode_id: int, name: str
) -> None:
    """``subs.vtt`` and ``remux.json`` are on disk but are never served as they are."""
    response = await client.get(f"/media/{episode_id}/{name}")

    assert response.status_code == 422


async def test_a_subtitle_segment_revalidates_and_moves_with_the_track(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    url = f"/media/{episode_id}/sub_00001.vtt"
    first = await client.get(url)
    again = await client.get(url, headers={"If-None-Match": first.headers["etag"]})
    other = await client.get(f"/media/{episode_id}/sub_00002.vtt")

    # Revalidated, never immutable: a re-remux rewrites the track under the
    # same URL (a new subtitle language, FR-P5).
    assert first.headers["cache-control"] == "private, no-cache"
    assert again.status_code == 304 and again.content == b""
    assert again.headers["cache-control"] == "private, no-cache"
    assert other.headers["etag"] != first.headers["etag"], "the index is part of the validator"

    track = output_dir_for(settings, episode_id) / "subs.vtt"
    track.write_text(cues_to_vtt([Cue(7.5, 8.0, "Rewritten.")]))
    later = track.stat().st_mtime_ns + 5_000_000_000
    os.utime(track, ns=(later, later))
    redone = await client.get(url, headers={"If-None-Match": first.headers["etag"]})

    assert redone.status_code == 200
    assert "Rewritten." in redone.text
    assert redone.headers["etag"] != first.headers["etag"]


async def test_the_subtitle_files_of_a_transcode_are_404(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    found = await add_remux(api_factory, settings, anilist_id=976003, kind="transcode")

    assert (await client.get(f"/media/{found}/subs.m3u8")).status_code == 404
    assert (await client.get(f"/media/{found}/sub_00000.vtt")).status_code == 404


# --- Refusals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["index.m3u8", "video.m3u8", "subs.m3u8", "sub_00000.vtt", "seg_00000.m4s"]
)
async def test_anonymous_requests_are_401(anon: AsyncClient, episode_id: int, name: str) -> None:
    response = await anon.get(f"/media/{episode_id}/{name}")

    assert response.status_code == 401


@pytest.mark.parametrize("name", ["video.m3u8", "index.m3u8", "subs.m3u8", "sub_00000.vtt"])
async def test_a_symlinked_video_playlist_serves_nothing(
    client: AsyncClient, episode_id: int, settings: Settings, name: str
) -> None:
    directory = output_dir_for(settings, episode_id)
    elsewhere = settings.data_dir / "elsewhere.m3u8"
    elsewhere.write_text(VIDEO_PLAYLIST.replace("seg_00000.m4s", "secret_marker"))
    (directory / "video.m3u8").unlink()
    (directory / "video.m3u8").symlink_to(elsewhere)

    response = await client.get(f"/media/{episode_id}/{name}")

    assert response.status_code == 404
    assert "secret_marker" not in response.text


async def test_a_symlinked_track_serves_no_subtitle_segment(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    directory = output_dir_for(settings, episode_id)
    elsewhere = settings.data_dir / "elsewhere.vtt"
    elsewhere.write_text(cues_to_vtt([Cue(1.0, 2.0, "secret_marker")]))
    (directory / "subs.vtt").unlink()
    (directory / "subs.vtt").symlink_to(elsewhere)

    response = await client.get(f"/media/{episode_id}/sub_00000.vtt")

    assert response.status_code == 404
    assert "secret_marker" not in response.text


# --- The full-size download (FR-S7, Q7) -------------------------------------------


async def test_the_full_size_file_is_refused_for_a_rendition_with_text_subtitles(
    client: AsyncClient, episode_id: int
) -> None:
    response = await client.get(f"/media/{episode_id}/episode.mp4")

    assert response.status_code == 404
    assert response.json() == {"detail": TEXT_SUBTITLES_REFUSED}


async def test_the_full_size_file_of_a_remux_without_subtitles_concatenates_video_m3u8(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    found = await add_remux(api_factory, settings, anilist_id=976004, text_subtitles=False)

    response = await client.get(f"/media/{found}/episode.mp4")

    assert response.status_code == 200
    assert response.content == b"INIT" * 8 + b"A" * 64 + b"B" * 64 + b"C" * 64


async def test_the_show_page_says_how_the_rendition_was_made(
    client: AsyncClient, episode_id: int, api_factory: SessionFactory
) -> None:
    async with api_factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        anime_id = episode.anime_id

    body = (await client.get(f"/api/anime/{anime_id}")).json()
    row = next(item for item in body["episodes"] if item["id"] == episode_id)

    assert row["rendition"]["kind"] == "remux"
    assert row["rendition"]["subtitles"] is True
    assert row["download_url"] is None, "the full-size file is refused, so it is not offered"
