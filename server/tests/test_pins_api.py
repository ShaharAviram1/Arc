"""Pinning a show: ``PUT``/``DELETE /api/anime/{id}/pin`` (FR-T8, M20 T8).

Idempotent both ways, 204 each time; 404 for an unknown show; 403 for the
demo account. ``pinned`` on the show payload is the caller's own pin, and the
admin disk view lists every pinned show with its bytes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from arc.db import SessionFactory
from arc.models import ShowPin, User, UserRole
from tests.acquisition_helpers import make_anime, make_episodes
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login

pytestmark = pytest.mark.pg

PASSWORD = "pin-password-123"


async def a_show(factory: SessionFactory, *, anilist_id: int) -> int:
    async with factory() as session:
        anime = await make_anime(session, anilist_id=anilist_id, status="FINISHED")
        await make_episodes(session, anime, 3, aired_through=3)
        await session.commit()
        return anime.id


async def pins(factory: SessionFactory, anime_id: int) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(ShowPin).where(ShowPin.anime_id == anime_id)
            )
            or 0
        )


@pytest.fixture
async def member(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, "pin-member@arc.test", PASSWORD)
    async with api_transport(api_app) as client:
        yield await login(client, "pin-member@arc.test", PASSWORD)


@pytest.fixture
async def demo(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    made = await add_user(api_factory, "pin-demo@arc.test", PASSWORD)
    async with api_factory() as session:
        row = await session.get(User, made.id)
        assert row is not None
        row.is_demo = True
        await session.commit()
    async with api_transport(api_app) as client:
        yield await login(client, "pin-demo@arc.test", PASSWORD)


async def test_pin_and_unpin_are_idempotent(
    member: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=973001)

    assert (await member.get(f"/api/anime/{anime_id}")).json()["pinned"] is False

    for _ in range(2):
        response = await member.put(f"/api/anime/{anime_id}/pin")
        assert response.status_code == 204, response.text
    assert await pins(api_factory, anime_id) == 1
    assert (await member.get(f"/api/anime/{anime_id}")).json()["pinned"] is True

    for _ in range(2):
        response = await member.delete(f"/api/anime/{anime_id}/pin")
        assert response.status_code == 204, response.text
    assert await pins(api_factory, anime_id) == 0
    assert (await member.get(f"/api/anime/{anime_id}")).json()["pinned"] is False


async def test_pinned_is_per_user(
    api_app: FastAPI, member: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=973002)
    await add_user(api_factory, "pin-other@arc.test", PASSWORD)
    assert (await member.put(f"/api/anime/{anime_id}/pin")).status_code == 204

    async with api_transport(api_app) as other:
        await login(other, "pin-other@arc.test", PASSWORD)
        assert (await other.get(f"/api/anime/{anime_id}")).json()["pinned"] is False


async def test_an_unknown_show_is_404(member: AsyncClient) -> None:
    assert (await member.put("/api/anime/999999999/pin")).status_code == 404
    assert (await member.delete("/api/anime/999999999/pin")).status_code == 404


async def test_the_demo_account_is_refused(demo: AsyncClient, api_factory: SessionFactory) -> None:
    anime_id = await a_show(api_factory, anilist_id=973003)

    put = await demo.put(f"/api/anime/{anime_id}/pin")
    assert put.status_code == 403
    assert put.json()["detail"] == "demo_account"
    assert (await demo.delete(f"/api/anime/{anime_id}/pin")).status_code == 403
    assert await pins(api_factory, anime_id) == 0


async def test_a_signed_out_caller_is_refused(api_client: AsyncClient) -> None:
    assert (await api_client.put("/api/anime/1/pin")).status_code == 401


async def test_the_disk_view_lists_pinned_shows(
    api_app: FastAPI, member: AsyncClient, api_factory: SessionFactory
) -> None:
    anime_id = await a_show(api_factory, anilist_id=973004)
    assert (await member.put(f"/api/anime/{anime_id}/pin")).status_code == 204
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(api_app) as admin:
        await login(admin, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = (await admin.get("/api/retention/disk")).json()

    (row,) = body["pinned"]
    assert row["anime_id"] == anime_id
    assert row["users"] == 1
    assert row["episodes"] == 0  # nothing retained yet
    assert row["bytes"] == 0
    assert row["anime_title"]
