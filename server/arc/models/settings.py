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
        # K — how many shows one user may have fetching at once (FR-A10).
        # 0 means *unlimited*, which is the opposite of what 0 means for N
        # above. Five is what one disk and qBittorrent's handful of download
        # slots actually allow to arrive at the same time; the shows over the
        # cap wait visibly on their own page and start as the others finish.
        "slot_cap_k": 5,
        # G — grace days before files are deleted (FR-T1).
        "grace_days_g": 7,
        # D — days a ready episode may sit unwatched before the want is
        # dropped (FR-T2).
        "unwatched_days_d": 21,
        # The acquisition kill switch. False on a fresh install: Arc acquires
        # by default, and this is the admin's brake for the day a list import
        # or a rule change asks for more than the machine (or the tracker)
        # should be given at once. Read by ``compute_wants`` and
        # ``search_release``; ``poll_qbit`` ignores it, so downloads already in
        # flight still finish and still reach the library.
        "acquisition_paused": False,
        # The storage floor, in whole GB (FR-T6). While free space on the data
        # volume is below it acquisition holds itself: the reconciler still
        # drops, shelves and cancels — those free room — but no new search
        # starts, and it resumes on its own once retention has made space. 10
        # GB is about two episodes' source plus their renditions plus the
        # transcode's scratch, which is the smallest margin that still leaves
        # the machine somewhere to put what it is already holding.
        "min_free_gb": 10,
        # Whether a finished show with no acceptable single at all may take a
        # batch and download only the wanted episode's file (FR-A4's
        # exception, FR-A11). True on a fresh install: it is the behaviour the
        # owner asked for, and for an old show it is often the difference
        # between an episode and a fortnight of "searching". It is here as the
        # kill switch for the riskiest acquisition change since M6 — turning
        # it off leaves every other path byte-identical, since nothing but
        # ``search_release``'s batch branch reads it, and a batch already in
        # flight is unaffected.
        "batch_fallback": True,
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
