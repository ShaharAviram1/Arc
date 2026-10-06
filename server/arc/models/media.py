"""Files on disk: ``media_files``, ``renditions``, ``offline_copies`` (architecture.md §4).

A ``MediaFile`` is the source Arc downloaded (or that was dropped in the
manual directory); a ``Rendition`` is the browser-ready HLS output; an
``OfflineCopy`` is the small single-file MP4 a device keeps (FR-P6). Sources
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
from arc.models.enums import OfflineCopyState, ReviewState, enum_column


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


class OfflineCopy(Base):
    """The small offline copy of one episode (FR-P6, architecture.md §5.3b).

    One per episode, keyed on it, so "is there a copy?" is a primary-key
    lookup and two requests for the same episode cannot make two rows. There
    is **no path column**: the file is ``DATA_DIR/offline/<episode_id>.mp4``,
    derived from the id (:func:`arc.services.media.names.offline_path_for`),
    so a hand-edited row cannot point the media route at anything else.

    Progress is not here either. It lives in the ``offline_encode`` job's
    payload, the same way a transcode's does (:mod:`arc.services.media.jobs`).
    """

    __tablename__ = "offline_copies"

    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[OfflineCopyState] = mapped_column(
        enum_column(OfflineCopyState),
        nullable=False,
        default=OfflineCopyState.QUEUED,
        server_default=OfflineCopyState.QUEUED.value,
    )
    #: Bytes of the finished file, and the strong validator the media route
    #: answers with — so a device can confirm it holds *this* copy (M19 T4).
    size: Mapped[int | None] = mapped_column(BigInteger)
    etag: Mapped[str | None] = mapped_column(String(80))
    #: What the copy was made with: ``h264`` or ``hevc``, the height cap, the
    #: CRF and the audio bitrate, and a short hash over every setting that
    #: changes the bytes (languages included). Recorded only: a settings
    #: change does not re-make a ready copy (owner, 2026-10-06).
    codec: Mapped[str | None] = mapped_column(String(16))
    height: Mapped[int | None] = mapped_column(Integer)
    crf: Mapped[int | None] = mapped_column(Integer)
    audio_bitrate: Mapped[str | None] = mapped_column(String(16))
    settings_key: Mapped[str | None] = mapped_column(String(32))
    #: The source the copy was made from. Null once that file is gone, which
    #: is the ordinary fate of a source (retention, or a trip, M19 T3).
    media_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_files.id", ondelete="SET NULL")
    )
    ready_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: When the media route last sent any of it, touched at most hourly. The
    #: idle rule (``offline_idle_days``) counts from here, else from
    #: ``ready_at``.
    last_served_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: The head and tail of the last failure, for the person who asked.
    error: Mapped[str | None] = mapped_column(Text)
