"""Admin-editable rules: ``settings`` (architecture.md §4, §9).

Anything an admin can change at runtime lives here rather than in the
environment: the acquisition window N, the retention windows G and D, group
and resolution preferences, languages (FR-D2, FR-T5). ``arc/config.py`` keeps
only what a process needs before it can reach the database.

:data:`DEFAULT_SETTINGS` is the single source for first-boot values; the
initial migration seeds exactly these keys.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base

#: First-boot values. Every value is JSON, so a list-valued rule
#: (``preferred_groups``) and a scalar one live in the same column.
DEFAULT_SETTINGS: Final[MappingProxyType[str, Any]] = MappingProxyType(
    {
        # Ordered list of release groups to prefer; empty means "no
        # preference, rank on resolution and seeders" (FR-A3).
        "preferred_groups": [],
        "preferred_resolution": "1080p",
        "fallback_resolution": "720p",
        # N — how many unwatched episodes ahead to keep (FR-A1).
        "look_ahead_n": 2,
        # G — grace days before files are deleted (FR-T1).
        "grace_days_g": 7,
        # D — days a ready episode may sit unwatched before the want is
        # dropped (FR-T2).
        "unwatched_days_d": 21,
        "sub_lang": "en",
        "audio_lang": "ja",
        # No "max_transcodes" here: the ffmpeg concurrency cap is a property of
        # the machine, not an editable rule, and it must be known before the
        # database is reachable. It stays MAX_TRANSCODES in arc.config
        # (architecture.md §9); two homes for one number is one too many.
    }
)


class Setting(Base):
    """One admin-editable rule."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
