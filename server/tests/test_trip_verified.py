"""A held copy is never destroyed, and "delivered" means verified (M20.4 E, server).

Owner, 2026-10-30, after episode 15 of trip 4 was confirmed by the iPad, lost
there, and found deleted on the server. Over HTTP and against the services:

* the copy's SHA-256 is published on the trip and on ``OfflineOut``;
* a confirmation carrying a hash or size that is not the copy's is 409
  ``copy_mismatch`` and changes nothing; one without them works as before;
* holdings take the old ``episode_ids`` and the new ``episodes`` shape;
* a delivered row of an active trip whose own device leaves it out of its
  report is re-pended — and nothing else is;
* the settle keeps a trip's copy until the episode is watched or the trip
  ends (finished, cancelled, expired);
* a device's event log is stored, pruned to twenty, and printed by the CLI.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from arc.api.offline_schemas import OfflineOut
from arc.cli import EXIT_FAILED, cmd_device_report
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    DeviceHolding,
    DeviceReport,
    Episode,
    EpisodeState,
    Job,
    JobStatus,
    MediaFile,
    OfflineCopy,
    OfflineCopyState,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    WatchProgress,
)
from arc.services.acquisition.holdings import MAX_HOLDINGS
from arc.services.acquisition.names import COMPUTE_WANTS
from arc.services.devices.reports import MAX_REPORTS_PER_DEVICE, format_report
from arc.services.jobs.queue import enqueue
from arc.services.jobs.registry import JobContext
from arc.services.media.names import offline_path_for
from arc.services.playback.progress import record_progress, unmark_watched
from arc.services.retention.jobs import DELETE_EPISODE_FILES, delete_files
from arc.services.retention.sweep import idle_copies
from arc.services.trips.create import COPY_MISMATCH, TRIP_ACTIVE, TripConflict, create_trip
from arc.services.trips.deliver import copy_mismatch
from arc.services.trips.lost import REPEND_GRACE
from arc.services.trips.names import (
    OFFLINE_SETTLE,
    TRIP_RELEASE,
    TRIP_SWEEP,
    TRIP_SWEEP_PRIORITY,
)
from arc.services.trips.release import release_episode
from arc.services.trips.rules import held_episode_ids
from arc.services.trips.settle import KEPT_HELD, settle_episode
from arc.services.trips.sweep import ALL_WATCHED, sweep_trips, trip_complete
from tests.conftest import add_user, api_transport, login
from tests.retention_helpers import write_source_file
from tests.test_trip_delivery_api import a_trip, delivered_url, row_state

pytestmark = pytest.mark.pg

OWNER_EMAIL = "verified-owner@arc.test"
OWNER_PASSWORD = "verified-owner-password"
DEMO_EMAIL = "verified-demo@arc.test"
DEMO_PASSWORD = "verified-demo-password"
IPAD = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
MAC = "0b9a8c7d-6e5f-4a3b-9c2d-1e0f2a3b4c5d"


@pytest.fixture
def settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        env="test", database_url=test_database_url, data_dir=tmp_path, _env_file=None
    )


@pytest.fixture
async def owner(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, OWNER_EMAIL, OWNER_PASSWORD)


@pytest.fixture
async def owner_client(api_app: FastAPI, owner: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, OWNER_EMAIL, OWNER_PASSWORD)


@pytest.fixture
async def demo_client(api_app: FastAPI, api_factory: SessionFactory) -> AsyncIterator[AsyncClient]:
    made = await add_user(api_factory, DEMO_EMAIL, DEMO_PASSWORD)
    async with api_factory() as session:
        row = await session.get(User, made.id)
        assert row is not None
        row.is_demo = True
        await session.commit()
    async with api_transport(api_app) as http:
        yield await login(http, DEMO_EMAIL, DEMO_PASSWORD)


def holdings_url(device: str = IPAD) -> str:
    return f"/api/devices/{device}/holdings"


def report_url(device: str = IPAD) -> str:
    return f"/api/devices/{device}/report"


async def hash_copy(factory: SessionFactory, settings: Settings, episode_id: int) -> OfflineCopy:
    """Stamp a copy's row with the SHA-256 of its file, as the encode now does."""
    digest = hashlib.sha256(offline_path_for(settings, episode_id).read_bytes()).hexdigest()
    async with factory() as session:
        copy = await session.get(OfflineCopy, episode_id)
        assert copy is not None
        copy.sha256 = digest
        await session.commit()
        return copy


async def deliver(
    factory: SessionFactory,
    trip_id: int,
    episode_id: int,
    *,
    ago: timedelta = timedelta(minutes=30),
    released: bool = False,
) -> None:
    """Mark a row delivered ``ago`` (and released), as a confirmation would have."""
    at = datetime.now(UTC) - ago
    async with factory() as session:
        row = await session.get(TripEpisode, (trip_id, episode_id))
        assert row is not None
        row.state = TripEpisodeState.DELIVERED
        row.delivered_at = at
        row.released_at = at if released else None
        await session.commit()


async def set_trip_state(factory: SessionFactory, trip_id: int, state: TripState) -> None:
    async with factory() as session:
        trip = await session.get(Trip, trip_id)
        assert trip is not None
        trip.state = state
        if state is not TripState.ACTIVE:
            trip.ended_at = datetime.now(UTC)
        await session.commit()


async def watch(factory: SessionFactory, user_id: int, episode_id: int) -> None:
    async with factory() as session:
        session.add(
            WatchProgress(
                user_id=user_id,
                episode_id=episode_id,
                position_s=1400.0,
                duration_s=1420.0,
                completed=True,
                completed_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def jobs_of(factory: SessionFactory, kind: str) -> list[Job]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(Job).where(Job.type == kind, Job.status == JobStatus.PENDING)
                )
            ).all()
        )


# --- The hash is published --------------------------------------------------


async def test_the_trip_publishes_the_copys_sha256(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987001)
    copy = await hash_copy(api_factory, settings, ids[1])

    (trip,) = (await owner_client.get("/api/trips")).json()
    by_id = {row["episode_id"]: row for row in trip["episodes"]}
    assert by_id[ids[1]]["sha256"] == copy.sha256
    assert len(by_id[ids[1]]["sha256"]) == 64
    assert by_id[ids[2]]["sha256"] is None, "a copy made before 2026-10-30 has none"


def test_offline_out_carries_the_sha256_once_available() -> None:
    copy = OfflineCopy(
        episode_id=1,
        state=OfflineCopyState.READY,
        size=10,
        etag='"x"',
        codec="hevc",
        height=1080,
        sha256="a" * 64,
    )
    out = OfflineOut.build(1, copy=copy, job=None, has_source=True, codec="hevc", height=1080)
    assert out.state == "available" and out.sha256 == "a" * 64
    queued = OfflineCopy(episode_id=1, state=OfflineCopyState.QUEUED, sha256=None)
    assert OfflineOut.build(1, copy=queued, job=None, has_source=True, codec="hevc").sha256 is None


# --- A confirmation is verified -----------------------------------------------


def test_copy_mismatch_compares_only_what_both_sides_have() -> None:
    ready = OfflineCopy(episode_id=1, state=OfflineCopyState.READY, size=10, sha256="ab" * 32)
    assert not copy_mismatch(ready, sha256=None, size=None), "an old client"
    assert not copy_mismatch(ready, sha256="AB" * 32, size=10), "case does not matter"
    assert copy_mismatch(ready, sha256="cd" * 32, size=10)
    assert copy_mismatch(ready, sha256=None, size=11)
    unhashed = OfflineCopy(episode_id=1, state=OfflineCopyState.READY, size=10, sha256=None)
    assert not copy_mismatch(unhashed, sha256="cd" * 32, size=10), "no hash to compare"
    assert copy_mismatch(unhashed, sha256="cd" * 32, size=9)
    queued = OfflineCopy(episode_id=1, state=OfflineCopyState.QUEUED, size=10, sha256="ab" * 32)
    assert not copy_mismatch(queued, sha256="cd" * 32, size=1), "only a ready copy"
    assert not copy_mismatch(None, sha256="cd" * 32, size=1)


async def test_a_matching_hash_and_size_confirm(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987002, device_id=IPAD)
    copy = await hash_copy(api_factory, settings, ids[1])
    assert copy.sha256 is not None

    response = await owner_client.post(
        delivered_url(trip_id, ids[1]),
        json={
            "etag": copy.etag,
            "device_id": IPAD,
            "sha256": copy.sha256.upper(),
            "size": copy.size,
        },
    )

    assert response.status_code == 204, response.text
    row = await row_state(api_factory, trip_id, ids[1])
    assert row.state is TripEpisodeState.DELIVERED and row.delivered_at is not None


@pytest.mark.parametrize("wrong", ["sha256", "size"])
async def test_a_mismatching_hash_or_size_is_409_and_changes_nothing(
    wrong: str,
    owner_client: AsyncClient,
    owner: User,
    api_factory: SessionFactory,
    settings: Settings,
) -> None:
    # A trip no device has claimed yet: a refused confirmation must not claim it.
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987003)
    copy = await hash_copy(api_factory, settings, ids[1])
    assert copy.sha256 is not None and copy.size is not None
    body: dict[str, object] = {
        "etag": copy.etag,
        "device_id": IPAD,
        "sha256": copy.sha256,
        "size": copy.size,
    }
    body[wrong] = "0" * 64 if wrong == "sha256" else copy.size - 1

    response = await owner_client.post(delivered_url(trip_id, ids[1]), json=body)

    assert response.status_code == 409
    assert response.json()["detail"] == COPY_MISMATCH
    row = await row_state(api_factory, trip_id, ids[1])
    assert row.state is TripEpisodeState.PENDING and row.delivered_at is None
    async with api_factory() as session:
        trip = await session.get(Trip, trip_id)
        assert trip is not None and trip.device_id is None, "not claimed by a refusal"
        assert trip.state is TripState.ACTIVE
        kept = await session.get(OfflineCopy, ids[1])
        assert kept is not None and kept.state is OfflineCopyState.READY


async def test_an_old_shape_confirmation_still_works(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987004)
    copy = await hash_copy(api_factory, settings, ids[1])

    response = await owner_client.post(delivered_url(trip_id, ids[1]), json={"etag": copy.etag})

    assert response.status_code == 204
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.DELIVERED


async def test_a_full_size_confirmation_is_not_checked_against_the_copy(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987008, device_id=IPAD)
    await hash_copy(api_factory, settings, ids[1])
    body = {"device_id": IPAD, "sha256": "0" * 64, "size": 1, "variant": "full"}

    response = await owner_client.post(delivered_url(trip_id, ids[1]), json=body)

    assert response.status_code == 204, response.text
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.DELIVERED
    small = {**body, "variant": "small"}
    refused = await owner_client.post(delivered_url(trip_id, ids[2]), json=small)
    assert refused.status_code == 409 and refused.json()["detail"] == COPY_MISMATCH
    bad = {**body, "variant": "medium"}
    assert (await owner_client.post(delivered_url(trip_id, ids[2]), json=bad)).status_code == 422


# --- Holdings: two shapes -----------------------------------------------------


async def test_holdings_accept_the_old_and_the_new_shape(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987005)

    old = await owner_client.put(holdings_url(), json={"episode_ids": [ids[0]]})
    assert old.status_code == 204, old.text
    async with api_factory() as session:
        (row,) = (await session.scalars(select(DeviceHolding))).all()
        assert (row.episode_id, row.etag, row.size, row.sha256) == (ids[0], None, None, None)

    new = await owner_client.put(
        holdings_url(),
        json={
            "episodes": [
                {"episode_id": ids[1], "etag": '"e-1"', "size": 4096, "sha256": "AB" * 32},
                {"episode_id": ids[2]},
            ]
        },
    )
    assert new.status_code == 204, new.text
    async with api_factory() as session:
        rows = {
            row.episode_id: (row.etag, row.size, row.sha256)
            for row in (await session.scalars(select(DeviceHolding))).all()
        }
    assert rows == {ids[1]: ('"e-1"', 4096, "ab" * 32), ids[2]: (None, None, None)}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"episode_ids": [1], "episodes": [{"episode_id": 1}]},
        {"episodes": [{"episode_id": 1, "sha256": "xyz"}]},
        {"episodes": [{"episode_id": 1, "size": -1}]},
        {"episodes": [{"episode_id": 1, "colour": "red"}]},
        {"episodes": [{"episode_id": 1}] * (MAX_HOLDINGS + 1)},
    ],
)
async def test_a_malformed_holdings_body_is_422(
    body: dict[str, object], owner_client: AsyncClient
) -> None:
    assert (await owner_client.put(holdings_url(), json=body)).status_code == 422


# --- A lost copy is re-pended -----------------------------------------------------


async def test_a_delivered_copy_the_device_lost_is_re_pended(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987006, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1])
    await deliver(api_factory, trip_id, ids[2])
    before = datetime.now(UTC)

    response = await owner_client.put(
        holdings_url(),
        # A full-size file of an episode is held as well: never re-pended.
        json={"episodes": [{"episode_id": ids[2], "size": 1, "variant": "full"}]},
    )

    assert response.status_code == 204
    lost = await row_state(api_factory, trip_id, ids[1])
    assert lost.state is TripEpisodeState.PENDING
    assert lost.delivered_at is None and lost.released_at is None
    assert lost.available_at is not None and lost.available_at >= before, "copy still on disk"
    held = await row_state(api_factory, trip_id, ids[2])
    assert held.state is TripEpisodeState.DELIVERED
    assert await jobs_of(api_factory, COMPUTE_WANTS)


async def test_a_lost_copy_with_no_copy_left_waits_for_a_new_one(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987007, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1])
    offline_path_for(settings, ids[1]).unlink()

    response = await owner_client.put(holdings_url(), json={"episodes": []})

    assert response.status_code == 204
    row = await row_state(api_factory, trip_id, ids[1])
    assert row.state is TripEpisodeState.PENDING and row.available_at is None
    # The trip want comes back by the reconciliation: queued here.
    assert await jobs_of(api_factory, COMPUTE_WANTS)


@pytest.mark.parametrize(
    "case", ["other_device", "legacy", "released", "ended", "watched", "grace", "truncated"]
)
async def test_a_report_leaves_every_other_row_alone(
    case: str,
    owner_client: AsyncClient,
    owner: User,
    api_factory: SessionFactory,
    settings: Settings,
) -> None:
    device = {"other_device": MAC, "legacy": None}.get(case, IPAD)
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987010, device_id=device)
    await deliver(
        api_factory,
        trip_id,
        ids[1],
        released=case == "released",
        ago=REPEND_GRACE / 2 if case == "grace" else timedelta(minutes=30),
    )
    if case == "ended":
        await set_trip_state(api_factory, trip_id, TripState.FINISHED)
    if case == "watched":
        await watch(api_factory, owner.id, ids[1])
    if case == "truncated":
        # A report at the cap dropped ids to fit: an absence proves nothing.
        filler = [{"episode_id": 10_000_000 + n} for n in range(MAX_HOLDINGS - 1)]
        body = {"episodes": [{"episode_id": ids[0]}, *filler]}
    else:
        body = {"episodes": [{"episode_id": ids[0]}]}

    response = await owner_client.put(holdings_url(IPAD), json=body)

    assert response.status_code == 204, response.text
    row = await row_state(api_factory, trip_id, ids[1])
    assert row.state is TripEpisodeState.DELIVERED and row.delivered_at is not None


# --- The settle keeps a held copy -------------------------------------------------


async def test_the_settle_keeps_a_held_copy_until_it_is_watched(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    # Episode 4 stays pending, so the trip stays active through the sweeps.
    trip_id, ids = await a_trip(
        api_factory, settings, owner, anilist_id=987020, episodes=4, device_id=IPAD
    )
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    await deliver(api_factory, trip_id, ids[2], ago=timedelta(hours=3), released=True)
    later = datetime.now(UTC) + timedelta(days=3)

    async with api_factory() as session:
        assert await held_episode_ids(session, ids) == {ids[1], ids[2]}
        kept = await settle_episode(session, settings, ids[1], now=later)
        released = await settle_episode(session, settings, ids[2], now=later)
        await session.commit()
    assert (kept.outcome, kept.why) == ("kept", KEPT_HELD)
    assert released.outcome == "kept", "released while the trip is active: still kept"
    assert offline_path_for(settings, ids[1]).exists()

    # The hourly sweep does not queue a settle for a held copy...
    async with api_factory() as session:
        swept = await sweep_trips(session, now=later)
        await session.commit()
    assert ids[1] not in swept.settles

    # ...and does once its user has watched it; that settle deletes it.
    await watch(api_factory, owner.id, ids[1])
    async with api_factory() as session:
        swept = await sweep_trips(session, now=later)
        await session.commit()
    assert ids[1] in swept.settles and ids[2] not in swept.settles
    async with api_factory() as session:
        gone = await settle_episode(session, settings, ids[1], now=later)
        await session.commit()
    assert gone.outcome == "copy_deleted"
    assert not offline_path_for(settings, ids[1]).exists()


async def test_the_last_confirmation_keeps_the_trip_and_its_copies(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    """2026-10-30: nothing pending no longer ends a trip; it is held to its deadline."""
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987021, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3), released=True)

    response = await owner_client.post(delivered_url(trip_id, ids[2]), json={"device_id": IPAD})

    assert response.status_code == 204
    async with api_factory() as session:
        trip = await session.get(Trip, trip_id)
        assert trip is not None and trip.state is TripState.ACTIVE
    assert await jobs_of(api_factory, OFFLINE_SETTLE) == []
    # The device still sees every episode as delivered, and may ask again.
    (listed,) = (await owner_client.get("/api/trips")).json()
    assert listed["state"] == "active"
    assert {row["phase"] for row in listed["episodes"]} == {"delivered"}
    again = await owner_client.post(
        f"/api/trips/{trip_id}/episodes/{ids[1]}/again", json={"device_id": IPAD}
    )
    assert again.status_code == 200, again.text
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.PENDING


async def test_the_next_trip_ends_one_with_nothing_pending(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987024, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    await deliver(api_factory, trip_id, ids[2], ago=timedelta(hours=3), released=True)
    async with api_factory() as session:
        old = await session.get(Trip, trip_id)
        assert old is not None
        anime_id = old.anime_id
        user = await session.get(User, owner.id)
        assert user is not None
        at = datetime.now(UTC)
        made = await create_trip(
            session, settings, user=user, anime_id=anime_id, count=1, now=at, device_id=IPAD
        )
        await session.commit()
        new_id = made.trip.id
    async with api_factory() as session:
        old = await session.get(Trip, trip_id)
        assert old is not None and old.state is TripState.FINISHED and old.ended_at is not None
        new = await session.get(Trip, new_id)
        assert new is not None and new.state is TripState.ACTIVE
    settles = await jobs_of(api_factory, OFFLINE_SETTLE)
    assert {job.payload["episode_id"] for job in settles} == set(ids[1:])
    assert all(job.run_after >= at + timedelta(minutes=59) for job in settles)


async def test_the_next_trip_is_refused_while_one_is_still_pending(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987025, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1])
    async with api_factory() as session:
        old = await session.get(Trip, trip_id)
        user = await session.get(User, owner.id)
        assert old is not None and user is not None
        with pytest.raises(TripConflict, match=TRIP_ACTIVE):
            await create_trip(
                session, settings, user=user, anime_id=old.anime_id, count=1, now=datetime.now(UTC)
            )
        await session.rollback()
    async with api_factory() as session:
        kept = await session.get(Trip, trip_id)
        assert kept is not None and kept.state is TripState.ACTIVE


async def test_a_cancel_lets_the_delivered_copies_go(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987022, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))

    assert (await owner_client.delete(f"/api/trips/{trip_id}")).status_code == 204

    settles = await jobs_of(api_factory, OFFLINE_SETTLE)
    assert ids[1] in {job.payload["episode_id"] for job in settles}
    async with api_factory() as session:
        settled = await settle_episode(session, settings, ids[1], now=datetime.now(UTC))
        await session.commit()
    assert settled.outcome == "copy_deleted"
    assert not offline_path_for(settings, ids[1]).exists()


async def test_an_expired_trip_lets_its_copies_go(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987023, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    later = datetime.now(UTC) + timedelta(days=30)

    # Episode 3 runs out its clock, the deadline has passed, the trip ends —
    # expired, since not every row was delivered — and the sweep's safety net
    # queues the delivered episode's settle in the same pass.
    async with api_factory() as session:
        swept = await sweep_trips(session, now=later)
        await session.commit()
    assert swept.ended == {trip_id: TripState.EXPIRED}
    assert ids[1] in swept.settles
    async with api_factory() as session:
        settled = await settle_episode(session, settings, ids[1], now=later)
        await session.commit()
    assert settled.outcome == "copy_deleted"


# --- A trip ends when all of it is watched (M20.4 F, owner 2026-10-31) -------------


def test_a_trip_is_complete_when_every_row_is_watched() -> None:
    assert trip_complete(rows=3, watched=3)
    assert not trip_complete(rows=3, watched=2)
    assert not trip_complete(rows=0, watched=0), "an empty trip waits for its deadline"


async def trip_of(factory: SessionFactory, trip_id: int) -> Trip:
    async with factory() as session:
        trip = await session.get(Trip, trip_id)
        assert trip is not None
        return trip


async def test_the_sweep_finishes_a_trip_once_every_episode_is_watched(
    owner: User,
    api_factory: SessionFactory,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987040, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    await deliver(api_factory, trip_id, ids[2], ago=timedelta(hours=3), released=True)
    await watch(api_factory, owner.id, ids[1])
    await watch(api_factory, owner.id, ids[2])
    at = datetime.now(UTC)

    with caplog.at_level(logging.INFO, logger="arc.services.trips.sweep"):
        async with api_factory() as session:
            swept = await sweep_trips(session, now=at)
            await session.commit()

    assert swept.ended == {trip_id: TripState.FINISHED}, "well before its deadline"
    trip = await trip_of(api_factory, trip_id)
    assert trip.state is TripState.FINISHED and trip.ended_at == at
    assert set(ids[1:]) <= set(swept.settles)
    ended = [r for r in caplog.records if getattr(r, "reason", None) == ALL_WATCHED]
    assert [r.__dict__["trip_id"] for r in ended] == [trip_id]
    assert {job.payload["episode_id"] for job in await jobs_of(api_factory, OFFLINE_SETTLE)} >= set(
        ids[1:]
    )


async def test_a_delivered_trip_with_one_episode_unwatched_stays_active(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987041, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    await deliver(api_factory, trip_id, ids[2], ago=timedelta(hours=3))
    await watch(api_factory, owner.id, ids[1])

    async with api_factory() as session:
        swept = await sweep_trips(session, now=datetime.now(UTC))
        await session.commit()

    assert swept.ended == {}
    assert (await trip_of(api_factory, trip_id)).state is TripState.ACTIVE
    assert ids[2] not in swept.settles, "the unwatched copy is still held"


async def test_a_pending_episode_watched_online_counts_and_its_row_expires(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987042, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    # Episode 3 never reached the device; its user watched it online.
    await watch(api_factory, owner.id, ids[1])
    await watch(api_factory, owner.id, ids[2])

    async with api_factory() as session:
        swept = await sweep_trips(session, now=datetime.now(UTC))
        await session.commit()

    assert swept.ended == {trip_id: TripState.FINISHED}
    assert (trip_id, ids[2]) in swept.expired_rows
    assert (await row_state(api_factory, trip_id, ids[2])).state is TripEpisodeState.EXPIRED
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.DELIVERED
    # The ended trip no longer waits on or holds either copy: both are settled.
    assert set(ids[1:]) <= set(swept.settles)
    async with api_factory() as session:
        settled = await settle_episode(
            session, settings, ids[2], now=datetime.now(UTC) + timedelta(hours=2)
        )
        await session.commit()
    assert settled.outcome == "copy_deleted"


async def test_another_users_watching_does_not_end_the_trip(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987043, device_id=IPAD)
    other = await add_user(api_factory, "verified-other@arc.test", "verified-other-password")
    for episode_id in ids[1:]:
        await deliver(api_factory, trip_id, episode_id, ago=timedelta(hours=3))
        await watch(api_factory, other.id, episode_id)

    async with api_factory() as session:
        swept = await sweep_trips(session, now=datetime.now(UTC))
        await session.commit()

    assert swept.ended == {}


async def test_the_deadline_still_ends_an_unwatched_trip_as_before(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    # Every row delivered, one episode unwatched, the deadline passed: the
    # 2026-10-30 rule, unchanged — ``finished`` because every row was delivered.
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987044, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    await deliver(api_factory, trip_id, ids[2], ago=timedelta(hours=3))
    await watch(api_factory, owner.id, ids[1])
    later = datetime.now(UTC) + timedelta(days=30)

    async with api_factory() as session:
        swept = await sweep_trips(session, now=later)
        await session.commit()

    assert swept.ended == {trip_id: TripState.FINISHED}
    assert swept.expired_rows == []


async def complete(factory: SessionFactory, user_id: int, episode_id: int) -> None:
    async with factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        outcome = await record_progress(
            session,
            user_id=user_id,
            episode=episode,
            position_s=1400.0,
            duration_s=1420.0,
            force_complete=True,
        )
        await session.commit()
    assert outcome.newly_completed


async def test_a_completion_on_a_trip_episode_queues_one_sweep(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987045, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    await deliver(api_factory, trip_id, ids[2], ago=timedelta(hours=3))

    # Episode 1 is not on the trip: nothing queued.
    await complete(api_factory, owner.id, ids[0])
    assert await jobs_of(api_factory, TRIP_SWEEP) == []

    await complete(api_factory, owner.id, ids[1])
    assert len(await jobs_of(api_factory, TRIP_SWEEP)) == 1
    await complete(api_factory, owner.id, ids[2])
    assert len(await jobs_of(api_factory, TRIP_SWEEP)) == 1, "deduplicated"

    # The queued sweep is what ends it.
    async with api_factory() as session:
        swept = await sweep_trips(session, now=datetime.now(UTC))
        await session.commit()
    assert swept.ended == {trip_id: TripState.FINISHED}


async def test_a_completion_off_any_active_trip_queues_no_sweep(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987046, device_id=IPAD)
    other = await add_user(api_factory, "verified-other2@arc.test", "verified-other2-password")

    # Another user finishing the trip's episode: not their trip.
    await complete(api_factory, other.id, ids[1])
    assert await jobs_of(api_factory, TRIP_SWEEP) == []

    # The owner, once the trip has ended (cancelled here).
    await set_trip_state(api_factory, trip_id, TripState.CANCELLED)
    await complete(api_factory, owner.id, ids[2])
    assert await jobs_of(api_factory, TRIP_SWEEP) == []


async def test_the_next_trip_ends_a_fully_watched_trip_with_a_row_pending(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(
        api_factory, settings, owner, anilist_id=987047, episodes=4, device_id=IPAD
    )
    # The trip is episodes 2 and 3; episode 4 is left for the next one.
    async with api_factory() as session:
        row = await session.get(TripEpisode, (trip_id, ids[3]))
        assert row is not None
        await session.delete(row)
        await session.commit()
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    await watch(api_factory, owner.id, ids[1])
    await watch(api_factory, owner.id, ids[2])  # online; its row is still pending

    async with api_factory() as session:
        user = await session.get(User, owner.id)
        old = await session.get(Trip, trip_id)
        assert user is not None and old is not None
        made = await create_trip(
            session,
            settings,
            user=user,
            anime_id=old.anime_id,
            count=1,
            now=datetime.now(UTC),
            device_id=IPAD,
        )
        await session.commit()
        new_id = made.trip.id

    old = await trip_of(api_factory, trip_id)
    assert old.state is TripState.FINISHED and old.ended_at is not None
    assert (await row_state(api_factory, trip_id, ids[2])).state is TripEpisodeState.EXPIRED
    assert (await trip_of(api_factory, new_id)).state is TripState.ACTIVE
    settles = {job.payload["episode_id"] for job in await jobs_of(api_factory, OFFLINE_SETTLE)}
    assert set(ids[1:3]) <= settles


async def sweep_jobs(factory: SessionFactory) -> list[Job]:
    async with factory() as session:
        return list(
            (
                await session.scalars(select(Job).where(Job.type == TRIP_SWEEP).order_by(Job.id))
            ).all()
        )


async def test_a_completion_during_a_running_sweep_queues_one_follow_up(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987048, device_id=IPAD)
    async with api_factory() as session:
        running = await enqueue(
            session, TRIP_SWEEP, priority=TRIP_SWEEP_PRIORITY, dedupe_key=TRIP_SWEEP
        )
        running.status = JobStatus.RUNNING
        await session.commit()
        running_id = running.id

    await complete(api_factory, owner.id, ids[1])
    jobs = await sweep_jobs(api_factory)
    assert [(job.id == running_id, job.status) for job in jobs] == [
        (True, JobStatus.RUNNING),
        (False, JobStatus.PENDING),
    ]

    await complete(api_factory, owner.id, ids[2])
    assert len(await sweep_jobs(api_factory)) == 2, "the pending follow-up is reused"


async def test_a_completion_with_a_sweep_pending_adds_none(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987049, device_id=IPAD)
    async with api_factory() as session:
        await enqueue(session, TRIP_SWEEP, priority=TRIP_SWEEP_PRIORITY, dedupe_key=TRIP_SWEEP)
        await session.commit()

    await complete(api_factory, owner.id, ids[1])
    assert [job.status for job in await sweep_jobs(api_factory)] == [JobStatus.PENDING]


async def test_an_un_mark_before_the_sweep_keeps_the_trip(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987050, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(hours=3))
    await deliver(api_factory, trip_id, ids[2], ago=timedelta(hours=3))
    await complete(api_factory, owner.id, ids[1])
    await complete(api_factory, owner.id, ids[2])  # a mistaken mark of the last one
    async with api_factory() as session:
        episode = await session.get(Episode, ids[2])
        assert episode is not None
        await unmark_watched(session, user_id=owner.id, episode=episode)
        await session.commit()

    async with api_factory() as session:
        swept = await sweep_trips(session, now=datetime.now(UTC))
        await session.commit()

    assert swept.ended == {}
    assert (await trip_of(api_factory, trip_id)).state is TripState.ACTIVE


async def test_a_second_sweep_leaves_a_finished_trip_alone(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987051, device_id=IPAD)
    for episode_id in ids[1:]:
        await deliver(api_factory, trip_id, episode_id, ago=timedelta(hours=3))
        await watch(api_factory, owner.id, episode_id)
    async with api_factory() as session:
        first = await sweep_trips(session, now=datetime.now(UTC))
        await session.commit()
    ended_at = (await trip_of(api_factory, trip_id)).ended_at

    async with api_factory() as session:
        second = await sweep_trips(session, now=datetime.now(UTC) + timedelta(hours=1))
        await session.commit()

    assert first.ended == {trip_id: TripState.FINISHED}
    assert second.ended == {} and second.expired_rows == []
    trip = await trip_of(api_factory, trip_id)
    assert trip.state is TripState.FINISHED and trip.ended_at == ended_at


# --- The device's report ------------------------------------------------------------


async def test_a_report_is_stored_pruned_and_printed(
    owner_client: AsyncClient,
    owner: User,
    api_factory: SessionFactory,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = [
        {"at": "2026-10-30T03:30:00Z", "kind": "confirmed", "episode_id": 15},
        {"at": "2026-10-30T04:40:00+00:00", "kind": "missing", "detail": {"why": "NotFound"}},
    ]
    for _ in range(MAX_REPORTS_PER_DEVICE + 1):
        response = await owner_client.post(report_url(), json={"events": events})
        assert response.status_code == 204, response.text

    async with api_factory() as session:
        count = await session.scalar(select(func.count()).select_from(DeviceReport))
        newest = await session.scalar(select(DeviceReport).order_by(DeviceReport.id.desc()))
    assert count == MAX_REPORTS_PER_DEVICE
    assert newest is not None and newest.user_id == owner.id and newest.device_id == IPAD
    assert newest.events[0] == {
        "at": "2026-10-30T03:30:00Z",
        "kind": "confirmed",
        "episode_id": 15,
    }
    lines = format_report(newest)
    assert "confirmed" in lines[1] and "episode 15" in lines[1]
    assert '{"why": "NotFound"}' in lines[2]

    async with api_factory() as session:
        args = SimpleNamespace(device=IPAD.upper(), last=2)
        assert await cmd_device_report(session, settings, args) == 0  # type: ignore[arg-type]
        out = capsys.readouterr().out
        assert out.count(f"from device {IPAD}") == 2
        assert "missing" in out and "NotFound" in out
        none = SimpleNamespace(device=MAC, last=1)
        assert await cmd_device_report(session, settings, none) == 0  # type: ignore[arg-type]
        assert "no report" in capsys.readouterr().out
        bad = SimpleNamespace(device="not-a-uuid", last=1)
        assert await cmd_device_report(session, settings, bad) == EXIT_FAILED  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"events": [{"kind": "x"}]},
        {"events": [{"at": "not a time", "kind": "x"}]},
        {"events": [{"at": "2026-10-30T03:30:00Z", "kind": ""}]},
        {"events": [{"at": "2026-10-30T03:30:00Z", "kind": "x", "extra": 1}]},
        {"events": [{"at": "2026-10-30T03:30:00Z", "kind": "x"}] * 501},
    ],
)
async def test_a_malformed_report_is_422(
    body: dict[str, object], owner_client: AsyncClient
) -> None:
    assert (await owner_client.post(report_url(), json=body)).status_code == 422


async def test_a_report_over_256_kib_is_413(owner_client: AsyncClient) -> None:
    detail = {"blob": "x" * (300 * 1024)}
    body = {"events": [{"at": "2026-10-30T03:30:00Z", "kind": "big", "detail": detail}]}
    assert (await owner_client.post(report_url(), json=body)).status_code == 413


async def test_the_demo_account_sends_no_report(demo_client: AsyncClient) -> None:
    body = {"events": [{"at": "2026-10-30T03:30:00Z", "kind": "x"}]}
    assert (await demo_client.post(report_url(), json=body)).status_code == 403


# --- Fix loop (review of E2, 2026-10-30) ---------------------------------------


async def test_an_old_shape_report_never_re_pends(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987030, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1])

    response = await owner_client.put(holdings_url(), json={"episode_ids": []})

    assert response.status_code == 204
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.DELIVERED


async def test_a_full_entry_is_held_without_its_detail(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987031, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1])
    entry = {"episode_id": ids[1], "etag": '"src"', "size": 9, "sha256": "ab" * 32}

    response = await owner_client.put(
        holdings_url(),
        json={"episodes": [{**entry, "variant": "full"}, {**entry, "variant": "full"}]},
    )

    assert response.status_code == 204
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.DELIVERED
    async with api_factory() as session:
        (row,) = (await session.scalars(select(DeviceHolding))).all()
    assert (row.episode_id, row.etag, row.size, row.sha256) == (ids[1], None, None, None)


async def test_a_full_confirmation_with_the_sources_etag_is_delivered(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987032, device_id=IPAD)
    await hash_copy(api_factory, settings, ids[1])
    body = {"device_id": IPAD, "etag": '"the-source"', "variant": "full", "size": 1}

    response = await owner_client.post(delivered_url(trip_id, ids[1]), json=body)

    assert response.status_code == 204
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.DELIVERED
    # The same ETag on a small confirmation is a copy from before a re-make.
    small = {**body, "variant": "small", "size": None}
    assert (await owner_client.post(delivered_url(trip_id, ids[2]), json=small)).status_code == 204
    assert (await row_state(api_factory, trip_id, ids[2])).state is TripEpisodeState.PENDING


async def test_a_null_hash_is_unknown_never_a_mismatch(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987033, device_id=IPAD)
    copy = await hash_copy(api_factory, settings, ids[1])

    response = await owner_client.post(
        delivered_url(trip_id, ids[1]),
        json={"device_id": IPAD, "sha256": None, "size": copy.size},
    )

    assert response.status_code == 204
    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.DELIVERED


async def a_source(factory: SessionFactory, settings: Settings, episode_id: int) -> Path:
    """A linked source file for one episode, as a download leaves it."""
    path = write_source_file(settings, episode_id)
    async with factory() as session:
        session.add(MediaFile(episode_id=episode_id, path=str(path), size=path.stat().st_size))
        await session.commit()
    return path


async def set_episode(factory: SessionFactory, episode_id: int, state: EpisodeState) -> None:
    async with factory() as session:
        episode = await session.get(Episode, episode_id)
        assert episode is not None
        episode.state = state
        await session.commit()


async def test_a_held_copy_does_not_pin_its_source(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987034, device_id=IPAD)
    source = await a_source(api_factory, settings, ids[1])
    await set_episode(api_factory, ids[1], EpisodeState.MATCHED)
    await deliver(api_factory, trip_id, ids[1], ago=timedelta(minutes=1))

    async with api_factory() as session:
        released = await release_episode(session, settings, ids[1])
        await session.commit()

    assert released.outcome == "source_deleted"
    assert not source.exists()
    assert offline_path_for(settings, ids[1]).exists()
    async with api_factory() as session:
        copy = await session.get(OfflineCopy, ids[1])
        assert copy is not None and copy.state is OfflineCopyState.READY
        episode = await session.get(Episode, ids[1])
        assert episode is not None and episode.state is EpisodeState.NOT_WANTED


async def test_the_settle_hands_a_held_copys_source_to_the_release(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987035, device_id=IPAD)
    await a_source(api_factory, settings, ids[1])
    await set_episode(api_factory, ids[1], EpisodeState.MATCHED)
    await deliver(api_factory, trip_id, ids[1])

    async with api_factory() as session:
        kept = await settle_episode(session, settings, ids[1], now=datetime.now(UTC))
        await session.commit()

    assert kept.outcome == "kept"
    releases = await jobs_of(api_factory, TRIP_RELEASE)
    assert [job.payload["episode_id"] for job in releases] == [ids[1]]


async def a_ready_held_episode(
    factory: SessionFactory, settings: Settings, owner: User, *, anilist_id: int
) -> tuple[int, list[int], Path]:
    """Episode 2 ``ready`` with a source and a month-old copy its trip's device holds."""
    trip_id, ids = await a_trip(factory, settings, owner, anilist_id=anilist_id, device_id=IPAD)
    source = await a_source(factory, settings, ids[1])
    await set_episode(factory, ids[1], EpisodeState.READY)
    async with factory() as session:
        copy = await session.get(OfflineCopy, ids[1])
        assert copy is not None
        copy.ready_at = datetime.now(UTC) - timedelta(days=30)
        await session.commit()
    await deliver(factory, trip_id, ids[1])
    return trip_id, ids, source


async def test_the_idle_rule_spares_a_held_copy(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids, _ = await a_ready_held_episode(api_factory, settings, owner, anilist_id=987036)
    at = datetime.now(UTC)

    async with api_factory() as session:
        held = await idle_copies(session, settings, now=at, idle=timedelta(days=7))
    assert ids[1] not in {item.episode_id for item in held}

    await watch(api_factory, owner.id, ids[1])
    async with api_factory() as session:
        watched = await idle_copies(session, settings, now=at, idle=timedelta(days=7))
    assert ids[1] in {item.episode_id for item in watched}


async def test_retention_deletes_a_ready_episode_but_keeps_its_held_copy(
    owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids, source = await a_ready_held_episode(
        api_factory, settings, owner, anilist_id=987037
    )
    async with api_factory() as session:
        job = Job(type=DELETE_EPISODE_FILES, payload={"episode_id": ids[1]})
        session.add(job)
        await session.flush()
        ctx = JobContext(
            job=job, session=session, settings=settings, log=logging.getLogger("arc.test")
        )
        await delete_files(ctx)
        await session.commit()

    assert not source.exists()
    assert offline_path_for(settings, ids[1]).exists()
    async with api_factory() as session:
        assert await session.get(OfflineCopy, ids[1]) is not None
        episode = await session.get(Episode, ids[1])
        assert episode is not None and episode.state is EpisodeState.NOT_WANTED


async def test_a_report_declaring_too_much_is_refused_unread(owner_client: AsyncClient) -> None:
    response = await owner_client.post(
        report_url(),
        content=b"{}",
        headers={"content-type": "application/json", "content-length": str(300 * 1024)},
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "event",
    [
        {"at": "2026-10-30T03:30:00Z", "kind": "has space"},
        {"at": "2026-10-30T03:30:00Z", "kind": "k" * 65},
        {"at": "2026-10-30T03:30:00Z", "kind": "x", "detail": {"why": "a\u0000b"}},
        {"at": "2026-10-30T03:30:00Z", "kind": "x", "detail": {"a\u0000": 1}},
        {"at": "2026-10-30T03:30:00Z", "kind": "x", "detail": {"list": ["\u0000"]}},
    ],
)
async def test_a_report_event_that_cannot_be_stored_is_422(
    event: dict[str, object], owner_client: AsyncClient
) -> None:
    response = await owner_client.post(report_url(), json={"events": [event]})
    assert response.status_code == 422


async def test_a_report_takes_a_naive_iso_time_and_machine_kinds(
    owner_client: AsyncClient, api_factory: SessionFactory
) -> None:
    event = {"at": "2026-10-30T03:30:00.123", "kind": "download.resume:ok-1"}
    assert (await owner_client.post(report_url(), json={"events": [event]})).status_code == 204
    async with api_factory() as session:
        report = await session.scalar(select(DeviceReport))
    assert report is not None and report.events[0]["kind"] == "download.resume:ok-1"


async def test_repeated_entries_do_not_count_towards_the_cap(
    owner_client: AsyncClient, owner: User, api_factory: SessionFactory, settings: Settings
) -> None:
    trip_id, ids = await a_trip(api_factory, settings, owner, anilist_id=987038, device_id=IPAD)
    await deliver(api_factory, trip_id, ids[1])

    body = {"episodes": [{"episode_id": ids[0]}] * MAX_HOLDINGS}
    assert (await owner_client.put(holdings_url(), json=body)).status_code == 204

    assert (await row_state(api_factory, trip_id, ids[1])).state is TripEpisodeState.PENDING
