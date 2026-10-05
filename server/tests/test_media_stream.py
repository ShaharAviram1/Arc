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
from typing import Any

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


# --- The whole episode as one file (FR-S7) --------------------------------------
#
# ``episode.mp4`` is the init segment and every media segment, in playlist
# order, byte for byte. The fixture's parts are distinguishable (the init is
# ASCII, the segments are a byte ramp), so a part out of order or an offset off
# by one shows up as a byte mismatch rather than as a length that happens to
# agree.

DEMO_EMAIL = "demo@arc.test"
DEMO_PASSWORD = "demo-password"


def whole_file(settings: Settings, episode_id: int) -> bytes:
    """What the download must equal: init, then the segments in playlist order."""
    directory = output_dir_for(settings, episode_id)
    names = ["init.mp4", "seg_00000.m4s", "seg_00001.m4s"]
    return b"".join((directory / name).read_bytes() for name in names)


def download_path(episode_id: int) -> str:
    return f"/media/{episode_id}/episode.mp4"


async def test_the_download_is_init_then_every_segment_in_order(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    # Make the two segments differ, so a swapped order cannot pass.
    directory = output_dir_for(settings, episode_id)
    (directory / "seg_00001.m4s").write_bytes(SEGMENT_BYTES[::-1])
    expected = whole_file(settings, episode_id)

    response = await client.get(download_path(episode_id))

    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"].startswith('"')
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "content-range" not in response.headers


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=0-99", 0, 99),
        # Across the init/segment boundary (the init is 88 bytes).
        ("bytes=80-1199", 80, 1199),
        # Across the boundary between the two segments.
        ("bytes=1100-1120", 1100, 1120),
        # Open-ended: from N to the end.
        ("bytes=2000-", 2000, None),
        # Suffix: the last N bytes.
        ("bytes=-50", None, None),
        # An end past the file is clamped, not refused.
        ("bytes=10-999999", 10, None),
    ],
)
async def test_a_range_of_the_download_is_exactly_that_range(
    client: AsyncClient,
    episode_id: int,
    settings: Settings,
    header: str,
    first: int | None,
    last: int | None,
) -> None:
    expected = whole_file(settings, episode_id)
    total = len(expected)
    if first is None:
        first = total - 50
    end = total - 1 if last is None else last

    response = await client.get(download_path(episode_id), headers={"Range": header})

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes {first}-{end}/{total}"
    assert response.headers["content-length"] == str(end - first + 1)
    assert response.content == expected[first : end + 1]


async def test_a_multi_range_request_gets_the_whole_file(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    """RFC 9110 allows a 200 for it, and multipart is machinery nobody uses."""
    response = await client.get(download_path(episode_id), headers={"Range": "bytes=0-9,20-29"})

    assert response.status_code == 200
    assert response.content == whole_file(settings, episode_id)


async def test_an_unsatisfiable_download_range_is_a_json_416(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    total = len(whole_file(settings, episode_id))

    response = await client.get(download_path(episode_id), headers={"Range": f"bytes={total}-"})

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{total}"
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "that range is not inside the file"}
    assert response.headers["etag"]


@pytest.mark.parametrize("header", ["kilograms=0-9", "bytes=9-0", "bytes=a-b", "bytes=-"])
async def test_a_malformed_download_range_is_a_json_400(
    client: AsyncClient, episode_id: int, header: str
) -> None:
    response = await client.get(download_path(episode_id), headers={"Range": header})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "range header is malformed"}


async def test_a_matching_if_none_match_on_the_download_is_304(
    client: AsyncClient, episode_id: int
) -> None:
    etag = (await client.head(download_path(episode_id))).headers["etag"]

    response = await client.get(download_path(episode_id), headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == "private, no-cache"


async def test_if_range_with_the_current_etag_honours_the_range(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    """How the offline downloader resumes: its first ETag, the next chunk."""
    etag = (await client.head(download_path(episode_id))).headers["etag"]

    response = await client.get(
        download_path(episode_id), headers={"Range": "bytes=100-199", "If-Range": etag}
    )

    assert response.status_code == 206
    assert response.content == whole_file(settings, episode_id)[100:200]


@pytest.mark.parametrize("validator", ['"deadbeef-1"', "W/{etag}", "Sat, 01 Jan 2000 00:00:00 GMT"])
async def test_if_range_with_any_other_validator_gets_the_whole_file(
    client: AsyncClient, episode_id: int, settings: Settings, validator: str
) -> None:
    """A stale, weak or date validator: "my copy is old", so all of the new one."""
    etag = (await client.head(download_path(episode_id))).headers["etag"]

    response = await client.get(
        download_path(episode_id),
        headers={"Range": "bytes=100-199", "If-Range": validator.format(etag=etag)},
    )

    assert response.status_code == 200
    assert response.content == whole_file(settings, episode_id)
    assert "content-range" not in response.headers


async def test_the_download_etag_changes_when_a_re_encode_rewrites_a_segment(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    """``force`` reuses every name (FR-P5); a resume must notice and restart."""
    first = (await client.head(download_path(episode_id))).headers["etag"]
    segment = output_dir_for(settings, episode_id) / "seg_00001.m4s"
    segment.write_bytes(SEGMENT_BYTES + b"re-encoded")

    response = await client.get(download_path(episode_id), headers={"If-None-Match": first})

    assert response.status_code == 200
    assert response.headers["etag"] != first
    # And unchanged files keep their validator: it is not a clock.
    again = await client.head(download_path(episode_id))
    assert again.headers["etag"] == response.headers["etag"]


async def test_a_head_of_the_download_reports_its_length_without_a_body(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    response = await client.head(download_path(episode_id))

    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(whole_file(settings, episode_id)))
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == b""


async def test_an_anonymous_download_is_401(anon: AsyncClient, episode_id: int) -> None:
    response = await anon.get(download_path(episode_id))

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


async def test_the_demo_account_cannot_download(
    api_app: FastAPI, api_factory: SessionFactory, episode_id: int
) -> None:
    """Owner, 2026-10-04: the demo account watches, it does not take files away."""
    demo = await add_user(api_factory, DEMO_EMAIL, DEMO_PASSWORD)
    async with api_factory() as session:
        row = await session.get(User, demo.id)
        assert row is not None
        row.is_demo = True
        await session.commit()

    async with api_transport(api_app) as http:
        await login(http, DEMO_EMAIL, DEMO_PASSWORD)
        response = await http.get(download_path(episode_id))
        head = await http.head(download_path(episode_id))
        # Streaming is unaffected: only the file download is off.
        playlist = await http.get(f"/media/{episode_id}/index.m3u8")

    assert response.status_code == 403
    assert response.json() == {"detail": "downloads are turned off for the demo account"}
    assert head.status_code == 403
    assert playlist.status_code == 200


async def test_a_download_of_an_episode_that_is_not_ready_is_404(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    preparing = await add_episode(api_factory, anilist_id=940010, state=EpisodeState.PREPARING)
    write_rendition(settings, preparing)

    response = await client.get(download_path(preparing))

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


async def test_a_download_with_a_symlinked_part_is_404(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    directory = output_dir_for(settings, episode_id)
    outside = settings.data_dir.parent / "outside-download.txt"
    outside.write_text("root:x:0:0:root:/root:/bin/sh\n")
    segment = directory / "seg_00001.m4s"
    segment.unlink()
    segment.symlink_to(outside)

    response = await client.get(download_path(episode_id))

    assert response.status_code == 404
    assert "root:x:0:0" not in response.text


async def test_a_download_whose_playlist_is_a_symlink_is_404(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    directory = output_dir_for(settings, episode_id)
    elsewhere = settings.data_dir / "other.m3u8"
    elsewhere.write_text(PLAYLIST)
    (directory / "index.m3u8").unlink()
    (directory / "index.m3u8").symlink_to(elsewhere)

    assert (await client.get(download_path(episode_id))).status_code == 404


@pytest.mark.parametrize(
    "foreign",
    ["../../secret.txt", "/etc/hosts", "notes.txt", "https://example.com/seg_00000.m4s"],
)
async def test_a_playlist_naming_a_foreign_file_is_404(
    client: AsyncClient, episode_id: int, settings: Settings, foreign: str
) -> None:
    """A playlist is not trusted to name files: every name must be the encoder's."""
    (settings.data_dir / "secret.txt").write_text("password")
    directory = output_dir_for(settings, episode_id)
    (directory / "notes.txt").write_text("password")
    (directory / "index.m3u8").write_text(PLAYLIST.replace("seg_00001.m4s", foreign))

    response = await client.get(download_path(episode_id))

    assert response.status_code == 404
    assert "password" not in response.text


async def test_a_download_with_a_missing_segment_is_404_not_a_short_file(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    (output_dir_for(settings, episode_id) / "seg_00001.m4s").unlink()

    response = await client.get(download_path(episode_id))

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


async def test_the_download_filename_comes_from_the_database_and_is_sanitised(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    """Non-ASCII, quotes and slashes: none of them reach a path or break the header."""
    async with api_factory() as session:
        anime = Anime(
            anilist_id=940020,
            summary_source="anilist",
            detail_source="anilist",
            title_english='Frieren: "Beyond" Journey/End — 葬送',
            format="TV",
            status="FINISHED",
            episodes=12,
        )
        session.add(anime)
        await session.flush()
        episode = Episode(anime_id=anime.id, number=7, state=EpisodeState.READY)
        session.add(episode)
        await session.commit()
        found = episode.id
    write_rendition(settings, found)

    response = await client.head(download_path(found))

    disposition = response.headers["content-disposition"]
    assert disposition == (
        'attachment; filename="Frieren - Beyond - Journey - End - 07.mp4"; '
        "filename*=UTF-8''Frieren%20-%20Beyond%20-%20Journey%20-%20End%20%E2%80%94%20"
        "%E8%91%AC%E9%80%81%20-%2007.mp4"
    )


async def test_a_download_range_with_a_huge_number_is_a_400_not_a_500(
    client: AsyncClient, episode_id: int
) -> None:
    """Past Python's 4300-digit ``int`` limit, which raises a plain ValueError."""
    response = await client.get(
        download_path(episode_id), headers={"Range": "bytes=0-" + "9" * 5000}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "range header is malformed"}


async def test_a_download_from_a_symlinked_rendition_directory_is_404(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, tmp_path: Path
) -> None:
    linked = await add_episode(api_factory, anilist_id=940030)
    elsewhere = tmp_path / "elsewhere-download"
    elsewhere.mkdir()
    (elsewhere / "index.m3u8").write_text(PLAYLIST)
    (elsewhere / "init.mp4").write_bytes(b"initsegment")
    for index in range(2):
        (elsewhere / f"seg_{index:05d}.m4s").write_bytes(SEGMENT_BYTES)
    directory = output_dir_for(settings, linked)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.symlink_to(elsewhere, target_is_directory=True)

    response = await client.get(download_path(linked))

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


async def test_an_anonymous_download_head_is_401(anon: AsyncClient, episode_id: int) -> None:
    assert (await anon.head(download_path(episode_id))).status_code == 401


async def test_a_head_with_a_range_reports_the_span(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    total = len(whole_file(settings, episode_id))

    response = await client.head(download_path(episode_id), headers={"Range": "bytes=100-299"})

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 100-299/{total}"
    assert response.headers["content-length"] == "200"
    assert response.content == b""


async def test_a_part_changed_mid_download_ends_short_through_the_whole_stack(
    api_app: FastAPI,
    user: User,
    episode_id: int,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The abort reaches the client as a short body, never as two renditions spliced.

    Through every middleware (session refresh, origin check, CORS): the second
    segment is rewritten after the headers have gone out, so the response is
    cut off where it stood rather than finished with the new bytes.
    """
    from httpx import ASGITransport

    from arc.api import media_stream
    from tests.conftest import ORIGIN

    expected = whole_file(settings, episode_id)
    real_open = media_stream._open_part

    def rewrite_then_open(dir_fd: int, part: Any) -> Any:
        if part.name == "seg_00001.m4s":
            (output_dir_for(settings, episode_id) / part.name).write_bytes(b"Z" * 2000)
        return real_open(dir_fd, part)

    monkeypatch.setattr(media_stream, "_open_part", rewrite_then_open)

    transport = ASGITransport(app=api_app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"Origin": ORIGIN}
    ) as http:
        await login(http, USER_EMAIL, USER_PASSWORD)
        response = await http.get(download_path(episode_id))

    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(expected))
    assert len(response.content) < len(expected)
    # A prefix of the promised file, so not one byte of the rewrite got in.
    assert expected.startswith(response.content)
