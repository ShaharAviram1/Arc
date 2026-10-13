"""Per-user show pins (FR-T8, M20 T8).

A pin is a user's choice to keep a show's files after watching. Delete after
watching (FR-T1) skips every episode of a show any user has pinned; only the
disk floor (FR-T6) may still remove them, as a last resort, least recently
watched first.

The table is the whole of the state: a row means "pinned", no row means "not
pinned". The pair is the key, so pinning twice is the same row, and both
foreign keys cascade, so a deleted account or show takes its pins with it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import created_at


class ShowPin(Base):
    """One user's "Keep files" on one show."""

    __tablename__ = "show_pins"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    anime_id: Mapped[int] = mapped_column(
        ForeignKey("anime.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = created_at()


__all__ = ["ShowPin"]
