"""The retention admin API: preview, sweep, manual delete, disk usage (FR-T4).

These run against the real test database with an app whose ``DATA_DIR`` is a
throwaway directory — the preview measures files on disk, and a test that
pointed the app at the developer's own ``server/data`` would be reporting on
(and, one careless line later, deleting) real renditions.

Nothing here deletes anything: both POSTs enqueue. What is asserted is the
contract — 202 with a job row, the dedupe, the two refusals, and that the
preview says the same thing the sweep would do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from arc.config import Settings
from arc.db import SessionFactory
from arc.main import create_app
from arc.models import EpisodeState, UserRole
from arc.services.media.names import output_dir_for
from arc.services.retention.names import DELETE_EPISODE_FILES, RETENTION_SWEEP
from tests.acquisition_helpers import make_user
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login
from tests.retention_helpers import add_completion, add_want, days_ago, make_retained_episode

pytestmark = pytest.mark.pg

USER_EMAIL = "member-retention@arc.test"
USER_PASSWORD = "memberpassword"


@pytest.fixture
def retention_settings(settings: Settings, tmp_path: Path) -> Settings:
    """The test settings with ``DATA_DIR`` pointed at a throwaway directory."""
    return settings.model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def retention_app(
    retention_settings: Settings, pg_engine: AsyncEngine, api_factory: SessionFactory
) -> FastAPI:
    app = create_app(retention_settings)
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    return app


@pytest.fixture
async def admin(retention_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(retention_app) as client:
        yield await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


async def deletable_episode(
    factory: SessionFactory, settings: Settings, *, anilist_id: int = 971000
) -> int:
    """An episode everybody finished a month ago. Returns its id."""
    async with factory() as session:
        episode = await make_retained_episode(
            session, settings, anilist_id=anilist_id, ready_at=days_ago(40)
        )
        user = await make_user(session, "watcher-retention@arc.test")
        await add_completion(session, user, episode, at=days_ago(30))
        await session.commit()
        return episode.id


# --- GET /api/retention/preview ---------------------------------------------


async def test_the_preview_lists_the_episode_with_a_reason_and_its_size(
    retention_app: FastAPI,
    retention_settings: Settings,
    api_factory: SessionFactory,
    admin: AsyncClient,
) -> None:
    episode_id = await deletable_episode(api_factory, retention_settings)

    response = await admin.get("/api/retention/preview")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is False
    (row,) = body["episodes"]
    assert row["episode_id"] == episode_id
    assert row["state"] == "ready"
    assert row["number"] == 7
    assert "watched" in row["reason"] and "grace period" in row["reason"]
    assert row["bytes"] > 0 and body["bytes"] == row["bytes"]
    assert row["rendition_dir"] == str(output_dir_for(retention_settings, episode_id).resolve())
    assert row["source_dir"] == str((retention_settings.downloads_dir / str(episode_id)).resolve())
    assert row["anime_title"].startswith("Retention Test")


async def test_the_preview_leaves_out_an_episode_somebody_still_wants(
    retention_app: FastAPI,
    retention_settings: Settings,
    api_factory: SessionFactory,
    admin: AsyncClient,
) -> None:
    async with api_factory() as session:
        episode = await make_retained_episode(
            session, retention_settings, anilist_id=971010, ready_at=days_ago(90)
        )
        watcher = await make_user(session, "finished@arc.test")
        waiting = await make_user(session, "waiting@arc.test")
        await add_completion(session, watcher, episode, at=days_ago(60))
        await add_want(session, waiting, episode)
        await session.commit()

    body = (await admin.get("/api/retention/preview")).json()

    assert body["episodes"] == []
    assert body["bytes"] == 0


async def test_the_preview_is_admin_only(
    retention_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(retention_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)

        assert (await client.get("/api/retention/preview")).status_code == 403
        assert (await client.post("/api/retention/sweep")).status_code == 403


# --- POST /api/retention/sweep ----------------------------------------------


async def test_the_sweep_is_accepted_and_deduplicated(admin: AsyncClient) -> None:
    first = await admin.post("/api/retention/sweep")
    second = await admin.post("/api/retention/sweep")

    assert first.status_code == 202, first.text
    assert second.status_code == 202
    assert first.json()["type"] == RETENTION_SWEEP
    assert first.json()["priority"] == 200
    assert second.json()["id"] == first.json()["id"], "the second press queues nothing new"


# --- POST /api/episodes/{id}/delete-files -----------------------------------


async def test_a_manual_delete_is_queued_for_the_episode(
    retention_app: FastAPI,
    retention_settings: Settings,
    api_factory: SessionFactory,
    admin: AsyncClient,
) -> None:
    episode_id = await deletable_episode(api_factory, retention_settings, anilist_id=971020)

    response = await admin.post(f"/api/episodes/{episode_id}/delete-files")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["type"] == DELETE_EPISODE_FILES
    assert body["payload"]["episode_id"] == episode_id
    again = await admin.post(f"/api/episodes/{episode_id}/delete-files")
    assert again.json()["id"] == body["id"], "deduplicated on the episode"


async def test_a_manual_delete_of_an_unknown_episode_is_a_404(admin: AsyncClient) -> None:
    response = await admin.post("/api/episodes/987654321/delete-files")

    assert response.status_code == 404
    assert response.json()["detail"] == "episode not found"


async def test_a_manual_delete_of_an_episode_being_prepared_is_a_409(
    retention_app: FastAPI,
    retention_settings: Settings,
    api_factory: SessionFactory,
    admin: AsyncClient,
) -> None:
    async with api_factory() as session:
        episode = await make_retained_episode(
            session,
            retention_settings,
            anilist_id=971030,
            state=EpisodeState.PREPARING,
            ready_at=days_ago(40),
        )
        await session.commit()
        episode_id = episode.id

    response = await admin.post(f"/api/episodes/{episode_id}/delete-files")

    assert response.status_code == 409
    assert "preparing" in response.json()["detail"]


# --- GET /api/acquisition/status --------------------------------------------


async def test_the_acquisition_status_reports_the_bytes_on_disk(
    retention_app: FastAPI,
    retention_settings: Settings,
    api_factory: SessionFactory,
    admin: AsyncClient,
) -> None:
    episode_id = await deletable_episode(api_factory, retention_settings, anilist_id=971040)
    preview = (await admin.get("/api/retention/preview")).json()

    body = (await admin.get("/api/acquisition/status")).json()

    assert body["retained_bytes"] == preview["episodes"][0]["bytes"]
    assert body["paused"] is False
    assert output_dir_for(retention_settings, episode_id).exists(), "nothing was deleted"


# --- GET /api/retention/disk ------------------------------------------------


async def test_the_disk_page_splits_arcs_bytes_from_the_filesystems(
    retention_app: FastAPI,
    retention_settings: Settings,
    api_factory: SessionFactory,
    admin: AsyncClient,
) -> None:
    """Two different measurements: what the host has, and Arc's share of it."""
    episode_id = await deletable_episode(api_factory, retention_settings, anilist_id=971050)

    body = (await admin.get("/api/retention/disk")).json()

    assert sorted(body) == ["data_dir", "episodes_retained", "retained"]
    assert sorted(body["data_dir"]) == ["free", "total", "used"]
    assert body["data_dir"]["total"] > 0
    assert body["data_dir"]["free"] <= body["data_dir"]["total"]

    source = Path(retention_settings.downloads_dir / str(episode_id) / "episode.mkv")
    assert body["retained"]["sources"] == source.stat().st_size
    assert body["retained"]["renditions"] > 0
    assert body["retained"]["total"] == (
        body["retained"]["sources"] + body["retained"]["renditions"]
    )
    assert body["episodes_retained"] == 1


async def test_the_disk_page_agrees_with_the_acquisition_status(
    retention_app: FastAPI,
    retention_settings: Settings,
    api_factory: SessionFactory,
    admin: AsyncClient,
) -> None:
    """One implementation, so two pages cannot report different totals."""
    await deletable_episode(api_factory, retention_settings, anilist_id=971060)

    disk = (await admin.get("/api/retention/disk")).json()
    status = (await admin.get("/api/acquisition/status")).json()

    assert disk["retained"]["total"] == status["retained_bytes"]


async def test_an_empty_library_reports_zero_without_failing(
    retention_app: FastAPI, admin: AsyncClient
) -> None:
    body = (await admin.get("/api/retention/disk")).json()

    assert body["retained"] == {"sources": 0, "renditions": 0, "total": 0}
    assert body["episodes_retained"] == 0
    assert body["data_dir"]["total"] > 0


async def test_a_data_dir_that_does_not_exist_yet_still_reports_the_filesystem(
    settings: Settings, pg_engine: AsyncEngine, api_factory: SessionFactory, tmp_path: Path
) -> None:
    """A fresh install has no ``DATA_DIR``; a GET must not create one either."""
    missing = tmp_path / "not-created-yet"
    app = create_app(settings.model_copy(update={"data_dir": missing}))
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(app) as client:
        await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = (await client.get("/api/retention/disk")).json()

    assert body["data_dir"]["total"] > 0
    assert not missing.exists()


async def test_a_non_admin_cannot_see_the_disk(
    retention_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.USER)

    async with api_transport(retention_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        assert (await client.get("/api/retention/disk")).status_code == 403


async def test_an_anonymous_caller_cannot_see_the_disk(api_client: AsyncClient) -> None:
    assert (await api_client.get("/api/retention/disk")).status_code == 401
