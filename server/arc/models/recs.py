"""Recommendations: ``rec_runs`` (architecture.md §4, §5.6)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import bigint_pk, created_at


class RecRun(Base):
    """One call to Claude and what came back (FR-R5).

    Runs are kept so the page is instant on reload and so the daily rate
    limit (10/user) can be counted from the table itself.
    """

    __tablename__ = "rec_runs"
    __table_args__ = (
        # Newest run for a user, and the per-day rate-limit count.
        Index("ix_rec_runs_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    #: The user's free-text mood prompt; null when they asked for nothing in
    #: particular (FR-R1).
    prompt: Mapped[str | None] = mapped_column(Text)
    #: The candidate pool sent to the model, so a pick can be explained later.
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    #: [{anilist_id, title, case}] — the structured output (FR-R4).
    picks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at()
