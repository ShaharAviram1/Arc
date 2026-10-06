"""Reading a trip back for the API (FR-A12). Queries only; nothing is written.

:func:`trip_facts` loads everything one trip's response needs in a fixed
number of queries, whatever the trip's length: the rows and their episodes,
the copies, the live encodes (whose payload carries the percentage), which
episodes still have a source, and each downloading episode's own fraction (a
single's torrent, or its file's claim in a pack, FR-A11). The phase itself is
the pure :func:`~arc.services.trips.phase.trip_phase`.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    OfflineCopy,
    OfflineCopyState,
    Torrent,
    TorrentFile,
    TorrentKind,
    Trip,
    TripEpisode,
    TripState,
)
from arc.services.acquisition.rules import is_storage_held
from arc.services.media.copies import episodes_with_sources, job_progress
from arc.services.media.names import latest_offline_jobs
from arc.services.trips.phase import PhaseFacts, TripPhase, trip_phase


@dataclass(frozen=True, slots=True)
class TripEpisodeFacts:
    """One episode of a trip, as the response renders it."""

    episode_id: int
    number: int
    phase: TripPhase
    progress: float | None
    size: int | None
    delivered: bool
    #: Whether the copy is ``ready`` and there for the device to fetch: phase
    #: ``available``, or ``delivered`` while the copy still stands.
    fetchable: bool = False


@dataclass(frozen=True, slots=True)
class TripFacts:
    """A trip and its episodes, ready to render."""

    trip: Trip
    anime: Anime
    episodes: list[TripEpisodeFacts] = field(default_factory=list)


async def _download_progress(
    session: AsyncSession, episode_ids: Collection[int]
) -> dict[int, float | None]:
    """``episode → 0..1`` for the downloading ones: a claim's own, else the torrent's."""
    if not episode_ids:
        return {}
    found: dict[int, float | None] = {}
    for torrent in (
        await session.scalars(
            select(Torrent).where(Torrent.episode_id.in_(episode_ids)).order_by(Torrent.id)
        )
    ).all():
        if torrent.episode_id is not None:
            found[torrent.episode_id] = torrent.progress
    for claim in (
        await session.scalars(
            select(TorrentFile)
            .join(Torrent, Torrent.id == TorrentFile.torrent_id)
            .where(
                TorrentFile.episode_id.in_(episode_ids),
                TorrentFile.wanted.is_(True),
                Torrent.kind == TorrentKind.BATCH,
            )
        )
    ).all():
        if claim.episode_id is not None:
            found[claim.episode_id] = claim.progress
    return found


def _progress(phase: TripPhase, *, download: float | None, job: Job | None) -> float | None:
    if phase == "downloading":
        return download
    if phase == "preparing":
        return job_progress(job) if job is not None else 0.0
    if phase == "available":
        return 1.0
    return None


async def trip_facts(session: AsyncSession, settings: Settings, trip: Trip) -> TripFacts:
    """Everything one trip's response needs (see the module docstring)."""
    anime = await session.get(Anime, trip.anime_id)
    if anime is None:  # pragma: no cover - the foreign key cascades
        raise LookupError(trip.anime_id)
    rows = (
        await session.execute(
            select(TripEpisode, Episode)
            .join(Episode, Episode.id == TripEpisode.episode_id)
            .where(TripEpisode.trip_id == trip.id)
            .order_by(Episode.number)
        )
    ).all()
    ids = [episode.id for _, episode in rows]
    copies = {
        copy.episode_id: copy
        for copy in (
            await session.scalars(select(OfflineCopy).where(OfflineCopy.episode_id.in_(ids)))
        ).all()
    }
    # Live jobs only, for every copy row: they decide "preparing" for any copy
    # state short of ready (FR-P6's no automatic retry included).
    jobs = await latest_offline_jobs(session, list(copies))
    sources = await episodes_with_sources(session, ids)
    downloading = [episode.id for _, episode in rows if episode.state is EpisodeState.DOWNLOADING]
    downloads = await _download_progress(session, downloading)
    held = trip.state is TripState.ACTIVE and await is_storage_held(session, settings)

    episodes: list[TripEpisodeFacts] = []
    for row, episode in rows:
        copy = copies.get(episode.id)
        phase = trip_phase(
            PhaseFacts(
                episode_state=episode.state,
                row_state=row.state,
                copy_state=copy.state if copy is not None else None,
                has_source=episode.id in sources,
                encoding=episode.id in jobs,
                held=held,
            )
        )
        episodes.append(
            TripEpisodeFacts(
                episode_id=episode.id,
                number=episode.number,
                phase=phase,
                progress=_progress(
                    phase, download=downloads.get(episode.id), job=jobs.get(episode.id)
                ),
                size=copy.size if copy is not None and phase == "available" else None,
                delivered=row.delivered_at is not None,
                fetchable=(
                    phase in ("available", "delivered")
                    and copy is not None
                    and copy.state is OfflineCopyState.READY
                ),
            )
        )
    return TripFacts(trip=trip, anime=anime, episodes=episodes)


__all__ = ["TripEpisodeFacts", "TripFacts", "trip_facts"]
