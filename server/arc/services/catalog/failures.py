"""What has gone wrong that is **mine** (FR-W6).

Watch Now's failure banner (M16, owner 2026-09-12: "a user's own failures
only"). Two kinds of failure, one question each, and both asked of the
caller's own rows:

* **An episode somebody is waiting for.** Every episode the viewer holds a
  live want on — the reconciler's (FR-A1) or a sample of their own (FR-A8) —
  whose state is ``failed`` (a transcode broke and needs an admin, FR-P4) or
  ``unavailable`` (FR-A6 gave up finding a release and retries daily). Those
  are the two states an episode sits in without anything happening to it: the
  rest of §6's lifecycle is either resting or in flight, and a page that
  reported "downloading" as a failure would cry wolf every evening.
* **A write to MyAnimeList that did not land.** The viewer's own
  ``mal_write_log`` rows with status ``failed`` (FR-M6: "failures surface as a
  badge on the show and in the user's sync page" — and, since M16, on Watch
  Now too, which is the page they actually open).

Three things this deliberately is not.

It is **not global**. A want is per user, the log is per user, and an admin
gets exactly what everybody else does about their own shows; the Admin jobs
tab is where the whole queue lives and it is untouched. The banner is about
the shows the viewer asked for, so a broken transcode of an episode nobody
wants is not news to anybody but an admin looking at the queue.

It is **not a queue view**. A row carries one sentence a person can act on —
the tail of ffmpeg's own complaint, trimmed to
:data:`REASON_LIMIT`, or FR-A6's daily-retry promise — and never the stderr
wall the job payload keeps. The whole thing is in the admin queue for the
person whose job it is to read it.

And it is **dismissable without a server round trip**, which is why every row
carries a stable :attr:`FailureRow.key`. The client remembers the keys it has
been shown and put away (per account, per browser — ``localStorage``), so the
key has to be the same string for the same failure on the next request *and* a
different string once the failure is a new one: an episode that failed again
this morning is not the failure somebody dismissed last night. Hence
``episode:<id>:<state>:<since>`` rather than ``episode:<id>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.core.text import trim_middle
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    MalWriteLog,
    MalWriteStatus,
    User,
    Want,
)
from arc.services.media.names import latest_transcode_jobs

#: How long a failure's reason may be. Two lines of a banner row: enough for
#: "ffmpeg exited 1" and the line of stderr that says what it was doing, and
#: short enough that three of these stacked above the hero are still a strip
#: rather than a page. :func:`~arc.core.text.trim_middle` keeps both ends, so
#: what goes is the middle of the diagnostic rather than the sentence naming it.
REASON_LIMIT = 160

#: The whole banner's cap. Twenty is already more failures than anybody is
#: going to read; the client shows three and folds the rest.
MAX_FAILURES = 20

#: And how many of those may be MyAnimeList writes. A push that failed on a
#: whole list would otherwise be the entire banner, and the twenty-first
#: identical "connection reset" says nothing the tenth did not.
MAL_LIMIT = 10

#: The two episode states that mean "this has stopped, and not because it is
#: finished" (spec §6). Everything else is resting or in flight.
FAILED_STATES = (EpisodeState.FAILED, EpisodeState.UNAVAILABLE)

#: What an ``unavailable`` episode says when it kept no reason of its own:
#: FR-A6's give-up, and the promise that matters more than the give-up does.
NO_RELEASE = "no release found; Arc retries daily"

#: The clause appended to the reason FR-A6 *did* record ("no seeders after 6
#: hours"), because "Arc has stopped looking" is the wrong thing to read into a
#: state the daily retry is still working on.
RETRY_CLAUSE = " — Arc retries daily"

#: What a ``failed`` episode says when its transcode job left nothing behind:
#: the job row may have been swept, and "it broke" is still worth saying.
NO_DETAIL = "preparing this episode failed; an admin can retry it"

#: And the same for a MyAnimeList row whose error column is empty.
NO_MAL_DETAIL = "the update did not go through"

#: The payload key the transcode handler puts its stderr tail under
#: (:mod:`arc.services.media.jobs`). Spelled as a literal here for the reason
#: :mod:`arc.api.anime_schemas` spells it as one: importing that module would
#: drag ffmpeg's planner and probe into a request path that only wants a string.
ERROR_TAIL_KEY = "error_tail"

#: Where a failure Arc cannot date sorts: last. Same constant, same reason, as
#: :data:`arc.services.catalog.progress.UNDATED` — a ``None`` in a
#: newest-first sort is a raise, not an ordering.
UNDATED = datetime.min.replace(tzinfo=UTC)


class FailureKind(StrEnum):
    """Which of the two questions produced a row."""

    EPISODE = "episode"
    MAL = "mal"


@dataclass(frozen=True, slots=True)
class FailureRow:
    """One thing that went wrong on one of the viewer's own shows.

    One dataclass with two nullable halves rather than a union, because the
    banner is one list in one order: the rows are sorted and capped against
    each other, and a client that renders them as one strip wants one shape.
    ``kind`` says which half is filled.
    """

    kind: FailureKind
    #: Stable per failure and different once the failure is a new one; what the
    #: client's dismissal is remembered under. See the module docstring.
    key: str
    #: The show, for the card's title and the link to its page.
    anime: Anime
    #: One sentence, already trimmed (:data:`REASON_LIMIT`).
    reason: str
    #: When it happened, as far as Arc can tell. Null only where nothing dated
    #: it — a hand-written row, or a transcode job that has been swept.
    since: datetime | None
    #: Filled for :attr:`FailureKind.EPISODE`.
    episode: Episode | None = None
    state: EpisodeState | None = None
    #: Filled for :attr:`FailureKind.MAL`: the log row this is, and what it was
    #: trying to write. ``old_value``/``new_value`` are JSONB, so ``Any``.
    log_id: int | None = None
    field: str | None = None
    old_value: Any = None
    new_value: Any = None


def _unavailable_reason(episode: Episode) -> str:
    """FR-A6's answer, with the daily retry always on the end of it.

    The stored reason is the half only Arc knows ("no metadata after 60
    minutes", "no seeders after 6 hours"); the retry is the half the viewer
    needs, because an ``unavailable`` episode somebody still wants is being
    looked for once a day and will quietly fix itself if the release ever
    appears.
    """
    stored = (episode.unavailable_reason or "").strip()
    if not stored:
        return NO_RELEASE
    return f"{trim_middle(stored, limit=REASON_LIMIT - len(RETRY_CLAUSE))}{RETRY_CLAUSE}"


def _transcode_reason(job: Job | None) -> str:
    """The tail of the failed transcode, trimmed to one readable sentence.

    Same precedence as the show page's ``failure_reason``
    (:class:`arc.api.anime_schemas.PrepareState`): the payload's own tail
    first, the job's ``last_error`` behind it, and a plain sentence when there
    is neither — so the two pages never disagree about why an episode broke,
    they only disagree about how much of it they print.
    """
    if job is None:
        return NO_DETAIL
    raw = job.payload.get(ERROR_TAIL_KEY)
    detail = raw if isinstance(raw, str) and raw.strip() else job.last_error
    if not detail:
        return NO_DETAIL
    return trim_middle(detail, limit=REASON_LIMIT)


def _episode_key(episode: Episode, since: datetime | None) -> str:
    """``episode:<id>:<state>:<since>`` — see the module docstring.

    The state is in it because an episode that gave up looking and an episode
    whose transcode broke are two different pieces of news about one row, and
    ``since`` is in it because the same news on a new day is new news: FR-A6
    retries daily, so the episode that failed again this morning must come
    back after last night's dismissal.
    """
    stamp = "" if since is None else since.isoformat()
    return f"episode:{episode.id}:{episode.state.value}:{stamp}"


async def episode_failures(session: AsyncSession, user: User) -> list[FailureRow]:
    """Episodes the viewer is waiting for that have stopped (FR-A6, FR-P4).

    A **live** want, which is the whole definition of "mine": dropped wants are
    kept for retention's sake (FR-T2) and a show somebody stopped waiting for
    is not a failure, while a sample (FR-A8) is somebody waiting for exactly
    one episode and belongs here like any other. ``wants`` is keyed
    ``(user_id, episode_id)``, so one row per episode falls out of the schema
    and there is nothing to deduplicate.

    Two queries at most: this one, and the transcode jobs of whichever episodes
    came back ``failed``. A page with nothing broken on it pays for one.
    """
    rows = await session.execute(
        select(Anime, Episode)
        .join(Episode, Episode.anime_id == Anime.id)
        .join(
            Want,
            and_(
                Want.episode_id == Episode.id,
                Want.user_id == user.id,
                Want.dropped_at.is_(None),
            ),
        )
        .where(Episode.state.in_(FAILED_STATES))
        # Newest first, and the id breaks the tie: an order that is not total
        # is an order that changes between two identical requests.
        .order_by(Episode.state_changed_at.desc().nullslast(), Episode.id.desc())
        .limit(MAX_FAILURES)
    )
    pairs = list(rows.all())
    broken = [episode.id for _, episode in pairs if episode.state is EpisodeState.FAILED]
    jobs = await latest_transcode_jobs(session, broken) if broken else {}

    failures: list[FailureRow] = []
    for anime, episode in pairs:
        job = jobs.get(episode.id)
        if episode.state is EpisodeState.FAILED:
            reason = _transcode_reason(job)
        else:
            reason = _unavailable_reason(episode)
        # The state's own timestamp is the answer wherever there is one — it is
        # the moment the thing being reported happened. The job's
        # ``finished_at`` is the fallback for a row from before that column was
        # filled, and null is the honest last resort: a failure with an
        # invented date would sort above real ones.
        since = episode.state_changed_at or (job.finished_at if job is not None else None)
        failures.append(
            FailureRow(
                kind=FailureKind.EPISODE,
                key=_episode_key(episode, since),
                anime=anime,
                reason=reason,
                since=since,
                episode=episode,
                state=episode.state,
            )
        )
    return failures


async def mal_failures(session: AsyncSession, user: User) -> list[FailureRow]:
    """The viewer's own MyAnimeList writes that did not land (FR-M6).

    ``failed`` and nothing else: ``pending`` is a write still owed and on its
    way, and ``skipped`` is FR-M5's non-write — a conflict MyAnimeList won, or
    a row with no MAL id to send to — neither of which is something that broke.

    Nothing is filtered for having been **reverted**, because nothing can have
    been: a revert is only offered on a write that *succeeded*
    (:func:`arc.services.mal.writelog.is_revertible` refuses anything but
    ``ok``), so a failed row has no revert to hide behind. What does supersede
    one is a later successful write to the same field, and that is why the cap
    is small and the order is newest first rather than why a row is dropped:
    the log is append-only evidence (FR-M5) and this banner reads it, it does
    not edit it.

    One query, the show joined in — ``mal_write_log.anime_id`` is RESTRICT, so
    the row it points at is still there by definition.
    """
    rows = await session.execute(
        select(MalWriteLog, Anime)
        .join(Anime, Anime.id == MalWriteLog.anime_id)
        .where(MalWriteLog.user_id == user.id, MalWriteLog.status == MalWriteStatus.FAILED)
        .order_by(MalWriteLog.created_at.desc(), MalWriteLog.id.desc())
        .limit(MAL_LIMIT)
    )
    return [
        FailureRow(
            kind=FailureKind.MAL,
            # The row's id is enough on its own: the log is append-only, so one
            # id is one attempt for ever and a retry is a new row.
            key=f"mal:{row.id}",
            anime=anime,
            reason=(
                trim_middle(row.error, limit=REASON_LIMIT)
                if row.error and row.error.strip()
                else NO_MAL_DETAIL
            ),
            since=row.created_at,
            log_id=row.id,
            field=row.field,
            old_value=row.old_value,
            new_value=row.new_value,
        )
        for row, anime in rows.all()
    ]


async def failures_for_user(session: AsyncSession, user: User) -> list[FailureRow]:
    """Both kinds, newest first, capped at :data:`MAX_FAILURES` (FR-W6).

    Sorted across the two kinds rather than shown as two lists, because from
    the viewer's side there is one question — "what is broken?" — and a
    transcode that failed an hour ago outranks a MyAnimeList write that failed
    last week whichever of the two Arc happens to ask about first.
    """
    rows = await episode_failures(session, user)
    rows += await mal_failures(session, user)
    rows.sort(key=lambda row: (row.since or UNDATED, row.key), reverse=True)
    return rows[:MAX_FAILURES]


__all__ = [
    "FAILED_STATES",
    "MAL_LIMIT",
    "MAX_FAILURES",
    "NO_DETAIL",
    "NO_MAL_DETAIL",
    "NO_RELEASE",
    "REASON_LIMIT",
    "RETRY_CLAUSE",
    "FailureKind",
    "FailureRow",
    "episode_failures",
    "failures_for_user",
    "mal_failures",
]
