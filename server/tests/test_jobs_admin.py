"""The admin queue view: filters, the summary, retry and cancel (FR-D3).

``tests/test_jobs.py`` owns the queue *mechanics* — claiming, backoff, the
stale sweep. This file is about the surface an admin presses buttons on: what
``GET /api/jobs`` will page through, what ``GET /api/jobs/summary`` says the
queue is holding and whether anything is running it, and the two transitions,
including every status they must refuse.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from arc.config import Settings
from arc.db import SessionFactory
from arc.main import create_app
from arc.models import Job, JobStatus, UserRole
from arc.services.jobs import enqueue
from arc.services.jobs.heartbeat import HEARTBEAT_FILENAME, HEARTBEAT_STALE_AFTER
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "member-jobs@arc.test"
USER_PASSWORD = "memberpassword"


@pytest.fixture
def worker_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Settings whose ``DATA_DIR`` is a directory this test owns.

    The heartbeat file lives under it, and a test that pointed the app at the
    developer's own ``server/data`` would report on their running worker.
    """
    return settings.model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def jobs_app(
    worker_settings: Settings, pg_engine: AsyncEngine, api_factory: SessionFactory
) -> FastAPI:
    app = create_app(worker_settings)
    app.state.engine = pg_engine
    app.state.session_factory = api_factory
    return app


@pytest.fixture
async def admin(jobs_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(jobs_app) as client:
        yield await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


def beat(settings: Settings, *, age_seconds: float = 0.0) -> Path:
    """Write a heartbeat file ``age_seconds`` old and return its path."""
    path = settings.data_dir / HEARTBEAT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("beat", encoding="utf-8")
    when = datetime.now(UTC).timestamp() - age_seconds
    os.utime(path, (when, when))
    return path


async def make_job(
    factory: SessionFactory,
    *,
    type: str = "noop",
    status: JobStatus = JobStatus.PENDING,
    attempts: int = 0,
    last_error: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """A job row in exactly the state a test needs. Returns its id."""
    async with factory() as session:
        job = await enqueue(session, type, payload)
        job.status = status
        job.attempts = attempts
        job.last_error = last_error
        if status is JobStatus.RUNNING:
            job.locked_by = "test-worker:1"
            job.locked_at = datetime.now(UTC)
            job.started_at = datetime.now(UTC)
        if status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            job.finished_at = datetime.now(UTC)
        await session.commit()
        return job.id


async def reload(factory: SessionFactory, job_id: int) -> Job:
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        return job


# --- GET /api/jobs: filters and paging ---------------------------------------


async def test_the_listing_filters_by_status_and_type(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    await make_job(api_factory, type="noop", status=JobStatus.PENDING)
    failed = await make_job(api_factory, type="library_scan", status=JobStatus.FAILED)
    await make_job(api_factory, type="library_scan", status=JobStatus.DONE)

    by_status = (await admin.get("/api/jobs", params={"status": "failed"})).json()
    by_type = (await admin.get("/api/jobs", params={"type": "library_scan"})).json()
    both = (
        await admin.get("/api/jobs", params={"status": "failed", "type": "library_scan"})
    ).json()

    assert [row["id"] for row in by_status] == [failed]
    assert len(by_type) == 2
    assert [row["id"] for row in both] == [failed]


async def test_an_unknown_status_is_a_422_not_an_empty_list(admin: AsyncClient) -> None:
    """A typo must not look like "the queue is empty"."""
    assert (await admin.get("/api/jobs", params={"status": "pendign"})).status_code == 422


async def test_limit_and_offset_page_through_newest_first(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    ids = [await make_job(api_factory) for _ in range(5)]

    first = (await admin.get("/api/jobs", params={"limit": 2})).json()
    second = (await admin.get("/api/jobs", params={"limit": 2, "offset": 2})).json()

    assert [row["id"] for row in first] == list(reversed(ids))[:2]
    assert [row["id"] for row in second] == list(reversed(ids))[2:4]


async def test_the_limit_is_bounded(admin: AsyncClient) -> None:
    assert (await admin.get("/api/jobs", params={"limit": 201})).status_code == 422
    assert (await admin.get("/api/jobs", params={"limit": 0})).status_code == 422
    assert (await admin.get("/api/jobs", params={"offset": -1})).status_code == 422


# --- GET /api/jobs/summary ---------------------------------------------------


async def test_the_summary_counts_every_status_and_the_pending_types(
    admin: AsyncClient, api_factory: SessionFactory, worker_settings: Settings
) -> None:
    beat(worker_settings)
    await make_job(api_factory, type="noop", status=JobStatus.PENDING)
    await make_job(api_factory, type="noop", status=JobStatus.PENDING)
    await make_job(api_factory, type="library_scan", status=JobStatus.PENDING)
    await make_job(api_factory, type="library_scan", status=JobStatus.RUNNING)
    await make_job(api_factory, type="transcode_episode", status=JobStatus.FAILED)

    body = (await admin.get("/api/jobs/summary")).json()

    assert body["by_status"] == {
        "pending": 3,
        "running": 1,
        "done": 0,
        "failed": 1,
        "cancelled": 0,
    }
    assert body["by_type_pending"] == {"library_scan": 1, "noop": 2}


async def test_an_empty_queue_still_reports_five_zeros(
    admin: AsyncClient, worker_settings: Settings
) -> None:
    """The client renders five figures, not however many are non-zero."""
    beat(worker_settings)
    body = (await admin.get("/api/jobs/summary")).json()

    assert body["by_status"] == {
        "pending": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
    }
    assert body["by_type_pending"] == {}


async def test_a_fresh_heartbeat_means_the_worker_is_alive(
    admin: AsyncClient, worker_settings: Settings
) -> None:
    beat(worker_settings)

    worker = (await admin.get("/api/jobs/summary")).json()["worker"]

    assert worker["alive"] is True
    assert worker["heartbeat_at"] is not None
    when = datetime.fromisoformat(worker["heartbeat_at"])
    assert abs((datetime.now(UTC) - when).total_seconds()) < 30


async def test_a_stale_heartbeat_means_the_worker_is_dead(
    admin: AsyncClient, worker_settings: Settings
) -> None:
    """The same decision the container healthcheck makes, from the same file."""
    beat(worker_settings, age_seconds=HEARTBEAT_STALE_AFTER + 10)

    worker = (await admin.get("/api/jobs/summary")).json()["worker"]

    assert worker["alive"] is False
    assert worker["heartbeat_at"] is not None


async def test_no_heartbeat_at_all_is_reported_as_dead(
    admin: AsyncClient, worker_settings: Settings
) -> None:
    assert not (worker_settings.data_dir / HEARTBEAT_FILENAME).exists()

    worker = (await admin.get("/api/jobs/summary")).json()["worker"]

    assert worker == {"heartbeat_at": None, "alive": False}


async def test_summary_is_a_route_and_not_a_job_id(admin: AsyncClient) -> None:
    """Declared before ``/api/jobs/{job_id}``; after it, this would be a 422."""
    assert (await admin.get("/api/jobs/summary")).status_code == 200


# --- POST /api/jobs/{id}/retry -----------------------------------------------


async def test_retrying_a_failed_job_makes_it_due_now_with_a_full_budget(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    job_id = await make_job(
        api_factory, status=JobStatus.FAILED, attempts=3, last_error="RuntimeError('boom')"
    )

    body = (await admin.post(f"/api/jobs/{job_id}/retry")).json()

    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["locked_by"] is None
    assert body["finished_at"] is None
    # Kept: it is the reason somebody pressed retry.
    assert body["last_error"] == "RuntimeError('boom')"
    due = datetime.fromisoformat(body["run_after"])
    assert abs((datetime.now(UTC) - due).total_seconds()) < 30


async def test_a_cancelled_job_may_be_retried(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    """An admin is allowed to change their mind about a cancellation."""
    job_id = await make_job(api_factory, status=JobStatus.CANCELLED)

    assert (await admin.post(f"/api/jobs/{job_id}/retry")).json()["status"] == "pending"


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.DONE])
async def test_retrying_anything_else_is_a_409(
    admin: AsyncClient, api_factory: SessionFactory, status: JobStatus
) -> None:
    job_id = await make_job(api_factory, status=status)

    response = await admin.post(f"/api/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert status.value in response.json()["detail"]
    assert (await reload(api_factory, job_id)).status is status


async def test_retrying_a_job_that_is_not_there_is_a_404(admin: AsyncClient) -> None:
    assert (await admin.post("/api/jobs/999999/retry")).status_code == 404


# --- POST /api/jobs/{id}/cancel ----------------------------------------------


async def test_cancelling_a_pending_job_takes_it_off_the_queue(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    job_id = await make_job(api_factory, status=JobStatus.PENDING)

    body = (await admin.post(f"/api/jobs/{job_id}/cancel")).json()

    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    assert (await reload(api_factory, job_id)).status is JobStatus.CANCELLED


async def test_a_running_job_cannot_be_cancelled_and_says_why(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    """A bare "only a pending one can" would not explain the case admins hit."""
    job_id = await make_job(api_factory, status=JobStatus.RUNNING)

    response = await admin.post(f"/api/jobs/{job_id}/cancel")

    assert response.status_code == 409
    assert "running" in response.json()["detail"]
    assert (await reload(api_factory, job_id)).status is JobStatus.RUNNING


@pytest.mark.parametrize("status", [JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED])
async def test_cancelling_a_finished_job_is_a_409(
    admin: AsyncClient, api_factory: SessionFactory, status: JobStatus
) -> None:
    job_id = await make_job(api_factory, status=status)

    response = await admin.post(f"/api/jobs/{job_id}/cancel")

    assert response.status_code == 409
    assert (await reload(api_factory, job_id)).status is status


async def test_cancelling_a_job_that_is_not_there_is_a_404(admin: AsyncClient) -> None:
    assert (await admin.post("/api/jobs/999999/cancel")).status_code == 404


async def test_a_cancelled_job_is_never_claimed_again(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    """The whole of the operation: the claim's ``WHERE`` does the rest."""
    from arc.services.jobs import claim_one

    job_id = await make_job(api_factory, status=JobStatus.PENDING)
    await admin.post(f"/api/jobs/{job_id}/cancel")

    async with api_factory() as session:
        assert await claim_one(session, "test-worker:1") is None


async def test_a_job_claimed_between_the_read_and_the_write_still_409s(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    """The status is in the ``WHERE`` clause, not read into Python first.

    A worker claiming the row a moment before the cancel lands is the race the
    guard exists for: without it the read says ``pending``, the write lands on
    a ``running`` job, and Arc has marked executing work cancelled. Run
    sequentially here — the claim commits before the request — because that is
    the same query order the race produces, and it is deterministic.
    """
    from arc.services.jobs import claim_one

    job_id = await make_job(api_factory, status=JobStatus.PENDING)
    async with api_factory() as session:
        claimed = await claim_one(session, "test-worker:1")
        assert claimed is not None and claimed.id == job_id

    response = await admin.post(f"/api/jobs/{job_id}/cancel")

    assert response.status_code == 409
    assert "running" in response.json()["detail"]
    assert (await reload(api_factory, job_id)).status is JobStatus.RUNNING


async def test_a_retried_job_becomes_claimable_again(
    admin: AsyncClient, api_factory: SessionFactory
) -> None:
    from arc.services.jobs import claim_one

    job_id = await make_job(api_factory, status=JobStatus.FAILED, attempts=3)

    async with api_factory() as session:
        assert await claim_one(session, "test-worker:1") is None

    await admin.post(f"/api/jobs/{job_id}/retry")

    async with api_factory() as session:
        claimed = await claim_one(session, "test-worker:1")
        assert claimed is not None and claimed.id == job_id


# --- Who may press the buttons ----------------------------------------------


async def test_a_non_admin_cannot_reach_any_of_it(
    jobs_app: FastAPI, api_factory: SessionFactory
) -> None:
    job_id = await make_job(api_factory, status=JobStatus.FAILED)
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.USER)

    async with api_transport(jobs_app) as client:
        await login(client, USER_EMAIL, USER_PASSWORD)
        assert (await client.get("/api/jobs/summary")).status_code == 403
        assert (await client.post(f"/api/jobs/{job_id}/retry")).status_code == 403
        assert (await client.post(f"/api/jobs/{job_id}/cancel")).status_code == 403

    assert (await reload(api_factory, job_id)).status is JobStatus.FAILED


async def test_an_anonymous_caller_gets_401(jobs_app: FastAPI) -> None:
    async with api_transport(jobs_app) as client:
        assert (await client.get("/api/jobs/summary")).status_code == 401
        assert (await client.post("/api/jobs/1/retry")).status_code == 401
        assert (await client.post("/api/jobs/1/cancel")).status_code == 401
