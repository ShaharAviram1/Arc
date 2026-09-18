"""What qBittorrent is doing right now, for the admin panel (FR-D3).

The one function here is the exception to Arc's usual rule that a service
raises and the router decides. :func:`qbit_status` **never raises**: "is
qBittorrent reachable?" is the question, and an exception is one of the two
answers rather than a failure to answer. An admin opening the panel because
downloads have stopped must be told *why* — a wrong password, a container that
is not running, a URL that resolves nowhere — not handed a 502 that says the
admin panel is broken too.

It is also the only place in Arc that talks to qBittorrent inside a request.
That is deliberate and bounded: two calls, a five-second timeout, and no
writes. Everything that *changes* something still goes on the queue.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Final

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import ConfigurationError, Settings
from arc.models import Torrent
from arc.services.acquisition.qbit import QbitClient, QbitError, TorrentInfo

log = logging.getLogger(__name__)

#: Budget for the whole probe. Shorter than the client's own 20 s
#: (``qbit.TIMEOUT_SECONDS``): a job can afford to wait for a busy client, a
#: page an admin is staring at cannot. Read at call time rather than baked into
#: a default argument, so it is the one place the figure lives.
STATUS_TIMEOUT_SECONDS: Final[float] = 5.0

TIMED_OUT = "qbittorrent did not answer within {seconds} s"


def _seconds(value: float) -> str:
    """``5.0`` → ``"5"``; anything shorter keeps its decimals, for the message."""
    return str(int(value)) if value == int(value) else str(value)


@dataclass(frozen=True, slots=True)
class _Known:
    """The three columns Arc's own row adds to what the client reported."""

    episode_id: int | None = None
    kind: str | None = None
    wanted_bytes: int | None = None


#: What a hash Arc has no row for reads as: a torrent in Arc's category that
#: somebody else added, or one whose row has since been deleted.
_UNKNOWN: Final[_Known] = _Known()


@dataclass(frozen=True, slots=True)
class TorrentStatus:
    """One live torrent, with the episode it belongs to when Arc knows it."""

    hash: str
    name: str
    state: str
    #: 0..1.
    progress: float
    #: What the client reports, which is already **the selected files only** —
    #: ``torrents/info``'s ``size`` is the total of the files chosen for
    #: download, not of the payload — so a pack whose one wanted file is 1.1 GB
    #: of 14.8 GB reads as 1.1 GB here with no arithmetic of Arc's own (FR-A11).
    size: int
    dlspeed: int
    upspeed: int
    #: From the ``torrents`` table, by info hash. Null for anything in Arc's
    #: category that Arc did not add, or whose row has been deleted since — and
    #: null for **every** pack, whose row belongs to no single episode (FR-A11).
    episode_id: int | None = None
    #: ``single`` or ``batch`` from that row, or null when Arc has no row. The
    #: one thing an admin cannot infer from the name: a pack looks like an
    #: ordinary torrent in the client and is holding twenty-five files nobody
    #: asked for at priority 0.
    kind: str | None = None
    #: The sum of the files Arc asked for, as the pick recorded it. Set for a
    #: pack and null for a single, whose wanted bytes are its whole payload.
    #: **``total_size`` is deliberately not reported**: the payload of a pack is
    #: not a figure any rule, log or reservation in Arc may read (FR-A11), and a
    #: panel that showed 14.8 GB beside a 1.1 GB download would be the one place
    #: it leaked back in.
    wanted_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class QbitStatus:
    """``GET /api/acquisition/qbit``."""

    reachable: bool
    version: str | None = None
    #: Why it is not reachable, in one sentence, or ``None`` when it is.
    error: str | None = None
    torrents: list[TorrentStatus] = field(default_factory=list)


async def qbit_status(
    session: AsyncSession,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float | None = None,
) -> QbitStatus:
    """Ask qBittorrent for its version and its torrents. Never raises.

    ``timeout`` is the budget for the **whole probe**, not for one request.
    That distinction is the point: the client logs in on its first call, and
    re-logs in and retries once on a 403, so two API calls are up to six
    requests — a per-request timeout of five seconds is a page that can hang
    for thirty. :func:`asyncio.timeout` bounds the lot; the per-request value
    is left at the same figure so a single stalled socket is cut off first and
    reported as itself.

    The episode ids come from Arc's own ``torrents`` table rather than from the
    client's tags: the tag is set on add and is whatever survived a user
    editing it, while the row is what every other part of acquisition keys off.
    """
    budget = STATUS_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        client = QbitClient.from_settings(settings, transport=transport, timeout=budget)
    except ConfigurationError as exc:
        # QBIT_USER / QBIT_PASS unset. Reported like any other reason it
        # cannot be reached — from the panel's point of view it is one.
        return QbitStatus(reachable=False, error=str(exc))

    version = ""
    found: list[TorrentInfo] = []
    error: str | None = None
    try:
        async with asyncio.timeout(budget):
            version = await client.version()
            found = await client.torrents()
    # Before ``OSError``: the builtin ``TimeoutError`` is a subclass of it, and
    # the other way round the timeout would be reported as an empty ``str(exc)``.
    except TimeoutError:
        error = TIMED_OUT.format(seconds=_seconds(budget))
    except (QbitError, httpx.HTTPError, OSError) as exc:
        error = str(exc)
    finally:
        # Runs after the timeout has been converted back into an exception, so
        # this is never itself cancelled; the socket is closed either way.
        await client.aclose()

    if error is not None:
        log.info("qbittorrent status probe failed", extra={"error": error})
        return QbitStatus(reachable=False, error=error)

    known = await _known(session, found)
    torrents: list[TorrentStatus] = []
    for info in found:
        row = known.get(info.hash, _UNKNOWN)
        torrents.append(
            TorrentStatus(
                hash=info.hash,
                name=info.name,
                state=info.state,
                progress=info.progress,
                size=info.size,
                dlspeed=info.dlspeed,
                upspeed=info.upspeed,
                episode_id=row.episode_id,
                kind=row.kind,
                wanted_bytes=row.wanted_bytes,
            )
        )
    return QbitStatus(reachable=True, version=version or None, torrents=torrents)


async def _known(session: AsyncSession, found: list[TorrentInfo]) -> dict[str, _Known]:
    """``info_hash → what Arc's row says`` for the hashes just reported."""
    hashes = [info.hash for info in found]
    if not hashes:
        return {}
    rows = await session.execute(
        select(Torrent.info_hash, Torrent.episode_id, Torrent.kind, Torrent.wanted_bytes).where(
            Torrent.info_hash.in_(hashes)
        )
    )
    return {
        info_hash.lower(): _Known(episode_id=episode_id, kind=kind.value, wanted_bytes=wanted_bytes)
        for info_hash, episode_id, kind, wanted_bytes in rows.all()
    }


__all__ = ["STATUS_TIMEOUT_SECONDS", "TIMED_OUT", "QbitStatus", "TorrentStatus", "qbit_status"]
