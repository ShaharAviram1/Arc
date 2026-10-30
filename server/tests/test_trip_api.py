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
from arc.services.trips.names import FALLBACK_COPY_BYTES
from tests.acquisition_helpers import make_anime, make_entry, make_episodes
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

EMAIL = "traveller@arc.test"
PASSWORD = "travellerpassword"
OTHER_EMAIL = "stayer@arc.test"
OTHER_PASSWORD = "stayerpassword"
#: The device that starts every trip here (owner, 2026-10-30: a trip is its
#: device's), and another device of the same user.
DEVICE = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
OTHER_DEVICE = "0b9a8c7d-6e5f-4a3b-9c2d-1e0f2a3b4c5d"


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
    assert (
        await api_client.post("/api/anime/1/trip", json={"count": 2, "device_id": DEVICE})
    ).status_code == 401
    assert (await api_client.get("/api/trips")).status_code == 401
    assert (await api_client.delete("/api/trips/1")).status_code == 401


async def test_a_trip_is_created_and_read_back(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=981001, aired=6)

    response = await user_client.post(
        f"/api/anime/{anime_id}/trip", json={"count": 10, "device_id": DEVICE}
    )

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
        "sha256",
        "episode_id",
        "number",
        "phase",
        "progress",
        "size",
        "delivered",
        "url",
        "etag",
    }
    assert body["device_id"] == DEVICE
    assert body["episodes"][0]["delivered"] is False
    assert body["episodes"][0]["url"] is None, "no copy yet"
    assert body["episodes"][0]["etag"] is None
    assert "copy_estimate_bytes" not in body, "the estimate rides on trip_limits only"

    listed = await user_client.get("/api/trips")
    assert listed.status_code == 200
    assert listed.json() == [body]


async def test_no_trip_reads_an_empty_list(user_client: AsyncClient) -> None:
    response = await user_client.get("/api/trips")
    assert response.status_code == 200
    assert response.json() == []


async def test_trips_on_several_shows_are_listed_oldest_first(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Any number of active trips, one per show (owner, 2026-10-28)."""
    first = await a_show(api_factory, anilist_id=981010)
    second = await a_show(api_factory, anilist_id=981011)
    third = await a_show(api_factory, anilist_id=981012)

    made = []
    for anime_id in (first, second, third):
        response = await user_client.post(
            f"/api/anime/{anime_id}/trip", json={"count": 2, "device_id": DEVICE}
        )
        assert response.status_code == 201, response.text
        made.append(response.json()["id"])
    same = await user_client.post(
        f"/api/anime/{second}/trip", json={"count": 2, "device_id": DEVICE}
    )
    assert (same.status_code, same.json()["detail"]) == (409, TRIP_ACTIVE)

    listed = (await user_client.get("/api/trips")).json()
    assert [trip["id"] for trip in listed] == made
    assert [trip["anime_id"] for trip in listed] == [first, second, third]

    assert (await user_client.delete(f"/api/trips/{made[1]}")).status_code == 204
    listed = (await user_client.get("/api/trips")).json()
    assert [trip["id"] for trip in listed] == [made[0], made[2]]
    again = await user_client.post(
        f"/api/anime/{second}/trip", json={"count": 2, "device_id": DEVICE}
    )
    assert again.status_code == 201, "the show's trip ended, so it may have another"


async def test_the_show_page_carries_its_own_trip_and_the_estimate(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    first = await a_show(api_factory, anilist_id=981013)
    second = await a_show(api_factory, anilist_id=981014)
    third = await a_show(api_factory, anilist_id=981015)
    one = (
        await user_client.post(f"/api/anime/{first}/trip", json={"count": 2, "device_id": DEVICE})
    ).json()
    two = (
        await user_client.post(f"/api/anime/{second}/trip", json={"count": 3, "device_id": DEVICE})
    ).json()

    page_one = (await user_client.get(f"/api/anime/{first}")).json()
    page_two = (await user_client.get(f"/api/anime/{second}")).json()
    page_three = (await user_client.get(f"/api/anime/{third}")).json()

    assert page_one["trip"]["id"] == one["id"]
    assert page_two["trip"]["id"] == two["id"]
    assert page_three["trip"] is None, "another show's trip is not this show's"
    assert page_three["trip_limits"] == {
        "max_episodes": 50,
        "copy_estimate_bytes": FALLBACK_COPY_BYTES,
    }


async def test_the_refusals(user_client: AsyncClient, api_factory: SessionFactory) -> None:
    anime_id = await a_show(api_factory, anilist_id=981002, aired=3)

    bad = await user_client.post(
        f"/api/anime/{anime_id}/trip", json={"count": 0, "device_id": DEVICE}
    )
    assert (bad.status_code, bad.json()["detail"]) == (422, COUNT_OUT_OF_RANGE)
    too_many = await user_client.post(
        f"/api/anime/{anime_id}/trip", json={"count": 51, "device_id": DEVICE}
    )
    assert (too_many.status_code, too_many.json()["detail"]) == (422, COUNT_OUT_OF_RANGE)
    assert (
        await user_client.post(
            f"/api/anime/{anime_id}/trip", json={"count": "2", "device_id": DEVICE}
        )
    ).status_code == 422
    assert (
        await user_client.post("/api/anime/999999/trip", json={"count": 2, "device_id": DEVICE})
    ).status_code == 404

    async with api_factory() as session:
        anime_user = await user_id(api_factory)
        anime = await session.get(Anime, anime_id)
        user = await session.get(User, anime_user)
        assert anime is not None and user is not None
        await make_entry(session, user, anime, progress=3)
        await session.commit()
    caught_up = await user_client.post(
        f"/api/anime/{anime_id}/trip", json={"count": 2, "device_id": DEVICE}
    )
    assert (caught_up.status_code, caught_up.json()["detail"]) == (422, NOTHING_AIRED)

    other = await a_show(api_factory, anilist_id=981003)
    assert (
        await user_client.post(f"/api/anime/{other}/trip", json={"count": 2, "device_id": DEVICE})
    ).status_code == 201
    # The same show again is refused; another show is not (owner, 2026-10-28).
    again = await user_client.post(
        f"/api/anime/{other}/trip", json={"count": 2, "device_id": DEVICE}
    )
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
        response = await client.post(
            f"/api/anime/{anime_id}/trip", json={"count": 2, "device_id": DEVICE}
        )
    assert (response.status_code, response.json()["detail"]) == (403, DEMO_REFUSED)


async def test_only_the_owner_can_cancel_and_others_see_404(
    api_app: FastAPI, api_factory: SessionFactory, user_client: AsyncClient
) -> None:
    anime_id = await a_show(api_factory, anilist_id=981005)
    trip = (
        await user_client.post(
            f"/api/anime/{anime_id}/trip", json={"count": 2, "device_id": DEVICE}
        )
    ).json()
    await add_user(api_factory, OTHER_EMAIL, OTHER_PASSWORD)
    async with api_transport(api_app) as other:
        await login(other, OTHER_EMAIL, OTHER_PASSWORD)
        refused = await other.delete(f"/api/trips/{trip['id']}")
        assert (refused.status_code, refused.json()["detail"]) == (404, TRIP_NOT_FOUND)
        assert (await other.get("/api/trips")).json() == []

    assert (await user_client.delete(f"/api/trips/{trip['id']}")).status_code == 204
    assert (await user_client.get("/api/trips")).json() == []
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

    posted = (
        await user_client.post(
            f"/api/anime/{anime_id}/trip", json={"count": 2, "device_id": DEVICE}
        )
    ).json()
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
    assert body["trip_limits"] == {
        "max_episodes": 50,
        "copy_estimate_bytes": FALLBACK_COPY_BYTES,
    }

    async with api_factory() as session:
        await session.merge(Setting(key="trip_max_episodes", value=7))
        await session.commit()
    body = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert body["trip_limits"]["max_episodes"] == 7


async def test_the_show_page_reads_the_estimate_for_everyone_but_the_demo(
    api_app: FastAPI,
    api_factory: SessionFactory,
    user_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The demo account can never make a trip: it is sent the fallback, unread."""
    from arc.api import anime as anime_api

    calls: list[int] = []

    async def measured(session: object, settings: object) -> int:
        calls.append(1)
        return 254_000_000

    monkeypatch.setattr(anime_api, "copy_estimate_bytes", measured)
    anime_id = await a_show(api_factory, anilist_id=981091)

    body = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert body["trip_limits"]["copy_estimate_bytes"] == 254_000_000
    assert calls == [1]

    await add_user(api_factory, "demo-estimate@arc.test", "demopassword123")
    async with api_factory() as session:
        demo = await session.scalar(select(User).where(User.email == "demo-estimate@arc.test"))
        assert demo is not None
        demo.is_demo = True
        await session.commit()
    async with api_transport(api_app) as client:
        await login(client, "demo-estimate@arc.test", "demopassword123")
        demo_body = (await client.get(f"/api/anime/{anime_id}")).json()
    assert demo_body["trip_limits"]["copy_estimate_bytes"] == FALLBACK_COPY_BYTES
    assert calls == [1], "no estimate query for the demo account"


async def test_the_trip_records_the_device_that_started_it(
    user_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """A trip is its device's (owner, 2026-10-30): the id is required and canonical."""
    anime_id = await a_show(api_factory, anilist_id=981120, aired=4)

    for body in (
        {"count": 2},
        {"count": 2, "device_id": "not-a-uuid-not-a-uuid-not-a-uuid-xx"},
        # A version 1 UUID is not a device's.
        {"count": 2, "device_id": "c232ab00-9414-11ec-b3c8-9e6bdeced846"},
    ):
        refused = await user_client.post(f"/api/anime/{anime_id}/trip", json=body)
        assert refused.status_code == 422, refused.text
    assert (await user_client.get("/api/trips")).json() == []

    made = await user_client.post(
        f"/api/anime/{anime_id}/trip", json={"count": 2, "device_id": OTHER_DEVICE.upper()}
    )
    assert made.status_code == 201, made.text
    assert made.json()["device_id"] == OTHER_DEVICE
    (listed,) = (await user_client.get("/api/trips")).json()
    assert listed["device_id"] == OTHER_DEVICE
    show = (await user_client.get(f"/api/anime/{anime_id}")).json()
    assert show["trip"]["device_id"] == OTHER_DEVICE
