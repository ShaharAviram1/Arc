"""Job queue endpoints.

Enough to enqueue work and look at what happened to it — the M1 definition of
done ("a job enqueued from the API is executed by the worker") and the seed of
the admin queue view (FR-D3). The router is thin on purpose: everything it
does lives in :mod:`arc.services.jobs`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from arc.api.deps import JobId, SessionDep, SettingsDep, get_admin_user
from arc.models import DEFAULT_MAX_ATTEMPTS, DEFAULT_PRIORITY, Job, JobStatus
from arc.services.acquisition.names import COMPUTE_WANTS, POLL_QBIT
from arc.services.jobs import (
    JobTransition,
    cancel_job,
    check_heartbeat,
    enqueue,
    find_active,
    heartbeat_at,
    retry_job,
)
from arc.services.library.names import LIBRARY_SCAN

# Admin only, at the router: the queue is operational surface (FR-D3), and a
# route added here later must not be able to forget the check. Anonymous
# callers get 401, signed-in non-admins 403.
router = APIRouter(tags=["jobs"], dependencies=[Depends(get_admin_user)])

log = logging.getLogger(__name__)

MAX_LIMIT = 200

JOB_NOT_FOUND = "job not found"


#: Bounds on what a caller may ask for. They are about keeping nonsense out of
#: the table, not about policy: an unbounded ``max_attempts`` makes a poison
#: job retry for years, and an unbounded ``priority`` is an integer-overflow
#: 500 rather than a 422.
MAX_ATTEMPTS_LIMIT = 20
MAX_PRIORITY = 1000

#: Job types whose *work* is the whole queue's, not one row's: a second one
#: queued beside the first would walk the same directories and find nothing,
#: and two of them running at once is two ffprobe storms. The scheduler
#: already dedupes them on the type (``arc/worker.py``); a caller that omits
#: ``dedupe_key`` gets the same key here, so "press the button twice" and "the
#: timer fired while a scan was pending" behave identically. An explicit
#: ``dedupe_key`` is left alone — a caller that names one means it.
TYPE_DEDUPED: frozenset[str] = frozenset({LIBRARY_SCAN, COMPUTE_WANTS, POLL_QBIT})


class JobCreate(BaseModel):
    """What a caller may set when queueing work."""

    #: A field the API does not know is a mistake worth reporting, not one to
    #: drop silently — a payload belongs under ``payload``.
    model_config = ConfigDict(extra="forbid")

    # ``type`` is not checked against the registry: handlers register
    # themselves from the worker's service packages, which the API process has
    # no reason to import, so the API cannot know the full set. A typo becomes
    # a `failed` row whose last_error names the unknown type and lists the
    # known ones, which is a better diagnostic than a 422 that lies.
    type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] | None = None
    priority: int = Field(default=DEFAULT_PRIORITY, ge=0, le=MAX_PRIORITY)
    run_after: datetime | None = None
    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=MAX_ATTEMPTS_LIMIT)
    #: When set, a pending/running job of the same type with the same key is
    #: returned instead of a second row being created.
    dedupe_key: str | None = None


class JobOut(BaseModel):
    """A job row as the API renders it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    payload: dict[str, Any]
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_by: str | None
    locked_at: datetime | None
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WorkerOut(BaseModel):
    """Whether anything is actually running the queue.

    A queue view without this is misleading in the one case that matters: a
    hundred pending jobs and a dead worker look exactly like a hundred pending
    jobs and a busy one. ``alive`` is the same decision the container
    healthcheck makes (``python -m arc.worker --check``), taken from the same
    file, so the page and Docker cannot disagree.
    """

    #: When the worker last beat, from the file's mtime. Null when it has
    #: never run, or when the API cannot see ``DATA_DIR``.
    heartbeat_at: datetime | None
    #: The beat is younger than three ticks (90 s).
    alive: bool


class JobSummaryOut(BaseModel):
    """``GET /api/jobs/summary`` — queue depths at a glance (FR-D3)."""

    #: Every ``JobStatus``, always present, zero where there is nothing.
    by_status: dict[str, int]
    #: Pending work by job type; the types with nothing waiting are absent,
    #: because the set of registered types lives in the worker process.
    by_type_pending: dict[str, int]
    worker: WorkerOut


@router.post(
    "/api/jobs",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Enqueue a job (201, or 200 for a dedupe hit)",
)
async def create_job(body: JobCreate, session: SessionDep, response: Response) -> Job:
    body = body.model_copy(
        update={"dedupe_key": body.dedupe_key or (body.type if body.type in TYPE_DEDUPED else None)}
    )
    if body.dedupe_key is not None:
        # Look first, so a dedupe hit can answer 200: nothing was created, and
        # 201 would tell the caller a row exists that it did not cause. The
        # extra SELECT costs one indexed lookup, only on the dedupe path.
        existing = await find_active(session, body.type, body.dedupe_key)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return existing

    job = await enqueue(
        session,
        body.type,
        body.payload,
        priority=body.priority,
        run_after=body.run_after,
        max_attempts=body.max_attempts,
        dedupe_key=body.dedupe_key,
    )
    await session.commit()
    return job


@router.get("/api/jobs", response_model=list[JobOut], summary="List jobs, newest first")
async def list_jobs(
    session: SessionDep,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    type: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Job]:
    """Newest first, filtered and paged for the admin queue view (FR-D3).

    ``id`` descending is the order, not ``created_at``: the ids are a sequence,
    so it is the same order without a second index, and it is *total* — two
    jobs enqueued in one transaction share a timestamp and would page
    unstably under it, which is how a row appears twice in an offset listing.
    """
    statement = select(Job).order_by(Job.id.desc()).limit(limit).offset(offset)
    if job_status is not None:
        statement = statement.where(Job.status == job_status)
    if type is not None:
        statement = statement.where(Job.type == type)
    return list((await session.scalars(statement)).all())


@router.get(
    "/api/jobs/summary",
    response_model=JobSummaryOut,
    summary="Queue depths and whether a worker is alive (admin, FR-D3)",
)
async def summary(session: SessionDep, settings: SettingsDep) -> JobSummaryOut:
    """Two ``GROUP BY``s and one ``stat``.

    Declared before ``/api/jobs/{job_id}`` on purpose: routes match in order,
    and after it ``summary`` would be an id that fails to parse.
    """
    status_rows = await session.execute(select(Job.status, func.count()).group_by(Job.status))
    counted = {row_status: count for row_status, count in status_rows.all()}
    type_rows = await session.execute(
        select(Job.type, func.count()).where(Job.status == JobStatus.PENDING).group_by(Job.type)
    )
    beat = heartbeat_at(settings)
    return JobSummaryOut(
        # Every status, always, so the client renders five figures rather than
        # however many happen to be non-zero this minute.
        by_status={member.value: counted.get(member, 0) for member in JobStatus},
        by_type_pending={job_type: count for job_type, count in sorted(type_rows.all())},
        worker=WorkerOut(heartbeat_at=beat, alive=check_heartbeat(settings)),
    )


@router.get("/api/jobs/{job_id}", response_model=JobOut, summary="One job")
async def get_job(job_id: JobId, session: SessionDep) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOB_NOT_FOUND)
    return job


async def _transition(job_id: int, session: SessionDep, retry: bool) -> Job:
    """Shared body of the two buttons: load, transition, commit, or 409."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOB_NOT_FOUND)
    was = job.status
    try:
        await (retry_job(session, job) if retry else cancel_job(session, job))
    except JobTransition as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    log.info(
        "job retried by an admin" if retry else "job cancelled by an admin",
        extra={"job_id": job.id, "type": job.type, "was": was.value},
    )
    return job


@router.post(
    "/api/jobs/{job_id}/retry",
    response_model=JobOut,
    summary="Queue a failed or cancelled job again (admin, FR-D3)",
    responses={404: {"description": JOB_NOT_FOUND}, 409: {"description": "wrong status"}},
)
async def retry(job_id: JobId, session: SessionDep) -> Job:
    """200, not 202: nothing was enqueued — an existing row is due again."""
    return await _transition(job_id, session, retry=True)


@router.post(
    "/api/jobs/{job_id}/cancel",
    response_model=JobOut,
    summary="Take a pending job off the queue (admin, FR-D3)",
    responses={404: {"description": JOB_NOT_FOUND}, 409: {"description": "wrong status"}},
)
async def cancel(job_id: JobId, session: SessionDep) -> Job:
    return await _transition(job_id, session, retry=False)
