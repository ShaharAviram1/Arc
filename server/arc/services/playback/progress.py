"""Watch progress, completion, and what completion costs (FR-S2, FR-S4, FR-W3).

The rules are small and the consequences are not, so they live here as
functions over a session and the routers in :mod:`arc.api.playback` do nothing
but call them.

**Completion is sticky.** ``position / duration >= 0.90`` sets
``watch_progress.completed`` (FR-S4) and nothing automatic ever clears it: the
player reports every ten seconds, and a user who finishes an episode and then
scrubs back to the opening would otherwise un-watch it — and, with the list
advance below, would have Arc argue with MyAnimeList about an episode they
plainly watched. Only the explicit ``DELETE …/watched`` (:func:`unmark_watched`)
takes it back.

**Completion is also the only automatic thing that moves a list entry.** The
first time it flips true, ``list_entries.progress`` advances to this episode's
number if the number is higher, ``updated_by`` becomes ``arc`` and
``mal_dirty`` becomes true — which is M9's cue and the *only* automatic one
(architecture.md §5.5 step 2). Never downwards: FR-M4 says progress written
from an automatic event never decreases, and the ``>`` below is where that is
true. Status is left alone even when progress reaches the last episode — FR-W2
gives "completed" to the user to set, and a show marked completed by a rewatch
of episode 12 would be Arc making a claim nobody made.

**The upsert is one statement, and "was it already complete?" comes out of
that same statement's ``RETURNING``.** At one POST per ten seconds per player
a read-then-write would be two round trips and a race; ``INSERT … ON CONFLICT
DO UPDATE`` is one of each.

The subtlety is which "before" the answer is measured against, and it is worth
spelling out because the obvious version is wrong. Reading the row in a CTE
alongside the upsert reads it *at the statement's snapshot* — the state before
this transaction started. Two players finishing the same episode at the same
moment (two tabs, a phone and a laptop, a beacon racing the ten-second timer)
both take that snapshot before either has committed, so both see "not
completed", both answer ``newly_completed``, and the episode is announced
twice: two next-episode prompts, and in M9 two pushes to MyAnimeList.

``ON CONFLICT DO UPDATE`` does not have that problem, because it does not use
the snapshot: the second statement blocks on the first's row lock and then
re-reads the row it committed. PostgreSQL 18's ``RETURNING old.…`` /
``new.…`` (the version architecture.md pins) is what exposes that re-read — so
``was_completed`` below is the value the *loser of the race* actually
overwrote, not the value it saw when it started. Exactly one caller can find
``old.completed`` false, whatever the clock says and however many of them
arrive together.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import false, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    ListEntry,
    ListStatus,
    UpdatedBy,
    WatchProgress,
)
from arc.models.enums import MalWriteCause
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.mal.names import enqueue_mal_push, is_linked
from arc.services.mal.writelog import FIELD_PROGRESS, record_pending

#: Watched at ninety per cent (FR-S4). Ends run about that long past the last
#: thing anybody watches for.
COMPLETION_FRACTION = 0.90

#: Resume only past this much (FR-S2): a few seconds in is the beginning, and
#: dropping a user thirty seconds into a cold open they have not seen is worse
#: than starting them at zero.
RESUME_MIN_S = 10.0

#: …and only before this much of it. Past ninety-five per cent the episode is
#: over in every sense except the flag, and resuming into the credits is an
#: offer to watch nothing.
RESUME_MAX_FRACTION = 0.95

#: How many rows "continue watching" carries (FR-W1). Twenty is more than a
#: home page renders and far less than a year of half-finished episodes.
CONTINUE_LIMIT = 20

#: And how far in an episode has to be to count as *started*. The same ten
#: seconds as :data:`RESUME_MIN_S`, and for the same reason: a row written by
#: a player that was open for four seconds is not something to come back to.
CONTINUE_MIN_POSITION_S = 10.0


def is_completed(position_s: float, duration_s: float) -> bool:
    """FR-S4's rule, and the only definition of it.

    A non-positive duration is never complete rather than a division by zero:
    the manual mark (FR-W3) stores ``0``/``0`` for an episode Arc has no
    rendition of, and that row must not read as a *computed* completion.
    """
    if duration_s <= 0:
        return False
    return position_s / duration_s >= COMPLETION_FRACTION


def resume_position(row: WatchProgress | None, duration_s: float | None) -> float | None:
    """Where the player should open, or ``None`` for the beginning (FR-S2).

    ``duration_s`` is the *rendition's*, not the one the client last reported:
    it is the file's own length, and it is the number the 95 % ceiling has to
    be measured against for the answer to be the same on every device.
    """
    if row is None or row.completed:
        return None
    if row.position_s <= RESUME_MIN_S:
        return None
    if duration_s is not None and duration_s > 0:
        if row.position_s >= RESUME_MAX_FRACTION * duration_s:
            return None
    return row.position_s


@dataclass(frozen=True, slots=True)
class ProgressOutcome:
    """What one progress report changed.

    ``newly_completed`` is what the client uses to offer the next episode
    (FR-S5) and what M9 will hang a push off; ``list_progress`` is the number
    the show page's badge would now read, and is ``None`` whenever nothing
    touched the list — which is every report but the one that finishes an
    episode.
    """

    completed: bool
    newly_completed: bool
    list_progress: int | None = None


async def record_progress(
    session: AsyncSession,
    *,
    user_id: int,
    episode: Episode,
    position_s: float,
    duration_s: float,
    now: datetime | None = None,
    force_complete: bool = False,
) -> ProgressOutcome:
    """Upsert one ``watch_progress`` row and, if it just finished, the list.

    ``force_complete`` is FR-W3's manual mark: "I watched this elsewhere" is
    the same event as reaching 90 %, and treating it as one is the requirement,
    so it takes the identical path down to the list advance and the wants
    recompute rather than a parallel one that could drift.

    Flushed, not committed. The caller's transaction is what makes the
    progress, the list advance and the queued reconciliation one thing.
    """
    at = now or datetime.now(UTC)
    completing = force_complete or is_completed(position_s, duration_s)

    statement = pg_insert(WatchProgress).values(
        user_id=user_id,
        episode_id=episode.id,
        position_s=position_s,
        duration_s=duration_s,
        completed=completing,
        completed_at=at if completing else None,
        updated_at=at,
    )
    upsert = statement.on_conflict_do_update(
        index_elements=[WatchProgress.user_id, WatchProgress.episode_id],
        set_={
            "position_s": statement.excluded.position_s,
            "duration_s": statement.excluded.duration_s,
            # Sticky: a later report at 3 % never un-watches an episode.
            "completed": or_(WatchProgress.completed, statement.excluded.completed),
            # Written once and never moved: retention measures its grace window
            # from this, and a rewatch must not push a deletion date away.
            "completed_at": func.coalesce(
                WatchProgress.completed_at, statement.excluded.completed_at
            ),
            # ``onupdate`` does not fire for the DO UPDATE half of an upsert
            # (see :func:`arc.models._columns.updated_at`), so it is set here.
            "updated_at": statement.excluded.updated_at,
        },
    )

    completed, was_completed = (
        await session.execute(
            upsert.returning(
                WatchProgress.completed,
                # ``old`` is PostgreSQL 18's name for the row as it was
                # immediately before this statement changed it — and on the
                # insert half of the upsert there was no such row, so it is
                # NULL and coalesces to false. A literal because SQLAlchemy
                # has no construct for the alias yet; the column is spelled by
                # hand and nothing user-supplied goes near it.
                func.coalesce(literal_column("old.completed"), false()).label("was_completed"),
            )
        )
    ).one()

    newly_completed = bool(completed) and not bool(was_completed)
    if not newly_completed:
        return ProgressOutcome(completed=bool(completed), newly_completed=False)

    progress = await _advance_list(session, user_id=user_id, episode=episode, now=at)
    # The acquisition window is a function of the list entry (FR-A1, FR-W4), so
    # finishing an episode is one of the events that can change it — the next
    # one in the look-ahead is now wanted. Deduplicated on the job type, so a
    # binge costs one reconciliation rather than one per episode.
    await enqueue_compute_wants(session)
    return ProgressOutcome(completed=True, newly_completed=True, list_progress=progress)


async def _advance_list(
    session: AsyncSession, *, user_id: int, episode: Episode, now: datetime
) -> int:
    """Move ``list_entries.progress`` to this episode's number if it is higher.

    A user with no row for the show gets one, as ``watching``. FR-W3 allows
    marking an episode watched without ever having pressed "add to list", and
    the spirit of it — and of FR-S4's "advances ListEntry.progress" — is that
    watching a show *is* the statement that you are watching it. ``watching``
    rather than anything else because it is the only status under which the
    show keeps acquiring (FR-A1), which is what somebody who just finished an
    episode wants next.

    Never downwards (FR-M4): a rewatch of episode 3 on a list that says 11
    leaves the 11 alone, and leaves ``mal_dirty`` alone with it, so nothing is
    pushed to MAL for a change that did not happen.
    """
    entry = await session.get(ListEntry, (user_id, episode.anime_id))
    if entry is None:
        entry = ListEntry(
            user_id=user_id,
            anime_id=episode.anime_id,
            status=ListStatus.WATCHING,
            progress=0,
        )
        session.add(entry)

    was = entry.progress
    advanced = episode.number > entry.progress
    if advanced:
        entry.progress = episode.number
        entry.updated_by = UpdatedBy.ARC
        entry.mal_dirty = True
        # Set explicitly for the same reason :mod:`arc.services.catalog.lists`
        # does: §5.5 step 4 resolves a MAL conflict by comparing this against
        # MAL's own timestamp, so it has to move whenever Arc changed anything.
        entry.updated_at = now
    # Status is deliberately untouched, even when progress reaches the episode
    # count: FR-W2 makes "completed" the user's word, and a show that marked
    # itself completed would be Arc writing a status change to MAL that nobody
    # asked for (FR-M7).

    await session.flush()
    if advanced and await is_linked(session, user_id):
        # The one *automatic* event allowed to write to MyAnimeList (FR-M4,
        # FR-M7), and it is recorded only when the number actually moved — a
        # rewatch changes nothing and must therefore send nothing.
        #
        # **Progress and nothing else**, with cause ``watch`` on the row
        # rather than on the job. That cause is what stops the push lowering
        # MyAnimeList's progress (:func:`arc.services.mal.sync.decide_push`),
        # and it has to be per field: the queued job is shared with whatever
        # else this show owes — a score the user typed a second ago — and
        # those fields are the user's word, not an automatic event's.
        await record_pending(
            session,
            user_id=user_id,
            anime_id=episode.anime_id,
            field=FIELD_PROGRESS,
            old_value=was,
            new_value=entry.progress,
            cause=MalWriteCause.WATCH,
        )
        await enqueue_mal_push(session, user_id=user_id, anime_id=episode.anime_id)
    return entry.progress


async def unmark_watched(
    session: AsyncSession, *, user_id: int, episode_id: int, now: datetime | None = None
) -> bool:
    """Clear ``completed`` for one (user, episode); ``False`` if there was no row.

    The position is kept — un-marking is "I had not finished this after all",
    not "I never opened it", and the episode should reappear in continue
    watching where the user left it. ``completed_at`` is cleared with the flag
    so retention's grace window (FR-T1) is not still counting down on an
    episode nobody has finished.

    **The list entry and MAL are not rolled back.** Arc has, by this point,
    possibly told MyAnimeList that episode 11 is watched; silently lowering
    progress here would be an automatic write that lowers progress, which
    FR-M4 forbids outright. A user who wants the number back sets it from the
    show page, which is a change they made and can therefore be pushed.
    """
    at = now or datetime.now(UTC)
    # ``RETURNING`` rather than ``rowcount``: it is the same round trip, and it
    # is a typed answer to "was there a row?" rather than a driver attribute.
    touched = await session.scalars(
        update(WatchProgress)
        .where(
            WatchProgress.user_id == user_id,
            WatchProgress.episode_id == episode_id,
        )
        .values(completed=False, completed_at=None, updated_at=at)
        .returning(WatchProgress.episode_id)
    )
    return touched.first() is not None


async def completed_episode_ids(
    session: AsyncSession, *, user_id: int, episode_ids: Sequence[int]
) -> frozenset[int]:
    """Which of ``episode_ids`` this user has finished (FR-S4).

    One query for a whole page — a show page asks it of twelve episodes and the
    home page of fifty — which is why it takes a sequence rather than an id.
    """
    if not episode_ids:
        return frozenset()
    rows = await session.scalars(
        select(WatchProgress.episode_id).where(
            WatchProgress.user_id == user_id,
            WatchProgress.episode_id.in_(set(episode_ids)),
            WatchProgress.completed.is_(True),
        )
    )
    return frozenset(rows.all())


@dataclass(frozen=True, slots=True)
class ContinueRow:
    """One episode started and not finished, with where the player got to."""

    anime: Anime
    episode: Episode
    position_s: float
    duration_s: float | None


async def continue_watching(
    session: AsyncSession, *, user_id: int, limit: int = CONTINUE_LIMIT
) -> list[ContinueRow]:
    """Episodes this user is part-way through, most recent first (FR-W1).

    Three conditions, and each excludes a different kind of noise: not
    completed (it is *continue* watching), past
    :data:`CONTINUE_MIN_POSITION_S` (a player that was open for four seconds
    started nothing), and the episode still ``ready`` (retention deletes
    renditions, and a row offering to resume a file that is gone is worse than
    no row).
    """
    rows = await session.execute(
        select(Anime, Episode, WatchProgress)
        .join(Episode, Episode.id == WatchProgress.episode_id)
        .join(Anime, Anime.id == Episode.anime_id)
        .where(
            WatchProgress.user_id == user_id,
            WatchProgress.completed.is_(False),
            WatchProgress.position_s > CONTINUE_MIN_POSITION_S,
            Episode.state == EpisodeState.READY,
        )
        # The episode id breaks ties: two rows written in the same transaction
        # share a timestamp, and an order that is not total is an order that
        # changes between two identical requests.
        .order_by(WatchProgress.updated_at.desc(), WatchProgress.episode_id.desc())
        .limit(limit)
    )
    return [
        ContinueRow(
            anime=anime,
            episode=episode,
            position_s=progress.position_s,
            duration_s=progress.duration_s,
        )
        for anime, episode, progress in rows.all()
    ]


__all__ = [
    "COMPLETION_FRACTION",
    "CONTINUE_LIMIT",
    "CONTINUE_MIN_POSITION_S",
    "RESUME_MAX_FRACTION",
    "RESUME_MIN_S",
    "ContinueRow",
    "ProgressOutcome",
    "completed_episode_ids",
    "continue_watching",
    "is_completed",
    "record_progress",
    "resume_position",
    "unmark_watched",
]
