"""Reading the admin-editable acquisition rules out of ``settings`` (FR-A3).

Three global keys — ``preferred_groups``, ``preferred_resolution``,
``fallback_resolution`` — plus the window ``look_ahead_n`` that
:mod:`~arc.services.acquisition.wants` needs, the kill switch
``acquisition_paused``, the storage floor ``min_free_gb`` (FR-T6), the per-user
slot cap ``slot_cap_k`` (FR-A10), the batch fallback ``batch_fallback``
(FR-A11), and an optional **per-show override** under ``override:anime:<id>``.

**The slot cap** (FR-A10, owner 2026-09-13) is read here and applied in
:mod:`arc.services.acquisition.slots`, which is where the rule and the argument
for it live. Only the number is this module's business, and the one thing worth
saying about it here is that **0 means unlimited** — the opposite of what 0
means for ``look_ahead_n`` two lines above, and the reason both readers spell
their zero out.

**The storage guard** (FR-T6, owner 2026-09-13) is the second brake beside the
pause, and the difference between them is who presses it. A pause is an admin's
decision and stays until it is taken back; a *hold* is the disk's, it needs
nobody's attention, and it lifts itself the moment retention frees room.
:func:`storage_hold` is pure — free bytes against a floor — so the matrix is a
table in the tests rather than a filesystem; :func:`is_storage_held` is the
same rule with the measurement in front of it.

What a hold stops is narrower than a pause. ``compute_wants`` still reconciles,
because everything it does when space is short *frees* space: wants are
dropped, rows are shelved, downloads nobody wants any more are cancelled with
their partial files. What it will not do is start a new search
(``_start_searches``), and ``search_release`` requeues itself exactly as it
does while paused. Ingest and transcodes are untouched: bytes already on the
disk are better finished than abandoned half-transcoded, and the finishing is
what lets retention delete the source.

A measurement that fails **never holds**. The floor is a statement about free
space, and "I could not read the filesystem" is not a small number — treating
it as one would stop Arc fetching for ever on a machine whose data directory
had been renamed.

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

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import DEFAULT_SETTINGS, Setting
from arc.services.storage import disk_usage

log = logging.getLogger(__name__)

#: The three global ranking keys, read in one query.
RULE_KEYS: Final[tuple[str, ...]] = (
    "preferred_groups",
    "preferred_resolution",
    "fallback_resolution",
)

#: The window N (FR-A1, FR-T5).
LOOK_AHEAD_KEY: Final[str] = "look_ahead_n"

#: K — how many shows one user may have fetching at once (FR-A10). 0 is no cap
#: at all, which is the opposite of what 0 means for N above; the rule and the
#: argument for it are in :mod:`arc.services.acquisition.slots`.
SLOT_CAP_KEY: Final[str] = "slot_cap_k"

#: The kill switch. While it is true :func:`compute_wants` does nothing at all
#: and every ``search_release`` requeues itself untouched, so nothing new is
#: asked of Nyaa or qBittorrent and no episode changes state. Deliberately
#: *not* read by ``poll_qbit``: a download already in flight still finishes and
#: is still handed to the library, because stranding half a gigabyte of bytes
#: in the client is not what "stop fetching" means.
PAUSED_KEY: Final[str] = "acquisition_paused"

#: The storage floor, in whole GB (FR-T6). Below this much free space on the
#: data volume acquisition holds itself; 0 turns the guard off, which is the
#: honest reading of "keep no free space in reserve".
MIN_FREE_KEY: Final[str] = "min_free_gb"

#: Whether a finished show with no acceptable single may take a batch and
#: download only the wanted episode's file (FR-A4's exception, FR-A11). The
#: kill switch for it, read in exactly one place —
#: ``search_release``'s batch branch — so turning it off leaves every other
#: path byte-identical rather than merely quieter. A batch already in flight is
#: unaffected: the switch decides whether a *new* one is taken, the same way
#: the pause decides whether a new search runs.
BATCH_FALLBACK_KEY: Final[str] = "batch_fallback"

#: Hard ceiling on N, whatever the table says. N is admin-editable, and an
#: admin who types 200 has asked Arc to torrent a whole show — which FR-A1
#: exists to forbid ("Nothing outside this window is fetched").
MAX_LOOK_AHEAD: Final[int] = 10

#: Ceiling on the floor. A terabyte of reserve is not a reserve, it is a
#: mistyped figure that would hold acquisition for ever on any machine Arc
#: runs on.
MAX_MIN_FREE_GB: Final[int] = 1000

#: Ceiling on K (FR-A10). Fifty simultaneous shows is not a cap, and past that
#: the honest setting is 0. Here rather than in
#: :mod:`arc.services.acquisition.slots` so that every admin-editable bound
#: lives beside the reader that clamps it, and so the rule module stays a leaf
#: nothing in ``services`` has to import to know what a number may be.
MAX_SLOT_CAP: Final[int] = 50

#: What one GB means here: binary, like ``df`` and like every other byte
#: figure in the admin panel.
BYTES_PER_GB: Final[int] = 1024**3

#: What every per-show override key starts with. Named so that the admin panel
#: can *list* the overrides (:func:`arc.services.settings.read_overrides`)
#: without a second copy of the string to keep in step with this one.
OVERRIDE_PREFIX: Final[str] = "override:anime:"


def override_key(anime_id: int) -> str:
    """The ``settings`` key holding one show's rule override."""
    return f"{OVERRIDE_PREFIX}{anime_id}"


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


def _whole_number(stored: Any, *, key: str, ceiling: int) -> int:
    """One stored count, type-checked, floored at 0 and clamped at ``ceiling``.

    The shared body of the three numeric readers here. Lenient on purpose: a
    hand-edited row of the wrong type, or a negative one, reads as the default
    with a warning rather than raising — the write path
    (:func:`arc.services.settings.validate`) is where a mistake can still be
    reported to the person making it.
    """
    default = int(DEFAULT_SETTINGS[key])
    if stored is None:
        return default
    # ``True`` is an ``int`` in Python, and "look_ahead_n: true" meaning N = 1
    # is not a reading anyone intended.
    if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
        log.warning("setting is not a non-negative integer, using the default", extra={"key": key})
        return default
    return min(stored, ceiling)


async def look_ahead_n(session: AsyncSession) -> int:
    """N — how many unwatched episodes ahead to keep (FR-A1, FR-T5)."""
    stored = (await _values(session, [LOOK_AHEAD_KEY])).get(LOOK_AHEAD_KEY)
    return _whole_number(stored, key=LOOK_AHEAD_KEY, ceiling=MAX_LOOK_AHEAD)


async def slot_cap_k(session: AsyncSession) -> int:
    """K — how many shows one user may have fetching at once (FR-A10).

    Lenient and clamped like the rest. ``0`` is returned as ``0`` and means
    *unlimited*: it is the honest reading of "no cap", and it is what the cap
    has to mean for an installation that does not want one.
    """
    stored = (await _values(session, [SLOT_CAP_KEY])).get(SLOT_CAP_KEY)
    return _whole_number(stored, key=SLOT_CAP_KEY, ceiling=MAX_SLOT_CAP)


async def look_ahead_and_cap(session: AsyncSession) -> tuple[int, int]:
    """``(N, K)`` in **one** query, for the reconciler that needs both.

    The two are read together on every ``compute_wants`` and on every show page
    that carries a slot picture, and they live in the same table: two round
    trips for two rows of the same four-row query is a cost with nothing to
    show for it. The clamping is the readers' own, so this cannot disagree with
    them.
    """
    stored = await _values(session, [LOOK_AHEAD_KEY, SLOT_CAP_KEY])
    return (
        _whole_number(stored.get(LOOK_AHEAD_KEY), key=LOOK_AHEAD_KEY, ceiling=MAX_LOOK_AHEAD),
        _whole_number(stored.get(SLOT_CAP_KEY), key=SLOT_CAP_KEY, ceiling=MAX_SLOT_CAP),
    )


async def is_paused(session: AsyncSession) -> bool:
    """Whether acquisition is paused.

    A missing row reads as *not* paused, which is both the documented default
    and the only safe way round: a key that has not been seeded yet must not
    silently stop Arc fetching, whereas a pause that has to be set again after
    a migration is a button an admin can press. A value of the wrong JSON type
    is ignored and logged, like every other rule here — one hand-edited row
    must not decide whether acquisition runs.
    """
    stored = (await _values(session, [PAUSED_KEY])).get(PAUSED_KEY)
    if stored is None:
        return bool(DEFAULT_SETTINGS[PAUSED_KEY])
    if not isinstance(stored, bool):
        log.warning("acquisition_paused is not a boolean, using the default")
        return bool(DEFAULT_SETTINGS[PAUSED_KEY])
    return stored


async def set_paused(session: AsyncSession, paused: bool, *, admin_id: int | None = None) -> bool:
    """Set the kill switch, inserting the row if it is not there yet.

    Flushed, not committed: the caller's transaction is what makes "resume, and
    queue the recompute that acts on it" one act or none.

    The **only** writer of this key. The pause/resume buttons and the rules
    editor (:func:`arc.services.settings.write_values`) both come through here,
    so the two cannot drift into writing it differently — and both leave the
    same audit line, with the previous value and the admin who changed it, in
    the shape :func:`~arc.services.settings.write_values` uses for every other
    rule. This one decides whether Arc downloads anything at all, so "who
    stopped acquisition, and when?" has to be answerable from the log.
    """
    row = await session.get(Setting, PAUSED_KEY)
    old = row.value if row is not None else DEFAULT_SETTINGS[PAUSED_KEY]
    if row is None:
        session.add(Setting(key=PAUSED_KEY, value=paused))
    else:
        row.value = paused
    await session.flush()
    log.info(
        "setting changed",
        extra={"setting": PAUSED_KEY, "old": old, "new": paused, "admin_id": admin_id},
    )
    return paused


async def batch_fallback(session: AsyncSession) -> bool:
    """Whether a batch may be taken for a finished show (FR-A4, FR-A11).

    Lenient in the same direction as :func:`is_paused` and for the same
    reason — a missing or hand-mangled row reads as the documented default,
    which here is **on** — but the consequence is the opposite way round: a
    default that failed closed would silently take the feature away from an
    installation that never touched the key, and the honest place to turn it
    off is the admin panel.
    """
    stored = (await _values(session, [BATCH_FALLBACK_KEY])).get(BATCH_FALLBACK_KEY)
    if stored is None:
        return bool(DEFAULT_SETTINGS[BATCH_FALLBACK_KEY])
    if not isinstance(stored, bool):
        log.warning("batch_fallback is not a boolean, using the default")
        return bool(DEFAULT_SETTINGS[BATCH_FALLBACK_KEY])
    return stored


async def min_free_gb(session: AsyncSession) -> int:
    """The storage floor in whole GB (FR-T6), clamped and type-checked.

    Lenient like every other reader here: a hand-edited row of the wrong type,
    or a negative one, reads as the default rather than raising. Clamped at
    :data:`MAX_MIN_FREE_GB` for the same reason N is clamped — a figure nobody
    can satisfy is not a policy.
    """
    stored = (await _values(session, [MIN_FREE_KEY])).get(MIN_FREE_KEY)
    return _whole_number(stored, key=MIN_FREE_KEY, ceiling=MAX_MIN_FREE_GB)


async def min_free_bytes(session: AsyncSession) -> int:
    """:func:`min_free_gb` in bytes, which is what the rule compares."""
    return await min_free_gb(session) * BYTES_PER_GB


def storage_hold(free_bytes: int, floor_bytes: int) -> bool:
    """Whether acquisition should hold itself: free space under the floor.

    Pure, so the whole of FR-T6's arithmetic is a table in the tests.

    A floor of zero never holds, whatever the disk says — that is what an
    admin asking for no reserve means, and it keeps "the guard is off" and "the
    disk is full" from sharing an answer. Exactly *at* the floor is not held
    either: the floor is the amount that must be left free, and leaving exactly
    that much has left it.
    """
    if floor_bytes <= 0:
        return False
    return free_bytes < floor_bytes


#: Set once the first unreadable ``DATA_DIR`` has been logged, and **cleared by
#: the next successful measurement**. The guard runs every fifteen minutes and
#: on every search, so a directory that stays renamed would otherwise put a
#: WARNING in the log four thousand times a day; but a transient failure — a
#: volume remounting, an NFS blip — must not silence the line for the rest of
#: the worker's life, because the *next* fault is the one somebody needs to
#: see. One line per episode of trouble, in other words.
_measure_failure_logged = False


async def is_storage_held(session: AsyncSession, settings: Settings) -> bool:
    """Whether the data volume is below the floor right now (FR-T6).

    The floor is read first because reading it is cheap and a floor of zero
    settles the question without touching the filesystem at all.

    ``shutil.disk_usage`` is a blocking syscall, so it goes to a worker thread
    — this is called from inside a job's transaction, and a reconciliation must
    not block the loop on a filesystem that is thinking about it.

    A measurement that could not be taken answers **False**: see the module
    docstring. It is logged once and then quietly, because a hold that nobody
    asked for and nothing lifts is the worst of the available failures.
    """
    global _measure_failure_logged

    floor = await min_free_bytes(session)
    if floor <= 0:
        return False
    usage = await asyncio.to_thread(disk_usage, settings.data_dir)
    if usage is None:
        if not _measure_failure_logged:
            _measure_failure_logged = True
            log.warning(
                "could not measure free space; acquisition is not held",
                extra={"path": str(settings.data_dir)},
            )
        return False
    # Measured again, so the next spell of trouble gets its own warning.
    _measure_failure_logged = False
    held = storage_hold(usage.free, floor)
    if held:
        log.info(
            "acquisition held: free space is below the floor",
            extra={"free_bytes": usage.free, "min_free_bytes": floor},
        )
    return held


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
    "BATCH_FALLBACK_KEY",
    "BYTES_PER_GB",
    "LOOK_AHEAD_KEY",
    "MAX_LOOK_AHEAD",
    "MAX_MIN_FREE_GB",
    "MAX_SLOT_CAP",
    "MIN_FREE_KEY",
    "OVERRIDE_PREFIX",
    "PAUSED_KEY",
    "RULE_KEYS",
    "SLOT_CAP_KEY",
    "Rules",
    "batch_fallback",
    "is_paused",
    "is_storage_held",
    "load_rules",
    "look_ahead_and_cap",
    "look_ahead_n",
    "min_free_bytes",
    "min_free_gb",
    "override_key",
    "set_paused",
    "slot_cap_k",
    "storage_hold",
]
