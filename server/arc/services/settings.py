"""Reading, validating and writing the admin-editable rules (FR-D2, FR-T5).

Every key in :data:`arc.models.DEFAULT_SETTINGS` is editable at runtime from
the admin panel, and this module is the only place that decides what a valid
value looks like. It is deliberately free of FastAPI: :func:`validate` is a
pure function over two mappings, so the whole validation matrix is testable
without an HTTP client, and the router's job is to turn one exception into a
422.

Three things are worth stating about the rules themselves.

**Validation here is stricter than the rule readers.**
:mod:`arc.services.acquisition.rules` and :mod:`arc.services.retention.rules`
accept anything and fall back to the default with a warning, because they read
a table an operator can edit by hand and one bad row must not stop acquisition
or, worse, turn a grace period of 7 into 0. This module is the *write* path,
which is the one place a mistake can still be reported to the person making
it — so a resolution Arc has never heard of, an N above
:data:`~arc.services.acquisition.rules.MAX_LOOK_AHEAD`, or a language tag that
is not a language tag is refused rather than stored and later ignored.

**A partial write is a partial write.** ``PUT`` carries only the keys the admin
changed; everything else keeps the value it has. The one cross-field rule
(preferred and fallback resolution must differ) is therefore checked against
the *effective* settings — what the table will hold once the patch is applied —
not against the patch alone, or "set the fallback to what the preferred already
is" would slip through whenever the patch names only one of the two. It is
checked **only when the patch names a resolution**, though: a stored pair that
already collides is not something an edit to N should be refused for, under
two field names the admin never sent.

**The kill switch has one writer.** ``acquisition_paused`` is handed to
:func:`~arc.services.acquisition.rules.set_paused` rather than written here, and
clearing it queues the recompute that acts on it — so the rules editor and the
pause button do the same thing.

**Per-show overrides are written through the same validators** (M16, owner
2026-09-18). :func:`write_override` and :func:`delete_override` are the editor
behind the show page's "Release rules for this show" and the Remove button in
Admin → Rules; a row they write is exactly what the global keys would accept
for the same two fields, because the ranker reads them with one set of rules
(:func:`~arc.services.acquisition.rules.load_rules`) and a per-show list of
thirty groups is no more a preference than a global one. An override that
names *neither* field is a **deletion**, not a stored ``{}``: an empty row
would show up in the admin table as a show that does not follow the global
rules while following them exactly.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DEFAULT_SETTINGS, Anime, Setting
from arc.services.acquisition.names import enqueue_compute_wants as queue_wants
from arc.services.acquisition.rules import (
    MAX_LOOK_AHEAD,
    MAX_MIN_FREE_GB,
    MAX_SLOT_CAP,
    OVERRIDE_PREFIX,
    PAUSED_KEY,
    override_key,
    set_paused,
)
from arc.services.catalog import preferred_title

log = logging.getLogger(__name__)

#: Everything an admin may change from the panel — which is every seeded key
#: (architecture.md §9). Spelled as a set of the defaults rather than a second
#: list, so a key added to ``DEFAULT_SETTINGS`` is editable the same day.
EDITABLE_KEYS: Final[frozenset[str]] = frozenset(DEFAULT_SETTINGS)

#: The resolutions Arc's ranker knows (FR-A3). A closed set: the value is
#: compared against what the filename parser reports, so "1080" or "FHD" would
#: match nothing and silently rank every release the same.
RESOLUTIONS: Final[tuple[str, ...]] = ("2160p", "1080p", "720p", "480p")

#: Bounds on ``preferred_groups``. The length matches the ``group`` column in
#: ``torrents``; the count is a sanity limit — a preference list longer than
#: this is not a preference.
MAX_GROUP_LENGTH: Final[int] = 64
MAX_GROUPS: Final[int] = 20

#: Ceiling on G and D as *written*. A year: long enough for any retention
#: policy an admin means, short enough that a stray zero-heavy typo is caught.
#: The readers clamp at :data:`arc.services.retention.rules.MAX_DAYS` (ten
#: years), which is the different question of what a hand-edited row may say.
MAX_DAYS: Final[int] = 365

#: BCP-47-ish: lowercase alphabetic subtags joined by dashes (``en``,
#: ``pt-br``). Not the full grammar — Arc only ever compares these to the
#: language tags ffprobe reports on a subtitle or audio stream.
_LANGUAGE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z]+(?:-[a-z]+)*$")
MIN_LANGUAGE_LENGTH: Final[int] = 2
MAX_LANGUAGE_LENGTH: Final[int] = 8

UNKNOWN_KEY = "not a setting Arc knows"
NOT_A_LIST = "must be a list of strings"
NOT_A_BOOL = "must be true or false"
FALLBACK_EQUALS_PREFERRED = "the fallback resolution must differ from the preferred one"


class SettingsInvalid(ValueError):
    """One or more values were refused. Carries a message per offending key."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors: dict[str, str] = dict(errors)
        super().__init__("; ".join(f"{key}: {message}" for key, message in self.errors.items()))


class NoSuchAnime(LookupError):
    """:func:`write_override` was given an id no ``anime`` row answers to.

    A 404 rather than a 422: the body was fine, the show is not there. Only
    the *write* asks — :func:`delete_override` deliberately does not, because
    an override that outlived the show it names is precisely the row an admin
    needs to be able to take away.
    """

    def __init__(self, anime_id: int) -> None:
        self.anime_id = anime_id
        super().__init__(f"no anime with id {anime_id}")


@dataclass(frozen=True, slots=True)
class Override:
    """One ``override:anime:<id>`` row, as the admin panel lists it."""

    anime_id: int
    #: The show's preferred title, or ``""`` when Arc has no such anime row —
    #: an override outliving the show it names is worth *seeing*, not hiding.
    title: str
    preferred_groups: list[str] | None = None
    resolution: str | None = None


# --- Validation -------------------------------------------------------------


def _one_of(value: Any, allowed: Iterable[str]) -> str:
    choices = tuple(allowed)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"must be one of {', '.join(choices)}")
    return value


def _bounded_int(value: Any, *, low: int, high: int) -> int:
    # ``True`` is an ``int`` in Python, and "look_ahead_n: true" meaning 1 is
    # not a reading anyone intended.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"must be a whole number between {low} and {high}")
    if not low <= value <= high:
        raise ValueError(f"must be between {low} and {high}")
    return value


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(NOT_A_BOOL)
    return value


def _language(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("must be a language tag such as en or pt-br")
    tag = value.strip().lower()
    if not MIN_LANGUAGE_LENGTH <= len(tag) <= MAX_LANGUAGE_LENGTH or not _LANGUAGE_RE.match(tag):
        raise ValueError(
            f"must be a language tag of {MIN_LANGUAGE_LENGTH}–{MAX_LANGUAGE_LENGTH} "
            "lowercase letters and dashes, such as en or pt-br"
        )
    return tag


def _groups(value: Any) -> list[str]:
    """Trim, refuse the empty and the over-long, then de-duplicate.

    De-duplication is case-insensitive because
    :meth:`arc.services.acquisition.rules.Rules.group_rank` compares that way:
    ``SubsPlease`` and ``subsplease`` are one group, and keeping both would put
    a second, unreachable entry in a ranked list. The first spelling wins, so
    the admin sees the casing they typed.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(NOT_A_LIST)
    cleaned: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = raw.strip()
        if not item:
            raise ValueError(f"entry {index + 1} is empty")
        if len(item) > MAX_GROUP_LENGTH:
            raise ValueError(f"entry {index + 1} is longer than {MAX_GROUP_LENGTH} characters")
        folded = item.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        cleaned.append(item)
    if len(cleaned) > MAX_GROUPS:
        raise ValueError(f"at most {MAX_GROUPS} groups")
    return cleaned


#: key → the function that normalises it or raises ``ValueError(message)``.
_VALIDATORS: Final[dict[str, Any]] = {
    "preferred_groups": _groups,
    "preferred_resolution": lambda value: _one_of(value, RESOLUTIONS),
    "fallback_resolution": lambda value: _one_of(value, RESOLUTIONS),
    "look_ahead_n": lambda value: _bounded_int(value, low=0, high=MAX_LOOK_AHEAD),
    # K — shows one user may have fetching at once (FR-A10). 0 is legal and
    # means *unlimited*, unlike N's 0 above, which means "fetch nothing"; the
    # ceiling is the figure the reader clamps at, so the panel cannot accept a
    # cap acquisition would quietly lower.
    "slot_cap_k": lambda value: _bounded_int(value, low=0, high=MAX_SLOT_CAP),
    "grace_days_g": lambda value: _bounded_int(value, low=0, high=MAX_DAYS),
    "unwatched_days_d": lambda value: _bounded_int(value, low=0, high=MAX_DAYS),
    "acquisition_paused": _boolean,
    # The storage floor in whole GB (FR-T6). 0 is a legal value and means "no
    # reserve, fetch until the disk is full"; the ceiling is the same defensive
    # figure the reader clamps at, so what the panel accepts and what
    # acquisition acts on cannot differ.
    "min_free_gb": lambda value: _bounded_int(value, low=0, high=MAX_MIN_FREE_GB),
    "sub_lang": _language,
    "audio_lang": _language,
}


def validate(patch: Mapping[str, Any], current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalise ``patch``, or raise :class:`SettingsInvalid` naming each fault.

    ``current`` is the settings as they stand (defaults when omitted) and is
    used only for the cross-field resolution rule. Returns exactly the keys
    ``patch`` named, with their stored form — trimmed, lowercased and
    de-duplicated — so the caller writes what was validated rather than what
    was sent.
    """
    effective: dict[str, Any] = dict(current if current is not None else DEFAULT_SETTINGS)
    errors: dict[str, str] = {}
    cleaned: dict[str, Any] = {}

    for key, value in patch.items():
        validator = _VALIDATORS.get(key)
        if validator is None:
            errors[key] = UNKNOWN_KEY
            continue
        try:
            cleaned[key] = validator(value)
        except ValueError as exc:
            errors[key] = str(exc)

    # The cross-field rule, and only when this patch is *about* a resolution.
    # A stored pair that already collides — hand-edited, or seeded before the
    # rule existed — must not make every unrelated edit fail, blaming two
    # fields the admin never touched and offering no way out except a request
    # that changes something else entirely. It is logged instead; the ranker
    # treats a fallback equal to the preferred as one resolution, which is
    # untidy rather than wrong.
    resolutions = ("preferred_resolution", "fallback_resolution")
    named = [key for key in resolutions if key in patch]
    if named and not any(key in errors for key in resolutions):
        effective |= cleaned
        if effective.get("preferred_resolution") == effective.get("fallback_resolution"):
            # Blamed on the keys the caller actually sent, so the client can
            # put the message under a field the admin can see.
            for key in named:
                errors[key] = FALLBACK_EQUALS_PREFERRED
    elif not named and effective.get("preferred_resolution") == effective.get(
        "fallback_resolution"
    ):
        log.warning(
            "the stored resolutions are equal; leaving them as they are",
            extra={"resolution": effective.get("preferred_resolution")},
        )

    if errors:
        raise SettingsInvalid(errors)
    return cleaned


def validate_override(preferred_groups: Any = None, resolution: Any = None) -> dict[str, Any]:
    """Normalise one per-show override, or raise :class:`SettingsInvalid`.

    Pure, like :func:`validate`, and deliberately built out of the *same* two
    validators the global keys use: FR-A3 allows a per-show override for group
    and resolution and nothing else, so there is nothing here that a global
    value would not have to satisfy.

    Returns only the fields the caller actually named, in the shape the
    ``override:anime:<id>`` row stores them (``preferred_groups``,
    ``resolution``). **An empty answer means "no override"** — the caller
    deletes the row rather than writing ``{}``. ``None`` is "not named", and so
    is an empty list or a blank string: the show page's groups field is free
    text and its resolution select has a "Use global" entry, which is how an
    admin says "take this show back to the global rules" — the same thing as
    having no override at all, and one fewer state for the ranker to read.
    """
    errors: dict[str, str] = {}
    cleaned: dict[str, Any] = {}

    if isinstance(preferred_groups, list) and not preferred_groups:
        preferred_groups = None
    if preferred_groups is not None:
        try:
            groups = _groups(preferred_groups)
        except ValueError as exc:
            errors["preferred_groups"] = str(exc)
        else:
            # ``_groups`` refuses a blank entry rather than dropping it, so
            # this can only be empty for an empty list — caught above already,
            # and again here so "an empty list is not a preference" is stated
            # beside the field it is about rather than two lines away.
            if groups:
                cleaned["preferred_groups"] = groups

    if isinstance(resolution, str) and not resolution.strip():
        resolution = None
    if resolution is not None:
        try:
            cleaned["resolution"] = _one_of(resolution, RESOLUTIONS)
        except ValueError as exc:
            errors["resolution"] = str(exc)

    if errors:
        raise SettingsInvalid(errors)
    return cleaned


# --- Reading and writing ----------------------------------------------------


def defaults() -> dict[str, Any]:
    """The first-boot values, as a plain mutable dict."""
    return dict(DEFAULT_SETTINGS)


async def read_values(session: AsyncSession) -> dict[str, Any]:
    """Every editable key with its stored value, or the default if unset.

    Stored values are reported **as they are**, without the readers' type
    coercion: this is the admin's view of the table, and a hand-edited row
    that acquisition is quietly ignoring is precisely the thing that view
    exists to make visible.
    """
    rows = await session.execute(
        select(Setting.key, Setting.value).where(Setting.key.in_(sorted(EDITABLE_KEYS)))
    )
    stored = {key: value for key, value in rows.all()}
    return {key: stored.get(key, default) for key, default in DEFAULT_SETTINGS.items()}


def _override(anime_id: int, title: str, value: Mapping[str, Any]) -> Override:
    """One stored row as the API renders it, lenient like the rule readers.

    A field of the wrong JSON type reads as absent rather than raising: these
    rows are hand-editable, and the admin table exists to *show* a bad one.
    """
    groups = value.get("preferred_groups")
    resolution = value.get("resolution")
    return Override(
        anime_id=anime_id,
        title=title,
        preferred_groups=([str(item) for item in groups] if isinstance(groups, list) else None),
        resolution=resolution if isinstance(resolution, str) else None,
    )


async def read_override(session: AsyncSession, anime_id: int) -> Override | None:
    """One show's override, or ``None`` when it follows the global rules.

    One query. The title costs no second one on the show page, which is the
    caller that matters: ``GET /api/anime/{id}`` has already loaded that
    ``Anime`` into this session, and sessions are made with
    ``expire_on_commit=False``, so :meth:`~sqlalchemy.orm.Session.get` answers
    from the identity map.
    """
    value = await session.scalar(select(Setting.value).where(Setting.key == override_key(anime_id)))
    if value is None:
        return None
    if not isinstance(value, dict):
        log.warning("ignoring a malformed per-show override", extra={"anime_id": anime_id})
        return None
    anime = await session.get(Anime, anime_id)
    return _override(anime_id, preferred_title(anime) if anime is not None else "", value)


async def read_overrides(session: AsyncSession) -> list[Override]:
    """Every per-show rule override, newest id first, titles resolved.

    A row whose key does not end in an integer, or whose value is not an
    object, is skipped: it cannot be an override that
    :func:`~arc.services.acquisition.rules.load_rules` would ever apply either.
    """
    rows = await session.execute(
        select(Setting.key, Setting.value).where(Setting.key.startswith(OVERRIDE_PREFIX))
    )
    parsed: list[tuple[int, dict[str, Any]]] = []
    for key, value in rows.all():
        suffix = key[len(OVERRIDE_PREFIX) :]
        if not suffix.isdigit() or not isinstance(value, dict):
            log.warning("ignoring a malformed per-show override", extra={"key": key})
            continue
        parsed.append((int(suffix), value))
    if not parsed:
        return []

    titles: dict[int, str] = {}
    anime_rows = await session.scalars(
        select(Anime).where(Anime.id.in_([anime_id for anime_id, _ in parsed]))
    )
    for anime in anime_rows.all():
        titles[anime.id] = preferred_title(anime)

    return [
        _override(anime_id, titles.get(anime_id, ""), value)
        for anime_id, value in sorted(parsed, key=lambda pair: pair[0])
    ]


async def write_values(
    session: AsyncSession, values: Mapping[str, Any], *, admin_id: int | None = None
) -> list[str]:
    """Upsert ``values``, returning the keys that actually changed.

    Flushed, not committed, like every other writer in ``services`` — the
    caller's transaction is what makes a multi-key edit one act or none.

    One INFO line per changed key, with the previous value and the admin who
    made the change: these are the rules that decide what Arc downloads and
    what it deletes, and "N was 2 this morning" has to be answerable from the
    log rather than from memory.

    ``acquisition_paused`` is not written here but handed to
    :func:`~arc.services.acquisition.rules.set_paused`, and clearing it queues
    a ``compute_wants`` in the same transaction. Otherwise unpausing from the
    rules editor and unpausing from the button would mean two different things
    — the button starts fetching again at once, the editor would leave Arc idle
    until the fifteen-minute tick came round — and a switch that behaves
    differently depending on which screen you flipped it from is a bug waiting
    to be reported as "resume doesn't work".
    """
    keys = sorted(values)
    rows = await session.scalars(select(Setting).where(Setting.key.in_(keys)))
    existing = {row.key: row for row in rows.all()}

    changed: list[str] = []
    for key in keys:
        value = values[key]
        row = existing.get(key)
        old = row.value if row is not None else DEFAULT_SETTINGS.get(key)
        if row is not None and row.value == value:
            continue
        if key == PAUSED_KEY:
            # Logs its own line, in this function's shape.
            await set_paused(session, bool(value), admin_id=admin_id)
            changed.append(key)
            continue
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
        changed.append(key)
        log.info(
            "setting changed",
            extra={"setting": key, "old": old, "new": value, "admin_id": admin_id},
        )
    await session.flush()

    if PAUSED_KEY in changed and values[PAUSED_KEY] is False:
        job = await queue_wants(session)
        log.info(
            "acquisition resumed from the rules editor",
            extra={"job_id": job.id, "admin_id": admin_id},
        )
    return changed


async def write_override(
    session: AsyncSession,
    *,
    anime_id: int,
    preferred_groups: Any = None,
    resolution: Any = None,
    admin_id: int | None = None,
) -> Override | None:
    """Write one show's ``override:anime:<id>`` row, or remove it.

    Validated by :func:`validate_override` — the same two validators the global
    keys use — so a refusal reaches the router as the one
    :class:`SettingsInvalid` it turns into a 422. Raises :class:`NoSuchAnime`
    when nothing answers to ``anime_id``: an override is a rule about a show,
    and a rule about a show Arc has never heard of is a typed id, not a policy.

    An override naming neither field is a **deletion** (see the module
    docstring), and the answer is then ``None`` — the row the caller asked for
    does not exist, which is the honest thing to say about it.

    Flushed, not committed, and logged with the previous value like
    :func:`write_values`, because this is the setting that answers "why did
    this one show get 720p from a group nobody else uses?".

    Nothing is enqueued. A rule change does not queue a recompute either (only
    *clearing the pause* does, and for its own reason): the ranker reads these
    rows when it searches, so the next search for this show already uses the
    new rule, and nothing already downloading is disturbed — which is what the
    rules editor tells the admin in so many words.
    """
    value = validate_override(preferred_groups, resolution)
    anime = await session.get(Anime, anime_id)
    if anime is None:
        raise NoSuchAnime(anime_id)
    if not value:
        await delete_override(session, anime_id=anime_id, admin_id=admin_id)
        return None

    key = override_key(anime_id)
    row = await session.scalar(select(Setting).where(Setting.key == key))
    old = row.value if row is not None else None
    if row is None:
        session.add(Setting(key=key, value=value))
    elif old == value:
        # Nothing changed. No write, and no log line claiming one.
        return _override(anime_id, preferred_title(anime), value)
    else:
        row.value = value
    await session.flush()
    log.info(
        "per-show override changed",
        extra={
            "setting": key,
            "anime_id": anime_id,
            "old": old,
            "new": value,
            "admin_id": admin_id,
        },
    )
    return _override(anime_id, preferred_title(anime), value)


async def delete_override(
    session: AsyncSession, *, anime_id: int, admin_id: int | None = None
) -> bool:
    """Remove one show's override row; True when there was one to remove.

    Idempotent, and it does not ask whether the show exists. Both for the same
    reason: the caller is a person pressing Remove on a row, and the two ways
    that row can already be gone — somebody else removed it, the show itself
    was deleted from under it — are not errors to report but the state they
    wanted.
    """
    key = override_key(anime_id)
    row = await session.scalar(select(Setting).where(Setting.key == key))
    if row is None:
        return False
    old = row.value
    await session.delete(row)
    await session.flush()
    log.info(
        "per-show override removed",
        extra={"setting": key, "anime_id": anime_id, "old": old, "admin_id": admin_id},
    )
    return True


__all__ = [
    "EDITABLE_KEYS",
    "FALLBACK_EQUALS_PREFERRED",
    "MAX_DAYS",
    "MAX_GROUPS",
    "MAX_GROUP_LENGTH",
    "MAX_LANGUAGE_LENGTH",
    "MIN_LANGUAGE_LENGTH",
    "NOT_A_BOOL",
    "NOT_A_LIST",
    "RESOLUTIONS",
    "UNKNOWN_KEY",
    "NoSuchAnime",
    "Override",
    "SettingsInvalid",
    "defaults",
    "delete_override",
    "read_override",
    "read_overrides",
    "read_values",
    "validate",
    "validate_override",
    "write_override",
    "write_values",
]
