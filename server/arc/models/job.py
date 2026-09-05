"""The job queue: ``jobs`` (architecture.md §2, §4).

Arc has no Redis. Background work is rows in this table, claimed by the
worker with ``SELECT … FOR UPDATE SKIP LOCKED``, which is what lets more than
one worker run without a lock server. The claim loop itself lives in
``arc.services.jobs``; this module only fixes the shape.

Claim order is ``(priority ASC, run_after ASC, id ASC)`` over rows that are
``pending`` and due, which is exactly the shape of ``ix_jobs_status_run_after
_priority``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk, created_at
from arc.models.enums import JobStatus, enum_column

#: Lower numbers run first. Transcodes for an episode a user is about to
#: reach are given a smaller number (FR-P3); housekeeping a larger one.
DEFAULT_PRIORITY = 100
DEFAULT_MAX_ATTEMPTS = 3


class Job(Base):
    """One unit of background work."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_run_after_priority", "status", "run_after", "priority"),
    )

    id: Mapped[int] = bigint_pk()
    #: Registry key, e.g. ``search_release``. Handlers are idempotent.
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Arguments. Must be small and serialisable — ids, not objects.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus),
        nullable=False,
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.value,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PRIORITY, server_default=str(DEFAULT_PRIORITY)
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MAX_ATTEMPTS,
        server_default=str(DEFAULT_MAX_ATTEMPTS),
    )
    #: Not claimable before this. Retry backoff pushes it forward.
    run_after: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    #: Identity of the worker holding the row; null when free.
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: Tail of the last failure, shown in the admin queue view (FR-D3).
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)
