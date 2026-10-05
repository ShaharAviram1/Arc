"""``POST /api/sync``: replaying what a device recorded offline (FR-S8).

Most of these are about what replay must **not** do: write to MyAnimeList a
second time, lower anything, let an old sample overwrite a newer one, or let
one bad record fail the rest. The MAL tests link the user so the write log is
actually written — an unlinked user would make "exactly one write" vacuous.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    MalWriteCause,
    Rendition,
    User,
    WatchProgress,
)
from arc.services.mal.names import PUSH
from arc.services.playback.sync import (
    MAX_AGE,
    MAX_BATCH,
    SyncItem,
    SyncKind,
    adjust_timestamp,
    completion_is_stale,
    completion_values,
    position_is_stale,
    unmark_is_stale,
)
from tests.conftest import add_user, api_transport, login
from tests.mal_helpers import (
    link_user,
    log_rows,
    mal_settings,  # noqa: F401  (installs the ``settings`` override: MAL configured)
)

pytestmark = pytest.mark.pg

USER_EMAIL = "sync@arc.test"
USER_PASSWORD = "sync-password"
DURATION = 1420.0
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr("arc.api.sync.now", lambda: NOW)
    monkeypatch.setattr("arc.api.playback.now", lambda: NOW)
    return NOW


@pytest.fixture
async def user(api_factory: SessionFactory) -> User:
    return await add_user(api_factory, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def client(api_app: FastAPI, user: User) -> AsyncIterator[AsyncClient]:
    async with api_transport(api_app) as http:
        yield await login(http, USER_EMAIL, USER_PASSWORD)


async def add_show(
    factory: SessionFactory, *, anilist_id: int, count: int = 12, ready: tuple[int, ...] = (11,)
) -> tuple[int, dict[int, int]]:
    async with factory() as session:
        anime = Anime(
            anilist_id=anilist_id,
            summary_source="anilist",
            detail_source="anilist",
            title_romaji="Sync Show",
            format="TV",
            status="RELEASING",
            episodes=count,
        )
        session.add(anime)
        await session.flush()
        ids: dict[int, int] = {}
        for number in range(1, count + 1):
            episode = Episode(
                anime_id=anime.id,
                number=number,
                air_at=NOW - timedelta(weeks=count - number),
                state=EpisodeState.READY if number in ready else EpisodeState.WANTED,
            )
            session.add(episode)
            await session.flush()
            ids[number] = episode.id
            if number in ready:
                session.add(
                    Rendition(
                        episode_id=episode.id,
                        dir=f"/data/renditions/{episode.id}",
                        playlist_path=f"/data/renditions/{episode.id}/index.m3u8",
                        duration=DURATION,
                        ready_at=NOW,
                    )
                )
        await session.commit()
        return anime.id, ids


async def follow(factory: SessionFactory, user: User, anime_id: int, *, progress: int) -> None:
    async with factory() as session:
        session.add(
            ListEntry(
                user_id=user.id, anime_id=anime_id, status=ListStatus.WATCHING, progress=progress
            )
        )
        await session.commit()


async def set_row(
    factory: SessionFactory,
    user: User,
    episode_id: int,
    *,
    position_s: float,
    updated_at: datetime,
    completed: bool = False,
    completed_at: datetime | None = None,
) -> None:
    async with factory() as session:
        session.add(
            WatchProgress(
                user_id=user.id,
                episode_id=episode_id,
                position_s=position_s,
                duration_s=DURATION,
                completed=completed,
                completed_at=completed_at,
                updated_at=updated_at,
            )
        )
        await session.commit()


async def row_of(factory: SessionFactory, user: User, episode_id: int) -> WatchProgress | None:
    async with factory() as session:
        return await session.get(WatchProgress, (user.id, episode_id))


async def entry_of(factory: SessionFactory, user: User, anime_id: int) -> ListEntry | None:
    async with factory() as session:
        return await session.get(ListEntry, (user.id, anime_id))


async def push_jobs(factory: SessionFactory) -> list[Job]:
    async with factory() as session:
        return list((await session.scalars(select(Job).where(Job.type == PUSH))).all())


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def item(
    kind: str,
    episode_id: int,
    at: datetime,
    *,
    position_s: float | None = None,
    duration_s: float | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_id": client_id or str(uuid.uuid4()),
        "kind": kind,
        "episode_id": episode_id,
        "at": iso(at),
    }
    if position_s is not None:
        body["position_s"] = position_s
    if duration_s is not None:
        body["duration_s"] = duration_s
    return body


def envelope(user_id: int, items: list[Any], *, sent_at: datetime = NOW) -> dict[str, Any]:
    """The batch body; ``sent_at`` defaults to the pinned server clock (no skew)."""
    return {"user_id": user_id, "sent_at": iso(sent_at), "items": items}


async def sync(client: AsyncClient, user: User, items: list[Any]) -> list[dict[str, Any]]:
    response = await client.post("/api/sync", json=envelope(user.id, items))
    assert response.status_code == 200, response.text
    results: list[dict[str, Any]] = response.json()["results"]
    return results


# --- pure rules ----------------------------------------------------------------


def test_timestamps_are_shifted_by_the_skew_and_clamped_into_the_window() -> None:
    # A future time is now; a naive one is UTC.
    assert adjust_timestamp(NOW + timedelta(days=1), sent_at=None, now=NOW) == NOW
    assert adjust_timestamp(datetime(2026, 11, 4, 11, 0), sent_at=None, now=NOW) == datetime(
        2026, 11, 4, 11, 0, tzinfo=UTC
    )
    # 1971 is a broken clock: read as the oldest moment Arc believes.
    assert adjust_timestamp(datetime(1971, 1, 1, tzinfo=UTC), sent_at=None, now=NOW) == (
        NOW - MAX_AGE
    )
    # A device an hour slow: its "five minutes before sending" is five minutes ago.
    slow = NOW - timedelta(hours=1)
    assert adjust_timestamp(slow - timedelta(minutes=5), sent_at=slow, now=NOW) == (
        NOW - timedelta(minutes=5)
    )
    # A device a day fast is pulled back the same way, not merely clamped.
    fast = NOW + timedelta(days=1)
    assert adjust_timestamp(fast - timedelta(hours=2), sent_at=fast, now=NOW) == (
        NOW - timedelta(hours=2)
    )


def test_staleness_rules() -> None:
    assert position_is_stale(NOW, NOW) is True
    assert position_is_stale(NOW, NOW + timedelta(seconds=1)) is False
    assert position_is_stale(None, NOW) is False
    assert completion_is_stale(NOW, NOW) is True
    assert completion_is_stale(NOW, NOW + timedelta(seconds=1)) is False
    assert completion_is_stale(None, NOW) is False
    assert unmark_is_stale(NOW, None, NOW - timedelta(seconds=1)) is True
    assert unmark_is_stale(NOW, None, NOW) is False
    assert unmark_is_stale(None, NOW, NOW - timedelta(seconds=1)) is True
    assert unmark_is_stale(None, None, NOW) is False


def test_a_completion_keeps_a_newer_resume_point() -> None:
    completion = SyncItem("c", SyncKind.COMPLETION, 1, NOW, position_s=1300.0, duration_s=DURATION)
    assert completion_values(
        item=completion,
        row_position=200.0,
        row_duration=DURATION,
        row_updated_at=NOW + timedelta(minutes=1),
        rendition_duration=DURATION,
    ) == (200.0, DURATION)
    assert completion_values(
        item=completion,
        row_position=200.0,
        row_duration=DURATION,
        row_updated_at=NOW - timedelta(minutes=1),
        rendition_duration=DURATION,
    ) == (1300.0, DURATION)
    bare = SyncItem("m", SyncKind.COMPLETION, 1, NOW)
    assert completion_values(
        item=bare,
        row_position=None,
        row_duration=None,
        row_updated_at=None,
        rendition_duration=DURATION,
    ) == (DURATION, DURATION)


# --- the endpoint --------------------------------------------------------------


async def test_a_batch_answers_each_item_by_its_client_id(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    anime_id, ids = await add_show(api_factory, anilist_id=960001)
    await follow(api_factory, user, anime_id, progress=10)
    t = NOW - timedelta(hours=2)

    results = await sync(
        client,
        user,
        [
            item("position", ids[11], t, position_s=300.0, duration_s=DURATION, client_id="a"),
            item(
                "completion",
                ids[11],
                t + timedelta(minutes=20),
                position_s=1290.0,
                duration_s=DURATION,
                client_id="b",
            ),
            # The viewer rewound after finishing, still offline.
            item(
                "position",
                ids[11],
                t + timedelta(minutes=25),
                position_s=60.0,
                duration_s=DURATION,
                client_id="c",
            ),
        ],
    )

    assert results == [
        {"client_id": "a", "status": "applied", "reason": None},
        {"client_id": "b", "status": "applied", "reason": None},
        {"client_id": "c", "status": "applied", "reason": None},
    ]
    row = await row_of(api_factory, user, ids[11])
    assert row is not None
    # The completion survived the later, lower sample, and the resume point is
    # the rewind.
    assert row.completed is True
    assert row.position_s == 60.0
    assert row.updated_at == t + timedelta(minutes=25)
    assert row.completed_at == t + timedelta(minutes=20)
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 11


async def test_a_replayed_completion_raises_progress_once_with_one_logged_write(
    client: AsyncClient, api_factory: SessionFactory, user: User, settings: Settings
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960002)
    await follow(api_factory, user, anime_id, progress=10)
    completion = item(
        "completion", ids[11], NOW - timedelta(hours=1), position_s=1300.0, duration_s=DURATION
    )

    first = await sync(client, user, [completion])
    # The same record again (a flush whose answer was lost), and a fresh
    # completion of the same episode (a second device): neither changes anything.
    second = await sync(client, user, [completion])
    third = await sync(client, user, [item("completion", ids[11], NOW - timedelta(minutes=5))])

    assert [r["status"] for r in first + second + third] == ["applied"] * 3
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 11 and entry.mal_dirty is True
    logged = await log_rows(api_factory, user.id)
    assert len(logged) == 1
    assert logged[0].field == "progress"
    assert logged[0].old_value == 10 and logged[0].new_value == 11
    assert logged[0].cause is MalWriteCause.WATCH
    assert len(await push_jobs(api_factory)) == 1


async def test_a_replay_of_what_the_online_path_already_did_is_a_no_op(
    client: AsyncClient, api_factory: SessionFactory, user: User, settings: Settings
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960003)
    await follow(api_factory, user, anime_id, progress=10)
    online = await client.post(f"/api/episodes/{ids[11]}/watched")
    assert online.status_code == 200

    before = await row_of(api_factory, user, ids[11])

    results = await sync(client, user, [item("completion", ids[11], NOW - timedelta(minutes=1))])

    assert results[0]["status"] == "applied"
    assert len(await log_rows(api_factory, user.id)) == 1
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 11
    after = await row_of(api_factory, user, ids[11])
    assert before is not None and after is not None
    assert after.completed is True
    assert after.completed_at == before.completed_at
    assert after.position_s == before.position_s
    assert len(await push_jobs(api_factory)) == 1


async def test_an_old_completion_never_lowers_a_higher_progress(
    client: AsyncClient, api_factory: SessionFactory, user: User, settings: Settings
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960004, ready=(3, 11))
    await follow(api_factory, user, anime_id, progress=10)

    results = await sync(
        client,
        user,
        [
            item(
                "completion",
                ids[3],
                NOW - timedelta(days=2),
                position_s=1300.0,
                duration_s=DURATION,
            )
        ],
    )

    assert results[0]["status"] == "applied"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 10 and entry.mal_dirty is False
    assert await log_rows(api_factory, user.id) == []
    row = await row_of(api_factory, user, ids[3])
    assert row is not None and row.completed is True


async def test_a_stale_position_does_not_overwrite_a_newer_one(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=960005)
    await set_row(api_factory, user, ids[11], position_s=900.0, updated_at=NOW - timedelta(hours=1))

    results = await sync(
        client,
        user,
        [
            item(
                "position", ids[11], NOW - timedelta(hours=3), position_s=100.0, duration_s=DURATION
            )
        ],
    )

    assert results[0]["status"] == "stale"
    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.position_s == 900.0
    assert row.updated_at == NOW - timedelta(hours=1)


async def test_a_completion_older_than_the_row_keeps_the_rows_position(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    anime_id, ids = await add_show(api_factory, anilist_id=960006)
    await follow(api_factory, user, anime_id, progress=10)
    await set_row(api_factory, user, ids[11], position_s=120.0, updated_at=NOW - timedelta(hours=1))

    await sync(
        client,
        user,
        [
            item(
                "completion",
                ids[11],
                NOW - timedelta(hours=3),
                position_s=1300.0,
                duration_s=DURATION,
            )
        ],
    )

    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.completed is True
    assert row.position_s == 120.0
    assert row.updated_at == NOW - timedelta(hours=1)
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 11


async def test_a_future_timestamp_is_clamped_to_now(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=960007)

    await sync(
        client,
        user,
        [item("position", ids[11], NOW + timedelta(days=30), position_s=50.0, duration_s=DURATION)],
    )

    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.updated_at == NOW


async def test_a_bad_item_is_rejected_without_failing_the_batch(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=960008)
    t = NOW - timedelta(minutes=10)

    results = await sync(
        client,
        user,
        [
            item("position", 987_654_321, t, position_s=1.0, duration_s=DURATION, client_id="gone"),
            {"client_id": "junk", "kind": "teleport", "episode_id": ids[11], "at": iso(t)},
            item("position", ids[11], t, client_id="no-numbers"),
            {"kind": "position"},
            item("position", ids[11], t, position_s=30.0, duration_s=DURATION, client_id="good"),
        ],
    )

    assert results == [
        {"client_id": "gone", "status": "rejected", "reason": "episode no longer exists"},
        {"client_id": "junk", "status": "rejected", "reason": "not a valid record"},
        {"client_id": "no-numbers", "status": "rejected", "reason": "not a valid record"},
        {"client_id": None, "status": "rejected", "reason": "not a valid record"},
        {"client_id": "good", "status": "applied", "reason": None},
    ]
    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.position_s == 30.0


async def test_more_than_the_batch_limit_is_refused(client: AsyncClient, user: User) -> None:
    items = [item("unmark", 1, NOW) for _ in range(MAX_BATCH + 1)]
    response = await client.post("/api/sync", json=envelope(user.id, items))
    assert response.status_code == 422


async def test_records_for_another_account_are_refused_whole(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=960009)
    response = await client.post(
        "/api/sync",
        json=envelope(
            user.id + 1000,
            [item("position", ids[11], NOW, position_s=30.0, duration_s=DURATION)],
        ),
    )
    assert response.status_code == 409
    assert await row_of(api_factory, user, ids[11]) is None


async def test_sync_needs_a_session(api_app: FastAPI) -> None:
    async with api_transport(api_app) as anon:
        response = await anon.post("/api/sync", json=envelope(1, []))
    assert response.status_code == 401


async def test_sync_without_an_origin_is_refused(
    api_app: FastAPI, api_factory: SessionFactory
) -> None:
    created = await add_user(api_factory, "sync-originless@arc.test", "originless-password")
    async with api_transport(api_app, origin=None) as http:
        await http.post(
            "/api/auth/login",
            json={"email": "sync-originless@arc.test", "password": "originless-password"},
            headers={"Origin": "http://localhost:5173"},
        )
        response = await http.post("/api/sync", json=envelope(created.id, []))
    assert response.status_code == 403


async def test_an_unmark_replay_lowers_the_latest_episode_with_a_manual_write(
    client: AsyncClient, api_factory: SessionFactory, user: User, settings: Settings
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960010)
    await follow(api_factory, user, anime_id, progress=11)
    await set_row(
        api_factory,
        user,
        ids[11],
        position_s=1300.0,
        updated_at=NOW - timedelta(hours=5),
        completed=True,
        completed_at=NOW - timedelta(hours=5),
    )

    results = await sync(client, user, [item("unmark", ids[11], NOW - timedelta(hours=1))])

    assert results[0]["status"] == "applied"
    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.completed is False and row.position_s == 1300.0
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 10
    logged = await log_rows(api_factory, user.id)
    assert [(r.old_value, r.new_value, r.cause) for r in logged] == [(11, 10, MalWriteCause.MANUAL)]


async def test_an_unmark_of_anything_but_the_latest_leaves_the_list_alone(
    client: AsyncClient, api_factory: SessionFactory, user: User, settings: Settings
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960011, ready=(4,))
    await follow(api_factory, user, anime_id, progress=9)

    results = await sync(client, user, [item("unmark", ids[4], NOW - timedelta(hours=1))])

    assert results[0]["status"] == "applied"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 9
    assert await log_rows(api_factory, user.id) == []


async def test_an_unmark_older_than_a_later_completion_is_stale(
    client: AsyncClient, api_factory: SessionFactory, user: User, settings: Settings
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960012)
    await follow(api_factory, user, anime_id, progress=11)
    await set_row(
        api_factory,
        user,
        ids[11],
        position_s=1300.0,
        updated_at=NOW - timedelta(hours=1),
        completed=True,
        completed_at=NOW - timedelta(hours=1),
    )

    results = await sync(client, user, [item("unmark", ids[11], NOW - timedelta(hours=2))])

    assert results[0]["status"] == "stale"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 11
    assert await log_rows(api_factory, user.id) == []


async def test_complete_unmark_then_rewatch_offline_replays_in_order(
    client: AsyncClient, api_factory: SessionFactory, user: User, settings: Settings
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960013)
    await follow(api_factory, user, anime_id, progress=10)
    t = NOW - timedelta(hours=3)

    results = await sync(
        client,
        user,
        [
            item("completion", ids[11], t, position_s=1290.0, duration_s=DURATION),
            item("unmark", ids[11], t + timedelta(minutes=1)),
            item(
                "position", ids[11], t + timedelta(minutes=2), position_s=400.0, duration_s=DURATION
            ),
        ],
    )

    assert [r["status"] for r in results] == ["applied", "applied", "applied"]
    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.completed is False and row.position_s == 400.0
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 10
    logged = await log_rows(api_factory, user.id)
    assert [(r.old_value, r.new_value, r.cause) for r in logged] == [
        (10, 11, MalWriteCause.WATCH),
        (11, 10, MalWriteCause.MANUAL),
    ]


async def test_an_offline_mark_with_no_numbers_writes_what_mark_watched_writes(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    anime_id, ids = await add_show(api_factory, anilist_id=960014, ready=(11,))

    await sync(
        client,
        user,
        [
            item("completion", ids[11], NOW - timedelta(minutes=3)),
            item("completion", ids[12], NOW - timedelta(minutes=2)),
        ],
    )

    eleven = await row_of(api_factory, user, ids[11])
    twelve = await row_of(api_factory, user, ids[12])
    assert eleven is not None and eleven.completed and eleven.position_s == DURATION
    assert twelve is not None and twelve.completed and twelve.position_s == 0.0
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 12


# --- review fixes (2026-10-05) ---------------------------------------------------


def at_clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    """Move the online endpoints' clock, so an online act happens at ``when``."""
    monkeypatch.setattr("arc.api.playback.now", lambda: when)


async def test_an_exit_sample_past_the_mark_does_not_undo_a_later_unmark(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: finish online, leave at 93 %, un-mark on the show page, then the queue flushes.

    The exit report is queued as a *position* (never a completion of unknown
    fate), and the un-mark is newer than it, so it is stale: the list stays
    lowered and nobody causes a third MAL write.
    """
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960101)
    await follow(api_factory, user, anime_id, progress=10)
    finished = NOW - timedelta(hours=2)

    at_clock(monkeypatch, finished)
    done = await client.post(
        "/api/progress",
        json={"episode_id": ids[11], "position_s": DURATION * 0.92, "duration_s": DURATION},
    )
    assert done.json()["newly_completed"] is True
    at_clock(monkeypatch, NOW - timedelta(hours=1))
    assert (await client.delete(f"/api/episodes/{ids[11]}/watched")).status_code == 200

    results = await sync(
        client,
        user,
        [
            item(
                "position",
                ids[11],
                finished + timedelta(minutes=1),
                position_s=DURATION * 0.93,
                duration_s=DURATION,
            )
        ],
    )

    assert results[0]["status"] == "stale"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 10
    logged = await log_rows(api_factory, user.id)
    assert [(r.old_value, r.new_value, r.cause) for r in logged] == [
        (10, 11, MalWriteCause.WATCH),
        (11, 10, MalWriteCause.MANUAL),
    ]


async def test_an_offline_rewatch_older_than_a_later_unmark_is_stale(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: rewatch offline at T0, un-mark online at T1 > T0, replay → stale; T2 > T1 applies."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960102)
    await follow(api_factory, user, anime_id, progress=11)
    await set_row(
        api_factory,
        user,
        ids[11],
        position_s=1300.0,
        updated_at=NOW - timedelta(hours=5),
        completed=True,
        completed_at=NOW - timedelta(hours=5),
    )
    t0, t1, t2 = NOW - timedelta(hours=2), NOW - timedelta(hours=1), NOW - timedelta(minutes=30)

    at_clock(monkeypatch, t1)
    assert (await client.delete(f"/api/episodes/{ids[11]}/watched")).status_code == 200

    stale = await sync(
        client, user, [item("completion", ids[11], t0, position_s=1300.0, duration_s=DURATION)]
    )

    assert stale[0]["status"] == "stale"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 10
    assert len(await log_rows(api_factory, user.id)) == 1  # the un-mark's own

    newer = await sync(
        client, user, [item("completion", ids[11], t2, position_s=1310.0, duration_s=DURATION)]
    )

    assert newer[0]["status"] == "applied"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 11
    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.completed is True and row.unmarked_at == t1


async def test_a_retried_flush_after_a_lost_response_does_not_undo_an_unmark_in_between(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960103)
    await follow(api_factory, user, anime_id, progress=10)
    completion = item(
        "completion", ids[11], NOW - timedelta(hours=2), position_s=1300.0, duration_s=DURATION
    )

    assert (await sync(client, user, [completion]))[0]["status"] == "applied"
    # ...the answer is lost on the way back; the user un-marks on another device.
    at_clock(monkeypatch, NOW - timedelta(hours=1))
    assert (await client.delete(f"/api/episodes/{ids[11]}/watched")).status_code == 200
    resent = await sync(client, user, [completion])

    assert resent[0]["status"] == "stale"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 10
    logged = await log_rows(api_factory, user.id)
    assert [r.cause for r in logged] == [MalWriteCause.WATCH, MalWriteCause.MANUAL]


async def test_an_unmark_of_a_list_vouched_episode_records_itself_against_old_completions(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2 where no row exists: the un-mark leaves a not-completed row carrying ``unmarked_at``."""
    anime_id, ids = await add_show(api_factory, anilist_id=960104, ready=())
    await follow(api_factory, user, anime_id, progress=9)
    at_clock(monkeypatch, NOW - timedelta(hours=1))
    assert (await client.delete(f"/api/episodes/{ids[9]}/watched")).status_code == 200

    results = await sync(client, user, [item("completion", ids[9], NOW - timedelta(hours=2))])

    assert results[0]["status"] == "stale"
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 8
    row = await row_of(api_factory, user, ids[9])
    assert row is not None and row.completed is False
    assert row.unmarked_at == NOW - timedelta(hours=1)


async def test_an_item_that_fails_is_answered_retry_and_the_rest_stand(
    client: AsyncClient,
    api_factory: SessionFactory,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1: an exception inside an item's savepoint is not a verdict."""
    _anime_id, ids = await add_show(api_factory, anilist_id=960105, ready=(10, 11))
    import arc.services.playback.sync as sync_service

    real = sync_service.record_progress
    calls = {"n": 0}

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("deadlock detected")
        return await real(*args, **kwargs)

    monkeypatch.setattr(sync_service, "record_progress", flaky)
    t = NOW - timedelta(minutes=5)

    results = await sync(
        client,
        user,
        [
            item("position", ids[10], t, position_s=40.0, duration_s=DURATION, client_id="x"),
            item("position", ids[11], t, position_s=50.0, duration_s=DURATION, client_id="y"),
        ],
    )

    assert results[0]["status"] == "retry" and results[0]["client_id"] == "x"
    assert results[1]["status"] == "applied"
    assert await row_of(api_factory, user, ids[10]) is None
    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.position_s == 50.0


async def test_two_concurrent_identical_unmarks_lower_and_log_once(
    client: AsyncClient, api_factory: SessionFactory, user: User, settings: Settings
) -> None:
    """S4: the list entry is locked, so the second un-mark finds the number moved."""
    await link_user(api_factory, settings, user_id=user.id)
    anime_id, ids = await add_show(api_factory, anilist_id=960106, ready=())
    await follow(api_factory, user, anime_id, progress=9)

    responses = await asyncio.gather(
        *(
            client.post(
                "/api/sync",
                json=envelope(user.id, [item("unmark", ids[9], NOW - timedelta(minutes=1))]),
            )
            for _ in range(5)
        )
    )

    assert [r.status_code for r in responses] == [200] * 5
    entry = await entry_of(api_factory, user, anime_id)
    assert entry is not None and entry.progress == 8
    assert len(await log_rows(api_factory, user.id)) == 1


async def test_a_far_past_timestamp_is_read_as_thirty_days_ago(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    anime_id, ids = await add_show(api_factory, anilist_id=960107)
    await follow(api_factory, user, anime_id, progress=10)

    await sync(
        client,
        user,
        [
            item(
                "completion",
                ids[11],
                datetime(1971, 1, 1, tzinfo=UTC),
                position_s=1300.0,
                duration_s=DURATION,
            )
        ],
    )

    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.completed_at == NOW - MAX_AGE


async def test_a_skewed_device_is_judged_on_the_servers_clock(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """A device a day fast says "two hours ago" in its own time; that is two hours ago."""
    _anime_id, ids = await add_show(api_factory, anilist_id=960108)
    await set_row(api_factory, user, ids[11], position_s=900.0, updated_at=NOW - timedelta(hours=1))
    fast = NOW + timedelta(days=1)

    response = await client.post(
        "/api/sync",
        json=envelope(
            user.id,
            [
                item(
                    "position",
                    ids[11],
                    fast - timedelta(hours=2),
                    position_s=100.0,
                    duration_s=DURATION,
                )
            ],
            sent_at=fast,
        ),
    )

    # Two hours ago is older than the row's hour ago: stale, not "tomorrow wins".
    assert response.json()["results"][0]["status"] == "stale"
    row = await row_of(api_factory, user, ids[11])
    assert row is not None and row.position_s == 900.0


async def test_an_unmark_older_than_a_later_list_change_is_stale(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    """S7: the user raised the list after the un-mark was made offline."""
    anime_id, ids = await add_show(api_factory, anilist_id=960109)
    await follow(api_factory, user, anime_id, progress=11)
    async with api_factory() as session:
        entry = await session.get(ListEntry, (user.id, anime_id))
        assert entry is not None
        entry.updated_at = NOW - timedelta(minutes=30)
        await session.commit()

    results = await sync(client, user, [item("unmark", ids[11], NOW - timedelta(hours=1))])

    assert results[0]["status"] == "stale"
    lowered = await entry_of(api_factory, user, anime_id)
    assert lowered is not None and lowered.progress == 11


async def test_a_non_object_item_is_rejected_on_its_own(
    client: AsyncClient, api_factory: SessionFactory, user: User
) -> None:
    _anime_id, ids = await add_show(api_factory, anilist_id=960110)

    results = await sync(
        client,
        user,
        [5, "x", item("position", ids[11], NOW, position_s=1.0, duration_s=DURATION)],
    )

    assert [r["status"] for r in results] == ["rejected", "rejected", "applied"]
    assert results[0]["client_id"] is None
