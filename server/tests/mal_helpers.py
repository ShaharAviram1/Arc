"""Fixtures and small builders shared by the MyAnimeList tests.

The MAL tests need settings the rest of the suite deliberately does not have —
a client id, a secret, and both MAL URLs pointed at the fake — so they override
the ``settings`` fixture rather than widening the shared one. Overriding is
enough: ``api_app`` and every fixture built on it take ``settings`` by name, so
importing :func:`settings` into a test module re-points the whole stack at the
fake without touching ``conftest``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from arc.config import Settings
from arc.core.crypto import encrypt
from arc.db import SessionFactory
from arc.models import (
    Anime,
    ListEntry,
    ListStatus,
    MalLink,
    MalWriteCause,
    MalWriteLog,
    MalWriteStatus,
    UpdatedBy,
)
from tests.conftest import TEST_FERNET_KEY
from tests.mal_api_mock import API_URL, CLIENT_ID, CLIENT_SECRET, OAUTH_URL, REDIRECT_URI

USER_EMAIL = "mal@arc.test"
USER_PASSWORD = "mal-password-123"

#: A second account, for the "this state is not yours" test.
OTHER_EMAIL = "other@arc.test"
OTHER_PASSWORD = "other-password-123"


@pytest.fixture(name="settings")
def mal_settings(test_database_url: str) -> Settings:
    """Test settings with MyAnimeList configured and pointed at the fake.

    Registered under the name ``settings`` while being *called* something
    else: a test module imports ``mal_settings`` to install the override, and
    its test functions take a parameter called ``settings``. Importing it under
    the fixture's own name would make every one of those parameters shadow the
    import, which is a lint error in forty places and a confusing one.
    """
    return Settings(  # type: ignore[call-arg]
        env="test",
        database_url=test_database_url,
        fernet_key=TEST_FERNET_KEY,
        mal_client_id=CLIENT_ID,
        mal_client_secret=CLIENT_SECRET,
        mal_redirect_uri=REDIRECT_URI,
        mal_api_url=API_URL,
        mal_oauth_url=OAUTH_URL,
        _env_file=None,
    )


async def link_user(
    factory: SessionFactory,
    settings: Settings,
    *,
    user_id: int,
    access: str = "access-0",
    refresh: str = "refresh-0",
    expires_in: timedelta | None = timedelta(days=28),
    username: str | None = "arc-tester",
) -> MalLink:
    """Write a linked ``mal_links`` row directly, with encrypted tokens."""
    async with factory() as session:
        link = MalLink(
            user_id=user_id,
            mal_username=username,
            access_token_enc=encrypt(settings, access),
            refresh_token_enc=encrypt(settings, refresh),
            expires_at=None if expires_in is None else datetime.now(UTC) + expires_in,
        )
        session.add(link)
        await session.commit()
        return link


async def make_anime(
    factory: SessionFactory,
    *,
    mal_id: int | None,
    anilist_id: int | None = None,
    title: str = "A Show",
) -> int:
    """A cached, *fresh* ``anime`` row; returns its internal id.

    ``refreshed_at`` is set so ``ensure_anime`` treats the row as current and
    never reaches for a catalogue source: these tests are about MyAnimeList,
    and a stray AniList fetch would make them about the network.

    A row needs at least one external id (``ck_anime_has_external_id``), so a
    show with no MAL id — the one Arc can never push — gets an AniList one.
    """
    async with factory() as session:
        anime = Anime(
            mal_id=mal_id,
            anilist_id=anilist_id,
            title_romaji=title,
            episodes=12,
            summary_source="anilist",
            detail_source="anilist",
            refreshed_at=datetime.now(UTC),
        )
        if anime.mal_id is None and anime.anilist_id is None:
            anime.anilist_id = 900_000 + abs(hash(title)) % 90_000
        session.add(anime)
        await session.commit()
        return anime.id


async def make_entry(
    factory: SessionFactory,
    *,
    user_id: int,
    anime_id: int,
    status: ListStatus = ListStatus.WATCHING,
    progress: int = 0,
    score: int | None = None,
    dirty: bool = False,
    updated_by: UpdatedBy = UpdatedBy.ARC,
    updated_at: datetime | None = None,
) -> None:
    """A ``list_entries`` row in a known state."""
    async with factory() as session:
        entry = ListEntry(
            user_id=user_id,
            anime_id=anime_id,
            status=status,
            progress=progress,
            score=score,
            mal_dirty=dirty,
            updated_by=updated_by,
        )
        if updated_at is not None:
            entry.updated_at = updated_at
        session.add(entry)
        await session.commit()


async def queue_write(
    factory: SessionFactory,
    *,
    user_id: int,
    anime_id: int,
    field: str,
    old: object = None,
    new: object = None,
    cause: MalWriteCause = MalWriteCause.MANUAL,
) -> int:
    """A ``pending`` write log row — the queue a ``mal_push`` job works from.

    The rows *are* the queue (:mod:`arc.services.mal.writelog`), so a test that
    wants a push to send something has to queue it, exactly as the user event
    would. ``cause`` is per row and is what the FR-M4 guards are decided from.
    """
    async with factory() as session:
        row = MalWriteLog(
            user_id=user_id,
            anime_id=anime_id,
            field=field,
            old_value=old,
            new_value=new,
            cause=cause,
            status=MalWriteStatus.PENDING,
        )
        session.add(row)
        await session.commit()
        return row.id


async def entry_of(factory: SessionFactory, *, user_id: int, anime_id: int) -> ListEntry | None:
    async with factory() as session:
        return await session.get(ListEntry, (user_id, anime_id))


async def link_of(factory: SessionFactory, user_id: int) -> MalLink | None:
    async with factory() as session:
        return await session.get(MalLink, user_id)


async def log_rows(factory: SessionFactory, user_id: int) -> list[MalWriteLog]:
    """This user's write log, oldest first."""
    async with factory() as session:
        rows = await session.scalars(
            select(MalWriteLog).where(MalWriteLog.user_id == user_id).order_by(MalWriteLog.id)
        )
        return list(rows.all())


__all__ = [
    "OTHER_EMAIL",
    "OTHER_PASSWORD",
    "USER_EMAIL",
    "USER_PASSWORD",
    "entry_of",
    "link_of",
    "link_user",
    "log_rows",
    "make_anime",
    "make_entry",
    "mal_settings",
    "queue_write",
]
