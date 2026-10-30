"""Recording what a device holds: the write half of FR-A15 (M20 T4).

A device reports its **whole** set of unwatched downloads each time, and the
set replaces what was stored for that (user, device). Replace rather than add
and remove, so a report is idempotent and a lost "I deleted it" heals itself on
the next launch; every row is restamped, so ``reported_at`` is the last time the
device said so. The reconciler reads the rows
(:func:`~arc.services.acquisition.wants._device_held`) for
:data:`~arc.services.acquisition.wants.DEVICE_HOLDING_DAYS` and no longer.

Three pieces of housekeeping ride on every report, all within the caller's own
rows: an advisory lock on (user, device), so two first reports arriving
together cannot collide on the key; the user's rows older than the TTL are
deleted (they no longer count, so they are only clutter); and at most
:data:`MAX_DEVICES` devices are kept per user, the ones that reported longest
ago evicted first — a browser that clears its storage mints a new device id,
and those must not pile up for ever.

A report may carry, per episode, what the device holds of its copy — the ETag
it downloaded, the bytes and the SHA-256 it computed (owner, 2026-10-30) —
which is stored beside the row for the operator; a report in the old shape
(ids only) leaves them null.

Ids that name no episode are dropped rather than refused: a device may hold a
copy of an episode the catalogue has since lost, and refusing the whole report
for it would stop the rest being recorded.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DeviceHolding, Episode
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.acquisition.wants import DEVICE_HOLDING_DAYS

log = logging.getLogger(__name__)

#: The most episode ids one report may carry. A device holding more than this
#: unwatched is not a case the window has to be exact about.
MAX_HOLDINGS = 500

#: The most devices whose reports are kept per user; the oldest report goes
#: first (owner-approved small call, 2026-10-13).
MAX_DEVICES = 10

#: First half of the (user, device) advisory lock; folded into ``int4`` like
#: the other two-part keys in Arc.
HOLDINGS_LOCK_KEY: int = (zlib.crc32(b"device_holdings") + (1 << 31)) % (1 << 32) - (1 << 31)


@dataclass(frozen=True, slots=True)
class HoldingDetail:
    """What a device says it holds of one episode's copy; any part may be unknown."""

    etag: str | None = None
    size: int | None = None
    sha256: str | None = None


async def replace_holdings(
    session: AsyncSession,
    *,
    user_id: int,
    device_id: str,
    episode_ids: Collection[int],
    now: datetime,
    details: Mapping[int, HoldingDetail] | None = None,
) -> bool:
    """Make this device's stored set equal ``episode_ids``. ``True`` if the set changed.

    A changed set queues a reconciliation, so an episode the device has just
    deleted comes back into the window without waiting for the timer.
    Flushes; the caller commits.
    """
    await session.execute(
        select(
            func.pg_advisory_xact_lock(HOLDINGS_LOCK_KEY, func.hashtext(f"{user_id}:{device_id}"))
        )
    )
    wanted = set(episode_ids)
    known = (
        set((await session.scalars(select(Episode.id).where(Episode.id.in_(wanted)))).all())
        if wanted
        else set()
    )
    before = set(
        (
            await session.scalars(
                select(DeviceHolding.episode_id).where(
                    DeviceHolding.user_id == user_id, DeviceHolding.device_id == device_id
                )
            )
        ).all()
    )
    await session.execute(
        delete(DeviceHolding).where(
            DeviceHolding.user_id == user_id, DeviceHolding.device_id == device_id
        )
    )
    found = details or {}
    session.add_all(
        _holding(user_id, device_id, episode_id, now, found.get(episode_id))
        for episode_id in sorted(known)
    )
    await session.flush()
    purged = await _purge(session, user_id=user_id, now=now)
    changed = before != known
    if changed or purged:
        await enqueue_compute_wants(session)
    log.info(
        "device holdings reported",
        extra={
            "user_id": user_id,
            "device_id": device_id,
            "episodes": len(known),
            "unknown": len(wanted - known),
            "changed": changed,
            "purged": purged,
        },
    )
    return changed


def _holding(
    user_id: int, device_id: str, episode_id: int, now: datetime, detail: HoldingDetail | None
) -> DeviceHolding:
    detail = detail or HoldingDetail()
    return DeviceHolding(
        user_id=user_id,
        device_id=device_id,
        episode_id=episode_id,
        reported_at=now,
        etag=detail.etag,
        size=detail.size,
        sha256=detail.sha256.lower() if detail.sha256 is not None else None,
    )


async def _purge(session: AsyncSession, *, user_id: int, now: datetime) -> int:
    """Drop this user's expired rows and devices beyond :data:`MAX_DEVICES`. Rows removed."""
    cutoff = now - timedelta(days=DEVICE_HOLDING_DAYS)
    expired = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(DeviceHolding).where(
                DeviceHolding.user_id == user_id, DeviceHolding.reported_at < cutoff
            )
        ),
    )
    removed: int = expired.rowcount or 0
    devices = (
        (
            await session.execute(
                select(DeviceHolding.device_id)
                .where(DeviceHolding.user_id == user_id)
                .group_by(DeviceHolding.device_id)
                .order_by(func.max(DeviceHolding.reported_at).desc(), DeviceHolding.device_id)
            )
        )
        .scalars()
        .all()
    )
    evicted = list(devices[MAX_DEVICES:])
    if evicted:
        gone = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(DeviceHolding).where(
                    DeviceHolding.user_id == user_id, DeviceHolding.device_id.in_(evicted)
                )
            ),
        )
        removed += gone.rowcount or 0
    return removed


__all__ = ["MAX_DEVICES", "MAX_HOLDINGS", "HoldingDetail", "replace_holdings"]
