"""Live updates: what changed, published from the write and fanned out by the API.

Arc runs two processes (architecture.md §2). The thing a viewer is waiting for —
an episode reaching ``ready``, a still landing — happens in the **worker**, and
the tab that wants to know about it is talking to the **api**. An in-process
event bus therefore cannot carry this: whatever the worker publishes has to
cross a process boundary, and the one piece of shared infrastructure both
already hold a connection to is Postgres. So the event source is
``LISTEN``/``NOTIFY`` on a single channel, :data:`CHANNEL`.

Two halves, and they never meet in the same process:

* **Publish** (:func:`publish`) — called from the write that matters:
  :func:`arc.services.acquisition.states.transition` for every episode state
  change and :func:`arc.services.tmdb.enrich.apply_enrichment` for artwork.
  It does not send anything itself. It *stages* the payload on the session and
  a ``before_commit`` listener turns the staged list into ``pg_notify`` calls
  **inside the transaction that is committing**, which is the whole point:
  Postgres delivers a notification only if that transaction commits, so a
  handler that rolls back — a failed job, an illegal transition caught two
  lines later — cannot tell a browser about a row that does not exist.
  Staging rather than awaiting is what lets ``transition()`` stay synchronous:
  it holds an ORM object, not a connection, and ``object_session()`` is enough
  to reach the transaction it belongs to.

* **Fan-out** (:class:`EventBroker`) — one dedicated asyncpg connection per
  **api** process, ``LISTEN``ing once and pushing each payload into a queue per
  open stream. ``GET /api/events`` (arc/api/events.py) is the stream.

What is *in* an event is deliberately thin: a kind, an anime id, an episode id,
a state, a timestamp. No titles, no paths, no user ids — a notification is
broadcast to every listener on the channel and read by every signed-in tab, so
it carries only the ids a client needs to decide which of its own queries is
now stale. The client re-asks the endpoints it already had, with its own
session, and the server answers as it always did. That also keeps the payload
far inside Postgres' 8 kB notification limit (:data:`MAX_PAYLOAD_BYTES`).

Nothing here is load-bearing. Every page that reacts to an event also polls
(``ACQUISITION_POLL_MS`` and friends in ``client/src/lib/anime.ts``), so a
listener that cannot connect, a dropped notification or a proxy that eats the
stream costs a viewer some seconds, not correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

import asyncpg
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: The one channel. A single channel rather than one per kind because the
#: listener is a process-wide connection and a client filters on ``kind``
#: anyway; adding channels would multiply the connections, not the information.
CHANNEL: Final = "arc_events"

#: Postgres refuses a notification payload of 8000 bytes or more. Arc's are
#: about 120, and this is a guard against a future field, not a real limit.
MAX_PAYLOAD_BYTES: Final = 7_900

#: How long a stream may be silent before it sends a comment line. Proxies and
#: load balancers close an idle connection, and a browser's ``EventSource``
#: reconnects when they do — cheap, but it means a gap. 25 s is comfortably
#: inside the usual 60 s idle timeouts (Caddy's included, §8).
HEARTBEAT_SECONDS: Final = 25.0

#: Concurrent streams one api process will hold. Each is an open connection and
#: a queue, nothing more, but they are unbounded by nature — a tab left open is
#: a stream held — so there is a ceiling and a 503 above it.
MAX_STREAMS: Final = 100

#: Events queued for one stream before the oldest are dropped (:func:`_offer`).
#: A stream this far behind is one nobody is reading; its viewer's polling is
#: the backstop.
QUEUE_MAXSIZE: Final = 200

#: Reconnect backoff for the listening connection, and how often the loop looks
#: at whether that connection is still alive.
_RECONNECT_MIN_SECONDS: Final = 1.0
_RECONNECT_MAX_SECONDS: Final = 30.0
_LIVENESS_CHECK_SECONDS: Final = 5.0

#: How long :meth:`EventBroker.start` waits for the first ``LISTEN`` before
#: giving up and serving the stream anyway (polling is the fallback).
_READY_TIMEOUT_SECONDS: Final = 2.0

#: ``session.info`` key the staged payloads live under.
_STAGED: Final = "arc_events_staged"

#: An episode moved through the state machine (spec §6).
EPISODE_STATE: Final = "episode_state"
#: Artwork landed on a show or its episodes (§5.8).
ART: Final = "art"

type EventKind = Literal["episode_state", "art"]


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that changed, as it travels on the wire.

    ``episode_id`` and ``state`` are null for an :data:`ART` event: artwork
    lands on a show and on any number of its episodes at once, and the client's
    answer to both is the same — re-ask for the show.
    """

    kind: EventKind
    anime_id: int
    episode_id: int | None = None
    state: str | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        """The payload, compact — every byte counts against Postgres' limit."""
        return json.dumps(
            {
                "kind": self.kind,
                "anime_id": self.anime_id,
                "episode_id": self.episode_id,
                "state": self.state,
                "ts": self.ts.isoformat(),
            },
            separators=(",", ":"),
        )


def episode_state_event(*, anime_id: int, episode_id: int | None, state: str) -> Event:
    """The event an episode state change publishes."""
    return Event(kind=EPISODE_STATE, anime_id=anime_id, episode_id=episode_id, state=state)


def art_event(*, anime_id: int) -> Event:
    """The event landing artwork publishes."""
    return Event(kind=ART, anime_id=anime_id)


# --- Publish ----------------------------------------------------------------


def _sync_session(session: AsyncSession | Session | None) -> Session | None:
    """The plain ``Session`` under whatever the caller is holding.

    ``transition()`` has an ORM object and reaches its session with
    ``object_session()``, which always returns the synchronous one; the
    enrichment has the ``AsyncSession`` it was handed. Both stage onto the same
    ``info`` dict, which is where the commit listener looks.
    """
    if session is None:
        return None
    if isinstance(session, AsyncSession):
        return session.sync_session
    return session


def publish(session: AsyncSession | Session | None, event_to_send: Event) -> bool:
    """Stage ``event_to_send`` for delivery when ``session``'s transaction commits.

    Returns whether it was staged. ``False`` — with a line in the log — for the
    three ways an event can have nowhere to go: no session at all (an ORM object
    a test built by hand and never added), a payload too large for a Postgres
    notification, and a session on a database that has no ``pg_notify`` (the
    SQLite engine a couple of unit tests build). None of them is an error: an
    unpublished event costs a viewer a polling interval.
    """
    target = _sync_session(session)
    if target is None:
        log.debug("event not published: no session", extra={"kind": event_to_send.kind})
        return False

    payload = event_to_send.to_json()
    if len(payload.encode()) >= MAX_PAYLOAD_BYTES:
        log.error("event not published: payload too large", extra={"kind": event_to_send.kind})
        return False

    staged: list[str] = target.info.setdefault(_STAGED, [])
    staged.append(payload)
    return True


def _notify_supported(session: Session) -> bool:
    """Whether this session's database has ``pg_notify``.

    Read off the bind rather than tried and caught: a failed statement poisons
    the transaction it is in, and this one is issued *inside* somebody else's
    commit. A session with no bind at all cannot be asked, and answers no.
    """
    try:
        bind = session.get_bind()
    except SQLAlchemyError:
        return False
    return bind.dialect.name == "postgresql"


@event.listens_for(Session, "before_commit")
def _emit_staged(session: Session) -> None:
    """Turn the staged payloads into ``pg_notify`` calls, inside this transaction.

    Registered on the ``Session`` class itself, once, at import: both processes
    reach this module through the two publishers, and a listener that only ever
    acts on a non-empty ``info`` key costs a session with no events one dict
    lookup per commit.

    A failure is swallowed. This runs in the middle of a commit that is about
    to persist real work — a transcode's ``ready``, a night's artwork — and
    taking that down because a notification could not be sent would trade the
    thing that matters for the thing that does not.
    """
    staged: list[str] = session.info.pop(_STAGED, [])
    if not staged:
        return
    if not _notify_supported(session):
        log.debug("events dropped: not postgresql", extra={"count": len(staged)})
        return

    for payload in staged:
        try:
            # ``session.connection().execute`` rather than
            # ``session.execute``: the latter **autoflushes** first, and this
            # runs inside a commit that is about to flush anyway. A flush
            # failing here rather than a line later turns an ordinary
            # ``IntegrityError`` the caller could have handled into a
            # ``PendingRollbackError`` raised from a notification — the
            # feature that is allowed to fail breaking the one that is not.
            # Going straight to the connection also skips the ORM entirely,
            # which is all a ``pg_notify`` ever needed.
            session.connection().execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": CHANNEL, "payload": payload},
            )
        except SQLAlchemyError as exc:
            log.warning("event not published", extra={"error": str(exc)})
            return


@event.listens_for(Session, "after_soft_rollback")
def _drop_staged(session: Session, _previous: Any) -> None:
    """A rolled-back transaction has nothing to announce.

    ``after_soft_rollback`` rather than ``after_rollback``, and it is the only
    clearer there is. Two reasons, in this order:

    * **It fires for every rollback**, including one on a session that staged
      an event and never emitted any SQL — which ``after_rollback``, being
      "after a real DBAPI rollback", does not see at all.
    * **It fires last.** SQLAlchemy dispatches ``after_rollback`` from inside
      ``_rollback_impl``, while the transaction being unwound is still on the
      session, and dispatches this one at the end of ``Session.rollback()``
      once the unwinding is done. Only at that point does
      :meth:`~sqlalchemy.orm.Session.in_transaction` tell the two cases apart.

    And telling them apart is the whole job, because **both** events also fire
    for a ``SAVEPOINT`` rollback — ``begin_nested()``, which
    :mod:`arc.services.catalog.cache` uses to swallow a racing insert's
    ``IntegrityError`` and carry on. There the outer transaction is very much
    alive and about to commit, so clearing the stage would throw away events
    the surrounding work is going to make true. A session still in a
    transaction has therefore not rolled anything back as far as this is
    concerned; only a rollback that leaves it with no transaction at all
    counts as "nothing happened".
    """
    if session.in_transaction():
        return
    session.info.pop(_STAGED, None)


# --- Fan-out ----------------------------------------------------------------


class BrokerFull(RuntimeError):
    """This api process is already holding :data:`MAX_STREAMS` streams."""


def _offer(queue: asyncio.Queue[str], payload: str) -> None:
    """Put ``payload`` on ``queue``, discarding the **oldest** if it is full.

    Which end to drop is a real choice, and the newest event is the one to
    keep: every payload is "this show changed, ask again", so the freshest one
    subsumes the ones in front of it, and a client that receives it re-asks for
    everything anyway. Dropping the newest instead would leave a stream whose
    last word is the most out of date — the one case where a live update is
    worse than no live update.

    A stream 200 events behind is not being read at all, so this is a
    bookkeeping detail rather than a data path; it is here so the behaviour
    matches what the docs promise.
    """
    while True:
        try:
            queue.put_nowait(payload)
            return
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - drained under us
                continue
            log.warning("event stream behind: dropped the oldest event")


def asyncpg_dsn(database_url: str) -> str:
    """``postgresql+asyncpg://…`` → the plain DSN asyncpg takes.

    The same URL the engine uses, with the driver name dropped: the listening
    connection must reach the same database as every write, and deriving it
    from one setting is what guarantees that (§7).
    """
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


class EventBroker:
    """One ``LISTEN`` connection per api process, fanned out to open streams.

    Lazily started (the first stream asks for it) and closed by the app's
    lifespan, so a process that nobody opens a stream against never opens the
    connection, and ``uvicorn --reload`` cannot leave one behind.

    The connection is supervised: it is re-opened with backoff whenever it
    drops, because a Postgres restart must not turn live updates off until the
    next deploy. While it is down the streams stay open and silent, sending
    their heartbeats, and the client's polling covers the gap.
    """

    def __init__(self, dsn: str, *, max_streams: int = MAX_STREAMS) -> None:
        self._dsn = dsn
        self._max_streams = max_streams
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = False

    @property
    def listening(self) -> bool:
        """Whether the ``LISTEN`` connection is up right now."""
        return self._ready.is_set()

    @property
    def stream_count(self) -> int:
        return len(self._subscribers)

    @property
    def at_capacity(self) -> bool:
        """Whether a new stream would be refused.

        Asked by the route *before* it starts a response, because 503 is only
        available while there are still headers to send. :meth:`subscribe` is
        the real gate — this is the one that can answer with a status code.
        """
        return len(self._subscribers) >= self._max_streams

    async def start(self, *, timeout: float = _READY_TIMEOUT_SECONDS) -> None:
        """Ensure the listener is running. Idempotent; safe to call per request.

        Waits briefly for the first ``LISTEN`` so that a stream opened right
        after start-up does not miss an event published a millisecond later,
        then gives up and returns: a stream that only heartbeats is a worse
        answer than no stream, but both are covered by polling, and refusing
        the request would be the only one of the three that breaks something.
        """
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self._listen_forever(), name="arc-events-listen")
        if self._ready.is_set():
            return
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            log.warning("event listener not ready; live updates fall back to polling")

    def subscribe(self) -> asyncio.Queue[str]:
        """A queue that receives every payload until it is unsubscribed."""
        if self.at_capacity:
            raise BrokerFull(f"{len(self._subscribers)} streams already open")
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def dispatch(self, payload: str) -> None:
        """Hand ``payload`` to every open stream. Never raises, never blocks."""
        for queue in list(self._subscribers):
            _offer(queue, payload)

    def _on_notify(self, _connection: object, _pid: int, _channel: str, payload: str) -> None:
        """asyncpg's callback. Synchronous by contract, so it only enqueues."""
        self.dispatch(payload)

    async def _listen_forever(self) -> None:
        delay = _RECONNECT_MIN_SECONDS
        while not self._closing:
            #: Untyped: asyncpg ships no stubs (pyproject's mypy overrides).
            connection: Any = None
            try:
                connection = await asyncpg.connect(self._dsn)
                await connection.add_listener(CHANNEL, self._on_notify)
                delay = _RECONNECT_MIN_SECONDS
                self._ready.set()
                log.info("event listener connected", extra={"channel": CHANNEL})
                while not self._closing and not connection.is_closed():
                    await asyncio.sleep(_LIVENESS_CHECK_SECONDS)
            except Exception as exc:
                # Deliberately everything — and ``Exception`` rather than
                # ``BaseException`` precisely so that the ``CancelledError``
                # that ends this task on shutdown still passes straight
                # through. A named list of driver errors is
                # the wrong shape here: the ways a socket can go wrong include
                # ``asyncpg.InterfaceError`` and ``InternalClientError`` on a
                # half-closed connection, neither of which is an ``OSError``
                # or a ``PostgresError``, and one unlisted exception does not
                # mean "stop listening for the life of the process" — it means
                # reconnect. This is a supervisor; its whole job is to survive
                # the thing it supervises.
                log.warning(
                    "event listener lost",
                    extra={"error": str(exc), "type": type(exc).__name__},
                )
            finally:
                self._ready.clear()
                if connection is not None:
                    # ``terminate`` rather than ``close``: it needs no round
                    # trip, so it also works on the cancellation path, which is
                    # how this task ends on shutdown.
                    connection.terminate()
            if self._closing:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_SECONDS)

    async def aclose(self) -> None:
        """Stop listening and forget every stream. Idempotent."""
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._subscribers.clear()
        self._ready.clear()


__all__ = [
    "ART",
    "CHANNEL",
    "EPISODE_STATE",
    "HEARTBEAT_SECONDS",
    "BrokerFull",
    "Event",
    "EventBroker",
    "EventKind",
    "art_event",
    "asyncpg_dsn",
    "episode_state_event",
    "publish",
]
