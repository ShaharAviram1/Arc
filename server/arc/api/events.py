"""``GET /api/events`` — server-sent events, so a tab stays true (§5.9).

One route, one job: hand a signed-in browser the payloads the worker published
(``arc/services/events.py``) as they happen, so Watch Now grows a "Ready to
watch" tile and a show page flips a row without anybody pressing reload.

Three things about it are deliberate.

**It takes a session like everything else.** ``CurrentUser`` — 401 for a
browser with no cookie. The stream is a GET, so the CSRF/origin middleware does
not look at it (it guards mutations), and it needs nothing from it: the payloads
carry ids and nothing else, so there is nothing here worth reading that a
signed-in client could not already ask for by id. There *is* no per-user
filtering, for the same reason: which rows matter to which viewer is a question
the client can answer for itself out of its own cache, and asking it here would
mean one query per event per stream.

**It is not cacheable and not buffered.** ``Cache-Control: no-cache`` because an
event stream that a proxy holds on to is a page that updates once, and
``X-Accel-Buffering: no`` for the proxies that read it. Caddy gives
``/api/events`` its own ``handle`` with ``flush_interval -1`` and keeps it out
of ``encode`` (§8) — without that the whole stream sits in a buffer and the
feature is invisible while looking configured.

**It is capped.** ``MAX_STREAMS`` per process, 503 above it. A stream is a held
connection, and a tab left open is a stream held; the cap is what stops a
process running out of them, and 503 is honest — the client keeps polling.

And one thing it must **not** hold: a database connection. See
:func:`events` — the request's session is closed before the response is
returned, because a dependency stack outlives a streaming body and a
half-finished transaction would be pinned to the pool for as long as the tab
is open.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, HTTPException, Request, status
from starlette.responses import StreamingResponse

from arc.api.deps import CurrentUser, SessionDep
from arc.services.events import (
    HEARTBEAT_SECONDS,
    BrokerFull,
    EventBroker,
    asyncpg_dsn,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["events"])

#: What a caller gets above the per-process cap.
TOO_MANY_STREAMS: Final = "too many live connections"

#: ``app.state`` attribute the broker lives on, so the lifespan can close it.
BROKER_ATTR: Final = "event_broker"

#: Every stream opens with this, before anything has happened: a comment line
#: (SSE ignores it) that makes the browser's ``EventSource`` fire ``open`` and
#: forces the first flush through whatever is between here and the tab.
_OPEN_FRAME: Final = b": open\n\n"

#: The idle keep-alive. Also a comment line, for the same reason.
_PING_FRAME: Final = b": ping\n\n"

#: Sent instead of :data:`_OPEN_FRAME` when the cap was reached between the
#: handler's check and the generator's first line. The headers have gone by
#: then, so this is all the explanation the protocol has room for.
_FULL_FRAME: Final = b": full\n\n"

SSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "X-Accel-Buffering": "no",
}


async def get_broker(request: Request) -> EventBroker:
    """The process's broker, built on first use.

    Lazily rather than in the lifespan for two reasons: a process nobody opens
    a stream against should not hold a Postgres connection for nothing, and
    ``uvicorn --reload`` replaces the app object — a broker created here is
    owned by the app that created it and closed with it, so a reload cannot
    leave a listener behind. There is no lock: building it touches no ``await``,
    so two concurrent first requests cannot both get past the ``None`` check,
    and :meth:`EventBroker.start` is idempotent anyway.
    """
    broker: EventBroker | None = getattr(request.app.state, BROKER_ATTR, None)
    if broker is None:
        broker = EventBroker(asyncpg_dsn(request.app.state.settings.database_url))
        setattr(request.app.state, BROKER_ATTR, broker)
    await broker.start()
    return broker


def _frame(payload: str) -> bytes:
    """One SSE message. ``data:`` only, so the client's ``onmessage`` fires."""
    return b"data: " + payload.encode() + b"\n\n"


async def _stream(broker: EventBroker) -> AsyncIterator[bytes]:
    """Frames for one open connection, until the client goes away.

    The queue is taken **here**, not in the handler, and that placement is
    load-bearing: a generator's body does not run until its first
    ``__anext__``, so a client that disconnects between the response headers
    and the first read never subscribes at all. Subscribing in the handler
    would leak that queue against the cap for the life of the process, since
    the ``finally`` below is reached only by a generator that started.

    The handler has already refused the request if the process is at its cap,
    so :exc:`BrokerFull` here is the narrow race between that check and this
    line. It ends the stream rather than erroring: the headers are long gone,
    so there is no status left to send, and a client whose stream closes falls
    back on the polling it never stopped doing.
    """
    try:
        queue = broker.subscribe()
    except BrokerFull:
        log.warning("event stream closed at once: at capacity")
        yield _FULL_FRAME
        return

    try:
        yield _OPEN_FRAME
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                yield _PING_FRAME
                continue
            # Arc writes every payload itself, as one line of JSON, and JSON
            # cannot contain a raw newline — so this can only trip on a
            # ``NOTIFY arc_events`` somebody else sent on the same database.
            # A newline in a frame is a frame boundary, so such a payload is
            # dropped rather than passed on.
            if "\n" in payload:
                log.warning("event dropped: payload is not one line")
                continue
            yield _frame(payload)
    finally:
        broker.unsubscribe(queue)


@router.get(
    "/events",
    summary="Live event stream (server-sent events)",
    response_class=StreamingResponse,
)
async def events(request: Request, user: CurrentUser, session: SessionDep) -> StreamingResponse:
    """Open the stream, having first given the database connection back.

    ``session`` is asked for explicitly so that it can be **closed before the
    response is returned**, and that is the whole reason it is in the
    signature. FastAPI tears a dependency stack down only after the response
    body is finished, and a stream's body finishes when the tab does — so the
    session ``CurrentUser`` resolved on (``resolve_session`` reads a row, which
    leaves the connection ``idle in transaction``) would stay checked out of
    the pool for hours. Fifteen open tabs would then hold fifteen connections
    and the pool's sixteenth caller would wait for a tab to close.

    Nothing downstream needs it: the authenticated user is reduced to an id for
    one log line, and the stream itself talks to the broker, never to the ORM.
    Closing early is safe — the dependency's own ``async with`` closes an
    already-closed session without complaint.
    """
    user_id = user.id
    await session.close()

    broker = await get_broker(request)
    if broker.at_capacity:
        log.warning("event stream refused", extra={"streams": broker.stream_count})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=TOO_MANY_STREAMS
        )

    log.info(
        "event stream opened",
        extra={"user_id": user_id, "streams": broker.stream_count, "live": broker.listening},
    )
    return StreamingResponse(
        _stream(broker),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


__all__ = ["BROKER_ATTR", "TOO_MANY_STREAMS", "router"]
