"""The small offline copy over HTTP (FR-P6): the media route and the two endpoints.

``GET /media/{id}/offline.mp4`` is asserted the way ``episode.mp4`` is in
:mod:`tests.test_media_stream` — real bytes in a ``DATA_DIR`` of the test's own,
ranges, validators, the symlink refusals — and ``POST``/``GET
/api/episodes/{id}/offline`` and ``EpisodeOut.offline`` for the contract the
device's client is written against.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    ReviewState,
    User,
)
from arc.services.media.download import stat_etag
from arc.services.media.names import OFFLINE_ENCODE, offline_path_for
from tests.conftest import add_user, api_transport, login
from tests.test_media_stream import add_episode, write_rendition

pytestmark = pytest.mark.pg

USER_EMAIL = "keeper@arc.test"
USER_PASSWORD = "keeper-password"
DEMO_EMAIL = "demo-offline@arc.test"
DEMO_PASSWORD = "demo-password"

#: A copy's bytes: non-repeating, so a wrong offset shows.
COPY_BYTES = bytes(range(256)) * 16


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        env="test", database_url=test_database_url, data_dir=tmp_path, _env_file=None
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


@pytest.fixture
async def demo(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    made = await add_user(api_factory, DEMO_EMAIL, DEMO_PASSWORD)
    async with api_factory() as session:
        row = await session.get(User, made.id)
        assert row is not None
        row.is_demo = True
        await session.commit()
    async with api_transport(api_app) as http:
        yield await login(http, DEMO_EMAIL, DEMO_PASSWORD)


async def write_copy(
    factory: SessionFactory,
    settings: Settings,
    episode_id: int,
    *,
    state: OfflineCopyState = OfflineCopyState.READY,
    body: bytes = COPY_BYTES,
    last_served_at: datetime | None = None,
) -> Path:
    """A finished copy on disk and its row, as the encode leaves them."""
    path = offline_path_for(settings, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    info = path.stat()
    async with factory() as session:
        session.add(
            OfflineCopy(
                episode_id=episode_id,
                state=state,
                size=info.st_size,
                etag=stat_etag(info.st_size, info.st_mtime_ns),
                codec="h264",
                height=720,
                crf=26,
                audio_bitrate="96k",
                ready_at=datetime.now(UTC),
                last_served_at=last_served_at,
            )
        )
        await session.commit()
    return path


async def add_source(factory: SessionFactory, settings: Settings, episode_id: int) -> Path:
    source = settings.downloads_dir / str(episode_id) / "[Group] Show - 01 [1080p].mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"a source")
    async with factory() as session:
        session.add(
            MediaFile(
                episode_id=episode_id,
                path=str(source),
                size=source.stat().st_size,
                review_state=ReviewState.AUTO,
            )
        )
        await session.commit()
    return source


@pytest.fixture
async def episode_id(api_factory: SessionFactory, settings: Settings) -> int:
    found = await add_episode(api_factory, anilist_id=941001)
    write_rendition(settings, found)
    await write_copy(api_factory, settings, found)
    return found


def copy_path(episode_id: int) -> str:
    return f"/media/{episode_id}/offline.mp4"


# --- The media route ------------------------------------------------------------


async def test_the_copy_is_served_whole_with_its_headers(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    response = await client.get(copy_path(episode_id))

    assert response.status_code == 200
    assert response.content == COPY_BYTES
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-length"] == str(len(COPY_BYTES))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["content-disposition"].startswith('attachment; filename="')
    assert "Stream Show - 01.mp4" in response.headers["content-disposition"]
    info = offline_path_for(settings, episode_id).stat()
    # The ETag is the very string the encode stored on the row.
    assert response.headers["etag"] == stat_etag(info.st_size, info.st_mtime_ns)


async def test_an_anonymous_request_is_401(anon: AsyncClient, episode_id: int) -> None:
    response = await anon.get(copy_path(episode_id))

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


async def test_the_demo_account_is_refused_before_any_lookup(
    demo: AsyncClient, episode_id: int
) -> None:
    for path in (copy_path(episode_id), copy_path(99_999_999)):
        response = await demo.get(path)
        assert response.status_code == 403
        assert response.json() == {"detail": "downloads are turned off for the demo account"}
    assert (await demo.head(copy_path(episode_id))).status_code == 403


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [("bytes=0-99", 0, 99), ("bytes=4000-", 4000, None), ("bytes=-50", None, None)],
)
async def test_a_range_is_exactly_that_range(
    client: AsyncClient, episode_id: int, header: str, first: int | None, last: int | None
) -> None:
    total = len(COPY_BYTES)
    start = total - 50 if first is None else first
    end = total - 1 if last is None else last

    response = await client.get(copy_path(episode_id), headers={"Range": header})

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes {start}-{end}/{total}"
    assert response.content == COPY_BYTES[start : end + 1]


async def test_an_unsatisfiable_range_is_a_json_416(client: AsyncClient, episode_id: int) -> None:
    total = len(COPY_BYTES)

    response = await client.get(copy_path(episode_id), headers={"Range": f"bytes={total}-"})

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{total}"
    assert response.json() == {"detail": "that range is not inside the file"}
    assert response.headers["etag"]


@pytest.mark.parametrize("header", ["kilograms=0-9", "bytes=a-b"])
async def test_a_malformed_range_is_a_json_400(
    client: AsyncClient, episode_id: int, header: str
) -> None:
    response = await client.get(copy_path(episode_id), headers={"Range": header})

    assert response.status_code == 400
    assert response.json() == {"detail": "range header is malformed"}


async def test_a_matching_if_none_match_is_304(client: AsyncClient, episode_id: int) -> None:
    etag = (await client.head(copy_path(episode_id))).headers["etag"]

    response = await client.get(copy_path(episode_id), headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == "private, no-cache"
    assert "content-disposition" not in response.headers


async def test_if_range_with_the_current_etag_honours_the_range(
    client: AsyncClient, episode_id: int
) -> None:
    etag = (await client.head(copy_path(episode_id))).headers["etag"]

    response = await client.get(
        copy_path(episode_id), headers={"Range": "bytes=100-199", "If-Range": etag}
    )

    assert response.status_code == 206
    assert response.content == COPY_BYTES[100:200]


async def test_an_if_range_mismatch_answers_200_with_the_whole_file(
    client: AsyncClient, episode_id: int
) -> None:
    """A stale validator means "my copy is old": all of the new one, not a slice."""
    response = await client.get(
        copy_path(episode_id), headers={"Range": "bytes=100-199", "If-Range": '"deadbeef-1"'}
    )

    assert response.status_code == 200
    assert response.content == COPY_BYTES
    assert "content-range" not in response.headers


async def test_a_head_reports_the_length_without_a_body(
    client: AsyncClient, episode_id: int
) -> None:
    response = await client.head(copy_path(episode_id))

    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(COPY_BYTES))
    assert response.content == b""


async def test_an_unknown_episode_is_404(client: AsyncClient) -> None:
    response = await client.get(copy_path(99_999_999))

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


async def test_an_episode_that_is_not_ready_is_404_even_with_a_copy(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    """Today a copy is any user's only on a ready episode (a trip's are T4's)."""
    matched = await add_episode(api_factory, anilist_id=941002, state=EpisodeState.MATCHED)
    await write_copy(api_factory, settings, matched)

    assert (await client.get(copy_path(matched))).status_code == 404


@pytest.mark.parametrize(
    "state", [OfflineCopyState.QUEUED, OfflineCopyState.PREPARING, OfflineCopyState.FAILED]
)
async def test_a_copy_that_is_not_ready_is_404(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, state: OfflineCopyState
) -> None:
    """A file at the name is not a copy until the row says it is."""
    ready = await add_episode(api_factory, anilist_id=941010 + list(OfflineCopyState).index(state))
    await write_copy(api_factory, settings, ready, state=state)

    assert (await client.get(copy_path(ready))).status_code == 404


async def test_a_ready_episode_with_no_copy_is_404(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    ready = await add_episode(api_factory, anilist_id=941003)
    write_rendition(settings, ready)

    assert (await client.get(copy_path(ready))).status_code == 404


async def test_a_ready_row_whose_file_has_gone_is_404(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    offline_path_for(settings, episode_id).unlink()

    response = await client.get(copy_path(episode_id))

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


async def test_a_copy_that_is_a_symlink_is_refused(
    client: AsyncClient, episode_id: int, settings: Settings
) -> None:
    path = offline_path_for(settings, episode_id)
    outside = settings.data_dir.parent / "outside-copy.txt"
    outside.write_text("root:x:0:0:root:/root:/bin/sh\n")
    path.unlink()
    path.symlink_to(outside)

    response = await client.get(copy_path(episode_id))

    assert response.status_code == 404
    assert "root:x" not in response.text


async def test_a_symlink_to_another_copy_inside_data_dir_is_refused_too(
    client: AsyncClient, api_factory: SessionFactory, episode_id: int, settings: Settings
) -> None:
    other = await add_episode(api_factory, anilist_id=941004)
    await write_copy(api_factory, settings, other, body=b"another copy")
    path = offline_path_for(settings, other)
    path.unlink()
    path.symlink_to(offline_path_for(settings, episode_id))

    assert (await client.get(copy_path(other))).status_code == 404


async def test_a_name_with_a_suffix_is_not_the_copy(client: AsyncClient, episode_id: int) -> None:
    # (``offline.mp4%0A`` is not here: Starlette compiles a literal route with
    # ``$``, which also matches before a trailing newline, so it reaches this
    # same route and this same, authorised file — as ``episode.mp4%0A`` does.)
    for name in ("offline.mp4.bak", "offline.MP4", "offline.mp"):
        response = await client.get(f"/media/{episode_id}/{name}")
        assert response.status_code in (404, 422)
        assert response.content != COPY_BYTES


async def test_last_served_at_is_touched_at_most_hourly(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    fresh = await add_episode(api_factory, anilist_id=941005)
    await write_copy(api_factory, settings, fresh)

    assert (await client.get(copy_path(fresh))).status_code == 200
    async with api_factory() as session:
        row = await session.get(OfflineCopy, fresh)
        assert row is not None and row.last_served_at is not None
        first = row.last_served_at

    assert (await client.get(copy_path(fresh), headers={"Range": "bytes=0-9"})).status_code == 206
    async with api_factory() as session:
        row = await session.get(OfflineCopy, fresh)
        assert row is not None and row.last_served_at == first, "not again within the hour"

    # An hour later it moves; a 304 and a HEAD never move it.
    async with api_factory() as session:
        row = await session.get(OfflineCopy, fresh)
        assert row is not None
        row.last_served_at = first - timedelta(hours=2)
        await session.commit()
    etag = (await client.head(copy_path(fresh))).headers["etag"]
    assert (await client.get(copy_path(fresh), headers={"If-None-Match": etag})).status_code == 304
    async with api_factory() as session:
        row = await session.get(OfflineCopy, fresh)
        assert row is not None and row.last_served_at == first - timedelta(hours=2)
    assert (await client.get(copy_path(fresh))).status_code == 200
    async with api_factory() as session:
        row = await session.get(OfflineCopy, fresh)
        assert row is not None and row.last_served_at is not None
        assert row.last_served_at > first - timedelta(minutes=1)


# --- The endpoints --------------------------------------------------------------


def api_path(episode_id: int) -> str:
    return f"/api/episodes/{episode_id}/offline"


async def test_post_queues_a_copy_and_answers_202(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    ready = await add_episode(api_factory, anilist_id=941101)
    await add_source(api_factory, settings, ready)

    response = await client.post(api_path(ready))

    assert response.status_code == 202, response.text
    assert response.json() == {
        "state": "queued",
        "progress": 0.0,
        "size": None,
        "url": None,
        "codecs": "avc1.640028",
    }
    again = await client.post(api_path(ready))
    assert again.status_code == 202
    async with api_factory() as session:
        jobs = (await session.scalars(select(Job).where(Job.type == OFFLINE_ENCODE))).all()
        assert len(jobs) == 1, "a second request dedupes onto the first"
        assert jobs[0].payload["episode_id"] == ready


async def test_post_on_an_available_copy_answers_its_url(
    client: AsyncClient, episode_id: int
) -> None:
    response = await client.post(api_path(episode_id))

    assert response.status_code == 202
    assert response.json() == {
        "state": "available",
        "progress": None,
        "size": len(COPY_BYTES),
        "url": f"/media/{episode_id}/offline.mp4",
        "codecs": "avc1.640028",
    }


async def test_post_without_a_source_is_409_source_gone(
    client: AsyncClient, api_factory: SessionFactory
) -> None:
    ready = await add_episode(api_factory, anilist_id=941102)

    response = await client.post(api_path(ready))

    assert response.status_code == 409
    assert response.json() == {"detail": "source_gone"}
    get = await client.get(api_path(ready))
    assert get.status_code == 200
    assert get.json()["state"] == "unavailable"
    assert get.json()["codecs"] is None


async def test_post_and_get_refuse_unknown_and_not_ready_episodes(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    matched = await add_episode(api_factory, anilist_id=941103, state=EpisodeState.MATCHED)
    await add_source(api_factory, settings, matched)

    for target in (matched, 99_999_999):
        assert (await client.post(api_path(target))).status_code == 404
        assert (await client.get(api_path(target))).status_code == 404


async def test_post_and_get_refuse_the_demo_account(demo: AsyncClient, episode_id: int) -> None:
    for response in (await demo.post(api_path(episode_id)), await demo.get(api_path(episode_id))):
        assert response.status_code == 403
        assert response.json() == {"detail": "downloads are turned off for the demo account"}


async def test_anonymous_callers_are_401(anon: AsyncClient, episode_id: int) -> None:
    assert (await anon.get(api_path(episode_id))).status_code == 401
    assert (await anon.post(api_path(episode_id))).status_code in (401, 403)


async def test_get_reports_the_progress_of_a_copy_being_made(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    ready = await add_episode(api_factory, anilist_id=941104)
    await add_source(api_factory, settings, ready)
    async with api_factory() as session:
        session.add(OfflineCopy(episode_id=ready, state=OfflineCopyState.PREPARING, codec="h264"))
        session.add(
            Job(
                type=OFFLINE_ENCODE,
                payload={"episode_id": ready, "why": "request", "progress": 0.42},
                status=JobStatus.RUNNING,
            )
        )
        await session.commit()

    response = await client.get(api_path(ready))

    assert response.status_code == 200
    assert response.json() == {
        "state": "preparing",
        "progress": 0.42,
        "size": None,
        "url": None,
        "codecs": "avc1.640028",
    }


async def test_get_says_none_for_a_ready_episode_with_a_source_and_no_copy(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    ready = await add_episode(api_factory, anilist_id=941105)
    await add_source(api_factory, settings, ready)

    body = (await client.get(api_path(ready))).json()

    assert body["state"] == "none"
    assert body["codecs"] == "avc1.640028"


# --- EpisodeOut.offline ---------------------------------------------------------


async def test_the_show_page_carries_each_episodes_copy(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    """Batched with the renditions: one show page, every row's copy, no N+1."""
    ready = await add_episode(api_factory, anilist_id=941201)
    write_rendition(settings, ready)
    await write_copy(api_factory, settings, ready)
    async with api_factory() as session:
        episode = await session.get(Episode, ready)
        assert episode is not None
        anime_id = episode.anime_id
        # A second, not-ready episode of the same show: no copy to offer.
        session.add(Episode(anime_id=anime_id, number=2, state=EpisodeState.DOWNLOADING))
        await session.commit()

    response = await client.get(f"/api/anime/{anime_id}")

    assert response.status_code == 200, response.text
    rows = {row["number"]: row for row in response.json()["episodes"]}
    assert rows[1]["offline"] == {
        "state": "available",
        "progress": None,
        "size": len(COPY_BYTES),
        "url": f"/media/{ready}/offline.mp4",
        "codecs": "avc1.640028",
    }
    assert rows[2]["offline"] is None


async def test_the_player_carries_the_copy_too(client: AsyncClient, episode_id: int) -> None:
    response = await client.get(f"/api/episodes/{episode_id}/play")

    assert response.status_code == 200
    assert response.json()["episode"]["offline"]["state"] == "available"


async def test_a_ready_episode_with_no_source_and_no_copy_reads_unavailable(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    ready = await add_episode(api_factory, anilist_id=941202)
    write_rendition(settings, ready)

    response = await client.get(f"/api/episodes/{ready}/play")

    assert response.json()["episode"]["offline"] == {
        "state": "unavailable",
        "progress": None,
        "size": None,
        "url": None,
        "codecs": None,
    }


def test_the_file_name_is_derived_from_the_id(settings: Settings) -> None:
    assert offline_path_for(settings, 42) == settings.offline_dir / "42.mp4"
    assert os.path.dirname(offline_path_for(settings, 42)) == str(settings.offline_dir)
