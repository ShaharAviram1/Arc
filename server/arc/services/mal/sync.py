"""The sync rules: who wins, what is written, and what is never written.

This is the module spec §7 calls the top priority ("correctness of MAL writes")
and CLAUDE.md calls a non-negotiable, so it is written to be read: the
decisions are pure functions over small dataclasses at the top, and the
database and HTTP work is glue underneath them. Every rule below can be tested
without a socket or a table.

**Import (FR-M2, FR-M3), one entry at a time.**

* No local row → create one from MAL, ``updated_by = mal``, not dirty.
* A local row Arc has not changed (``mal_dirty = false``) → MyAnimeList is
  authoritative; overwrite it.
* A local row Arc *has* changed → the more recent change wins. If MAL's is
  newer, overwrite and log the loss as a ``conflict`` row (``skipped``: no
  write was made, a change was discarded). If Arc's is newer, leave it alone;
  its queued ``mal_push`` will carry it upstream.
* On Arc's list but not on MAL → **left alone, and nothing is logged.** Arc
  never deletes a user's data because a remote list does not mention it. A
  MAL-side deletion and a MAL-side hiccup look identical from here, and only
  one of them is worth losing a watch history over. The user removing the show
  in Arc is what removes it, and that pushes a ``DELETE`` (FR-M4).

**Push (FR-M4, FR-M7): the write log is the queue.** Each user event writes one
``pending`` ``mal_write_log`` row per field it changed, in the same transaction
as the local change, carrying that event's cause. The ``mal_push`` job reads
those rows; the queued job itself carries nothing but the pair.

That is not bookkeeping, it is the correctness of the guards below. A queued
push is deduplicated per (user, anime) and the queue returns the *existing* job
unchanged, so one job routinely has to carry two events — a manual score edit
and then a watch completion. A cause on the job would be whichever event queued
it first, and the guards would then be applied to the other event's fields:
either lowering MyAnimeList's progress for a watch folded into a ``manual``
job, or silently dropping a deliberate score clear folded into a ``watch`` one.
A cause per *row* cannot make that mistake, and it is why the rows are the
state and the job is only a nudge.

The push loads the pending rows, coalesces each field to its latest value
(older rows for that field close as ``skipped``: superseded), reads the current
MAL entry — so the diff is against what is actually there and the log records
the value really replaced — and applies the guards **per field, from that
field's own cause**:

* **Progress is never lowered by a watch event.** FR-M4 says so outright. A
  rewatch, a re-import, a second device — any of them can leave Arc's number
  below MAL's, and none of them is a statement that the user un-watched
  anything. An explicit list edit that lowers it *is* such a statement, and
  goes through. A refused row is closed ``skipped`` with the reason, not
  dropped: FR-M5 wants the decision on the record.
* **A missing score never clears one automatically.** Arc cannot distinguish
  "never scored" from "score cleared" once the column is null, so on a watch
  event the null is read as the former and MAL's score is left alone. An
  explicit edit or a revert is read as the latter and clears it (MAL spells
  that ``score=0``).

Status has no such guard because it cannot be produced by an automatic event:
:func:`arc.services.playback.progress._advance_list` moves progress and
nothing else, deliberately (FR-W2).

Everything sendable goes in **one** ``PATCH``.

**Failure (FR-M6): a retry has to find the rows it is retrying.** The job
runner rolls a raising handler's session back and re-runs it with backoff
(:mod:`arc.services.jobs.runner`), and the rows *are* the queue — so what a
failed attempt does to them decides whether the retry means anything at all.

* A **retryable** failure — a 5xx, a rate limit, a dropped connection, a
  timeout — leaves the rows ``pending`` and writes the message into their
  ``error`` column as a note about the last attempt. ``mal_dirty`` stays set.
  The handler commits that note and *then* raises, so the runner's rollback
  has nothing to undo and the next attempt loads the same rows and sends them
  again. Closing them ``failed`` here was the bug this paragraph exists to
  prevent: the retry would find nothing queued, take the "nothing queued"
  branch, clear ``mal_dirty`` — and the change would be lost twice over, once
  because it was never sent and once because the next import would overwrite
  it.
* Only when the job's attempts are **spent** (``last_attempt``) do the rows
  close ``failed`` with that error, ``mal_dirty`` still set. That is the state
  the show badge and the sync page report, and the state ``mal_push_all``
  reopens when the user presses "push pending" (FR-M6).
* A **non-retryable** failure — a 400 for a form MyAnimeList will refuse just
  as firmly in an hour, a 404 for an entry that is not there — closes the rows
  ``failed`` at once. Waiting five backoffs to say so helps nobody.

``mal_dirty`` is therefore cleared only when the pair has neither a queued row
nor a field whose last attempt failed — :func:`arc.services.mal.writelog.is_settled`
— which is what keeps the flag, the queue and the badge from disagreeing.

**One more event, one more job.** A user event that lands while the pair's push
is already ``running`` finds that job through the dedupe key and is handed it
back — but that job took its snapshot of the queue before the row existed, so
nothing would ever pick it up. Each push therefore ends by comparing the queue
against its own snapshot and reporting ``more_queued``; the handler enqueues a
follow-up with its own id excluded from the dedupe, exactly as the catalogue
and import jobs do.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import (
    Anime,
    ListEntry,
    ListStatus,
    MalLink,
    MalWriteCause,
    MalWriteLog,
    MalWriteStatus,
    UpdatedBy,
)
from arc.services.catalog.cache import ensure_anime
from arc.services.catalog.service import CatalogService
from arc.services.catalog.source import SourceNotFound, SourceUnavailable
from arc.services.mal import writelog
from arc.services.mal.client import (
    MalApiError,
    MalClient,
    MalListEntry,
    MalNotLinked,
    MalStatus,
)
from arc.services.mal.writelog import FIELD_PROGRESS, FIELD_SCORE, FIELD_STATUS, FIELDS

log = logging.getLogger(__name__)

#: How many shows one import will look up from a catalogue source. An import
#: is the first thing that happens after linking, and a three-hundred-title
#: list would otherwise be three hundred upstream requests in a row. Fifty per
#: run, spaced, with the rest picked up by the run this one queues behind it.
RESOLVE_LIMIT = 50

#: Seconds between those lookups. Two, not the five the catalogue's own hourly
#: reconciliation uses, and the difference is what the job is for: the
#: reconciliation is background repair with nobody waiting, while this runs
#: inside the import a user is watching a spinner for, and it holds one of the
#: worker's two slots for the whole time. The clients underneath pace
#: themselves (AniList at 700 ms a request, MAL likewise), so two seconds is
#: still comfortably inside every published limit — it just stops a fifty-title
#: chunk from occupying a slot for four minutes to no one's benefit.
RESOLVE_SPACING_SECONDS = 2.0

#: How long before the follow-up import runs, when one run could not resolve
#: every unknown title. Long enough not to be a hot loop, short enough that a
#: big list is complete within the hour.
CONTINUE_DELAY_SECONDS = 300.0

#: MAL's spelling of "no score".
UNSCORED = 0


async def _sleep(seconds: float) -> None:
    """Indirection so a test can skip the resolution spacing."""
    import asyncio

    await asyncio.sleep(seconds)


# --- The rules, as pure functions ------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalEntry:
    """The half of a ``list_entries`` row the sync rules care about."""

    status: ListStatus
    progress: int
    score: int | None
    updated_at: datetime
    mal_dirty: bool

    @classmethod
    def of(cls, entry: ListEntry) -> LocalEntry:
        return cls(
            status=entry.status,
            progress=entry.progress,
            score=entry.score,
            updated_at=entry.updated_at,
            mal_dirty=entry.mal_dirty,
        )


#: What an import does with one entry.
type ImportAction = Literal["create", "overwrite", "conflict", "keep"]

#: An empty MAL entry: what "this show is not on the user's MAL list" looks
#: like to the push diff, so that adding a show and changing one is one code
#: path rather than two.
ABSENT = MalStatus(status=None, score=None, progress=0, updated_at=None)


def decide_import(local: LocalEntry | None, remote: MalStatus) -> ImportAction:
    """Which side wins for one entry (FR-M2, FR-M3).

    A MAL entry with no ``updated_at`` — MAL has been known to omit it on very
    old rows — counts as *older* than any local change. Arc's timestamp is
    known; guessing that an unknown one is newer would discard a change the
    user definitely made in favour of one that may not have happened.
    """
    if local is None:
        return "create"
    if not local.mal_dirty:
        return "overwrite"
    if remote.updated_at is not None and remote.updated_at > local.updated_at:
        return "conflict"
    return "keep"


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field of one entry, as it will be logged and written."""

    field: str
    old: Any
    new: Any


#: Why a queued row was closed without being sent. Sentences rather than codes:
#: they are rendered verbatim in the user's log (FR-M5, FR-M6).
SKIP_SUPERSEDED = "superseded by a later change"
SKIP_LOWERS_PROGRESS = "automatic progress never lowers MAL"
SKIP_CLEARS_SCORE = "an automatic event never clears a MyAnimeList score"
SKIP_ALREADY = "MyAnimeList already has this value"
SKIP_NO_MAL_ID = "this show has no MyAnimeList id, so it cannot be synced"
SKIP_LEFT_THE_LIST = "the show is no longer on your list"
SKIP_REMOVED = "the show was removed from your list"
SKIP_RE_ADDED = "the show was re-added"
SKIP_DISCONNECTED = "MyAnimeList was disconnected"
SKIP_NO_MAL_ID_DELETE = "this show has no MyAnimeList id"

#: What a push says when it found the queue empty: a retry after the write
#: landed, or a ``mal_push_all`` that got to the pair first.
NOTHING_QUEUED = "nothing queued"

#: …and when every queued field turned out to need no write.
NOTHING_TO_CHANGE = "nothing to change"


@dataclass(frozen=True, slots=True)
class QueuedWrite:
    """One pending log row, as the rules see it: a field, a value, an event."""

    field: str
    value: Any
    cause: MalWriteCause

    @classmethod
    def of(cls, row: MalWriteLog) -> QueuedWrite:
        return cls(field=row.field, value=row.new_value, cause=row.cause)


@dataclass(frozen=True, slots=True)
class FieldPlan:
    """What the push will do with one field, and why.

    ``skipped`` is the reason the field is *not* being sent, or ``None`` when
    it is. ``old`` is MyAnimeList's current value — the value the write
    replaces, which is what the log has to record (FR-M5).
    """

    field: str
    old: Any
    new: Any
    cause: MalWriteCause
    skipped: str | None = None

    @property
    def sendable(self) -> bool:
        return self.skipped is None


def _status_value(status: ListStatus | None) -> str | None:
    return status.value if status is not None else None


def _remote_value(remote: MalStatus, field: str) -> Any:
    """MyAnimeList's current value for one field name."""
    if field == FIELD_STATUS:
        return _status_value(remote.status)
    if field == FIELD_SCORE:
        return remote.score
    if field == FIELD_PROGRESS:
        return remote.progress
    raise ValueError(f"unknown MyAnimeList field {field!r}")  # pragma: no cover


def _guard(change: QueuedWrite, old: Any) -> str | None:
    """The FR-M4 guards for one field, from *that field's* cause.

    Returns the reason the field must not be sent, or ``None`` to send it.
    """
    if old == change.value:
        return SKIP_ALREADY
    if change.cause is not MalWriteCause.WATCH:
        # An explicit edit or a revert is a statement about what the user
        # wants; neither guard applies to one.
        return None
    if change.field == FIELD_PROGRESS and int(change.value or 0) < int(old or 0):
        return SKIP_LOWERS_PROGRESS
    if change.field == FIELD_SCORE and change.value is None:
        return SKIP_CLEARS_SCORE
    return None


def decide_push(changes: Sequence[QueuedWrite], remote: MalStatus) -> list[FieldPlan]:
    """What to do with each queued field, in log order (FR-M4, FR-M7).

    Pure, and the whole of the write rules: one plan per field, each carrying
    the value MyAnimeList currently holds, the value the user asked for, the
    event that asked, and — when Arc refuses — the sentence saying why.
    """
    plans: list[FieldPlan] = []
    for field in FIELDS:
        change = next((item for item in changes if item.field == field), None)
        if change is None:
            continue
        writelog.assert_write_cause(change.cause)
        old = _remote_value(remote, field)
        plans.append(
            FieldPlan(
                field=field,
                old=old,
                new=change.value,
                cause=change.cause,
                skipped=_guard(change, old),
            )
        )
    return plans


def coalesce(rows: Sequence[MalWriteLog]) -> tuple[dict[str, MalWriteLog], list[MalWriteLog]]:
    """``field → the row that stands``, and the rows it superseded.

    Two edits to one field before a single push are one write, not two: MAL
    would end up at the later value either way, and sending the first would be
    a write to a value the user has already moved past. The earlier rows are
    still closed — ``skipped``, with the reason — because FR-M5's log is a
    history of what the user did, not only of what left the building.
    """
    latest: dict[str, MalWriteLog] = {}
    superseded: list[MalWriteLog] = []
    for row in rows:
        previous = latest.get(row.field)
        if previous is not None:
            superseded.append(previous)
        latest[row.field] = row
    return latest, superseded


def apply_change(entry: ListEntry, *, field: str, value: Any) -> None:
    """Write one logged value back onto a list entry — the revert primitive.

    Used by ``POST /api/mal/log/{id}/revert`` to replay ``old_value``. Kept
    here rather than in the router so that "what a field name means" is
    written once, next to the code that produced the name.
    """
    if field == FIELD_STATUS:
        if value is None:
            raise ValueError("a list entry cannot have no status")
        entry.status = ListStatus(str(value))
    elif field == FIELD_SCORE:
        entry.score = int(value) if value is not None else None
    elif field == FIELD_PROGRESS:
        entry.progress = int(value or 0)
    else:  # pragma: no cover - the three constants are the only field names
        raise ValueError(f"unknown MyAnimeList field {field!r}")


# --- Import ----------------------------------------------------------------


@dataclass(slots=True)
class ImportReport:
    """What one import run did, for the log line and the follow-up decision."""

    seen: int = 0
    created: int = 0
    overwritten: int = 0
    conflicts: int = 0
    kept: int = 0
    resolved: int = 0
    #: Titles whose MAL id Arc has no local row for and did not have the
    #: budget to look up this run. A non-zero value means "run me again".
    deferred: int = 0
    unresolvable: int = 0


async def run_import(
    session: AsyncSession,
    catalog: CatalogService,
    client: MalClient,
    *,
    user_id: int,
    now: datetime | None = None,
) -> ImportReport:
    """Pull the user's whole MAL list and reconcile it (FR-M2, FR-M3)."""
    at = now or datetime.now(UTC)
    report = ImportReport()
    remote_entries = await client.animelist()
    report.seen = len(remote_entries)

    known = await _rows_by_mal_id(session, [entry.mal_id for entry in remote_entries])
    unknown = [entry for entry in remote_entries if entry.mal_id not in known]
    for index, entry in enumerate(unknown):
        if index >= RESOLVE_LIMIT:
            report.deferred = len(unknown) - RESOLVE_LIMIT
            break
        if index:
            await _sleep(RESOLVE_SPACING_SECONDS)
        anime = await _resolve(session, catalog, entry)
        if anime is None:
            report.unresolvable += 1
            continue
        known[entry.mal_id] = anime
        report.resolved += 1

    for entry in remote_entries:
        anime = known.get(entry.mal_id)
        if anime is None:
            continue
        await _import_one(
            session, user_id=user_id, anime=anime, remote=entry, now=at, report=report
        )

    link = await session.get(MalLink, user_id)
    if link is not None:
        link.last_import_at = at
    await session.flush()
    return report


async def _rows_by_mal_id(session: AsyncSession, mal_ids: Sequence[int]) -> dict[int, Anime]:
    """One query for every show on the list Arc already has cached."""
    if not mal_ids:
        return {}
    rows = (await session.scalars(select(Anime).where(Anime.mal_id.in_(list(set(mal_ids)))))).all()
    return {row.mal_id: row for row in rows if row.mal_id is not None}


async def _resolve(
    session: AsyncSession, catalog: CatalogService, entry: MalListEntry
) -> Anime | None:
    """Fetch and cache the show behind one MAL id, or ``None`` if nothing can.

    A source that is merely down is not an error worth failing the import for:
    the entries Arc already knows still import, and the next run tries this one
    again.
    """
    try:
        return await ensure_anime(session, catalog, mal_id=entry.mal_id)
    except (SourceNotFound, SourceUnavailable) as exc:
        log.warning(
            "could not resolve a MyAnimeList entry",
            extra={"mal_id": entry.mal_id, "title": entry.title, "error": str(exc)},
        )
        return None


async def _import_one(
    session: AsyncSession,
    *,
    user_id: int,
    anime: Anime,
    remote: MalListEntry,
    now: datetime,
    report: ImportReport,
) -> None:
    """Apply :func:`decide_import` to one entry."""
    entry = await session.get(ListEntry, (user_id, anime.id))
    action = decide_import(LocalEntry.of(entry) if entry is not None else None, remote.status)
    if action == "keep":
        report.kept += 1
        return

    status = remote.status.status
    if status is None:  # pragma: no cover - filtered out when the page is parsed
        return

    if action == "conflict":
        assert entry is not None
        await _log_conflict(
            session, user_id=user_id, anime=anime, local=entry, remote=remote.status
        )
        # The local change lost, so its queued write must not go on to
        # resurrect it. Closing the rows here is what makes "MAL wins" mean
        # the same thing to the log, the badge and the push job.
        await discard_pending(
            session,
            user_id=user_id,
            anime_id=anime.id,
            reason="MyAnimeList changed this entry more recently; the queued write was dropped",
            include_failed=True,
        )
        report.conflicts += 1
    elif action == "create":
        entry = ListEntry(user_id=user_id, anime_id=anime.id, status=status, progress=0)
        session.add(entry)
        report.created += 1
    else:
        report.overwritten += 1

    assert entry is not None
    entry.status = status
    entry.progress = remote.status.progress
    entry.score = remote.status.score
    entry.updated_by = UpdatedBy.MAL
    entry.mal_dirty = False
    entry.mal_synced_at = now
    # MAL's own timestamp, not the import's: this column is what §5.5 step 4
    # compares against MAL next time, and stamping it "now" would make every
    # imported row look like the most recent change in the world.
    entry.updated_at = remote.status.updated_at or now
    await session.flush()


async def _log_conflict(
    session: AsyncSession,
    *,
    user_id: int,
    anime: Anime,
    local: ListEntry,
    remote: MalStatus,
) -> None:
    """Record the local change MyAnimeList overrode (FR-M3).

    One row per field that actually differed, read the same way as a write
    row: ``old_value`` is what the field held and lost, ``new_value`` is what
    replaced it. The difference is the cause and the status — ``conflict`` and
    ``skipped`` together mean "Arc sent nothing; this is what it gave up".
    """
    losses = [
        FieldChange(FIELD_STATUS, local.status.value, _status_value(remote.status)),
        FieldChange(FIELD_SCORE, local.score, remote.score),
        FieldChange(FIELD_PROGRESS, local.progress, remote.progress),
    ]
    for change in losses:
        if change.old == change.new:
            continue
        await writelog.record_closed(
            session,
            user_id=user_id,
            anime_id=anime.id,
            field=change.field,
            old_value=change.old,
            new_value=change.new,
            cause=MalWriteCause.CONFLICT,
            status=MalWriteStatus.SKIPPED,
            error="MyAnimeList changed this entry more recently; the local change was discarded",
        )


# --- Push ------------------------------------------------------------------


@dataclass(slots=True)
class PushReport:
    """What one push did. ``failed`` carries the message the user will see.

    ``needs_relink`` separates the one failure that must not be retried —
    MyAnimeList has disowned the credentials — from the ones that must.
    ``retryable`` says whether this failure is one the caller should raise on
    so the runner comes back with backoff; ``more_queued`` says an event landed
    after this run took its snapshot and a follow-up job is owed (FR-M6).
    """

    written: list[str]
    skipped: str | None = None
    failed: str | None = None
    needs_relink: bool = False
    retryable: bool = False
    more_queued: bool = False


async def push_entry(
    session: AsyncSession,
    client: MalClient,
    *,
    user_id: int,
    anime_id: int,
    now: datetime | None = None,
    last_attempt: bool = False,
) -> PushReport:
    """Send this pair's queued fields to MyAnimeList (FR-M4, FR-M5, FR-M7).

    The queue is the ``pending`` ``mal_write_log`` rows, written by the user
    event itself; this job carries no cause of its own, because each row
    carries the cause of the event that produced it (see the module docstring).

    A failure is *returned*, not raised, with the rows already noted or closed
    in the session. The caller commits them and then decides whether to retry —
    the job runner rolls a handler's session back when it raises, so recording
    the failure and signalling it cannot be the same act
    (:mod:`arc.services.jobs.runner`).

    ``last_attempt`` is "this job has no attempts left after this one". It is
    the only thing that turns a retryable failure into a closed ``failed`` row;
    while it is false the rows stay queued for the next attempt to find.
    """
    at = now or datetime.now(UTC)
    rows = await writelog.pending_for(session, user_id=user_id, anime_id=anime_id)
    seen = {row.id for row in rows}
    entry = await session.get(ListEntry, (user_id, anime_id))
    if not rows:
        # Nothing queued: a retry after the write landed, or a ``mal_push_all``
        # that got here first. Idempotence, which the queue requires.
        await _settle(session, entry, at=at)
        return PushReport(written=[], skipped=NOTHING_QUEUED)
    if entry is None:
        # The show left the list between the event and the run. Removal has its
        # own job (``delete=True``) and it closes these rows, so nothing here
        # may send anything: the entry it would describe no longer exists.
        return PushReport(written=[], skipped=SKIP_LEFT_THE_LIST)

    anime = await session.get(Anime, anime_id)
    if anime is None or anime.mal_id is None:
        # A row Arc only knows through AniList may have no MAL id at all. There
        # is nothing to write and no amount of retrying will produce one, so
        # the rows are closed with the reason and the entry stops claiming it
        # is behind. A later catalogue reconciliation can attach an id; the
        # user's next change queues a new row and it pushes then.
        for row in rows:
            writelog.finish(row, status=MalWriteStatus.SKIPPED, error=SKIP_NO_MAL_ID)
        await _settle(session, entry, at=at)
        return await _with_followup(
            session,
            PushReport(written=[], skipped=SKIP_NO_MAL_ID),
            user_id=user_id,
            anime_id=anime_id,
            seen=seen,
        )

    latest, superseded = coalesce(rows)
    for row in superseded:
        writelog.finish(row, status=MalWriteStatus.SKIPPED, error=SKIP_SUPERSEDED)

    remote = await client.my_list_status(anime.mal_id) or ABSENT
    plans = decide_push([QueuedWrite.of(row) for row in latest.values()], remote)
    for plan in plans:
        if not plan.sendable:
            writelog.finish(latest[plan.field], status=MalWriteStatus.SKIPPED, error=plan.skipped)

    sending = [plan for plan in plans if plan.sendable]
    if not sending:
        await _settle(session, entry, at=at)
        return await _with_followup(
            session,
            PushReport(written=[], skipped=NOTHING_TO_CHANGE),
            user_id=user_id,
            anime_id=anime_id,
            seen=seen,
        )

    sent_rows = []
    for plan in sending:
        row = latest[plan.field]
        # The value the write actually replaces, which is what FR-M5 asks the
        # log to record and what a revert has to put back. The row was written
        # with Arc's own previous value, before MyAnimeList had been asked.
        row.old_value = plan.old
        sent_rows.append(row)

    by_field = {plan.field: plan for plan in sending}
    try:
        await client.update_list_status(
            anime.mal_id,
            status=ListStatus(by_field[FIELD_STATUS].new) if FIELD_STATUS in by_field else None,
            score=_score_for_mal(by_field[FIELD_SCORE].new) if FIELD_SCORE in by_field else None,
            progress=by_field[FIELD_PROGRESS].new if FIELD_PROGRESS in by_field else None,
        )
    except MalNotLinked as exc:
        # Nothing was sent, and no amount of retrying fixes a rejected refresh
        # token. The rows are closed ``failed`` rather than left pending: the
        # event's record was committed by the event's own transaction, so
        # leaving it queued would be a change nothing will ever come back for,
        # counted forever in ``pending_writes``. Failed, with MyAnimeList's own
        # sentence, is what the log and the badge can act on (FR-M6).
        for row in sent_rows:
            writelog.finish(row, status=MalWriteStatus.FAILED, error=str(exc))
        await session.flush()
        return PushReport(written=[], failed=str(exc), needs_relink=True)
    except MalApiError as exc:
        return await _record_failure(session, sent_rows, exc, last_attempt=last_attempt)

    for row in sent_rows:
        writelog.finish(row, status=MalWriteStatus.OK)
    await _settle(session, entry, at=at)
    return await _with_followup(
        session,
        PushReport(written=[plan.field for plan in sending]),
        user_id=user_id,
        anime_id=anime_id,
        seen=seen,
    )


async def _record_failure(
    session: AsyncSession,
    rows: Sequence[MalWriteLog],
    exc: MalApiError,
    *,
    last_attempt: bool,
) -> PushReport:
    """What a write that did not land does to the rows it was carrying.

    The whole of the retry contract, in one place, because getting it wrong is
    silent: rows closed too early are rows the retry cannot find, and a retry
    that finds nothing queued reports success for a change that never left.
    See the module docstring's failure section.
    """
    message = str(exc)
    if exc.retryable and not last_attempt:
        for row in rows:
            writelog.note_error(row, message)
        await session.flush()
        return PushReport(written=[], failed=message, retryable=True)
    for row in rows:
        writelog.finish(row, status=MalWriteStatus.FAILED, error=message)
    await session.flush()
    return PushReport(written=[], failed=message, retryable=exc.retryable)


async def _with_followup(
    session: AsyncSession,
    report: PushReport,
    *,
    user_id: int,
    anime_id: int,
    seen: set[int],
) -> PushReport:
    """Flag the rows that arrived after this run took its snapshot.

    The event that wrote them found this job ``running`` under the pair's
    dedupe key and was handed it back, so unless somebody says so nothing will
    ever come for them. The handler queues the follow-up; deciding *that* one
    is owed is a rule, and lives here (see the module docstring).
    """
    left = await writelog.pending_for(session, user_id=user_id, anime_id=anime_id)
    report.more_queued = any(row.id not in seen for row in left)
    return report


async def _settle(session: AsyncSession, entry: ListEntry | None, *, at: datetime) -> None:
    """Clear ``mal_dirty`` if — and only if — the pair owes nothing at all.

    The flag is a cheap "does this show owe MyAnimeList anything", and the
    rows are the answer, so it is recomputed from them rather than assumed.
    ``session.flush()`` first because the rows this push just closed are still
    only in the session, and the question below is a query.
    """
    await session.flush()
    if entry is None:
        return
    if not await writelog.is_settled(session, user_id=entry.user_id, anime_id=entry.anime_id):
        return
    entry.mal_dirty = False
    entry.mal_synced_at = at
    await session.flush()


def _score_for_mal(score: int | None) -> int:
    """``None`` is MyAnimeList's ``0`` — see the module docstring's guard."""
    return UNSCORED if score is None else int(score)


async def discard_pending(
    session: AsyncSession,
    *,
    user_id: int,
    anime_id: int | None = None,
    reason: str,
    include_failed: bool = False,
) -> int:
    """Close queued rows as ``skipped`` — "this will never be sent, and why".

    ``skipped`` rather than ``failed`` because nothing was attempted and
    nothing went wrong: an import decided MyAnimeList's newer change wins, or
    the user disconnected the account. ``failed`` would badge the show as
    broken and invite a retry of a write Arc has deliberately abandoned.
    With no ``anime_id``, every queued row this user has.

    ``include_failed`` also closes the rows whose last attempt failed, and is
    used by exactly one caller: the import's conflict branch. Those rows are
    changes Arc still owed, MyAnimeList has just overridden them, and
    :func:`reopen_failed` would otherwise put them back on the queue and
    resurrect the very change the conflict discarded (FR-M3). The row keeps
    its values and its cause; what changes is that it is now closed, with the
    sentence saying who won.
    """
    targets = (
        [anime_id]
        if anime_id is not None
        else await writelog.pending_anime_ids(
            session, user_id=user_id, include_failed=include_failed
        )
    )
    closed = 0
    for target in targets:
        rows = list(await writelog.pending_for(session, user_id=user_id, anime_id=target))
        if include_failed:
            rows += await writelog.failed_attempts(session, user_id=user_id, anime_id=target)
        for row in rows:
            writelog.finish(row, status=MalWriteStatus.SKIPPED, error=reason)
        closed += len(rows)
    if closed:
        await session.flush()
    return closed


async def reopen_failed(session: AsyncSession, *, user_id: int, anime_id: int | None = None) -> int:
    """Put spent failures back on the queue — the "push pending" button (FR-M6).

    A row closed ``failed`` is a change Arc still holds and MyAnimeList has not
    been told about; the only thing that ended was the job's patience. Pressing
    the button is the user saying "try again now", and the queue is the rows,
    so trying again *is* making them pending. Only the rows whose field's last
    attempt failed are reopened: an old failure that a later write has already
    superseded would otherwise push MyAnimeList back to a value the user has
    moved past (:func:`~arc.services.mal.writelog.failed_attempts`).
    """
    rows = await writelog.failed_attempts(session, user_id=user_id, anime_id=anime_id)
    for row in rows:
        writelog.reopen(row)
    if rows:
        await session.flush()
    return len(rows)


async def abandon_pending(
    session: AsyncSession, *, user_id: int, anime_id: int | None = None, error: str
) -> int:
    """Close queued rows as ``failed`` — the "the link is gone" path (FR-M6).

    Used when a push cannot even be attempted because MyAnimeList has disowned
    the credentials. Left pending, those rows would be a promise nothing is
    coming back for; ``failed`` with the reason is what the user's log and the
    show badge can show, and what a re-link plus a fresh edit clears.
    """
    anime_ids = (
        [anime_id]
        if anime_id is not None
        else await writelog.pending_anime_ids(session, user_id=user_id)
    )
    closed = 0
    for target in anime_ids:
        rows = await writelog.pending_for(session, user_id=user_id, anime_id=target)
        for row in rows:
            writelog.finish(row, status=MalWriteStatus.FAILED, error=error)
        closed += len(rows)
    if closed:
        await session.flush()
    return closed


def is_removal(row: MalWriteLog) -> bool:
    """Whether this queued row is the record of a *removal* (FR-M5).

    :func:`record_removal` writes the one row that can say so: a ``status``
    whose new value is "no status at all", which is what leaving the list
    means and which no list edit can produce. Telling it apart from the edits
    that may be queued alongside it is what stops a ``DELETE`` from closing an
    unsent ``score`` change as though it had been written.
    """
    return row.field == FIELD_STATUS and row.new_value is None


async def delete_entry(
    session: AsyncSession,
    client: MalClient,
    *,
    user_id: int,
    anime_id: int,
    last_attempt: bool = False,
) -> PushReport:
    """Remove the show from the user's MAL list (FR-M4).

    The pending log row was written when the user removed the show, before
    this job existed, because *that* is the record of what happened: the list
    entry is gone and cannot carry a dirty flag. This closes it.

    If the show is back on the list by the time the job runs — removed and
    re-added — the delete is abandoned as ``skipped`` and the re-add's own
    push writes the truth. Sending the ``DELETE`` first and letting the PATCH
    undo it would be two writes for one user action, and a window in which
    MyAnimeList had lost an entry Arc never meant to remove.

    **The ``DELETE`` is never sent without a queued row to close.** No rows is
    an early return, exactly as the push's is: after a failed attempt closed
    its rows the retry would otherwise send a second, unlogged ``DELETE`` — a
    write to MyAnimeList with nothing in the log saying it happened, which is
    the one thing FR-M5 exists to make impossible. The retry rules are the
    push's (see the module docstring), and ``last_attempt`` means the same
    thing here.

    Edits queued for the pair before the removal are closed ``skipped``, not
    ``ok``: nothing was sent for them, the entry they described is gone, and a
    log that called them written would be a log that lied.
    """
    rows = await writelog.pending_for(session, user_id=user_id, anime_id=anime_id)
    seen = {row.id for row in rows}
    if not rows:
        return PushReport(written=[], skipped=NOTHING_QUEUED)

    if await session.get(ListEntry, (user_id, anime_id)) is not None:
        for row in rows:
            writelog.finish(row, status=MalWriteStatus.SKIPPED, error=SKIP_RE_ADDED)
        await session.flush()
        return PushReport(written=[], skipped=SKIP_RE_ADDED)

    removals = [row for row in rows if is_removal(row)]
    edits = [row for row in rows if not is_removal(row)]
    if not removals:
        # Edits for a show that is not on the list and has no removal to carry
        # them: there is no entry for them to describe and no request that
        # could send them. Closed rather than left queued, so they stop being
        # counted as work somebody is coming back for.
        for row in edits:
            writelog.finish(row, status=MalWriteStatus.SKIPPED, error=SKIP_LEFT_THE_LIST)
        await session.flush()
        return PushReport(written=[], skipped=SKIP_LEFT_THE_LIST)

    anime = await session.get(Anime, anime_id)
    if anime is None or anime.mal_id is None:
        for row in rows:
            writelog.finish(row, status=MalWriteStatus.SKIPPED, error=SKIP_NO_MAL_ID_DELETE)
        await session.flush()
        return PushReport(written=[], skipped=SKIP_NO_MAL_ID_DELETE)

    # Closed *before* the request, so that a retry after a failure finds only
    # the removal row and this stays "one DELETE, one pending row" however many
    # attempts it takes.
    for row in edits:
        writelog.finish(row, status=MalWriteStatus.SKIPPED, error=SKIP_REMOVED)
    await session.flush()

    try:
        await client.delete_list_status(anime.mal_id)
    except MalNotLinked as exc:
        # Same reasoning as the push: the rows outlive the handler's session,
        # so leaving them pending would queue work nothing will come back for.
        for row in removals:
            writelog.finish(row, status=MalWriteStatus.FAILED, error=str(exc))
        await session.flush()
        return PushReport(written=[], failed=str(exc), needs_relink=True)
    except MalApiError as exc:
        return await _record_failure(session, removals, exc, last_attempt=last_attempt)

    for row in removals:
        writelog.finish(row, status=MalWriteStatus.OK)
    await session.flush()
    return await _with_followup(
        session,
        PushReport(written=[FIELD_STATUS]),
        user_id=user_id,
        anime_id=anime_id,
        seen=seen,
    )


async def record_removal(
    session: AsyncSession, *, user_id: int, anime_id: int, status: ListStatus
) -> MalWriteLog:
    """The queued row a list removal writes before its job is (FR-M5).

    Written at the moment of the removal rather than by the job, because by
    the time the job runs the entry is gone and nothing could say what it
    held. The row is the record; the job only closes it.
    """
    return await writelog.record_pending(
        session,
        user_id=user_id,
        anime_id=anime_id,
        field=FIELD_STATUS,
        old_value=status.value,
        new_value=None,
        cause=MalWriteCause.MANUAL,
    )


async def pending_anime_ids(
    session: AsyncSession, *, user_id: int, include_failed: bool = False
) -> list[int]:
    """Shows this user still owes MyAnimeList — what a full push works through.

    ``include_failed`` widens "queued" to "queued or last failed", which is the
    set the "push pending" button means (FR-M6) and the set
    :func:`reopen_failed` has just put back on the queue.
    """
    return await writelog.pending_anime_ids(session, user_id=user_id, include_failed=include_failed)


__all__ = [
    "ABSENT",
    "CONTINUE_DELAY_SECONDS",
    "NOTHING_QUEUED",
    "NOTHING_TO_CHANGE",
    "RESOLVE_LIMIT",
    "RESOLVE_SPACING_SECONDS",
    "SKIP_ALREADY",
    "SKIP_CLEARS_SCORE",
    "SKIP_DISCONNECTED",
    "SKIP_LEFT_THE_LIST",
    "SKIP_LOWERS_PROGRESS",
    "SKIP_NO_MAL_ID",
    "SKIP_NO_MAL_ID_DELETE",
    "SKIP_RE_ADDED",
    "SKIP_REMOVED",
    "SKIP_SUPERSEDED",
    "UNSCORED",
    "FieldChange",
    "FieldPlan",
    "ImportAction",
    "ImportReport",
    "LocalEntry",
    "PushReport",
    "QueuedWrite",
    "abandon_pending",
    "apply_change",
    "coalesce",
    "decide_import",
    "decide_push",
    "delete_entry",
    "discard_pending",
    "is_removal",
    "pending_anime_ids",
    "push_entry",
    "record_removal",
    "reopen_failed",
    "run_import",
]
