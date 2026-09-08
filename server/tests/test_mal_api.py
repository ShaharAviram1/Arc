"""The MyAnimeList endpoints over HTTP (spec §4.7).

Three groups. The **handshake** (``/link`` and ``/callback``) is the security
half: an authorize URL MAL will accept, a state nobody else can use, tokens
that are unreadable in the database. The **status and log** routes are what
the client's MAL page renders. **Revert** is FR-M5's promise that every write
can be taken back, and most of its tests are about when it must refuse.

Nothing here reaches MyAnimeList except through ``tests/mal_api_mock.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.api.mal import ERROR_EXCHANGE, ERROR_STATE, NEWER_QUEUED
from arc.config import Settings
from arc.db import SessionFactory
from arc.models import (
    Job,
    ListEntry,
    ListStatus,
    MalLink,
    MalWriteCause,
    MalWriteLog,
    MalWriteStatus,
)
from arc.services.mal import oauth
from arc.services.mal.names import IMPORT, PUSH, PUSH_ALL
from arc.services.mal.sync import SKIP_DISCONNECTED
from tests.conftest import add_user, api_transport, login
from tests.mal_api_mock import GOOD_CODE, MAL_USERNAME, FakeMalApi
from tests.mal_helpers import (
    OTHER_EMAIL,
    OTHER_PASSWORD,
    USER_EMAIL,
    USER_PASSWORD,
    link_of,
    link_user,
    log_rows,
    make_anime,
    make_entry,
    mal_settings,  # noqa: F401  (installs the ``settings`` override: MAL configured)
    queue_write,
)

pytestmark = pytest.mark.pg


@pytest.fixture
def mal(monkeypatch: pytest.MonkeyPatch) -> FakeMalApi:
    return FakeMalApi().install(monkeypatch)


@pytest.fixture
async def user_id(api_factory: SessionFactory) -> int:
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    return user.id


@pytest.fixture
async def client(api_app: FastAPI, user_id: int) -> AsyncClient:
    return await login(api_transport(api_app), USER_EMAIL, USER_PASSWORD)


async def _jobs(factory: SessionFactory, job_type: str) -> list[Job]:
    async with factory() as session:
        rows = await session.scalars(select(Job).where(Job.type == job_type).order_by(Job.id))
        return list(rows.all())


# --- Status -----------------------------------------------------------------


async def test_status_reports_an_unlinked_but_configured_server(client: AsyncClient) -> None:
    response = await client.get("/api/mal/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["linked"] is False
    assert body["needs_relink"] is False
    assert body["mal_username"] is None
    assert (body["pending_writes"], body["failed_writes"]) == (0, 0)


async def test_status_says_unconfigured_without_a_client_id(
    api_factory: SessionFactory, test_database_url: str
) -> None:
    """A deployment with no MAL app must not offer a link button."""
    from arc.main import create_app

    bare = Settings(env="test", database_url=test_database_url, _env_file=None)  # type: ignore[call-arg]
    app = create_app(bare)
    app.state.session_factory = api_factory
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    async with api_transport(app) as anon:
        signed_in = await login(anon, USER_EMAIL, USER_PASSWORD)
        assert (await signed_in.get("/api/mal/status")).json()["configured"] is False
        assert (await signed_in.post("/api/mal/link")).status_code == 503


async def test_status_counts_pending_and_failed_writes(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """Both counts come off the log: the queued rows are what is owed (FR-M6)."""
    await link_user(api_factory, settings, user_id=user_id)
    queued = await make_anime(api_factory, mal_id=101)
    clean = await make_anime(api_factory, mal_id=102)
    await make_entry(api_factory, user_id=user_id, anime_id=queued, dirty=True)
    await make_entry(api_factory, user_id=user_id, anime_id=clean, dirty=False)
    await queue_write(
        api_factory, user_id=user_id, anime_id=queued, field="status", old=None, new="watching"
    )
    async with api_factory() as session:
        session.add(
            MalWriteLog(
                user_id=user_id,
                anime_id=clean,
                field="score",
                old_value=5,
                new_value=7,
                cause=MalWriteCause.MANUAL,
                status=MalWriteStatus.FAILED,
                error="upstream said no",
            )
        )
        await session.commit()

    body = (await client.get("/api/mal/status")).json()
    assert body["pending_writes"] == 1
    assert body["failed_writes"] == 1


async def test_pending_writes_counts_fields_not_shows(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """One show whose status, score and progress all moved owes three writes."""
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=103)
    await make_entry(api_factory, user_id=user_id, anime_id=anime_id, dirty=True)
    for field, new in (("status", "watching"), ("score", 8), ("progress", 3)):
        await queue_write(
            api_factory, user_id=user_id, anime_id=anime_id, field=field, old=None, new=new
        )

    assert (await client.get("/api/mal/status")).json()["pending_writes"] == 3


async def test_the_endpoints_require_a_session(api_client: AsyncClient) -> None:
    assert (await api_client.get("/api/mal/status")).status_code == 401
    assert (await api_client.post("/api/mal/link")).status_code == 401
    assert (await api_client.get("/api/mal/log")).status_code == 401


# --- Link -------------------------------------------------------------------


async def test_link_returns_an_authorize_url_mal_would_accept(
    client: AsyncClient, settings: Settings, user_id: int
) -> None:
    response = await client.post("/api/mal/link")

    assert response.status_code == 200, response.text
    url = response.json()["authorize_url"]
    assert url.startswith(f"{settings.mal_oauth_url}/authorize?")
    assert "code_challenge_method=plain" in url
    # The state is the whole session: it decrypts to the caller and to the
    # verifier that will be presented at the token endpoint.
    state = url.split("state=")[1].split("&")[0]
    decoded = oauth.decode_state(settings, state)
    assert decoded.user_id == user_id
    assert decoded.verifier in url


async def test_linking_twice_is_a_conflict(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    await link_user(api_factory, settings, user_id=user_id)
    assert (await client.post("/api/mal/link")).status_code == 409


async def test_a_link_that_needs_reauthorising_may_be_relinked(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """The 409 above must not lock a user out of fixing a dead refresh token."""
    await link_user(api_factory, settings, user_id=user_id)
    async with api_factory() as session:
        link = await session.get(MalLink, user_id)
        assert link is not None
        link.access_token_enc = ""
        link.refresh_token_enc = ""
        await session.commit()

    assert (await client.post("/api/mal/link")).status_code == 200
    assert (await client.get("/api/mal/status")).json()["needs_relink"] is True


# --- Callback ---------------------------------------------------------------


def _state(settings: Settings, user_id: int, *, age: timedelta = timedelta()) -> str:
    return oauth.encode_state(
        settings, user_id=user_id, verifier="v" * 43, now=datetime.now(UTC) - age
    )


async def test_the_callback_stores_encrypted_tokens_and_queues_the_import(
    client: AsyncClient,
    api_factory: SessionFactory,
    settings: Settings,
    user_id: int,
    mal: FakeMalApi,
) -> None:
    response = await client.get(
        "/api/mal/callback",
        params={"code": GOOD_CODE, "state": _state(settings, user_id)},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{settings.public_url}/mal?linked=1"

    link = await link_of(api_factory, user_id)
    assert link is not None
    assert link.mal_username == MAL_USERNAME
    # Encrypted at rest (spec §7): the column must not contain the token.
    assert mal.access_token not in link.access_token_enc
    assert mal.refresh_token not in link.refresh_token_enc
    assert link.expires_at is not None and link.expires_at > datetime.now(UTC)

    # The exchange used PKCE, the registered redirect, and the app's secret.
    form = mal.token_calls[0]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == GOOD_CODE
    assert form["code_verifier"] == "v" * 43
    assert form["redirect_uri"] == settings.mal_redirect_uri
    assert form["client_secret"] == settings.require("mal_client_secret")

    assert [job.payload["user_id"] for job in await _jobs(api_factory, IMPORT)] == [user_id]


async def test_a_state_issued_to_another_user_is_refused(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, mal: FakeMalApi
) -> None:
    """Encryption proves Arc issued the state, not that this browser owns it."""
    other = await add_user(api_factory, OTHER_EMAIL, OTHER_PASSWORD)

    response = await client.get(
        "/api/mal/callback",
        params={"code": GOOD_CODE, "state": _state(settings, other.id)},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert await link_of(api_factory, other.id) is None
    assert mal.token_calls == []


async def test_an_expired_state_redirects_with_an_error(
    client: AsyncClient, settings: Settings, user_id: int, mal: FakeMalApi
) -> None:
    stale = _state(settings, user_id, age=timedelta(seconds=oauth.STATE_TTL_SECONDS + 60))

    response = await client.get(
        "/api/mal/callback",
        params={"code": GOOD_CODE, "state": stale},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"].endswith(f"/mal?error={ERROR_STATE}")
    assert mal.token_calls == []


async def test_a_denied_authorisation_comes_back_as_an_error(
    client: AsyncClient, api_factory: SessionFactory, user_id: int
) -> None:
    """ "Deny" on MyAnimeList's page is a normal outcome, not a 500."""
    response = await client.get(
        "/api/mal/callback", params={"error": "access_denied"}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"].endswith("/mal?error=access_denied")
    assert await link_of(api_factory, user_id) is None


async def test_a_refused_token_exchange_redirects_and_stores_nothing(
    client: AsyncClient,
    api_factory: SessionFactory,
    settings: Settings,
    user_id: int,
    mal: FakeMalApi,
) -> None:
    mal.exchange_fails = True

    response = await client.get(
        "/api/mal/callback",
        params={"code": GOOD_CODE, "state": _state(settings, user_id)},
        follow_redirects=False,
    )

    assert response.headers["location"].endswith(f"/mal?error={ERROR_EXCHANGE}")
    assert await link_of(api_factory, user_id) is None


# --- Unlink -----------------------------------------------------------------


async def test_unlink_drops_the_tokens_and_keeps_the_audit_trail(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=303)
    await make_entry(api_factory, user_id=user_id, anime_id=anime_id, dirty=True)
    async with api_factory() as session:
        session.add(
            MalWriteLog(
                user_id=user_id,
                anime_id=anime_id,
                field="progress",
                old_value=1,
                new_value=2,
                cause=MalWriteCause.WATCH,
                status=MalWriteStatus.OK,
            )
        )
        await session.commit()

    assert (await client.delete("/api/mal/link")).status_code == 204

    assert await link_of(api_factory, user_id) is None
    # The log is evidence, not a preference: it survives (FR-M5). So does the
    # dirty flag, which is the true statement that Arc holds unpushed changes.
    assert len(await log_rows(api_factory, user_id)) == 1
    async with api_factory() as session:
        entry = await session.get(ListEntry, (user_id, anime_id))
        assert entry is not None and entry.mal_dirty is True

    assert (await client.delete("/api/mal/link")).status_code == 404


async def test_unlink_abandons_the_queued_writes_and_leaves_the_failures(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """There is nothing left to send them to, and nothing coming back for them.

    A row that had already *failed* is a different thing: it records an attempt
    that really happened, and unlinking is not a licence to rewrite it (FR-M5).
    """
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=304)
    await make_entry(api_factory, user_id=user_id, anime_id=anime_id, dirty=True)
    await queue_write(
        api_factory, user_id=user_id, anime_id=anime_id, field="status", old=None, new="watching"
    )
    async with api_factory() as session:
        session.add(
            MalWriteLog(
                user_id=user_id,
                anime_id=anime_id,
                field="score",
                old_value=5,
                new_value=7,
                cause=MalWriteCause.MANUAL,
                status=MalWriteStatus.FAILED,
                error="upstream said no",
            )
        )
        await session.commit()

    assert (await client.delete("/api/mal/link")).status_code == 204

    rows = {row.field: row for row in await log_rows(api_factory, user_id)}
    assert (rows["status"].status, rows["status"].error) == (
        MalWriteStatus.SKIPPED,
        SKIP_DISCONNECTED,
    )
    assert (rows["score"].status, rows["score"].error) == (
        MalWriteStatus.FAILED,
        "upstream said no",
    )
    body = (await client.get("/api/mal/status")).json()
    assert body["linked"] is False and body["pending_writes"] == 0
    assert body["failed_writes"] == 1


# --- Import and push buttons ------------------------------------------------


async def test_the_import_and_push_buttons_queue_deduplicated_jobs(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    await link_user(api_factory, settings, user_id=user_id)

    assert (await client.post("/api/mal/import")).status_code == 202
    assert (await client.post("/api/mal/import")).status_code == 202
    assert (await client.post("/api/mal/push")).status_code == 202
    assert (await client.post("/api/mal/push")).status_code == 202

    assert len(await _jobs(api_factory, IMPORT)) == 1
    assert len(await _jobs(api_factory, PUSH_ALL)) == 1


async def test_the_buttons_need_a_link(client: AsyncClient) -> None:
    assert (await client.post("/api/mal/import")).status_code == 404
    assert (await client.post("/api/mal/push")).status_code == 404


# --- The log and revert -----------------------------------------------------


async def _write(
    factory: SessionFactory,
    *,
    user_id: int,
    anime_id: int,
    field: str = "score",
    old: object = 5,
    new: object = 8,
    status: MalWriteStatus = MalWriteStatus.OK,
    cause: MalWriteCause = MalWriteCause.MANUAL,
) -> int:
    async with factory() as session:
        row = MalWriteLog(
            user_id=user_id,
            anime_id=anime_id,
            field=field,
            old_value=old,
            new_value=new,
            cause=cause,
            status=status,
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_the_log_lists_writes_newest_first_with_their_show(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=404, title="Frieren")
    await make_entry(api_factory, user_id=user_id, anime_id=anime_id)
    first = await _write(api_factory, user_id=user_id, anime_id=anime_id, field="progress", new=2)
    second = await _write(api_factory, user_id=user_id, anime_id=anime_id, field="score")

    rows = (await client.get("/api/mal/log")).json()

    assert [row["id"] for row in rows] == [second, first]
    assert rows[0]["anime"]["title"]["preferred"] == "Frieren"
    assert rows[0]["anime"]["list_status"] == "watching"
    assert (rows[0]["old_value"], rows[0]["new_value"]) == (5, 8)
    # Both are the newest successful write to their own field, so both stand.
    assert [row["revertible"] for row in rows] == [True, True]


async def test_the_log_can_be_filtered_by_status(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=405)
    await _write(api_factory, user_id=user_id, anime_id=anime_id, field="progress")
    failed = await _write(
        api_factory, user_id=user_id, anime_id=anime_id, status=MalWriteStatus.FAILED
    )

    rows = (await client.get("/api/mal/log", params={"status": "failed"})).json()
    assert [row["id"] for row in rows] == [failed]
    assert rows[0]["revertible"] is False


async def test_a_superseded_write_is_no_longer_revertible(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """Reverting an older value would silently discard the newer one."""
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=406)
    older = await _write(api_factory, user_id=user_id, anime_id=anime_id, old=3, new=5)
    await _write(api_factory, user_id=user_id, anime_id=anime_id, old=5, new=8)

    rows = {row["id"]: row for row in (await client.get("/api/mal/log")).json()}
    assert rows[older]["revertible"] is False
    assert (await client.post(f"/api/mal/log/{older}/revert")).status_code == 409


async def test_a_field_with_a_queued_change_is_not_revertible(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """The newest successful write is not the newest thing the user asked for.

    A change made while the last one was still on its way — or still being
    retried — is newer than any of them, and reverting on top of it would
    supersede it without ever saying so.
    """
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=408)
    await make_entry(api_factory, user_id=user_id, anime_id=anime_id, score=8)
    written = await _write(api_factory, user_id=user_id, anime_id=anime_id, old=5, new=8)
    await queue_write(api_factory, user_id=user_id, anime_id=anime_id, field="score", old=8, new=10)

    rows = {row["id"]: row for row in (await client.get("/api/mal/log")).json()}
    assert rows[written]["revertible"] is False

    refused = await client.post(f"/api/mal/log/{written}/revert")
    assert refused.status_code == 409
    assert refused.json()["detail"] == NEWER_QUEUED
    # …and the queued change is still queued: nothing was written over it.
    async with api_factory() as session:
        entry = await session.get(ListEntry, (user_id, anime_id))
        assert entry is not None and entry.score == 8


async def test_revert_writes_the_old_value_back_and_queues_the_push(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=407)
    await make_entry(api_factory, user_id=user_id, anime_id=anime_id, score=8, dirty=False)
    row_id = await _write(api_factory, user_id=user_id, anime_id=anime_id, old=5, new=8)

    response = await client.post(f"/api/mal/log/{row_id}/revert")

    assert response.status_code == 202, response.text
    assert response.json()["value"] == 5
    async with api_factory() as session:
        entry = await session.get(ListEntry, (user_id, anime_id))
        assert entry is not None
        assert entry.score == 5
        assert entry.mal_dirty is True
    assert len(await _jobs(api_factory, PUSH)) == 1
    # The cause is on the *row*, not on the job: it is what tells the push that
    # putting a value back is a statement, not an automatic event (FR-M4).
    queued = [row for row in await log_rows(api_factory, user_id) if row.id != row_id]
    assert [(row.field, row.old_value, row.new_value, row.cause, row.status) for row in queued] == [
        ("score", 8, 5, MalWriteCause.REVERT, MalWriteStatus.PENDING)
    ]


async def test_reverting_a_removal_puts_the_show_back_on_the_list(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """``old_value`` is the whole entry when the field is ``status`` (FR-M5)."""
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=408)
    row_id = await _write(
        api_factory,
        user_id=user_id,
        anime_id=anime_id,
        field="status",
        old=ListStatus.WATCHING.value,
        new=None,
    )

    assert (await client.post(f"/api/mal/log/{row_id}/revert")).status_code == 202

    async with api_factory() as session:
        entry = await session.get(ListEntry, (user_id, anime_id))
        assert entry is not None and entry.status is ListStatus.WATCHING


async def test_a_failed_write_cannot_be_reverted(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """It never landed, so there is nothing on MyAnimeList to put back."""
    await link_user(api_factory, settings, user_id=user_id)
    anime_id = await make_anime(api_factory, mal_id=409)
    row_id = await _write(
        api_factory, user_id=user_id, anime_id=anime_id, status=MalWriteStatus.FAILED
    )
    assert (await client.post(f"/api/mal/log/{row_id}/revert")).status_code == 409


async def test_reverting_needs_a_link_and_another_users_row_is_invisible(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    anime_id = await make_anime(api_factory, mal_id=410)
    row_id = await _write(api_factory, user_id=user_id, anime_id=anime_id)
    # Not linked yet: there is nothing to write the reverted value to.
    assert (await client.post(f"/api/mal/log/{row_id}/revert")).status_code == 409

    other = await add_user(api_factory, OTHER_EMAIL, OTHER_PASSWORD)
    theirs = await _write(api_factory, user_id=other.id, anime_id=anime_id)
    await link_user(api_factory, settings, user_id=user_id)
    # 404 rather than 403: a 403 would tell the caller which ids exist.
    assert (await client.post(f"/api/mal/log/{theirs}/revert")).status_code == 404
    assert (await client.post("/api/mal/log/999999/revert")).status_code == 404


# --- The show page badge ----------------------------------------------------


async def test_the_show_page_reports_the_sync_state(
    client: AsyncClient, api_factory: SessionFactory, settings: Settings, user_id: int
) -> None:
    """The badge is derived from the log rows, which are the queue (FR-M6)."""
    anime_id = await make_anime(api_factory, mal_id=411)
    await make_entry(api_factory, user_id=user_id, anime_id=anime_id, dirty=True)
    await queue_write(
        api_factory, user_id=user_id, anime_id=anime_id, field="progress", old=0, new=1
    )

    unlinked = (await client.get(f"/api/anime/{anime_id}")).json()
    assert unlinked["list_entry"]["mal_sync"]["state"] == "unlinked"

    await link_user(api_factory, settings, user_id=user_id)
    pending = (await client.get(f"/api/anime/{anime_id}")).json()
    assert pending["list_entry"]["mal_sync"]["state"] == "pending"

    await _write(
        api_factory,
        user_id=user_id,
        anime_id=anime_id,
        status=MalWriteStatus.FAILED,
    )
    failed = (await client.get(f"/api/anime/{anime_id}")).json()
    assert failed["list_entry"]["mal_sync"]["state"] == "failed"
