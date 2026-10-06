"""The trip routes and the show page's trip fields (FR-A12, M19 T3).

Asserted on the JSON: the shape is the contract the device is written against.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.db import SessionFactory
from arc.models import Anime, Episode, Setting, User, Want
from arc.services.acquisition import compute_wants
from arc.services.trips.create import (
    COUNT_OUT_OF_RANGE,
    DEMO_REFUSED,
    NOTHING_AIRED,
    TRIP_ACTIVE,
    TRIP_NOT_FOUND,
)
from tests.acquisition_helpers import make_anime, make_entry, make_episodes
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

EMAIL = "traveller@arc.test"
PASSWORD = "travellerpassword"
OTHER_EMAIL = "stayer@arc.test"
OTHER_PASSWORD = "stayerpassword"


@pytest.fixture
async def user_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, EMAIL, PASSWORD)
    # No storage floor: whatever this machine's disk holds, a trip is allowed.
    async with api_factory() as session:
        row = await session.get(Setting, "min_free_gb")
        assert row is not None
        row.value = 0
        await session.commit()
    async with api_transport(api_app) as client:
        yield await login(client, EMAIL, PASSWORD)


async def a_show(factory: SessionFactory, *, anilist_id: int, aired: int = 12) -> int:
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id)
        await make_episodes(session, anime, 12, aired_through=aired)
        await session.commit()
        return anime.id


async def user_id(factory: SessionFactory, email: str = EMAIL) -> int:
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        return user.id


async def test_anonymous_callers_get_401(api_client: AsyncClient) -> None:
    assert (await api_client.post("/api/anime/1/trip", json={"count": 2})).status_code == 401
    assert (await api_client.get("/api/trips/current")).status_code == 401
    assert (await api_client.delete("/api/trips/1")).status_code == 401


async def test_a_trip_is_created_and_read_back(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=981001, aired=6)

    response = await user_client.post(f"/api/anime/{anime_id}/trip", json={"count": 10})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["anime_id"] == anime_id
    assert body["anime_title"]
    assert (body["first_number"], body["last_number"], body["count"]) == (1, 6, 6)
    assert body["state"] == "active"
    assert body["created_at"] and body["deadline_at"]
    assert [row["number"] for row in body["episodes"]] == [1, 2, 3, 4, 5, 6]
    assert {row["phase"] for row in body["episodes"]} == {"searching"}
    assert set(body["episodes"][0]) == {
        "episode_id",
        "number",
        "phase",
        "progress",
        "size",
        "delivered",
        "url",
    }
    assert body["episodes"][0]["delivered"] is False
    assert body["episodes"][0]["url"] is None, "no copy yet"

    current = await user_client.get("/api/trips/current")
    assert current.status_code == 200
    assert current.json() == body


async def test_no_trip_reads_null(user_client: AsyncClient) -> None:
    response = await user_client.get("/api/trips/current")
    assert response.status_code == 200
    assert response.json() is None


async def test_the_refusals(user_client: AsyncClient, api_factory: SessionFactory) -> None:
    anime_id = await a_show(api_factory, anilist_id=981002, aired=3)

    bad = await user_client.post(f"/api/anime/{anime_id}/trip", json={"count": 0})
    assert (bad.status_code, bad.json()["detail"]) == (422, COUNT_OUT_OF_RANGE)
    too_many = await user_client.post(f"/api/anime/{anime_id}/trip", json={"count": 51})
    assert (too_many.status_code, too_many.json()["detail"]) == (422, COUNT_OUT_OF_RANGE)
    assert (
        await user_client.post(f"/api/anime/{anime_id}/trip", json={"count": "2"})
    ).status_code == 422
    assert (await user_client.post("/api/anime/999999/trip", json={"count": 2})).status_code == 404

    async with api_factory() as session:
        anime_user = await user_id(api_factory)
        anime = await session.get(Anime, anime_id)
        user = await session.get(User, anime_user)
        assert anime is not None and user is not None
        await make_entry(session, user, anime, progress=3)
        await session.commit()
    caught_up = await user_client.post(f"/api/anime/{anime_id}/trip", json={"count": 2})
    assert (caught_up.status_code, caught_up.json()["detail"]) == (422, NOTHING_AIRED)

    other = await a_show(api_factory, anilist_id=981003)
    assert (
        await user_client.post(f"/api/anime/{other}/trip", json={"count": 2})
    ).status_code == 201
    again = await user_client.post(f"/api/anime/{other}/trip", json={"count": 2})
    assert (again.status_code, again.json()["detail"]) == (409, TRIP_ACTIVE)


async def test_the_demo_account_is_refused(
    api_app: FastAPI, api_factory: SessionFactory, user_client: AsyncClient
) -> None:
    anime_id = await a_show(api_factory, anilist_id=981004)
    await add_user(api_factory, "demo@arc.test", "demopassword123")
    async with api_factory() as session:
        demo = await session.scalar(select(User).where(User.email == "demo@arc.test"))
        assert demo is not None
        demo.is_demo = True
        await session.commit()
    async with api_transport(api_app) as client:
        await login(client, "demo@arc.test", "demopassword123")
        response = await client.post(f"/api/anime/{anime_id}/trip", json={"count": 2})
    assert (response.status_code, response.json()["detail"]) == (403, DEMO_REFUSED)


async def test_only_the_owner_can_cancel_and_others_see_404(
    api_app: FastAPI, api_factory: SessionFactory, user_client: AsyncClient
) -> None:
    anime_id = await a_show(api_factory, anilist_id=981005)
    trip = (await user_client.post(f"/api/anime/{anime_id}/trip", json={"count": 2})).json()
    await add_user(api_factory, OTHER_EMAIL, OTHER_PASSWORD)
    async with api_transport(api_app) as other:
        await login(other, OTHER_EMAIL, OTHER_PASSWORD)
        refused = await other.delete(f"/api/trips/{trip['id']}")
        assert (refused.status_code, refused.json()["detail"]) == (404, TRIP_NOT_FOUND)
        assert (await other.get("/api/trips/current")).json() is None

    assert (await user_client.delete(f"/api/trips/{trip['id']}")).status_code == 204
    assert (await user_client.get("/api/trips/current")).json() is None
    assert (await user_client.delete(f"/api/trips/{trip['id']}")).status_code == 204
    assert (await user_client.delete("/api/trips/999999")).status_code == 404
    async with api_factory() as session:
        await compute_wants(session)
        await session.commit()
        wants = (await session.scalars(select(Want))).all()
        assert wants and all(want.dropped_at is not None for want in wants), (
            "the queued reconciliation ends the trip's wants"
        )


async def test_the_show_page_carries_the_trip_and_trip_only(
    api_app: FastAPI, api_factory: SessionFactory, user_client: AsyncClient
) -> None:
    anime_id = await a_show(api_factory, anilist_id=981006)
    before = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert before["trip"] is None
    assert all(row["trip_only"] is False for row in before["episodes"])

    posted = (await user_client.post(f"/api/anime/{anime_id}/trip", json={"count": 2})).json()
    async with api_factory() as session:
        await compute_wants(session)
        await session.commit()
        first_two = set(
            (
                await session.scalars(
                    select(Episode.id).where(Episode.anime_id == anime_id, Episode.number <= 2)
                )
            ).all()
        )

    after = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert after["trip"]["id"] == posted["id"]
    assert {row["id"] for row in after["episodes"] if row["trip_only"]} == first_two

    # Every caller sees the flag; only the traveller sees the trip.
    await add_user(api_factory, OTHER_EMAIL, OTHER_PASSWORD)
    async with api_transport(api_app) as other:
        await login(other, OTHER_EMAIL, OTHER_PASSWORD)
        theirs = (await other.get(f"/api/anime/{anime_id}")).json()
    assert theirs["trip"] is None
    assert {row["id"] for row in theirs["episodes"] if row["trip_only"]} == first_two


async def test_the_show_page_carries_the_trip_cap(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=981090)

    body = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert body["trip_limits"] == {"max_episodes": 50}

    async with api_factory() as session:
        await session.merge(Setting(key="trip_max_episodes", value=7))
        await session.commit()
    body = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert body["trip_limits"] == {"max_episodes": 7}
