"""Files on disk: ``media_files`` and ``renditions`` (architecture.md §4).

A ``MediaFile`` is the source Arc downloaded (or that was dropped in the
manual directory); a ``Rendition`` is the browser-ready HLS output. Sources
are kept after transcoding so a rendition can be redone (FR-P5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk, created_at
from arc.models.enums import ReviewState, enum_column


class MediaFile(Base):
    """A source video file and what the matcher made of it (spec §4.3)."""

    __tablename__ = "media_files"
    __table_args__ = (
        Index("ix_media_files_episode_id", "episode_id"),
        # The match-review queue is "everything pending", so it is worth an
        # index of its own (FR-L4, FR-D4).
        Index("ix_media_files_review_state", "review_state"),
    )

    id: Mapped[int] = bigint_pk()
    #: Null until the file is matched, and set back to null if the episode is
    #: deleted — the file itself outlives the link.
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="SET NULL"),
    )
    #: Absolute path under DATA_DIR. Never taken from user input (§7).
    #: Unique: one row per file on disk, so a rescan of the library upserts
    #: rather than accumulating a duplicate row per pass.
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: Bytes. BIGINT: a 1080p batch file can exceed the 2 GB INT ceiling.
    size: Mapped[int | None] = mapped_column(BigInteger)
    #: anitopy output plus ffprobe stream summary (FR-L2).
    parsed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: 0..1; ≥ the threshold auto-links, below it goes to review (FR-L4).
    match_confidence: Mapped[float | None] = mapped_column(Float)
    #: [{anilist_id, episode, score, why}] shown in the review UI.
    match_candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    review_state: Mapped[ReviewState] = mapped_column(
        enum_column(ReviewState),
        nullable=False,
        default=ReviewState.AUTO,
        server_default=ReviewState.AUTO.value,
    )
    #: Claude's proposal for an unsure file. Displayed, never applied (FR-L5).
    llm_suggestion: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()


class Rendition(Base):
    """The HLS output for one episode: one rendition per episode (FR-P1)."""

    __tablename__ = "renditions"

    id: Mapped[int] = bigint_pk()
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    #: Directory under DATA_DIR/renditions holding playlist and segments.
    dir: Mapped[str] = mapped_column(Text, nullable=False)
    playlist_path: Mapped[str] = mapped_column(Text, nullable=False)
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    #: The subtitle track burned in, and the audio track kept (FR-P2). Null
    #: subtitle_lang means the file had none and the episode is flagged.
    subtitle_lang: Mapped[str | None] = mapped_column(String(16))
    audio_lang: Mapped[str | None] = mapped_column(String(16))
    #: Set when the transcode finished; null while it is still preparing.
    ready_at: Mapped[datetime | None] = mapped_column(TZDateTime)
