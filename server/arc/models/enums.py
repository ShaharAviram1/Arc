"""Enumerations shared by the models.

All of these are stored as ``VARCHAR(32)`` rather than native Postgres enum
types: adding a value to a native enum needs its own DDL and cannot be done
inside a transaction on older servers, which makes migrations awkward for no
real gain at this scale. ``sa.Enum(..., native_enum=False)`` gives the same
Python-side round-tripping (a query returns the enum member, not a bare
string) with a plain varchar column underneath and no check constraint.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum

ENUM_LENGTH = 32


class UserRole(StrEnum):
    """spec §2 — two roles, nothing in between."""

    ADMIN = "admin"
    USER = "user"


class EpisodeState(StrEnum):
    """spec §6 — the episode lifecycle.

    ``not_wanted → wanted → searching → downloading → downloaded →
    matching → (review) → matched → preparing → ready``; retention returns a
    ready episode to ``not_wanted``; the retry window ends in ``unavailable``;
    a transcode error lands in ``failed``.
    """

    NOT_WANTED = "not_wanted"
    WANTED = "wanted"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    MATCHING = "matching"
    MATCHED = "matched"
    PREPARING = "preparing"
    READY = "ready"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class ListStatus(StrEnum):
    """spec §4.6 FR-W2 — the five list states, MAL's vocabulary."""

    WATCHING = "watching"
    PLANNED = "planned"
    ON_HOLD = "on_hold"
    DROPPED = "dropped"
    COMPLETED = "completed"


class ReviewState(StrEnum):
    """Match-review state of a media file (spec §4.3 FR-L4, FR-L6).

    ``auto`` is a file the matcher linked on its own (confidence over the
    threshold); it never enters the queue. ``pending`` is the queue itself.
    """

    AUTO = "auto"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    IGNORED = "ignored"


class JobStatus(StrEnum):
    """Lifecycle of a row in ``jobs`` (architecture.md §4)."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MalWriteCause(StrEnum):
    """Why Arc wrote to MAL (spec §4.7 FR-M4, FR-M7).

    There is no fourth value on purpose: every write must be traceable to a
    user-originated event or an explicit revert.
    """

    WATCH = "watch"
    MANUAL = "manual"
    REVERT = "revert"


class MalWriteStatus(StrEnum):
    """Outcome of a MAL write attempt (spec §4.7 FR-M6)."""

    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"


class UpdatedBy(StrEnum):
    """Which side last changed a list entry (architecture.md §5.5)."""

    ARC = "arc"
    MAL = "mal"


def enum_column(enum_cls: type[StrEnum]) -> SAEnum:
    """A varchar column that round-trips ``enum_cls`` members.

    ``values_callable`` matters: by default SQLAlchemy stores the enum
    *member name* (``NOT_WANTED``), and we want the value (``not_wanted``)
    so the column reads the same as the spec and the API.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=ENUM_LENGTH,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )
