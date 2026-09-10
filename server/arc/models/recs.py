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
    """One call to the recommendation model and what came back (FR-R5).

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
    #: The candidate pool as sent, so a pick can be explained later — and so a
    #: prompt change can be evaluated against what a past run actually saw.
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    #: Everything the page shows, in one list, each entry tagged with ``kind``:
    #: ``{kind: "pick", anime_id, title, case}`` for the model's picks (FR-R4)
    #: and ``{kind: "continuation", anime_id, title, because}`` for the
    #: deterministic "new in your franchises" section. One column rather than
    #: two because they are one page and a second column would need a
    #: migration; a row written before the tag existed carries no ``kind`` and
    #: reads as a pick, which is what it was.
    #:
    #: Keyed by Arc's **internal** id throughout. Not AniList's, which §5.6
    #: originally specified: the catalogue moved to internal ids in M3b and a
    #: cached row is allowed to have no AniList id at all.
    picks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    #: The model that answered, as the provider reported it — which is not
    #: necessarily the one that was asked for (Anthropic's server-side
    #: fallbacks can substitute one).
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at()
