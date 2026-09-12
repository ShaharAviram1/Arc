"""Importing the offline catalogue: the replace, the job, the CLI (M15.5).

Three properties, and they are the only ones that matter here.

**Replace, not merge.** The tables are a copy of somebody else's file; an
import that left last week's rows behind would accumulate every title that was
ever renamed. Importing twice must leave exactly what importing once left.

**Never empty.** The offline catalogue exists because AniList went down for
three days. A failed download is the case it was built for, so a source that
fails keeps the rows it already had and the *other* source still imports.

**Cheap when nothing changed.** The weekly job downloads 14 MB whatever
happens; it must not also re-parse and re-write 41k rows when the file is the
one already loaded.

The network is an ``httpx.MockTransport`` serving the captured fixture slices,
which is how the rest of the suite mocks httpx (tests/anilist_mock.py). The
files are served *uncompressed* — the importer sniffs zstd's frame magic rather
than trusting a file name, so a plain slice exercises the same path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.cli import cmd_import_catalogue
from arc.config import Settings
from arc.models import Job, JobStatus, OfflineAnime, OfflineId, OfflineImport
from arc.services.catalog.offline import jobs as offline_jobs
from arc.services.catalog.offline.download import download, header_version
from arc.services.catalog.offline.importer import import_fribb, import_manami
from arc.services.catalog.offline.names import FRIBB, IMPORT_OFFLINE, MANAMI
from arc.services.jobs import JobContext, registered_types

pytestmark = pytest.mark.pg

FIXTURES = Path(__file__).parent / "fixtures" / "offline"
MANAMI_SLICE = FIXTURES / "manami-slice.jsonl"
FRIBB_SLICE = FIXTURES / "fribb-slice.json"

#: What the fixture slice holds, asserted once here so every count below reads
#: as "all of them" rather than as a number.
SLICE_ANIME_ROWS = len(MANAMI_SLICE.read_text().splitlines()) - 1
SLICE_ID_ROWS = len(json.loads(FRIBB_SLICE.read_text()))

FRIBB_ETAG = 'W/"8ba2b45168348a528d8e8dec379a048f"'


def checksum_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def offline_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Settings whose ``DATA_DIR`` is this test's temporary directory.

    The download writes a temporary file under ``DATA_DIR/offline``; without
    this it would be the developer's real ``server/data``.
    """
    return settings.model_copy(update={"data_dir": tmp_path})


def transport(
    *,
    manami: bytes | None = None,
    fribb: bytes | None = None,
    manami_status: int = 200,
    fribb_status: int = 200,
) -> httpx.MockTransport:
    """Serve the two files, or a failure for either."""
    manami_body = MANAMI_SLICE.read_bytes() if manami is None else manami
    fribb_body = FRIBB_SLICE.read_bytes() if fribb is None else fribb

    def handler(request: httpx.Request) -> httpx.Response:
        if "manami" in str(request.url):
            return httpx.Response(manami_status, content=manami_body)
        return httpx.Response(fribb_status, content=fribb_body, headers={"ETag": FRIBB_ETAG})

    return httpx.MockTransport(handler)


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Point the job's downloader at a mock transport.

    Patched at :mod:`arc.services.catalog.offline.jobs`'s own name rather than
    deeper, so everything below it — the streaming, the sha256, the header
    version, the zstd sniff, the parse, the replace — is the production code.
    ``knobs`` is what a test mutates to make a source fail.
    """
    knobs: dict[str, Any] = {}

    def patched(url: str, *, data_dir: Path, client: httpx.AsyncClient | None = None) -> Any:
        mock = httpx.AsyncClient(transport=transport(**knobs))
        return download(url, data_dir=data_dir, client=mock)

    monkeypatch.setattr(offline_jobs, "download", patched)
    yield knobs


def context(session: AsyncSession, settings: Settings) -> JobContext:
    job = Job(id=1, type=IMPORT_OFFLINE, payload={}, status=JobStatus.RUNNING)
    return JobContext(
        job=job, session=session, settings=settings, log=logging.getLogger("test.offline")
    )


async def counts(session: AsyncSession) -> tuple[int, int]:
    anime = await session.scalar(select(func.count()).select_from(OfflineAnime))
    ids = await session.scalar(select(func.count()).select_from(OfflineId))
    return int(anime or 0), int(ids or 0)


# --- the importers ----------------------------------------------------------


async def test_the_manami_slice_becomes_rows_and_a_version(db_session: AsyncSession) -> None:
    result = await import_manami(db_session, MANAMI_SLICE, checksum="abc")

    assert (result.rows, result.unchanged) == (SLICE_ANIME_ROWS, False)
    assert result.version == "2026-27"

    logged = await db_session.get(OfflineImport, MANAMI)
    assert logged is not None
    assert (logged.rows, logged.version, logged.checksum) == (SLICE_ANIME_ROWS, "2026-27", "abc")

    frieren = await db_session.scalar(select(OfflineAnime).where(OfflineAnime.mal_id == 52991))
    assert frieren is not None
    assert frieren.anilist_id == 154587
    assert frieren.search_text.startswith("sousou no frieren")
    assert frieren.synonyms


async def test_the_fribb_slice_becomes_rows_with_the_header_version(
    db_session: AsyncSession,
) -> None:
    result = await import_fribb(db_session, FRIBB_SLICE, version="etag-value", checksum="def")

    assert (result.rows, result.version) == (SLICE_ID_ROWS, "etag-value")

    spirited_away = await db_session.scalar(select(OfflineId).where(OfflineId.mal_id == 199))
    assert spirited_away is not None
    assert spirited_away.tmdb_movie_id == 129
    assert spirited_away.imdb_id == "tt0245429"


async def test_importing_twice_replaces_rather_than_accumulates(
    db_session: AsyncSession,
) -> None:
    await import_manami(db_session, MANAMI_SLICE, checksum="one")
    await import_manami(db_session, MANAMI_SLICE, checksum="two")

    anime, _ = await counts(db_session)
    assert anime == SLICE_ANIME_ROWS


async def test_a_changed_file_replaces_every_row(db_session: AsyncSession, tmp_path: Path) -> None:
    await import_manami(db_session, MANAMI_SLICE, checksum="one")

    lines = MANAMI_SLICE.read_text().splitlines()
    smaller = tmp_path / "smaller.jsonl"
    smaller.write_text("\n".join(lines[:3]) + "\n")
    result = await import_manami(db_session, smaller, checksum="two")

    assert result.rows == 2
    anime, _ = await counts(db_session)
    assert anime == 2


async def test_an_unchanged_checksum_skips_the_whole_import(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Proved by handing it a *different* file under the same checksum: if the
    parse ran at all, the row count would change."""
    await import_manami(db_session, MANAMI_SLICE, checksum="same")

    other = tmp_path / "other.jsonl"
    other.write_text("\n".join(MANAMI_SLICE.read_text().splitlines()[:2]) + "\n")
    result = await import_manami(db_session, other, checksum="same")

    assert result.unchanged is True
    assert result.rows == SLICE_ANIME_ROWS
    anime, _ = await counts(db_session)
    assert anime == SLICE_ANIME_ROWS


async def test_a_matching_checksum_over_an_empty_table_imports_anyway(
    db_session: AsyncSession,
) -> None:
    """The log says 29 rows and the table has none: that is a state to repair,
    not one to preserve."""
    await import_manami(db_session, MANAMI_SLICE, checksum="same")
    await db_session.execute(OfflineAnime.__table__.delete())

    result = await import_manami(db_session, MANAMI_SLICE, checksum="same")

    assert result.unchanged is False
    anime, _ = await counts(db_session)
    assert anime == SLICE_ANIME_ROWS


async def test_no_checksum_means_always_import(db_session: AsyncSession) -> None:
    await import_manami(db_session, MANAMI_SLICE, checksum=None)
    result = await import_manami(db_session, MANAMI_SLICE, checksum=None)

    assert result.unchanged is False


# --- the job ----------------------------------------------------------------


def test_the_job_type_is_registered() -> None:
    assert IMPORT_OFFLINE in registered_types()


async def test_the_job_imports_both_sources(
    db_session: AsyncSession, offline_settings: Settings, network: dict[str, Any]
) -> None:
    await offline_jobs.import_offline_catalogue(context(db_session, offline_settings))

    assert await counts(db_session) == (SLICE_ANIME_ROWS, SLICE_ID_ROWS)

    manami = await db_session.get(OfflineImport, MANAMI)
    fribb = await db_session.get(OfflineImport, FRIBB)
    assert manami is not None and manami.version == "2026-27"
    assert fribb is not None
    # The quotes and the ``W/`` are transport syntax, not part of the version.
    assert fribb.version == FRIBB_ETAG.removeprefix("W/").strip('"')
    assert manami.checksum == checksum_of(MANAMI_SLICE)


async def test_running_the_job_twice_changes_nothing_and_skips_the_parse(
    db_session: AsyncSession, offline_settings: Settings, network: dict[str, Any]
) -> None:
    ctx = context(db_session, offline_settings)
    await offline_jobs.import_offline_catalogue(ctx)
    first = await db_session.get(OfflineImport, MANAMI)
    assert first is not None
    imported_at = first.imported_at

    await offline_jobs.import_offline_catalogue(ctx)

    assert await counts(db_session) == (SLICE_ANIME_ROWS, SLICE_ID_ROWS)
    again = await db_session.get(OfflineImport, MANAMI)
    assert again is not None
    # Untouched, because the import was skipped rather than redone.
    assert again.imported_at == imported_at


async def test_one_source_failing_keeps_its_rows_and_does_not_stop_the_other(
    db_session: AsyncSession, offline_settings: Settings, network: dict[str, Any]
) -> None:
    ctx = context(db_session, offline_settings)
    await offline_jobs.import_offline_catalogue(ctx)
    await db_session.execute(OfflineId.__table__.delete())

    network["manami_status"] = 503
    # Does not raise: half of it worked, and the half that did not still has
    # last week's rows.
    await offline_jobs.import_offline_catalogue(ctx)

    assert await counts(db_session) == (SLICE_ANIME_ROWS, SLICE_ID_ROWS)


async def test_the_job_raises_only_when_every_source_failed(
    db_session: AsyncSession, offline_settings: Settings, network: dict[str, Any]
) -> None:
    network.update(manami_status=500, fribb_status=500)

    with pytest.raises(RuntimeError, match="every source"):
        await offline_jobs.import_offline_catalogue(context(db_session, offline_settings))

    assert await counts(db_session) == (0, 0)


async def test_a_body_that_is_not_the_dataset_fails_that_source_only(
    db_session: AsyncSession, offline_settings: Settings, network: dict[str, Any]
) -> None:
    """A captive portal, a GitHub error page: a 200 with the wrong bytes."""
    network["fribb"] = b"<html>not json</html>"

    await offline_jobs.import_offline_catalogue(context(db_session, offline_settings))

    anime, ids = await counts(db_session)
    assert (anime, ids) == (SLICE_ANIME_ROWS, 0)
    assert await db_session.get(OfflineImport, FRIBB) is None


async def test_the_downloaded_file_is_deleted_afterwards(
    db_session: AsyncSession, offline_settings: Settings, network: dict[str, Any]
) -> None:
    await offline_jobs.import_offline_catalogue(context(db_session, offline_settings))

    leftovers = list((offline_settings.data_dir / "offline").glob("download-*"))
    assert leftovers == []


def test_the_version_of_a_file_that_carries_none() -> None:
    """ETag, then Last-Modified, then the date it was fetched."""
    assert header_version(httpx.Headers({"etag": '"abc"'})) == "abc"
    assert header_version(httpx.Headers({"etag": 'W/"abc"'})) == "abc"
    modified = "Wed, 10 Sep 2026 10:00:00 GMT"
    assert header_version(httpx.Headers({"last-modified": modified})) == modified
    assert header_version(httpx.Headers({})).startswith("20")


# --- the CLI ----------------------------------------------------------------


async def test_the_cli_reports_every_source_and_exits_zero(
    db_session: AsyncSession,
    offline_settings: Settings,
    network: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await cmd_import_catalogue(db_session, offline_settings, argparse.Namespace())

    assert code == 0
    out = capsys.readouterr().out
    assert "manami" in out and "fribb" in out
    assert "imported" in out and "2026-27" in out
    assert f"{SLICE_ANIME_ROWS} rows" in out
    assert await counts(db_session) == (SLICE_ANIME_ROWS, SLICE_ID_ROWS)


async def test_the_cli_says_unchanged_on_a_second_run(
    db_session: AsyncSession,
    offline_settings: Settings,
    network: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    await cmd_import_catalogue(db_session, offline_settings, argparse.Namespace())
    capsys.readouterr()

    assert await cmd_import_catalogue(db_session, offline_settings, argparse.Namespace()) == 0
    assert "unchanged" in capsys.readouterr().out


async def test_the_cli_exits_one_when_a_source_failed(
    db_session: AsyncSession,
    offline_settings: Settings,
    network: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stricter than the job on purpose: a person ran this and is watching."""
    network["fribb_status"] = 404

    code = await cmd_import_catalogue(db_session, offline_settings, argparse.Namespace())

    assert code == 1
    captured = capsys.readouterr()
    assert "FAILED" in captured.err
    assert "untouched" in captured.err
    # The half that worked still landed.
    anime, _ = await counts(db_session)
    assert anime == SLICE_ANIME_ROWS
