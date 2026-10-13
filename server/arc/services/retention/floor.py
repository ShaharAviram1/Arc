"""The disk floor's eviction plan, shared by the sweep and its preview (FR-T6).

One function so that ``GET /api/retention/preview`` lists exactly what the
hourly sweep would evict (M20 T8): the same need, the same exclusions, the
same order and the same reason strings.

The need is measured **after** the normal pass: the episodes it deletes come
off the disk first, so their bytes count towards the target and they are not
listed again. In the preview, and in a dry run, nothing has actually been
deleted, so their bytes are added to the measured free space by hand.

Imports acquisition's storage rules, which is why this is not in
:mod:`arc.services.retention.sweep` (kept free of acquisition) and not in
:mod:`arc.services.retention.jobs` (importing that registers the handlers).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.services.acquisition.rules import min_free_bytes, storage_hold
from arc.services.retention.sweep import Deletable, floor_evictions
from arc.services.storage import disk_usage

#: Eviction stops this far above the floor, so that one new download does not
#: put the disk straight back under it and start the next sweep evicting again.
FLOOR_HYSTERESIS_BYTES: Final[int] = 1024**3


async def free_bytes(settings: Settings) -> int | None:
    """Free space on the data volume, or ``None`` when it cannot be measured."""
    usage = await asyncio.to_thread(disk_usage, settings.data_dir)
    return None if usage is None else usage.free


async def floor_target(session: AsyncSession) -> int | None:
    """Free bytes eviction aims for (floor + 1 GiB); ``None`` with no floor."""
    floor = await min_free_bytes(session)
    return None if floor <= 0 else floor + FLOOR_HYSTERESIS_BYTES


async def floor_plan(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime,
    already: Sequence[Deletable] = (),
    pending_bytes: int = 0,
) -> list[Deletable]:
    """What the floor would evict after the normal pass ``already``.

    ``already`` is excluded from the plan. ``pending_bytes`` is what this
    sweep has freed (or would free) that the disk does not show yet: zero for
    a real sweep measured after its pass, the normal pass's and the idle
    copies' bytes for the preview and a dry run. Empty when the volume is not
    under its floor, when there is no floor, or when the disk cannot be
    measured — the same answers as
    :func:`~arc.services.acquisition.rules.is_storage_held`.
    """
    floor = await min_free_bytes(session)
    if floor <= 0:
        return []
    measured = await free_bytes(settings)
    if measured is None:
        return []
    free = measured + pending_bytes
    if not storage_hold(free, floor):
        return []
    return await floor_evictions(
        session,
        settings,
        need_bytes=floor + FLOOR_HYSTERESIS_BYTES - free,
        now=now,
        exclude=frozenset(item.episode_id for item in already),
    )


__all__ = ["FLOOR_HYSTERESIS_BYTES", "floor_plan", "floor_target", "free_bytes"]
