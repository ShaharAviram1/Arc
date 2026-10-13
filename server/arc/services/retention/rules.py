"""The retention windows, read out of ``settings`` (FR-T5).

**G** (``grace_days_g``, default 7) is how long an episode's files survive
after the last person who wanted it finished with it (FR-T1). **D**
(``unwatched_days_d``, default 21) is how long a ready episode may sit
unwatched before that user's want is dropped (FR-T2).

Both are admin-editable and therefore live in the ``settings`` table rather
than in the environment, exactly like ``look_ahead_n`` beside them
(:mod:`arc.services.acquisition.rules`). The **dry-run switch** does not: it is
an operator's brake on a process that deletes files, it is set before the
process starts rather than from the admin UI, and a value hand-edited into a
table Arc itself writes is the wrong place for "do not delete anything". It is
``RETENTION_DRY_RUN`` in the environment (:mod:`arc.config`).

Every read falls back to :data:`arc.models.DEFAULT_SETTINGS`, and a value of
the wrong JSON type is logged and ignored rather than raising: one bad row
must not stop the sweep, and — more to the point — must not be able to turn a
7 into a 0 and delete tonight's episode this afternoon.

This module deliberately imports nothing from
:mod:`arc.services.acquisition`. ``compute_wants`` reads D from here (FR-T2 is
applied inside the reconciler), and the retention sweep reaches back into
acquisition's state machine; one of the two directions has to be free of the
other, and this is the leaf.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DEFAULT_SETTINGS, Setting

log = logging.getLogger(__name__)

#: G — days between the last completion (or drop) and the deletion (FR-T1).
GRACE_KEY: Final[str] = "grace_days_g"

#: D — days a ready episode may go unwatched before the want is dropped
#: (FR-T2).
UNWATCHED_KEY: Final[str] = "unwatched_days_d"

#: How long a ready episode's small offline copy may go unfetched (FR-P6,
#: FR-T7). Read with a floor of one day: a zero would delete a copy in the
#: hour between its encode and the device fetching it.
OFFLINE_IDLE_KEY: Final[str] = "offline_idle_days"

#: Hard ceiling on either window, whatever the table says. Ten years: the
#: point is to catch a hand-edited row that would otherwise mean "never", not
#: to second-guess an admin who wants a long grace period.
MAX_DAYS: Final[int] = 3650

#: The delete-after-watching grace, in hours (FR-T1, M20 T8): how long an
#: episode's files survive the last completion by everyone who wanted it. 0
#: means "at the next hourly sweep".
WATCHED_GRACE_KEY: Final[str] = "watched_grace_hours"

#: Ceiling on the hours above, as written and as read: a week. Past that the
#: day-counted G is the rule that was meant.
MAX_WATCHED_GRACE_HOURS: Final[int] = 168


async def _value(session: AsyncSession, key: str) -> Any:
    return await session.scalar(select(Setting.value).where(Setting.key == key))


async def _days(session: AsyncSession, key: str, *, ceiling: int = MAX_DAYS) -> int:
    """One non-negative integer count out of ``settings``, clamped at ``ceiling``."""
    default = int(DEFAULT_SETTINGS[key])
    stored = await _value(session, key)
    if stored is None:
        return default
    # ``True`` is an ``int`` in Python and would silently mean one day.
    if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
        log.warning("setting is not a non-negative integer, using the default", extra={"key": key})
        return default
    return min(stored, ceiling)


async def grace_days(session: AsyncSession) -> int:
    """G, in days (FR-T1, FR-T5)."""
    return await _days(session, GRACE_KEY)


async def unwatched_days(session: AsyncSession) -> int:
    """D, in days (FR-T2, FR-T5)."""
    return await _days(session, UNWATCHED_KEY)


async def watched_grace_hours(session: AsyncSession) -> int:
    """The delete-after-watching grace in hours (FR-T1, M20), at most a week.

    Lenient like the day readers: a missing row or one of the wrong JSON type
    reads as the default (6) with a warning, never as 0.
    """
    return await _days(session, WATCHED_GRACE_KEY, ceiling=MAX_WATCHED_GRACE_HOURS)


async def offline_idle_days(session: AsyncSession) -> int:
    """Days an unfetched offline copy is kept (FR-P6), never fewer than one."""
    return max(await _days(session, OFFLINE_IDLE_KEY), 1)


async def offline_idle_period(session: AsyncSession) -> timedelta:
    """:func:`offline_idle_days` as a ``timedelta``."""
    return timedelta(days=await offline_idle_days(session))


async def grace_period(session: AsyncSession) -> timedelta:
    """G as a ``timedelta``, which is how every caller uses it."""
    return timedelta(days=await grace_days(session))


async def unwatched_period(session: AsyncSession) -> timedelta:
    """D as a ``timedelta``."""
    return timedelta(days=await unwatched_days(session))


__all__ = [
    "GRACE_KEY",
    "MAX_DAYS",
    "MAX_WATCHED_GRACE_HOURS",
    "OFFLINE_IDLE_KEY",
    "UNWATCHED_KEY",
    "WATCHED_GRACE_KEY",
    "grace_days",
    "grace_period",
    "offline_idle_days",
    "offline_idle_period",
    "unwatched_days",
    "unwatched_period",
    "watched_grace_hours",
]
