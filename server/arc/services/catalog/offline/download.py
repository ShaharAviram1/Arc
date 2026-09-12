"""Fetching the two datasets, and opening what came back.

Both files are large enough that nothing here holds one in memory: the
download streams to a file under ``DATA_DIR/offline`` while hashing it, and
manami's zstd stream is decompressed line by line on the way into the parser.
Sixty-two megabytes of JSON is not a thing to keep in a worker that also runs
ffmpeg.

The sha256 computed while streaming is what makes the weekly job cheap: an
unchanged file is recognised after the download and before the parse, so a
week where neither dataset moved costs two downloads and nothing else.

Version, for Fribb, comes from the response headers — it is a file in a git
repository, not a release, so there is no version *in* it. ``ETag`` first
(GitHub's raw host sends the blob sha), ``Last-Modified`` second, and the date
of the download when the host sends neither, because "when Arc fetched it" is
still more useful than nothing. manami's version is the release tag inside the
file's own header line and is read by the parser instead.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator, Iterator
from compression import zstd
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

log = logging.getLogger(__name__)

#: Connect/read/write/pool budget for one download. Sixty seconds is the *read*
#: timeout between chunks rather than a deadline for the whole transfer, so a
#: 62 MB body on a slow link is fine and a stalled socket is not.
TIMEOUT_SECONDS = 60.0

#: How much is read at a time. 1 MiB: large enough that a 62 MB file is sixty
#: iterations rather than sixty thousand, small enough to be nothing on the
#: heap.
CHUNK_BYTES = 1024 * 1024

#: zstd's frame magic. Used to decide how to open the manami file rather than
#: trusting its extension, which is what lets the test fixture be a plain
#: ``.jsonl`` slice and the production file a ``.jsonl.zst`` and the importer
#: not care.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

#: Where downloads land, under ``DATA_DIR``. A directory of its own so an
#: interrupted run leaves an obvious stray rather than something that looks
#: like media.
SUBDIRECTORY = "offline"


@dataclass(slots=True)
class Downloaded:
    """A file on disk, with what is known about where it came from."""

    path: Path
    #: sha256 of the bytes as received (compressed, for manami).
    checksum: str
    size: int
    #: ``ETag``/``Last-Modified``, or the download date. Ignored for manami,
    #: whose version is inside the file.
    version: str


def header_version(headers: httpx.Headers, *, now: datetime | None = None) -> str:
    """A version string for a file that carries none: ETag, else date."""
    etag = headers.get("etag")
    if etag:
        # ``W/"abc"`` and ``"abc"`` are the same file as far as this is
        # concerned; the quotes are transport syntax.
        return str(etag).removeprefix("W/").strip('"')
    modified = headers.get("last-modified")
    if modified:
        return str(modified)
    return (now or datetime.now(UTC)).date().isoformat()


@asynccontextmanager
async def download(
    url: str,
    *,
    data_dir: Path,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[Downloaded]:
    """Stream ``url`` to a temporary file under ``data_dir``; delete it after.

    A context manager rather than a function returning a path, because the
    thing being handed over is a 6–62 MB temporary file and the one behaviour
    that must not be optional is deleting it — including when the parse or the
    import raises half-way through.

    ``client`` is accepted so a test can pass one built on
    ``httpx.MockTransport``; production passes nothing and gets a client with
    the timeouts above.
    """
    directory = data_dir / SUBDIRECTORY
    directory.mkdir(parents=True, exist_ok=True)

    owned = client is None
    session = client or httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "arc/0.1 (offline catalogue import)"},
    )
    # ``delete=False``: the file has to outlive the handle so the parser can
    # reopen it by path, and the ``finally`` below is what removes it.
    handle = NamedTemporaryFile(dir=directory, prefix="download-", delete=False)
    path = Path(handle.name)
    digest = hashlib.sha256()
    size = 0
    try:
        async with session.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
            handle.flush()
            handle.close()
            version = header_version(response.headers)
        log.info(
            "offline catalogue file downloaded",
            extra={"url": url, "bytes": size, "version": version},
        )
        yield Downloaded(path=path, checksum=digest.hexdigest(), size=size, version=version)
    finally:
        if not handle.closed:
            handle.close()
        path.unlink(missing_ok=True)
        if owned:
            await session.aclose()


@contextmanager
def open_lines(path: Path) -> Iterator[Iterator[str]]:
    """Yield the file's lines as text, decompressing zstd when it is zstd.

    Decided by the frame magic rather than the file name: the download lands in
    a ``NamedTemporaryFile`` with no extension at all, and the test fixture is
    a plain slice of the same format. ``compression.zstd`` is Python 3.14's
    standard library, so this costs no dependency.

    ``errors="replace"`` rather than strict: the dataset is UTF-8 and has never
    been anything else, but a truncated multi-byte sequence at the end of a
    damaged download should cost one mangled title, not the import.
    """
    with path.open("rb") as probe:
        compressed = probe.read(len(ZSTD_MAGIC)) == ZSTD_MAGIC

    if compressed:
        with zstd.open(path, "rt", encoding="utf-8", errors="replace") as stream:
            yield iter(stream)
    else:
        with path.open("rt", encoding="utf-8", errors="replace") as stream:
            yield iter(stream)


__all__ = [
    "CHUNK_BYTES",
    "SUBDIRECTORY",
    "TIMEOUT_SECONDS",
    "ZSTD_MAGIC",
    "Downloaded",
    "download",
    "header_version",
    "open_lines",
]
