"""Per-user show pins: "Keep files" (FR-T8, M20 T8).

A pin keeps a show's files after watching: the retention sweep's delete after
watching (FR-T1) skips every episode of a show that **any** user has pinned,
source and rendition both (owner 2026-10-13). It is not a promise against a
full disk: the floor eviction (FR-T6) may still remove a pinned show's files as
a last resort, least recently watched first.

Pinning and unpinning are idempotent: the row is keyed on the pair, a second
pin is ``ON CONFLICT DO NOTHING`` and a second unpin deletes nothing. Neither
fetches anything — a pin only changes what is *kept*.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Episode, MediaFile, OfflineCopy, OfflineCopyState, Rendition, ShowPin
from arc.services.media.names import output_dir_for
from arc.services.retention.sweep import RETAINED_STATES, dir_size


async def pin_show(session: AsyncSession, *, user_id: int, anime_id: int) -> None:
    """Pin ``anime_id`` for ``user_id``. A second pin is the same row."""
    await session.execute(
        insert(ShowPin)
        .values(user_id=user_id, anime_id=anime_id)
        .on_conflict_do_nothing(index_elements=[ShowPin.user_id, ShowPin.anime_id])
    )


async def unpin_show(session: AsyncSession, *, user_id: int, anime_id: int) -> None:
    """Unpin ``anime_id`` for ``user_id``. Unpinning an unpinned show is a no-op."""
    await session.execute(
        delete(ShowPin).where(ShowPin.user_id == user_id, ShowPin.anime_id == anime_id)
    )


async def is_pinned(session: AsyncSession, *, user_id: int, anime_id: int) -> bool:
    """Whether ``user_id`` has pinned ``anime_id`` (the show page's toggle)."""
    return await session.get(ShowPin, (user_id, anime_id)) is not None


@dataclass(frozen=True, slots=True)
class PinnedShow:
    """One pinned show and what its retained episodes hold on the disk."""

    anime_id: int
    #: How many users have pinned it.
    users: int
    #: Retained episodes of the show (``ready`` or with a file left behind).
    episodes: int
    #: Source, rendition and offline copy bytes of those episodes.
    bytes: int


async def pinned_usage(session: AsyncSession, settings: Settings) -> list[PinnedShow]:
    """Every pinned show with its retained bytes, largest first (FR-T4, FR-T8).

    Measured the way :func:`arc.services.retention.sweep.retained_usage` is:
    sources from ``media_files.size``, offline copies from
    ``offline_copies.size``, renditions on the disk on a worker thread.
    """
    pinned = (
        await session.execute(select(ShowPin.anime_id, func.count()).group_by(ShowPin.anime_id))
    ).all()
    if not pinned:
        return []
    users = {anime_id: int(count) for anime_id, count in pinned}

    episodes: dict[int, int] = {}
    sources: dict[int, int] = {}
    for anime_id, count, size in (
        await session.execute(
            select(
                Episode.anime_id,
                func.count(func.distinct(Episode.id)),
                func.coalesce(func.sum(MediaFile.size), 0),
            )
            .outerjoin(MediaFile, MediaFile.episode_id == Episode.id)
            .where(Episode.anime_id.in_(users), Episode.state.in_(RETAINED_STATES))
            .group_by(Episode.anime_id)
        )
    ).all():
        episodes[anime_id] = int(count)
        sources[anime_id] = int(size or 0)

    offline: dict[int, int] = {
        anime_id: int(size or 0)
        for anime_id, size in (
            await session.execute(
                select(Episode.anime_id, func.coalesce(func.sum(OfflineCopy.size), 0))
                .join(OfflineCopy, OfflineCopy.episode_id == Episode.id)
                .where(
                    Episode.anime_id.in_(users),
                    Episode.state.in_(RETAINED_STATES),
                    OfflineCopy.state == OfflineCopyState.READY,
                )
                .group_by(Episode.anime_id)
            )
        ).all()
    }

    directories: list[tuple[int, Path]] = [
        (anime_id, Path(stored) if stored else output_dir_for(settings, episode_id))
        for anime_id, episode_id, stored in (
            await session.execute(
                select(Episode.anime_id, Rendition.episode_id, Rendition.dir)
                .join(Episode, Episode.id == Rendition.episode_id)
                .where(Episode.anime_id.in_(users), Episode.state.in_(RETAINED_STATES))
            )
        ).all()
    ]

    def measure() -> dict[int, int]:
        sizes: dict[int, int] = {}
        for anime_id, directory in directories:
            sizes[anime_id] = sizes.get(anime_id, 0) + dir_size(directory)
        return sizes

    renditions = await asyncio.to_thread(measure) if directories else {}
    shows = [
        PinnedShow(
            anime_id=anime_id,
            users=count,
            episodes=episodes.get(anime_id, 0),
            bytes=sources.get(anime_id, 0) + renditions.get(anime_id, 0) + offline.get(anime_id, 0),
        )
        for anime_id, count in users.items()
    ]
    return sorted(shows, key=lambda show: (-show.bytes, show.anime_id))


__all__ = ["PinnedShow", "is_pinned", "pin_show", "pinned_usage", "unpin_show"]
