"""``GET /api/acquisition/qbit`` — is the client there, and what has it (FR-D3).

The one route in Arc that talks to qBittorrent inside a request, so the thing
worth pinning is that it **cannot fail**: a client that is down, refuses the
password, or was never configured is an answer (``reachable: false`` with the
reason), not a 502. An admin opens this page precisely when qBittorrent is
misbehaving, and a page that breaks in that case is no page at all.

The client is the stub from ``tests/acquisition_helpers.QbitStub`` throughout;
nothing here reaches a real qBittorrent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from arc.config import Settings
from arc.db import SessionFactory
from arc.main import create_app
from arc.models import EpisodeState, Torrent, UserRole
from arc.services.acquisition import status as qbit_status_module
from arc.services.acquisition.qbit import QbitClient
from tests.acquisition_helpers import (
    QbitStub,
    force_transport,
    make_anime,
    make_episodes,
)
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "member-qbit@arc.test"
USER_PASSWORD = "memberpassword"


@pytest.fixture
def qbit_settings(settings: Settings) -> Settings:
    """Settings with qBittorrent configured, pointed at the stub's host."""
    return settings.model_copy(
        update={
            "qbit_url": "http://qbit.test",
            "qbit_user": "admin",
            "qbit_pass": "adminadmin",
        }
    )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> QbitStub:
    """A stub qBittorrent, wired into every ``QbitClient`` the app builds."""
    stub = QbitStub()
    monkeypatch.setattr(QbitClient, "__init__", force_transport(QbitClient, stub.transport()))
    return stub


@pytest.fixture
def qbit_app(
    qbit_settings: Settings, pg_engine: AsyncEngine, api_factory: SessionFactory
) -> FastAPI:
    app = create_app(qbit_settings)
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    return app


@pytest.fixture
async def admin(qbit_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(qbit_app) as client:
        yield await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


async def a_downloading_episode(factory: SessionFactory, info_hash: str) -> int:
    """An episode with a ``torrents`` row carrying ``info_hash``."""
    async with factory() as session:
        anime = await make_anime(session, anilist_id=991000)
        episodes = await make_episodes(session, anime, 12, aired_through=7)
        episode = episodes[6]
        episode.state = EpisodeState.DOWNLOADING
        session.add(Torrent(episode_id=episode.id, info_hash=info_hash))
        await session.commit()
        return episode.id


# --- Reachable --------------------------------------------------------------


async def test_a_reachable_client_reports_its_version_and_torrents(
    admin: AsyncClient, stub: QbitStub, api_factory: SessionFactory
) -> None:
    episode_id = await a_downloading_episode(api_factory, "a" * 40)
    stub.add_torrent(
        "a" * 40,
        name="[SubsPlease] Frieren - 07 (1080p).mkv",
        progress=0.42,
        state="downloading",
        size=1_400_000_000,
        dlspeed=3_500_000,
        upspeed=120_000,
    )

    body = (await admin.get("/api/acquisition/qbit")).json()

    assert body["reachable"] is True
    assert body["version"] == "v5.2.0"
    assert body["error"] is None
    assert body["torrents"] == [
        {
            "hash": "a" * 40,
            "name": "[SubsPlease] Frieren - 07 (1080p).mkv",
            "state": "downloading",
            "progress": pytest.approx(0.42),
            "size": 1_400_000_000,
            "dlspeed": 3_500_000,
            "upspeed": 120_000,
            "episode_id": episode_id,
        }
    ]


async def test_a_torrent_arc_has_no_row_for_carries_a_null_episode(
    admin: AsyncClient, stub: QbitStub
) -> None:
    stub.add_torrent("b" * 40)

    body = (await admin.get("/api/acquisition/qbit")).json()

    assert body["torrents"][0]["episode_id"] is None


async def test_an_empty_client_is_reachable_with_nothing_in_it(
    admin: AsyncClient, stub: QbitStub
) -> None:
    body = (await admin.get("/api/acquisition/qbit")).json()

    assert body == {"reachable": True, "version": "v5.2.0", "error": None, "torrents": []}


async def test_only_arcs_own_category_is_listed(admin: AsyncClient, stub: QbitStub) -> None:
    """The client filters on the category; somebody else's downloads are not
    Arc's to show."""
    stub.add_torrent("c" * 40, category="arc")
    stub.add_torrent("d" * 40, category="personal")

    body = (await admin.get("/api/acquisition/qbit")).json()

    assert [row["hash"] for row in body["torrents"]] == ["c" * 40]


class HangingTransport(httpx.AsyncBaseTransport):
    """A qBittorrent that accepts the connection and then says nothing.

    The failure mode a per-request timeout does not cover: every call is
    *individually* within budget only because it never finishes at all.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover


# --- Unreachable ------------------------------------------------------------


async def test_a_client_that_is_not_running_is_an_answer_not_an_error(
    admin: AsyncClient, stub: QbitStub
) -> None:
    stub.down = True

    response = await admin.get("/api/acquisition/qbit")

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is False
    assert body["version"] is None
    assert body["torrents"] == []
    assert "not reachable" in body["error"]


async def test_a_wrong_password_is_reported_as_the_reason(
    admin: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = QbitStub(password="something else")
    monkeypatch.setattr(QbitClient, "__init__", force_transport(QbitClient, other.transport()))

    body = (await admin.get("/api/acquisition/qbit")).json()

    assert body["reachable"] is False
    assert "credentials" in body["error"]


async def test_an_unconfigured_client_says_so_rather_than_500(
    settings: Settings, pg_engine: AsyncEngine, api_factory: SessionFactory
) -> None:
    """``QBIT_USER``/``QBIT_PASS`` unset is a configuration answer, not a crash."""
    app = create_app(settings)
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        response = await client.get("/api/acquisition/qbit")

    assert response.status_code == 200
    assert response.json()["reachable"] is False
    assert response.json()["error"]


async def test_a_client_that_never_answers_is_cut_off_at_the_budget(
    admin: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole probe is bounded, not each of its up-to-six requests.

    ``version()`` alone would sit there for the per-request timeout, and then
    ``torrents()`` for another — with the login and the 403 retry underneath,
    a five-second per-request budget is a page that hangs for thirty. The
    assertion is on the wall clock for exactly that reason.
    """
    monkeypatch.setattr(QbitClient, "__init__", force_transport(QbitClient, HangingTransport()))
    monkeypatch.setattr(qbit_status_module, "STATUS_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    response = await admin.get("/api/acquisition/qbit")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is False
    assert body["error"] == "qbittorrent did not answer within 0.2 s"
    assert body["torrents"] == []
    # Generously above the budget and far below two per-request timeouts.
    assert elapsed < 2.0


# --- Who may look -----------------------------------------------------------


async def test_a_non_admin_cannot_see_the_client(
    qbit_app: FastAPI, api_factory: SessionFactory, stub: QbitStub
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.USER)

    async with api_transport(qbit_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        assert (await client.get("/api/acquisition/qbit")).status_code == 403


async def test_an_anonymous_caller_gets_401(qbit_app: FastAPI, stub: QbitStub) -> None:
    async with api_transport(qbit_app) as client:
        assert (await client.get("/api/acquisition/qbit")).status_code == 401


# --- The client call it is built on -----------------------------------------


async def test_the_client_reports_the_version_string() -> None:
    stub = QbitStub(version="v4.6.7")
    client = QbitClient(
        base_url="http://qbit.test",
        username="admin",
        password="adminadmin",
        category="arc",
        downloads_path="/data/downloads",
        transport=stub.transport(),
    )

    async with client:
        assert await client.version() == "v4.6.7"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"progress": 1.0000001}, 1.0),
        ({"progress": -0.5}, 0.0),
        ({"progress": "half"}, 0.0),
        ({}, 0.0),
        ({"progress": 0.42}, 0.42),
    ],
)
async def test_progress_is_clamped_to_a_fraction(raw: dict[str, Any], expected: float) -> None:
    """It is rendered straight into a progress bar's width."""
    from arc.services.acquisition.qbit import TorrentInfo

    info = TorrentInfo.from_json({"hash": "f" * 40, **raw})

    assert info is not None and info.progress == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [({"size": -1}, 0), ({"size": "big"}, 0), ({}, 0), ({"size": 12.7}, 12)],
)
async def test_a_size_the_client_does_not_know_reads_as_zero(
    raw: dict[str, Any], expected: int
) -> None:
    """A magnet whose metadata has not arrived reports ``-1``."""
    from arc.services.acquisition.qbit import TorrentInfo

    info = TorrentInfo.from_json({"hash": "e" * 40, **raw})

    assert info is not None and info.size == expected
