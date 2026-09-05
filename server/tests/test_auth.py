"""Login, sessions, the CSRF origin check, rate limiting, and bootstrap.

These commit for real against the test database (the ``api_*`` fixtures in
conftest) rather than using the rolled-back ``db_session``: a session cookie is
only meaningful across several requests, each of which gets its own database
session.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import select

from arc.api.auth import INVALID_CREDENTIALS, RATE_LIMITED
from arc.api.csrf import DENIED
from arc.api.deps import ADMIN_REQUIRED, NOT_AUTHENTICATED
from arc.config import Settings
from arc.core.security import MAX_PASSWORD_LENGTH, generate_token, hash_token
from arc.db import SessionFactory
from arc.models import Session, User, UserRole
from arc.services.auth import COOKIE_NAME, bootstrap_admin
from tests.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    ORIGIN,
    add_user,
    api_transport,
    login,
)

pytestmark = pytest.mark.pg

USER_EMAIL = "viewer@arc.test"
USER_PASSWORD = "viewerpassword"


def set_cookie_header(response: Response) -> str:
    """The raw ``Set-Cookie`` line for the session cookie."""
    value = optional_set_cookie_header(response)
    if value is None:
        raise AssertionError(
            f"no {COOKIE_NAME} cookie in {response.headers.get_list('set-cookie')}"
        )
    return value


def optional_set_cookie_header(response: Response) -> str | None:
    """The session cookie line, or ``None`` if the response set no cookie."""
    for value in response.headers.get_list("set-cookie"):
        if value.startswith(f"{COOKIE_NAME}="):
            return value
    return None


async def add_session(factory: SessionFactory, user_id: int, expires_at: datetime) -> str:
    """Plant a session row with an exact expiry and return its raw token."""
    token = generate_token()
    async with factory() as session:
        session.add(Session(id=hash_token(token), user_id=user_id, expires_at=expires_at))
        await session.commit()
    return token


# --- Login ------------------------------------------------------------------


async def test_login_returns_the_user_and_sets_a_hardened_cookie(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)

    response = await api_client.post(
        "/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == ADMIN_EMAIL
    assert body["role"] == "admin"
    assert body["timezone"] == "UTC"
    assert set(body) == {"id", "email", "role", "timezone", "created_at"}

    cookie = set_cookie_header(response)
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("SameSite=Lax", "SameSite=lax")
    assert "Path=/" in cookie
    # ENV=test, not prod: Secure would stop the cookie reaching a plain-http
    # dev server, and the whole login flow with it.
    assert "Secure" not in cookie
    assert api_client.cookies.get(COOKIE_NAME)


async def test_user_json_never_carries_the_password_hash(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    await login(api_client, ADMIN_EMAIL, ADMIN_PASSWORD)

    for path in ("/api/auth/me", "/api/users"):
        response = await api_client.get(path)
        assert response.status_code == 200, response.text
        assert "password_hash" not in response.text
        assert "$argon2" not in response.text


async def test_the_email_is_matched_case_insensitively(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    response = await api_client.post(
        "/api/auth/login", json={"email": "  VIEWER@ARC.test ", "password": USER_PASSWORD}
    )

    assert response.status_code == 200, response.text
    assert response.json()["email"] == USER_EMAIL


async def test_wrong_password_and_unknown_email_are_indistinguishable(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Anything else is a user-enumeration oracle."""
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    wrong_password = await api_client.post(
        "/api/auth/login", json={"email": USER_EMAIL, "password": "not-the-password"}
    )
    unknown_email = await api_client.post(
        "/api/auth/login", json={"email": "nobody@arc.test", "password": USER_PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"detail": INVALID_CREDENTIALS}
    assert COOKIE_NAME not in wrong_password.cookies


async def test_an_over_long_password_is_refused_without_echoing_it(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """No stored password is that long, so it is bad credentials, not a 422.

    A pydantic `max_length` would answer 422 with the rejected value in
    `input` — the caller's password, in the response body and in whatever
    keeps a copy of one.
    """
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    too_long = "z" * (MAX_PASSWORD_LENGTH + 1)

    response = await api_client.post(
        "/api/auth/login", json={"email": USER_EMAIL, "password": too_long}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_CREDENTIALS}
    assert too_long not in response.text


async def test_an_unknown_field_in_the_body_is_refused(api_client: AsyncClient) -> None:
    """`extra="forbid"`: a field the server does not know is a caller bug."""
    response = await api_client.post(
        "/api/auth/login",
        json={"email": USER_EMAIL, "password": USER_PASSWORD, "remember_me": True},
    )

    assert response.status_code == 422


async def test_an_inactive_user_cannot_log_in(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD, is_active=False)

    response = await api_client.post(
        "/api/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_CREDENTIALS}


# --- me / logout ------------------------------------------------------------


async def test_me_is_401_without_a_cookie(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json() == {"detail": NOT_AUTHENTICATED}


async def test_logout_clears_the_cookie_and_the_row(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    await login(api_client, USER_EMAIL, USER_PASSWORD)
    assert (await api_client.get("/api/auth/me")).status_code == 200

    response = await api_client.post("/api/auth/logout")

    assert response.status_code == 204
    assert not api_client.cookies.get(COOKIE_NAME)
    async with api_factory() as session:
        assert (await session.scalars(select(Session))).all() == []
    assert (await api_client.get("/api/auth/me")).status_code == 401


async def test_logout_without_a_session_is_still_204(api_client: AsyncClient) -> None:
    """A client tidying up after an expired cookie must not see an error."""
    assert (await api_client.post("/api/auth/logout")).status_code == 204


async def test_a_tampered_cookie_is_not_authenticated(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    await login(api_client, USER_EMAIL, USER_PASSWORD)

    original = api_client.cookies[COOKIE_NAME]
    api_client.cookies.set(COOKIE_NAME, original[:-1] + ("A" if original[-1] != "A" else "B"))

    response = await api_client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json() == {"detail": NOT_AUTHENTICATED}


async def test_an_expired_session_row_is_refused(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    token = generate_token()
    async with api_factory() as session:
        session.add(
            Session(
                id=hash_token(token),
                user_id=user.id,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()

    api_client.cookies.set(COOKIE_NAME, token)

    assert (await api_client.get("/api/auth/me")).status_code == 401


async def test_a_session_close_to_expiry_slides_forward(
    api_client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    """Daily use must never log anybody out (arc.services.auth.sessions)."""
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    token = generate_token()
    nearly_gone = datetime.now(UTC) + timedelta(hours=1)
    async with api_factory() as session:
        session.add(Session(id=hash_token(token), user_id=user.id, expires_at=nearly_gone))
        await session.commit()

    api_client.cookies.set(COOKIE_NAME, token)
    assert (await api_client.get("/api/auth/me")).status_code == 200

    async with api_factory() as session:
        row = await session.get(Session, hash_token(token))
        assert row is not None
        assert row.expires_at > nearly_gone
        expected = datetime.now(UTC) + settings.session_ttl
        assert abs((row.expires_at - expected).total_seconds()) < 60


async def test_a_fresh_session_is_not_rewritten_on_every_request(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The extension is worth one UPDATE a day, not one per request."""
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    await login(api_client, USER_EMAIL, USER_PASSWORD)
    key = hash_token(api_client.cookies[COOKIE_NAME])

    async with api_factory() as session:
        row = await session.get(Session, key)
        assert row is not None
        before = row.expires_at

    assert (await api_client.get("/api/auth/me")).status_code == 200

    async with api_factory() as session:
        row = await session.get(Session, key)
        assert row is not None
        assert row.expires_at == before


async def test_extending_a_session_re_issues_the_cookie(
    api_client: AsyncClient, api_factory: SessionFactory, settings: Settings
) -> None:
    """The row and the cookie have to slide together.

    Extending only the row leaves the browser dropping the cookie at 30 days
    after *login*, which is sliding expiry that does not slide.
    """
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    token = await add_session(api_factory, user.id, datetime.now(UTC) + timedelta(days=10))
    api_client.cookies.set(COOKIE_NAME, token)

    response = await api_client.get("/api/auth/me")

    assert response.status_code == 200, response.text
    cookie = set_cookie_header(response)
    assert f"{COOKIE_NAME}={token}" in cookie, "the same token, with a fresh lifetime"
    assert f"Max-Age={int(settings.session_ttl.total_seconds())}" in cookie
    assert "HttpOnly" in cookie


async def test_a_session_that_was_not_extended_sets_no_cookie(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """One Set-Cookie a day, not one per request — the row is not rewritten
    either (see above), so there is nothing to tell the browser."""
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    token = await add_session(
        api_factory, user.id, datetime.now(UTC) + timedelta(days=29, hours=12)
    )
    api_client.cookies.set(COOKIE_NAME, token)

    response = await api_client.get("/api/auth/me")

    assert response.status_code == 200, response.text
    assert optional_set_cookie_header(response) is None


async def test_logging_in_again_keeps_the_new_cookie(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The refresh must never overwrite a cookie the handler just issued."""
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    stale = await add_session(api_factory, user.id, datetime.now(UTC) + timedelta(days=10))
    api_client.cookies.set(COOKIE_NAME, stale)

    response = await api_client.post(
        "/api/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD}
    )

    assert response.status_code == 200, response.text
    issued = set_cookie_header(response)
    assert f"{COOKIE_NAME}={stale}" not in issued, "the handler's token, not the stale one"
    assert len(response.headers.get_list("set-cookie")) == 1, "and only one of them"


async def test_deactivating_a_user_ends_their_open_session(
    api_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    await login(api_client, USER_EMAIL, USER_PASSWORD)
    assert (await api_client.get("/api/auth/me")).status_code == 200

    await add_user(api_factory, ADMIN_EMAIL, ADMIN_PASSWORD, role=UserRole.ADMIN)
    async with api_transport(api_app) as admin:
        await login(admin, ADMIN_EMAIL, ADMIN_PASSWORD)
        patched = await admin.patch(f"/api/users/{user.id}", json={"is_active": False})
        assert patched.status_code == 200, patched.text

    assert (await api_client.get("/api/auth/me")).status_code == 401


# --- CSRF -------------------------------------------------------------------


async def test_a_write_without_an_origin_is_refused(api_app: FastAPI) -> None:
    async with api_transport(api_app, origin=None) as client:
        response = await client.post(
            "/api/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD}
        )

    assert response.status_code == 403
    assert response.json() == {"detail": DENIED}


async def test_a_write_from_a_foreign_origin_is_refused(api_app: FastAPI) -> None:
    async with api_transport(api_app, origin="https://evil.example") as client:
        response = await client.post(
            "/api/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD}
        )

    assert response.status_code == 403
    assert response.json() == {"detail": DENIED}


async def test_a_referer_stands_in_for_a_missing_origin(api_app: FastAPI) -> None:
    async with api_transport(api_app, origin=None) as client:
        response = await client.post(
            "/api/auth/logout", headers={"Referer": "http://localhost:5173/login"}
        )

    assert response.status_code == 204


async def test_reads_need_no_origin(api_app: FastAPI) -> None:
    """GET changes nothing; requiring a header there would break every link."""
    async with api_transport(api_app, origin=None) as client:
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/api/auth/me")).status_code == 401


async def test_an_error_from_an_allowed_origin_still_carries_cors_headers(
    api_app: FastAPI,
) -> None:
    """CORS is the outermost middleware, so every response goes through it.

    Without that ordering a browser sees "blocked by CORS" instead of the 401
    the server actually sent, and the client cannot tell "log in again" from
    "the server is misconfigured".
    """
    async with api_transport(api_app) as client:
        response = await client.post("/api/jobs", json={"type": "noop"})

    assert response.status_code == 401
    assert response.json() == {"detail": NOT_AUTHENTICATED}
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_a_refused_origin_gets_no_cors_grant(api_app: FastAPI) -> None:
    """403, and *not* a header telling the page it may read the answer."""
    async with api_transport(api_app, origin="https://evil.example") as client:
        response = await client.post("/api/auth/logout")

    assert response.status_code == 403
    assert response.json() == {"detail": DENIED}
    assert "access-control-allow-origin" not in response.headers


# --- Rate limiting ----------------------------------------------------------


async def test_the_eleventh_attempt_from_one_ip_is_rate_limited(
    api_client: AsyncClient,
) -> None:
    """Ten per IP, per the default budget. Distinct addresses, so it is the
    IP budget being hit and not the tighter per-email one."""
    for attempt in range(10):
        response = await api_client.post(
            "/api/auth/login",
            json={"email": f"nobody{attempt}@arc.test", "password": "whatever-long"},
        )
        assert response.status_code == 401, f"attempt {attempt}: {response.text}"

    blocked = await api_client.post(
        "/api/auth/login", json={"email": "nobody10@arc.test", "password": "whatever-long"}
    )

    assert blocked.status_code == 429
    assert blocked.json() == {"detail": RATE_LIMITED}
    assert int(blocked.headers["retry-after"]) > 0


async def test_the_per_email_budget_is_tighter_than_the_per_ip_one(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """Five per address, so a spread-out attempt cannot pile onto one account."""
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    for attempt in range(5):
        response = await api_client.post(
            "/api/auth/login", json={"email": USER_EMAIL, "password": "wrong-password"}
        )
        assert response.status_code == 401, f"attempt {attempt}: {response.text}"

    blocked = await api_client.post(
        "/api/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD}
    )

    assert blocked.status_code == 429, "a correct password must not bypass the budget"
    assert "retry-after" in blocked.headers


# --- Access control on the rest of the API ----------------------------------


async def test_jobs_needs_a_session_and_the_admin_role(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The M2 definition of done, applied to the router M1 left open."""
    anonymous = await api_client.get("/api/jobs")
    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": NOT_AUTHENTICATED}

    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    await login(api_client, USER_EMAIL, USER_PASSWORD)

    as_user = await api_client.get("/api/jobs")
    assert as_user.status_code == 403
    assert as_user.json() == {"detail": ADMIN_REQUIRED}

    posted = await api_client.post("/api/jobs", json={"type": "noop"})
    assert posted.status_code == 403


async def test_health_stays_public(api_client: AsyncClient) -> None:
    assert (await api_client.get("/api/health")).status_code == 200


# --- Bootstrap --------------------------------------------------------------


async def test_bootstrap_creates_the_admin_once_and_never_overwrites(
    api_factory: SessionFactory, settings: Settings
) -> None:
    configured = settings.model_copy(
        update={
            "bootstrap_admin_email": "  Boot@Arc.Test ",
            "bootstrap_admin_password": SecretStr("bootstrapped-123"),
        }
    )

    created = await bootstrap_admin(api_factory, configured)
    assert created is not None
    assert created.email == "boot@arc.test", "stored lowercased"
    assert created.role is UserRole.ADMIN
    first_hash = created.password_hash

    again = await bootstrap_admin(api_factory, configured)
    assert again is None, "a second run must create nothing"

    async with api_factory() as session:
        rows = list((await session.scalars(select(User))).all())
    assert len(rows) == 1
    assert rows[0].password_hash == first_hash, "an existing password is never reset"


async def test_two_simultaneous_bootstraps_create_exactly_one_admin(
    api_factory: SessionFactory, settings: Settings
) -> None:
    """`--workers N`, or a container restarting alongside its replacement.

    Both processes look, both see nothing, both insert. The unique index
    settles it, and the loser must treat "somebody else made it" as the
    outcome it wanted rather than as an exception out of a lifespan.
    """
    configured = settings.model_copy(
        update={
            "bootstrap_admin_email": "boot@arc.test",
            "bootstrap_admin_password": SecretStr("bootstrapped-123"),
        }
    )

    results = await asyncio.gather(
        bootstrap_admin(api_factory, configured),
        bootstrap_admin(api_factory, configured),
        return_exceptions=True,
    )

    assert not any(isinstance(result, BaseException) for result in results), results
    assert sum(result is not None for result in results) == 1, "exactly one creator"

    async with api_factory() as session:
        rows = list((await session.scalars(select(User))).all())
    assert len(rows) == 1
    assert rows[0].email == "boot@arc.test"
    assert rows[0].role is UserRole.ADMIN


async def test_bootstrap_does_nothing_when_the_password_is_empty(
    api_factory: SessionFactory, settings: Settings
) -> None:
    """The shipped default in .env.example. A no-op, not a policy failure."""
    blank = settings.model_copy(
        update={
            "bootstrap_admin_email": "boot@arc.test",
            "bootstrap_admin_password": SecretStr(""),
        }
    )

    assert await bootstrap_admin(api_factory, blank) is None
    async with api_factory() as session:
        assert (await session.scalars(select(User))).all() == []


async def test_bootstrap_does_nothing_when_unconfigured(
    api_factory: SessionFactory, settings: Settings
) -> None:
    bare = settings.model_copy(
        update={"bootstrap_admin_email": None, "bootstrap_admin_password": None}
    )

    assert await bootstrap_admin(api_factory, bare) is None
    async with api_factory() as session:
        assert (await session.scalars(select(User))).all() == []


async def test_bootstrap_refuses_a_password_the_policy_rejects(
    api_factory: SessionFactory, settings: Settings
) -> None:
    """Logged and skipped, not raised: the API must still start."""
    weak = settings.model_copy(
        update={
            "bootstrap_admin_email": "boot@arc.test",
            "bootstrap_admin_password": SecretStr("short"),
        }
    )

    assert await bootstrap_admin(api_factory, weak) is None
    async with api_factory() as session:
        assert (await session.scalars(select(User))).all() == []
