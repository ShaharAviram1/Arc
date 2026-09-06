"""Job queue endpoints.

Enough to enqueue work and look at what happened to it — the M1 definition of
done ("a job enqueued from the API is executed by the worker") and the seed of
the admin queue view (FR-D3). The router is thin on purpose: everything it
does lives in :mod:`arc.services.jobs`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from arc.api.deps import SessionDep, get_admin_user
from arc.models import DEFAULT_MAX_ATTEMPTS, DEFAULT_PRIORITY, Job, JobStatus
from arc.services.acquisition.names import COMPUTE_WANTS, POLL_QBIT
from arc.services.jobs import enqueue, find_active
from arc.services.library.names import LIBRARY_SCAN

# Admin only, at the router: the queue is operational surface (FR-D3), and a
# route added here later must not be able to forget the check. Anonymous
# callers get 401, signed-in non-admins 403.
router = APIRouter(tags=["jobs"], dependencies=[Depends(get_admin_user)])

MAX_LIMIT = 200


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
    type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
) -> list[Job]:
    statement = select(Job).order_by(Job.id.desc()).limit(limit)
    if job_status is not None:
        statement = statement.where(Job.status == job_status)
    if type is not None:
        statement = statement.where(Job.type == type)
    return list((await session.scalars(statement)).all())


@router.get("/api/jobs/{job_id}", response_model=JobOut, summary="One job")
async def get_job(job_id: int, session: SessionDep) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job
