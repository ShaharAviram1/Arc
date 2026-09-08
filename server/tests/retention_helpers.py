"""Row and file builders for the M10 tests (FR-T1..FR-T4).

Kept out of ``conftest.py`` for the same reason
:mod:`tests.acquisition_helpers` is: these are specific to retention, and a
fixture in ``conftest`` is a fixture every test collects.

Everything here works against a *frozen* clock. Retention is a rule about
days, and a test that waited for them would be a test nobody runs; so the
builders take the moment an episode became ready, or was watched, or was
dropped, and the rules take ``now=`` (:func:`arc.services.retention.sweep.
candidates`) rather than reading the wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    MediaFile,
    Rendition,
    Torrent,
    User,
    Want,
    WatchProgress,
)
from arc.services.media.names import output_dir_for

#: The moment every retention test calls "now". A fixed instant rather than
#: ``datetime.now()`` so that a failure is reproducible and the arithmetic in
#: the test names ("8 days ago") is the arithmetic in the assertions.
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

DAY = timedelta(days=1)


def days_ago(days: float, *, now: datetime = NOW) -> datetime:
    return now - timedelta(days=days)


def write_rendition_dir(settings: Settings, episode_id: int, *, segments: int = 3) -> Path:
    """A plausible HLS output: a playlist and a few segments."""
    directory = output_dir_for(settings, episode_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (directory / "init.mp4").write_bytes(b"0" * 64)
    for index in range(segments):
        (directory / f"seg_{index:05d}.m4s").write_bytes(b"0" * 128)
    return directory


def write_source_file(
    settings: Settings, episode_id: int, *, name: str = "episode.mkv", size: int = 2048
) -> Path:
    """``downloads/<episode id>/<name>``, as qBittorrent would have left it."""
    directory = settings.downloads_dir / str(episode_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"0" * size)
    return path


async def make_retained_episode(
    session: AsyncSession,
    settings: Settings,
    *,
    anilist_id: int,
    number: int = 7,
    state: EpisodeState = EpisodeState.READY,
    ready_at: datetime | None = None,
    created_at: datetime | None = None,
    with_rendition: bool = True,
    with_source: bool = True,
    info_hash: str | None = None,
) -> Episode:
    """A show, an episode with bytes on the disk, and the rows that name them."""
    anime = Anime(
        anilist_id=anilist_id,
        title_romaji=f"Retention Test {anilist_id}",
        status="FINISHED",
        episodes=12,
    )
    session.add(anime)
    await session.flush()

    episode = Episode(
        anime_id=anime.id,
        number=number,
        air_at=ready_at or NOW - 30 * DAY,
        state=state,
        state_changed_at=ready_at or NOW - 30 * DAY,
    )
    session.add(episode)
    await session.flush()

    if with_rendition:
        directory = write_rendition_dir(settings, episode.id)
        session.add(
            Rendition(
                episode_id=episode.id,
                dir=str(directory),
                playlist_path=str(directory / "index.m3u8"),
                ready_at=ready_at or NOW - 30 * DAY,
            )
        )
    if with_source:
        path = write_source_file(settings, episode.id)
        session.add(
            MediaFile(
                episode_id=episode.id,
                path=str(path.resolve()),
                size=path.stat().st_size,
                created_at=created_at or ready_at or NOW - 30 * DAY,
            )
        )
    if info_hash is not None:
        session.add(
            Torrent(
                episode_id=episode.id,
                info_hash=info_hash,
                title="[Group] Retention Test - 07 (1080p).mkv",
                qbit_state="stalledUP",
                progress=1.0,
            )
        )
    await session.flush()
    return episode


async def add_want(
    session: AsyncSession,
    user: User,
    episode: Episode,
    *,
    dropped_at: datetime | None = None,
    drop_reason: str | None = None,
) -> Want:
    want = Want(
        user_id=user.id,
        episode_id=episode.id,
        dropped_at=dropped_at,
        drop_reason=drop_reason,
    )
    session.add(want)
    await session.flush()
    return want


async def add_completion(
    session: AsyncSession, user: User, episode: Episode, *, at: datetime
) -> WatchProgress:
    progress = WatchProgress(
        user_id=user.id,
        episode_id=episode.id,
        position_s=1400.0,
        duration_s=1440.0,
        completed=True,
        completed_at=at,
    )
    session.add(progress)
    await session.flush()
    return progress


__all__ = [
    "DAY",
    "NOW",
    "add_completion",
    "add_want",
    "days_ago",
    "make_retained_episode",
    "write_rendition_dir",
    "write_source_file",
]
