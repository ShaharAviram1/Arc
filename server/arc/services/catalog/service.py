"""One catalogue built from two sources (FR-C6, architecture.md §5.0).

:class:`CatalogService` implements :class:`CatalogSource` itself: it takes a
primary (AniList) and a fallback (MAL), tries them in order, and hands back
whichever answered. Callers see the interface of a single source, plus
:meth:`status` for the admin view.

Three rules decide when the fallback is used.

1. **Unavailability falls back.** A connection error, a timeout, a 5xx,
   AniList's "temporarily disabled" 403, an unconfigured client id — anything
   that means "this source could not answer" — moves on to the next source.
2. **"Not found" does not.** A genuine 404 for an AniList id is the answer,
   and asking MAL would either fail (it cannot look up an AniList id) or, worse,
   return a different show. The one exception is :meth:`by_mal_id`: AniList
   simply has no row for many MAL ids, so its "not found" there means "I cannot
   help", not "no such title".
3. **A failure is remembered.** Every :class:`SourceUnavailable` opens that
   source's breaker, so the rest of the outage costs no timeouts at all.

The "catalogue is now coming from MAL" warning is written when the breaker
*opens*, not when it is consulted: an outage is one log line, not one per
request.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from arc.services.catalog.breaker import Breaker
from arc.services.catalog.source import (
    SOURCE_NAMES,
    CatalogMedia,
    CatalogSource,
    SearchPage,
    SourceNotFound,
    SourceUnavailable,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

#: What the API says when neither source could answer.
CATALOGUE_UNAVAILABLE = "catalogue is unavailable"


class CatalogService:
    """AniList first, MAL second, with a breaker in front of both."""

    #: Not a :data:`SourceName`: this is the composite, and calling it
    #: "anilist" made a log line or a status field claiming a fallback answer
    #: came from AniList. The concrete sources keep their own names.
    name: str = "catalog"

    def __init__(
        self,
        primary: CatalogSource,
        fallback: CatalogSource | None = None,
        breaker: Breaker | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.breaker = breaker if breaker is not None else Breaker()

    @property
    def configured(self) -> bool:
        """A catalogue exists as long as one of its sources does."""
        return any(source.configured for source in self.sources)

    @property
    def sources(self) -> tuple[CatalogSource, ...]:
        """The sources, in the order they are tried."""
        if self.fallback is None:
            return (self.primary,)
        return (self.primary, self.fallback)

    def source(self, name: str) -> CatalogSource | None:
        """The source called ``name``, if this service has it."""
        for source in self.sources:
            if source.name == name:
                return source
        return None

    def healthy(self, name: str) -> bool:
        """Whether ``name`` is configured and its breaker is closed.

        The reconciliation job asks this before starting: filling in AniList
        ids while AniList is down would be fifty timeouts and nothing learned.
        """
        source = self.source(name)
        if source is None or not source.configured:
            return False
        return not self.breaker.is_open(name)

    # --- The interface ---

    async def search(self, term: str, *, page: int = 1) -> SearchPage:
        """Search, falling back only when a source could not answer.

        An empty page is an answer: a term nobody has heard of must not cost a
        second round trip to a second source that has not heard of it either.
        """
        empty = SearchPage(results=[], page=page, has_next=False)
        return await self._first(
            lambda source: source.search(term, page=page),
            operation="search",
            empty=empty,
            not_found_final=False,
        )

    async def by_anilist_id(self, anilist_id: int) -> CatalogMedia | None:
        """One title by AniList id. A 404 here is final (rule 2)."""
        return await self._first(
            lambda source: source.by_anilist_id(anilist_id),
            operation="by_anilist_id",
            empty=None,
            not_found_final=True,
        )

    async def by_mal_id(self, mal_id: int) -> CatalogMedia | None:
        """One title by MAL id.

        AniList's "not found" is *not* final here: it maps only a subset of MAL
        ids, so a miss means "ask MAL", which is the whole point of the second
        source.
        """
        return await self._first(
            lambda source: source.by_mal_id(mal_id),
            operation="by_mal_id",
            empty=None,
            not_found_final=False,
        )

    async def season(self, year: int, season: str) -> list[CatalogMedia]:
        """A whole season's summaries, for the pre-cache sweep (FR-C7)."""
        empty: list[CatalogMedia] = []
        return await self._first(
            lambda source: source.season(year, season),
            operation="season",
            empty=empty,
            not_found_final=False,
        )

    async def aclose(self) -> None:
        for source in self.sources:
            await source.aclose()

    # --- The loop all four share ---

    async def _first(
        self,
        call: Callable[[CatalogSource], Awaitable[T]],
        *,
        operation: str,
        empty: T,
        not_found_final: bool,
    ) -> T:
        """Ask each source in turn; return the first real answer.

        ``empty`` is what "everyone was asked and nobody had it" looks like for
        this operation — ``None`` for a by-id lookup, an empty page for a
        search. It is only returned when no source failed: if one did, the
        caller gets that failure, because "no results" and "the catalogue is
        down" must not look the same to a user.
        """
        failure: SourceUnavailable | None = None
        for source in self.sources:
            if not source.configured:
                continue
            if self.breaker.is_open(source.name):
                continue
            try:
                result = await call(source)
            except SourceNotFound:
                # A 404 is the source answering, so it counts as health: an
                # otherwise-idle deployment whose only AniList traffic is
                # lookups for ids AniList does not have would never close its
                # breaker, and ``healthy("anilist")`` gates the reconciliation
                # job and the "a MAL fill is stale now" rule.
                self.breaker.record_success(source.name)
                if not_found_final:
                    raise
                continue
            except SourceUnavailable as exc:
                failure = exc
                self._note_failure(source.name, exc, operation=operation)
                continue
            self.breaker.record_success(source.name)
            if result is None:
                # The source has no way to answer this question (MAL cannot
                # look a title up by AniList id). Not a failure; ask the next.
                continue
            return result

        if failure is not None:
            raise failure
        return empty

    def _note_failure(self, name: str, exc: SourceUnavailable, *, operation: str) -> None:
        """Open the breaker, and log the switch once rather than per request."""
        if self.breaker.record_failure(name, exc.reason):
            log.warning(
                "catalogue source unavailable; falling back",
                extra={
                    "source": name,
                    "reason": exc.reason,
                    "operation": operation,
                    "breaker_seconds": self.breaker.seconds,
                },
            )

    # --- Admin view ---

    def status(self) -> dict[str, Any]:
        """Source health and breaker state for ``GET /api/catalog/status``."""
        sources: dict[str, Any] = {}
        for name in SOURCE_NAMES:
            source = self.source(name)
            state = self.breaker.state(name)
            sources[name] = {
                "state": state.state,
                "healthy_at": state.healthy_at,
                "failed_at": state.failed_at,
                "reason": state.reason,
                "configured": bool(source is not None and source.configured),
            }
        return {"sources": sources, "active": self._active()}

    def _active(self) -> str:
        """Which source the next read would actually go to."""
        for source in self.sources:
            if source.configured and not self.breaker.is_open(source.name):
                return source.name
        return "none"


__all__ = ["CATALOGUE_UNAVAILABLE", "CatalogService"]
