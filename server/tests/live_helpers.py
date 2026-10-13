"""A downloading episode, a qBittorrent whose pieces arrive over time, and a
fake ffmpeg that reads Matroska from stdin (M20 T6).

The fake ffmpeg answers only the live invocation (``-i pipe:0``) itself: it
records every byte it is fed into ``record``, writes ``init.mp4`` and one
``seg_NNNNN.m4s`` per ``segment_bytes`` received into its working directory,
keeps an EVENT ``video.m3u8`` naming them, writes the subtitle track at EOF and
``#EXT-X-ENDLIST`` last. Every other invocation is handed to the M7/M20 fake
of :mod:`tests.media_helpers`, so a remux run after it is still recorded in
the same marker file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, EpisodeState, Torrent, TorrentKind, Want
from arc.services.acquisition import qbit as qbit_module
from tests.acquisition_helpers import (
    QbitStub,
    force_transport,
    make_anime,
    make_episodes,
    make_user,
)
from tests.media_helpers import REMUX_ASS, REMUX_SOURCE_PROBE, install_fake_ffmpeg, write_script

KIB = 1024
MIB = 1024 * KIB

_LIVE_FFMPEG = """\
import os, signal, sys, time
args = sys.argv[1:]
BASE = {base!r}
if "pipe:0" not in args:
    os.execv(BASE, [BASE] + args)
RECORD = {record!r}
MARKER = {marker!r}
SEG = {segment_bytes!r}
FAIL = {fail!r}
DELAY = {delay!r}
ASS = {ass!r}
if MARKER:
    with open(MARKER, "a") as handle:
        handle.write("live\\n")
    with open(MARKER + ".argv", "a") as handle:
        handle.write("\\0".join(args) + "\\n")
segments = []
def playlist(end):
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:6",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:EVENT",
             "#EXT-X-INDEPENDENT-SEGMENTS", '#EXT-X-MAP:URI="init.mp4"']
    for name in segments:
        lines += ["#EXTINF:6.000000,", name]
    if end:
        lines.append("#EXT-X-ENDLIST")
    with open("video.m3u8.tmp", "w") as handle:
        handle.write("\\n".join(lines) + "\\n")
    os.replace("video.m3u8.tmp", "video.m3u8")
def segment(data):
    name = "seg_%05d.m4s" % len(segments)
    with open(name + ".tmp", "wb") as handle:
        handle.write(b"S" + data[:32])
    os.replace(name + ".tmp", name)
    segments.append(name)
    playlist(False)
received = bytearray()
written = 0
# The subtitle output grows as events are muxed (``-flush_packets 1``): the
# script's head and first dialogue once the first segment is out, the rest at
# EOF. Only when the flag is there, so the test notices if it goes missing.
sub = open(args[-1], "w") if "-c:s" in args else None
progressive = "-flush_packets" in args
head, _, rest = ASS.partition("Dialogue: 0,0:00:02.00")
rest = "Dialogue: 0,0:00:02.00" + rest
with open(RECORD, "ab") as record:
    while True:
        chunk = sys.stdin.buffer.read1(65536)
        if not chunk:
            break
        record.write(chunk)
        record.flush()
        received += chunk
        if FAIL and len(received) > SEG:
            sys.stderr.write("[matroska @ 0x1] stub failure\\n")
            sys.exit(1)
        if not os.path.exists("init.mp4"):
            with open("init.mp4", "wb") as handle:
                handle.write(b"INIT" + bytes(received[:16]))
        while len(received) >= (written + 1) * SEG:
            segment(bytes(received[written * SEG:(written + 1) * SEG]))
            written += 1
            if sub is not None and progressive and head:
                sub.write(head)
                sub.flush()
                head = ""
        if DELAY:
            time.sleep(DELAY)
if len(received) > written * SEG:
    segment(bytes(received[written * SEG:]))
if sub is not None:
    sub.write(head + rest)
    sub.close()
playlist(True)
sys.exit(0)
"""


def install_live_ffmpeg(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    record: Path,
    marker: Path | None = None,
    segment_bytes: int = 512 * KIB,
    fail: bool = False,
    delay: float = 0.0,
) -> None:
    """The fake ffmpeg/ffprobe pair, with the live branch first on ``PATH``."""
    base = install_fake_ffmpeg(
        directory / "base", monkeypatch, marker=marker, source_probe=REMUX_SOURCE_PROBE
    )
    write_script(
        directory / "live",
        "ffmpeg",
        _LIVE_FFMPEG.format(
            base=str(base / "ffmpeg"),
            record=str(record),
            marker=str(marker) if marker else "",
            segment_bytes=segment_bytes,
            fail=fail,
            delay=delay,
            ass=REMUX_ASS,
        ),
    )
    monkeypatch.setenv("PATH", str(directory / "live"), prepend=os.pathsep)


def write_served(directory: Path, *, segments: int = 2, ended: bool = False) -> None:
    """A live directory with ``segments`` six-second segments served (and its init)."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-TARGETDURATION:6",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    (directory / "init.mp4").write_bytes(b"INIT")
    for index in range(segments):
        name = f"seg_{index:05d}.m4s"
        (directory / name).write_bytes(b"OLD" + bytes([index]))
        lines += ["#EXTINF:6.000000,", name]
    if ended:
        lines.append("#EXT-X-ENDLIST")
    (directory / "video.m3u8").write_text("\n".join(lines) + "\n")


@dataclass
class Download:
    """One demanded episode downloading a single, and the stub serving it."""

    episode: Episode
    torrent: Torrent
    stub: QbitStub
    source: Path
    data: bytes
    piece_size: int
    file_name: str

    @property
    def pieces(self) -> int:
        return -(-len(self.data) // self.piece_size)

    def advance(self, per_poll: int, *, first_last: bool = True) -> None:
        """Every ``pieceStates`` call checks ``per_poll`` more pieces, in order.

        With ``first_last`` the first call also checks the last piece, as
        ``firstLastPiecePrio`` makes the real client do.
        """
        info_hash = self.torrent.info_hash.lower()
        states = self.stub.piece_states[info_hash]
        calls = [0]

        def hook(requested: str) -> None:
            if requested != info_hash:
                return
            if calls[0] == 0 and first_last:
                states[-1] = 2
            calls[0] += 1
            done = 0
            for index, value in enumerate(states):
                if value != 2 and done < per_poll:
                    states[index] = 2
                    done += 1
            checked = sum(1 for value in states if value == 2)
            for row in self.stub.torrents:
                if row["hash"].lower() == info_hash:
                    row["progress"] = checked / len(states)

        self.stub.on_piece_states = hook

    def stall_after(self, pieces: int) -> None:
        """Pieces arrive up to ``pieces`` and then never again."""
        info_hash = self.torrent.info_hash.lower()
        states = self.stub.piece_states[info_hash]

        def hook(requested: str) -> None:
            for index in range(min(pieces, len(states))):
                states[index] = 2

        self.stub.on_piece_states = hook


async def a_download(
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    anilist_id: int,
    file_name: str = "[SubsPlease] Sousou no Frieren - 03 (1080p) [ABCD1234].mkv",
    size: int = 3 * MIB + 12_345,
    piece_size: int = 256 * KIB,
    demanded: bool = True,
    info_hash: str = "ab" * 20,
    kind: TorrentKind = TorrentKind.SINGLE,
) -> Download:
    """A downloading episode 3 with its single in the stub, the file pre-allocated."""
    anime = await make_anime(session, anilist_id=anilist_id)
    episodes = await make_episodes(session, anime, 3)
    episode = episodes[-1]
    episode.state = EpisodeState.DOWNLOADING
    user = await make_user(session, f"live-{anilist_id}@arc.test")
    torrent = Torrent(
        episode_id=episode.id if kind is TorrentKind.SINGLE else None,
        info_hash=info_hash,
        magnet=f"magnet:?xt=urn:btih:{info_hash}",
        title=file_name,
        qbit_state="downloading",
        progress=0.0,
        kind=kind,
    )
    session.add(torrent)
    session.add(
        Want(
            user_id=user.id,
            episode_id=episode.id,
            demanded_at=datetime.now(UTC) if demanded else None,
        )
    )
    await session.flush()
    await session.commit()

    data = os.urandom(size)
    source = settings.downloads_dir / str(episode.id) / file_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)  # qBittorrent pre-allocates: the size says nothing

    stub = QbitStub()
    stub.add_torrent(info_hash, name=file_name, progress=0.0, size=size, num_complete=7)
    stub.add_files(info_hash, [file_name], size=size, progress=0.0)
    pieces = -(-size // piece_size)
    stub.piece_states[info_hash] = [0] * pieces
    stub.piece_sizes[info_hash] = piece_size
    monkeypatch.setattr(
        qbit_module.QbitClient,
        "__init__",
        force_transport(qbit_module.QbitClient, stub.transport()),
    )
    return Download(
        episode=episode,
        torrent=torrent,
        stub=stub,
        source=source,
        data=data,
        piece_size=piece_size,
        file_name=file_name,
    )


__all__ = ["KIB", "MIB", "Download", "a_download", "install_live_ffmpeg", "write_served"]
