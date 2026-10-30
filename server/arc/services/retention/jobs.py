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
from arc.services.media.live import sweep_live_leftovers
from arc.services.retention.delete import (
    delete_episode_files,
    delete_idle_copy,
    prune_remembered,
)
from arc.services.retention.floor import floor_plan, floor_target, free_bytes
from arc.services.retention.names import DELETE_EPISODE_FILES, RETENTION_SWEEP
from arc.services.retention.rules import offline_idle_period
from arc.services.retention.sweep import (
    REASON_MANUAL,
    Deletable,
    candidates,
    idle_copies,
    in_use_now,
    targets_for_episode,
)
from arc.services.trips.rules import held_episode_ids


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


async def _held(ctx: JobContext, episode_id: int) -> bool:
    """Whether an active trip's device holds this episode's copy unwatched.

    Such a copy survives every retention deletion — the grace sweep, the
    floor eviction and an admin's — while its source and rendition go (owner,
    2026-10-30: a held copy is never lost mid-trip); the episode then rests
    ``not_wanted`` holding only the copy, as a trip-only one does, and the
    trip's settle deletes it once watched or the trip ends.
    """
    return bool(await held_episode_ids(ctx.session, [episode_id]))


@register(RETENTION_SWEEP)
async def retention_sweep(ctx: JobContext) -> None:
    """Delete everything past its grace period (FR-T1, FR-T3), then, if the
    disk is still under its floor, evict least recently watched (FR-T6)."""
    now = datetime.now(UTC)
    dry_run = ctx.settings.retention_dry_run
    found = await candidates(ctx.session, ctx.settings, now=now)

    swept = 0
    freed = 0
    unreachable = 0
    acted: list[Deletable] = []
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
                keep_copy=await _held(ctx, episode.id),
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
        acted.append(target)
        if not dry_run:
            await ctx.session.commit()

    # Then the small offline copies of ready episodes nobody has fetched for
    # ``offline_idle_days`` (FR-P6). The episode stays; only the copy goes, one
    # commit each for the same reason as above.
    idle = await idle_copies(
        ctx.session, ctx.settings, now=now, idle=await offline_idle_period(ctx.session)
    )
    copies = 0
    for copy in idle:
        if await delete_idle_copy(ctx.session, ctx.settings, copy, dry_run=dry_run):
            copies += 1
            freed += copy.bytes
            if not dry_run:
                await ctx.session.commit()

    # Last, the disk floor (FR-T6, M20 T8): if the volume is still under it,
    # evict least recently watched first until it is a GiB above it.
    evicted, evicted_bytes = await _evict_to_floor(
        ctx,
        now=now,
        dry_run=dry_run,
        swept=acted,
        pending_bytes=freed if dry_run else 0,
    )
    freed += evicted_bytes

    # Live directories (M20 T6) nothing stands behind any more: a download
    # that was cancelled, a file that went to review, a remux that did not
    # adopt. Never in a dry run: it is housekeeping, not a retention decision.
    if not dry_run:
        await sweep_live_leftovers(ctx.session, ctx.settings)

    # Remembered releases (M20 T9, FR-T3) older than REMEMBER_DAYS: rows only,
    # no bytes, so last and in one statement.
    forgotten = await prune_remembered(ctx.session, now=now, dry_run=dry_run)
    if forgotten and not dry_run:
        await ctx.session.commit()

    ctx.log.info(
        "retention sweep finished",
        extra={
            "job_id": ctx.job.id,
            "candidates": len(found),
            "deleted": swept,
            "idle_copies": copies,
            "floor_evicted": evicted,
            "remembered_forgotten": forgotten,
            "skipped_qbit_down": unreachable,
            "freed_bytes": freed,
            "dry_run": dry_run,
        },
    )


async def _evict_to_floor(
    ctx: JobContext,
    *,
    now: datetime,
    dry_run: bool,
    swept: list[Deletable],
    pending_bytes: int,
) -> tuple[int, int]:
    """Evict until free space is at least the floor plus 1 GiB (FR-T6, M20 T8).

    The plan is :func:`~arc.services.retention.floor.floor_plan` — the same
    function the preview calls — measured after the normal pass: ``swept`` is
    excluded, and under ``RETENTION_DRY_RUN`` its bytes (``pending_bytes``)
    count as freed although nothing was deleted. Before each deletion the live
    want, a play within 30 minutes and a copy being sent are asked again; each
    episode is committed on its own, and the disk is measured again after
    each, so the loop stops as soon as the target is reached. In a dry run
    nothing is deleted and the would-be bytes are counted instead.
    """
    found = await floor_plan(
        ctx.session, ctx.settings, now=now, already=swept, pending_bytes=pending_bytes
    )
    if not found:
        return 0, 0
    target = await floor_target(ctx.session)
    measured = await free_bytes(ctx.settings)
    if target is None or measured is None:  # pragma: no cover - floor_plan was empty then
        return 0, 0
    free = measured + pending_bytes
    ctx.log.warning(
        "free space is below the floor; evicting least recently watched first",
        extra={"free_bytes": free, "target_bytes": target, "planned": len(found)},
    )

    evicted = 0
    freed = 0
    for item in found:
        if free >= target:
            break
        episode = await ctx.session.get(Episode, item.episode_id)
        if episode is None:  # pragma: no cover - the show was deleted mid-sweep
            continue
        if await _wanted_again(ctx, item.episode_id):
            continue
        if await in_use_now(ctx.session, item.episode_id, now=datetime.now(UTC)):
            ctx.log.info(
                "somebody is playing or fetching this episode; not evicting it",
                extra={"episode_id": item.episode_id},
            )
            continue
        try:
            removed = await delete_episode_files(
                ctx.session,
                ctx.settings,
                episode,
                item.targets,
                reason=item.reason,
                dry_run=dry_run,
                keep_copy=await _held(ctx, episode.id),
            )
        except QbitUnavailable as exc:
            ctx.log.warning(
                "qBittorrent is unreachable; not evicting this episode",
                extra={"episode_id": item.episode_id, "error": str(exc)},
            )
            continue
        if not removed.acted:
            continue
        evicted += 1
        freed += removed.freed_bytes
        if dry_run:
            free += removed.freed_bytes
            continue
        await ctx.session.commit()
        again = await free_bytes(ctx.settings)
        free = again if again is not None else free + removed.freed_bytes
    return evicted, freed


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
        keep_copy=await _held(ctx, episode.id),
    )
    ctx.log.info(
        "manual deletion finished",
        extra={"job_id": ctx.job.id, **removed.as_dict()},
    )


__all__ = ["DELETE_EPISODE_FILES", "RETENTION_SWEEP", "delete_files", "retention_sweep"]
