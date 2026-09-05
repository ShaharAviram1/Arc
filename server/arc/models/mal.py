"""MyAnimeList: ``mal_links`` and ``mal_write_log`` (architecture.md §4).

``mal_write_log`` is append-only and is the evidence behind the spec's
top-priority requirement: no write Arc cannot explain, every write revertible
(FR-M5, FR-M7).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk, created_at
from arc.models.enums import MalWriteCause, MalWriteStatus, enum_column


class MalLink(Base):
    """One MAL account per Arc user (FR-M1).

    Tokens are stored Fernet-encrypted (``*_enc``); nothing here is usable
    without ``FERNET_KEY`` (architecture.md §7).
    """

    __tablename__ = "mal_links"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    mal_username: Mapped[str | None] = mapped_column(String(64))
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    #: Access token expiry; the client refreshes ahead of it.
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: Last successful full import (FR-M2, FR-M3).
    last_import_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class MalWriteLog(Base):
    """Every write Arc made to MAL, with the value it replaced (FR-M5)."""

    __tablename__ = "mal_write_log"
    __table_args__ = (
        # The user's own sync page: their log, newest first.
        Index("ix_mal_write_log_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    #: RESTRICT, not CASCADE: this log is the evidence that every MAL write
    #: can be explained and reverted (FR-M5, FR-M7), so dropping a cached
    #: AniList row must not silently delete the history that points at it.
    #: Purging an anime is then a deliberate act that has to deal with its log.
    anime_id: Mapped[int] = mapped_column(
        ForeignKey("anime.id", ondelete="RESTRICT"), nullable=False
    )
    #: "progress" | "status" | "score" — the MAL field that was written.
    field: Mapped[str] = mapped_column(String(32), nullable=False)
    #: JSONB rather than text so an int score and a string status are both
    #: stored as themselves; revert replays ``old_value`` verbatim (§5.5).
    old_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    cause: Mapped[MalWriteCause] = mapped_column(enum_column(MalWriteCause), nullable=False)
    status: Mapped[MalWriteStatus] = mapped_column(
        enum_column(MalWriteStatus),
        nullable=False,
        default=MalWriteStatus.PENDING,
        server_default=MalWriteStatus.PENDING.value,
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
