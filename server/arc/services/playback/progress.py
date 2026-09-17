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
takes it back — and that one, since the owner's revision of 2026-09-13, also
lowers the list by one episode, which makes it the single path in Arc that
lowers MyAnimeList's progress. It is allowed to because a person pressed it;
:func:`_retreat_list` has the argument.

**Completion is also the only automatic thing that moves a list entry.** The
first time it flips true, ``list_entries.progress`` advances to this episode's
number if the number is higher, ``updated_by`` becomes ``arc`` and
``mal_dirty`` becomes true — which is M9's cue and the *only* automatic one
(architecture.md §5.5 step 2). Never downwards: FR-M4 says progress written
from an automatic event never decreases, and the ``>`` below is where that is
true.

**And the advance is what "watched" means from here on** (FR-W5, owner
2026-09-13). An episode counts as watched for a user when its number is at or
below their ``list_entries.progress`` *or* they have a completion row for it,
so a list imported from MyAnimeList at episode 9 marks nine episodes watched
without Arc having nine rows to show for it. :func:`~arc.services.playback.watched.watched_source`
is that rule and the only definition of it; nothing here writes synthetic completion
rows for 1…N-1, because progress already says it and eight fabricated rows
would be eight lies about when somebody watched something.

**Progress reaching the end of a finished show completes it** (FR-W5). That
used to be deliberately untrue — FR-W2 gives "completed" to the user, and a
show marked completed by a rewatch of episode 12 would be Arc making a claim
nobody made. The owner's answer on 2026-09-13 narrows it to the case where the
claim is not a guess: the count is known, the source says ``FINISHED``, and
the number just *moved* to the end. An airing show is never completed (episode
13 may exist next week), an unknown count has no end to reach, and a rewatch
does not advance anything so it triggers nothing. :func:`_auto_completes` is
that condition, and the status change is logged like any other so the push
sends status and progress together.

**Every progress report is a touch, though** (FR-A9). ``activated_at`` on an
existing list entry is stamped on the *first* report of an episode, not on the
completion: pressing play on a show a MyAnimeList import brought in is the user
asking Arc for it, and waiting for 90 % would mean the next episode only
started downloading after they had finished this one. It never *creates* an
entry — "pressed play" is not "is watching this show", and FR-S4 draws that
line at the completion — so the one row this can stamp is one that already
exists (:func:`_activate_entry`).

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

from sqlalchemy import and_, false, func, literal_column, or_, select, update
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
from arc.services.acquisition.dormancy import activate
from arc.services.acquisition.names import enqueue_compute_wants
from arc.services.catalog.airing import FINISHED
from arc.services.mal.names import enqueue_mal_push, is_linked
from arc.services.mal.writelog import FIELD_PROGRESS, FIELD_STATUS, record_pending

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

#: And how far in an episode has to be to count as *started*. Stricter than
#: :data:`RESUME_MIN_S` on purpose: ten seconds is enough to be worth resuming
#: once a user has chosen the episode, but the home shelf is a list Arc offers
#: unprompted, and half a minute is the point at which somebody was watching
#: rather than sampling.
CONTINUE_MIN_POSITION_S = 30.0

#: The shelf's other end, and it is **tighter than the resume ceiling on
#: purpose** (owner, 2026-09-17). Resuming into the last three minutes of an
#: episode is a thing a viewer may ask for — they chose that episode — but
#: *offering* it unprompted is Arc putting a credits roll on the home page and
#: calling it something left to watch. Three minutes is about an outro and a
#: next-episode preview, which is the part nobody comes back for.
#:
#: A fixed tail rather than a fraction, so "there is something left to watch"
#: means the same thing on a forty-minute episode and a five-minute short. The
#: consequence at the short end is deliberate and worth knowing: a file under
#: three minutes long never reaches this shelf at all, because it is never more
#: than three minutes from its own end.
CONTINUE_TAIL_S = 180.0


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

    **``completed`` is not consulted.** A rewatch stopped at the midpoint is a
    saved position like any other, and the flag says the user finished this
    episode once, not that they are not part-way through it now — dropping them
    back at zero loses the only record of where they were. The two bounds do
    the work instead: a row at the very end resumes nowhere, whatever the flag
    says, and a mark written by hand (``0``/``0``, FR-W3) is below the floor.
    Nothing here reads or writes the flag, so completion itself — the MAL push,
    the once-only advance, the watched mark — is untouched.
    """
    if row is None:
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

    # Play is one of FR-A9's touches, and it is a touch from the *first*
    # report rather than from the one that crosses 90 %: somebody watching
    # thirty seconds of an imported show has asked Arc for it, and waiting for
    # the completion would mean the next episode only started arriving after
    # they had finished this one. An entry that does not exist yet is left to
    # the completion path below (FR-S4 creates it, activated); this only ever
    # stamps a row that is already there.
    await _activate_entry(session, user_id=user_id, anime_id=episode.anime_id, now=at)

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


async def _activate_entry(
    session: AsyncSession, *, user_id: int, anime_id: int, now: datetime
) -> None:
    """Stamp this show's list entry as touched in Arc, if there is one (FR-A9).

    A ``SELECT`` on the primary key per progress report, which at one report
    per ten seconds per player is nothing, and it is the write-once
    :func:`~arc.services.acquisition.dormancy.activate` behind it — so the
    second report of an episode reads the row, finds a stamp and changes
    nothing.

    Deliberately does **not** create an entry. "The user pressed play" is not
    "the user is watching this show"; FR-S4 draws that line at the completion,
    and drawing it here would put a show on somebody's list — and into their
    MyAnimeList push queue — because they opened an episode and changed their
    mind.
    """
    entry = await session.get(ListEntry, (user_id, anime_id))
    if entry is not None and activate(entry, now=now):
        await session.flush()


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

    And when the advance lands on the last episode of a show that has finished
    airing, the entry becomes ``completed`` in this same transaction (FR-W5,
    :func:`_auto_completes`), with its own logged write so the push sends the
    status and the progress together rather than in two rounds.
    """
    entry = await session.get(ListEntry, (user_id, episode.anime_id))
    # ``None`` rather than ``watching`` for a row this function is about to
    # create: the write log's ``old_value`` is what the user had before, and
    # for a create that is nothing at all — the same convention
    # :mod:`arc.services.catalog.lists` uses for its own snapshot.
    status_was = entry.status.value if entry is not None else None
    if entry is None:
        entry = ListEntry(
            user_id=user_id,
            anime_id=episode.anime_id,
            status=ListStatus.WATCHING,
            progress=0,
        )
        session.add(entry)

    # FR-A9, for the entry this function has just *created*: an existing one
    # was stamped by :func:`_activate_entry` on the first progress report of
    # the episode, long before this. Kept here rather than left to that, and
    # unconditional rather than inside the ``advanced`` branch, because a row
    # created by FR-S4 is the user's own choice to watch the show and must not
    # spend its first fifteen minutes dormant. Write-once, so the two writers
    # cannot disagree.
    activate(entry, now=now)

    was = entry.progress
    advanced = episode.number > entry.progress
    finished = False
    if advanced:
        entry.progress = episode.number
        entry.updated_by = UpdatedBy.ARC
        entry.mal_dirty = True
        # Set explicitly for the same reason :mod:`arc.services.catalog.lists`
        # does: §5.5 step 4 resolves a MAL conflict by comparing this against
        # MAL's own timestamp, so it has to move whenever Arc changed anything.
        entry.updated_at = now
        # FR-W5's auto-complete, in the same transaction as the advance that
        # earned it. Only ever on an advance: a rewatch of the last episode of
        # a show already at 12/12 moves nothing and must therefore claim
        # nothing, which is also what keeps an already-completed entry as it
        # is.
        anime = await session.get(Anime, episode.anime_id)
        finished = anime is not None and _auto_completes(entry, anime)
        if finished:
            entry.status = ListStatus.COMPLETED

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
        if finished:
            # FR-W5's status write. Cause ``watch`` because that is the event
            # that asked for it — finishing the last episode of a finished
            # show is as user-originated as an event gets (FR-M7) — and the
            # FR-M4 guards that cause carries are about progress and score,
            # neither of which this is. One row, so the push sends one PATCH
            # with both fields on it.
            await record_pending(
                session,
                user_id=user_id,
                anime_id=episode.anime_id,
                field=FIELD_STATUS,
                old_value=status_was,
                new_value=entry.status.value,
                cause=MalWriteCause.WATCH,
            )
        await enqueue_mal_push(session, user_id=user_id, anime_id=episode.anime_id)
    return entry.progress


def _auto_completes(entry: ListEntry, anime: Anime) -> bool:
    """Whether this advance finished the show (FR-W5, owner 2026-09-13).

    Three conditions, and each of them is one of the edges the owner decided:

    * the **count is known**. A show airing without one has no end to reach,
      and "12/None" is not 12/12.
    * the source says **FINISHED**. A ``RELEASING`` show's count is a
      projection — episode 13 of a "12-episode" season is a weekly
      occurrence — and completing a show that is still airing would be a
      status write Arc has to take back next Friday.
    * the entry is **not already completed**. A rewatch of a completed show
      stays completed and writes nothing; the caller's ``advanced`` guard
      covers most of that, and this covers the rest (progress edited below the
      count on a completed entry, then watched back up).

    **Every other status completes**, including ``on_hold`` and ``dropped``
    (owner, 2026-09-13). Watching the last episode of a show you had dropped is
    the clearest statement anybody makes about a list entry, and leaving it
    "dropped at 12/12" would be Arc preserving a state the viewer has plainly
    moved past. The status write is logged like any other, so the log says what
    it came from and the revert can put it back.

    ``>=`` rather than ``==`` because a stale episode count is the ordinary
    case: a user whose progress already passed a count the catalogue later
    revised downwards has finished the show by any reading of it.
    """
    if entry.status is ListStatus.COMPLETED:
        return False
    if anime.status != FINISHED:
        return False
    count = anime.episodes
    if not count:
        return False
    return entry.progress >= int(count)


@dataclass(frozen=True, slots=True)
class UnmarkOutcome:
    """What one un-mark changed.

    ``cleared`` is whether there was a completion row to take back at all;
    ``list_progress`` is the number the list now reads, and is ``None``
    whenever the un-mark did not move it — which is every un-mark except one of
    the viewer's latest watched episode.
    """

    cleared: bool
    list_progress: int | None = None


async def unmark_watched(
    session: AsyncSession, *, user_id: int, episode: Episode, now: datetime | None = None
) -> UnmarkOutcome:
    """Take back one watched mark, and the list progress if it was the latest.

    The position is kept — un-marking is "I had not finished this after all",
    not "I never opened it", and the episode should reappear in continue
    watching where the user left it. ``completed_at`` is cleared with the flag,
    which takes this user's completion out of retention's grace window (FR-T1).

    **The list entry is rolled back by exactly one episode** (FR-S4, revised by
    the owner on 2026-09-13; the 2026-09-07 clarification said it never was).
    Since FR-W5 derives the watched marks from ``list_entries.progress``, the
    old rule left the viewer no way to correct a mark at all: clearing the
    completion row of episode 9 on a list that says 9 changed nothing anybody
    could see. So an un-mark of the *latest* watched episode lowers progress to
    ``N-1``, with ``updated_by = arc``, ``mal_dirty`` and one logged progress
    write carrying the previous value.

    That write's cause is ``manual``, not ``watch``, and the distinction is the
    whole of FR-M4 here. **This is the one path on which Arc lowers
    MyAnimeList's progress**, and it may only because a person pressed the
    button: :func:`arc.services.mal.sync._guard` refuses a *lowering* progress
    write whose cause is ``watch`` — every automatic one — and lets an explicit
    edit through. A rewatch, a scrub back, a second beacon: none of them reach
    this function, and none of them could lower anything if they did.

    Lower, and nothing else. **The status is untouched**, even when the episode
    was the last one of a show FR-W5 auto-completed: "completed" is a word the
    viewer owns (FR-W2), and Arc taking it back off a show they still consider
    finished would be Arc making a claim nobody made — the mirror image of the
    argument that lets the auto-complete set it in the first place.

    Two un-marks that are no-ops for the list, deliberately: one of an episode
    *below* the progress (taking back episode 4 of a list that says 9 would
    have to say something about 5…9 that nobody said), and one of an episode
    *above* it (there is nothing to lower). Both still clear the row.
    """
    at = now or datetime.now(UTC)
    # ``RETURNING`` rather than ``rowcount``: it is the same round trip, and it
    # is a typed answer to "was there a row?" rather than a driver attribute.
    touched = await session.scalars(
        update(WatchProgress)
        .where(
            WatchProgress.user_id == user_id,
            WatchProgress.episode_id == episode.id,
        )
        .values(completed=False, completed_at=None, updated_at=at)
        .returning(WatchProgress.episode_id)
    )
    cleared = touched.first() is not None
    lowered = await _retreat_list(session, user_id=user_id, episode=episode, now=at)
    return UnmarkOutcome(cleared=cleared, list_progress=lowered)


async def _retreat_list(
    session: AsyncSession, *, user_id: int, episode: Episode, now: datetime
) -> int | None:
    """Lower ``list_entries.progress`` to ``N-1``, if ``N`` is where it stands.

    The mirror of :func:`_advance_list`, and narrower on purpose: it moves the
    number by one and only from the episode the viewer actually pressed, which
    is what makes it a correction rather than an edit. ``None`` when there is
    no entry, when the number is somewhere else, or when the list is already at
    zero — in each case nothing moved, so nothing is dirty and MyAnimeList is
    owed nothing.

    An un-mark is also one of FR-A9's touches: it is a progress change the user
    made in Arc. And it moves the acquisition window back onto the episode, so
    the reconciliation is queued — FR-T3's "if a user later rewinds … it is
    re-acquired" is precisely this.
    """
    entry = await session.get(ListEntry, (user_id, episode.anime_id))
    if entry is None or entry.progress <= 0 or entry.progress != episode.number:
        return None

    was = entry.progress
    entry.progress = was - 1
    entry.updated_by = UpdatedBy.ARC
    entry.mal_dirty = True
    # Set explicitly for the same reason the advance does: §5.5 step 4 resolves
    # a MAL conflict by comparing this against MAL's own timestamp.
    entry.updated_at = now
    activate(entry, now=now)
    await session.flush()

    await enqueue_compute_wants(session)
    if await is_linked(session, user_id):
        # Cause ``manual``: a person pressed this, which is what allows the one
        # progress write in Arc that goes *down* (FR-M4, and
        # :func:`arc.services.mal.sync._guard`).
        await record_pending(
            session,
            user_id=user_id,
            anime_id=episode.anime_id,
            field=FIELD_PROGRESS,
            old_value=was,
            new_value=entry.progress,
            cause=MalWriteCause.MANUAL,
        )
        await enqueue_mal_push(session, user_id=user_id, anime_id=episode.anime_id)
    return entry.progress


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
    """One episode with an unfinished position, and where the player got to.

    ``completed`` rides along because a rewatch belongs here (see
    :func:`continue_watching`) and the card still has to say, truthfully,
    whether the user has watched this episode before.
    """

    anime: Anime
    episode: Episode
    position_s: float
    duration_s: float | None
    completed: bool = False


async def continue_watching(
    session: AsyncSession, *, user_id: int, limit: int = CONTINUE_LIMIT
) -> list[ContinueRow]:
    """Episodes this user is part-way through, most recent first (FR-W1).

    The question is "is there something left to watch here?", and the answer
    is the saved position and nothing else. Three conditions, each excluding a
    different kind of noise: at least :data:`CONTINUE_MIN_POSITION_S` in (a
    player that was open for four seconds started nothing), short of the end
    (below), and the episode still ``ready`` (retention deletes renditions, and
    a row offering to resume a file that is gone is worse than no row).

    **Short of the end is two rules, and the first of them to bite wins**
    (FR-W1, owner 2026-09-17, from production). An episode leaves the shelf
    once the viewer is past the completion mark — :data:`COMPLETION_FRACTION`,
    the same 90 % FR-S4 calls watched, because a shelf that keeps offering an
    episode Arc has already written down as watched is arguing with itself —
    **or** once it has less than :data:`CONTINUE_TAIL_S` left, whichever comes
    first. The tail is what covers the long episode: 90 % of fifty minutes
    still leaves five, and 90 % of a short leaves seconds. Both bounds are
    tighter than :data:`RESUME_MAX_FRACTION`, so everything this shelf offers
    still resumes where it says it will; the reverse is not true, and
    deliberately so — an episode at 92 % resumes if the viewer opens it
    themselves and is simply not *offered*.

    What takes its place is the next episode, on Ready to watch
    (:func:`arc.services.catalog.progress.ready_to_watch`): past the completion
    mark the episode has a completion row, so that shelf excludes it too, and
    the list advance FR-S4 made puts its successor above
    ``list_entries.progress`` where that shelf looks. The one row the two
    shelves never both hold is still guaranteed by the floor —
    ``RESUME_MIN_S < CONTINUE_MIN_POSITION_S``.

    **``completed`` is not one of them.** It used to be, and the case that
    broke was the ordinary one: rewatch an episode, stop at the midpoint, and
    the home page had nothing to offer — the shelf was the only way back to
    that position and the flag hid it. A finished episode reopened and left
    half-way is exactly the thing "continue watching" names, so the position
    decides and the flag does not. Watched to the end, it falls off the shelf
    again by the ceiling rather than by the flag, which is the same answer for
    a first watch and a fifth.

    Nothing else changes with it: this function only reads, so completion still
    means what it meant to MyAnimeList, to the once-only advance, and to the
    show page's watched marks.
    """
    duration = WatchProgress.duration_s
    rows = await session.execute(
        select(Anime, Episode, WatchProgress)
        .join(Episode, Episode.id == WatchProgress.episode_id)
        .join(Anime, Anime.id == Episode.anime_id)
        .where(
            WatchProgress.user_id == user_id,
            WatchProgress.position_s >= CONTINUE_MIN_POSITION_S,
            or_(
                # No duration to measure against — a row from a player that
                # never reported one, or FR-W3's ``0``/``0`` hand-written mark,
                # which the floor above has already excluded anyway.
                duration.is_(None),
                duration <= 0,
                and_(
                    # Two bounds rather than one ``least``, because the two
                    # rules do not have the same edge: the completion mark is
                    # reached *at* 90 % (FR-S4 is ``>=``), while "less than
                    # three minutes left" still leaves an episode with exactly
                    # three minutes left on the shelf.
                    WatchProgress.position_s < duration * COMPLETION_FRACTION,
                    WatchProgress.position_s <= duration - CONTINUE_TAIL_S,
                ),
            ),
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
            completed=progress.completed,
        )
        for anime, episode, progress in rows.all()
    ]


__all__ = [
    "COMPLETION_FRACTION",
    "CONTINUE_TAIL_S",
    "CONTINUE_LIMIT",
    "CONTINUE_MIN_POSITION_S",
    "RESUME_MAX_FRACTION",
    "RESUME_MIN_S",
    "ContinueRow",
    "ProgressOutcome",
    "UnmarkOutcome",
    "completed_episode_ids",
    "continue_watching",
    "is_completed",
    "record_progress",
    "resume_position",
    "unmark_watched",
]
