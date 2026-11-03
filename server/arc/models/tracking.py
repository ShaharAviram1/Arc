"""Per-user tracking: ``list_entries``, ``watch_progress``, ``rewatches``, ``walks``.

The first two are keyed by (user, thing): a user's view of a show and of an
episode. Everything acquisition and MAL sync do starts from them
(architecture.md §5.1, §5.5). ``rewatches`` holds a user's rewatch passes of a
show (FR-W7, M21) and ``walks`` where a user's look-ahead on a show starts
after a play (FR-A1, M21).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, false, text
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk, updated_at
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
    #: When the user first touched this show **in Arc** (FR-A9): a status,
    #: progress or score change, a completed episode, or a "try episode 1".
    #: Null means nobody has — which is what a row a MyAnimeList import created
    #: and nobody has acted on since looks like, and the one state that
    #: generates no acquisition wants
    #: (:func:`arc.services.acquisition.wants.is_dormant`).
    #:
    #: Write-once and never cleared: FR-A9's "activation never expires". The
    #: MAL import, push and reconcile paths leave it exactly as they find it,
    #: which is the whole point — MyAnimeList moving a score is not the user
    #: asking Arc to download 400 episodes.
    activated_at: Mapped[datetime | None] = mapped_column(TZDateTime)


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
    #: When the user last took the watched mark back (FR-S4's explicit un-mark),
    #: never moving backwards. Kept as history once a later completion lands:
    #: it is what lets an offline completion replayed later (FR-S8) be judged
    #: against an un-mark made after it, elsewhere (owner, 2026-10-05).
    unmarked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = updated_at()
    #: The **latest** moment this user crossed the completion mark (90 %) or
    #: marked the episode watched by hand, in any mode — normal or a rewatch
    #: pass (FR-S4, FR-W7, M21). Stamped again on every crossing, never moved
    #: backwards by a late replay, and cleared by an explicit un-mark (inside a
    #: rewatch pass only when it falls within that pass). :attr:`completed_at`
    #: stays the once-written *first* watch; this is the one that ends a demand
    #: (``finished_at >= wants.demanded_at``), counts a rewatch pass
    #: (``finished_at >= rewatches.started_at``) and anchors retention's
    #: watched rule. Backfilled from ``completed_at`` where ``completed``.
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)


#: The two values of :attr:`Rewatch.end_reason`: the user turned rewatch mode
#: off, or the pass reached its last episode (the show's last, or the newest
#: aired one of an airing show).
REWATCH_END_OFF = "off"
REWATCH_END_FINISHED = "finished"

#: At most one open pass per user and show. A partial unique index rather than
#: a check in code, for the reason :data:`arc.models.trips.ONE_ACTIVE_TRIP_INDEX`
#: gives: two requests racing the start path's own check can both pass it.
OPEN_REWATCH_INDEX = "uq_rewatches_open"
OPEN_REWATCH_PREDICATE = "ended_at IS NULL"

#: "The pass of (user, show) open at time t" and "this user's passes": the
#: user prefix serves the second, the full key the first.
REWATCH_LOOKUP_INDEX = "ix_rewatches_user_id_anime_id_started_at"


class Rewatch(Base):
    """One rewatch pass of one show by one user (FR-W7, M21).

    Open while ``ended_at`` is null. Ended rows are kept: an offline report
    replayed later is judged against the pass that was open when the device
    recorded it (FR-S8). There is no cursor column — the pass's position is
    the highest episode number whose ``watch_progress.finished_at`` is at or
    after :attr:`started_at`. Written only by
    ``arc.services.playback.rewatch``.
    """

    __tablename__ = "rewatches"
    __table_args__ = (
        Index(
            OPEN_REWATCH_INDEX,
            "user_id",
            "anime_id",
            unique=True,
            postgresql_where=text(OPEN_REWATCH_PREDICATE),
        ),
        Index(REWATCH_LOOKUP_INDEX, "user_id", "anime_id", "started_at"),
    )

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    anime_id: Mapped[int] = mapped_column(
        ForeignKey("anime.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    #: Null while the pass is open.
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: :data:`REWATCH_END_OFF` or :data:`REWATCH_END_FINISHED`; null while open.
    end_reason: Mapped[str | None] = mapped_column(String(16))


class Walk(Base):
    """Where one user's look-ahead on one show starts after a play (FR-A1, M21).

    ``after_number`` is k−1 while episode k plays and k once it is finished;
    the window takes the next N aired episodes after it. Written only by
    ``record_progress``, and only by a report whose time is at or after
    :attr:`touched_at`, so an older offline replay does not move it. Stored
    rather than derived from ``watch_progress`` so the progress rows from
    before M21 cannot start walks on long-dropped shows: walks start empty.
    """

    __tablename__ = "walks"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    anime_id: Mapped[int] = mapped_column(
        ForeignKey("anime.id", ondelete="CASCADE"), primary_key=True
    )
    after_number: Mapped[int] = mapped_column(Integer, nullable=False)
    touched_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
