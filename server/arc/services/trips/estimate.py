"""How big one trip copy is likely to be (FR-A12, owner 2026-10-28).

The show page checks whether a trip fits the device's free space before it is
asked for, and the device's room is shared by every active trip of the user.
The client used to guess with a constant; the server knows what its copies
actually weigh, so it says: the median ``size`` of the last
:data:`ESTIMATE_SAMPLE` ``ready`` copies made with the host's current
settings, else :data:`FALLBACK_COPY_BYTES`. Queries only.
"""

from __future__ import annotations

from statistics import median
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import OfflineCopy, OfflineCopyState
from arc.services.media.copies import offline_options, settings_key
from arc.services.media.jobs import language_rules
from arc.services.trips.names import FALLBACK_COPY_BYTES

#: How many of the newest current-settings copies the median is taken over.
ESTIMATE_SAMPLE: Final[int] = 20


async def copy_estimate_bytes(session: AsyncSession, settings: Settings) -> int:
    """The median size of the newest ready copies made the way copies are made now.

    "The way copies are made now" is the copy's ``settings_key`` against the
    key the current ``OFFLINE_*`` settings and language rules give, so a copy
    made before an admin changed the codec or the height does not count.
    """
    sub_lang, audio_lang = await language_rules(session)
    key = settings_key(offline_options(settings), sub_lang=sub_lang, audio_lang=audio_lang)
    sizes = [
        int(size)
        for size in (
            await session.scalars(
                select(OfflineCopy.size)
                .where(
                    OfflineCopy.state == OfflineCopyState.READY,
                    OfflineCopy.settings_key == key,
                    OfflineCopy.size.is_not(None),
                    OfflineCopy.size > 0,
                )
                .order_by(OfflineCopy.ready_at.desc().nulls_last(), OfflineCopy.episode_id.desc())
                .limit(ESTIMATE_SAMPLE)
            )
        ).all()
        if size is not None
    ]
    if not sizes:
        return FALLBACK_COPY_BYTES
    return int(median(sizes))


__all__ = ["ESTIMATE_SAMPLE", "FALLBACK_COPY_BYTES", "copy_estimate_bytes"]
