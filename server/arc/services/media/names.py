"""The transcode job type, its dedupe key, and its priority (FR-P1, FR-P3).

Handler-free, like :mod:`arc.services.library.names` and
:mod:`arc.services.acquisition.names`, and for the same reason: the API route
that queues a re-encode and the link service that queues the first one both
need to *name* the job, and importing the handler module from either would
drag ffmpeg's whole service package — and the job registry with it — into the
request path.

**The priority rule is here rather than in the handler because it is decided
at enqueue time.** ``jobs.priority`` is read by the claim ``ORDER BY``, so it
has to be right when the row is inserted; a handler could not change the order
of a queue it is already at the front of.

FR-P3 says the queue prioritises the episodes users are closest to reaching,
and that is exactly what :func:`transcode_priority` computes: for every user
who still wants this episode, how many episodes they have left before they get
to it (``number - progress``), and the smallest of those wins. Someone about to
watch episode 5 having finished 4 gives a distance of 1 and a priority of 10;
someone who has watched nothing of a show whose episode 12 just landed gives
120. An episode nobody wants — a manual drop, an admin re-encode — takes
:data:`~arc.models.DEFAULT_PRIORITY`, which sits between the two and behind
anything anybody is actually waiting for.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DEFAULT_PRIORITY, Episode, Job, ListEntry, Want
from arc.services.jobs.queue import enqueue

#: Prepare one episode for playback: probe, plan, burn in, package (FR-P1).
TRANSCODE = "transcode"

#: Payload key holding the episode a transcode is for. The job row is where
#: progress and the failure tail live (:mod:`arc.services.media.jobs` explains
#: why), so "which job is this episode's?" is asked often enough to name.
EPISODE_KEY = "episode_id"

#: Multiplier from "episodes until this user reaches it" to a queue priority.
#: Ten, so that the ordering has room for a tie-break later (a two-hour-old
#: job ahead of a two-minute-old one at the same distance) without a migration.
PRIORITY_PER_EPISODE = 10

#: Priority ceiling from the distance rule. Beyond about a season's worth of
#: episodes the difference stops meaning anything, and without a cap a want on
#: episode 900 of a long-runner would sort behind housekeeping.
MAX_DISTANCE_PRIORITY = 500


def transcode_dedupe_key(episode_id: int) -> str:
    """One queued transcode per episode, however many things ask for it."""
    return f"{TRANSCODE}:{episode_id}"


async def latest_transcode_jobs(
    session: AsyncSession, episode_ids: Sequence[int]
) -> dict[int, Job]:
    """The most recent ``transcode`` job for each of ``episode_ids``.

    One query for a whole show's episode list rather than one per row — the
    show page asks this of twelve episodes at a time, and the answer is what
    fills in the preparing percentage and the failure sentence (FR-P4).

    Matched on ``payload->>'episode_id'`` rather than on a column of its own:
    the payload is where the transcode's own state lives, and
    ``ix_jobs_transcode_episode`` (:mod:`arc.models.job`) is the partial
    expression index that makes that as cheap as a column would have been.

    ``DISTINCT ON`` rather than "fetch them all and keep the last": an episode
    that has been retried a dozen times has a dozen rows, and only the newest
    is ever read. Postgres walks the index and stops at the first row of each
    key, so the answer costs one row per episode instead of one per attempt.
    """
    if not episode_ids:
        return {}
    wanted = {str(episode_id) for episode_id in episode_ids}
    key = cast(ColumnElement[str], Job.payload[EPISODE_KEY].astext)
    rows = await session.scalars(
        select(Job)
        .where(Job.type == TRANSCODE, key.in_(wanted))
        .distinct(key)
        .order_by(key, Job.id.desc())
    )
    latest: dict[int, Job] = {}
    for job in rows.all():
        raw: Any = job.payload.get(EPISODE_KEY)
        try:
            latest[int(raw)] = job
        except TypeError, ValueError:  # pragma: no cover - a hand-written row
            continue
    return latest


async def transcode_priority(session: AsyncSession, episode_id: int) -> int:
    """How soon somebody will reach this episode, as a queue priority (FR-P3).

    ``min(max(number - progress, 0))`` over the users with a live want, times
    :data:`PRIORITY_PER_EPISODE`. No live want — nobody is waiting — is
    :data:`~arc.models.DEFAULT_PRIORITY`.

    ``progress`` is the list entry's, left-joined: a want can outlive the list
    entry by the length of one reconciliation, and a missing entry reads as
    "watched nothing", which is the pessimistic answer and therefore the safe
    one.
    """
    distance = func.greatest(Episode.number - func.coalesce(ListEntry.progress, 0), 0)
    nearest = await session.scalar(
        select(func.min(distance))
        .select_from(Want)
        .join(Episode, Episode.id == Want.episode_id)
        .outerjoin(
            ListEntry,
            (ListEntry.user_id == Want.user_id) & (ListEntry.anime_id == Episode.anime_id),
        )
        .where(Want.episode_id == episode_id, Want.dropped_at.is_(None))
    )
    if nearest is None:
        return DEFAULT_PRIORITY
    return min(int(nearest) * PRIORITY_PER_EPISODE, MAX_DISTANCE_PRIORITY)


async def enqueue_transcode(
    session: AsyncSession,
    episode_id: int,
    *,
    force: bool = False,
    priority: int | None = None,
) -> Job:
    """Queue a transcode for one episode, deduplicated on the episode.

    Flushed, not committed — the caller's transaction is what makes the link
    and its consequence atomic (:mod:`arc.services.library.link`).

    ``force`` asks the handler to throw away an existing rendition and encode
    again (FR-P5). On a dedupe hit the *existing* row is returned unchanged, so
    forcing while a transcode is already queued or running gets that one rather
    than a second: there is already a fresh encode on its way, which is what
    was asked for.
    """
    payload: dict[str, Any] = {"episode_id": episode_id}
    if force:
        payload["force"] = True
    if priority is None:
        priority = await transcode_priority(session, episode_id)
    return await enqueue(
        session,
        TRANSCODE,
        payload,
        priority=priority,
        dedupe_key=transcode_dedupe_key(episode_id),
    )


__all__ = [
    "EPISODE_KEY",
    "MAX_DISTANCE_PRIORITY",
    "PRIORITY_PER_EPISODE",
    "TRANSCODE",
    "enqueue_transcode",
    "latest_transcode_jobs",
    "transcode_dedupe_key",
    "transcode_priority",
]
