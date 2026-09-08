"""The two retention windows, read out of ``settings`` (FR-T5).

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

#: Hard ceiling on either window, whatever the table says. Ten years: the
#: point is to catch a hand-edited row that would otherwise mean "never", not
#: to second-guess an admin who wants a long grace period.
MAX_DAYS: Final[int] = 3650


async def _value(session: AsyncSession, key: str) -> Any:
    return await session.scalar(select(Setting.value).where(Setting.key == key))


async def _days(session: AsyncSession, key: str) -> int:
    """One non-negative integer day count out of ``settings``."""
    default = int(DEFAULT_SETTINGS[key])
    stored = await _value(session, key)
    if stored is None:
        return default
    # ``True`` is an ``int`` in Python and would silently mean one day.
    if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
        log.warning("setting is not a non-negative integer, using the default", extra={"key": key})
        return default
    return min(stored, MAX_DAYS)


async def grace_days(session: AsyncSession) -> int:
    """G, in days (FR-T1, FR-T5)."""
    return await _days(session, GRACE_KEY)


async def unwatched_days(session: AsyncSession) -> int:
    """D, in days (FR-T2, FR-T5)."""
    return await _days(session, UNWATCHED_KEY)


async def grace_period(session: AsyncSession) -> timedelta:
    """G as a ``timedelta``, which is how every caller uses it."""
    return timedelta(days=await grace_days(session))


async def unwatched_period(session: AsyncSession) -> timedelta:
    """D as a ``timedelta``."""
    return timedelta(days=await unwatched_days(session))


__all__ = [
    "GRACE_KEY",
    "MAX_DAYS",
    "UNWATCHED_KEY",
    "grace_days",
    "grace_period",
    "unwatched_days",
    "unwatched_period",
]
