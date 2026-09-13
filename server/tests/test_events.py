"""Live updates: publication on commit and the SSE fan-out (§5.9).

The publication half is tested against a **real** ``LISTEN``, because the one
property that matters — an event reaches a browser if and only if the write
that caused it committed — is Postgres' behaviour, not Arc's, and a mock would
be asserting that the code calls the function rather than that the guarantee
holds. So these tests open their own asyncpg connection, listen on the channel,
and watch what arrives.

The fan-out half goes through the app: an ASGI client opening
``GET /api/events`` and reading frames off the stream.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from arc.api.events import TOO_MANY_STREAMS
from arc.config import Settings
from arc.db import SessionFactory
from arc.main import _lifespan, create_app
from arc.models import Anime, Episode, EpisodeState
from arc.services.acquisition.states import transition
from arc.services.auth import COOKIE_NAME
from arc.services.catalog.cache import episodes_for
from arc.services.events import _STAGED as STAGED_KEY
from arc.services.events import (
    ART,
    CHANNEL,
    EPISODE_STATE,
    QUEUE_MAXSIZE,
    Event,
    EventBroker,
    art_event,
    asyncpg_dsn,
    publish,
)
from arc.services.tmdb.enrich import TmdbPayloads, apply_enrichment, plan_enrichment
from tests.acquisition_helpers import make_anime
from tests.tmdb_mock import frieren_credits, frieren_season, frieren_show

#: Every wait in this file. Long enough that a loaded machine does not flake,
#: short enough that a genuinely undelivered event fails rather than hangs.
WAIT = 5.0


def staged_count(session: AsyncSession) -> int:
    """How many events are waiting on this session's commit.

    A test-side reader of the private staging key rather than a helper in the
    module: nothing in the application needs to ask this question, and an
    exported accessor for it would be production surface that exists only
    because the tests were written. Almost every test below asserts on what a
    real ``LISTEN`` *received* instead; this is for the two that are about the
    staging itself.
    """
    staged: list[str] = session.sync_session.info.get(STAGED_KEY, [])
    return len(staged)


class _AnyTs:
    """Equal to any ISO timestamp, so a frame can be asserted whole."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and other.startswith("20")

    def __hash__(self) -> int:  # pragma: no cover - never used as a key
        return 0


ANY_TS = _AnyTs()


class Listener:
    """A raw ``LISTEN arc_events``, as a browser's stream would see it."""

    def __init__(self) -> None:
        self.payloads: asyncio.Queue[str] = asyncio.Queue()

    def _on_notify(self, *args: Any) -> None:
        self.payloads.put_nowait(args[-1])

    async def next(self, *, timeout: float = WAIT) -> dict[str, Any]:
        payload = await asyncio.wait_for(self.payloads.get(), timeout)
        parsed: dict[str, Any] = json.loads(payload)
        return parsed

    def pending(self) -> int:
        return self.payloads.qsize()


@pytest.fixture
async def listener(pg_engine: AsyncEngine, test_database_url: str) -> AsyncIterator[Listener]:
    """A second connection to the test database, listening on the channel."""
    hook = Listener()
    connection = await asyncpg.connect(asyncpg_dsn(test_database_url))
    await connection.add_listener(CHANNEL, hook._on_notify)
    try:
        yield hook
    finally:
        await connection.close()


# --- The payload ------------------------------------------------------------


def test_the_payload_is_ids_and_nothing_else() -> None:
    """A notification is broadcast to every listener, so it carries no data."""
    parsed = json.loads(
        Event(kind=EPISODE_STATE, anime_id=12, episode_id=34, state="ready").to_json()
    )

    assert set(parsed) == {"kind", "anime_id", "episode_id", "state", "ts"}
    assert parsed["anime_id"] == 12
    assert parsed["episode_id"] == 34
    assert parsed["state"] == "ready"


def test_the_dsn_keeps_the_database_and_drops_the_driver() -> None:
    assert (
        asyncpg_dsn("postgresql+asyncpg://arc:arc@localhost:5432/arc_test_b")
        == "postgresql://arc:arc@localhost:5432/arc_test_b"
    )


def test_publishing_without_a_session_is_not_an_error() -> None:
    """An ORM object a unit test built by hand has nowhere to publish to."""
    assert publish(None, art_event(anime_id=1)) is False


# --- Publication ------------------------------------------------------------


@pytest.mark.pg
async def test_an_event_is_delivered_when_the_transaction_commits(
    api_factory: SessionFactory, listener: Listener
) -> None:
    async with api_factory() as session:
        assert publish(session, art_event(anime_id=7)) is True
        assert staged_count(session) == 1
        await session.commit()

    delivered = await listener.next()
    assert delivered["kind"] == ART
    assert delivered["anime_id"] == 7


@pytest.mark.pg
async def test_an_event_is_not_delivered_when_the_transaction_rolls_back(
    api_factory: SessionFactory, listener: Listener
) -> None:
    """The whole reason ``pg_notify`` is issued inside the transaction.

    A second, committed event is published afterwards so the assertion is
    positive: what arrives is the committed one, and the rolled-back one is not
    merely late.
    """
    async with api_factory() as session:
        anime = await make_anime(session)
        publish(session, art_event(anime_id=anime.id))
        await session.rollback()
        assert staged_count(session) == 0

    async with api_factory() as session:
        publish(session, art_event(anime_id=999))
        await session.commit()

    delivered = await listener.next()
    assert delivered["anime_id"] == 999
    assert listener.pending() == 0


@pytest.mark.pg
async def test_a_transition_publishes_the_state_it_moved_to(
    api_factory: SessionFactory, listener: Listener
) -> None:
    async with api_factory() as session:
        anime = await make_anime(session)
        episode = Episode(anime_id=anime.id, number=1)
        session.add(episode)
        await session.commit()
        anime_id, episode_id = anime.id, episode.id

    async with api_factory() as session:
        row = await session.get(Episode, episode_id)
        assert row is not None
        assert transition(row, EpisodeState.WANTED) is True
        await session.commit()

    delivered = await listener.next()
    assert delivered == {
        "kind": EPISODE_STATE,
        "anime_id": anime_id,
        "episode_id": episode_id,
        "state": "wanted",
        "ts": delivered["ts"],
    }


@pytest.mark.pg
async def test_a_no_op_transition_publishes_nothing(
    api_factory: SessionFactory, listener: Listener
) -> None:
    """Every handler may run twice; the second run must not wake every tab."""
    async with api_factory() as session:
        anime = await make_anime(session)
        episode = Episode(anime_id=anime.id, number=1, state=EpisodeState.WANTED)
        session.add(episode)
        await session.commit()
        episode_id = episode.id

    async with api_factory() as session:
        row = await session.get(Episode, episode_id)
        assert row is not None
        assert transition(row, EpisodeState.WANTED) is False
        await session.commit()

    await asyncio.sleep(0.2)
    assert listener.pending() == 0


@pytest.mark.pg
async def test_a_savepoint_rollback_keeps_the_outer_events(
    api_factory: SessionFactory, listener: Listener
) -> None:
    """A nested rollback is not "nothing happened".

    ``arc.services.catalog.cache`` wraps a racing insert in ``begin_nested()``
    and swallows the ``IntegrityError`` so the surrounding work carries on.
    That rollback fires ``after_soft_rollback`` just like a real one, and
    clearing the stage there would silently drop every event the outer
    transaction is about to make true.
    """
    async with api_factory() as session:
        anime = await make_anime(session)
        publish(session, art_event(anime_id=anime.id))
        assert staged_count(session) == 1

        nested = await session.begin_nested()
        session.add(Episode(anime_id=anime.id, number=1))
        await session.flush()
        await nested.rollback()

        assert staged_count(session) == 1, "the outer transaction is still going to commit"
        await session.commit()
        anime_id = anime.id

    assert (await listener.next())["anime_id"] == anime_id


@pytest.mark.pg
async def test_a_real_rollback_drops_the_stage(api_factory: SessionFactory) -> None:
    """The other half of the same rule, asserted on the stage itself."""
    async with api_factory() as session:
        anime = await make_anime(session)
        publish(session, art_event(anime_id=anime.id))
        await session.rollback()

        assert staged_count(session) == 0


async def test_a_full_stream_queue_drops_the_oldest_event() -> None:
    """The freshest event is the one worth keeping.

    Every payload means "this show changed, ask again", so the newest subsumes
    the ones in front of it — and a stream whose last word is the most out of
    date is the one case where a live update is worse than none.
    """
    broker = EventBroker("postgresql://unused", max_streams=1)
    queue = broker.subscribe()
    for number in range(QUEUE_MAXSIZE + 2):
        broker.dispatch(str(number))

    assert queue.qsize() == QUEUE_MAXSIZE
    # The two oldest went; the newest is still in there.
    assert queue.get_nowait() == "2"
    drained = [queue.get_nowait() for _ in range(queue.qsize())]
    assert drained[-1] == str(QUEUE_MAXSIZE + 1)


def _payloads() -> TmdbPayloads:
    return TmdbPayloads(show=frieren_show(), season=frieren_season(), credits=frieren_credits())


@pytest.mark.pg
async def test_landing_artwork_delivers_art(
    api_factory: SessionFactory, listener: Listener
) -> None:
    """The other publisher: stills and key art arriving (§5.8).

    Through a real notification rather than the staging count, so this asserts
    what a browser would actually receive — including that the enrichment's own
    ``flush()`` does not send it early and the commit does.
    """
    async with api_factory() as session:
        anime = Anime(anilist_id=4242, title_romaji="Sousou no Frieren", status="FINISHED")
        session.add(anime)
        await session.flush()
        session.add(Episode(anime_id=anime.id, number=1))
        await session.flush()

        plan = plan_enrichment(anime, await episodes_for(session, anime.id), _payloads())
        await apply_enrichment(session, anime, plan)
        assert listener.pending() == 0, "not until it commits"
        await session.commit()
        anime_id = anime.id

    delivered = await listener.next()
    assert delivered["kind"] == ART
    assert delivered["anime_id"] == anime_id
    assert delivered["episode_id"] is None


@pytest.mark.pg
async def test_an_enrichment_that_writes_nothing_delivers_nothing(
    api_factory: SessionFactory, listener: Listener
) -> None:
    """A nightly sweep over rows TMDB has already filled must wake nobody."""
    async with api_factory() as session:
        anime = Anime(anilist_id=4243, title_romaji="Sousou no Frieren", status="FINISHED")
        session.add(anime)
        await session.flush()
        session.add(Episode(anime_id=anime.id, number=1))
        await session.flush()

        plan = plan_enrichment(anime, await episodes_for(session, anime.id), _payloads())
        await apply_enrichment(session, anime, plan)
        await session.commit()
        anime_id = anime.id

    assert (await listener.next())["anime_id"] == anime_id

    async with api_factory() as session:
        anime = await session.get_one(Anime, anime_id)
        again = plan_enrichment(anime, await episodes_for(session, anime_id), _payloads())
        await apply_enrichment(session, anime, again)
        await session.commit()

    assert again.empty
    await asyncio.sleep(0.2)
    assert listener.pending() == 0


# --- The stream -------------------------------------------------------------


async def close_broker(app: FastAPI) -> None:
    """Shut the app's listener down, as the lifespan would."""
    broker: EventBroker | None = getattr(app.state, "event_broker", None)
    if broker is not None:
        await broker.aclose()


class AsgiStream:
    """One open ``GET /api/events``, read frame by frame.

    Driving the ASGI app directly rather than through ``ASGITransport``,
    because that transport buffers: it collects every ``http.response.body``
    message and asserts the response is complete before it returns one, so a
    stream that never ends never returns. Everything else about the request is
    real — the middleware stack, the session dependency, the route.
    """

    def __init__(self, app: FastAPI, *, cookie: str) -> None:
        self._app = app
        self._scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"test"), (b"cookie", cookie.encode())],
            "client": ("127.0.0.1", 51234),
            "server": ("test", 80),
        }
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._started = asyncio.Event()
        self._disconnect = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {
                key.decode().lower(): value.decode() for key, value in message.get("headers", [])
            }
            self._started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                self._chunks.put_nowait(body)

    async def __aenter__(self) -> AsgiStream:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))  # type: ignore[arg-type]
        await asyncio.wait_for(self._started.wait(), WAIT)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._disconnect.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def frames(self, count: int, *, timeout: float = WAIT) -> list[str]:
        """The next ``count`` frames, trailing blank line stripped."""
        out: list[str] = []
        while len(out) < count:
            chunk = await asyncio.wait_for(self._chunks.get(), timeout)
            out.append(chunk.decode().rstrip("\n"))
        return out


def session_cookie(client: AsyncClient) -> str:
    """The signed-in cookie off an httpx client's jar, as a header value."""
    token = client.cookies.get(COOKIE_NAME)
    assert token is not None
    return f"{COOKIE_NAME}={token}"


@pytest.mark.pg
async def test_the_stream_needs_a_session(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/events")

    assert response.status_code == 401


async def _held_transactions(dsn: str, database: str) -> int:
    """Backends on ``database`` sitting inside an unfinished transaction."""
    connection = await asyncpg.connect(dsn)
    try:
        held: int = await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND state = 'idle in transaction'",
            database,
        )
        return held
    finally:
        await connection.close()


@pytest.mark.pg
async def test_an_open_stream_holds_no_database_connection(
    api_app: FastAPI, admin_client: AsyncClient, test_database_url: str
) -> None:
    """The one thing that decides whether this feature can be deployed at all.

    FastAPI tears a dependency stack down only after the response body is
    finished, and a stream's body finishes when the tab does. The session
    ``CurrentUser`` resolved on has read a row by then, which leaves its
    connection ``idle in transaction`` — so without the explicit
    ``session.close()`` in the route, every open tab would pin one pooled
    connection for hours and a dozen of them would stall the API.

    Asserted against ``pg_stat_activity`` rather than the pool's counters
    because the pool is the thing under test: the question is whether Postgres
    still has a transaction open on Arc's behalf, and that is where the answer
    lives.
    """
    dsn = asyncpg_dsn(test_database_url)
    database = make_url(test_database_url).database
    assert database is not None

    try:
        async with AsgiStream(api_app, cookie=session_cookie(admin_client)) as stream:
            assert await stream.frames(1) == [": open"]

            assert await _held_transactions(dsn, database) == 0
    finally:
        await close_broker(api_app)


@pytest.mark.pg
async def test_the_stream_opens_and_heartbeats(
    api_app: FastAPI, admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comment line on open and another every ``HEARTBEAT_SECONDS``.

    Both are SSE comments, which is what keeps a proxy from closing an idle
    connection without saying anything the client has to parse.
    """
    monkeypatch.setattr("arc.api.events.HEARTBEAT_SECONDS", 0.05)
    try:
        async with AsgiStream(api_app, cookie=session_cookie(admin_client)) as stream:
            assert stream.status == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            assert "no-cache" in stream.headers["cache-control"]

            assert await stream.frames(2) == [": open", ": ping"]
    finally:
        await close_broker(api_app)


@pytest.mark.pg
async def test_the_stream_carries_a_published_event(
    api_app: FastAPI, admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    try:
        async with AsgiStream(api_app, cookie=session_cookie(admin_client)) as stream:
            # The open frame proves this connection is subscribed, so the event
            # published below cannot be lost to a race with the handshake.
            assert await stream.frames(1) == [": open"]

            async with api_factory() as session:
                publish(session, art_event(anime_id=31))
                await session.commit()

            frames = await stream.frames(1)

        assert frames[0].startswith("data: ")
        assert json.loads(frames[0].removeprefix("data: ")) == {
            "kind": ART,
            "anime_id": 31,
            "episode_id": None,
            "state": None,
            "ts": ANY_TS,
        }
    finally:
        await close_broker(api_app)


@pytest.mark.pg
async def test_a_closed_stream_stops_counting_against_the_cap(
    api_app: FastAPI, admin_client: AsyncClient
) -> None:
    """A tab that goes away has to come off the broker, or the cap fills up."""
    cookie = session_cookie(admin_client)
    async with AsgiStream(api_app, cookie=cookie) as stream:
        await stream.frames(1)
        broker: EventBroker = api_app.state.event_broker
        assert broker.stream_count == 1

    try:
        await asyncio.sleep(0.1)
        assert broker.stream_count == 0
    finally:
        await close_broker(api_app)


@pytest.mark.pg
async def test_the_stream_is_capped_per_process(
    api_app: FastAPI, admin_client: AsyncClient, settings: Settings
) -> None:
    """One process holds so many streams and says 503 above it."""
    api_app.state.event_broker = EventBroker(asyncpg_dsn(settings.database_url), max_streams=0)
    try:
        response = await admin_client.get("/api/events")

        assert response.status_code == 503
        assert response.json()["detail"] == TOO_MANY_STREAMS
    finally:
        await close_broker(api_app)


async def test_the_lifespan_closes_the_listener() -> None:
    """The one property ``uvicorn --reload`` depends on.

    A reload builds a new app and runs the old one's lifespan shutdown. The
    broker is created by the first stream and owned by the app that created it,
    so the shutdown has to close it — otherwise every reload leaves a ``LISTEN``
    connection and a supervising task behind.

    Deliberately pointed at a database that is not there: the lifespan logs a
    warning for the ping and for the admin bootstrap and carries on (an api
    that runs while Postgres restarts is worth more than a clean exit), which
    is exactly the path this needs, and it keeps the test off every real
    database.
    """
    nowhere = Settings(  # type: ignore[call-arg]
        env="test",
        database_url="postgresql+asyncpg://arc:arc@127.0.0.1:1/nowhere",
        _env_file=None,
    )
    app = create_app(nowhere)
    broker = EventBroker(asyncpg_dsn(nowhere.database_url))
    app.state.event_broker = broker

    async with _lifespan(app):
        pass

    assert broker.listening is False
    assert broker.stream_count == 0
