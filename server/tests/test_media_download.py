"""The pure half of the whole-episode download (FR-S7).

Playlist → part list, ``Range`` → span → reads, the ETag and ``If-Range``, and
the filename. No database and no files: the route's own tests in
:mod:`tests.test_media_stream` cover the bytes on the wire.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import IO, Any

import anyio
import pytest
from starlette.types import Message, Scope

from arc.services.media.download import (
    MAX_PLAYLIST_BYTES,
    PART_NAME_PATTERN,
    ByteSpan,
    PartRead,
    PlaylistError,
    RangeMalformed,
    RangeNotSatisfiable,
    content_disposition,
    download_etag,
    download_filename,
    if_range_matches,
    parse_range,
    playlist_parts,
    slice_parts,
)

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.006000,
seg_00000.m4s
#EXTINF:6.006000,
seg_00001.m4s
#EXTINF:2.502000,
seg_00002.m4s
#EXT-X-ENDLIST
"""


# --- Playlist -> parts ----------------------------------------------------------


def test_an_encoder_playlist_is_init_then_segments_in_order() -> None:
    assert playlist_parts(PLAYLIST) == [
        "init.mp4",
        "seg_00000.m4s",
        "seg_00001.m4s",
        "seg_00002.m4s",
    ]


def test_crlf_line_endings_are_read_the_same() -> None:
    assert playlist_parts(PLAYLIST.replace("\n", "\r\n")) == playlist_parts(PLAYLIST)


def test_the_order_is_the_playlists_not_the_names() -> None:
    swapped = PLAYLIST.replace("seg_00000.m4s", "TMP").replace("seg_00001.m4s", "seg_00000.m4s")
    swapped = swapped.replace("TMP", "seg_00001.m4s")

    assert playlist_parts(swapped)[1:3] == ["seg_00001.m4s", "seg_00000.m4s"]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # A foreign name, a path, a URL, a near miss of the pattern.
        ("seg_00001.m4s", "notes.txt"),
        ("seg_00001.m4s", "../seg_00001.m4s"),
        ("seg_00001.m4s", "/etc/hosts"),
        ("seg_00001.m4s", "https://example.com/seg_00001.m4s"),
        ("seg_00001.m4s", "seg_1.m4s"),
        ("seg_00001.m4s", "init.mp4"),
        # The init segment must be init.mp4, whole, and declared first.
        ('URI="init.mp4"', 'URI="seg_00009.m4s"'),
        ('URI="init.mp4"', 'URI="../init.mp4"'),
        ('URI="init.mp4"', 'URI="init.mp4",BYTERANGE="100@0"'),
        # A segment named twice.
        ("seg_00002.m4s", "seg_00000.m4s"),
        # No end: an interrupted encode's playlist.
        ("#EXT-X-ENDLIST", ""),
        # Shapes a byte concatenation cannot reproduce.
        ("#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-BYTERANGE:100@0"),
        ("#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-DISCONTINUITY"),
        ("#EXT-X-INDEPENDENT-SEGMENTS", '#EXT-X-KEY:METHOD=AES-128,URI="k"'),
        # Not a playlist at all.
        ("#EXTM3U", "hello"),
    ],
)
def test_anything_the_encoder_never_writes_is_refused(before: str, after: str) -> None:
    assert before in PLAYLIST
    with pytest.raises(PlaylistError):
        playlist_parts(PLAYLIST.replace(before, after, 1))


def test_a_segment_before_the_map_is_refused() -> None:
    text = PLAYLIST.replace('#EXT-X-MAP:URI="init.mp4"\n', "").replace(
        "#EXT-X-ENDLIST", '#EXT-X-MAP:URI="init.mp4"\n#EXT-X-ENDLIST'
    )
    with pytest.raises(PlaylistError):
        playlist_parts(text)


def test_a_playlist_with_no_segments_is_refused() -> None:
    with pytest.raises(PlaylistError):
        playlist_parts('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXT-X-ENDLIST\n')


def test_a_line_after_the_end_is_refused() -> None:
    with pytest.raises(PlaylistError):
        playlist_parts(PLAYLIST + "seg_00003.m4s\n")


def test_the_router_vocabulary_is_the_same_constant() -> None:
    from arc.api.media_stream import SEGMENT_PATTERN

    assert SEGMENT_PATTERN == PART_NAME_PATTERN
    assert re.match(PART_NAME_PATTERN, "seg_00000.m4s\n") is None


# --- Range ----------------------------------------------------------------------


def test_no_range_header_is_the_whole_file() -> None:
    assert parse_range(None, 1000) is None


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=0-99", 0, 99),
        ("bytes=100-", 100, 999),
        ("bytes=-100", 900, 999),
        ("bytes=-5000", 0, 999),  # a suffix longer than the file is all of it
        ("bytes=990-5000", 990, 999),  # clamped
        ("bytes=999-999", 999, 999),
        ("Bytes = 0-0", 0, 0),
    ],
)
def test_a_single_range_is_read(header: str, first: int, last: int) -> None:
    span = parse_range(header, 1000)

    assert span == ByteSpan(first=first, last=last, total=1000)
    assert span.content_range() == f"bytes {first}-{last}/1000"
    assert span.length == last - first + 1


def test_more_than_one_range_is_the_whole_file() -> None:
    assert parse_range("bytes=0-9, 20-29", 1000) is None


@pytest.mark.parametrize(
    "header",
    [
        "kilograms=0-9",
        "bytes",
        "bytes=",
        "bytes=9-0",
        "bytes=a-b",
        "bytes=-",
        "bytes=1",
        "bytes=²-3",
        # Past Python's 4300-digit int limit, which would otherwise be a 500.
        "bytes=0-" + "9" * 5000,
        "bytes=-" + "9" * 20,
    ],
)
def test_a_malformed_range_is_refused(header: str) -> None:
    with pytest.raises(RangeMalformed):
        parse_range(header, 1000)


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5000-6000", "bytes=-0"])
def test_a_range_outside_the_file_is_unsatisfiable(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, 1000)


def test_slices_cross_part_boundaries() -> None:
    sizes = [10, 100, 100]

    assert slice_parts(sizes, ByteSpan(0, 209, 210)) == [
        PartRead(0, 0, 10),
        PartRead(1, 0, 100),
        PartRead(2, 0, 100),
    ]
    assert slice_parts(sizes, ByteSpan(5, 114, 210)) == [
        PartRead(0, 5, 5),
        PartRead(1, 0, 100),
        PartRead(2, 0, 5),
    ]
    assert slice_parts(sizes, ByteSpan(10, 10, 210)) == [PartRead(1, 0, 1)]
    assert slice_parts(sizes, ByteSpan(209, 209, 210)) == [PartRead(2, 99, 1)]


def test_slices_skip_empty_parts() -> None:
    assert slice_parts([0, 5, 0, 5], ByteSpan(0, 9, 10)) == [PartRead(1, 0, 5), PartRead(3, 0, 5)]


def test_slices_always_add_up_to_the_span() -> None:
    sizes = [7, 13, 1, 29, 50]
    total = sum(sizes)
    data = bytes(range(total))
    parts: list[bytes] = []
    start = 0
    for size in sizes:
        parts.append(data[start : start + size])
        start += size

    for first in range(total):
        for last in range(first, total, 7):
            reads = slice_parts(sizes, ByteSpan(first, last, total))
            got = b"".join(parts[r.index][r.offset : r.offset + r.length] for r in reads)
            assert got == data[first : last + 1]


# --- ETag and If-Range ----------------------------------------------------------


def test_the_etag_is_strong_and_stable() -> None:
    parts = [("init.mp4", 10, 1), ("seg_00000.m4s", 100, 2)]

    etag = download_etag(parts)

    assert etag.startswith('"') and etag.endswith('"') and not etag.startswith("W/")
    assert download_etag(list(parts)) == etag
    assert etag.startswith('"6e-')  # the total, in hex


@pytest.mark.parametrize(
    "changed",
    [
        [("init.mp4", 10, 1), ("seg_00000.m4s", 100, 3)],  # a newer mtime
        [("init.mp4", 10, 1), ("seg_00000.m4s", 101, 2)],  # a new size
        [("seg_00000.m4s", 100, 2), ("init.mp4", 10, 1)],  # another order
    ],
)
def test_the_etag_moves_when_any_part_does(changed: list[tuple[str, int, int]]) -> None:
    assert download_etag(changed) != download_etag([("init.mp4", 10, 1), ("seg_00000.m4s", 100, 2)])


def test_if_range_is_a_strong_exact_comparison() -> None:
    etag = '"6e-abc"'

    assert if_range_matches(None, etag)
    assert if_range_matches(etag, etag)
    assert not if_range_matches(f"W/{etag}", etag)
    assert not if_range_matches('"6e-abd"', etag)
    assert not if_range_matches("Sat, 01 Jan 2000 00:00:00 GMT", etag)


# --- Filename -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "number", "fallback", "utf8"),
    [
        ("Frieren", 12, "Frieren - 12.mp4", "Frieren - 12.mp4"),
        ("Frieren", 3, "Frieren - 03.mp4", "Frieren - 03.mp4"),
        ("Fate/Zero", 1, "Fate - Zero - 01.mp4", "Fate - Zero - 01.mp4"),
        ('Say "Hi"', 1, "Say - Hi - 01.mp4", "Say - Hi - 01.mp4"),
        ("../../etc/passwd", 1, "etc - passwd - 01.mp4", "etc - passwd - 01.mp4"),
        ("Pokémon", 100, "Pokemon - 100.mp4", "Pokémon - 100.mp4"),
        ("葬送のフリーレン", 5, "Episode - 05.mp4", "葬送のフリーレン - 05.mp4"),
        ("a\nb\tc\x00d", 1, "a b c d - 01.mp4", "a b c d - 01.mp4"),
        ("...", 1, "Episode - 01.mp4", "Episode - 01.mp4"),
        ("C:\\Windows\\<x>|y?*", 1, "C - Windows - x - y - 01.mp4", "C - Windows - x - y - 01.mp4"),
    ],
)
def test_the_filename_is_sanitised(title: str, number: int, fallback: str, utf8: str) -> None:
    assert download_filename(title, number) == (fallback, utf8)


def test_a_long_title_is_cut() -> None:
    fallback, utf8 = download_filename("x" * 500, 1)

    assert len(utf8) < 100
    assert fallback == utf8


def test_the_header_carries_both_forms() -> None:
    header = content_disposition("葬送のフリーレン", 1)

    assert header.startswith('attachment; filename="Episode - 01.mp4"; ')
    assert header.endswith(
        "filename*=UTF-8''%E8%91%AC%E9%80%81%E3%81%AE%E3%83%95%E3%83%AA%E3%83%BC%E3%83%AC%E3%83%B3%20-%2001.mp4"
    )
    assert header.isascii()


# --- Streaming ------------------------------------------------------------------


def open_rendition(directory: Path, contents: dict[str, bytes]) -> Any:
    """A real rendition directory, opened the way the route opens one."""
    from arc.api import media_stream

    directory.mkdir(parents=True, exist_ok=True)
    segments = [name for name in contents if name != "init.mp4"]
    playlist = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
    playlist += "".join(f"#EXTINF:6.0,\n{name}\n" for name in segments)
    (directory / "index.m3u8").write_text(playlist + "#EXT-X-ENDLIST\n")
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    return media_stream._open_rendition(directory, 1)


def fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


async def test_a_disconnect_stops_the_reads_and_closes_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body is pulled chunk by chunk, and a client that leaves ends the pull.

    Driven at the ASGI level with the route's own response class: four
    1000-byte parts read 16 bytes at a time, and a disconnect after the first
    few chunks. Far fewer than 4000 bytes go out, the part that was open is
    closed, and so is the rendition directory.
    """
    from arc.api import media_stream

    monkeypatch.setattr(media_stream, "DOWNLOAD_CHUNK", 16)
    opened: list[IO[bytes]] = []
    real_open = media_stream._open_part

    def recording_open(dir_fd: int, part: Any) -> IO[bytes]:
        handle = real_open(dir_fd, part)
        opened.append(handle)
        return handle

    monkeypatch.setattr(media_stream, "_open_part", recording_open)

    contents = {"init.mp4": b"i" * 1000} | {
        f"seg_{index:05d}.m4s": bytes([index]) * 1000 for index in range(3)
    }
    rendition = open_rendition(tmp_path / "r", contents)
    dir_fd = rendition.fd
    reads = slice_parts([1000] * 4, ByteSpan(0, 3999, 4000))
    response = media_stream._PartsResponse(
        media_stream._stream_parts(rendition, reads, 1), rendition=rendition
    )

    sent: list[Message] = []
    gone = anyio.Event()

    async def send(message: Message) -> None:
        sent.append(message)
        if len(sent) >= 4:
            gone.set()

    async def receive() -> Message:
        await gone.wait()
        return {"type": "http.disconnect"}

    scope: Scope = {"type": "http", "asgi": {"spec_version": "2.3"}, "method": "GET"}
    await response(scope, receive, send)

    delivered = sum(len(m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
    assert 0 < delivered < 4000
    assert opened and all(handle.closed for handle in opened)
    assert not fd_is_open(dir_fd)


async def test_a_response_that_never_starts_still_closes_the_directory(tmp_path: Path) -> None:
    """A disconnect before the first chunk: the generator never runs at all."""
    from arc.api import media_stream

    rendition = open_rendition(tmp_path / "r", {"init.mp4": b"i", "seg_00000.m4s": b"s"})
    dir_fd = rendition.fd
    response = media_stream._PartsResponse(
        media_stream._stream_parts(rendition, [], 1), rendition=rendition
    )

    async def send(message: Message) -> None:
        raise OSError("the client is gone")

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    scope: Scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET"}
    with pytest.raises(Exception):  # noqa: B017 - Starlette's ClientDisconnect
        await response(scope, receive, send)
    assert not fd_is_open(dir_fd)


async def test_a_part_rewritten_mid_download_aborts_rather_than_splices(tmp_path: Path) -> None:
    """Headers promised one rendition; a re-encode underneath must not be mixed in."""
    from arc.api import media_stream

    rendition = open_rendition(tmp_path / "r", {"init.mp4": b"i", "seg_00000.m4s": b"a" * 100})
    (tmp_path / "r" / "seg_00000.m4s").write_bytes(b"b" * 120)

    stream = media_stream._stream_parts(rendition, [PartRead(1, 0, 100)], 1)
    with pytest.raises(media_stream._PartChanged):
        await anext(stream)
    rendition.close()


async def test_a_part_replaced_with_the_same_size_and_mtime_aborts(tmp_path: Path) -> None:
    """Identity, not only size and time: a different inode is a different file."""
    from arc.api import media_stream

    directory = tmp_path / "r"
    rendition = open_rendition(directory, {"init.mp4": b"i", "seg_00000.m4s": b"a" * 100})
    original = directory / "seg_00000.m4s"
    before = original.stat()
    impostor = directory / "impostor"
    impostor.write_bytes(b"b" * 100)
    os.utime(impostor, ns=(before.st_atime_ns, before.st_mtime_ns))
    impostor.replace(original)
    after = original.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert after.st_ino != before.st_ino

    stream = media_stream._stream_parts(rendition, [PartRead(1, 0, 100)], 1)
    with pytest.raises(media_stream._PartChanged):
        await anext(stream)
    rendition.close()


async def test_reads_stay_in_the_directory_that_was_checked(tmp_path: Path) -> None:
    """Swap the rendition directory for another after the headers: still the old bytes.

    Every part is opened relative to the directory descriptor, so renaming the
    directory away and putting a different one (or a link) at its path cannot
    redirect the reads still to come.
    """
    from arc.api import media_stream

    directory = tmp_path / "r"
    rendition = open_rendition(directory, {"init.mp4": b"i" * 10, "seg_00000.m4s": b"a" * 10})
    directory.rename(tmp_path / "moved")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "seg_00000.m4s").write_bytes(b"X" * 10)
    directory.symlink_to(elsewhere, target_is_directory=True)

    stream = media_stream._stream_parts(rendition, [PartRead(1, 0, 10)], 1)
    assert await anext(stream) == b"a" * 10
    await stream.aclose()
    rendition.close()


def test_a_refused_rendition_closes_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 out of :func:`_open_rendition` leaves no descriptor behind."""
    from fastapi import HTTPException

    from arc.api import media_stream

    opened: list[int] = []
    real_open = os.open

    def recording(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            opened.append(fd)
        return fd

    directory = tmp_path / "r"
    directory.mkdir()
    (directory / "index.m3u8").write_text("not a playlist")
    monkeypatch.setattr(os, "open", recording)
    with pytest.raises(HTTPException):
        media_stream._open_rendition(directory, 1)
    monkeypatch.undo()

    assert len(opened) == 1
    assert not fd_is_open(opened[0])


def test_an_oversized_playlist_is_refused(tmp_path: Path) -> None:
    from fastapi import HTTPException

    from arc.api import media_stream

    directory = tmp_path / "r"
    directory.mkdir()
    (directory / "index.m3u8").write_bytes(b"#EXTM3U\n" + b"#" * MAX_PLAYLIST_BYTES)
    with pytest.raises(HTTPException) as refused:
        media_stream._open_rendition(directory, 1)
    assert refused.value.status_code == 404
