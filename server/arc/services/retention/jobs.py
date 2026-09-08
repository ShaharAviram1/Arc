"""The two retention handlers: the hourly sweep and the manual delete (M10).

``retention_sweep`` asks :mod:`arc.services.retention.sweep` what may go and
hands each answer to :mod:`arc.services.retention.delete`.
``delete_episode_files`` is FR-T4's button: one episode, now, whatever the
grace period says.

**The sweep commits after every episode.** Not for speed — a sweep deletes a
handful of episodes an hour — but because the files are gone the moment
``rmtree`` returns and cannot be rolled back with the transaction. If the
fourth episode's qBittorrent call fails, the three before it must keep their
row deletions and their ``not_wanted`` state, or the database would claim to
be holding renditions that are no longer on the disk.

**The sweep is idempotent** in the only way that matters here: everything it
deletes is checked for existence first, and an episode it has already swept is
``not_wanted`` and therefore not a candidate on the next run. That is also why
an unreachable qBittorrent is survivable *per episode* rather than fatal to the
run: the episode whose torrent could not be removed is left untouched and is a
candidate again in an hour, and the ones behind it in the list — most of which
have no torrent at all — are swept now rather than in however long the client
stays down.

Deliberately **not** gated on ``acquisition_paused``. Pausing means "stop
fetching more", and a paused Arc is often a full-disk Arc — the moment
retention matters most (:mod:`arc.services.acquisition.rules`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from arc.models import Episode, Want
from arc.services.acquisition.qbit import QbitUnavailable
from arc.services.jobs.registry import JobContext, register
from arc.services.retention.delete import delete_episode_files
from arc.services.retention.names import DELETE_EPISODE_FILES, RETENTION_SWEEP
from arc.services.retention.sweep import REASON_MANUAL, candidates, targets_for_episode


async def _wanted_again(ctx: JobContext, episode_id: int) -> bool:
    """Whether somebody has come to want this episode since it was planned.

    The candidate list is worked out once and then acted on one episode at a
    time, each in its own transaction; a ``compute_wants`` running alongside
    can put a live want on the third episode while the second is being
    deleted. One indexed lookup (``ix_wants_episode_id_active``) immediately
    before an irreversible deletion is worth having.
    """
    found = await ctx.session.scalar(
        select(Want.user_id)
        .where(Want.episode_id == episode_id, Want.dropped_at.is_(None))
        .limit(1)
    )
    if found is None:
        return False
    ctx.log.info(
        "somebody wants this episode again; leaving its files alone",
        extra={"episode_id": episode_id},
    )
    return True


@register(RETENTION_SWEEP)
async def retention_sweep(ctx: JobContext) -> None:
    """Delete everything past its grace period (FR-T1, FR-T3)."""
    now = datetime.now(UTC)
    dry_run = ctx.settings.retention_dry_run
    found = await candidates(ctx.session, ctx.settings, now=now)

    swept = 0
    freed = 0
    unreachable = 0
    for target in found:
        episode = await ctx.session.get(Episode, target.episode_id)
        if episode is None:  # pragma: no cover - the show was deleted mid-sweep
            continue
        if await _wanted_again(ctx, target.episode_id):
            continue
        try:
            removed = await delete_episode_files(
                ctx.session,
                ctx.settings,
                episode,
                target.targets,
                reason=target.reason,
                dry_run=dry_run,
            )
        except QbitUnavailable as exc:
            # Only episodes with a torrent talk to qBittorrent, and the call is
            # the first thing the deleter makes, so this episode is exactly as
            # it was. The rest of the list has nothing to do with that torrent
            # and is swept normally; this one comes round again on the hour,
            # by which time the client is usually back. Raising instead would
            # let one unreachable torrent hold up every deletion behind it for
            # as long as the client stayed down.
            unreachable += 1
            ctx.log.warning(
                "qBittorrent is unreachable; leaving this episode for the next sweep",
                extra={"episode_id": target.episode_id, "error": str(exc)},
            )
            continue
        if not removed.acted:
            continue
        swept += 1
        freed += removed.freed_bytes
        if not dry_run:
            await ctx.session.commit()

    ctx.log.info(
        "retention sweep finished",
        extra={
            "job_id": ctx.job.id,
            "candidates": len(found),
            "deleted": swept,
            "skipped_qbit_down": unreachable,
            "freed_bytes": freed,
            "dry_run": dry_run,
        },
    )


@register(DELETE_EPISODE_FILES)
async def delete_files(ctx: JobContext) -> None:
    """Delete one episode's files on an admin's say-so (FR-T4).

    No grace period and no want check: the admin has looked at the disk usage
    and decided. An episode somebody still wants goes back to ``not_wanted``
    like any other, so the next ``compute_wants`` re-acquires it — which is
    also how FR-T4's "or re-fetch" works, without a second job type.
    """
    episode_id = int(ctx.payload["episode_id"])
    episode = await ctx.session.get(Episode, episode_id)
    if episode is None:
        ctx.log.info("episode went away before its files were deleted", extra={"id": episode_id})
        return

    targets = await targets_for_episode(ctx.session, ctx.settings, episode_id)
    removed = await delete_episode_files(
        ctx.session,
        ctx.settings,
        episode,
        targets,
        reason=REASON_MANUAL,
        dry_run=ctx.settings.retention_dry_run,
    )
    ctx.log.info(
        "manual deletion finished",
        extra={"job_id": ctx.job.id, **removed.as_dict()},
    )


__all__ = ["DELETE_EPISODE_FILES", "RETENTION_SWEEP", "delete_files", "retention_sweep"]
