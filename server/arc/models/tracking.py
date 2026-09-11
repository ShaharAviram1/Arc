"""Per-user tracking: ``list_entries`` and ``watch_progress``.

Both are keyed by (user, thing): a user's view of a show and of an episode.
Everything acquisition and MAL sync do starts from these two tables
(architecture.md §5.1, §5.5).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, false, text
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, updated_at
from arc.models.enums import ListStatus, UpdatedBy, enum_column

#: One user's rows, newest first. Named as a constant for the same reason
#: :data:`arc.models.job.TRANSCODE_EPISODE_INDEX` is: the migration that
#: creates it and the test that proves it survived both spell it, and three
#: spellings of one index is how an index quietly disappears.
#:
#: Since 2026-09-11 it no longer covers "continue watching": that query dropped
#: its ``completed`` filter so a rewatch left half-way appears (FR-W1), and a
#: partial index cannot serve a query that does not carry its predicate. The
#: full ``ix_watch_progress_user_id_updated_at`` below answers it instead —
#: same columns, same order, without the predicate. This one is kept rather
#: than dropped because dropping an index is a schema change, and it still fits
#: any question that does ask only for unfinished rows.
IN_PROGRESS_INDEX = "ix_watch_progress_in_progress"

#: ``DESC`` because the query orders that way, and a descending scan of an
#: ascending index costs a sort on a partial index Postgres would otherwise
#: walk straight. Written as SQL rather than as a column list because an
#: ordering is not something ``Index("…", "updated_at")`` can express.
IN_PROGRESS_EXPRESSION = "updated_at DESC"

#: The partial predicate. Completion is sticky (FR-S4), so the rows this index
#: excludes are excluded for good — the index stays the size of what a user is
#: part-way through rather than of everything they have ever watched.
IN_PROGRESS_PREDICATE = "completed = false"


class ListEntry(Base):
    """(user, anime) → status, progress, score (spec §3, FR-W2).

    This is the row MAL sync argues with: ``updated_by`` and ``mal_dirty``
    together say whether Arc or MAL made the last change and whether it still
    needs pushing (FR-M3, FR-M4).
    """

    __tablename__ = "list_entries"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    anime_id: Mapped[int] = mapped_column(
        ForeignKey("anime.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[ListStatus] = mapped_column(enum_column(ListStatus), nullable=False)
    #: Episodes watched, MAL's definition: the highest completed number.
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: 1–10, or null for "not scored".
    score: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = updated_at()
    #: Which side made the last change (§5.5 step 4 picks a winner with it).
    updated_by: Mapped[UpdatedBy] = mapped_column(
        enum_column(UpdatedBy),
        nullable=False,
        default=UpdatedBy.ARC,
        server_default=UpdatedBy.ARC.value,
    )
    mal_synced_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: True when Arc holds a change MAL has not been told about yet.
    mal_dirty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class WatchProgress(Base):
    """(user, episode) → where the player got to (FR-S3, FR-S4)."""

    __tablename__ = "watch_progress"
    __table_args__ = (
        # "Continue watching", most recent first, for one user (FR-W1).
        Index("ix_watch_progress_user_id_updated_at", "user_id", "updated_at"),
        # The same columns restricted to unfinished rows. No longer what
        # :func:`arc.services.playback.progress.continue_watching` reads — see
        # :data:`IN_PROGRESS_INDEX` — and kept because removing it is a schema
        # change rather than because anything now depends on it.
        Index(
            IN_PROGRESS_INDEX,
            "user_id",
            text(IN_PROGRESS_EXPRESSION),
            postgresql_where=text(IN_PROGRESS_PREDICATE),
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    position_s: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    #: The duration the client reported; null before the player knows it.
    duration_s: Mapped[float | None] = mapped_column(Float)
    #: Set once at ≥ 90 % and never unset automatically (FR-S4).
    completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    #: When ``completed`` first flipped true, written once alongside it and
    #: never moved afterwards. Retention measures the grace window G from this
    #: rather than from ``updated_at`` (FR-T1): ``updated_at`` moves whenever
    #: the player scrubs back through an already-finished episode, which would
    #: keep pushing the deletion date away for as long as anyone rewatches.
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = updated_at()
