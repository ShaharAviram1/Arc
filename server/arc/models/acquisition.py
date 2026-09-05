"""Acquisition: ``wants`` and ``torrents`` (architecture.md §4, §5.1).

A ``Want`` is one user's interest in one episode. Wants from all users merge:
an episode with any live want is fetched once (FR-A2). A ``Torrent`` is the
Nyaa release picked for an episode and handed to qBittorrent.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk, created_at


class Want(Base):
    """(user, episode) → "this user wants this episode" (FR-A1).

    Rows are not deleted when a want lapses: ``dropped_at`` plus
    ``drop_reason`` records why, which retention needs (FR-T2) and which
    makes "why was this never fetched?" answerable.
    """

    __tablename__ = "wants"
    __table_args__ = (
        # The acquisition question is always "does this episode still have a
        # live want?", so the index covers only rows that are still live.
        Index(
            "ix_wants_episode_id_active",
            "episode_id",
            postgresql_where=text("dropped_at IS NULL"),
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = created_at()
    #: Null while the want is live. Set when the show is dropped/completed or
    #: the episode went unwatched for D days (FR-T2).
    dropped_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    drop_reason: Mapped[str | None] = mapped_column(String(64))


class Torrent(Base):
    """The Nyaa release chosen for an episode and its qBittorrent state."""

    __tablename__ = "torrents"
    __table_args__ = (Index("ix_torrents_episode_id", "episode_id"),)

    id: Mapped[int] = bigint_pk()
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    #: 40 hex chars for a v1 hash, 64 for v2; sized for the longer one.
    #: Unique: the hash *is* the torrent's identity, and qBittorrent keys its
    #: own state by it, so two rows for one hash would be two views of one
    #: download. A batch release picked for two episodes is the case this
    #: forbids; those are handled by one torrent row and several media files.
    info_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    magnet: Mapped[str | None] = mapped_column(Text)
    #: The raw release title, kept for debugging the ranker.
    title: Mapped[str | None] = mapped_column(Text)
    #: Release group, as parsed. Quoted in SQL: ``group`` is a keyword.
    group: Mapped[str | None] = mapped_column("group", String(64))
    resolution: Mapped[str | None] = mapped_column(String(16))
    #: Seeders *at pick time* — a ranking input, not a live figure (FR-A3).
    seeders: Mapped[int | None] = mapped_column(Integer)
    trusted: Mapped[bool | None] = mapped_column(Boolean)
    #: qBittorrent's own state string (downloading, stalledDL, …).
    qbit_state: Mapped[str | None] = mapped_column(String(32))
    #: 0..1 download progress, polled every 60 s (§5.1 step 4).
    progress: Mapped[float | None] = mapped_column(Float)
    added_at: Mapped[datetime] = created_at()
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
