"""The offline catalogue: two imported datasets and their import log (M15.5).

Arc's live catalogue is AniList with MyAnimeList behind it, and the week
AniList answered 403 for three days is what this module exists for. Both of
those are *services*; these three tables are a **file**, downloaded once a week
and replaced whole, so search, filename matching and id mapping have something
to consult that cannot be unreachable (FR-C6, architecture.md §5.0a).

* ``offline_anime`` — one row per title from manami's ``anime-offline-database``
  (≈ 41k), with its titles, synonyms, season, type and the cross-ids parsed out
  of the entry's ``sources`` URLs. ``search_text`` is the lowercased title and
  synonyms in one string with a trigram index over it, which is what a later
  task searches; nothing here searches anything yet.
* ``offline_ids`` — one row per entry of Fribb's ``anime-lists``, the id map
  that adds TMDB (series *and* season), TVDB and IMDb to the four manami
  carries. Separate from ``offline_anime`` because it is a separate file with
  its own release cadence and its own coverage: an anime in one is not
  necessarily in the other, and joining them at import time would mean
  choosing which side's absence wins.
* ``offline_imports`` — one row per source: which version is loaded, when, how
  many rows, and the sha256 of the file it came from. The checksum is what
  makes a weekly job that downloads an unchanged file cost nothing, and
  ``imported_at`` is what the admin endpoint calls stale after 14 days.

None of these three has a foreign key to ``anime``, and that is deliberate:
they are a **cache of a public dataset**, not part of Arc's own aggregate. The
import truncates and refills them, which a foreign key into user data could
never survive.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk

#: Length of the short vocabularies copied straight out of the datasets:
#: manami's ``type`` (TV, MOVIE, OVA, ONA, SPECIAL, UNKNOWN), ``status``
#: (FINISHED, ONGOING, UPCOMING, UNKNOWN) and ``animeSeason.season`` (SPRING,
#: SUMMER, FALL, WINTER, UNDEFINED). Not an enum: the dataset is somebody
#: else's, and a new value in next week's release must be an odd string in a
#: column rather than a failed import.
VOCAB_LENGTH = 16

#: Length of ``offline_imports.source``. Two values today ("manami", "fribb").
SOURCE_LENGTH = 32

#: The trigram index the later search task reads. Named here so a test can ask
#: the database whether the migration actually created it — a GIN index with
#: ``gin_trgm_ops`` is the one thing in this schema that a plain column
#: declaration cannot imply.
OFFLINE_SEARCH_INDEX = "ix_offline_anime_search_trgm"

#: The composite index a season listing uses (M15.5 bullet 3).
OFFLINE_SEASON_INDEX = "ix_offline_anime_season"


class OfflineAnime(Base):
    """One title from the manami offline database."""

    __tablename__ = "offline_anime"
    __table_args__ = (
        # ILIKE '%needle%' cannot use a btree; a trigram GIN can, and that is
        # the whole point of storing ``search_text`` denormalised.
        Index(
            OFFLINE_SEARCH_INDEX,
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index(OFFLINE_SEASON_INDEX, "season_year", "season"),
    )

    #: A surrogate key. The dataset has no id of its own — an entry is
    #: identified by its ``sources`` URLs, any of which may be absent — and the
    #: table is replaced whole on every import, so nothing outside it ever
    #: holds one of these values.
    id: Mapped[int] = bigint_pk()

    #: The cross-ids parsed out of the entry's ``sources`` list. All nullable:
    #: plenty of entries carry only two or three of the four. Indexed but
    #: **not unique** — this is somebody else's file, and a duplicate in it
    #: must be a duplicate row rather than a failed import.
    anilist_id: Mapped[int | None] = mapped_column(Integer, index=True)
    mal_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kitsu_id: Mapped[int | None] = mapped_column(Integer)
    anidb_id: Mapped[int | None] = mapped_column(Integer)

    #: manami's single ``title``, and its ``synonyms`` (every other spelling,
    #: every language, every release-group abbreviation people use). The
    #: synonyms are the reason this dataset is worth importing at all: AniList
    #: gives three titles, this gives thirty.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    synonyms: Mapped[list[str] | None] = mapped_column(JSONB)

    #: ``TV`` | ``MOVIE`` | ``OVA`` | ``ONA`` | ``SPECIAL`` | ``UNKNOWN``.
    type: Mapped[str | None] = mapped_column(String(VOCAB_LENGTH))
    #: Total episodes as the dataset knows them; 0 for an unaired entry.
    episodes: Mapped[int | None] = mapped_column(Integer)
    #: ``FINISHED`` | ``ONGOING`` | ``UPCOMING`` | ``UNKNOWN``.
    status: Mapped[str | None] = mapped_column(String(VOCAB_LENGTH))
    #: ``animeSeason``. The year is genuinely null for a good number of
    #: entries, and the season is the literal string ``UNDEFINED`` rather than
    #: null when the dataset does not know it.
    season: Mapped[str | None] = mapped_column(String(VOCAB_LENGTH))
    season_year: Mapped[int | None] = mapped_column(Integer)

    #: Cover art and its thumbnail, usually MAL's CDN. Stored as given; nothing
    #: here downloads or proxies them.
    picture: Mapped[str | None] = mapped_column(Text)
    thumbnail: Mapped[str | None] = mapped_column(Text)

    studios: Mapped[list[str] | None] = mapped_column(JSONB)
    tags: Mapped[list[str] | None] = mapped_column(JSONB)

    #: ``score.arithmeticMean`` — the dataset's average of every source's
    #: score, on a 0–10 scale. Absent on anything nobody has rated.
    score: Mapped[float | None] = mapped_column(Float)
    #: ``duration`` normalised to seconds on the way in, because the dataset
    #: expresses it in whichever unit the source did.
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    #: ``relatedAnime``: source URLs, exactly as given, of prequels, sequels
    #: and side stories. Kept as URLs rather than resolved to ids — the
    #: consumer (season seeding) does not exist yet, and parsing them a second
    #: way later is cheaper than guessing now.
    related: Mapped[list[str] | None] = mapped_column(JSONB)

    #: ``title`` and every synonym, lowercased, joined by ``" | "``. Built at
    #: import time so a search is one ILIKE against one trigram index rather
    #: than a JSONB traversal per row.
    search_text: Mapped[str] = mapped_column(Text, nullable=False)


class OfflineId(Base):
    """One entry of Fribb's ``anime-lists``: the cross-id map.

    The reason this table exists and ``offline_anime``'s four id columns are
    not enough: TMDB. AniList publishes no TMDB id, and TMDB is where M15.5's
    backdrops, stills and credits come from, so the route from an Arc show to
    a TMDB series is MAL id → this table → ``tmdb_tv_id`` (+ ``tmdb_season``).
    """

    __tablename__ = "offline_ids"

    id: Mapped[int] = bigint_pk()

    #: The four ids that overlap with ``offline_anime``. The two that are
    #: looked *up* by are indexed; the other two are payload.
    anidb_id: Mapped[int | None] = mapped_column(Integer)
    anilist_id: Mapped[int | None] = mapped_column(Integer, index=True)
    mal_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kitsu_id: Mapped[int | None] = mapped_column(Integer)

    #: TMDB is two namespaces, not one: a series and a film have independent
    #: id spaces, and Fribb encodes which by the key it uses
    #: (``themoviedb_id: {"tv": N}`` or ``{"movie": N}``). Two columns rather
    #: than an id plus a kind, so a query for "the series" cannot accidentally
    #: match a film with the same number.
    tmdb_tv_id: Mapped[int | None] = mapped_column(Integer)
    tmdb_movie_id: Mapped[int | None] = mapped_column(Integer)
    #: Which TMDB season this anime *is*. Anime seasons are TMDB seasons of one
    #: series far more often than they are separate series, so an enrichment
    #: job that ignored this would fetch season 1's stills for season 3.
    tmdb_season: Mapped[int | None] = mapped_column(Integer)

    tvdb_id: Mapped[int | None] = mapped_column(Integer)
    tvdb_season: Mapped[int | None] = mapped_column(Integer)

    #: The first IMDb id when the entry carries several (it is a list about as
    #: often as it is a string). Text, because an IMDb id is ``tt0286390``.
    imdb_id: Mapped[str | None] = mapped_column(Text)

    #: Fribb's own ``type`` string. Same vocabulary as manami's, near enough,
    #: and stored for the same reason: it is in the file.
    type: Mapped[str | None] = mapped_column(String(VOCAB_LENGTH))


class OfflineImport(Base):
    """What is currently loaded, per source, and when it arrived."""

    __tablename__ = "offline_imports"

    #: ``"manami"`` or ``"fribb"``. The natural key: there is exactly one
    #: loaded version of each file, and a surrogate id would only make it
    #: possible to have two.
    source: Mapped[str] = mapped_column(String(SOURCE_LENGTH), primary_key=True)

    #: The release the loaded rows came from — manami's tag (``2026-27``, read
    #: out of the header's ``$schema`` URL) or, for Fribb's plain file in a git
    #: repository, its ``ETag`` or ``Last-Modified``, falling back to the date
    #: it was downloaded. Nullable because a version Arc could not determine
    #: must not stop the import.
    version: Mapped[str | None] = mapped_column(Text)

    imported_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False)

    #: sha256 of the downloaded file, computed while it streams. The next
    #: week's job compares against this and skips the whole parse-and-replace
    #: when the file has not changed — which is most weeks for Fribb.
    checksum: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "OFFLINE_SEARCH_INDEX",
    "OFFLINE_SEASON_INDEX",
    "SOURCE_LENGTH",
    "VOCAB_LENGTH",
    "OfflineAnime",
    "OfflineId",
    "OfflineImport",
]
