"""The job queue: enqueue, claim, retry, recover, and the API on top of it.

These tests do not use the ``db_session`` fixture. That fixture wraps a test
in a transaction that is rolled back, which is exactly wrong here: the whole
point of the queue is what several *concurrent transactions* see of each
other, and a savepoint hides that. Everything below commits for real against
the test database, and :func:`jobs_factory` empties the table afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import DEFAULT_MAX_ATTEMPTS, DEFAULT_PRIORITY, Job, JobStatus, Setting
from arc.services.acquisition import names as acquisition_names
from arc.services.catalog import names as catalog_names
from arc.services.jobs import (
    JobContext,
    backoff,
    claim_one,
    claim_statement,
    enqueue,
    register,
    requeue_stale,
    run_job,
    run_worker_loop,
)
from arc.services.jobs.runner import MAX_BACKOFF
from arc.services.library import names as library_names
from arc.services.mal import names as mal_names
from arc.services.media import names as media_names

pytestmark = pytest.mark.pg

WORKER = "test-worker:1"

#: A handler that never succeeds, to prove attempts are exhausted rather than
#: retried forever. Registered once, at import.
ALWAYS_FAILS = "test_always_fails"

#: Long enough to still be running when a drain starts, short enough that a
#: patient drain waits it out.
SLEEPS_BRIEFLY = "test_sleeps_briefly"

#: Longer than any drain timeout in these tests: it is always cancelled.
SLEEPS_FOREVER = "test_sleeps_forever"

#: Writes a row and then fails, to prove the handler's session is rolled back.
WRITES_THEN_FAILS = "test_writes_then_fails"

#: The row that handler tries to write. It must never survive; the initial
#: migration seeds ``settings`` and ``test_migrations`` compares it exactly.
ROLLBACK_MARKER = "test_rollback_marker"


@register(ALWAYS_FAILS)
async def _always_fails(ctx: JobContext) -> None:
    raise ValueError(f"always fails (attempt {ctx.job.attempts})")


@register(SLEEPS_BRIEFLY)
async def _sleeps_briefly(ctx: JobContext) -> None:
    await asyncio.sleep(0.5)


@register(SLEEPS_FOREVER)
async def _sleeps_forever(ctx: JobContext) -> None:
    await asyncio.sleep(5)


@register(WRITES_THEN_FAILS)
async def _writes_then_fails(ctx: JobContext) -> None:
    ctx.session.add(Setting(key=ROLLBACK_MARKER, value={"written": True}))
    await ctx.session.flush()
    raise RuntimeError("wrote a row, then blew up")


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def jobs_factory(api_factory: SessionFactory) -> SessionFactory:
    """Real (committing) sessions against the test database.

    ``api_factory`` (conftest) empties ``jobs`` — and the account tables — on
    the way out, so tests cannot see each other's rows.
    """
    return api_factory


@pytest.fixture
def jobs_client(admin_client: AsyncClient) -> AsyncClient:
    """An HTTP client signed in as an admin: ``/api/jobs`` requires the role."""
    return admin_client


@pytest.fixture
async def no_rollback_marker(pg_engine: AsyncEngine) -> AsyncIterator[None]:
    """Guarantee the marker row is gone even if the rollback under test fails.

    ``settings`` is seeded by the initial migration and compared key-for-key by
    ``test_migrations``; a leaked row here would break an unrelated test.
    """
    try:
        yield
    finally:
        async with pg_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM settings WHERE key = :key"), {"key": ROLLBACK_MARKER}
            )


async def _reload(factory: SessionFactory, job_id: int) -> Job:
    async with factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        return job


async def _wait_for_status(
    factory: SessionFactory, job_id: int, wanted: JobStatus, timeout: float = 3.0
) -> Job:
    """Poll until the row reaches ``wanted``, rather than sleeping a guess."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    job = await _reload(factory, job_id)
    while loop.time() < deadline:
        if job.status is wanted:
            return job
        await asyncio.sleep(0.02)
        job = await _reload(factory, job_id)
    raise AssertionError(f"job {job_id} never became {wanted}; it is {job.status}")


def _worker(factory: SessionFactory, settings: Settings, stop: asyncio.Event) -> asyncio.Task[None]:
    """A worker loop running against the test database, one job at a time."""
    return asyncio.create_task(
        run_worker_loop(
            factory, settings, stop, worker_id=WORKER, concurrency=1, poll_interval=0.05
        )
    )


async def _enqueued(
    factory: SessionFactory,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = DEFAULT_PRIORITY,
    run_after: datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Job:
    """Enqueue and commit, the way a router or another job would."""
    async with factory() as session:
        job = await enqueue(
            session,
            type,
            payload,
            priority=priority,
            run_after=run_after,
            max_attempts=max_attempts,
        )
        await session.commit()
        return job


# --- Claiming ---------------------------------------------------------------


async def test_enqueue_then_claim(jobs_factory: SessionFactory) -> None:
    job = await _enqueued(jobs_factory, "noop", {"hello": "m1"})
    assert job.status is JobStatus.PENDING
    assert job.attempts == 0

    async with jobs_factory() as session:
        claimed = await claim_one(session, WORKER)

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.locked_by == WORKER
    assert claimed.locked_at is not None
    assert claimed.started_at is not None
    assert claimed.payload == {"hello": "m1"}

    # The row is no longer pending, so a second claim finds nothing.
    async with jobs_factory() as session:
        assert await claim_one(session, WORKER) is None


async def test_claim_order_is_priority_then_run_after(jobs_factory: SessionFactory) -> None:
    low = await _enqueued(jobs_factory, "noop", priority=200)
    high = await _enqueued(jobs_factory, "noop", priority=10)

    async with jobs_factory() as session:
        first = await claim_one(session, WORKER)
    async with jobs_factory() as session:
        second = await claim_one(session, WORKER)

    assert first is not None and second is not None
    assert first.id == high.id, "lower priority number must be claimed first"
    assert second.id == low.id


async def test_future_run_after_is_not_claimed(jobs_factory: SessionFactory) -> None:
    later = datetime.now(UTC) + timedelta(hours=1)
    await _enqueued(jobs_factory, "noop", run_after=later)

    async with jobs_factory() as session:
        assert await claim_one(session, WORKER) is None


async def test_a_naive_run_after_is_read_as_utc(jobs_factory: SessionFactory) -> None:
    """Not as host-local time — which would shift the delay by the offset."""
    naive = datetime(2030, 1, 2, 3, 4, 5)  # deliberately tz-naive

    async with jobs_factory() as session:
        job = await enqueue(session, "noop", run_after=naive)
        await session.commit()

    # Raw, so the assertion is about the instant Postgres stored rather than
    # about anything SQLAlchemy might do on the way back out.
    async with jobs_factory() as session:
        stored = await session.scalar(
            text("SELECT run_after FROM jobs WHERE id = :id").bindparams(id=job.id)
        )

    assert stored == naive.replace(tzinfo=UTC)


async def test_a_cancelled_job_is_never_claimed(jobs_factory: SessionFactory) -> None:
    job = await _enqueued(jobs_factory, "noop")

    async with jobs_factory() as session:
        row = await session.get(Job, job.id)
        assert row is not None
        row.status = JobStatus.CANCELLED
        await session.commit()

    async with jobs_factory() as session:
        assert await claim_one(session, WORKER) is None

    assert (await _reload(jobs_factory, job.id)).status is JobStatus.CANCELLED


async def test_skip_locked_lets_two_workers_claim_in_parallel(
    pg_engine: AsyncEngine, jobs_factory: SessionFactory
) -> None:
    """Two connections, two pending rows, no blocking — the whole design.

    Separate connections (not the savepoint session) are required: row locks
    are only visible between real, concurrent transactions.
    """
    first = await _enqueued(jobs_factory, "noop", priority=10)
    second = await _enqueued(jobs_factory, "noop", priority=20)

    async with pg_engine.connect() as one, pg_engine.connect() as two:
        await one.begin()
        await two.begin()

        row_one = (await asyncio.wait_for(one.execute(claim_statement()), timeout=5)).one()
        # Would block forever without SKIP LOCKED: the first transaction is
        # still holding the row it selected.
        row_two = (await asyncio.wait_for(two.execute(claim_statement()), timeout=5)).one()

        assert row_one.id == first.id
        assert row_two.id == second.id, "second worker must skip the locked row, not wait"


# --- Running, retrying, failing ---------------------------------------------


async def _claim_and_run(factory: SessionFactory, settings: Settings) -> JobStatus:
    async with factory() as session:
        job = await claim_one(session, WORKER)
    assert job is not None
    return await run_job(job, factory, settings)


async def test_successful_job_is_marked_done(
    jobs_factory: SessionFactory, settings: Settings
) -> None:
    job = await _enqueued(jobs_factory, "noop")

    assert await _claim_and_run(jobs_factory, settings) is JobStatus.DONE

    stored = await _reload(jobs_factory, job.id)
    assert stored.status is JobStatus.DONE
    assert stored.attempts == 1
    assert stored.finished_at is not None
    assert stored.locked_by is None
    assert stored.locked_at is None
    assert stored.last_error is None


async def test_failure_schedules_a_retry_and_then_succeeds(
    jobs_factory: SessionFactory, settings: Settings
) -> None:
    job = await _enqueued(jobs_factory, "fail_once")

    assert await _claim_and_run(jobs_factory, settings) is JobStatus.PENDING

    after_failure = await _reload(jobs_factory, job.id)
    assert after_failure.status is JobStatus.PENDING
    assert after_failure.attempts == 1
    assert after_failure.run_after > datetime.now(UTC), "retry must be delayed by the backoff"
    assert after_failure.last_error is not None
    assert "fail_once" in after_failure.last_error
    assert after_failure.locked_by is None
    assert after_failure.finished_at is None

    # It is not claimable until the backoff has elapsed; bring it forward.
    async with jobs_factory() as session:
        assert await claim_one(session, WORKER) is None
        due = await session.get(Job, job.id)
        assert due is not None
        due.run_after = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    assert await _claim_and_run(jobs_factory, settings) is JobStatus.DONE

    stored = await _reload(jobs_factory, job.id)
    assert stored.status is JobStatus.DONE
    assert stored.attempts == 2
    assert stored.finished_at is not None


async def test_attempts_are_exhausted_then_the_job_fails(
    jobs_factory: SessionFactory, settings: Settings
) -> None:
    job = await _enqueued(jobs_factory, ALWAYS_FAILS, max_attempts=2)

    assert await _claim_and_run(jobs_factory, settings) is JobStatus.PENDING
    async with jobs_factory() as session:
        due = await session.get(Job, job.id)
        assert due is not None
        due.run_after = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await _claim_and_run(jobs_factory, settings) is JobStatus.FAILED

    stored = await _reload(jobs_factory, job.id)
    assert stored.status is JobStatus.FAILED
    assert stored.attempts == 2
    assert stored.finished_at is not None
    assert stored.last_error is not None
    assert "always fails" in stored.last_error
    assert len(stored.last_error) <= 2000


async def test_unknown_type_fails_immediately(
    jobs_factory: SessionFactory, settings: Settings
) -> None:
    job = await _enqueued(jobs_factory, "no_such_handler", max_attempts=5)

    assert await _claim_and_run(jobs_factory, settings) is JobStatus.FAILED

    stored = await _reload(jobs_factory, job.id)
    assert stored.status is JobStatus.FAILED, "an unknown type is never worth retrying"
    assert stored.attempts == 1
    assert stored.last_error is not None
    assert "no_such_handler" in stored.last_error


def test_backoff_grows_and_is_capped() -> None:
    assert backoff(1) == timedelta(seconds=10)
    assert backoff(2) == timedelta(seconds=60)
    assert backoff(3) == timedelta(seconds=300)
    assert backoff(9) == timedelta(hours=1)


def test_backoff_never_raises_however_many_attempts() -> None:
    """A job may legitimately have a large ``max_attempts``.

    The exponent used to be applied before the cap, so ``5 ** n`` overflowed
    ``timedelta`` at around the twentieth attempt — inside ``run_job``, which
    promises never to raise, leaving the row stuck in ``running``.
    """
    previous = timedelta(0)
    for attempts in range(1, 101):
        delay = backoff(attempts)
        assert delay >= previous, f"backoff must never shrink (at {attempts})"
        assert delay <= MAX_BACKOFF, f"backoff must never exceed the cap (at {attempts})"
        previous = delay

    assert backoff(20) == MAX_BACKOFF
    assert backoff(10**6) == MAX_BACKOFF


async def test_a_failing_handler_writes_nothing(
    jobs_factory: SessionFactory, settings: Settings, no_rollback_marker: None
) -> None:
    """The handler's session is rolled back; only the job row survives."""
    job = await _enqueued(jobs_factory, WRITES_THEN_FAILS, max_attempts=2)

    assert await _claim_and_run(jobs_factory, settings) is JobStatus.PENDING
    assert (await _reload(jobs_factory, job.id)).status is JobStatus.PENDING
    async with jobs_factory() as session:
        assert await session.get(Setting, ROLLBACK_MARKER) is None, "half-done write survived"

        due = await session.get(Job, job.id)
        assert due is not None
        due.run_after = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    assert await _claim_and_run(jobs_factory, settings) is JobStatus.FAILED

    stored = await _reload(jobs_factory, job.id)
    assert stored.status is JobStatus.FAILED
    assert stored.last_error is not None and "blew up" in stored.last_error
    async with jobs_factory() as session:
        assert await session.get(Setting, ROLLBACK_MARKER) is None


# --- Dedupe and crash recovery ----------------------------------------------


async def test_dedupe_key_collapses_duplicate_enqueues(jobs_factory: SessionFactory) -> None:
    async with jobs_factory() as session:
        first = await enqueue(session, "noop", {"user_id": 3}, dedupe_key="wants:3")
        await session.commit()
    async with jobs_factory() as session:
        second = await enqueue(session, "noop", {"user_id": 3}, dedupe_key="wants:3")
        await session.commit()
        rows = list((await session.scalars(select(Job.id))).all())

    assert second.id == first.id
    assert len(rows) == 1
    assert first.payload["dedupe_key"] == "wants:3"


async def test_requeue_stale_recovers_abandoned_jobs(jobs_factory: SessionFactory) -> None:
    abandoned = await _enqueued(jobs_factory, "noop")
    fresh = await _enqueued(jobs_factory, "noop")

    async with jobs_factory() as session:
        for job_id in (abandoned.id, fresh.id):
            claimed = await session.get(Job, job_id)
            assert claimed is not None
            claimed.status = JobStatus.RUNNING
            claimed.locked_by = "dead-worker:1"
            claimed.locked_at = datetime.now(UTC)
            claimed.attempts = 1
        stale = await session.get(Job, abandoned.id)
        assert stale is not None
        stale.locked_at = datetime.now(UTC) - timedelta(minutes=30)
        await session.commit()

    async with jobs_factory() as session:
        assert await requeue_stale(session) == 1

    recovered = await _reload(jobs_factory, abandoned.id)
    assert recovered.status is JobStatus.PENDING
    assert recovered.locked_by is None
    assert recovered.locked_at is None
    assert recovered.attempts == 1, "the failed attempt still counts"

    untouched = await _reload(jobs_factory, fresh.id)
    assert untouched.status is JobStatus.RUNNING
    assert untouched.locked_by == "dead-worker:1"


async def test_requeue_stale_fails_a_job_with_no_attempts_left(
    jobs_factory: SessionFactory,
) -> None:
    """A job that kills its worker every time must stop, not cycle forever."""
    job = await _enqueued(jobs_factory, "noop", max_attempts=2)

    async with jobs_factory() as session:
        row = await session.get(Job, job.id)
        assert row is not None
        row.status = JobStatus.RUNNING
        row.locked_by = "dead-worker:1"
        row.locked_at = datetime.now(UTC) - timedelta(minutes=30)
        row.attempts = 2
        await session.commit()

    async with jobs_factory() as session:
        assert await requeue_stale(session) == 1

    stored = await _reload(jobs_factory, job.id)
    assert stored.status is JobStatus.FAILED, "attempts were spent; there is nothing to retry"
    assert stored.locked_by is None
    assert stored.locked_at is None
    assert stored.finished_at is not None
    assert stored.last_error is not None
    assert "lock expired" in stored.last_error


# --- Shutdown ---------------------------------------------------------------


async def test_drain_waits_for_an_in_flight_job(
    jobs_factory: SessionFactory, settings: Settings
) -> None:
    """Stopping mid-job lets it finish: the default drain timeout is generous."""
    job = await _enqueued(jobs_factory, SLEEPS_BRIEFLY)

    stop = asyncio.Event()
    worker = _worker(jobs_factory, settings, stop)
    try:
        await _wait_for_status(jobs_factory, job.id, JobStatus.RUNNING)
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
    finally:
        stop.set()

    stored = await _reload(jobs_factory, job.id)
    assert stored.status is JobStatus.DONE, "the drain must not cut a running job short"
    assert stored.finished_at is not None


async def test_drain_timeout_cancels_and_requeues(
    jobs_factory: SessionFactory, settings: Settings
) -> None:
    """A job too slow for the drain goes back to pending, not to the sweep."""
    job = await _enqueued(jobs_factory, SLEEPS_FOREVER)
    impatient = settings.model_copy(update={"worker_drain_timeout": 0.3})

    stop = asyncio.Event()
    worker = _worker(jobs_factory, impatient, stop)
    try:
        await _wait_for_status(jobs_factory, job.id, JobStatus.RUNNING)
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
    finally:
        stop.set()

    stored = await _reload(jobs_factory, job.id)
    assert stored.status is JobStatus.PENDING, "a cancelled job must not be left running"
    assert stored.locked_by is None
    assert stored.locked_at is None
    assert stored.attempts == 1, "the interrupted attempt still counts"
    assert stored.last_error is not None
    assert stored.run_after <= datetime.now(UTC), "it is claimable at once"


# --- API --------------------------------------------------------------------


async def test_api_create_and_read_a_job(jobs_client: AsyncClient) -> None:
    created = await jobs_client.post(
        "/api/jobs", json={"type": "noop", "payload": {"hello": "m1"}, "priority": 5}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["type"] == "noop"
    assert body["payload"] == {"hello": "m1"}
    assert body["status"] == "pending"
    assert body["priority"] == 5
    assert body["attempts"] == 0

    fetched = await jobs_client.get(f"/api/jobs/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]

    listed = await jobs_client.get("/api/jobs", params={"status": "pending", "type": "noop"})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [body["id"]]

    assert (await jobs_client.get("/api/jobs/999999")).status_code == 404


async def test_api_rejects_an_unknown_status_filter(jobs_client: AsyncClient) -> None:
    assert (await jobs_client.get("/api/jobs", params={"status": "nope"})).status_code == 422


@pytest.mark.parametrize(
    "field, value",
    [
        ("max_attempts", 0),
        ("max_attempts", 100000),
        ("priority", -1),
        ("priority", 99999999999),
    ],
)
async def test_api_rejects_out_of_range_numbers(
    jobs_client: AsyncClient, field: str, value: int
) -> None:
    """A 422, not a 500 from an integer too wide for the column."""
    created = await jobs_client.post("/api/jobs", json={"type": "noop", field: value})
    assert created.status_code == 422, created.text


async def test_api_dedupe_hit_is_200_not_201(jobs_client: AsyncClient) -> None:
    """201 would claim a row was created; the second call created nothing."""
    first = await jobs_client.post("/api/jobs", json={"type": "noop", "dedupe_key": "z"})
    assert first.status_code == 201, first.text

    second = await jobs_client.post("/api/jobs", json={"type": "noop", "dedupe_key": "z"})
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]

    listed = await jobs_client.get("/api/jobs", params={"type": "noop"})
    assert len(listed.json()) == 1


async def test_a_library_scan_dedupes_on_its_type_without_being_asked(
    jobs_client: AsyncClient,
) -> None:
    """A scan is the whole queue's work, not one row's (``TYPE_DEDUPED``).

    The scheduler already queues it with the type as the key; a caller that
    presses the button while that one is pending would otherwise queue a
    second walk of the same directories — and two ffprobe storms at once.
    """
    first = await jobs_client.post("/api/jobs", json={"type": "library_scan"})
    assert first.status_code == 201, first.text
    assert first.json()["payload"]["dedupe_key"] == "library_scan"

    second = await jobs_client.post("/api/jobs", json={"type": "library_scan"})

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    listed = await jobs_client.get("/api/jobs", params={"type": "library_scan"})
    assert len(listed.json()) == 1


async def test_an_explicit_dedupe_key_is_left_alone(jobs_client: AsyncClient) -> None:
    """A caller that names a key means it; only the *default* is filled in."""
    created = await jobs_client.post(
        "/api/jobs", json={"type": "library_scan", "dedupe_key": "library_scan:manual"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["payload"]["dedupe_key"] == "library_scan:manual"


async def test_other_types_still_queue_twice(jobs_client: AsyncClient) -> None:
    """The default is per type, not a blanket rule: two matches are two files."""
    first = await jobs_client.post("/api/jobs", json={"type": "match_file"})
    second = await jobs_client.post("/api/jobs", json={"type": "match_file"})
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["id"] != second.json()["id"]


# --- End to end (M1 definition of done) -------------------------------------


async def test_job_enqueued_from_the_api_is_run_by_the_worker_loop(
    jobs_client: AsyncClient, jobs_factory: SessionFactory, settings: Settings
) -> None:
    created = await jobs_client.post("/api/jobs", json={"type": "noop", "payload": {"e2e": True}})
    assert created.status_code == 201
    job_id = created.json()["id"]

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker_loop(
            jobs_factory,
            settings,
            stop,
            worker_id=WORKER,
            concurrency=2,
            poll_interval=0.05,
        )
    )

    try:
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if (await jobs_client.get(f"/api/jobs/{job_id}")).json()["status"] == "done":
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=5)

    final = (await jobs_client.get(f"/api/jobs/{job_id}")).json()
    assert final["status"] == "done", final
    assert final["attempts"] == 1
    assert final["locked_by"] is None
    assert final["finished_at"] is not None


# --- Priorities across the job types ----------------------------------------
#
# The numbers live in each service's ``names`` module and are passed at every
# enqueue. What matters is not any one of them but the *order* they put the
# queue in, so that is what is asserted: one job of every type, all due at the
# same instant, claimed one at a time.


#: Every job type Arc queues with a priority of its own, grouped into the tiers
#: the claim loop must take them in. Within a tier the order is ``run_after``
#: then ``id`` — oldest first — which is a statement about age, not about type,
#: so a tier is a set.
EXPECTED_TIERS: list[tuple[int, set[str]]] = [
    # A person changed a status or finished an episode and MyAnimeList does not
    # know yet. One HTTP call, and the only failure visible outside Arc.
    (mal_names.PUSH_PRIORITY, {mal_names.PUSH, mal_names.PUSH_ALL}),
    # A transcode for an episode somebody is two away from (FR-P3). Its number
    # is computed per job — ten times the distance, capped at 500 — so unlike
    # the rest of this table it spans the range rather than sitting at a point:
    # the very next episode comes out at 10 and shares the tier above, an
    # episode nobody is waiting for takes the default, and a far-off one sorts
    # behind the searches. Two episodes out is the middle of that.
    (2 * media_names.PRIORITY_PER_EPISODE, {media_names.TRANSCODE}),
    # Bytes already on the disk: finish the download, hand it to the library.
    (acquisition_names.POLL_QBIT_PRIORITY, {acquisition_names.POLL_QBIT}),
    # Everything that has not asked for a number sits here — ``match_file``,
    # and the two catalogue sweeps that only enqueue other jobs.
    (DEFAULT_PRIORITY, {library_names.MATCH_FILE, catalog_names.REFRESH_ALL}),
    # Then acquisition, in the order it happens: decide, then go looking.
    (acquisition_names.COMPUTE_WANTS_PRIORITY, {acquisition_names.COMPUTE_WANTS}),
    (acquisition_names.SEARCH_RELEASE_PRIORITY, {acquisition_names.SEARCH_RELEASE}),
    # And last, the bulk work on timers that nobody is waiting for. Every one
    # of these is 200; they are one tier and are listed as one.
    (
        mal_names.IMPORT_PRIORITY,
        {
            mal_names.IMPORT,
            mal_names.IMPORT_ALL,
            catalog_names.REFRESH,
            catalog_names.RECONCILE,
            catalog_names.SEASON_SWEEP,
            library_names.LIBRARY_SCAN,
        },
    ),
]


async def test_the_claim_order_is_the_priority_table(jobs_factory: SessionFactory) -> None:
    """One job of each type, all due at the same instant, claimed one at a time.

    Enqueued with the tiers *backwards*, so a run that passed because the queue
    happened to be in insertion order would fail here.
    """
    due = datetime.now(UTC) - timedelta(seconds=1)
    async with jobs_factory() as session:
        for priority, types in reversed(EXPECTED_TIERS):
            for job_type in sorted(types):
                await enqueue(session, job_type, priority=priority, run_after=due)
        await session.commit()

    claimed: list[str] = []
    while True:
        async with jobs_factory() as session:
            job = await claim_one(session, WORKER)
            if job is None:
                break
            claimed.append(job.type)

    assert len(claimed) == sum(len(types) for _, types in EXPECTED_TIERS)
    taken = iter(claimed)
    for priority, types in EXPECTED_TIERS:
        tier = {next(taken) for _ in types}
        assert tier == types, f"tier {priority} was claimed as {tier}"


async def test_a_mal_push_beats_a_burst_of_searches(jobs_factory: SessionFactory) -> None:
    """The regression this table exists for.

    Linking a MyAnimeList account imported fifteen watching shows, ``compute_
    wants`` wanted about thirty episodes, and thirty ``search_release`` jobs —
    all at the default priority, all enqueued before it — sat in front of the
    ``mal_push`` the user's own next list change produced. With the numbers
    passed at every enqueue, the push goes first however long the queue is.
    """
    due = datetime.now(UTC) - timedelta(seconds=1)
    async with jobs_factory() as session:
        for episode_id in range(30):
            await enqueue(
                session,
                acquisition_names.SEARCH_RELEASE,
                {"episode_id": episode_id},
                priority=acquisition_names.SEARCH_RELEASE_PRIORITY,
                run_after=due,
            )
        push = await enqueue(
            session,
            mal_names.PUSH,
            {"user_id": 1, "anime_id": 1},
            priority=mal_names.PUSH_PRIORITY,
            run_after=due,
        )
        await session.commit()
        push_id = push.id

    async with jobs_factory() as session:
        first = await claim_one(session, WORKER)

    assert first is not None
    assert first.id == push_id, "the user's MAL write must not queue behind thirty searches"
