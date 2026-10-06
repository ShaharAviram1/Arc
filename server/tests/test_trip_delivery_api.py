"""The device's trip routes over HTTP, the copy's authorisation, ``/play`` (M19 T4).

``POST``/``DELETE /api/trips/{id}/episodes/{eid}/delivered``, ``POST
…/again``, ``GET /media/{id}/offline.mp4`` for a trip-only episode and
``GET /api/episodes/{id}/play`` answering ``offline_only``. Asserted on status
codes and JSON: this is the contract the device (M19 T6) is written against.
"""

from __future__ import annotations

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
    Anime,
    Episode,
    EpisodeState,
    Job,
    OfflineCopy,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    WatchProgress,
)
from arc.services.trips.create import TRIP_NOT_ACTIVE
from arc.services.trips.names import OFFLINE_SETTLE
from tests.conftest import add_user, api_transport, login
from tests.test_media_stream import write_rendition
from tests.test_offline_copy_routes import COPY_BYTES, write_copy

pytestmark = pytest.mark.pg

OWNER_EMAIL = "trip-owner@arc.test"
OWNER_PASSWORD = "trip-owner-password"
OTHER_EMAIL = "trip-other@arc.test"
OTHER_PASSWORD = "trip-other-password"
DEMO_EMAIL = "trip-demo@arc.test"
DEMO_PASSWORD = "trip-demo-password"


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        env="test", database_url=test_database_url, data_dir=tmp_path, _env_file=None
    )


@pytest.fixture
async def owner(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, OWNER_EMAIL, OWNER_PASSWORD)


@pytest.fixture
async def owner_client(api_app: FastAPI, owner: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, OWNER_EMAIL, OWNER_PASSWORD)


@pytest.fixture
async def other_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, OTHER_EMAIL, OTHER_PASSWORD)
    async with api_transport(api_app) as http:
        yield await login(http, OTHER_EMAIL, OTHER_PASSWORD)


@pytest.fixture
async def demo_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    made = await add_user(api_factory, DEMO_EMAIL, DEMO_PASSWORD)
    async with api_factory() as session:
        row = await session.get(User, made.id)
        assert row is not None
        row.is_demo = True
        await session.commit()
    async with api_transport(api_app) as http:
        yield await login(http, DEMO_EMAIL, DEMO_PASSWORD)


async def a_trip(
    factory: SessionFactory,
    settings: Settings,
    user: User,
    *,
    anilist_id: int,
    episodes: int = 3,
) -> tuple[int, list[int]]:
    """A show of ``episodes`` and the user's active trip on 2..last, each trip-only.

    Episode 1 is ``ready`` (with a rendition), the rest ``not_wanted`` with a
    copy waiting, as T3 leaves them. Returns the trip id and the episode ids.
    """
    now = datetime.now(UTC)
    async with factory() as session:
        anime = Anime(
            anilist_id=anilist_id,
            summary_source="anilist",
            detail_source="anilist",
            title_romaji="Trip Show",
            format="TV",
            status="FINISHED",
            episodes=episodes,
        )
        session.add(anime)
        await session.flush()
        rows = [
            Episode(
                anime_id=anime.id,
                number=number,
                state=EpisodeState.READY if number == 1 else EpisodeState.NOT_WANTED,
                air_at=now - timedelta(days=30 - number),
            )
            for number in range(1, episodes + 1)
        ]
        session.add_all(rows)
        await session.flush()
        trip = Trip(
            user_id=user.id,
            anime_id=anime.id,
            first_number=2,
            last_number=episodes,
            count=episodes - 1,
            deadline_at=now + timedelta(days=14),
        )
        session.add(trip)
        await session.flush()
        for episode in rows[1:]:
            session.add(TripEpisode(trip_id=trip.id, episode_id=episode.id, available_at=now))
        await session.commit()
        ids = [episode.id for episode in rows]
        trip_id = trip.id
    write_rendition(settings, ids[0])
    for episode_id in ids[1:]:
        await write_copy(factory, settings, episode_id)
    return trip_id, ids


async def row_state(factory: SessionFactory, trip_id: int, episode_id: int) -> TripEpisode:
    async with factory() as session:
        row = await session.get(TripEpisode, (trip_id, episode_id))
        assert row is not None
        return row


def delivered_url(trip_id: int, episode_id: int) -> str:
    return f"/api/trips/{trip_id}/episodes/{episode_id}/delivered"


# --- Delivery routes --------------------------------------------------------


async def test_anonymous_callers_get_401(api_client: AsyncClient) -> None:
    assert (await api_client.post(delivered_url(1, 1), json={})).status_code == 401
    assert (await api_client.delete(delivered_url(1, 1))).status_code == 401
    assert (await api_client.post("/api/trips/1/episodes/1/again")).status_code == 401


async def test_a_confirmation_is_204_and_idempotent(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986001)

    first = await owner_client.post(delivered_url(trip_id, ids[1]), json={"etag": "anything"})
    assert first.status_code == 204, first.text
    row = await row_state(api_factory, trip_id, ids[1])
    assert row.state is TripEpisodeState.DELIVERED and row.delivered_at is not None
    stamped = row.delivered_at

    second = await owner_client.post(delivered_url(trip_id, ids[1]), json={})
    assert second.status_code == 204
    assert (await row_state(api_factory, trip_id, ids[1])).delivered_at == stamped
    async with api_factory() as session:
        settles = (await session.scalars(select(Job).where(Job.type == OFFLINE_SETTLE))).all()
    assert len(settles) == 1, "one settle, however often it is confirmed"

    current = (await owner_client.get("/api/trips/current")).json()
    phases = {
        row["episode_id"]: (row["phase"], row["delivered"], row["url"])
        for row in current["episodes"]
    }
    assert phases[ids[1]] == ("delivered", True, f"/media/{ids[1]}/offline.mp4")
    assert phases[ids[2]] == ("available", False, f"/media/{ids[2]}/offline.mp4")


async def test_the_body_is_checked(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986002)
    bad = await owner_client.post(delivered_url(trip_id, ids[1]), json={"etag": 5})
    assert bad.status_code == 422
    extra = await owner_client.post(delivered_url(trip_id, ids[1]), json={"size": 5})
    assert extra.status_code == 422


async def test_someone_elses_trip_is_404(
    owner_client: AsyncClient,
    other_client: AsyncClient,
    owner: User,
    api_factory: SessionFactory,
    settings: Settings,
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986003)

    for response in (
        await other_client.post(delivered_url(trip_id, ids[1]), json={}),
        await other_client.delete(delivered_url(trip_id, ids[1])),
        await other_client.post(f"/api/trips/{trip_id}/episodes/{ids[1]}/again"),
        # The owner, but an episode the trip does not hold, and an unknown trip.
        await owner_client.post(delivered_url(trip_id, ids[0]), json={}),
        await owner_client.post(delivered_url(99_999_999, ids[1]), json={}),
    ):
        assert response.status_code == 404
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.PENDING


async def test_the_device_deleting_its_copy_is_recorded(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986004)
    await owner_client.post(delivered_url(trip_id, ids[1]), json={})

    response = await owner_client.delete(delivered_url(trip_id, ids[1]))

    assert response.status_code == 204
    row = await row_state(api_factory, trip_id, ids[1])
    assert row.released_at is not None and row.state is TripEpisodeState.DELIVERED
    stamped = row.released_at
    assert (await owner_client.delete(delivered_url(trip_id, ids[1]))).status_code == 204
    assert (await row_state(api_factory, trip_id, ids[1])).released_at == stamped


async def test_again_re_pends_and_answers_the_trip(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986005)
    await owner_client.post(delivered_url(trip_id, ids[1]), json={})

    response = await owner_client.post(f"/api/trips/{trip_id}/episodes/{ids[1]}/again")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == trip_id
    by_id = {row["episode_id"]: row for row in body["episodes"]}
    assert by_id[ids[1]]["phase"] == "available" and by_id[ids[1]]["delivered"] is False
    row = await row_state(api_factory, trip_id, ids[1])
    assert row.state is TripEpisodeState.PENDING and row.delivered_at is None


async def test_again_on_an_ended_trip_is_409(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986006)
    async with api_factory() as session:
        trip = await session.get(Trip, trip_id)
        assert trip is not None
        trip.state = TripState.FINISHED
        trip.ended_at = datetime.now(UTC)
        await session.commit()

    response = await owner_client.post(f"/api/trips/{trip_id}/episodes/{ids[1]}/again")

    assert (response.status_code, response.json()["detail"]) == (409, TRIP_NOT_ACTIVE)
    assert (await owner_client.get("/api/trips/current")).json() is None, "current = active only"


# --- The copy's authorisation -----------------------------------------------


async def test_the_owner_fetches_a_trip_copy_and_nobody_else_does(
    owner_client: AsyncClient,
    other_client: AsyncClient,
    demo_client: AsyncClient,
    owner: User,
    api_factory: SessionFactory,
    settings: Settings,
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986007)
    url = f"/media/{ids[1]}/offline.mp4"

    pending = await owner_client.get(url)
    assert pending.status_code == 200 and pending.content == COPY_BYTES
    assert (await other_client.get(url)).status_code == 404, "404, never 403"
    assert (await other_client.head(url)).status_code == 404
    assert (await demo_client.get(url)).status_code == 403, "demo: refused before any lookup"

    await owner_client.post(delivered_url(trip_id, ids[1]), json={})
    assert (await owner_client.get(url)).status_code == 200, "a second device, within the hour"

    async with api_factory() as session:
        row = await session.get(TripEpisode, (trip_id, ids[1]))
        assert row is not None
        row.state = TripEpisodeState.EXPIRED
        await session.commit()
    assert (await owner_client.get(url)).status_code == 404, "expired"

    # A ready episode's copy: any account.
    await write_copy(api_factory, settings, ids[0])
    assert (await other_client.get(f"/media/{ids[0]}/offline.mp4")).status_code == 200


# --- /play ------------------------------------------------------------------


async def test_play_answers_a_trip_episode_offline_only(
    owner_client: AsyncClient,
    other_client: AsyncClient,
    owner: User,
    api_factory: SessionFactory,
    settings: Settings,
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986008)
    async with api_factory() as session:
        session.add(
            WatchProgress(user_id=owner.id, episode_id=ids[1], position_s=300.0, duration_s=1420.0)
        )
        await session.commit()

    response = await owner_client.get(f"/api/episodes/{ids[1]}/play")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["playlist_url"] is None
    assert body["offline_only"] is True
    assert body["resume_position"] == 300.0
    assert body["duration"] == 1420.0
    assert body["episode"]["id"] == ids[1]
    assert body["previous"]["id"] == ids[0] and body["next"]["id"] == ids[2]

    assert (await other_client.get(f"/api/episodes/{ids[1]}/play")).status_code == 404

    await owner_client.post(delivered_url(trip_id, ids[1]), json={})
    assert (await owner_client.get(f"/api/episodes/{ids[1]}/play")).json()["offline_only"]

    async with api_factory() as session:
        row = await session.get(TripEpisode, (trip_id, ids[1]))
        assert row is not None
        row.state = TripEpisodeState.EXPIRED
        await session.commit()
    assert (await owner_client.get(f"/api/episodes/{ids[1]}/play")).status_code == 404


async def test_play_of_a_ready_episode_is_unchanged(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    _, ids = await a_trip(api_factory, settings, owner, anilist_id=986009)

    body = (await owner_client.get(f"/api/episodes/{ids[0]}/play")).json()

    assert body["playlist_url"] == f"/media/{ids[0]}/index.m3u8"
    assert body["offline_only"] is False


async def test_a_late_confirmation_does_not_reopen_the_copy(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986010)
    async with api_factory() as session:
        cancelled = await session.get(TripEpisode, (trip_id, ids[1]))
        expired = await session.get(TripEpisode, (trip_id, ids[2]))
        trip = await session.get(Trip, trip_id)
        assert cancelled is not None and expired is not None and trip is not None
        cancelled.state = TripEpisodeState.CANCELLED
        expired.state = TripEpisodeState.EXPIRED
        trip.state = TripState.CANCELLED
        trip.ended_at = datetime.now(UTC)
        await session.commit()

    for episode_id in ids[1:]:
        confirmed = await owner_client.post(delivered_url(trip_id, episode_id), json={})
        assert confirmed.status_code == 204
        assert (await owner_client.get(f"/media/{episode_id}/offline.mp4")).status_code == 404
        row = await row_state(api_factory, trip_id, episode_id)
        assert row.state in (TripEpisodeState.CANCELLED, TripEpisodeState.EXPIRED)
        assert row.delivered_at is not None


async def test_the_url_is_null_without_a_ready_copy(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=986011)
    async with api_factory() as session:
        copy = await session.get(OfflineCopy, ids[1])
        assert copy is not None
        await session.delete(copy)
        await session.commit()

    current = (await owner_client.get("/api/trips/current")).json()
    by_id = {row["episode_id"]: row for row in current["episodes"]}
    assert by_id[ids[1]]["url"] is None
    assert by_id[ids[2]]["url"] == f"/media/{ids[2]}/offline.mp4"
