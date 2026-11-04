"""Every route that used to refuse the demo account still needs a live session.

M21 T9 removed the demo 403s; what is left is the ordinary rule for every one
of those routes: no session is a 401, and so is a session whose account has
since been deactivated (``resolve_session`` checks ``is_active`` on every
request). Media routes are behind the session too (spec §7).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from arc.db import SessionFactory
from arc.models import User
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

DEVICE = "0b7c3f2e-5d3a-4c1b-9a8e-1f2d3c4b5a69"
EMAIL = "former-demo-routes@arc.test"
PASSWORD = "former-demo-routes-password"

#: (method, path, JSON body or None): every route a demo guard sat on.
FORMER_DEMO_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("put", f"/api/devices/{DEVICE}/holdings", {"episode_ids": []}),
    ("post", f"/api/devices/{DEVICE}/report", {"events": []}),
    ("get", "/api/episodes/1/offline", None),
    ("post", "/api/episodes/1/offline", None),
    ("put", "/api/anime/1/pin", None),
    ("delete", "/api/anime/1/pin", None),
    ("post", "/api/anime/1/trip", {"count": 1, "device_id": DEVICE}),
    ("get", "/api/episodes/1/releases", None),
    ("post", "/api/trips/1/release", {"link": "https://nyaa.si/view/1"}),
    ("get", "/media/1/episode.mp4", None),
    ("head", "/media/1/episode.mp4", None),
    ("get", "/media/1/offline.mp4", None),
]

ROUTE_IDS = [f"{method.upper()} {path}" for method, path, _ in FORMER_DEMO_ROUTES]


async def call(
    client: AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> Response:
    if body is None:
        return await client.request(method.upper(), path)
    return await client.request(method.upper(), path, json=body)


@pytest.mark.parametrize(("method", "path", "body"), FORMER_DEMO_ROUTES, ids=ROUTE_IDS)
async def test_no_session_is_401(
    api_app: FastAPI, method: str, path: str, body: dict[str, Any] | None
) -> None:
    async with api_transport(api_app) as anon:
        response = await call(anon, method, path, body)

    assert response.status_code == 401, response.text


@pytest.mark.parametrize(("method", "path", "body"), FORMER_DEMO_ROUTES, ids=ROUTE_IDS)
async def test_a_deactivated_account_is_401(
    api_app: FastAPI,
    api_factory: SessionFactory,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    made = await add_user(api_factory, EMAIL, PASSWORD)
    async with api_transport(api_app) as client:
        await login(client, EMAIL, PASSWORD)
        async with api_factory() as session:
            row = await session.get(User, made.id)
            assert row is not None
            row.is_active = False
            await session.commit()

        response = await call(client, method, path, body)

    assert response.status_code == 401, response.text
