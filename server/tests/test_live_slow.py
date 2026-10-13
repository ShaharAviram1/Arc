"""The real ffmpeg fed a real MKV through the gated reader (M20 T6, slow).

A SubsPlease 1080p episode from the developer's library is cloned (``cp -c``,
an APFS copy-on-write clone: instant, and the library file is never opened
for writing) into this test's own ``DATA_DIR``. Its first
:data:`FEED_BYTES` are fed to the live remux's ffmpeg through
:class:`~arc.services.media.live.FrontGatedReader`, with a front that grows a
few megabytes per poll as a sequential download's would. Asserted:

* the head probes through a pipe, the plan is copyable, and
  ``expected_duration`` is the whole episode's (the MKV header says so);
* the EVENT playlist grows while the front does, and ends with ``ENDLIST``;
* a run killed half-way and started again from byte 0 writes the very same
  ``seg_00000…`` bytes as an uninterrupted run (deterministic output, the
  restart rule's premise).

Skipped when there is no such file or no ffmpeg (a fresh checkout, CI).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from arc.services.media.live import Front, FrontGatedReader, probe_head
from arc.services.media.live_serve import live_media_playlist
from arc.services.media.plan import RemuxOptions, build_plan, copyable, remux_args

pytestmark = pytest.mark.slow

_HERE = Path(__file__).resolve()
CANDIDATE_ROOTS = [parent / "data" / "downloads" for parent in _HERE.parents]
SUBSPLEASE = re.compile(r"\[SubsPlease\] .* \(1080p\) \[[0-9A-F]{8}\]\.mkv\Z")

#: How much of the episode is fed: about two minutes of 1080p.
FEED_BYTES = 96 << 20
#: How far the front moves per poll, and how often it is polled.
STEP_BYTES = 6 << 20
POLL_SECONDS = 0.05
HEAD_BYTES = 8 << 20


def _library_file() -> Path | None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return None
    for root in CANDIDATE_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/*.mkv")):
            if SUBSPLEASE.search(path.name) and path.stat().st_size > FEED_BYTES:
                return path
    return None


LIBRARY = _library_file()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    if LIBRARY is None:
        pytest.skip("no SubsPlease 1080p MKV in data/downloads, or no ffmpeg")
    target = tmp_path / "data" / "downloads" / "1" / LIBRARY.name
    target.parent.mkdir(parents=True)
    done = subprocess.run(["cp", "-c", str(LIBRARY), str(target)], check=False)
    if done.returncode != 0:
        pytest.skip("cannot clone the library file (not APFS?)")
    target.chmod(0o444)
    return target


def _segments(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.glob("seg_*.m4s"))}


def _served(directory: Path) -> tuple[int, bool]:
    try:
        text = (directory / "video.m3u8").read_text()
    except OSError:
        return 0, False
    present = {entry.name for entry in os.scandir(directory) if entry.is_file()}
    playlist = live_media_playlist(text, present)
    return len(playlist.bounds), playlist.ended


async def _run(
    source: Path,
    directory: Path,
    args: list[str],
    *,
    kill_after: int | None = None,
    subtitle_sizes: list[tuple[int, int]] | None = None,
) -> list[int]:
    """Feed ``source`` to ffmpeg in ``directory``; returns the segment counts seen.

    ``subtitle_sizes`` collects ``(front, size of _work/sub.ass)`` at every poll.
    """
    shutil.rmtree(directory, ignore_errors=True)
    (directory / "_work").mkdir(parents=True)
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        cwd=str(directory),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdin is not None
    front = [HEAD_BYTES]
    counts: list[int] = []

    async def poll() -> Front:
        counts.append(_served(directory)[0])
        if subtitle_sizes is not None:
            sub = directory / "_work" / "sub.ass"
            subtitle_sizes.append((front[0], sub.stat().st_size if sub.exists() else 0))
        if kill_after is not None and front[0] >= kill_after:
            os.killpg(process.pid, signal.SIGKILL)
            raise RuntimeError("killed")
        front[0] = min(front[0] + STEP_BYTES, FEED_BYTES)
        return Front(front[0], FEED_BYTES, complete=front[0] >= FEED_BYTES)

    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        reader = FrontGatedReader(fd, poll, idle_seconds=60, poll_seconds=POLL_SECONDS)
        try:
            await reader.run(process.stdin, first=Front(front[0], FEED_BYTES))
        except RuntimeError:
            await process.wait()
            return counts
    finally:
        os.close(fd)
    code = await asyncio.wait_for(process.wait(), timeout=120)
    assert code == 0
    return counts


async def test_a_real_mkv_through_the_gated_reader(source: Path, tmp_path: Path) -> None:
    with source.open("rb") as handle:
        head = handle.read(HEAD_BYTES)
    probe = await probe_head("ffprobe", head)
    assert probe is not None
    plan = build_plan(probe, source=source, output_dir=tmp_path, sub_lang="en", audio_lang="ja")
    verdict = copyable(probe, plan)
    assert verdict.video, verdict.reason
    # The header's Segment duration: the whole episode, not what was probed.
    assert plan.duration is not None and plan.duration > 600
    args = remux_args(plan, RemuxOptions(audio_copy=verdict.audio_copy), live=True)
    assert args[args.index("-i") + 1] == "pipe:0"

    assert args[args.index("-flush_packets") + 1] == "1"
    whole = tmp_path / "live-a"
    sizes: list[tuple[int, int]] = []
    counts = await _run(source, whole, args, subtitle_sizes=sizes)
    # The subtitle track reaches the disk while the download is still going
    # (``-flush_packets 1``), not only when ffmpeg exits.
    growing = [size for fed, size in sizes if fed < FEED_BYTES]
    assert any(size > 0 for size in growing), sizes
    assert growing == sorted(growing)
    assert len(set(growing)) >= 2, sizes
    assert len(set(counts)) >= 3, counts  # it grew while the front did
    assert counts == sorted(counts)
    text = (whole / "video.m3u8").read_text()
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in text
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
    total, ended = _served(whole)
    assert ended and total >= 10
    first = _segments(whole)
    assert len(first) == total
    assert (whole / "_work" / "sub.ass").stat().st_size > 0

    # Killed half-way: what it had finished is what the whole run wrote.
    killed = tmp_path / "live-b"
    await _run(source, killed, args, kill_after=FEED_BYTES // 2)
    partial, ended = _served(killed)
    assert not ended and 0 < partial < total
    listed = list(_segments(killed))[:partial]
    assert {name: (killed / name).read_bytes() for name in listed} == {
        name: first[name] for name in listed
    }
    # And started again from byte 0: byte-identical, every segment.
    await _run(source, killed, args)
    assert _segments(killed) == first
    assert (killed / "init.mp4").read_bytes() == (whole / "init.mp4").read_bytes()
