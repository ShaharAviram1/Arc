"""The four MyAnimeList job handlers (FR-M2, FR-M3, FR-M4, FR-M6).

* ``mal_import`` — pull one user's whole list and reconcile it. Queued when an
  account is linked, by the six-hourly sweep, and by the import button.
* ``mal_import_all`` — the sweep: one ``mal_import`` per linked user, spaced.
* ``mal_push`` — send one dirty entry, or delete one removed entry.
* ``mal_push_all`` — send everything a user still owes MyAnimeList.

Two things about failure are specific to this package and worth stating,
because getting either wrong loses the audit trail the whole milestone exists
to produce.

**A failure must be committed before it is raised.** The job runner rolls a
handler's session back when it raises (:mod:`arc.services.jobs.runner`), which
is exactly right for a half-written import and exactly wrong for the
``mal_write_log`` row that records *why* a write failed. So the handlers here
commit the log rows first and raise afterwards; the runner's rollback then has
nothing left to undo, and the retry it schedules finds the evidence waiting.

**What the retry finds is the point.** The rows are the queue, so a retryable
failure leaves them ``pending`` with the error as a note and they close
``failed`` only when this job has no attempts left — which is why every call
below passes ``last_attempt``, computed from the job's own row. The rule lives
in :mod:`arc.services.mal.sync`; the handler's part is to say whether the
attempts are spent, to commit, and then to raise so the backoff happens. A
non-retryable failure — a form MyAnimeList will refuse just as firmly in an
hour — is recorded and the job ends normally: five backoffs would only delay
the moment the user is told.

**"Needs re-authorising" is not retried.** A rejected refresh token cannot be
fixed by waiting, and five backoffs would only delay the moment the user is
told. Those handlers close the queued rows as ``failed`` with MyAnimeList's
own sentence and return normally: the link row says the credentials are gone,
``GET /api/mal/status`` says ``needs_relink``, and the log and the show badge
say what was not sent. The rows are closed rather than left queued because
they were committed by the user's transaction and would otherwise be a promise
nothing is coming back for — and because a re-link runs an import, which is
the thing that decides who wins after an absence (FR-M2, FR-M3).

**A misconfigured server is not retried either.** A missing client id, secret
or Fernet key raises :class:`~arc.config.ConfigurationError`; no backoff fixes
a deployment, so it is logged at ``error`` and the job ends.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from arc.config import ConfigurationError
from arc.models import ListEntry, MalLink
from arc.services.catalog.factory import catalog_for
from arc.services.jobs.queue import enqueue
from arc.services.jobs.registry import JobContext, register
from arc.services.mal import sync
from arc.services.mal.client import MalApiError, MalNotLinked
from arc.services.mal.factory import client_for
from arc.services.mal.names import (
    IMPORT,
    IMPORT_ALL,
    IMPORT_PRIORITY,
    PUSH,
    PUSH_ALL,
    PUSH_MAX_ATTEMPTS,
    PUSH_PRIORITY,
    import_dedupe_key,
    push_dedupe_key,
)

#: Seconds between the imports the sweep queues. One user's import is a couple
#: of MAL requests plus up to fifty catalogue lookups, so starting a hundred
#: of them at once would be the noisiest thing Arc does all day.
SWEEP_SPACING_SECONDS = 30.0


@register(IMPORT)
async def mal_import(ctx: JobContext) -> None:
    """Import one user's MyAnimeList list (FR-M2, FR-M3)."""
    user_id = int(ctx.payload["user_id"])
    try:
        async with (
            client_for(ctx.settings, ctx.session, user_id=user_id) as client,
            catalog_for(ctx.settings) as catalog,
        ):
            report = await sync.run_import(ctx.session, catalog, client, user_id=user_id)
    except MalNotLinked as exc:
        ctx.log.warning(
            "MyAnimeList import skipped: the link needs attention",
            extra={"user_id": user_id, "error": str(exc)},
        )
        return
    except ConfigurationError as exc:
        ctx.log.error(
            "MyAnimeList import skipped: the server is not configured for it",
            extra={"user_id": user_id, "error": str(exc)},
        )
        return

    if report.deferred:
        # More unknown titles than one run's catalogue budget. Queue the
        # remainder rather than leaving them for the six-hourly sweep: the
        # import is the baseline every later decision is made against, and a
        # partial baseline is what makes a conflict resolve the wrong way.
        # ``exclude_job_id`` because this job is itself ``running`` under the
        # very key the follow-up would dedupe against.
        await enqueue(
            ctx.session,
            IMPORT,
            {"user_id": user_id},
            priority=IMPORT_PRIORITY,
            run_after=datetime.now(UTC) + timedelta(seconds=sync.CONTINUE_DELAY_SECONDS),
            dedupe_key=import_dedupe_key(user_id),
            exclude_job_id=ctx.job.id,
        )

    ctx.log.info(
        "MyAnimeList import",
        extra={
            "user_id": user_id,
            "seen": report.seen,
            # Not ``created``: ``logging.LogRecord`` already owns that name and
            # refuses to have it overwritten by an ``extra``.
            "created_entries": report.created,
            "overwritten": report.overwritten,
            "conflicts": report.conflicts,
            "kept": report.kept,
            "resolved": report.resolved,
            "deferred": report.deferred,
            "unresolvable": report.unresolvable,
        },
    )


@register(IMPORT_ALL)
async def mal_import_all(ctx: JobContext) -> None:
    """Queue an import for every linked account (FR-M3's six-hourly pull)."""
    rows = await ctx.session.scalars(select(MalLink.user_id).order_by(MalLink.user_id))
    user_ids = list(rows.all())
    for index, user_id in enumerate(user_ids):
        await enqueue(
            ctx.session,
            IMPORT,
            {"user_id": user_id},
            priority=IMPORT_PRIORITY,
            run_after=datetime.now(UTC) + timedelta(seconds=index * SWEEP_SPACING_SECONDS),
            dedupe_key=import_dedupe_key(user_id),
        )
    ctx.log.info("MyAnimeList import sweep", extra={"users": len(user_ids)})


@register(PUSH)
async def mal_push(ctx: JobContext) -> None:
    """Send one pair's queued writes to MyAnimeList (FR-M4, FR-M5, FR-M7).

    The payload is the pair and nothing else: *what* to write, and the cause of
    each field, is in the pending ``mal_write_log`` rows the user event wrote
    (:mod:`arc.services.mal.writelog`).
    """
    user_id = int(ctx.payload["user_id"])
    anime_id = int(ctx.payload["anime_id"])
    delete = bool(ctx.payload.get("delete"))
    last = _last_attempt(ctx)

    try:
        async with client_for(ctx.settings, ctx.session, user_id=user_id) as client:
            if delete:
                report = await sync.delete_entry(
                    ctx.session, client, user_id=user_id, anime_id=anime_id, last_attempt=last
                )
            else:
                report = await sync.push_entry(
                    ctx.session, client, user_id=user_id, anime_id=anime_id, last_attempt=last
                )
    except MalNotLinked as exc:
        # The link is gone before a single byte was attempted. The queued rows
        # were committed by the user's own transaction and nothing is coming
        # back for them, so they are closed ``failed`` with the reason rather
        # than counted as pending for ever (FR-M6).
        await _abandon(ctx, user_id=user_id, anime_id=anime_id, error=str(exc))
        return
    except ConfigurationError as exc:
        # No client id, no secret, no Fernet key: a deployment problem, not a
        # transient one. Retrying five times would only delay the log line.
        ctx.log.error(
            "MyAnimeList push skipped: the server is not configured for it",
            extra={"user_id": user_id, "anime_id": anime_id, "error": str(exc)},
        )
        return

    if report.more_queued:
        # An event landed while this job was ``running``: it found this very
        # job under the pair's dedupe key and was handed it back, but this run
        # had already read the queue. ``exclude_job_id`` is what stops the
        # follow-up deduping against the caller and vanishing — the same
        # reason ``mal_import`` passes it above. No new cause is asserted: the
        # rows carry the cause of the event that wrote them (FR-M7).
        await enqueue(
            ctx.session,
            PUSH,
            {"user_id": user_id, "anime_id": anime_id, "delete": False},
            priority=PUSH_PRIORITY,
            max_attempts=PUSH_MAX_ATTEMPTS,
            dedupe_key=push_dedupe_key(user_id, anime_id),
            exclude_job_id=ctx.job.id,
        )

    ctx.log.info(
        "MyAnimeList push",
        extra={
            "user_id": user_id,
            "anime_id": anime_id,
            "delete": delete,
            "written": report.written,
            "skipped": report.skipped,
            "failed": report.failed,
            "retryable": report.retryable,
            "more_queued": report.more_queued,
        },
    )
    if report.failed is not None and not report.needs_relink:
        # The log rows — noted and still queued, or closed ``failed`` because
        # the attempts ran out — are only in this session; commit them, then
        # raise so the runner schedules the retry (FR-M6). See the module
        # docstring: the two cannot be one act.
        await ctx.session.commit()
        if report.retryable:
            raise MalApiError(report.failed, retryable=True)
        ctx.log.warning(
            "MyAnimeList refused a push; it will not be retried",
            extra={"user_id": user_id, "anime_id": anime_id, "error": report.failed},
        )
    if report.needs_relink:
        ctx.log.warning(
            "MyAnimeList push abandoned: the link needs attention",
            extra={"user_id": user_id, "anime_id": anime_id, "error": report.failed},
        )


def _last_attempt(ctx: JobContext) -> bool:
    """Whether this run is the job's final one (FR-M6).

    ``attempts`` was incremented by the claim, so it counts *this* attempt:
    equal to ``max_attempts`` means the runner will fail the row rather than
    schedule another backoff, and therefore that nothing will come back for a
    row left queued.
    """
    return ctx.job.attempts >= ctx.job.max_attempts


async def _abandon(ctx: JobContext, *, user_id: int, anime_id: int | None, error: str) -> None:
    """Close this user's queued rows as ``failed`` and say so once."""
    closed = await sync.abandon_pending(
        ctx.session, user_id=user_id, anime_id=anime_id, error=error
    )
    ctx.log.warning(
        "MyAnimeList push skipped: the link needs attention",
        extra={"user_id": user_id, "anime_id": anime_id, "abandoned": closed, "error": error},
    )


@register(PUSH_ALL)
async def mal_push_all(ctx: JobContext) -> None:
    """Retry every change this user still owes MyAnimeList (FR-M6).

    Deliberately does **not** queue one ``mal_push`` per entry: that helper is
    reserved for the three user-originated events of FR-M7, and this is not
    one of them — it re-sends changes those events already queued and
    something transient dropped. It also means a hundred stale entries cost
    one job rather than a hundred.

    **It reopens the failures first.** A row that ran out of attempts is a
    change Arc still holds and MyAnimeList has not been told about; the only
    thing that ended was the job's patience, and pressing the button is the
    user saying "try again now". The queue is the rows, so trying again is
    making them ``pending`` again (:func:`~arc.services.mal.sync.reopen_failed`
    explains which ones, and why not all of them).

    One entry's failure does not abandon the rest: each is recorded in the
    write log where the user can see it. A *retryable* one does fail the job
    at the end, so the backoff FR-M6 asks for applies here too and the entries
    that already went through are simply found settled by the next attempt.

    It works through the shows that still owe something, not the ones flagged
    ``mal_dirty``, and it asserts no cause of its own: each row still carries
    the event that produced it, so a watch advance retried by this button is
    still a watch advance and still may not lower MyAnimeList's progress. A
    pair whose entry is gone is a *removal* that never landed, and goes to
    :func:`~arc.services.mal.sync.delete_entry` for the same reason.
    """
    user_id = int(ctx.payload["user_id"])
    last = _last_attempt(ctx)
    written = 0
    failed = 0
    retryable: str | None = None
    try:
        await sync.reopen_failed(ctx.session, user_id=user_id)
        anime_ids = await sync.pending_anime_ids(ctx.session, user_id=user_id)
        async with client_for(ctx.settings, ctx.session, user_id=user_id) as client:
            for anime_id in anime_ids:
                gone = await ctx.session.get(ListEntry, (user_id, anime_id)) is None
                report = (
                    await sync.delete_entry(
                        ctx.session, client, user_id=user_id, anime_id=anime_id, last_attempt=last
                    )
                    if gone
                    else await sync.push_entry(
                        ctx.session, client, user_id=user_id, anime_id=anime_id, last_attempt=last
                    )
                )
                if report.failed is not None:
                    failed += 1
                    if report.retryable:
                        retryable = report.failed
                elif report.written:
                    written += 1
    except MalNotLinked as exc:
        await _abandon(ctx, user_id=user_id, anime_id=None, error=str(exc))
        return
    except ConfigurationError as exc:
        ctx.log.error(
            "MyAnimeList push-all skipped: the server is not configured for it",
            extra={"user_id": user_id, "error": str(exc)},
        )
        return

    ctx.log.info(
        "MyAnimeList push-all",
        extra={"user_id": user_id, "entries": len(anime_ids), "written": written, "failed": failed},
    )
    if retryable is not None:
        # Same two-step as the single push: the notes are only in this session,
        # and the runner will roll it back on the way out.
        await ctx.session.commit()
        raise MalApiError(retryable, retryable=True)


__all__ = ["SWEEP_SPACING_SECONDS", "mal_import", "mal_import_all", "mal_push", "mal_push_all"]
