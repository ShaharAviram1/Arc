"""What a catalogue source is, and what it returns (architecture.md §5.0).

Arc reads its catalogue from AniList when AniList is up and from the MyAnimeList
official API when it is not (FR-C6). Both answer the same four questions —
search, one title by AniList id, one title by MAL id, one season — and both
answer them with the same :class:`CatalogMedia`, so nothing above this module
has to know which of them replied.

Two rules shape the dataclasses:

* **Identity is a pair, not an id.** A title has an AniList id, a MAL id, or
  both, and which ones are known changes over the row's life (FR-C6). Nothing
  here has a single ``id``; the internal one belongs to the database.
* **A source says how sure it is.** ``full`` distinguishes a summary (a search
  card) from a detail fetch, and :attr:`AiringEntry.estimated` marks an air
  time that was synthesised from a broadcast slot rather than published as a
  schedule. Both exist so the cache can refuse to let weaker data overwrite
  stronger data.

Errors are the other half of the contract. :class:`SourceUnavailable` means
"ask someone else" — a connection failure, a timeout, a 5xx, AniList's
"temporarily disabled" 403, a missing client id. :class:`SourceNotFound` means
"this is the answer": the id does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal, Protocol, runtime_checkable

#: The two sources, as stored in ``anime.summary_source`` / ``detail_source``.
SourceName = Literal["anilist", "mal"]

#: Every source name, in the order :class:`CatalogService` tries them.
SOURCE_NAMES: tuple[SourceName, ...] = ("anilist", "mal")


class CatalogError(RuntimeError):
    """Base class for the two things a source can go wrong with."""


class SourceUnavailable(CatalogError):
    """This source could not answer; another one might.

    Raised for a connection error, a timeout, a 5xx, AniList's "temporarily
    disabled" 403, and a source that has no credentials configured. Never for
    "no such title" — that is :class:`SourceNotFound`, and falling back on it
    would turn a correct 404 into a second round trip and a wrong answer.
    """

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source} is unavailable: {reason}")
        self.source = source
        self.reason = reason


class SourceNotFound(CatalogError):
    """The source answered, and the answer is that the id does not exist."""


@dataclass(frozen=True, slots=True)
class MediaTitle:
    """The three titles Arc keeps, plus the one it renders."""

    romaji: str | None = None
    english: str | None = None
    native: str | None = None

    @property
    def preferred(self) -> str:
        """English if there is one, else romaji (FR-C1).

        Falls back through native to the empty string so that this is a
        ``str`` and never ``None``: it is the label on every card in the
        client, and a null there is a rendering bug, not a state to handle.
        """
        return self.english or self.romaji or self.native or ""


@dataclass(frozen=True, slots=True)
class AiringEntry:
    """When one episode aired, and how firmly the source knows it.

    ``estimated`` is true for an air time Arc worked out itself from a MAL
    broadcast slot (FR-C6). The client badges those, and the cache lets a
    published AniList time overwrite one but never the other way round.
    """

    episode: int
    at: datetime
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class MediaRelation:
    """A sequel/prequel/side story edge, as stored in ``anime.relations``.

    Carries whichever external ids the source knew. AniList gives an AniList
    id, MAL a MAL id; the API resolves either to an internal id when Arc has
    a row for the related show, and to ``null`` when it does not.
    """

    relation_type: str
    title: MediaTitle
    anilist_id: int | None = None
    mal_id: int | None = None
    format: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogMedia:
    """One title, from either source.

    ``full`` is the difference between the two queries a source answers: a
    search result carries only the summary fields, and writing its empty
    ``genres`` or ``relations`` over a cached row would quietly destroy data
    the by-id query paid for. The cache checks this flag rather than guessing
    from empties (a title really can have no relations).
    """

    source: SourceName
    title: MediaTitle
    anilist_id: int | None = None
    mal_id: int | None = None
    format: str | None = None
    episodes: int | None = None
    status: str | None = None
    season: str | None = None
    season_year: int | None = None
    cover_url: str | None = None
    #: How many people have the show on a list, and the average score out of
    #: 100. Summary fields, because the recommendation pool ranks by them and
    #: only ever sees summaries for a season (§5.6). Both are null from a
    #: source that does not publish them.
    popularity: int | None = None
    average_score: int | None = None
    #: Only meaningful when ``full``; empty on a search result.
    banner_url: str | None = None
    description: str | None = None
    genres: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    tags: list[dict[str, Any]] = field(default_factory=list)
    studio: str | None = None
    relations: list[MediaRelation] = field(default_factory=list)
    #: AniList's ``nextAiringEpisode`` blob, stored verbatim in JSONB. MAL has
    #: no equivalent and always leaves this null.
    next_airing: dict[str, Any] | None = None
    airing: list[AiringEntry] = field(default_factory=list)
    #: ``(weekday, local time)`` of the broadcast slot, in Asia/Tokyo — MAL's
    #: only statement about when episodes air. Monday is 0.
    broadcast: tuple[int, time] | None = None
    #: The day the first episode aired, when the source knows it.
    start_date: date | None = None
    full: bool = False

    @property
    def ids(self) -> tuple[int | None, int | None]:
        """``(anilist_id, mal_id)``, the pair that identifies this title."""
        return (self.anilist_id, self.mal_id)


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of search results (FR-C1)."""

    results: list[CatalogMedia]
    page: int
    has_next: bool


@runtime_checkable
class CatalogSource(Protocol):
    """The four questions every catalogue source answers.

    ``by_anilist_id`` and ``by_mal_id`` return ``None`` when the source has no
    way to answer at all — MAL cannot look a title up by its AniList id — as
    opposed to raising :class:`SourceNotFound`, which is a statement that the
    id does not exist.
    """

    @property
    def name(self) -> SourceName: ...

    @property
    def configured(self) -> bool:
        """Whether this source has what it needs to be asked anything."""
        ...

    async def search(self, term: str, *, page: int = 1) -> SearchPage: ...

    async def by_anilist_id(self, anilist_id: int) -> CatalogMedia | None: ...

    async def by_mal_id(self, mal_id: int) -> CatalogMedia | None: ...

    async def season(self, year: int, season: str) -> list[CatalogMedia]: ...

    async def aclose(self) -> None: ...


__all__ = [
    "SOURCE_NAMES",
    "AiringEntry",
    "CatalogError",
    "CatalogMedia",
    "CatalogSource",
    "MediaRelation",
    "MediaTitle",
    "SearchPage",
    "SourceName",
    "SourceNotFound",
    "SourceUnavailable",
]
