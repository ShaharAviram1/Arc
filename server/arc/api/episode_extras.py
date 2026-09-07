"""The three side lookups an :class:`~arc.api.anime_schemas.EpisodeOut` needs.

An episode row on a page is more than the ``episodes`` row behind it: the
download percentage comes from ``torrents`` (FR-A7), the preparing percentage
and the failure sentence from the latest ``transcode`` job's payload (FR-P4),
and the duration and track languages from ``renditions`` (FR-P1). Three tables,
none of them joinable into the episode query without turning one row into
several.

So they are three queries, each taking *every* episode id on the page at once.
That is the whole content of this module and the reason it exists rather than
living in one of the two routers: the show page and the home page render the
same episode shape, and a helper that only the show page had is how the home
page came to render a ``preparing`` episode with no percentage on it. Keyed on
episode id rather than on anime id for the same reason — the home page's rows
come from a dozen different shows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Job, Rendition, Torrent
from arc.services.media.names import latest_transcode_jobs


@dataclass(frozen=True, slots=True)
class EpisodeExtras:
    """Everything the three lookups found, keyed by episode id.

    Plain dictionaries with ``.get``: an episode with no torrent, no rendition
    and no transcode job is the ordinary case for anything that has not aired
    yet, and the schema treats a missing entry and a null the same way.
    """

    torrents: dict[int, Torrent] = field(default_factory=dict)
    renditions: dict[int, Rendition] = field(default_factory=dict)
    transcode_jobs: dict[int, Job] = field(default_factory=dict)


async def torrents_for(session: AsyncSession, episode_ids: Sequence[int]) -> dict[int, Torrent]:
    """``episode_id → torrent``, newest row per episode.

    Ordered by id so the last write wins when an episode has been re-fetched:
    the release a user is shown is the one currently downloading, not the one
    that was abandoned last week.
    """
    if not episode_ids:
        return {}
    rows = await session.scalars(
        select(Torrent).where(Torrent.episode_id.in_(set(episode_ids))).order_by(Torrent.id)
    )
    return {torrent.episode_id: torrent for torrent in rows.all()}


async def renditions_for(session: AsyncSession, episode_ids: Sequence[int]) -> dict[int, Rendition]:
    """``episode_id → rendition``.

    One row per episode by construction (``renditions.episode_id`` is unique),
    so this is a plain map and never has to decide which of two wins.
    """
    if not episode_ids:
        return {}
    rows = await session.scalars(
        select(Rendition).where(Rendition.episode_id.in_(set(episode_ids)))
    )
    return {rendition.episode_id: rendition for rendition in rows.all()}


async def episode_extras(session: AsyncSession, episode_ids: Sequence[int]) -> EpisodeExtras:
    """All three lookups for one page's worth of episodes, in three queries."""
    if not episode_ids:
        return EpisodeExtras()
    return EpisodeExtras(
        torrents=await torrents_for(session, episode_ids),
        renditions=await renditions_for(session, episode_ids),
        transcode_jobs=await latest_transcode_jobs(session, episode_ids),
    )


__all__ = ["EpisodeExtras", "episode_extras", "renditions_for", "torrents_for"]
