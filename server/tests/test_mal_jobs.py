"""The MAL jobs end to end: import, push, delete, refresh (spec §4.7).

``tests/test_mal_rules.py`` proves the decisions; these prove the plumbing
around them — that the decision reaches the wire, that the write log records
what happened, that a failure survives the rollback the job runner performs,
and that the two hooks in the rest of Arc queue what they should and nothing
they should not.

Every assertion about a write is made against :attr:`FakeMalApi.patches`, which
holds the form exactly as httpx encoded it. "Progress was not lowered" is
therefore "no ``num_watched_episodes`` was sent", not "a helper returned an
empty list".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from arc.config import ConfigurationError, Settings
from arc.db import SessionFactory
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    ListEntry,
    ListStatus,
    MalLink,
    MalWriteCause,
    MalWriteLog,
    MalWriteStatus,
    UpdatedBy,
    User,
)
from arc.services.jobs import run_job
from arc.services.jobs.queue import enqueue
from arc.services.mal import jobs as mal_jobs  # noqa: F401  (registers the handlers)
from arc.services.mal import sync
from arc.services.mal.client import needs_relink, store_tokens
from arc.services.mal.factory import client_for, oauth_client
from arc.services.mal.names import (
    IMPORT,
    IMPORT_PRIORITY,
    PUSH,
    PUSH_ALL,
    PUSH_MAX_ATTEMPTS,
    PUSH_PRIORITY,
    enqueue_mal_push,
    push_dedupe_key,
)
from arc.services.mal.oauth import MalTokens
from arc.services.mal.sync import apply_change
from arc.services.mal.writelog import record_pending
from arc.services.playback.progress import record_progress
from tests.conftest import add_user
from tests.mal_api_mock import FakeMalApi, entry
from tests.mal_helpers import (
    USER_EMAIL,
    USER_PASSWORD,
    entry_of,
    link_of,
    link_user,
    log_rows,
    make_anime,
    make_entry,
    mal_settings,  # noqa: F401  (installs the ``settings`` override: MAL configured)
    queue_write,
)

pytestmark = pytest.mark.pg

OLD = "2026-01-01T00:00:00+00:00"
NEW = "2026-08-01T00:00:00+00:00"


@pytest.fixture
def mal(monkeypatch: pytest.MonkeyPatch) -> FakeMalApi:
    """The fake MAL API, wired into every client Arc builds."""
    monkeypatch.setattr("arc.services.mal.sync._sleep", _no_wait)
    return FakeMalApi().install(monkeypatch)


async def _no_wait(seconds: float) -> None:
    """The import spaces its catalogue lookups; a test must not pay for it."""
    return None


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


async def run(
    factory: SessionFactory,
    settings: Settings,
    job_type: str,
    payload: dict[str, Any],
) -> JobStatus:
    """Queue one job, claim it, run it — the worker's own path."""
    async with factory() as session:
        job = await enqueue(session, job_type, payload)
        await session.commit()
        job_id = job.id
    return await run_queued(factory, settings, job_id)


async def run_queued(factory: SessionFactory, settings: Settings, job_id: int) -> JobStatus:
    """Claim a job row that is already on the queue and run it."""
    async with factory() as session:
        claimed = await session.get(Job, job_id)
        assert claimed is not None
        claimed.status = JobStatus.RUNNING
        claimed.attempts += 1
        await session.commit()
        detached = claimed
    return await run_job(detached, factory, settings)


async def attempts(
    factory: SessionFactory,
    settings: Settings,
    job_type: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = PUSH_MAX_ATTEMPTS,
    until: Callable[[int], None] | None = None,
    limit: int = 12,
) -> list[JobStatus]:
    """Queue one job and run it through its retries; returns each outcome.

    The worker's own loop with the waiting taken out: the runner writes a
    ``run_after`` in the future and this claims the row anyway, so a test of
    the backoff *rule* does not cost the backoff itself. ``until`` is called
    with the number of attempts made so far, which is where a test lets the
    upstream recover partway through.
    """
    async with factory() as session:
        job = await enqueue(session, job_type, payload, max_attempts=max_attempts)
        await session.commit()
        job_id = job.id

    outcomes: list[JobStatus] = []
    for _ in range(limit):
        if until is not None:
            until(len(outcomes))
        async with factory() as session:
            claimed = await session.get(Job, job_id)
            assert claimed is not None
            if claimed.status is not JobStatus.PENDING:
                break
        outcomes.append(await run_queued(factory, settings, job_id))
        if outcomes[-1] is not JobStatus.PENDING:
            break
    return outcomes


def queued_probe(
    factory: SessionFactory, calls: list[tuple[str, int, int]]
) -> Callable[[str, int], Awaitable[None]]:
    """A mock probe that counts the show's queued rows as the write leaves.

    FR-M5's strongest claim, made at the only moment it can be: every request
    that changes something on MyAnimeList has a ``pending`` row in the log at
    the instant it is sent. Afterwards an unlogged write and a logged one look
    identical — the log has closed rows either way — so the count is taken
    from a session of its own while the handler's transaction is still open.
    """

    async def probe(method: str, mal_id: int) -> None:
        async with factory() as session:
            anime_id = await session.scalar(select(Anime.id).where(Anime.mal_id == mal_id))
            queued = await session.scalar(
                select(func.count())
                .select_from(MalWriteLog)
                .where(
                    MalWriteLog.anime_id == anime_id,
                    MalWriteLog.status == MalWriteStatus.PENDING,
                )
            )
            calls.append((method, mal_id, int(queued or 0)))

    return probe


async def jobs_of(factory: SessionFactory, job_type: str) -> list[Job]:
    async with factory() as session:
        rows = await session.scalars(select(Job).where(Job.type == job_type).order_by(Job.id))
        return list(rows.all())


# --- Import (FR-M2, FR-M3) --------------------------------------------------


async def test_import_creates_entries_from_every_page(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """The list is paged; a one-page import would silently truncate a big list."""
    await link_user(api_factory, settings, user_id=user.id)
    ids = [await make_anime(api_factory, mal_id=mal_id) for mal_id in (11, 22, 33)]
    mal.pages = [
        [entry(11, status="watching", progress=3, score=8)],
        [entry(22, status="completed", progress=12)],
        [entry(33, status="plan_to_watch")],
    ]

    assert await run(api_factory, settings, IMPORT, {"user_id": user.id}) is JobStatus.DONE

    first = await entry_of(api_factory, user_id=user.id, anime_id=ids[0])
    assert first is not None
    assert (first.status, first.progress, first.score) == (ListStatus.WATCHING, 3, 8)
    assert first.updated_by is UpdatedBy.MAL
    assert first.mal_dirty is False
    assert first.mal_synced_at is not None
    second = await entry_of(api_factory, user_id=user.id, anime_id=ids[1])
    assert second is not None and second.status is ListStatus.COMPLETED
    third = await entry_of(api_factory, user_id=user.id, anime_id=ids[2])
    assert third is not None and third.status is ListStatus.PLANNED

    link = await link_of(api_factory, user.id)
    assert link is not None and link.last_import_at is not None
    # An import never writes: it is a read and a reconciliation (FR-M7).
    assert mal.patches == [] and mal.deletes == []


async def test_import_overwrites_a_clean_row_and_keeps_a_newer_local_change(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    clean = await make_anime(api_factory, mal_id=11)
    dirty = await make_anime(api_factory, mal_id=22)
    await make_entry(api_factory, user_id=user.id, anime_id=clean, progress=1, dirty=False)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=dirty,
        progress=9,
        dirty=True,
        updated_at=datetime.now(UTC),
    )
    mal.pages = [
        [
            entry(11, progress=7, updated_at=NEW),
            entry(22, progress=2, updated_at=OLD),
        ]
    ]

    await run(api_factory, settings, IMPORT, {"user_id": user.id})

    overwritten = await entry_of(api_factory, user_id=user.id, anime_id=clean)
    assert overwritten is not None and overwritten.progress == 7
    kept = await entry_of(api_factory, user_id=user.id, anime_id=dirty)
    assert kept is not None
    assert kept.progress == 9
    # Still owed to MyAnimeList: its queued push is what carries it up.
    assert kept.mal_dirty is True
    assert await log_rows(api_factory, user.id) == []


async def test_a_newer_mal_change_wins_and_the_loss_is_logged(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """FR-M3: the more recent change wins, and the conflict is recorded."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=11)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        status=ListStatus.WATCHING,
        progress=4,
        score=6,
        dirty=True,
        updated_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    mal.pages = [[entry(11, status="dropped", progress=4, score=9, updated_at=NEW)]]

    await run(api_factory, settings, IMPORT, {"user_id": user.id})

    row = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert row is not None
    assert (row.status, row.score, row.progress) == (ListStatus.DROPPED, 9, 4)
    assert row.mal_dirty is False

    logged = await log_rows(api_factory, user.id)
    # One row per field that actually differed — progress agreed, so no row.
    assert {(item.field, item.old_value, item.new_value) for item in logged} == {
        ("status", "watching", "dropped"),
        ("score", 6, 9),
    }
    assert {item.cause for item in logged} == {MalWriteCause.CONFLICT}
    assert {item.status for item in logged} == {MalWriteStatus.SKIPPED}
    assert mal.patches == []


async def test_a_conflict_drops_the_queued_write_it_lost_to(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """MAL won, so the queued row must not go on to put the loser back.

    The rows are the queue: leaving one pending after the import overwrote the
    entry would send a value the import has just decided against, one job later
    (FR-M3).
    """
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=11)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        status=ListStatus.WATCHING,
        dirty=True,
        updated_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    queued = await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="planned",
        new="watching",
    )
    mal.pages = [[entry(11, status="dropped", updated_at=NEW)]]

    await run(api_factory, settings, IMPORT, {"user_id": user.id})

    rows = {row.id: row for row in await log_rows(api_factory, user.id)}
    assert rows[queued].status is MalWriteStatus.SKIPPED
    assert rows[queued].error and "more recently" in rows[queued].error
    # And a push that runs afterwards finds nothing left to send.
    await run(api_factory, settings, PUSH, push(user.id, anime_id))
    assert mal.patches == []


async def test_a_show_absent_from_mal_is_left_alone(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """Arc never deletes a user's data because a remote list omits it."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=99)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, progress=5)
    mal.pages = [[]]

    await run(api_factory, settings, IMPORT, {"user_id": user.id})

    kept = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert kept is not None and kept.progress == 5
    assert await log_rows(api_factory, user.id) == []


async def test_unknown_titles_are_resolved_through_the_catalogue_and_capped(
    api_factory: SessionFactory,
    settings: Settings,
    user: User,
    mal: FakeMalApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first import of a large list must not be one upstream request per row."""
    await link_user(api_factory, settings, user_id=user.id)
    monkeypatch.setattr(sync, "RESOLVE_LIMIT", 2)
    resolved: list[int] = []

    async def fake_ensure(session: Any, catalog: Any, **kwargs: Any) -> Anime:
        mal_id = int(kwargs["mal_id"])
        resolved.append(mal_id)
        anime = Anime(mal_id=mal_id, title_romaji=f"Show {mal_id}", episodes=12)
        session.add(anime)
        await session.flush()
        return anime

    monkeypatch.setattr(sync, "ensure_anime", fake_ensure)
    mal.pages = [[entry(mal_id) for mal_id in (101, 102, 103, 104)]]

    await run(api_factory, settings, IMPORT, {"user_id": user.id})

    assert resolved == [101, 102]
    async with api_factory() as session:
        rows = await session.scalars(select(ListEntry).where(ListEntry.user_id == user.id))
        assert len(list(rows.all())) == 2
    # The remainder is queued rather than left for the six-hourly sweep.
    queued = await jobs_of(api_factory, IMPORT)
    follow_ups = [job for job in queued if job.status is JobStatus.PENDING]
    assert len(follow_ups) == 1
    assert follow_ups[0].run_after > datetime.now(UTC)
    # …and behind everything, like the run that queued it: an import is bulk
    # work on a timer, and it must not sit in front of a user's own write.
    assert follow_ups[0].priority == IMPORT_PRIORITY
    assert IMPORT_PRIORITY > PUSH_PRIORITY


async def test_an_import_for_an_unlinked_user_does_nothing(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    assert await run(api_factory, settings, IMPORT, {"user_id": user.id}) is JobStatus.DONE
    assert mal.calls == []


# --- Push (FR-M4, FR-M5, FR-M6) ---------------------------------------------
#
# The queue is the ``pending`` write log rows, so every push test queues what
# the user event would have queued (``queue_write``) and then runs the job with
# nothing but the pair. The job carries no cause: each row carries its own.


def push(user_id: int, anime_id: int, *, delete: bool = False) -> dict[str, Any]:
    """The whole payload of a ``mal_push``: a pair, and whether it is a removal."""
    return {"user_id": user_id, "anime_id": anime_id, "delete": delete}


async def test_a_push_sends_only_the_changed_fields_and_logs_each(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        status=ListStatus.COMPLETED,
        progress=12,
        score=9,
        dirty=True,
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="completed",
    )
    mal.statuses[52991] = {
        "status": "watching",
        "score": 9,
        "num_episodes_watched": 12,
        "updated_at": OLD,
    }

    status = await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert status is JobStatus.DONE
    # Only the field the user changed was queued, so only it was sent.
    assert mal.patch_form(52991) == {"status": "completed"}

    logged = await log_rows(api_factory, user.id)
    assert [(row.field, row.old_value, row.new_value) for row in logged] == [
        ("status", "watching", "completed")
    ]
    assert logged[0].status is MalWriteStatus.OK
    assert logged[0].cause is MalWriteCause.MANUAL

    pushed = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert pushed is not None
    assert pushed.mal_dirty is False and pushed.mal_synced_at is not None


async def test_a_queued_field_mal_already_agrees_with_is_closed_not_sent(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """A PATCH to a value MyAnimeList already holds is a write nobody asked for."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, score=9, dirty=True)
    await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="score", old=None, new=9
    )
    mal.statuses[52991] = {"status": "watching", "score": 9, "num_episodes_watched": 0}

    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert mal.patches == []
    logged = await log_rows(api_factory, user.id)
    assert [(row.status, row.error) for row in logged] == [
        (MalWriteStatus.SKIPPED, sync.SKIP_ALREADY)
    ]
    settled = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert settled is not None and settled.mal_dirty is False


async def test_a_watch_push_never_lowers_the_progress_mal_already_has(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """FR-M4, on the wire: no ``num_watched_episodes`` may leave — and it is logged."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, progress=2, dirty=True)
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="progress",
        old=1,
        new=2,
        cause=MalWriteCause.WATCH,
    )
    mal.statuses[52991] = {
        "status": "watching",
        "score": 0,
        "num_episodes_watched": 9,
        "updated_at": OLD,
    }

    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert mal.patches == []
    # Refused, not forgotten: FR-M5 wants the decision on the record.
    logged = await log_rows(api_factory, user.id)
    assert [(row.field, row.cause, row.status, row.error) for row in logged] == [
        ("progress", MalWriteCause.WATCH, MalWriteStatus.SKIPPED, sync.SKIP_LOWERS_PROGRESS)
    ]
    # Nothing was owed after all, so the entry stops claiming it is behind.
    settled = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert settled is not None and settled.mal_dirty is False


async def test_a_manual_push_may_lower_progress_and_says_so_in_the_log(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, progress=2, dirty=True)
    await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="progress", old=5, new=2
    )
    mal.statuses[52991] = {
        "status": "watching",
        "score": 0,
        "num_episodes_watched": 9,
        "updated_at": OLD,
    }

    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert mal.patch_form(52991) == {"num_watched_episodes": "2"}
    logged = await log_rows(api_factory, user.id)
    # ``old_value`` is MyAnimeList's own number, not Arc's: it is the value the
    # write replaced, and the value a revert has to put back (FR-M5).
    assert [(row.field, row.old_value, row.new_value) for row in logged] == [("progress", 9, 2)]


async def test_one_push_carries_a_manual_edit_and_a_watch_advance_correctly(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """The defect, on the wire.

    The user sets a score and a status, then finishes an episode of a show
    they are rewatching. Both events find the *same* queued job, because the
    queue deduplicates on the pair and returns the pending row unchanged. The
    score and status are the user's word and must go; the progress is
    automatic and below MyAnimeList's number, so it must not.
    """
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        status=ListStatus.WATCHING,
        progress=3,
        score=9,
        dirty=True,
    )
    await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="score", old=None, new=9
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="planned",
        new="watching",
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="progress",
        old=2,
        new=3,
        cause=MalWriteCause.WATCH,
    )
    mal.statuses[52991] = {"status": "plan_to_watch", "score": 0, "num_episodes_watched": 11}

    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    # One PATCH, and no ``num_watched_episodes`` in it.
    assert mal.patch_form(52991) == {"status": "watching", "score": "9"}
    logged = {row.field: row for row in await log_rows(api_factory, user.id)}
    assert (logged["score"].status, logged["score"].cause) == (
        MalWriteStatus.OK,
        MalWriteCause.MANUAL,
    )
    assert logged["status"].status is MalWriteStatus.OK
    assert (logged["progress"].status, logged["progress"].error) == (
        MalWriteStatus.SKIPPED,
        sync.SKIP_LOWERS_PROGRESS,
    )


async def test_a_manual_score_clear_survives_a_watch_advance_in_the_same_job(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """The mirror: a clear folded into a watch push used to be dropped silently."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, progress=4, dirty=True)
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="progress",
        old=3,
        new=4,
        cause=MalWriteCause.WATCH,
    )
    await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="score", old=8, new=None
    )
    mal.statuses[52991] = {"status": "watching", "score": 8, "num_episodes_watched": 3}

    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    # MAL spells "no score" as zero, and both fields go in one PATCH.
    assert mal.patch_form(52991) == {"score": "0", "num_watched_episodes": "4"}
    logged = {row.field: row for row in await log_rows(api_factory, user.id)}
    assert (logged["score"].status, logged["score"].old_value, logged["score"].new_value) == (
        MalWriteStatus.OK,
        8,
        None,
    )
    assert logged["progress"].status is MalWriteStatus.OK


async def test_two_edits_to_one_field_before_a_push_are_one_write(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """Coalescing: MAL gets the latest value, and the log says what it replaced."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, progress=7, dirty=True)
    first = await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="progress", old=1, new=4
    )
    second = await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="progress", old=4, new=7
    )
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 1}

    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert mal.patch_form(52991) == {"num_watched_episodes": "7"}
    logged = {row.id: row for row in await log_rows(api_factory, user.id)}
    assert (logged[first].status, logged[first].error) == (
        MalWriteStatus.SKIPPED,
        sync.SKIP_SUPERSEDED,
    )
    assert (logged[second].status, logged[second].old_value, logged[second].new_value) == (
        MalWriteStatus.OK,
        1,
        7,
    )


async def test_a_failed_push_is_noted_keeps_the_row_queued_and_retries(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """The evidence must survive the rollback the runner performs (FR-M6).

    And the *row* must survive it as ``pending``: the rows are the queue, so a
    row closed here is a row the retry cannot find, and a retry that finds
    nothing queued reports success for a change that never left.
    """
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory, user_id=user.id, anime_id=anime_id, status=ListStatus.DROPPED, dirty=True
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="dropped",
    )
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    mal.patch_status = 500

    status = await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert status is JobStatus.PENDING  # queued for another attempt
    logged = await log_rows(api_factory, user.id)
    assert len(logged) == 1
    assert logged[0].status is MalWriteStatus.PENDING
    assert logged[0].error and "500" in logged[0].error
    still_owed = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert still_owed is not None and still_owed.mal_dirty is True


async def test_the_retry_finds_the_row_again_and_sends_it(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """One HTTP attempt per job attempt, and the change lands on the second."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory, user_id=user.id, anime_id=anime_id, status=ListStatus.DROPPED, dirty=True
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="dropped",
    )
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    mal.patch_status = 500

    outcomes = await attempts(
        api_factory, settings, PUSH, push(user.id, anime_id), until=lambda n: _recover(mal, n)
    )

    assert outcomes == [JobStatus.PENDING, JobStatus.DONE]
    # Once per attempt, not once ever and not twice in one.
    assert [form for _, form in mal.patches] == [{"status": "dropped"}, {"status": "dropped"}]
    logged = await log_rows(api_factory, user.id)
    assert [(row.status, row.error) for row in logged] == [(MalWriteStatus.OK, None)]
    settled = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert settled is not None and settled.mal_dirty is False


def _recover(mal: FakeMalApi, attempt: int) -> None:
    """Let MyAnimeList come back after the first attempt."""
    if attempt >= 1:
        mal.patch_status = None


async def test_a_write_that_never_recovers_ends_failed_and_stays_owed(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """Attempts spent: the row closes ``failed`` and the entry stays dirty.

    Dirty is the half that matters most. A change that could not be sent is
    still a change MyAnimeList has not heard about, and clearing the flag
    would let the next import quietly overwrite it (FR-M3).
    """
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory, user_id=user.id, anime_id=anime_id, status=ListStatus.DROPPED, dirty=True
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="dropped",
    )
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    mal.patch_status = 500

    outcomes = await attempts(api_factory, settings, PUSH, push(user.id, anime_id))

    assert outcomes == [JobStatus.PENDING] * (PUSH_MAX_ATTEMPTS - 1) + [JobStatus.FAILED]
    assert len(mal.patches) == PUSH_MAX_ATTEMPTS
    logged = await log_rows(api_factory, user.id)
    assert [row.status for row in logged] == [MalWriteStatus.FAILED]
    assert logged[0].error and "500" in logged[0].error
    owed = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert owed is not None and owed.mal_dirty is True

    # …and the button reopens it once MyAnimeList is back (FR-M6).
    mal.patch_status = None
    assert await run(api_factory, settings, PUSH_ALL, {"user_id": user.id}) is JobStatus.DONE

    assert len(mal.patches) == PUSH_MAX_ATTEMPTS + 1
    reopened = await log_rows(api_factory, user.id)
    assert [(row.status, row.error) for row in reopened] == [(MalWriteStatus.OK, None)]
    pushed = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert pushed is not None and pushed.mal_dirty is False


async def test_a_form_mal_refuses_fails_at_once_without_a_retry(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """A 400 will be a 400 in an hour too; five backoffs only delay the news."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, score=9, dirty=True)
    await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="score", old=None, new=9
    )
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    mal.patch_status = 400

    outcomes = await attempts(api_factory, settings, PUSH, push(user.id, anime_id))

    assert outcomes == [JobStatus.DONE]
    assert len(mal.patches) == 1
    logged = await log_rows(api_factory, user.id)
    assert [row.status for row in logged] == [MalWriteStatus.FAILED]
    assert logged[0].error and "400" in logged[0].error
    owed = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert owed is not None and owed.mal_dirty is True


async def test_an_import_does_not_overwrite_a_change_whose_push_failed(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """The failure keeps the entry dirty, and dirty is what protects it."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        status=ListStatus.DROPPED,
        dirty=True,
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="dropped",
    )
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    mal.patch_status = 400  # fails once, and for good
    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    # MyAnimeList's copy is older than the change Arc could not send.
    mal.pages = [[entry(52991, status="watching", updated_at=OLD)]]
    await run(api_factory, settings, IMPORT, {"user_id": user.id})

    kept = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert kept is not None
    assert kept.status is ListStatus.DROPPED and kept.mal_dirty is True


async def test_an_import_newer_than_the_failed_push_still_wins_and_is_logged(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """The conflict rule is unchanged: MAL's newer change beats a lost write."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        status=ListStatus.DROPPED,
        dirty=True,
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="dropped",
    )
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    mal.patch_status = 400
    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    mal.pages = [[entry(52991, status="completed", progress=12, updated_at=NEW)]]
    await run(api_factory, settings, IMPORT, {"user_id": user.id})

    overwritten = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert overwritten is not None
    assert overwritten.status is ListStatus.COMPLETED and overwritten.mal_dirty is False
    rows = await log_rows(api_factory, user.id)
    # The write that failed is closed against the conflict rather than left as
    # a failure the "push pending" button would resurrect.
    assert [(row.cause, row.status) for row in rows] == [
        (MalWriteCause.MANUAL, MalWriteStatus.SKIPPED),
        (MalWriteCause.CONFLICT, MalWriteStatus.SKIPPED),
        (MalWriteCause.CONFLICT, MalWriteStatus.SKIPPED),
    ]


async def test_a_push_is_idempotent(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """A retried job must not write a second time or log a second row.

    The rows are the queue, so the second run finds nothing queued — which is
    a stronger guarantee than the dirty flag was: it is per field.
    """
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory, user_id=user.id, anime_id=anime_id, status=ListStatus.DROPPED, dirty=True
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="dropped",
    )

    await run(api_factory, settings, PUSH, push(user.id, anime_id))
    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert len(mal.patches) == 1
    assert len(await log_rows(api_factory, user.id)) == 1


async def test_a_show_with_no_mal_id_is_skipped_once_and_stops_asking(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=None, anilist_id=123456)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, dirty=True)
    await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="status", old=None, new="watching"
    )

    await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert mal.patches == []
    logged = await log_rows(api_factory, user.id)
    assert [(row.status, row.error) for row in logged] == [
        (MalWriteStatus.SKIPPED, sync.SKIP_NO_MAL_ID)
    ]
    quiet = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert quiet is not None and quiet.mal_dirty is False


# --- Removal ----------------------------------------------------------------


async def test_removing_a_show_deletes_it_on_mal_and_closes_the_pending_row(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    from arc.services.catalog.lists import remove_list_entry

    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, status=ListStatus.DROPPED)
    mal.statuses[52991] = {"status": "dropped", "score": 0, "num_episodes_watched": 0}

    async with api_factory() as session:
        assert await remove_list_entry(session, user_id=user.id, anime_id=anime_id) is True
        await session.commit()

    # The record exists before the job does: the entry is gone and nothing else
    # could say what it held (FR-M5).
    pending = await log_rows(api_factory, user.id)
    assert [(row.field, row.old_value, row.new_value, row.status) for row in pending] == [
        ("status", "dropped", None, MalWriteStatus.PENDING)
    ]
    queued = await jobs_of(api_factory, PUSH)
    assert queued[0].payload["delete"] is True
    assert queued[0].payload["dedupe_key"] == push_dedupe_key(user.id, anime_id, delete=True)
    # A push is the one job a person is waiting on; it goes in front of the
    # acquisition burst a list change is quite likely to have started.
    assert queued[0].priority == PUSH_PRIORITY

    await run(api_factory, settings, PUSH, push(user.id, anime_id, delete=True))

    assert mal.deletes == [52991]
    closed = await log_rows(api_factory, user.id)
    assert closed[0].status is MalWriteStatus.OK


async def test_a_removal_overtaken_by_a_re_add_sends_nothing(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """One user action must not become a DELETE and a PATCH that undoes it."""
    from arc.services.catalog.lists import remove_list_entry

    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id)
    async with api_factory() as session:
        await remove_list_entry(session, user_id=user.id, anime_id=anime_id)
        await session.commit()
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, dirty=True)

    await run(api_factory, settings, PUSH, push(user.id, anime_id, delete=True))

    assert mal.deletes == []
    rows = await log_rows(api_factory, user.id)
    assert rows[0].status is MalWriteStatus.SKIPPED


async def test_a_removal_does_not_mark_unsent_edits_as_written(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """A DELETE closes the removal. The edits it overtook were never sent."""
    from arc.services.catalog.lists import remove_list_entry

    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, score=9)
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="score", old=None, new=9
    )
    async with api_factory() as session:
        await remove_list_entry(session, user_id=user.id, anime_id=anime_id)
        await session.commit()

    await run(api_factory, settings, PUSH, push(user.id, anime_id, delete=True))

    assert mal.deletes == [52991]
    assert mal.patches == []  # the score never left, so nothing may say it did
    rows = await log_rows(api_factory, user.id)
    assert [(row.field, row.status, row.error) for row in rows] == [
        ("score", MalWriteStatus.SKIPPED, sync.SKIP_REMOVED),
        ("status", MalWriteStatus.OK, None),
    ]


async def test_a_failed_delete_is_retried_and_never_sent_unlogged(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """One DELETE per attempt, each with the removal's row still queued.

    The bug this pins down: closing the row on the first failure left the
    retry with nothing to close, and it sent the DELETE again anyway — a write
    to MyAnimeList with no queued row behind it, which is the one thing FR-M5
    exists to make impossible.
    """
    from arc.services.catalog.lists import remove_list_entry

    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id)
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    async with api_factory() as session:
        await remove_list_entry(session, user_id=user.id, anime_id=anime_id)
        await session.commit()
    mal.delete_status = 500
    calls: list[tuple[str, int, int]] = []
    mal.probe = queued_probe(api_factory, calls)

    outcomes = await attempts(
        api_factory,
        settings,
        PUSH,
        push(user.id, anime_id, delete=True),
        until=lambda n: _recover_delete(mal, n),
    )

    assert outcomes == [JobStatus.PENDING, JobStatus.DONE]
    assert mal.deletes == [52991, 52991]
    assert calls == [("DELETE", 52991, 1), ("DELETE", 52991, 1)]
    rows = await log_rows(api_factory, user.id)
    assert [(row.field, row.status) for row in rows] == [("status", MalWriteStatus.OK)]


def _recover_delete(mal: FakeMalApi, attempt: int) -> None:
    if attempt >= 1:
        mal.delete_status = None


async def test_an_unlinked_user_removing_a_show_logs_nothing(
    api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    from arc.services.catalog.lists import remove_list_entry

    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id)
    async with api_factory() as session:
        await remove_list_entry(session, user_id=user.id, anime_id=anime_id)
        await session.commit()

    assert await log_rows(api_factory, user.id) == []
    assert await jobs_of(api_factory, PUSH) == []


# --- Tokens (FR-M1) ---------------------------------------------------------


async def test_an_expired_token_is_refreshed_and_the_write_goes_through(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    await link_user(api_factory, settings, user_id=user.id, expires_in=timedelta(seconds=30))
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory, user_id=user.id, anime_id=anime_id, status=ListStatus.DROPPED, dirty=True
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="dropped",
    )

    status = await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert status is JobStatus.DONE
    assert [call["grant_type"] for call in mal.token_calls] == ["refresh_token"]
    assert mal.patch_form(52991) == {"status": "dropped"}
    # The rotated pair is stored, encrypted, for the next call.
    link = await link_of(api_factory, user.id)
    assert link is not None
    assert mal.access_token not in link.access_token_enc
    assert link.expires_at is not None and link.expires_at > datetime.now(UTC) + timedelta(days=1)


async def test_two_concurrent_pushes_refresh_the_token_once(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """FR-M1 under concurrency: one refresh, two writes, no spurious relink.

    MyAnimeList rotates the refresh token on use and refuses the spent one —
    which the fake does here too. Two jobs that both found the access token
    expiring would, without the row lock in :meth:`MalClient.refresh`, both
    refresh: the loser presents a token MAL has retired, is refused, and marks
    a perfectly healthy link "needs re-authorising". The lock makes the second
    one wait, re-read, and simply use what the first stored.
    """
    await link_user(api_factory, settings, user_id=user.id, expires_in=timedelta(seconds=30))
    mal.invalidate_used_refresh = True
    first = await make_anime(api_factory, mal_id=11)
    second = await make_anime(api_factory, mal_id=22)
    for anime_id, status_name in ((first, "dropped"), (second, "completed")):
        await make_entry(api_factory, user_id=user.id, anime_id=anime_id, dirty=True)
        await queue_write(
            api_factory,
            user_id=user.id,
            anime_id=anime_id,
            field="status",
            old="watching",
            new=status_name,
        )

    outcomes = await asyncio.gather(
        run(api_factory, settings, PUSH, push(user.id, first)),
        run(api_factory, settings, PUSH, push(user.id, second)),
    )

    assert outcomes == [JobStatus.DONE, JobStatus.DONE]
    assert [call["grant_type"] for call in mal.token_calls] == ["refresh_token"]
    assert sorted(mal_id for mal_id, _ in mal.patches) == [11, 22]
    link = await link_of(api_factory, user.id)
    assert link is not None and needs_relink(link) is False
    assert [row.status for row in await log_rows(api_factory, user.id)] == [
        MalWriteStatus.OK,
        MalWriteStatus.OK,
    ]


async def test_a_refusal_is_retried_once_when_another_job_had_rotated_the_token(
    api_factory: SessionFactory,
    settings: Settings,
    user: User,
    mal: FakeMalApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal caused by a spent token must not cost the user a re-link.

    The row lock is what stops two jobs racing in the first place, so this
    drives the belt rather than the braces: the lock is disabled for one
    client — modelling a refresh that could not take it — another session
    rotates the tokens mid-flight, and MyAnimeList refuses the one this client
    presented. The re-read finds the token has moved and the second attempt is
    made with the one that is actually current, instead of the link being
    written off.
    """
    await link_user(api_factory, settings, user_id=user.id, expires_in=timedelta(seconds=30))
    mal.invalidate_used_refresh = True

    async def rotate_elsewhere() -> None:
        """The other job: a real refresh, committed, while this one is mid-flight."""
        async with oauth_client(settings) as other_oauth:
            tokens = await other_oauth.refresh(refresh_token="refresh-0", now=datetime.now(UTC))
        async with api_factory() as other:
            link = await other.get(MalLink, user.id)
            assert link is not None
            store_tokens(settings, link, tokens)
            # Still expiring, so this client does not simply give up early:
            # the point of the test is the refusal, not the short-circuit.
            link.expires_at = datetime.now(UTC) + timedelta(seconds=30)
            await other.commit()

    presented: list[str] = []
    async with (
        api_factory() as session,
        client_for(settings, session, user_id=user.id) as client,
    ):
        monkeypatch.setattr(client, "_lock_link", _no_lock)
        real = client._oauth.refresh

        async def refresh(*, refresh_token: str, now: datetime) -> MalTokens:
            presented.append(refresh_token)
            if len(presented) == 1:
                await rotate_elsewhere()
            return await real(refresh_token=refresh_token, now=now)

        monkeypatch.setattr(client._oauth, "refresh", refresh)
        await client.refresh()

    # The first attempt presented the token MyAnimeList had already retired;
    # the re-read found the live one and the second attempt used it.
    assert len(presented) == 2
    assert presented[0] == "refresh-0" and presented[1] != "refresh-0"
    link = await link_of(api_factory, user.id)
    assert link is not None and needs_relink(link) is False


async def _no_lock() -> bool:
    """A refresh that could not take the ``mal_links`` row lock."""
    return False


async def test_a_rejected_refresh_marks_the_link_as_needing_reauthorisation(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """No amount of retrying fixes a dead refresh token; the user must act."""
    await link_user(api_factory, settings, user_id=user.id, expires_in=timedelta(seconds=30))
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory, user_id=user.id, anime_id=anime_id, status=ListStatus.DROPPED, dirty=True
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="dropped",
    )
    mal.refresh_fails = True

    status = await run(api_factory, settings, PUSH, push(user.id, anime_id))

    assert status is JobStatus.DONE  # not retried
    link = await link_of(api_factory, user.id)
    assert link is not None and needs_relink(link) is True
    assert link.mal_username is not None  # the row and its history survive
    still_owed = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert still_owed is not None and still_owed.mal_dirty is True
    assert mal.patches == []
    # The queued row is closed rather than left pending for ever: nothing is
    # coming back for it, and the user's log is where they find out (FR-M6).
    logged = await log_rows(api_factory, user.id)
    assert [row.status for row in logged] == [MalWriteStatus.FAILED]
    assert logged[0].error and "rejected the stored credentials" in logged[0].error


async def test_a_misconfigured_server_gives_up_instead_of_retrying(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """No FERNET_KEY, no client id: a deployment problem, not a transient one."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(api_factory, user_id=user.id, anime_id=anime_id, dirty=True)
    await queue_write(
        api_factory, user_id=user.id, anime_id=anime_id, field="status", old=None, new="watching"
    )

    def unconfigured(*args: Any, **kwargs: Any) -> Any:
        raise ConfigurationError("MAL_CLIENT_ID is not set")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("arc.services.mal.jobs.client_for", unconfigured)
        assert await run(api_factory, settings, PUSH, push(user.id, anime_id)) is JobStatus.DONE
        assert await run(api_factory, settings, PUSH_ALL, {"user_id": user.id}) is JobStatus.DONE

    assert mal.patches == []
    # Left queued: a configuration mistake is fixable, and the change is still
    # owed once it is fixed.
    assert [row.status for row in await log_rows(api_factory, user.id)] == [MalWriteStatus.PENDING]


# --- Push-all ---------------------------------------------------------------


async def test_push_all_sends_every_queued_entry_in_one_job(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    first = await make_anime(api_factory, mal_id=11)
    second = await make_anime(api_factory, mal_id=22)
    clean = await make_anime(api_factory, mal_id=33)
    await make_entry(
        api_factory, user_id=user.id, anime_id=first, status=ListStatus.DROPPED, dirty=True
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=first,
        field="status",
        old="watching",
        new="dropped",
    )
    await make_entry(api_factory, user_id=user.id, anime_id=second, progress=4, dirty=True)
    await queue_write(api_factory, user_id=user.id, anime_id=second, field="progress", old=1, new=4)
    await make_entry(api_factory, user_id=user.id, anime_id=clean, dirty=False)

    assert await run(api_factory, settings, PUSH_ALL, {"user_id": user.id}) is JobStatus.DONE

    assert sorted(mal_id for mal_id, _ in mal.patches) == [11, 22]
    assert len(await jobs_of(api_factory, PUSH)) == 0


async def test_push_all_keeps_a_watch_advance_a_watch_advance(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """The button re-sends queued work; it does not relabel it ``manual``.

    An unpushed watch advance below MyAnimeList's number is still automatic
    however it is retried, so the guard still applies — and the manual edit
    queued beside it still goes.
    """
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=11)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        status=ListStatus.WATCHING,
        progress=2,
        dirty=True,
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="progress",
        old=1,
        new=2,
        cause=MalWriteCause.WATCH,
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="planned",
        new="watching",
    )
    mal.statuses[11] = {"status": "plan_to_watch", "score": 0, "num_episodes_watched": 9}

    assert await run(api_factory, settings, PUSH_ALL, {"user_id": user.id}) is JobStatus.DONE

    assert mal.patch_form(11) == {"status": "watching"}
    logged = {row.field: row for row in await log_rows(api_factory, user.id)}
    assert (logged["progress"].status, logged["progress"].error) == (
        MalWriteStatus.SKIPPED,
        sync.SKIP_LOWERS_PROGRESS,
    )
    assert logged["progress"].cause is MalWriteCause.WATCH
    assert logged["status"].status is MalWriteStatus.OK


# --- The queue racing itself ------------------------------------------------


async def test_an_event_during_a_running_push_earns_a_follow_up_job(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """A change made while the push is in flight must not be stranded.

    The event finds the pair's job ``running`` under its dedupe key and is
    handed it back — but that job read the queue before the row existed. Left
    alone, nothing would ever come for it: no job, no dirty flag anybody acts
    on, and a change the user made sitting queued for ever.
    """
    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        status=ListStatus.COMPLETED,
        score=8,
        dirty=True,
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=anime_id,
        field="status",
        old="watching",
        new="completed",
    )
    mal.statuses[52991] = {"status": "watching", "score": 0, "num_episodes_watched": 0}
    async with api_factory() as session:
        first = await enqueue_mal_push(session, user_id=user.id, anime_id=anime_id)
        await session.commit()
        assert first is not None
        job_id = first.id

    deduped: list[int] = []

    async def land(method: str, mal_id: int) -> None:
        """The user scores the show while the status push is on the wire."""
        mal.probe = None
        async with api_factory() as session:
            await record_pending(
                session,
                user_id=user.id,
                anime_id=anime_id,
                field="score",
                old_value=None,
                new_value=8,
                cause=MalWriteCause.MANUAL,
            )
            handed = await enqueue_mal_push(session, user_id=user.id, anime_id=anime_id)
            await session.commit()
            assert handed is not None
            deduped.append(handed.id)

    mal.probe = land

    assert await run_queued(api_factory, settings, job_id) is JobStatus.DONE

    # The event's own enqueue was handed the job that was running…
    assert deduped == [job_id]
    # …so the push that was running queued the follow-up itself.
    queued = await jobs_of(api_factory, PUSH)
    assert [job.id for job in queued] == [job_id, queued[-1].id]
    assert len(queued) == 2 and queued[1].status is JobStatus.PENDING
    assert await run_queued(api_factory, settings, queued[1].id) is JobStatus.DONE

    assert [form for _, form in mal.patches] == [{"status": "completed"}, {"score": "8"}]
    rows = await log_rows(api_factory, user.id)
    assert [(row.field, row.status) for row in rows] == [
        ("status", MalWriteStatus.OK),
        ("score", MalWriteStatus.OK),
    ]
    settled = await entry_of(api_factory, user_id=user.id, anime_id=anime_id)
    assert settled is not None and settled.mal_dirty is False


# --- The ledger (FR-M5) -----------------------------------------------------


async def test_every_write_had_a_queued_row_at_the_moment_it_left(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """FR-M5 as a property of a whole run, not of one happy path.

    Successes, one write that fails and is retried, and one removal — every
    request that changed something on MyAnimeList is asserted, *as it is
    being sent*, to have a ``pending`` row in the log behind it. An unlogged
    write is invisible after the fact; this is the only moment it can be seen.
    """
    from arc.services.catalog.lists import remove_list_entry

    await link_user(api_factory, settings, user_id=user.id)
    edited = await make_anime(api_factory, mal_id=111, title="Edited")
    flaky = await make_anime(api_factory, mal_id=222, title="Flaky")
    removed = await make_anime(api_factory, mal_id=333, title="Removed")
    for mal_id in (111, 222, 333):
        mal.statuses[mal_id] = {"status": "watching", "score": 0, "num_episodes_watched": 0}

    await make_entry(
        api_factory, user_id=user.id, anime_id=edited, status=ListStatus.COMPLETED, dirty=True
    )
    await queue_write(
        api_factory,
        user_id=user.id,
        anime_id=edited,
        field="status",
        old="watching",
        new="completed",
    )
    await make_entry(api_factory, user_id=user.id, anime_id=flaky, score=7, dirty=True)
    await queue_write(api_factory, user_id=user.id, anime_id=flaky, field="score", old=None, new=7)
    await make_entry(api_factory, user_id=user.id, anime_id=removed)
    async with api_factory() as session:
        await remove_list_entry(session, user_id=user.id, anime_id=removed)
        await session.commit()

    calls: list[tuple[str, int, int]] = []
    counting = queued_probe(api_factory, calls)
    stumbled: set[int] = set()

    async def probe(method: str, mal_id: int) -> None:
        await counting(method, mal_id)
        # One transient failure, on the second show's first attempt only.
        mal.patch_status = 500 if mal_id == 222 and 222 not in stumbled else None
        stumbled.add(mal_id)

    mal.probe = probe

    outcomes = await attempts(api_factory, settings, PUSH_ALL, {"user_id": user.id})

    assert outcomes == [JobStatus.PENDING, JobStatus.DONE]
    assert calls == [
        ("PATCH", 111, 1),
        ("PATCH", 222, 1),
        ("DELETE", 333, 1),
        ("PATCH", 222, 1),
    ]
    assert all(queued >= 1 for _, _, queued in calls), calls
    rows = await log_rows(api_factory, user.id)
    assert {(row.anime_id, row.field, row.status) for row in rows} == {
        (edited, "status", MalWriteStatus.OK),
        (flaky, "score", MalWriteStatus.OK),
        (removed, "status", MalWriteStatus.OK),
    }


# --- The two hooks in the rest of Arc ---------------------------------------


async def test_a_list_edit_queues_a_manual_row_per_changed_field_when_linked(
    api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    from arc.services.catalog.lists import set_list_entry

    anime_id = await make_anime(api_factory, mal_id=52991)

    async with api_factory() as session:
        await set_list_entry(
            session,
            _no_catalogue(),
            user_id=user.id,
            anime_id=anime_id,
            status=ListStatus.WATCHING,
        )
        await session.commit()
    # No link: the change is still owed (``mal_dirty``), but nothing is queued.
    assert await jobs_of(api_factory, PUSH) == []
    assert await log_rows(api_factory, user.id) == []

    await link_user(api_factory, settings, user_id=user.id)
    async with api_factory() as session:
        await set_list_entry(
            session,
            _no_catalogue(),
            user_id=user.id,
            anime_id=anime_id,
            status=ListStatus.COMPLETED,
            score=9,
            score_given=True,
        )
        await session.commit()

    assert len(await jobs_of(api_factory, PUSH)) == 1
    queued = await log_rows(api_factory, user.id)
    # One row per field that moved — status, score, and the progress the
    # "completed means all of it" rule advanced to the episode count.
    assert [(row.field, row.old_value, row.new_value) for row in queued] == [
        ("status", "watching", "completed"),
        ("score", None, 9),
        ("progress", 0, 12),
    ]
    assert {row.cause for row in queued} == {MalWriteCause.MANUAL}
    assert {row.status for row in queued} == {MalWriteStatus.PENDING}


async def test_a_list_edit_that_changes_nothing_queues_nothing(
    api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    """A PUT that re-sends the state a show already has is not a change."""
    from arc.services.catalog.lists import set_list_entry

    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    await make_entry(
        api_factory, user_id=user.id, anime_id=anime_id, status=ListStatus.WATCHING, progress=3
    )

    async with api_factory() as session:
        await set_list_entry(
            session,
            _no_catalogue(),
            user_id=user.id,
            anime_id=anime_id,
            status=ListStatus.WATCHING,
            progress=3,
        )
        await session.commit()

    assert await log_rows(api_factory, user.id) == []


async def test_finishing_an_episode_queues_a_watch_row_but_a_rewatch_does_not(
    api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    """The only automatic write, and only when the number actually moved."""
    from arc.services.playback.progress import record_progress

    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)
    async with api_factory() as session:
        episode = Episode(anime_id=anime_id, number=3, state=EpisodeState.READY)
        session.add(episode)
        await session.commit()
        episode_id = episode.id

    async def finish() -> None:
        async with api_factory() as session:
            episode = await session.get(Episode, episode_id)
            assert episode is not None
            await record_progress(
                session,
                user_id=user.id,
                episode=episode,
                position_s=1400.0,
                duration_s=1420.0,
            )
            await session.commit()

    await finish()
    assert len(await jobs_of(api_factory, PUSH)) == 1
    queued = await log_rows(api_factory, user.id)
    # Progress and *only* progress: a completion is not a statement about a
    # status or a score, so it queues neither (FR-W2).
    assert [(row.field, row.old_value, row.new_value, row.cause) for row in queued] == [
        ("progress", 0, 3, MalWriteCause.WATCH)
    ]

    # Watching it again moves nothing, so nothing new may be queued — and the
    # completion is sticky, so the second call is not even a new completion.
    await finish()
    assert len(await jobs_of(api_factory, PUSH)) == 1
    assert len(await log_rows(api_factory, user.id)) == 1


async def test_a_watch_completion_for_an_unlinked_user_queues_nothing(
    api_factory: SessionFactory, settings: Settings, user: User
) -> None:
    from arc.services.playback.progress import record_progress

    anime_id = await make_anime(api_factory, mal_id=52991)
    async with api_factory() as session:
        episode = Episode(anime_id=anime_id, number=2, state=EpisodeState.READY)
        session.add(episode)
        await session.commit()
        await session.refresh(episode)
        await record_progress(
            session, user_id=user.id, episode=episode, position_s=1400.0, duration_s=1420.0
        )
        await session.commit()

    assert await log_rows(api_factory, user.id) == []
    assert await jobs_of(api_factory, PUSH) == []


# --- The whole thing, as a user would drive it ------------------------------


async def test_the_life_of_one_show_from_add_to_revert(
    api_factory: SessionFactory, settings: Settings, user: User, mal: FakeMalApi
) -> None:
    """Every event in order, each producing exactly one write and no more.

    Set watching with progress 3 → one PATCH. Finish it → one PATCH carrying
    progress only. Revert that → one PATCH lowering it, because a revert is a
    statement. Then an import, which must write nothing at all (FR-M7).
    """
    from arc.services.catalog.lists import set_list_entry

    await link_user(api_factory, settings, user_id=user.id)
    anime_id = await make_anime(api_factory, mal_id=52991)

    async with api_factory() as session:
        await set_list_entry(
            session,
            _no_catalogue(),
            user_id=user.id,
            anime_id=anime_id,
            status=ListStatus.WATCHING,
            progress=3,
        )
        await session.commit()
    await run(api_factory, settings, PUSH, push(user.id, anime_id))
    assert mal.patches == [(52991, {"status": "watching", "num_watched_episodes": "3"})]

    # …then watches episode 4 to the end.
    async with api_factory() as session:
        episode = Episode(anime_id=anime_id, number=4, state=EpisodeState.READY)
        session.add(episode)
        await session.commit()
        await session.refresh(episode)
        await record_progress(
            session, user_id=user.id, episode=episode, position_s=1400.0, duration_s=1420.0
        )
        await session.commit()
    await run(api_factory, settings, PUSH, push(user.id, anime_id))
    assert mal.patches[-1] == (52991, {"num_watched_episodes": "4"})

    # …then takes it back from the log. A revert may lower progress.
    watch_row = next(
        row for row in await log_rows(api_factory, user.id) if row.cause is MalWriteCause.WATCH
    )
    assert watch_row.status is MalWriteStatus.OK
    async with api_factory() as session:
        row = await session.get(ListEntry, (user.id, anime_id))
        assert row is not None
        apply_change(row, field=watch_row.field, value=watch_row.old_value)
        row.mal_dirty = True
        await session.flush()
        await record_pending(
            session,
            user_id=user.id,
            anime_id=anime_id,
            field=watch_row.field,
            old_value=4,
            new_value=watch_row.old_value,
            cause=MalWriteCause.REVERT,
        )
        await session.commit()
    await run(api_factory, settings, PUSH, push(user.id, anime_id))
    assert mal.patches[-1] == (52991, {"num_watched_episodes": "3"})

    # Four PATCHes would be two writes for one user action somewhere above.
    assert len(mal.patches) == 3

    # An import writes nothing, whatever it finds (FR-M7).
    before = len(mal.patches)
    mal.pages = [[entry(52991, status="completed", progress=12, score=7)]]
    await run(api_factory, settings, IMPORT, {"user_id": user.id})
    assert len(mal.patches) == before and mal.deletes == []


class _FreshCatalogue:
    """Stands in for the catalogue service, and refuses to be *fetched* from.

    ``set_list_entry`` goes through ``ensure_anime``, which asks the breaker
    whether AniList is healthy before deciding the cached row is fresh. That
    one question is answered; anything else — a search, a by-id fetch — raises,
    so a test about MyAnimeList cannot quietly become a test about the network.
    """

    def healthy(self, name: str) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - a guard
        raise AssertionError(f"the catalogue should not be asked for {name}")


def _no_catalogue() -> Any:
    return _FreshCatalogue()
