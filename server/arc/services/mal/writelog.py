"""``mal_write_log``: what Arc told MyAnimeList, and what it replaced.

This table is the evidence behind the spec's top-priority requirement — "never
write a change I did not make" (FR-M5, FR-M7, spec §7) — so the rules for
writing and reading it live in one module rather than being spread across the
job that pushes and the endpoint that lists.

**One row per field.** A single list edit that changes status *and* score is
two rows. That is what makes a revert meaningful: reverting is "put this one
field back", and a row that bundled three values could only be reverted as a
bundle, undoing changes the user never asked to undo.

**The pending rows are the queue.** :func:`record_pending` is called by the
*user event* — in the same transaction as the local change — and not by the
job that sends it. The row therefore carries the cause of the event that
produced it, which is what makes the FR-M4 guards decidable per field rather
than per job: a queued ``mal_push`` can be shared by a watch completion and a
manual edit (the queue deduplicates on the pair), and only the rows can say
which of the two asked for which field. The job loads them, coalesces them,
and closes them; :attr:`~arc.models.ListEntry.mal_dirty` is left as the cheap
flag it always was.

A row is therefore also written *before* the request, which is what it was
before: a worker killed mid-write leaves a ``pending`` row — evidence that
something *may* have reached MyAnimeList — rather than nothing at all.

**Revertibility.** A row can be reverted when both of these hold:

1. it succeeded (``status = ok``) — a write that never landed changed nothing
   on MyAnimeList, so there is nothing to put back; and
2. it is the **newest successful row for its (user, anime, field)** — nothing
   has overwritten that field since.

The second clause is doing all the work and it subsumes the cases one would
otherwise enumerate. A row that has already been reverted is not the newest
any more (the revert itself is), so it cannot be reverted twice. A row from
before a later edit of the same field is not the newest either, so "undo" can
never resurrect a value the user has since deliberately moved past. And the
revert row *is* revertible, which is the redo everybody expects: revert twice
and you are back where you started, with all three writes in the log.

A third clause is enforced by the caller rather than by the rule above: a
field with a ``pending`` row is **not** revertible, because the newest write
to it has not happened yet. The newest *successful* row is not the newest
intention, and reverting it would silently supersede a change the user made a
moment ago and is still waiting for (:mod:`arc.api.mal`).

**A pending row can carry an error.** ``status`` says where the row is in its
life — queued, sent, refused, abandoned — and ``error`` says what happened
last. A retryable failure (MyAnimeList 5xx, a rate limit, a dropped
connection) leaves the row ``pending`` and writes the message into ``error``
as a "last attempt" note, because the write is still owed and the job is still
coming back for it (FR-M6). Only when the job's attempts run out does the row
close ``failed``. That is the difference between "this has not landed yet" and
"this did not land", and it is why :func:`note_error` exists next to
:func:`finish`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import MalWriteCause, MalWriteLog, MalWriteStatus

#: The three fields Arc ever writes (FR-M4). Spelled as constants because the
#: column is a string and a typo would be a row nobody could revert.
FIELD_STATUS = "status"
FIELD_SCORE = "score"
FIELD_PROGRESS = "progress"

#: In the order a reader of the log expects them.
FIELDS = (FIELD_STATUS, FIELD_SCORE, FIELD_PROGRESS)

#: How much of an error message is kept. The column is unbounded ``TEXT``, but
#: the log is a UI surface: a stack-trace-shaped string in a table cell helps
#: nobody, and the full one is in the worker's log with the job id.
ERROR_LIMIT = 500

#: Sync states reported per show (``AnimeDetail.list_entry.mal_sync``) and used
#: for the failure badge of FR-M6.
SYNC_SYNCED = "synced"
SYNC_PENDING = "pending"
SYNC_FAILED = "failed"
SYNC_UNLINKED = "unlinked"

#: Statuses that describe a *write attempt*. ``skipped`` is excluded: those
#: rows record something Arc decided not to send, so letting one count as the
#: latest state of a field would badge a healthy entry as out of date.
ATTEMPTED = (MalWriteStatus.PENDING, MalWriteStatus.OK, MalWriteStatus.FAILED)


@dataclass(frozen=True, slots=True)
class SyncState:
    """How one (user, anime) pair stands with MyAnimeList."""

    state: str
    error: str | None = None
    last_write_at: datetime | None = None


def assert_write_cause(cause: MalWriteCause) -> MalWriteCause:
    """Refuse to attribute an actual write to anything but a user event.

    FR-M7 in one function. Every path that sends bytes to MyAnimeList passes
    its cause through here, so "a write is always a watch, a manual edit or a
    revert" is enforced at the point of writing rather than asserted in a
    comment. :attr:`~arc.models.enums.MalWriteCause.CONFLICT` is the one value
    that never accompanies a write — see the enum's docstring.
    """
    if cause is MalWriteCause.CONFLICT:
        raise ValueError("conflict rows record a discarded change, never a write")
    return cause


async def _insert(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int,
    field: str,
    old_value: Any,
    new_value: Any,
    cause: MalWriteCause,
    status: MalWriteStatus,
    error: str | None = None,
) -> MalWriteLog:
    """Append one row and return it, flushed so it has an id."""
    row = MalWriteLog(
        user_id=user_id,
        anime_id=anime_id,
        field=field,
        old_value=old_value,
        new_value=new_value,
        cause=cause,
        status=status,
        error=error,
    )
    session.add(row)
    await session.flush()
    return row


async def record_pending(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int,
    field: str,
    old_value: Any,
    new_value: Any,
    cause: MalWriteCause,
) -> MalWriteLog:
    """Queue one field of one user event for MyAnimeList (FR-M5, FR-M7).

    The single way a write is ever queued, and therefore the second half of
    FR-M7's enforcement: ``tests/test_mal_guard.py`` walks the source tree and
    fails if anything but the three user-originated events (and the removal
    primitive next to them) calls this. ``cause`` is checked here rather than
    when the row is sent, so a write nobody made cannot even be queued.

    ``old_value`` is the value the field held **in Arc** at the moment of the
    event. The push refreshes it to MyAnimeList's own value for the row it
    actually sends, because that is the value the write replaced (FR-M5) and
    the value a revert has to put back.
    """
    assert_write_cause(cause)
    return await _insert(
        session,
        user_id=user_id,
        anime_id=anime_id,
        field=field,
        old_value=old_value,
        new_value=new_value,
        cause=cause,
        status=MalWriteStatus.PENDING,
    )


async def record_closed(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int,
    field: str,
    old_value: Any,
    new_value: Any,
    cause: MalWriteCause,
    status: MalWriteStatus,
    error: str | None = None,
) -> MalWriteLog:
    """Append a row that was never a queued write: a conflict, or a refusal.

    Separate from :func:`record_pending` so that "this row is queued work" and
    "this row is a note about something Arc did not send" cannot be confused,
    and so that the FR-M7 guard has one narrow function to police.
    """
    if status is MalWriteStatus.PENDING:  # pragma: no cover - a programming error
        raise ValueError("a pending row is queued work; use record_pending")
    return await _insert(
        session,
        user_id=user_id,
        anime_id=anime_id,
        field=field,
        old_value=old_value,
        new_value=new_value,
        cause=cause,
        status=status,
        error=error,
    )


async def pending_for(session: AsyncSession, *, user_id: int, anime_id: int) -> list[MalWriteLog]:
    """Every queued row for one (user, anime), oldest first.

    Oldest first because the order is the queue: the last row for a field is
    the value the user last asked for, and the ones before it are what that
    value superseded.
    """
    rows = await session.scalars(
        select(MalWriteLog)
        .where(
            MalWriteLog.user_id == user_id,
            MalWriteLog.anime_id == anime_id,
            MalWriteLog.status == MalWriteStatus.PENDING,
        )
        .order_by(MalWriteLog.id)
    )
    return list(rows.all())


async def pending_anime_ids(
    session: AsyncSession, *, user_id: int, include_failed: bool = False
) -> list[int]:
    """The shows this user has queued rows for — what a full push works through.

    ``include_failed`` widens that to the shows whose *last attempt* at some
    field failed. Those rows are not queued any more — the retries ran out —
    but they are still changes Arc owes MyAnimeList, and "push everything I
    owe" is exactly the button that must find them again (FR-M6). It is off by
    default so that the callers who mean "what is queued" keep meaning it.
    """
    rows = await session.scalars(
        select(MalWriteLog.anime_id)
        .where(MalWriteLog.user_id == user_id, MalWriteLog.status == MalWriteStatus.PENDING)
        .group_by(MalWriteLog.anime_id)
        .order_by(MalWriteLog.anime_id)
    )
    ids = set(rows.all())
    if include_failed:
        ids |= {row.anime_id for row in await failed_attempts(session, user_id=user_id)}
    return sorted(ids)


async def pending_fields(
    session: AsyncSession, *, user_id: int, anime_ids: Sequence[int] | None = None
) -> set[tuple[int, str]]:
    """``{(anime_id, field)}`` this user has a queued row for.

    One query for a whole page of the log: a row whose field is queued cannot
    be reverted, and asking that per row would be fifty existence checks.
    """
    statement = (
        select(MalWriteLog.anime_id, MalWriteLog.field)
        .where(MalWriteLog.user_id == user_id, MalWriteLog.status == MalWriteStatus.PENDING)
        .group_by(MalWriteLog.anime_id, MalWriteLog.field)
    )
    if anime_ids is not None:
        statement = statement.where(MalWriteLog.anime_id.in_(list(anime_ids)))
    return {(anime_id, field) for anime_id, field in (await session.execute(statement)).all()}


async def has_pending_field(
    session: AsyncSession, *, user_id: int, anime_id: int, field: str
) -> bool:
    """Whether one field of one show already has a change waiting to be sent."""
    queued = await session.scalar(
        select(MalWriteLog.id)
        .where(
            MalWriteLog.user_id == user_id,
            MalWriteLog.anime_id == anime_id,
            MalWriteLog.field == field,
            MalWriteLog.status == MalWriteStatus.PENDING,
        )
        .limit(1)
    )
    return queued is not None


def finish(row: MalWriteLog, *, status: MalWriteStatus, error: str | None = None) -> MalWriteLog:
    """Close a pending row with its outcome. Errors are trimmed for the UI."""
    row.status = status
    row.error = error[:ERROR_LIMIT] if error else None
    return row


def note_error(row: MalWriteLog, error: str) -> MalWriteLog:
    """Record what the last attempt said **without** closing the row (FR-M6).

    The write is still owed and the job is still coming back for it, so the
    row stays ``pending``; the message is the "last attempt" note the user's
    log and the worker's next run both read. Closing it here is the bug this
    function exists to prevent: a closed row is not found by the retry, and a
    retry that finds nothing queued is a change that is never sent at all.
    """
    row.error = error[:ERROR_LIMIT] if error else None
    return row


def reopen(row: MalWriteLog) -> MalWriteLog:
    """Put a ``failed`` row back on the queue, keeping why it failed.

    "Push everything I owe" after the backoff ran out (FR-M6). The error is
    kept rather than cleared: until the next attempt answers, the last thing
    that happened to this row is still the last thing that happened to it.
    """
    row.status = MalWriteStatus.PENDING
    return row


async def latest_ok_ids(
    session: AsyncSession, *, user_id: int, anime_ids: Sequence[int] | None = None
) -> dict[tuple[int, str], int]:
    """``(anime_id, field) → id`` of the newest successful write for each.

    One query for a whole page of the log, so rendering fifty rows with their
    "revert" buttons does not cost fifty existence checks.
    """
    statement = (
        select(
            MalWriteLog.anime_id,
            MalWriteLog.field,
            func.max(MalWriteLog.id),
        )
        .where(MalWriteLog.user_id == user_id, MalWriteLog.status == MalWriteStatus.OK)
        .group_by(MalWriteLog.anime_id, MalWriteLog.field)
    )
    if anime_ids is not None:
        statement = statement.where(MalWriteLog.anime_id.in_(list(anime_ids)))
    rows = await session.execute(statement)
    return {(anime_id, field): row_id for anime_id, field, row_id in rows.all()}


def _latest_attempt_statement(user_id: int) -> Select[tuple[MalWriteLog]]:
    """The newest *attempted* row per (anime, field) for one user.

    ``DISTINCT ON`` rather than a window function or a correlated subquery:
    Postgres answers it from one index scan, and it reads as what it is — the
    first row of each group, in the order the ``ORDER BY`` spells out. The id
    tiebreak matters because two rows of one push share a ``created_at`` to
    the microsecond often enough to be a real source of flapping.
    """
    return (
        select(MalWriteLog)
        .distinct(MalWriteLog.anime_id, MalWriteLog.field)
        .where(MalWriteLog.user_id == user_id, MalWriteLog.status.in_(ATTEMPTED))
        .order_by(
            MalWriteLog.anime_id,
            MalWriteLog.field,
            MalWriteLog.created_at.desc(),
            MalWriteLog.id.desc(),
        )
    )


async def latest_attempts(
    session: AsyncSession, *, user_id: int, anime_ids: Sequence[int] | None = None
) -> dict[tuple[int, str], MalWriteLog]:
    """``(anime_id, field) → the newest attempted row``."""
    statement = _latest_attempt_statement(user_id)
    if anime_ids is not None:
        statement = statement.where(MalWriteLog.anime_id.in_(list(anime_ids)))
    rows = (await session.scalars(statement)).all()
    return {(row.anime_id, row.field): row for row in rows}


async def failed_attempts(
    session: AsyncSession, *, user_id: int, anime_id: int | None = None
) -> list[MalWriteLog]:
    """Rows whose field's **most recent** attempt failed (FR-M6).

    Per (anime, field) rather than per row, which is what makes "put the
    failures back on the queue" safe: a score that failed at 5 and was later
    written successfully at 7 has no failed *attempt* left, and re-sending the
    old row would push the user back to a value they have moved past.
    """
    statement = _latest_attempt_statement(user_id)
    if anime_id is not None:
        statement = statement.where(MalWriteLog.anime_id == anime_id)
    rows = (await session.scalars(statement)).all()
    return [row for row in rows if row.status is MalWriteStatus.FAILED]


async def is_settled(session: AsyncSession, *, user_id: int, anime_id: int) -> bool:
    """Whether this pair owes MyAnimeList nothing at all.

    The question :attr:`~arc.models.ListEntry.mal_dirty` is a cache of, and the
    reason it is recomputed rather than assumed. A pair is settled when nothing
    is queued *and* no field's last attempt failed — a failed write is a change
    Arc still holds and MyAnimeList has not been told about, which is precisely
    what the flag means and what stops the next import overwriting it (FR-M3).
    """
    if await pending_for(session, user_id=user_id, anime_id=anime_id):
        return False
    return not await failed_attempts(session, user_id=user_id, anime_id=anime_id)


async def is_revertible(session: AsyncSession, row: MalWriteLog) -> bool:
    """The rule from the module docstring, for one row."""
    if row.status is not MalWriteStatus.OK:
        return False
    newer = await session.scalar(
        select(MalWriteLog.id)
        .where(
            MalWriteLog.user_id == row.user_id,
            MalWriteLog.anime_id == row.anime_id,
            MalWriteLog.field == row.field,
            MalWriteLog.status == MalWriteStatus.OK,
            MalWriteLog.id > row.id,
        )
        .limit(1)
    )
    return newer is None


async def recent(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = 50,
    status: MalWriteStatus | None = None,
) -> list[MalWriteLog]:
    """One user's log, newest first (FR-M5)."""
    statement = (
        select(MalWriteLog)
        .where(MalWriteLog.user_id == user_id)
        .order_by(MalWriteLog.created_at.desc(), MalWriteLog.id.desc())
        .limit(limit)
    )
    if status is not None:
        statement = statement.where(MalWriteLog.status == status)
    return list((await session.scalars(statement)).all())


async def sync_state(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int,
    linked: bool,
) -> SyncState:
    """What the show page says about this entry's MyAnimeList state (FR-M6).

    Read off the log rather than off ``mal_dirty``, because the rows are the
    queue: "pending" is "there is a queued row for this show", which is true
    exactly while a change is owed. ``failed`` outranks it — both can be true
    at once, and "it failed, here is why" is the more useful half — and
    ``unlinked`` outranks everything: there is nothing to be out of step with.
    """
    latest = await latest_attempts(session, user_id=user_id, anime_ids=[anime_id])
    rows = list(latest.values())
    last_ok: datetime | None = await session.scalar(
        select(func.max(MalWriteLog.created_at)).where(
            MalWriteLog.user_id == user_id,
            MalWriteLog.anime_id == anime_id,
            MalWriteLog.status == MalWriteStatus.OK,
        )
    )
    if not linked:
        return SyncState(SYNC_UNLINKED, None, last_ok)
    failed = [row for row in rows if row.status is MalWriteStatus.FAILED]
    if failed:
        worst = max(failed, key=lambda row: (row.created_at, row.id))
        return SyncState(SYNC_FAILED, worst.error, last_ok)
    if any(row.status is MalWriteStatus.PENDING for row in rows):
        return SyncState(SYNC_PENDING, None, last_ok)
    return SyncState(SYNC_SYNCED, None, last_ok)


async def pending_count(session: AsyncSession, *, user_id: int) -> int:
    """Fields Arc still owes MyAnimeList: one per queued row.

    The rows are the queue, so this is a count of them and nothing else. It
    counts fields rather than shows — "3 pending writes" for one show whose
    status, score and progress all moved is the literal truth, and it is the
    same number the log page shows when filtered to ``pending``.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(MalWriteLog)
        .where(MalWriteLog.user_id == user_id, MalWriteLog.status == MalWriteStatus.PENDING)
    )
    return int(count or 0)


async def failed_count(session: AsyncSession, *, user_id: int) -> int:
    """Fields whose *most recent* attempt failed (FR-M6).

    Counted per (anime, field) rather than per row so that a write that failed
    four times and then succeeded contributes nothing: the badge is about the
    current state, not about the history, and the history is what the log page
    is for. A write still being retried is ``pending``, not failed, and so is
    not counted here either — it has not failed until the attempts run out.
    """
    return len(await failed_attempts(session, user_id=user_id))


__all__ = [
    "ATTEMPTED",
    "FIELDS",
    "FIELD_PROGRESS",
    "FIELD_SCORE",
    "FIELD_STATUS",
    "SYNC_FAILED",
    "SYNC_PENDING",
    "SYNC_SYNCED",
    "SYNC_UNLINKED",
    "SyncState",
    "assert_write_cause",
    "failed_attempts",
    "failed_count",
    "finish",
    "has_pending_field",
    "is_revertible",
    "is_settled",
    "latest_attempts",
    "latest_ok_ids",
    "note_error",
    "pending_anime_ids",
    "pending_count",
    "pending_fields",
    "pending_for",
    "recent",
    "record_closed",
    "record_pending",
    "reopen",
    "sync_state",
]
