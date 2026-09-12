"""The weekly offline-catalogue import (M15.5, FR-C6).

One handler, ``import_offline_catalogue``, which downloads both public datasets
and replaces the two tables from them. Registered here rather than in
:mod:`arc.services.catalog.jobs` because it shares nothing with the five
``catalog_*`` handlers: no source, no breaker, no rate limit, no AniList.

**The two sources are independent.** manami failing must not stop Fribb's id
map from being refreshed, and neither failing may leave a table empty — the
whole point of the offline catalogue is that it is the thing that still works,
so a week with a bad download keeps last week's rows and says so in the log.
Each source therefore gets its own try/except and its own transaction (the
importers commit), and the handler raises only when *both* failed: a job that
always succeeds would never be retried, and one that failed when half worked
would redo the half that did.

Idempotent by construction. The import is a replace, and the checksum
short-circuit means running it twice in a row costs the second download and
nothing else.

:func:`import_all` is the part ``python -m arc.cli import-catalogue`` shares,
so the operator's manual run and the Monday one do exactly the same thing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import NamedTuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.services.catalog.offline.download import Downloaded, download
from arc.services.catalog.offline.importer import ImportResult, import_fribb, import_manami
from arc.services.catalog.offline.names import FRIBB, IMPORT_OFFLINE, MANAMI
from arc.services.jobs.registry import JobContext, register

log = logging.getLogger(__name__)


class SourceFailure(NamedTuple):
    """One source that could not be imported."""

    source: str
    error: str

    def __str__(self) -> str:
        return f"{self.source}: {self.error}"


#: What counts as "the download or the file went wrong" rather than a bug.
#: ``httpx.HTTPError`` covers timeouts, connection failures and the
#: ``raise_for_status`` of a missing release; ``OSError`` covers a full disk;
#: ``ValueError`` covers a body that is not the JSON it claims to be
#: (``json.JSONDecodeError`` and zstd's ``ZstdError`` are both subclasses).
DOWNLOAD_ERRORS = (httpx.HTTPError, OSError, ValueError)


async def _load_manami(session: AsyncSession, path: Path, fetched: Downloaded) -> ImportResult:
    """manami's version is inside the file, so the headers are ignored."""
    return await import_manami(session, path, checksum=fetched.checksum)


async def _load_fribb(session: AsyncSession, path: Path, fetched: Downloaded) -> ImportResult:
    """Fribb's file carries no version, so the response headers are it."""
    return await import_fribb(session, path, version=fetched.version, checksum=fetched.checksum)


type Loader = Callable[[AsyncSession, Path, Downloaded], Awaitable[ImportResult]]

#: ``source -> (the setting holding its URL, what to do with the file)``. A
#: table rather than two hand-written blocks, so "one source failing must not
#: affect the other" is one loop with one ``except`` instead of a rule somebody
#: has to remember twice.
SOURCES: tuple[tuple[str, str, Loader], ...] = (
    (MANAMI, "offline_manami_url", _load_manami),
    (FRIBB, "offline_fribb_url", _load_fribb),
)


async def import_source(
    session: AsyncSession,
    settings: Settings,
    source: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ImportResult:
    """Download one source and replace its table. Commits, or raises."""
    url_field, load = next((field, loader) for name, field, loader in SOURCES if name == source)
    url: str = getattr(settings, url_field)
    async with download(url, data_dir=settings.data_dir, client=client) as fetched:
        return await load(session, fetched.path, fetched)


async def import_all(
    session: AsyncSession,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[ImportResult], list[SourceFailure]]:
    """Import both sources; return what worked and what did not.

    Never raises for a source: a failure is a :class:`SourceFailure` in the
    second list and the table it would have replaced is left exactly as it was.
    The caller decides what an empty first list means — the job retries, the
    CLI exits 1.
    """
    results: list[ImportResult] = []
    failures: list[SourceFailure] = []
    for source, _field, _loader in SOURCES:
        try:
            results.append(await import_source(session, settings, source, client=client))
        except DOWNLOAD_ERRORS as exc:
            # The importers commit for themselves, so nothing of this source's
            # work is pending — but a failure part-way through one leaves the
            # session's transaction open, and the next source must not inherit
            # it.
            await session.rollback()
            failures.append(SourceFailure(source, f"{type(exc).__name__}: {exc}"))
            log.warning(
                "offline catalogue source failed; existing rows kept",
                extra={"source": source, "error": str(exc)},
            )
    return results, failures


@register(IMPORT_OFFLINE)
async def import_offline_catalogue(ctx: JobContext) -> None:
    """Download both datasets and replace ``offline_anime`` and ``offline_ids``.

    Raises only when both sources failed, so the queue retries a week in which
    nothing worked and lets one in which half worked stand.
    """
    results, failures = await import_all(ctx.session, ctx.settings)

    for result in results:
        ctx.log.info(
            "offline catalogue source imported",
            extra={
                "source": result.source,
                "version": result.version,
                "rows": result.rows,
                "unchanged": result.unchanged,
            },
        )

    if not results:
        raise RuntimeError(
            "offline catalogue import failed for every source: "
            + "; ".join(str(failure) for failure in failures)
        )


__all__ = [
    "DOWNLOAD_ERRORS",
    "IMPORT_OFFLINE",
    "SOURCES",
    "SourceFailure",
    "import_all",
    "import_offline_catalogue",
    "import_source",
]
