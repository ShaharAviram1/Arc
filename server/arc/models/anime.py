"""Catalogue: ``anime`` and ``episodes`` (architecture.md §4, spec §3).

``anime`` is a local cache of AniList, keyed by the AniList id so that a
refresh is a plain upsert and nothing has to translate ids. ``episodes`` is
Arc's own per-episode state machine (spec §6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk
from arc.models.enums import EpisodeState, enum_column


class Anime(Base):
    """A title as AniList knows it, cached locally and refreshed (FR-C5)."""

    __tablename__ = "anime"
    __table_args__ = (
        # The schedule and "behind on" queries filter by airing status.
        Index("ix_anime_status", "status"),
    )

    #: The AniList id. Not generated: it comes from upstream.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    #: MAL id, when AniList knows one. The join key for all MAL sync.
    mal_id: Mapped[int | None] = mapped_column(Integer, index=True)

    title_romaji: Mapped[str | None] = mapped_column(Text)
    title_english: Mapped[str | None] = mapped_column(Text)
    title_native: Mapped[str | None] = mapped_column(Text)
    #: Alternative titles, used by the matcher's token-set ratio (§5.2).
    synonyms: Mapped[list[str] | None] = mapped_column(JSONB)

    #: AniList MediaFormat (TV, MOVIE, OVA, …).
    format: Mapped[str | None] = mapped_column(String(32))
    #: Total episode count; null while a show is airing without a known count.
    episodes: Mapped[int | None] = mapped_column(Integer)
    #: AniList MediaStatus (RELEASING, FINISHED, NOT_YET_RELEASED, …).
    status: Mapped[str | None] = mapped_column(String(32))
    #: AniList MediaSeason (WINTER, SPRING, SUMMER, FALL).
    season: Mapped[str | None] = mapped_column(String(16))
    season_year: Mapped[int | None] = mapped_column(Integer)

    cover_url: Mapped[str | None] = mapped_column(Text)
    banner_url: Mapped[str | None] = mapped_column(Text)
    #: A real array rather than JSONB: genres are queried ("top-3 genres" in
    #: the recommendation candidate pool, §5.6) and Postgres can index them.
    genres: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    #: [{name, rank}] — shape belongs to AniList, so JSONB.
    tags: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    studio: Mapped[str | None] = mapped_column(Text)
    #: Sequels/prequels/side stories, used by the recommender (§5.6).
    relations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    #: AniList's nextAiringEpisode blob {episode, airingAt, timeUntilAiring}.
    next_airing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: When this row was last pulled from AniList (FR-C5).
    refreshed_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class Episode(Base):
    """One episode of an anime, plus where Arc has got to with it."""

    __tablename__ = "episodes"
    __table_args__ = (
        # One row per (show, episode number); the acquisition pipeline upserts
        # against this.
        UniqueConstraint("anime_id", "number", name="uq_episodes_anime_id_number"),
        # "aired in the last 7 days", "airs today" (FR-W1, §5.1).
        Index("ix_episodes_air_at", "air_at"),
    )

    id: Mapped[int] = bigint_pk()
    anime_id: Mapped[int] = mapped_column(
        ForeignKey("anime.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    #: From the AniList airing schedule; null for shows that have finished.
    air_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    state: Mapped[EpisodeState] = mapped_column(
        enum_column(EpisodeState),
        nullable=False,
        default=EpisodeState.NOT_WANTED,
        server_default=EpisodeState.NOT_WANTED.value,
    )
    #: When ``state`` last changed — drives retry windows and the UI. Defaulted
    #: by the database so a freshly inserted episode already has a meaningful
    #: age; the services set it explicitly on every subsequent transition.
    state_changed_at: Mapped[datetime | None] = mapped_column(
        TZDateTime,
        server_default=func.now(),
    )
    #: Why the search gave up, shown to the user (FR-A6, FR-A7).
    unavailable_reason: Mapped[str | None] = mapped_column(Text)
