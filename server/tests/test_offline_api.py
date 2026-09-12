"""``GET /api/catalogue/offline`` — what the offline import has loaded (M15.5).

The endpoint exists because the offline catalogue is invisible when it works:
it is consulted before AniList by search and matching, and an import that
stopped running three months ago looks exactly like one that ran on Monday
until the week AniList goes down again. So the assertions here are about the
two things an admin actually needs — *is it stale* and *are the rows there* —
and about the fact that nobody but an admin can ask.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from arc.db import SessionFactory
from arc.models import OfflineAnime, OfflineId, OfflineImport, UserRole
from arc.services.catalog.offline.names import FRIBB, MANAMI
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

URL = "/api/catalogue/offline"

MEMBER_EMAIL = "member@arc.test"
MEMBER_PASSWORD = "memberpassword1"


async def seed_import(
    factory: SessionFactory,
    source: str,
    *,
    age_days: float = 0.0,
    rows: int = 41_000,
    version: str = "2026-27",
) -> None:
    async with factory() as session:
        session.add(
            OfflineImport(
                source=source,
                version=version,
                rows=rows,
                checksum="0" * 64,
                imported_at=datetime.now(UTC) - timedelta(days=age_days),
            )
        )
        await session.commit()


async def seed_rows(factory: SessionFactory, *, anime: int, ids: int) -> None:
    async with factory() as session:
        for index in range(anime):
            session.add(
                OfflineAnime(title=f"Show {index}", search_text=f"show {index}", mal_id=index + 1)
            )
        for index in range(ids):
            session.add(OfflineId(mal_id=index + 1, tmdb_tv_id=index + 100))
        await session.commit()


async def member_client(app: FastAPI, factory: SessionFactory) -> AsyncClient:
    await add_user(factory, MEMBER_EMAIL, MEMBER_PASSWORD, role=UserRole.USER)
    return await login(api_transport(app), MEMBER_EMAIL, MEMBER_PASSWORD)


async def test_a_deployment_that_has_never_imported_is_stale_and_empty(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get(URL)

    assert response.status_code == 200
    body = response.json()
    assert body == {"sources": [], "stale": True, "anime_rows": 0, "id_rows": 0}


async def test_a_fresh_import_is_reported_in_full(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await seed_import(api_factory, MANAMI, age_days=1)
    await seed_import(api_factory, FRIBB, age_days=2, version="etag-value", rows=39_000)
    await seed_rows(api_factory, anime=3, ids=2)

    body = (await admin_client.get(URL)).json()

    assert body["stale"] is False
    assert (body["anime_rows"], body["id_rows"]) == (3, 2)
    # Newest first, so the manami row an admin came for is the one they see.
    assert [source["source"] for source in body["sources"]] == [MANAMI, FRIBB]
    manami = body["sources"][0]
    assert manami["version"] == "2026-27"
    assert manami["rows"] == 41_000
    assert manami["checksum"] == "0" * 64


async def test_an_import_older_than_the_window_is_stale(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Fourteen days is two missed weekly runs, which is a job that is broken
    rather than a release that slipped."""
    await seed_import(api_factory, MANAMI, age_days=15)

    assert (await admin_client.get(URL)).json()["stale"] is True


async def test_the_window_is_configurable(
    api_app: FastAPI, api_factory: SessionFactory, admin_client: AsyncClient
) -> None:
    await seed_import(api_factory, MANAMI, age_days=15)
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"offline_catalogue_stale_days": 30}
    )

    assert (await admin_client.get(URL)).json()["stale"] is False


async def test_fribb_alone_is_still_stale(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Staleness is manami's: the id map without the titles is not a catalogue."""
    await seed_import(api_factory, FRIBB, age_days=0)

    body = (await admin_client.get(URL)).json()

    assert body["stale"] is True
    assert [source["source"] for source in body["sources"]] == [FRIBB]


async def test_a_member_may_not_look(api_app: FastAPI, api_factory: SessionFactory) -> None:
    client = await member_client(api_app, api_factory)
    try:
        response = await client.get(URL)
    finally:
        await client.aclose()

    assert response.status_code == 403


async def test_a_stranger_may_not_look(api_client: AsyncClient) -> None:
    assert (await api_client.get(URL)).status_code == 401
