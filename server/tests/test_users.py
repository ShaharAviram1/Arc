"""Account administration: listing, enabling, and the self-lockout guard."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from arc.api.deps import ADMIN_REQUIRED, NOT_AUTHENTICATED
from arc.api.users import LAST_ADMIN, NO_SELF_DEACTIVATE, NO_SELF_DEMOTE, USER_NOT_FOUND
from arc.db import SessionFactory
from arc.models import User, UserRole
from arc.services.auth import count_active_admins, lock_admin_changes
from tests.conftest import ADMIN_EMAIL, add_user, api_transport, login

pytestmark = pytest.mark.pg

USER_EMAIL = "member@arc.test"
USER_PASSWORD = "member-password"
SECOND_ADMIN_EMAIL = "second@arc.test"


async def me(client: AsyncClient) -> dict[str, object]:
    response = await client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


# --- Listing ----------------------------------------------------------------


async def test_the_listing_shows_every_account_with_its_active_flag(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    await add_user(api_factory, "off@arc.test", USER_PASSWORD, is_active=False)

    response = await admin_client.get("/api/users")

    assert response.status_code == 200
    rows = response.json()
    assert [row["email"] for row in rows] == [ADMIN_EMAIL, USER_EMAIL, "off@arc.test"]
    assert set(rows[0]) == {"id", "email", "role", "timezone", "created_at", "is_active"}
    assert [row["is_active"] for row in rows] == [True, True, False]


async def test_only_an_admin_may_see_or_change_accounts(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anonymous = await api_client.get("/api/users")
    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": NOT_AUTHENTICATED}

    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)
    await login(api_client, USER_EMAIL, USER_PASSWORD)

    listed = await api_client.get("/api/users")
    assert listed.status_code == 403
    assert listed.json() == {"detail": ADMIN_REQUIRED}

    promoted = await api_client.patch(f"/api/users/{user.id}", json={"role": "admin"})
    assert promoted.status_code == 403, "a user must not be able to promote themselves"


# --- Patching ---------------------------------------------------------------


async def test_an_admin_can_deactivate_and_reinstate_somebody_else(
    admin_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    off = await admin_client.patch(f"/api/users/{user.id}", json={"is_active": False})
    assert off.status_code == 200, off.text
    assert off.json()["is_active"] is False

    async with api_transport(api_app) as member:
        refused = await member.post(
            "/api/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD}
        )
        assert refused.status_code == 401

    on = await admin_client.patch(f"/api/users/{user.id}", json={"is_active": True})
    assert on.status_code == 200
    assert on.json()["is_active"] is True

    async with api_transport(api_app) as member:
        await login(member, USER_EMAIL, USER_PASSWORD)


async def test_promoting_a_user_takes_effect_on_their_next_request(
    admin_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    async with api_transport(api_app) as member:
        await login(member, USER_EMAIL, USER_PASSWORD)
        assert (await member.get("/api/users")).status_code == 403

        promoted = await admin_client.patch(f"/api/users/{user.id}", json={"role": "admin"})
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "admin"

        assert (await member.get("/api/users")).status_code == 200
        assert (await me(member))["role"] == "admin"


async def test_an_omitted_field_is_left_alone(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD, role=UserRole.ADMIN)

    response = await admin_client.patch(f"/api/users/{user.id}", json={"is_active": False})

    assert response.status_code == 200
    assert response.json()["role"] == "admin", "role was not in the body"


async def test_an_admin_cannot_lock_themselves_out(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The last admin deactivating themselves is a database-console recovery."""
    admin = await me(admin_client)

    deactivated = await admin_client.patch(f"/api/users/{admin['id']}", json={"is_active": False})
    assert deactivated.status_code == 409
    assert deactivated.json() == {"detail": NO_SELF_DEACTIVATE}

    demoted = await admin_client.patch(f"/api/users/{admin['id']}", json={"role": "user"})
    assert demoted.status_code == 409
    assert demoted.json() == {"detail": NO_SELF_DEMOTE}

    # Nothing was written by either attempt.
    async with api_factory() as session:
        row = await session.get(User, int(str(admin["id"])))
        assert row is not None
        assert row.is_active is True
        assert row.role is UserRole.ADMIN


async def test_an_admin_may_still_patch_their_own_harmless_fields(
    admin_client: AsyncClient,
) -> None:
    admin = await me(admin_client)

    response = await admin_client.patch(
        f"/api/users/{admin['id']}", json={"is_active": True, "role": "admin"}
    )

    assert response.status_code == 200


async def test_another_admin_may_demote_them(
    admin_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    """The rule is about self-lockout, not about admins being untouchable."""
    admin = await me(admin_client)
    await add_user(api_factory, "second@arc.test", USER_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(api_app) as other:
        await login(other, "second@arc.test", USER_PASSWORD)
        response = await other.patch(f"/api/users/{admin['id']}", json={"role": "user"})

    assert response.status_code == 200
    assert response.json()["role"] == "user"


# --- The last active admin --------------------------------------------------


async def test_an_admin_may_still_remove_the_only_other_admin(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """The guard is "never zero", not "never fewer": one is a fine number."""
    other = await add_user(api_factory, SECOND_ADMIN_EMAIL, USER_PASSWORD, role=UserRole.ADMIN)

    deactivated = await admin_client.patch(f"/api/users/{other.id}", json={"is_active": False})
    assert deactivated.status_code == 200, deactivated.text

    reinstated = await admin_client.patch(f"/api/users/{other.id}", json={"is_active": True})
    assert reinstated.status_code == 200

    demoted = await admin_client.patch(f"/api/users/{other.id}", json={"role": "user"})
    assert demoted.status_code == 200

    async with api_factory() as session:
        assert await count_active_admins(session) == 1


async def wait_for_a_blocked_advisory_lock(factory: SessionFactory) -> None:
    """Block until some other transaction is queued on the admin lock.

    Polling ``pg_locks`` rather than sleeping a guessed interval: the test
    below depends on a request having got *past* authentication and *stuck* at
    the lock, and a sleep that is occasionally too short is a test that
    occasionally asserts something else.
    """
    for _ in range(200):
        async with factory() as probe:
            waiting = await probe.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no transaction ever queued on the advisory lock")


async def test_the_last_active_admin_cannot_be_removed_under_concurrency(
    admin_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    """Two admins removing each other at the same instant.

    Each passes the self-check (the target is somebody else) and each sees the
    other still in place, so neither can be refused on what it read. The
    guard therefore applies the change and counts what is left, inside a
    transaction the advisory lock has serialised.

    The lock is held by the test to pin the interleaving down: the request
    below authenticates as the second admin, blocks, and only then is that
    admin deactivated out from under it.
    """
    first = await me(admin_client)
    second = await add_user(api_factory, SECOND_ADMIN_EMAIL, USER_PASSWORD, role=UserRole.ADMIN)

    async with api_transport(api_app) as other:
        await login(other, SECOND_ADMIN_EMAIL, USER_PASSWORD)

        async with api_factory() as holder:
            await lock_admin_changes(holder)

            pending = asyncio.create_task(
                other.patch(f"/api/users/{first['id']}", json={"is_active": False})
            )
            await wait_for_a_blocked_advisory_lock(api_factory)

            # The other admin goes while that request waits its turn.
            row = await holder.get(User, second.id)
            assert row is not None
            row.is_active = False
            await holder.commit()

        response = await pending

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": LAST_ADMIN}

    async with api_factory() as session:
        assert await count_active_admins(session) == 1, "the first admin was rolled back"
        remaining = await session.get(User, int(str(first["id"])))
        assert remaining is not None
        assert remaining.is_active is True


async def test_patching_an_account_that_is_not_there_is_404(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.patch("/api/users/999999", json={"is_active": False})

    assert response.status_code == 404
    assert response.json() == {"detail": USER_NOT_FOUND}


async def test_an_unknown_role_is_422(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    response = await admin_client.patch(f"/api/users/{user.id}", json={"role": "superuser"})

    assert response.status_code == 422


async def test_an_unknown_field_is_422(
    admin_client: AsyncClient, api_factory: SessionFactory
) -> None:
    """`extra="forbid"`: a misspelt `is_actve` must not read as "change nothing"."""
    user = await add_user(api_factory, USER_EMAIL, USER_PASSWORD)

    response = await admin_client.patch(f"/api/users/{user.id}", json={"is_actve": False})

    assert response.status_code == 422
