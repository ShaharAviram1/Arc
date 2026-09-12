"""AniList as a :class:`~arc.services.catalog.source.CatalogSource`.

A thin adapter over :class:`AniListClient`: the client keeps everything that
is about talking to AniList politely (pacing, retries, schedule paging), and
this class keeps everything that is about being one of two interchangeable
catalogue sources — which is, almost entirely, translating AniList's four
exception types into the catalogue's three.

That translation is the whole point. Above this module, "AniList is having an
outage" and "MAL has no client id" have to look the same, or the fallback logic
would need a branch per source (FR-C6).
"""

from __future__ import annotations

import httpx

from arc.config import Settings
from arc.services.anilist.client import (
    AniListClient,
    AniListDisabled,
    AniListError,
    AniListNotFound,
    AniListRateLimited,
)
from arc.services.catalog.source import (
    CatalogError,
    CatalogMedia,
    SearchPage,
    SourceName,
    SourceNotFound,
    SourceRateLimited,
    SourceUnavailable,
)

#: What ``GET /api/catalog/status`` shows while AniList's own outage is on.
DISABLED_REASON = "api temporarily disabled upstream"


def _as_catalog_error(exc: AniListError) -> CatalogError:
    """AniList's error vocabulary in the catalogue's terms.

    One function rather than a try/except per method: the mapping is a single
    decision, and repeating it four times is how the day comes that one call
    reports an outage as a missing show and 404s a user mid-outage.
    """
    if isinstance(exc, AniListNotFound):
        return SourceNotFound(str(exc))
    if isinstance(exc, AniListRateLimited):
        return SourceRateLimited("anilist", str(exc))
    if isinstance(exc, AniListDisabled):
        return SourceUnavailable("anilist", DISABLED_REASON)
    return SourceUnavailable("anilist", str(exc))


class AniListSource:
    """The primary catalogue source (architecture.md §5.0)."""

    name: SourceName = "anilist"

    def __init__(self, client: AniListClient) -> None:
        self.client = client

    @classmethod
    def from_settings(cls, settings: Settings, *, wait_on_rate_limit: bool = True) -> AniListSource:
        """``wait_on_rate_limit=False`` for the app, true for a job (§6)."""
        return cls(AniListClient.from_settings(settings, wait_on_rate_limit=wait_on_rate_limit))

    @classmethod
    def over(
        cls,
        transport: httpx.AsyncBaseTransport,
        *,
        url: str = "http://anilist.test/graphql",
        wait_on_rate_limit: bool = True,
    ) -> AniListSource:
        """A source over a mock transport, for tests and fixture servers."""
        return cls(
            AniListClient(
                url=url,
                min_interval=0.0,
                transport=transport,
                wait_on_rate_limit=wait_on_rate_limit,
            )
        )

    @property
    def configured(self) -> bool:
        """AniList needs no credentials, so it is always worth asking."""
        return True

    async def aclose(self) -> None:
        await self.client.aclose()

    # --- The interface ---

    async def search(self, term: str, *, page: int = 1) -> SearchPage:
        try:
            return await self.client.search(term, page=page)
        except AniListError as exc:
            raise _as_catalog_error(exc) from exc

    async def by_anilist_id(self, anilist_id: int) -> CatalogMedia | None:
        try:
            return await self.client.media(anilist_id)
        except AniListError as exc:
            raise _as_catalog_error(exc) from exc

    async def by_mal_id(self, mal_id: int) -> CatalogMedia | None:
        try:
            return await self.client.media_by_mal_id(mal_id)
        except AniListError as exc:
            raise _as_catalog_error(exc) from exc

    async def season(self, year: int, season: str) -> list[CatalogMedia]:
        try:
            return await self.client.season(year, season)
        except AniListError as exc:
            raise _as_catalog_error(exc) from exc


__all__ = ["DISABLED_REASON", "AniListSource"]
