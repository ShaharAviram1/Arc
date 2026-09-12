"""Replacing the offline tables from a downloaded file.

The shape of an import is: read what is loaded, decide whether to bother,
empty the table, insert the new rows in chunks, record the version. All of it
in **one transaction**, which is the only property that matters here — the
tables are read by search and matching, and a window in which
``offline_anime`` is half-replaced is a window in which a search returns a
third of the catalogue. A reader either sees last week's rows or this week's.

``DELETE`` rather than ``TRUNCATE``, deliberately, even though truncating 41k
rows is faster: ``TRUNCATE`` takes an ``ACCESS EXCLUSIVE`` lock, which would
block every concurrent reader of the table for the length of the import. A
``DELETE`` lets them keep reading the old rows until the commit.

The checksum short-circuit is what makes a weekly job on an unchanged file
free. Fribb's list changes most weeks and manami's release is new every week,
but a job retried after a failure downstream, a manual ``import-catalogue``
run, and a week where a release was skipped all arrive at a file Arc already
has — and re-parsing 62 MB to write identical rows is pure cost.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import OfflineAnime, OfflineId, OfflineImport
from arc.services.catalog.offline.download import open_lines
from arc.services.catalog.offline.names import FRIBB, MANAMI
from arc.services.catalog.offline.parse import parse_fribb, parse_manami

log = logging.getLogger(__name__)

#: Rows per ``INSERT``. Two thousand is about a megabyte of parameters per
#: statement — big enough that 41k rows is twenty round trips rather than
#: forty-one thousand, small enough that the driver is not building a
#: statement the size of the file.
CHUNK_ROWS = 2000


@dataclass(slots=True)
class ImportResult:
    """What one source's import did."""

    source: str
    version: str | None
    rows: int
    #: True when the file's checksum matched what is already loaded and
    #: nothing was written. ``rows`` is then what is in the table, not what
    #: was inserted.
    unchanged: bool
    imported_at: datetime


def _chunks(rows: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """Consume ``rows`` lazily in lists of at most ``size``."""
    iterator = iter(rows)
    while chunk := list(islice(iterator, size)):
        yield chunk


async def current_import(session: AsyncSession, source: str) -> OfflineImport | None:
    """The ``offline_imports`` row for ``source``, or ``None``."""
    return await session.get(OfflineImport, source)


async def _record(
    session: AsyncSession,
    *,
    source: str,
    version: str | None,
    rows: int,
    checksum: str | None,
    imported_at: datetime,
) -> None:
    """Upsert the one-row-per-source import log."""
    statement = pg_insert(OfflineImport).values(
        source=source,
        version=version,
        rows=rows,
        checksum=checksum,
        imported_at=imported_at,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[OfflineImport.source],
            set_={
                "version": statement.excluded.version,
                "rows": statement.excluded.rows,
                "checksum": statement.excluded.checksum,
                "imported_at": statement.excluded.imported_at,
            },
        )
    )


async def _unchanged(
    session: AsyncSession, source: str, checksum: str | None
) -> ImportResult | None:
    """The "nothing to do" result, when the file is the one already loaded.

    Requires a non-empty table as well as a matching checksum: a row claiming
    41k rows over an empty table is what a half-finished manual truncate looks
    like, and refusing to re-import it would leave the deployment stuck.
    """
    if checksum is None:
        return None
    loaded = await current_import(session, source)
    if loaded is None or loaded.checksum != checksum or loaded.rows <= 0:
        return None
    model = OfflineAnime if source == MANAMI else OfflineId
    present = await session.scalar(select(func.count()).select_from(model))
    if not present:
        return None
    log.info(
        "offline catalogue unchanged; import skipped",
        extra={"source": source, "version": loaded.version, "rows": loaded.rows},
    )
    return ImportResult(
        source=source,
        version=loaded.version,
        rows=loaded.rows,
        unchanged=True,
        imported_at=loaded.imported_at,
    )


async def _replace(
    session: AsyncSession, model: type[OfflineAnime] | type[OfflineId], rows: Iterable[Any]
) -> int:
    """Empty ``model``'s table and insert ``rows``; return how many."""
    await session.execute(delete(model))
    written = 0
    for chunk in _chunks(rows, CHUNK_ROWS):
        await session.execute(insert(model), chunk)
        written += len(chunk)
    return written


async def import_manami(
    session: AsyncSession,
    path: Path,
    *,
    checksum: str | None = None,
) -> ImportResult:
    """Replace ``offline_anime`` from a manami JSONL file (plain or zstd).

    The version is the release tag out of the file's own header line; a header
    Arc could not read leaves it null rather than failing the import, because
    the rows are worth having either way.

    Commits. The transaction is the unit of atomicity described in the module
    docstring, and it is also what keeps one source's failure from rolling back
    the other's success in the weekly job.
    """
    existing = await _unchanged(session, MANAMI, checksum)
    if existing is not None:
        return existing

    imported_at = datetime.now(UTC)
    with open_lines(path) as lines:
        header, rows = parse_manami(lines)
        written = await _replace(session, OfflineAnime, rows)
    await _record(
        session,
        source=MANAMI,
        version=header.tag,
        rows=written,
        checksum=checksum,
        imported_at=imported_at,
    )
    await session.commit()
    log.info(
        "offline catalogue imported",
        extra={
            "source": MANAMI,
            "version": header.tag,
            "last_update": header.last_update,
            "rows": written,
        },
    )
    return ImportResult(
        source=MANAMI,
        version=header.tag,
        rows=written,
        unchanged=False,
        imported_at=imported_at,
    )


async def import_fribb(
    session: AsyncSession,
    path: Path,
    *,
    version: str | None = None,
    checksum: str | None = None,
) -> ImportResult:
    """Replace ``offline_ids`` from Fribb's ``anime-list-full.json``.

    ``version`` comes from the response headers (there is none in the file);
    the caller is :func:`arc.services.catalog.offline.download.header_version`
    by way of the job. Commits, for the same reason :func:`import_manami` does.
    """
    existing = await _unchanged(session, FRIBB, checksum)
    if existing is not None:
        return existing

    imported_at = datetime.now(UTC)
    with open_lines(path) as lines:
        payload = json.loads("".join(lines))
    rows = parse_fribb(payload)
    written = await _replace(session, OfflineId, rows)
    await _record(
        session,
        source=FRIBB,
        version=version,
        rows=written,
        checksum=checksum,
        imported_at=imported_at,
    )
    await session.commit()
    log.info(
        "offline catalogue imported",
        extra={"source": FRIBB, "version": version, "rows": written},
    )
    return ImportResult(
        source=FRIBB,
        version=version,
        rows=written,
        unchanged=False,
        imported_at=imported_at,
    )


__all__ = [
    "CHUNK_ROWS",
    "ImportResult",
    "current_import",
    "import_fribb",
    "import_manami",
]
