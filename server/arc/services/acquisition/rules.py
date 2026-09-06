"""Reading the admin-editable acquisition rules out of ``settings`` (FR-A3).

Three global keys — ``preferred_groups``, ``preferred_resolution``,
``fallback_resolution`` — plus the window ``look_ahead_n`` that
:mod:`~arc.services.acquisition.wants` needs, and an optional **per-show
override** under ``override:anime:<id>``.

The override lives in the same key/value table rather than in a column of its
own on ``anime``, because it is a rule and rules live in ``settings`` (spec
FR-A3 allows per-show overrides for group and resolution, and nothing else).
That also means adding one is an insert, not a migration.

Every read falls back to :data:`arc.models.DEFAULT_SETTINGS`, so a database
that has never been seeded still ranks releases the way the spec says it
should. A value of the wrong JSON type is treated as absent and logged rather
than raising: ``settings`` is hand-editable, and one bad row must not stop
acquisition for every show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DEFAULT_SETTINGS, Setting

log = logging.getLogger(__name__)

#: The three global ranking keys, read in one query.
RULE_KEYS: Final[tuple[str, ...]] = (
    "preferred_groups",
    "preferred_resolution",
    "fallback_resolution",
)

#: The window N (FR-A1, FR-T5).
LOOK_AHEAD_KEY: Final[str] = "look_ahead_n"

#: Hard ceiling on N, whatever the table says. N is admin-editable, and an
#: admin who types 200 has asked Arc to torrent a whole show — which FR-A1
#: exists to forbid ("Nothing outside this window is fetched").
MAX_LOOK_AHEAD: Final[int] = 10


def override_key(anime_id: int) -> str:
    """The ``settings`` key holding one show's rule override."""
    return f"override:anime:{anime_id}"


@dataclass(frozen=True, slots=True)
class Rules:
    """The ranking rules in force for one show (FR-A3)."""

    #: Ordered; earlier is better. Compared case-insensitively, because
    #: ``SubsPlease`` and ``subsplease`` are one group.
    preferred_groups: tuple[str, ...] = ()
    preferred_resolution: str | None = None
    fallback_resolution: str | None = None
    #: True when a ``override:anime:<id>`` row contributed. Reported in the
    #: search log so "why did it pick that?" is answerable.
    overridden: bool = False

    def group_rank(self, group: str | None) -> int:
        """Position in :attr:`preferred_groups`; last for anything else."""
        if group is None:
            return len(self.preferred_groups)
        folded = group.casefold()
        for index, preferred in enumerate(self.preferred_groups):
            if preferred.casefold() == folded:
                return index
        return len(self.preferred_groups)

    def resolution_rank(self, resolution: str | None) -> int:
        """0 for the preferred resolution, 1 for the fallback, 2 otherwise."""
        if resolution is None:
            return 2
        folded = resolution.casefold()
        if self.preferred_resolution and folded == self.preferred_resolution.casefold():
            return 0
        if self.fallback_resolution and folded == self.fallback_resolution.casefold():
            return 1
        return 2


def _string_list(value: Any, *, key: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        log.warning("setting is not a list of strings, ignoring", extra={"key": key})
        return None
    return tuple(item for item in value if item.strip())


def _string(value: Any, *, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        log.warning("setting is not a string, ignoring", extra={"key": key})
        return None
    return value or None


async def _values(session: AsyncSession, keys: list[str]) -> dict[str, Any]:
    rows = await session.execute(select(Setting.key, Setting.value).where(Setting.key.in_(keys)))
    return {key: value for key, value in rows.all()}


async def look_ahead_n(session: AsyncSession) -> int:
    """N — how many unwatched episodes ahead to keep (FR-A1, FR-T5)."""
    stored = (await _values(session, [LOOK_AHEAD_KEY])).get(LOOK_AHEAD_KEY)
    default = int(DEFAULT_SETTINGS[LOOK_AHEAD_KEY])
    if stored is None:
        return default
    # ``True`` is an ``int`` in Python and would silently mean N = 1.
    if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
        log.warning("look_ahead_n is not a non-negative integer, using the default")
        return default
    return min(stored, MAX_LOOK_AHEAD)


async def load_rules(session: AsyncSession, anime_id: int | None = None) -> Rules:
    """The rules for ``anime_id``, global rules underneath a show override.

    The override may name either field or both; whatever it omits keeps the
    global value. Its resolution replaces the *preferred* one and leaves the
    fallback alone — "I want 720p for this show" means prefer 720p, not "and
    never accept anything else", because FR-A3's resolution rule is a
    preference and the seeder count below it is what breaks the tie.
    """
    keys = list(RULE_KEYS)
    if anime_id is not None:
        keys.append(override_key(anime_id))
    stored = await _values(session, keys)

    groups = _string_list(stored.get("preferred_groups"), key="preferred_groups")
    preferred = _string(stored.get("preferred_resolution"), key="preferred_resolution")
    fallback = _string(stored.get("fallback_resolution"), key="fallback_resolution")
    if groups is None:
        groups = _string_list(DEFAULT_SETTINGS["preferred_groups"], key="preferred_groups") or ()
    if preferred is None:
        preferred = _string(DEFAULT_SETTINGS["preferred_resolution"], key="preferred_resolution")
    if fallback is None:
        fallback = _string(DEFAULT_SETTINGS["fallback_resolution"], key="fallback_resolution")

    overridden = False
    raw_override = stored.get(override_key(anime_id)) if anime_id is not None else None
    if isinstance(raw_override, dict):
        key = override_key(anime_id) if anime_id is not None else "override"
        over_groups = _string_list(raw_override.get("preferred_groups"), key=key)
        over_resolution = _string(raw_override.get("resolution"), key=key)
        if over_groups is not None:
            groups = over_groups
            overridden = True
        if over_resolution is not None:
            preferred = over_resolution
            overridden = True
    elif raw_override is not None:
        log.warning("per-show override is not an object, ignoring", extra={"anime_id": anime_id})

    return Rules(
        preferred_groups=groups,
        preferred_resolution=preferred,
        fallback_resolution=fallback,
        overridden=overridden,
    )


__all__ = [
    "LOOK_AHEAD_KEY",
    "MAX_LOOK_AHEAD",
    "RULE_KEYS",
    "Rules",
    "load_rules",
    "look_ahead_n",
    "override_key",
]
