"""The trip job handlers (FR-A12). Importing this registers them; only the worker does."""

from __future__ import annotations

from datetime import UTC, datetime

from arc.services.jobs.registry import JobContext, register
from arc.services.trips.names import (
    OFFLINE_SETTLE,
    TRIP_RELEASE,
    TRIP_SWEEP,
    enqueue_offline_settle,
)
from arc.services.trips.release import release_episode
from arc.services.trips.settle import settle_episode
from arc.services.trips.sweep import sweep_trips


@register(TRIP_RELEASE)
async def trip_release(ctx: JobContext) -> None:
    """Delete what a trip no longer needs for one episode (:mod:`.release`).

    Idempotent: once the source is gone the episode is ``not_wanted`` and a
    re-run finds nothing to do. A deletion deferred because an encode still
    holds the source raises, and the runner's backoff is the retry.
    """
    episode_id = int(ctx.payload["episode_id"])
    released = await release_episode(ctx.session, ctx.settings, episode_id)
    ctx.log.info(
        "trip release",
        extra={
            "job_id": ctx.job.id,
            "episode_id": episode_id,
            "outcome": released.outcome,
            "why": released.why,
        },
    )


@register(OFFLINE_SETTLE)
async def offline_settle(ctx: JobContext) -> None:
    """Delete a trip episode's copy once nothing waits for it (:mod:`.settle`).

    Idempotent: a settled episode has no copy and a re-run deletes nothing. A
    settle that ran too early (a confirmation under an hour old, an encode in
    flight) queues itself again for when it may act, rather than failing — the
    wait is expected, not an error.
    """
    episode_id = int(ctx.payload["episode_id"])
    settled = await settle_episode(ctx.session, ctx.settings, episode_id, now=datetime.now(UTC))
    if settled.retry_at is not None:
        await enqueue_offline_settle(
            ctx.session, episode_id, run_after=settled.retry_at, exclude_job_id=ctx.job.id
        )
    ctx.log.info(
        "offline settle",
        extra={
            "job_id": ctx.job.id,
            "episode_id": episode_id,
            "outcome": settled.outcome,
            "why": settled.why,
            "retry_at": settled.retry_at.isoformat() if settled.retry_at else None,
        },
    )


@register(TRIP_SWEEP)
async def trip_sweep(ctx: JobContext) -> None:
    """Expire rows, end trips, queue settles (:mod:`.sweep`). Idempotent."""
    swept = await sweep_trips(ctx.session, now=datetime.now(UTC))
    ctx.log.info(
        "trip sweep finished",
        extra={
            "job_id": ctx.job.id,
            "expired_rows": len(swept.expired_rows),
            "ended": len(swept.ended),
            "settles": len(swept.settles),
        },
    )


__all__ = ["offline_settle", "trip_release", "trip_sweep"]
