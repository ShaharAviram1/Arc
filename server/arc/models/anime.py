"""Catalogue: ``anime`` and ``episodes`` (architecture.md §4, spec §3).

``anime`` is a local cache of whichever catalogue source answered, keyed by an
**internal** id. It used to be keyed by the AniList id, which was simpler right
up to the day AniList went down: a show first seen through MyAnimeList has no
AniList id yet, and one that gains it later must stay the same row, with the
same list entries and the same episodes (FR-C6). So the primary key is Arc's
own, the two external ids are nullable and unique, and a check constraint keeps
a row from having neither.

``episodes`` is Arc's own per-episode state machine (spec §6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk
from arc.models.enums import EpisodeState, enum_column

#: Length of ``summary_source`` / ``detail_source``. Both hold one of the two
#: source names ("anilist", "mal"); 16 leaves room for a third without a
#: migration and is still short enough to read as "a name, not a sentence".
SOURCE_LENGTH = 16


class Anime(Base):
    """A title, from whichever source Arc could reach (FR-C5, FR-C6)."""

    __tablename__ = "anime"
    __table_args__ = (
        # A row with neither external id could never be refreshed or matched
        # against anything; it would be a dead cache entry that nothing can
        # ever reach again.
        CheckConstraint(
            "anilist_id IS NOT NULL OR mal_id IS NOT NULL",
            name="has_external_id",
        ),
        # The schedule and "behind on" queries filter by airing status.
        Index("ix_anime_status", "status"),
    )

    #: Arc's own id. Stable across everything that happens to the external ids.
    id: Mapped[int] = bigint_pk()

    #: The AniList id, when AniList has been reached for this title. Unique, so
    #: two sources cannot produce two rows for one show.
    anilist_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True)
    #: The MAL id. The join key for all MAL sync, and the id the catalogue
    #: fallback finds a show by.
    mal_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True)

    #: Which source last wrote the summary columns, and which last filled the
    #: detail ones ("anilist" | "mal"). ``detail_source`` is what tells
    #: ``ensure_anime`` that a row filled from MAL should be upgraded as soon
    #: as AniList is healthy again, and what the API reports as ``source`` so
    #: the client can say "catalogue via MAL".
    summary_source: Mapped[str | None] = mapped_column(String(SOURCE_LENGTH))
    detail_source: Mapped[str | None] = mapped_column(String(SOURCE_LENGTH))

    title_romaji: Mapped[str | None] = mapped_column(Text)
    title_english: Mapped[str | None] = mapped_column(Text)
    title_native: Mapped[str | None] = mapped_column(Text)
    #: Alternative titles, used by the matcher's token-set ratio (§5.2).
    synonyms: Mapped[list[str] | None] = mapped_column(JSONB)

    #: AniList MediaFormat (TV, MOVIE, OVA, …). MAL's ``media_type`` is mapped
    #: into the same vocabulary on the way in.
    format: Mapped[str | None] = mapped_column(String(32))
    #: Total episode count; null while a show is airing without a known count.
    episodes: Mapped[int | None] = mapped_column(Integer)
    #: AniList MediaStatus (RELEASING, FINISHED, NOT_YET_RELEASED, …).
    status: Mapped[str | None] = mapped_column(String(32))
    #: AniList MediaSeason (WINTER, SPRING, SUMMER, FALL).
    season: Mapped[str | None] = mapped_column(String(16))
    season_year: Mapped[int | None] = mapped_column(Integer)

    #: The synopsis, stored as plain text (AniList's HTML is stripped on the
    #: way in; MAL's is already plain). Cached rather than fetched per request:
    #: the show page needs it on every view and the recommendation candidate
    #: pool needs it for forty titles at once (§5.6), and AniList's rate limit
    #: does not allow either.
    description: Mapped[str | None] = mapped_column(Text)

    cover_url: Mapped[str | None] = mapped_column(Text)
    #: The key visual at AniList's largest size (``coverImage.extraLarge``).
    #: ``cover_url`` already prefers it when AniList answered, but a row filled
    #: from MAL carries ``main_picture.large`` there — 230 px wide, visibly
    #: soft on a 172 px card and unusable as the 2:3 artwork the redesigned
    #: shelves render (M15). So the big one gets a column of its own, null on
    #: anything MAL wrote, and the client falls back to ``cover_url``.
    cover_large_url: Mapped[str | None] = mapped_column(Text)
    banner_url: Mapped[str | None] = mapped_column(Text)
    #: How many people have the show on a list (AniList ``popularity``, MAL
    #: ``num_list_users``), and the average score on AniList's 0–100 scale
    #: (MAL's 0–10 ``mean`` is scaled on the way in). The recommendation pool
    #: ranks by them — the season by popularity, the genre matches by score
    #: (§5.6) — which is the only thing they are for, so they are unindexed:
    #: both queries already filter to a few hundred rows before sorting.
    #:
    #: Null on any row written before these columns existed, and on a source
    #: that does not publish them. They fill in on the row's next refresh
    #: rather than by a backfill job: `NULLS LAST` in both orderings means a
    #: null sorts to the bottom, which is the right default for an unranked
    #: title anyway.
    popularity: Mapped[int | None] = mapped_column(Integer)
    average_score: Mapped[int | None] = mapped_column(Integer)
    #: A real array rather than JSONB: genres are queried ("top-3 genres" in
    #: the recommendation candidate pool, §5.6) and Postgres can index them.
    genres: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    #: [{name, rank}] — shape belongs to AniList, so JSONB. MAL has no tags.
    tags: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    studio: Mapped[str | None] = mapped_column(Text)
    #: ``[{role, name}]`` — the "Made by" block on the show page (M15). The
    #: first row is always the studio, so ``studio`` above stays the single
    #: source of that fact and this column is the ordered list the page
    #: renders; the rest are AniList staff whose role maps onto one of the six
    #: credits the design names (Director, Series Composition, Character
    #: Design, Music, Original Creator). JSONB because the list is read whole
    #: and never queried, and null on a row MAL filled beyond its studio.
    credits: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    #: Sequels/prequels/side stories, used by the recommender (§5.6). Each
    #: entry carries whichever external ids the source knew.
    relations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    #: AniList's nextAiringEpisode blob {episode, airingAt, timeUntilAiring}.
    #: MAL publishes no equivalent, so a MAL-filled row carries a slot Arc
    #: synthesised from the broadcast time instead — same ``airingAt``, a null
    #: ``episode``, and ``estimated: true`` to say so (FR-C6). The schedule
    #: needs it either way: no next broadcast means no weekday (FR-C3).
    next_airing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: When the detail columns were last filled, from either source (FR-C5).
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
    #: The episode thumbnail AniList publishes with its streaming links
    #: (``streamingEpisodes.thumbnail``), 16:9. What the redesigned episode
    #: rows and the Up Next shelf render instead of a placeholder (M15). Only
    #: ever written where it is null, like ``title``: a confirmed manual value
    #: outranks anything a refresh brings back.
    still_url: Mapped[str | None] = mapped_column(Text)
    #: From the AniList airing schedule, or synthesised from a MAL broadcast
    #: slot; null for shows that have finished and for which neither source
    #: keeps dates.
    air_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: True when ``air_at`` was worked out from a weekly broadcast slot rather
    #: than published per episode (FR-C6). The client badges these; a real
    #: AniList time overwrites one and clears the flag, and an estimate never
    #: overwrites a real time.
    air_at_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
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
