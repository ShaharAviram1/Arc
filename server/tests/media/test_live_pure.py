"""The pure halves of the progressive remux (M20 T6): the front, the gated reader,
the re-emitted playlist, the growing subtitle file, and the duration guard.

No database, no ffmpeg, no qBittorrent: what is asserted here is arithmetic and
ordering, which is where "never read past the download front" lives.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from arc.services.acquisition.qbit import FileInfo
from arc.services.media.live import (
    Front,
    FrontGatedReader,
    LiveIdle,
    LiveOwner,
    contiguous_bytes,
    file_offset,
    largest_video_file,
    offset_agrees,
)
from arc.services.media.live_serve import (
    LIVE_TARGET_DURATION,
    growing_cues,
    held_back,
    live_media_playlist,
)
from arc.services.media.transcode import TranscodeError
from arc.services.playback.progress import truncated_duration

KIB = 1024

# --- contiguous_bytes ----------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "piece", "offset", "size", "expected"),
    [
        # Nothing downloaded.
        ([0, 0, 0, 0], 100, 0, 400, 0),
        # The head only.
        ([2, 2, 0, 0], 100, 0, 400, 200),
        # First and last pieces first (FR-A5): the last piece is in, but the
        # gap after the head means only the head counts.
        ([2, 0, 0, 2], 100, 0, 400, 100),
        ([2, 2, 1, 2], 100, 0, 400, 200),
        # The first piece is downloading (1), not checked: nothing.
        ([1, 2, 2, 2], 100, 0, 400, 0),
        # Everything: clamped to the file.
        ([2, 2, 2, 2], 100, 0, 400, 400),
        # A final short piece: 3.5 pieces of file, all in.
        ([2, 2, 2, 2], 100, 0, 350, 350),
        ([2, 2, 2, 0], 100, 0, 350, 300),
        # A multi-file torrent: the file starts 150 bytes in, inside piece 1.
        # Piece 0 belongs to the file before; it does not matter.
        ([0, 2, 2, 0, 0], 100, 150, 300, 150),
        ([2, 2, 2, 2, 2], 100, 150, 300, 300),
        ([2, 0, 2, 2, 2], 100, 150, 300, 0),
        # The file's first byte is exactly on a piece boundary.
        ([0, 0, 2, 2, 0], 100, 200, 250, 200),
        # Degenerate inputs never claim bytes.
        ([2, 2], 0, 0, 200, 0),
        ([2, 2], 100, 0, 0, 0),
        ([], 100, 0, 200, 0),
    ],
)
def test_contiguous_bytes(
    states: list[int], piece: int, offset: int, size: int, expected: int
) -> None:
    assert contiguous_bytes(states, piece, offset, size) == expected


def test_the_file_offset_is_the_files_before_it() -> None:
    files = [
        FileInfo(index=0, name="Show/notes.txt", size=150, priority=1, progress=0.0),
        FileInfo(index=1, name="Show/[Group] Show - 03.mkv", size=900, priority=1, progress=0.0),
        FileInfo(index=2, name="Show/sample.mkv", size=90, priority=1, progress=0.0),
    ]
    chosen = largest_video_file(files, frozenset({"mkv", "mp4"}))
    assert chosen is not None and chosen.index == 1
    assert file_offset(files, chosen) == 150


def test_piece_range_is_read_from_the_file_list() -> None:
    row = FileInfo.from_json(
        {
            "index": 0,
            "name": "a.mkv",
            "size": 10,
            "priority": 1,
            "progress": 0.1,
            "piece_range": [3, 9],
        },
        position=0,
    )
    assert row is not None and row.piece_range == (3, 9)
    odd = FileInfo.from_json({"name": "a.mkv", "piece_range": [9, 3]}, position=0)
    assert odd is not None and odd.piece_range is None


# --- The gated reader --------------------------------------------------------------


class RecordingSink:
    """ffmpeg's stdin, recording what it was given and checking every write."""

    def __init__(self, front_now: list[int]) -> None:
        self.front_now = front_now
        self.received = bytearray()
        self.writes: list[tuple[int, int]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        offset = len(self.received)
        # The assertion the whole design rests on: no byte at or beyond the
        # front the reader was last told.
        assert offset + len(data) <= self.front_now[0], (offset, len(data), self.front_now[0])
        self.writes.append((offset, len(data)))
        self.received += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _source(tmp_path: Path, size: int) -> tuple[Path, bytes]:
    data = os.urandom(size)
    path = tmp_path / "source.mkv"
    path.write_bytes(data)
    return path, data


async def test_the_reader_never_passes_the_front(tmp_path: Path) -> None:
    """qBittorrent's checked pieces advance over fake time, sometimes not at all.

    The file on disk is whole from the start — qBittorrent pre-allocates — so
    only the front can keep the reader honest.
    """
    piece = 64 * KIB
    size = 37 * piece + 1234
    path, data = _source(tmp_path, size)
    pieces = -(-size // piece)
    states = [0] * pieces
    # First and last pieces first, then in order, with stalls and a hole.
    schedule = [
        [0, pieces - 1],
        [1, 2, 3],
        [],
        [5, 6],
        [4],
        [],
        [],
        list(range(7, 20)),
        [],
        list(range(20, pieces - 1)),
    ]
    front_now = [0]
    time = FakeTime()
    polls = 0

    async def poll() -> Front:
        nonlocal polls
        if polls < len(schedule):
            for index in schedule[polls]:
                states[index] = 2
        polls += 1
        front = contiguous_bytes(states, piece, 0, size)
        front_now[0] = front
        return Front(front, size, complete=front == size)

    sink = RecordingSink(front_now)
    fd = os.open(path, os.O_RDONLY)
    try:
        reader = FrontGatedReader(
            fd, poll, idle_seconds=3600, chunk=50 * KIB, clock=time.clock, sleep=time.sleep
        )
        fed = await reader.run(sink)
    finally:
        os.close(fd)
    assert fed == size
    assert bytes(sink.received) == data
    assert sink.closed
    # It waited between polls once it had caught up, two seconds each.
    assert time.now >= 2.0 * (len(schedule) - 1)
    # No write was larger than a chunk.
    assert max(length for _, length in sink.writes) <= 50 * KIB


async def test_the_reader_stops_when_the_front_does_not_move(tmp_path: Path) -> None:
    size = 10 * KIB
    path, _ = _source(tmp_path, size)
    time = FakeTime()
    front_now = [4 * KIB]

    async def poll() -> Front:
        return Front(4 * KIB, size)

    fd = os.open(path, os.O_RDONLY)
    try:
        reader = FrontGatedReader(fd, poll, idle_seconds=600, clock=time.clock, sleep=time.sleep)
        with pytest.raises(LiveIdle):
            await reader.run(RecordingSink(front_now))
        assert reader.position == 4 * KIB
    finally:
        os.close(fd)
    assert 600 <= time.now <= 604


async def test_a_whole_file_closes_stdin_without_waiting_for_complete(tmp_path: Path) -> None:
    """Every piece hash-checked is the whole file: the client's "complete" is not awaited."""
    size = 8 * KIB
    path, data = _source(tmp_path, size)
    time = FakeTime()
    polls = 0

    async def poll() -> Front:
        nonlocal polls
        polls += 1
        return Front(size, size, complete=False)

    sink = RecordingSink([size])
    fd = os.open(path, os.O_RDONLY)
    try:
        reader = FrontGatedReader(fd, poll, idle_seconds=60, clock=time.clock, sleep=time.sleep)
        assert await reader.run(sink) == size
    finally:
        os.close(fd)
    assert bytes(sink.received) == data and sink.closed
    assert polls == 1 and time.now == 0.0


async def test_a_file_shorter_than_the_front_goes_idle(tmp_path: Path) -> None:
    """A front past the file's end (a temporary name, a truncated file) never spins."""
    path, _ = _source(tmp_path, 4 * KIB)
    time = FakeTime()

    async def poll() -> Front:
        return Front(16 * KIB, 16 * KIB)

    fd = os.open(path, os.O_RDONLY)
    try:
        reader = FrontGatedReader(fd, poll, idle_seconds=300, clock=time.clock, sleep=time.sleep)
        with pytest.raises(LiveIdle):
            await reader.run(RecordingSink([16 * KIB]))
        assert reader.position == 4 * KIB
    finally:
        os.close(fd)
    assert 300 <= time.now <= 304


class HungSink(RecordingSink):
    async def drain(self) -> None:
        await asyncio.Event().wait()


async def test_a_hung_ffmpeg_is_a_transcode_error(tmp_path: Path) -> None:
    path, _ = _source(tmp_path, 4 * KIB)

    async def poll() -> Front:
        return Front(4 * KIB, 4 * KIB)

    fd = os.open(path, os.O_RDONLY)
    try:
        reader = FrontGatedReader(fd, poll, idle_seconds=60, drain_timeout=0.05)
        with pytest.raises(TranscodeError, match="stopped reading"):
            await reader.run(HungSink([4 * KIB]))
    finally:
        os.close(fd)


async def test_an_ffmpeg_that_exits_while_caught_up_is_noticed(tmp_path: Path) -> None:
    path, _ = _source(tmp_path, 8 * KIB)
    time = FakeTime()
    alive = [True]
    polls = 0

    async def poll() -> Front:
        nonlocal polls
        polls += 1
        alive[0] = polls < 3
        return Front(4 * KIB, 8 * KIB)

    fd = os.open(path, os.O_RDONLY)
    try:
        reader = FrontGatedReader(
            fd,
            poll,
            idle_seconds=3600,
            clock=time.clock,
            sleep=time.sleep,
            alive=lambda: alive[0],
        )
        with pytest.raises(BrokenPipeError):
            await reader.run(RecordingSink([4 * KIB]))
    finally:
        os.close(fd)
    assert polls == 3


@pytest.mark.parametrize(
    ("offset", "piece_range", "agrees"),
    [
        (0, None, True),
        (0, (0, 9), True),
        # Files listed before ours add up to where the client says it starts.
        (300 * KIB, (1, 9), True),
        # A hidden pad file after a 100-byte .nfo: ours starts at piece 1, the
        # listed sizes say piece 0.
        (100, (1, 9), False),
        # Not the first file and no range to check: refused.
        (100, None, False),
    ],
)
def test_the_offset_must_agree_with_the_piece_range(
    offset: int, piece_range: tuple[int, int] | None, agrees: bool
) -> None:
    assert offset_agrees(offset, 256 * KIB, piece_range) is agrees


# --- The re-emitted playlist -------------------------------------------------------


FFMPEG_EVENT = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:7
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.006000,
seg_00000.m4s
#EXTINF:6.006000,
seg_00001.m4s
#EXTINF:7.507500,
seg_00002.m4s
"""


def test_the_live_playlist_has_the_fixed_header_and_existing_segments() -> None:
    present = {"init.mp4", "seg_00000.m4s", "seg_00001.m4s"}
    playlist = live_media_playlist(FFMPEG_EVENT, present)
    lines = playlist.text.splitlines()
    assert lines[:7] == [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{LIVE_TARGET_DURATION}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    assert "#EXT-X-TARGETDURATION:7" not in playlist.text
    assert "seg_00002.m4s" not in playlist.text
    assert "#EXT-X-ENDLIST" not in playlist.text
    assert playlist.bounds == ((0.0, 6.006), (6.006, 12.012))
    assert playlist.front_seconds == pytest.approx(12.012)
    assert not playlist.ended


def test_endlist_only_when_ffmpeg_wrote_it_and_everything_is_there() -> None:
    ended = FFMPEG_EVENT + "#EXT-X-ENDLIST\n"
    everything = {"init.mp4", "seg_00000.m4s", "seg_00001.m4s", "seg_00002.m4s"}
    assert live_media_playlist(ended, everything).ended
    assert live_media_playlist(ended, everything).text.endswith("#EXT-X-ENDLIST\n")
    # A missing middle segment ends the list there, and there is no ENDLIST.
    gap = live_media_playlist(ended, everything - {"seg_00001.m4s"})
    assert not gap.ended and len(gap.bounds) == 1
    # No init segment: nothing at all.
    none = live_media_playlist(ended, everything - {"init.mp4"})
    assert none.bounds == () and not none.ended and "EXT-X-MAP" not in none.text


def test_names_outside_the_vocabulary_are_never_listed() -> None:
    hostile = FFMPEG_EVENT.replace("seg_00001.m4s", "../../etc/passwd")
    playlist = live_media_playlist(hostile, {"init.mp4", "seg_00000.m4s", "seg_00002.m4s"})
    assert "passwd" not in playlist.text


# --- The growing subtitle file -----------------------------------------------------


ASS_HEAD = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment
Style: Default,Arial,48,&H00FFFFFF,0,0,2

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,Hello there.
Dialogue: 0,0:00:09.00,0:00:10.50,Default,,0,0,0,,Second line.
"""


def test_a_half_written_line_is_not_read() -> None:
    raw = (ASS_HEAD + "Dialogue: 0,0:00:20.00,0:00:21.00,Default,,0,0,0,,Cut of").encode()
    cues = growing_cues(raw, ass=True)
    assert [cue.text for cue in cues] == ["Hello there.", "Second line."]


def test_cues_past_the_last_finished_segment_are_held_back() -> None:
    cues = growing_cues(ASS_HEAD.encode(), ass=True)
    assert [cue.text for cue in held_back(cues, 6.0)] == ["Hello there."]
    assert len(held_back(cues, 12.0)) == 2


def test_an_srt_block_waits_for_its_blank_line() -> None:
    raw = b"1\n00:00:01,000 --> 00:00:02,000\nOne.\n\n2\n00:00:03,000 --> 00:00:04,000\nTw"
    assert [cue.text for cue in growing_cues(raw, ass=False)] == ["One."]


# --- The owner record and the duration guard ---------------------------------------


def test_the_owner_record_round_trips_and_refuses_garbage() -> None:
    owner = LiveOwner(
        job_id=7,
        info_hash="ab" * 20,
        file_index=0,
        file_size=123,
        path="/x/y.mkv",
        width=1920,
        height=1080,
        subtitle_lang="en",
        audio_lang="ja",
        audio_copy=True,
        expected_duration=1420.5,
    )
    assert LiveOwner.from_json(owner.to_json()) == owner
    assert LiveOwner.from_json("not json") is None
    assert LiveOwner.from_json('{"job_id": 1}') is None


@pytest.mark.parametrize(
    ("duration", "expected", "live", "truncated"),
    [
        (300.0, 1440.0, True, True),
        (1295.0, 1440.0, True, True),
        (1296.0, 1440.0, True, False),
        (1440.0, 1440.0, True, False),
        # A live episode with no expected length: refused, never passed.
        (1440.0, None, True, True),
        (1440.0, 0.0, True, True),
        # No live remux behind it (an offline copy, a trip): not this guard's.
        (300.0, None, False, False),
        (300.0, 1440.0, False, False),
    ],
)
def test_the_duration_guard(
    duration: float, expected: float | None, live: bool, truncated: bool
) -> None:
    assert truncated_duration(duration, expected, live=live) is truncated
