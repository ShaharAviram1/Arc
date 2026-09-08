"""Building MyAnimeList clients, and the one seam the tests replace.

Both authenticated clients — the OAuth one and the list one — are short-lived:
built for a request or a job, closed on the way out. There is no shared,
process-wide MAL client the way there is a shared catalogue, because there is
nothing to share: the pacing that justifies AniList's singleton does not apply
(MAL's per-user endpoints are called a handful of times a day), and the tokens
belong to one user rather than to the process.

:func:`transport_for` is the seam. In production it returns ``None`` and httpx
uses its own transport; the test suite replaces this one function with a
``MockTransport`` and every client built anywhere — inside a job, inside the
callback route — is wired to the fake without a single constructor having to
take a ``transport=`` argument it would otherwise never use. The same
indirection the catalogue's ``_sleep`` is, for the same reason.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import MalLink
from arc.services.mal.client import MalClient
from arc.services.mal.oauth import MalOAuthClient


def transport_for(settings: Settings) -> httpx.AsyncBaseTransport | None:
    """The transport MAL clients are built on; ``None`` for httpx's default."""
    return None


@asynccontextmanager
async def oauth_client(settings: Settings) -> AsyncIterator[MalOAuthClient]:
    """A token-endpoint client for the length of one handshake."""
    client = MalOAuthClient(settings, transport=transport_for(settings))
    try:
        yield client
    finally:
        await client.aclose()


@asynccontextmanager
async def client_for(
    settings: Settings,
    session: AsyncSession,
    *,
    user_id: int,
    now: datetime | None = None,
) -> AsyncIterator[MalClient]:
    """An authenticated client for one user, or :class:`MalNotLinked`."""
    client = await MalClient.open(
        settings, session, user_id=user_id, transport=transport_for(settings), now=now
    )
    try:
        yield client
    finally:
        await client.aclose()


@asynccontextmanager
async def client_of(
    settings: Settings, link: MalLink, *, now: datetime | None = None
) -> AsyncIterator[MalClient]:
    """A client for a link row already in hand — the callback's case.

    The link has just been written and is not committed yet, so looking it up
    again would be a query for a row this code is holding.
    """
    # No session, and therefore no row lock on a refresh: this is the callback,
    # which has just written the tokens itself and cannot be racing anybody
    # for them. :meth:`MalClient.refresh` handles the absence.
    client = MalClient(settings, link, transport=transport_for(settings), now=now)
    try:
        yield client
    finally:
        await client.aclose()


__all__ = ["client_for", "client_of", "oauth_client", "transport_for"]
