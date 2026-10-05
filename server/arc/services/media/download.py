"""One prepared episode as one MP4 file: the pure half (FR-S7, architecture §5.4a).

A rendition is fragmented MP4 cut into HLS segments, and fMP4 has a property
that makes "download the episode" almost free: ``init.mp4`` followed by every
media segment, in playlist order, byte for byte, *is* a valid fragmented MP4.
ffprobe reads the right duration from it and decodes it cleanly, and so does
every player Arc's viewers are likely to open it in. So the download route
runs no ffmpeg, writes no temporary file and holds nothing in memory: the
response is a virtual concatenation of files that are already on disk, and
everything about it — its length, its ranges, its validator — is arithmetic
over the parts' sizes.

That arithmetic lives here, as pure functions, so the router stays a thin
layer of filesystem checks and headers:

* :func:`playlist_parts` — the part list, read from the rendition's own
  playlist rather than from a directory listing, so the order is the one the
  encoder declared and a stray file in the directory is never part of anyone's
  download.
* :func:`parse_range` and :func:`slice_parts` — a ``Range`` header turned into
  a span, and the span turned into (part, offset, length) reads.
* :func:`download_etag` and :func:`if_range_matches` — a strong validator over
  every part, and the ``If-Range`` comparison that uses it. The in-app offline
  downloader resumes 8 MB chunks against the first ETag it saw, so the ETag
  has to change whenever *any* part does (a ``force`` re-encode reuses every
  name, FR-P5) and must never change when nothing did.
* :func:`content_disposition` — the human filename, built from the database
  and nothing else.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

#: Every file name a rendition may contain apart from its playlist, as the
#: media router's path pattern. ``\A``/``\z`` rather than ``^``/``$`` so a
#: trailing newline cannot ride along (:mod:`arc.api.media_stream` explains
#: why); both pydantic-core's Rust engine and Python 3.14's ``re`` read ``\z``
#: as end-of-input, which is what lets one constant serve the router's path
#: validation and the playlist check below.
PART_NAME_PATTERN: Final[str] = r"\A(init\.mp4|seg_\d{5}\.m4s)\z"

_INIT_NAME: Final = re.compile(r"\Ainit\.mp4\z")
_SEGMENT_NAME: Final = re.compile(r"\Aseg_\d{5}\.m4s\z")

#: ``#EXT-X-MAP:URI="init.mp4"`` — the URI attribute and nothing else. A map
#: that names a byte range of a file is not one this concatenation can serve.
_MAP_URI: Final = re.compile(r'(?:\A|,)URI="([^"]*)"')

#: Tags whose presence means the playlist does not describe "whole files, in
#: order", which is the one shape a byte concatenation reproduces faithfully.
_REFUSED_TAGS: Final = ("#EXT-X-BYTERANGE", "#EXT-X-DISCONTINUITY", "#EXT-X-KEY")

#: The longest title that goes into a filename, in characters. Filesystems
#: cap a name at 255 *bytes*; a CJK title is three bytes a character in UTF-8,
#: so this leaves room for the episode number and the extension.
MAX_TITLE_CHARS: Final = 80

#: What a filename may not contain on any of the systems a download lands on:
#: path separators, Windows' reserved punctuation, and quotes (which would end
#: the quoted ``filename=`` early). A run of them, with the spaces around it,
#: becomes one `` - ``: replaced, not dropped, so "Fate/Zero" stays two words
#: and "Frieren: \"Beyond\"" does not become a row of dashes.
_UNSAFE_FILENAME: Final = re.compile(r'\s*[/\\:*?"<>|][/\\:*?"<>|\s]*')


#: The most digits a ``Range`` bound may have: 2**63 has nineteen.
MAX_RANGE_DIGITS: Final = 19

#: The largest playlist the download will read. ffmpeg writes about 40 bytes a
#: segment, so this is tens of thousands of segments; anything larger is not
#: a playlist the encoder wrote.
MAX_PLAYLIST_BYTES: Final = 1 << 20


class PlaylistError(ValueError):
    """The playlist does not describe a rendition this route will concatenate."""


class RangeMalformed(ValueError):
    """The ``Range`` header is not one this route can read: a 400."""


class RangeNotSatisfiable(ValueError):
    """The ``Range`` header names no byte of the file: a 416."""


@dataclass(frozen=True, slots=True)
class ByteSpan:
    """An inclusive byte range ``first..last`` of a file ``total`` bytes long."""

    first: int
    last: int
    total: int

    @property
    def length(self) -> int:
        return self.last - self.first + 1

    def content_range(self) -> str:
        """The ``Content-Range`` value a 206 for this span carries."""
        return f"bytes {self.first}-{self.last}/{self.total}"


@dataclass(frozen=True, slots=True)
class PartRead:
    """Read ``length`` bytes of part ``index``, starting at ``offset``."""

    index: int
    offset: int
    length: int


def playlist_parts(text: str) -> list[str]:
    """The files of a rendition, in the order they concatenate to an MP4.

    The init segment named by ``#EXT-X-MAP`` first, then every media segment
    URI in playlist order. Strict about everything the encoder never writes,
    because the answer is a list of files the server is about to send: exactly
    one map, naming ``init.mp4``, before any segment; every segment a
    ``seg_NNNNN.m4s`` name; no name twice; at least one segment; and an
    ``#EXT-X-ENDLIST``, without which the playlist may be one an interrupted
    encode left behind and the file it describes is a truncated episode.

    Raises :class:`PlaylistError` for anything else — a foreign name, a path,
    a URL, a byte-range or encrypted playlist. The caller turns that into the
    router's one 404; it is never a partial file.
    """
    lines = [line.strip() for line in text.splitlines()]
    if not lines or lines[0] != "#EXTM3U":
        raise PlaylistError("not an M3U8 playlist")

    init: str | None = None
    segments: list[str] = []
    ended = False
    for line in lines[1:]:
        if not line:
            continue
        if ended:
            raise PlaylistError("content after #EXT-X-ENDLIST")
        if line.startswith(_REFUSED_TAGS):
            raise PlaylistError(f"unsupported tag {line.split(':', 1)[0]}")
        if line.startswith("#EXT-X-MAP:"):
            attributes = line.removeprefix("#EXT-X-MAP:")
            if init is not None or segments or "BYTERANGE=" in attributes:
                raise PlaylistError("the init segment must be one whole file, declared first")
            found = _MAP_URI.search(attributes)
            if found is None or not _INIT_NAME.match(found.group(1)):
                raise PlaylistError("the init segment is not init.mp4")
            init = found.group(1)
            continue
        if line == "#EXT-X-ENDLIST":
            ended = True
            continue
        if line.startswith("#"):
            # #EXTINF, #EXT-X-VERSION, #EXT-X-TARGETDURATION and the like: they
            # describe timing, which the fragments already carry themselves.
            continue
        if init is None:
            raise PlaylistError("a segment before the init segment")
        if not _SEGMENT_NAME.match(line):
            raise PlaylistError("a segment name the encoder never writes")
        segments.append(line)

    if init is None or not segments:
        raise PlaylistError("no init segment or no media segments")
    if not ended:
        raise PlaylistError("no #EXT-X-ENDLIST; the rendition may be incomplete")
    if len(set(segments)) != len(segments):
        raise PlaylistError("a segment named twice")
    return [init, *segments]


def parse_range(header: str | None, total: int) -> ByteSpan | None:
    """The single span a ``Range`` header asks for, or ``None`` for "all of it".

    ``bytes=N-M``, ``bytes=N-`` and the suffix form ``bytes=-N`` (the last N
    bytes) are understood, with ``M`` clamped to the end of the file
    (RFC 9110 §14.1.2). A request for more than one range answers ``None``:
    a 200 with the whole file is a correct reply to it (§14.2), and multipart
    byteranges is machinery no client of this route uses.

    :class:`RangeMalformed` for a header that does not parse — another unit,
    a reversed range, a non-number — matching the router's existing 400 for
    ``kilograms=0-9``. :class:`RangeNotSatisfiable` for one that parses but
    starts at or past the end, or a suffix of zero bytes.
    """
    if header is None:
        return None
    unit, sep, spec = header.strip().partition("=")
    if not sep or unit.strip().lower() != "bytes":
        raise RangeMalformed("not a byte range")

    spans: list[tuple[int | None, int | None]] = []
    for raw in spec.split(","):
        first_raw, dash, last_raw = raw.strip().partition("-")
        if not dash:
            raise RangeMalformed("a range without a dash")
        first = _decimal(first_raw)
        last = _decimal(last_raw)
        if first is None and last is None:
            raise RangeMalformed("a range with neither end")
        if first is not None and last is not None and last < first:
            raise RangeMalformed("a range that ends before it starts")
        spans.append((first, last))

    if len(spans) != 1:
        return None
    first, last = spans[0]
    if first is None:
        # Suffix: the last ``last`` bytes. ``bytes=-0`` names nothing.
        assert last is not None
        if last == 0 or total == 0:
            raise RangeNotSatisfiable("an empty suffix")
        return ByteSpan(first=max(total - last, 0), last=total - 1, total=total)
    if first >= total:
        raise RangeNotSatisfiable("starts past the end")
    end = total - 1 if last is None else min(last, total - 1)
    return ByteSpan(first=first, last=end, total=total)


def _decimal(value: str) -> int | None:
    """A non-negative decimal, ``None`` for an empty string, else malformed.

    ``str.isdigit`` alone would admit "²" and other Unicode digits that
    ``int`` then refuses; ASCII-only is what the grammar says. Length-capped
    too: ``int`` refuses a string of more than 4300 digits with a plain
    ``ValueError`` (a 500), and no file has an offset longer than
    :data:`MAX_RANGE_DIGITS`.
    """
    value = value.strip()
    if not value:
        return None
    if not (value.isascii() and value.isdigit()) or len(value) > MAX_RANGE_DIGITS:
        raise RangeMalformed("not a number")
    return int(value)


def slice_parts(sizes: Sequence[int], span: ByteSpan) -> list[PartRead]:
    """The reads that produce ``span`` of the concatenation of ``sizes``.

    Empty parts contribute nothing and are skipped. ``span`` is trusted to lie
    inside ``sum(sizes)`` — :func:`parse_range` is what guarantees it.
    """
    reads: list[PartRead] = []
    start = 0
    for index, size in enumerate(sizes):
        end = start + size  # exclusive
        if size and end > span.first and start <= span.last:
            offset = max(span.first - start, 0)
            stop = min(span.last + 1, end) - start
            reads.append(PartRead(index=index, offset=offset, length=stop - offset))
        start = end
        if start > span.last:
            break
    return reads


def download_etag(parts: Sequence[tuple[str, int, int]]) -> str:
    """A strong ETag over ``(name, size, mtime_ns)`` for every part, in order.

    A digest rather than the concatenated numbers because there are a couple of
    hundred parts. Hashed over the metadata, not the bytes: the same reasoning
    as the per-segment validator in :mod:`arc.api.media_stream` — size *and*
    mtime, because a re-encode rewrites every name and may well produce a part
    of the same length. The total length leads so two validators can be told
    apart by eye in a log.
    """
    digest = hashlib.sha256()
    total = 0
    for name, size, mtime_ns in parts:
        digest.update(f"{name}\0{size}\0{mtime_ns}\n".encode())
        total += size
    return f'"{total:x}-{digest.hexdigest()[:32]}"'


def if_range_matches(header: str | None, etag: str) -> bool:
    """Whether an ``If-Range`` lets a ``Range`` through (RFC 9110 §13.1.5).

    Absent, it does. Present, it must be this exact strong entity tag: a weak
    one never matches, and a date — this route sends no ``Last-Modified`` —
    cannot be compared and so does not match either. A mismatch is not an
    error; it means "the copy I have is stale, send me the whole new one",
    which is a 200.
    """
    if header is None:
        return True
    return header.strip() == etag


def _clean(text: str) -> str:
    """``text`` with nothing a filename on any system objects to."""
    text = unicodedata.normalize("NFC", text)
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    text = _UNSAFE_FILENAME.sub(" - ", text)
    text = re.sub(r"\s+", " ", text)
    # A leading dot hides a file on Unix; trailing dots and spaces are dropped
    # by Windows. Leading and trailing dashes are what replacing an edge
    # character above leaves behind.
    return text.strip(" .-")[:MAX_TITLE_CHARS].strip(" .-")


def _ascii(text: str) -> str:
    """The closest ASCII spelling: accents folded, everything else dropped."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _clean(folded)


def download_filename(title: str, number: int) -> tuple[str, str]:
    """``(ascii_fallback, utf8_name)`` for episode ``number`` of ``title``.

    ``Frieren - 12.mp4``. The number is zero-padded to two digits so a
    season's worth of downloads sorts in a file manager. A title with nothing
    left after cleaning — all punctuation, or (for the ASCII fallback) all
    CJK — falls back to ``Episode``.
    """
    suffix = f" - {number:02d}.mp4"
    utf8 = _clean(title) or "Episode"
    fallback = _ascii(title) or "Episode"
    return fallback + suffix, utf8 + suffix


def content_disposition(title: str, number: int) -> str:
    """``attachment`` with an ASCII ``filename`` and an RFC 5987 ``filename*``.

    Both, because ``filename*`` is what every current browser uses and
    ``filename`` is what everything else falls back on (RFC 6266 §4.3). The
    fallback is already free of quotes and backslashes (:func:`_clean`), so it
    can sit inside a quoted-string as is; the UTF-8 one is percent-encoded in
    full.
    """
    fallback, utf8 = download_filename(title, number)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(utf8, safe='')}"


__all__ = [
    "MAX_PLAYLIST_BYTES",
    "MAX_RANGE_DIGITS",
    "MAX_TITLE_CHARS",
    "PART_NAME_PATTERN",
    "ByteSpan",
    "PartRead",
    "PlaylistError",
    "RangeMalformed",
    "RangeNotSatisfiable",
    "content_disposition",
    "download_etag",
    "download_filename",
    "if_range_matches",
    "parse_range",
    "playlist_parts",
    "slice_parts",
]
