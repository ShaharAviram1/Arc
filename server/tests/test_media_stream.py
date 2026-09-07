"""Serving HLS behind a session (FR-S1, spec §7).

Every test here runs against a real directory of real (tiny) files under a
``DATA_DIR`` of its own, because everything worth asserting about this router
is about bytes and headers: a range that is 100 bytes long, an ETag that
survives a revalidation, a file name that is not a path.

The rendition fixture writes the same shapes ffmpeg does — ``index.m3u8``
with relative segment URIs, ``init.mp4``, ``seg_NNNNN.m4s`` — so the traversal
and pattern tests are refusing names that look exactly like the ones that work.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from arc.api.media_stream import PLAYLIST_TYPE, SEGMENT_CACHE, SEGMENT_TYPE
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import Anime, Episode, EpisodeState, User
from arc.services.media.names import output_dir_for
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "viewer@arc.test"
USER_PASSWORD = "viewer-password"

#: One segment's worth of bytes: enough that a 100-byte range is a proper
#: subset of it, and non-repeating so a wrong offset would be visible.
SEGMENT_BYTES = bytes(range(256)) * 4

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:6
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.000000,
seg_00000.m4s
#EXTINF:6.000000,
seg_00001.m4s
#EXT-X-ENDLIST
"""


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    """The conftest settings with ``DATA_DIR`` pointed at this test's own tree.

    Overriding the fixture rather than monkeypatching a path: the app, the
    router and :func:`output_dir_for` all read the same ``Settings``, so
    replacing it is the one change that makes every one of them agree.
    """
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


async def add_episode(
    factory: SessionFactory,
    *,
    anilist_id: int,
    state: EpisodeState = EpisodeState.READY,
) -> int:
    """One show with one episode in ``state``; returns the episode id."""
    async with factory() as session:
        anime = Anime(
            anilist_id=anilist_id,
            summary_source="anilist",
            detail_source="anilist",
            title_romaji="Stream Show",
            format="TV",
            status="FINISHED",
            episodes=1,
        )
        session.add(anime)
        await session.flush()
        episode = Episode(anime_id=anime.id, number=1, state=state)
        session.add(episode)
        await session.commit()
        return episode.id


def write_rendition(settings: Settings, episode_id: int, *, segments: int = 2) -> Path:
    """A rendition directory shaped like the encoder's output."""
    directory = output_dir_for(settings, episode_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.m3u8").write_text(PLAYLIST)
    (directory / "init.mp4").write_bytes(b"initsegment" * 8)
    for index in range(segments):
        (directory / f"seg_{index:05d}.m4s").write_bytes(SEGMENT_BYTES)
    return directory


@pytest.fixture
async def episode_id(api_factory: SessionFactory, settings: Settings) -> int:
    found = await add_episode(api_factory, anilist_id=940001)
    write_rendition(settings, found)
    return found


# --- Authentication -----------------------------------------------------------


async def test_an_anonymous_playlist_request_is_401(anon: AsyncClient, episode_id: int) -> None:
    """Spec §7: all media routes require a session."""
    response = await anon.get(f"/media/{episode_id}/index.m3u8")

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


async def test_an_anonymous_segment_request_is_401(anon: AsyncClient, episode_id: int) -> None:
    response = await anon.get(f"/media/{episode_id}/seg_00000.m4s")

    assert response.status_code == 401


# --- What may be asked for ----------------------------------------------------


async def test_an_unknown_episode_is_404(client: AsyncClient) -> None:
    response = await client.get("/media/999999/index.m3u8")

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


async def test_an_episode_that_is_not_ready_is_404(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    """A preparing episode may have a directory; it is still not playable."""
    preparing = await add_episode(api_factory, anilist_id=940002, state=EpisodeState.PREPARING)
    write_rendition(settings, preparing)

    assert (await client.get(f"/media/{preparing}/index.m3u8")).status_code == 404
    assert (await client.get(f"/media/{preparing}/seg_00000.m4s")).status_code == 404


async def test_a_ready_episode_with_no_files_is_404(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Retention can take the directory out from under a ``ready`` row."""
    swept = await add_episode(api_factory, anilist_id=940003)

    assert (await client.get(f"/media/{swept}/index.m3u8")).status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "seg_1.m4s",  # not five digits
        "seg_000000.m4s",  # six
        "evil.m3u8",
        "init.mp4.bak",
        "..",
        "%2e%2e%2fetc%2fpasswd",
        "..%2f..%2fetc%2fpasswd",
        "seg_00000.m4s%00.txt",
    ],
)
async def test_a_name_the_encoder_never_wrote_is_refused(
    client: AsyncClient, episode_id: int, name: str
) -> None:
    """The pattern is the whole defence, and it admits no separator (spec §7)."""
    response = await client.get(f"/media/{episode_id}/{name}")

    assert response.status_code in (404, 422), response.text
    assert "video" not in response.headers.get("content-type", "")


async def test_a_traversal_out_of_the_rendition_directory_reads_nothing(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    """The classic shape, with a real file to find at the other end."""
    (settings.data_dir / "secret.txt").write_text("password")

    response = await client.get(f"/media/{episode_id}/..%2f..%2fsecret.txt")

    assert response.status_code in (404, 422)
    assert "password" not in response.text


async def test_a_name_with_a_trailing_newline_is_not_the_name(
    client: AsyncClient, episode_id: int
) -> None:
    """``$`` would match before it; :data:`SEGMENT_PATTERN` anchors with ``\\z``.

    Path parameters are percent-decoded before the pattern sees them, so this
    is a real string the router is asked for and not a curiosity about regexes.
    """
    response = await client.get(f"/media/{episode_id}/seg_00000.m4s%0A")

    assert response.status_code == 422


async def test_an_id_too_large_for_bigint_is_a_422_not_a_500(client: AsyncClient) -> None:
    """A number no row can have is a malformed request, not a database error."""
    response = await client.get("/media/99999999999999999999/index.m3u8")

    assert response.status_code == 422


# --- Symlinks -----------------------------------------------------------------
#
# The pattern settles what a request may *say*; these settle what may be at the
# other end of it. A rendition is written by ffmpeg and contains plain files,
# so a link inside one is never legitimate and is refused outright — with the
# 404 every other miss gets, because "there is a symlink here" is not something
# a caller has any business learning.


async def test_a_segment_that_is_a_symlink_out_of_the_tree_is_404(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    """The headline case: a link to a file the server can read and must not send."""
    directory = output_dir_for(settings, episode_id)
    outside = settings.data_dir.parent / "outside.txt"
    outside.write_text("root:x:0:0:root:/root:/bin/sh\n")
    segment = directory / "seg_00001.m4s"
    segment.unlink()
    segment.symlink_to(outside)

    response = await client.get(f"/media/{episode_id}/seg_00001.m4s")

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
    assert "root:x:0:0" not in response.text
    # …and the plain file beside it still serves, so the refusal is about the
    # link rather than about the episode.
    assert (await client.get(f"/media/{episode_id}/seg_00000.m4s")).status_code == 200


async def test_a_segment_that_is_a_symlink_inside_data_dir_is_also_404(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    """Confinement is not the whole rule: a link is refused wherever it points.

    Resolving and comparing against the rendition root would happily serve this
    one — it stays inside ``DATA_DIR`` — which is exactly why the ``lstat``
    check exists as well. Another user's rendition is inside ``DATA_DIR`` too.
    """
    directory = output_dir_for(settings, episode_id)
    inside = settings.data_dir / "renditions" / "notes.txt"
    inside.write_text("someone else's rendition")
    segment = directory / "seg_00001.m4s"
    segment.unlink()
    segment.symlink_to(inside)

    response = await client.get(f"/media/{episode_id}/seg_00001.m4s")

    assert response.status_code == 404
    assert "someone else" not in response.text


async def test_an_episode_directory_that_is_a_symlink_serves_nothing(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, tmp_path: Path
) -> None:
    """The other half of the hole: move the root and everything under it resolves.

    Every file below a symlinked ``renditions/<id>`` is *inside* the directory
    the request named, so the confinement check alone would pass all of them.
    """
    linked = await add_episode(api_factory, anilist_id=940004)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "index.m3u8").write_text(PLAYLIST)
    (elsewhere / "seg_00000.m4s").write_bytes(SEGMENT_BYTES)
    directory = output_dir_for(settings, linked)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.symlink_to(elsewhere, target_is_directory=True)

    assert (await client.get(f"/media/{linked}/index.m3u8")).status_code == 404
    assert (await client.get(f"/media/{linked}/seg_00000.m4s")).status_code == 404


# --- The playlist -------------------------------------------------------------


async def test_the_playlist_is_served_with_its_own_type_and_no_cache(
    client: AsyncClient, episode_id: int
) -> None:
    response = await client.get(f"/media/{episode_id}/index.m3u8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(PLAYLIST_TYPE)
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.text.startswith("#EXTM3U")
    # Relative URIs, so a player resolves them back onto this router without a
    # rewrite (architecture.md §5.4).
    assert "seg_00000.m4s" in response.text
    assert "/media/" not in response.text


# --- Segments -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["init.mp4", "seg_00000.m4s", "seg_00001.m4s"])
async def test_a_segment_is_video_mp4_and_cached_forever(
    client: AsyncClient, episode_id: int, name: str
) -> None:
    response = await client.get(f"/media/{episode_id}/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(SEGMENT_TYPE)
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == SEGMENT_CACHE
    assert response.headers["etag"]


async def test_a_range_request_returns_exactly_that_range(
    client: AsyncClient, episode_id: int
) -> None:
    """FR-S1's range support, which is what seeking in a long episode costs."""
    size = len(SEGMENT_BYTES)

    response = await client.get(
        f"/media/{episode_id}/seg_00000.m4s", headers={"Range": "bytes=0-99"}
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-99/{size}"
    assert len(response.content) == 100
    assert response.content == SEGMENT_BYTES[:100]


async def test_a_range_from_the_middle_starts_where_it_was_asked_to(
    client: AsyncClient, episode_id: int
) -> None:
    response = await client.get(
        f"/media/{episode_id}/seg_00000.m4s", headers={"Range": "bytes=256-511"}
    )

    assert response.status_code == 206
    assert response.content == SEGMENT_BYTES[256:512]


async def test_a_range_that_starts_past_the_end_is_416(
    client: AsyncClient, episode_id: int
) -> None:
    """416 answers in JSON like every other refusal, and keeps its validators.

    Starlette writes this one itself, as ``text/plain``, from inside
    ``FileResponse`` — a client that had to sniff the body of a 416 while
    parsing the body of a 404 is a client with a bug waiting in it. The ETag
    and the caching rules come with it because the file has not changed: only
    the request's idea of it was wrong.
    """
    size = len(SEGMENT_BYTES)

    response = await client.get(
        f"/media/{episode_id}/seg_00000.m4s", headers={"Range": f"bytes={size}-"}
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{size}"
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "that range is not inside the file"}
    assert response.headers["cache-control"] == SEGMENT_CACHE
    assert response.headers["etag"]


async def test_a_malformed_range_header_is_a_json_400(client: AsyncClient, episode_id: int) -> None:
    """The other half Starlette writes as plain text."""
    response = await client.get(
        f"/media/{episode_id}/seg_00000.m4s", headers={"Range": "kilograms=0-9"}
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "range header is malformed"}


async def test_a_matching_if_none_match_is_304_with_no_body(
    client: AsyncClient, episode_id: int
) -> None:
    first = await client.get(f"/media/{episode_id}/seg_00000.m4s")
    etag = first.headers["etag"]

    second = await client.get(f"/media/{episode_id}/seg_00000.m4s", headers={"If-None-Match": etag})

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == SEGMENT_CACHE
    # RFC 9110 §15.4.5: a 304 carries the validators the 200 would have. A
    # cache that was given ``Last-Modified`` once must not lose it here.
    assert second.headers["last-modified"] == first.headers["last-modified"]


async def test_a_stale_if_none_match_gets_the_file(client: AsyncClient, episode_id: int) -> None:
    response = await client.get(
        f"/media/{episode_id}/seg_00000.m4s", headers={"If-None-Match": '"deadbeef-1"'}
    )

    assert response.status_code == 200
    assert len(response.content) == len(SEGMENT_BYTES)


async def test_the_etag_changes_when_a_re_encode_rewrites_the_same_name(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    """``force=true`` reuses the names (FR-P5); ``immutable`` must not lie."""
    first = (await client.get(f"/media/{episode_id}/seg_00000.m4s")).headers["etag"]
    segment = output_dir_for(settings, episode_id) / "seg_00000.m4s"
    segment.write_bytes(SEGMENT_BYTES + b"re-encoded")

    response = await client.get(
        f"/media/{episode_id}/seg_00000.m4s", headers={"If-None-Match": first}
    )

    assert response.status_code == 200
    assert response.headers["etag"] != first


async def test_the_playlist_also_revalidates(client: AsyncClient, episode_id: int) -> None:
    first = await client.get(f"/media/{episode_id}/index.m3u8")

    second = await client.get(
        f"/media/{episode_id}/index.m3u8", headers={"If-None-Match": first.headers["etag"]}
    )

    assert second.status_code == 304


async def test_a_head_request_reports_the_length_without_the_body(
    client: AsyncClient, episode_id: int
) -> None:
    """hls.js and every media element probe with HEAD before they fetch."""
    response = await client.head(f"/media/{episode_id}/seg_00000.m4s")

    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(SEGMENT_BYTES))
    assert response.content == b""
