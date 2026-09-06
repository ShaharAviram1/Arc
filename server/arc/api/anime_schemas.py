"""Response shapes for the catalogue and list endpoints (M3).

Separate from :mod:`arc.api.schemas` because there are a dozen of them and
they belong together: two routers (``anime`` and ``list``) share
``AnimeSummary``, ``ListEntryOut`` and the episode shape, and the client's
types are generated from exactly these.

Everything here is built by an explicit ``from_*`` classmethod rather than by
``from_attributes``. The models and the API disagree on purpose — the API
groups the three title columns into an object and adds a computed
``preferred``, turns AniList's epoch seconds into a datetime, and decides
``aired`` and ``watched`` against the caller and the clock — and a
constructor makes each of those decisions visible in one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arc.models import Anime, Episode, EpisodeState, ListEntry, ListStatus
from arc.services.catalog import preferred_title
from arc.services.catalog.airing import (
    FINISHED,
    RELEASING,
    aired_through,
    is_aired,
    next_airing_at,
    next_airing_episode,
)


class TitleOut(BaseModel):
    """The three titles plus the one to render."""

    romaji: str | None = None
    english: str | None = None
    native: str | None = None
    #: English if there is one, else romaji. Never null, so the client never
    #: has to write the fallback chain itself.
    preferred: str

    @classmethod
    def from_anime(cls, anime: Anime) -> TitleOut:
        return cls(
            romaji=anime.title_romaji,
            english=anime.title_english,
            native=anime.title_native,
            preferred=preferred_title(anime),
        )

    @classmethod
    def from_blob(cls, raw: dict[str, Any] | None) -> TitleOut:
        """A title stored inside ``anime.relations`` JSONB."""
        raw = raw or {}
        romaji = raw.get("romaji")
        english = raw.get("english")
        native = raw.get("native")
        return cls(
            romaji=romaji,
            english=english,
            native=native,
            preferred=raw.get("preferred") or english or romaji or native or "",
        )


class AnimeCore(BaseModel):
    """The fields a summary and a detail response agree on.

    They differ over one name — ``episodes`` is the episode *count* on a card
    and the episode *list* on a show page — so the shared half is a base class
    rather than the summary itself. Nothing returns an ``AnimeCore``.
    """

    #: Arc's own id. Every route that names an anime takes this one; the
    #: external ids below are for display and for linking out, never for
    #: addressing (FR-C6).
    id: int
    anilist_id: int | None = None
    mal_id: int | None = None
    #: Which catalogue source this record came from ("anilist" | "mal", or null
    #: for a row nothing has filled in yet). The client shows a "catalogue via
    #: MAL" notice on ``"mal"``.
    source: str | None = None
    title: TitleOut
    format: str | None = None
    status: str | None = None
    season: str | None = None
    season_year: int | None = None
    cover_url: str | None = None
    #: The caller's own list state for this show, or null if it is not on it.
    list_status: ListStatus | None = None


class AnimeSummary(AnimeCore):
    """A search result or a list row: enough for a card."""

    #: AniList's total episode count; null while a show airs without one.
    episodes: int | None = None

    @classmethod
    def from_anime(cls, anime: Anime, list_status: ListStatus | None = None) -> AnimeSummary:
        return cls(
            id=anime.id,
            anilist_id=anime.anilist_id,
            mal_id=anime.mal_id,
            # A card renders from the summary columns, so it reports which
            # source wrote *those* — a row whose detail came from AniList can
            # still carry a MAL-sourced cover after an outage.
            source=anime.summary_source,
            title=TitleOut.from_anime(anime),
            format=anime.format,
            episodes=anime.episodes,
            status=anime.status,
            season=anime.season,
            season_year=anime.season_year,
            cover_url=anime.cover_url,
            list_status=list_status,
        )


class SearchPage(BaseModel):
    """One page of catalogue search results (FR-C1)."""

    results: list[AnimeSummary]
    page: int
    has_next: bool


class NextAiringOut(BaseModel):
    """When the next episode airs, if the show is still airing.

    Only AniList publishes this, so a show whose detail came from MAL has none
    even while it is airing; the episode list's ``air_at`` values carry the
    estimate instead.
    """

    episode: int
    at: datetime

    @classmethod
    def from_blob(cls, raw: dict[str, Any] | None) -> NextAiringOut | None:
        """AniList's ``nextAiringEpisode`` as stored, or ``None``.

        Null for the slot Arc synthesises from a MAL broadcast time as well: it
        knows when the next episode airs but not which one, and a show page
        that named an episode number it had guessed would be worse than one
        that says nothing. The schedule renders that case from the raw blob
        instead (:mod:`arc.services.catalog.schedule`).
        """
        episode = next_airing_episode(raw)
        at = next_airing_at(raw)
        if episode is None or at is None:
            return None
        return cls(episode=episode, at=at)


class RelationOut(BaseModel):
    """A sequel/prequel/side story, as the show page links to it.

    ``id`` is Arc's internal id and is **null** until Arc has a row for the
    related show — which is most of them, since a relation is a title nobody
    has necessarily opened yet. The client links only the ones that have one
    and renders the rest as plain text; the external ids are there so a future
    "add this" can resolve them.
    """

    id: int | None = None
    anilist_id: int | None = None
    mal_id: int | None = None
    relation_type: str
    title: TitleOut
    format: str | None = None

    @classmethod
    def from_blob(
        cls, raw: dict[str, Any], internal_ids: dict[tuple[str, int], int] | None = None
    ) -> RelationOut | None:
        anilist_id = raw.get("anilist_id")
        mal_id = raw.get("mal_id")
        if anilist_id is None and mal_id is None:
            return None
        found = internal_ids or {}
        internal = None
        if anilist_id is not None:
            internal = found.get(("anilist", int(anilist_id)))
        if internal is None and mal_id is not None:
            internal = found.get(("mal", int(mal_id)))
        return cls(
            id=internal,
            anilist_id=anilist_id,
            mal_id=mal_id,
            relation_type=str(raw.get("relation_type") or "OTHER"),
            title=TitleOut.from_blob(raw.get("title")),
            format=raw.get("format"),
        )

    @staticmethod
    def external_ids(anime: Anime) -> tuple[set[int], set[int]]:
        """The AniList and MAL ids named by ``anime``'s relations.

        The endpoint looks these up in one query and passes the answers to
        :meth:`from_blob`; doing it per relation would be a round trip each.
        """
        anilist_ids: set[int] = set()
        mal_ids: set[int] = set()
        for raw in anime.relations or []:
            if not isinstance(raw, dict):
                continue
            if raw.get("anilist_id") is not None:
                anilist_ids.add(int(raw["anilist_id"]))
            if raw.get("mal_id") is not None:
                mal_ids.add(int(raw["mal_id"]))
        return anilist_ids, mal_ids


class EpisodeOut(BaseModel):
    """One row of the show page's episode list."""

    id: int
    number: int
    title: str | None = None
    air_at: datetime | None = None
    #: True when ``air_at`` was worked out from a MAL broadcast slot rather
    #: than published per episode (FR-C6). The client badges these as
    #: estimated; they are replaced the next time AniList answers.
    air_at_estimated: bool = False
    #: Whether the episode has aired *now*, so the client does not have to
    #: compare against a clock the server may disagree with.
    aired: bool
    state: EpisodeState
    #: Whether this user finished it (FR-S4). Always false until M8 writes
    #: ``watch_progress``.
    watched: bool = False

    @classmethod
    def from_episode(
        cls,
        episode: Episode,
        *,
        now: datetime,
        anime_status: str | None,
        boundary: int = 0,
        watched: bool = False,
    ) -> EpisodeOut:
        """``boundary`` is the list's :func:`aired_through`; see that module."""
        return cls(
            id=episode.id,
            number=episode.number,
            title=episode.title,
            air_at=episode.air_at,
            air_at_estimated=episode.air_at_estimated,
            aired=is_aired(episode, now=now, anime_status=anime_status, boundary=boundary),
            state=episode.state,
            watched=watched,
        )


class ListEntryOut(BaseModel):
    """(user, anime) → status, progress, score (FR-W2)."""

    model_config = ConfigDict(from_attributes=True)

    anime_id: int
    status: ListStatus
    progress: int
    score: int | None = None
    updated_at: datetime


class ListEntryPatch(BaseModel):
    """The body of ``PUT /api/list/{anime_id}``.

    Every field is optional so the same endpoint serves "add as watching",
    "set my score" and "I'm on episode 4". ``status`` is required only when
    the entry does not exist yet, which no schema can express — the service
    raises and the router answers 422.
    """

    model_config = ConfigDict(extra="forbid")

    status: ListStatus | None = None
    progress: int | None = Field(default=None, ge=0)
    #: ``null`` clears the score; omitting the field leaves it alone. Pydantic
    #: cannot tell those apart, so the router checks ``model_fields_set``.
    score: int | None = Field(default=None, ge=1, le=10)


class ListRow(BaseModel):
    """One row of ``GET /api/list``: the show and the caller's state for it."""

    anime: AnimeSummary
    entry: ListEntryOut


class AnimeDetail(AnimeCore):
    """Everything the show page renders.

    ``episodes`` here is the episode *list*, not the count — that is what the
    show page needs under that name, and it is what the M3 API contract
    specifies. The count is still available, as ``episode_count``.

    ``source`` here is the *detail* source: which catalogue filled the synopsis,
    genres and relations, and therefore whether the air dates below are
    published or estimated (FR-C6).
    """

    #: One row per episode, 1..N, in order.
    episodes: list[EpisodeOut] = Field(default_factory=list)
    #: The total episode count; null while a show airs without one. The same
    #: value ``AnimeSummary.episodes`` carries on a card.
    episode_count: int | None = None
    synopsis: str | None = None
    genres: list[str] = Field(default_factory=list)
    studio: str | None = None
    banner_url: str | None = None
    next_airing: NextAiringOut | None = None
    relations: list[RelationOut] = Field(default_factory=list)
    list_entry: ListEntryOut | None = None

    @classmethod
    def build(
        cls,
        anime: Anime,
        *,
        episodes: list[Episode],
        now: datetime,
        list_entry: ListEntry | None = None,
        watched: frozenset[int] = frozenset(),
        relation_ids: dict[tuple[str, int], int] | None = None,
    ) -> AnimeDetail:
        raw_relations = [raw for raw in (anime.relations or []) if isinstance(raw, dict)]
        boundary = aired_through(
            episodes,
            now=now,
            anime_status=anime.status,
            next_airing=anime.next_airing,
        )
        relations = [
            relation
            for relation in (RelationOut.from_blob(raw, relation_ids) for raw in raw_relations)
            if relation is not None
        ]
        return cls(
            id=anime.id,
            anilist_id=anime.anilist_id,
            mal_id=anime.mal_id,
            source=anime.detail_source,
            title=TitleOut.from_anime(anime),
            format=anime.format,
            status=anime.status,
            season=anime.season,
            season_year=anime.season_year,
            cover_url=anime.cover_url,
            list_status=list_entry.status if list_entry is not None else None,
            episode_count=anime.episodes,
            synopsis=anime.description,
            genres=list(anime.genres or []),
            studio=anime.studio,
            banner_url=anime.banner_url,
            next_airing=NextAiringOut.from_blob(anime.next_airing),
            relations=relations,
            list_entry=(ListEntryOut.model_validate(list_entry) if list_entry else None),
            episodes=[
                EpisodeOut.from_episode(
                    episode,
                    now=now,
                    anime_status=anime.status,
                    boundary=boundary,
                    watched=episode.id in watched,
                )
                for episode in episodes
            ],
        )


#: ``FINISHED``, ``RELEASING``, ``aired_through`` and ``is_aired`` are
#: re-exported rather than defined here: the aired rule moved into
#: :mod:`arc.services.catalog.airing` when the home page started needing it
#: too (FR-C4), and one rule with two definitions is how a show page and a
#: "behind by N" come to disagree about episode 6.
__all__ = [
    "FINISHED",
    "RELEASING",
    "AnimeCore",
    "AnimeDetail",
    "AnimeSummary",
    "EpisodeOut",
    "ListEntryOut",
    "ListEntryPatch",
    "ListRow",
    "NextAiringOut",
    "RelationOut",
    "SearchPage",
    "TitleOut",
    "aired_through",
    "is_aired",
]
