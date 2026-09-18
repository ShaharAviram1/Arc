"""The four side lookups an :class:`~arc.api.anime_schemas.EpisodeOut` needs.

An episode row on a page is more than the ``episodes`` row behind it: the
download percentage comes from ``torrents`` — or, for an episode being served
out of a pack, from the ``torrent_files`` row that claims it (FR-A7,
FR-A11) — the preparing percentage
and the failure sentence from the latest ``transcode`` job's payload (FR-P4),
the duration and track languages from ``renditions`` (FR-P1), and *when Arc
will look again* from the pending ``search_release`` job's ``run_after``
(FR-A7, 2026-09-14). Four tables, none of them joinable into the episode query
without turning one row into several.

So they are four lookups — five queries, since the release is reached two ways
— each taking *every* episode id on the page at once.
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
from datetime import datetime
from typing import cast

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import Job, JobStatus, Rendition, Torrent, TorrentFile, TorrentKind
from arc.services.acquisition.names import SEARCH_RELEASE
from arc.services.media.names import latest_transcode_jobs

#: The payload key both job types happen to spell the same way.
EPISODE_KEY = "episode_id"


@dataclass(frozen=True, slots=True)
class EpisodeRelease:
    """The release behind one episode, and how far *the episode* has got.

    A single and a batch answer "what is downloading here?" with the same
    ``torrents`` row — the group, the title and the resolution are the release's
    and are true of every file in it — and with **different percentages**
    (FR-A11). A single's is the torrent's, because the torrent is the episode.
    A pack's is its own file's: a season pack that is 80 % done says nothing
    about whether *this* episode's file is one of the eighty, and a row that
    read the torrent's number would show a viewer a bar that is not about
    anything they asked for.

    So the percentage is resolved here rather than in the schema, and
    :attr:`batch` is carried beside it so the page can say where the file is
    coming from instead of leaving "1 % for an hour, then done" unexplained.
    """

    torrent: Torrent
    #: 0..1, or ``None`` when nothing has been reported yet.
    progress: float | None
    #: Whether this episode is one file of a pack (FR-A11).
    batch: bool = False


@dataclass(frozen=True, slots=True)
class EpisodeExtras:
    """Everything the four lookups found, keyed by episode id.

    Plain dictionaries with ``.get``: an episode with no torrent, no rendition
    and no transcode job is the ordinary case for anything that has not aired
    yet, and the schema treats a missing entry and a null the same way.
    """

    torrents: dict[int, EpisodeRelease] = field(default_factory=dict)
    renditions: dict[int, Rendition] = field(default_factory=dict)
    transcode_jobs: dict[int, Job] = field(default_factory=dict)
    #: ``episode_id → when the next search runs``, for the episodes that have
    #: a ``search_release`` job waiting (FR-A7). The retry schedule lives in
    #: the job row rather than in a column (FR-A6), so this is the only place
    #: "next try at 23:26" can be read from.
    next_searches: dict[int, datetime] = field(default_factory=dict)


async def torrents_for(
    session: AsyncSession, episode_ids: Sequence[int]
) -> dict[int, EpisodeRelease]:
    """``episode_id → release``, newest row per episode.

    Two queries, because an episode's release is reached two ways (FR-A11).

    The first is the one that has always been here: ``torrents`` keyed on
    ``episode_id``, ordered by id so the last write wins when an episode has
    been re-fetched — the release a user is shown is the one currently
    downloading, not the one that was abandoned last week. ``episode_id`` is
    nullable since batches and ``IN`` never matches a null, so a pack cannot
    come back from it at all.

    The second finds a pack through the claim it holds: a ``torrent_files`` row
    that is still ``wanted``, which is precisely "this pack is holding this
    episode for Arc" (``ux_torrent_files_one_wanted_per_episode`` makes it at
    most one). A claim that has been given back — cancelled, rejected, swept —
    is not a release to show, and leaves the episode's own ``torrents`` row, if
    it ever had one, to answer for it.

    **A live claim wins over a single row.** The two do not overlap in practice
    — ``batch.claim_existing`` runs before Nyaa is asked, so a pack that holds
    the episode is found before a single can be picked — and where they somehow
    did, the claim is the one with bytes moving for this episode right now.
    """
    if not episode_ids:
        return {}
    wanted = set(episode_ids)
    rows = await session.scalars(
        select(Torrent).where(Torrent.episode_id.in_(wanted)).order_by(Torrent.id)
    )
    found: dict[int, EpisodeRelease] = {
        torrent.episode_id: EpisodeRelease(torrent=torrent, progress=torrent.progress)
        for torrent in rows.all()
        if torrent.episode_id is not None
    }

    claims = await session.execute(
        select(TorrentFile, Torrent)
        .join(Torrent, Torrent.id == TorrentFile.torrent_id)
        .where(
            TorrentFile.episode_id.in_(wanted),
            TorrentFile.wanted.is_(True),
            Torrent.kind == TorrentKind.BATCH,
        )
        .order_by(TorrentFile.id)
    )
    for claim, torrent in claims.all():
        if claim.episode_id is None:  # pragma: no cover - the WHERE says otherwise
            continue
        found[claim.episode_id] = EpisodeRelease(
            torrent=torrent, progress=claim.progress, batch=True
        )
    return found


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


async def next_searches_for(
    session: AsyncSession, episode_ids: Sequence[int]
) -> dict[int, datetime]:
    """``episode_id → run_after`` of the soonest pending search (FR-A7).

    FR-A6's retry schedule is a job row and not a column — a search that found
    nothing requeues itself with a delay — so the only record of "Arc will look
    again at 23:26" is the ``run_after`` of that queued job, and a row that
    says ``Searching`` with no idea when is the thing an owner watched for a day
    this week.

    One query for the whole page, keyed on ``payload->>'episode_id'`` the way
    :func:`~arc.services.media.names.latest_transcode_jobs` is. ``pending``
    only: a *running* job is the search happening now, which the row already
    says by being in ``searching``, and a finished one is in the past.
    Soonest-first, because a paused requeue and a retry can both be queued at
    once and the earlier of the two is when something will actually happen.
    """
    if not episode_ids:
        return {}
    wanted = {str(episode_id) for episode_id in episode_ids}
    key = cast(ColumnElement[str], Job.payload[EPISODE_KEY].astext)
    rows = await session.execute(
        select(key, Job.run_after)
        .where(Job.type == SEARCH_RELEASE, Job.status == JobStatus.PENDING, key.in_(wanted))
        .distinct(key)
        .order_by(key, Job.run_after.asc())
    )
    found: dict[int, datetime] = {}
    for raw, run_after in rows.all():
        try:
            found[int(raw)] = run_after
        except TypeError, ValueError:  # pragma: no cover - a hand-written row
            continue
    return found


async def episode_extras(session: AsyncSession, episode_ids: Sequence[int]) -> EpisodeExtras:
    """All four lookups for one page's worth of episodes, in five queries."""
    if not episode_ids:
        return EpisodeExtras()
    return EpisodeExtras(
        torrents=await torrents_for(session, episode_ids),
        renditions=await renditions_for(session, episode_ids),
        transcode_jobs=await latest_transcode_jobs(session, episode_ids),
        next_searches=await next_searches_for(session, episode_ids),
    )


__all__ = [
    "EpisodeExtras",
    "EpisodeRelease",
    "episode_extras",
    "next_searches_for",
    "renditions_for",
    "torrents_for",
]
