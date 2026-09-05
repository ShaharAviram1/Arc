"""Invites: issue, inspect, accept, revoke — and the single-use guarantee.

The M2 definition of done in the roadmap names exactly two of these: "tests
cover token single-use and expiry". The concurrency test is the one that
matters most, because single-use is only real if it survives two acceptances
arriving at the same instant.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from arc.api.deps import ADMIN_REQUIRED, NOT_AUTHENTICATED
from arc.api.invites import (
    EMAIL_MISMATCH,
    EMAIL_REQUIRED,
    EMAIL_TAKEN,
    INVITE_NOT_FOUND,
    RATE_LIMITED,
)
from arc.config import Settings
from arc.core.security import MAX_PASSWORD_LENGTH, generate_token
from arc.db import SessionFactory
from arc.models import Invite, User, UserRole
from arc.services.auth import COOKIE_NAME
from tests.conftest import add_user, api_transport, login

pytestmark = pytest.mark.pg

INVITEE_EMAIL = "invitee@arc.test"
INVITEE_PASSWORD = "invitee-password"


async def issue(client: AsyncClient, **body: object) -> dict[str, object]:
    """Create an invite as an admin and return the 201 body."""
    response = await client.post("/api/invites", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, object] = response.json()
    return created


async def expire(factory: SessionFactory, invite_id: int) -> None:
    """Push an invite's expiry into the past."""
    async with factory() as session:
        invite = await session.get(Invite, invite_id)
        assert invite is not None
        invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


# --- Creating and listing (admin) -------------------------------------------


async def test_create_returns_token_and_url(admin_client: AsyncClient) -> None:
    created = await issue(admin_client, email=INVITEE_EMAIL, expires_in_hours=24)

    assert created["email"] == INVITEE_EMAIL
    token = created["token"]
    assert isinstance(token, str) and len(token) >= 40
    # settings.public_url is the dev default in the test settings.
    assert created["url"] == f"http://localhost:5173/invite/{token}"
    assert created["expires_at"]


async def test_the_token_is_never_shown_again(admin_client: AsyncClient) -> None:
    created = await issue(admin_client)

    listed = await admin_client.get("/api/invites")

    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert set(rows[0]) == {
        "id",
        "email",
        "created_by",
        "created_at",
        "expires_at",
        "used_at",
        "status",
    }
    assert str(created["token"]) not in listed.text
    assert "token_hash" not in listed.text
    assert rows[0]["status"] == "pending"


async def test_the_default_expiry_is_seven_days(admin_client: AsyncClient) -> None:
    created = await issue(admin_client)

    expires = datetime.fromisoformat(str(created["expires_at"]))
    assert timedelta(days=6, hours=23) < expires - datetime.now(UTC) <= timedelta(days=7)


async def test_an_absurd_expiry_is_refused(admin_client: AsyncClient) -> None:
    assert (
        await admin_client.post("/api/invites", json={"expires_in_hours": 10_000})
    ).status_code == 422


async def test_only_an_admin_may_manage_invites(
    api_client: AsyncClient, api_factory: SessionFactory
) -> None:
    anonymous = await api_client.post("/api/invites", json={})
    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": NOT_AUTHENTICATED}

    await add_user(api_factory, "plain@arc.test", INVITEE_PASSWORD)
    await login(api_client, "plain@arc.test", INVITEE_PASSWORD)

    for response in (
        await api_client.post("/api/invites", json={}),
        await api_client.get("/api/invites"),
        await api_client.delete("/api/invites/1"),
    ):
        assert response.status_code == 403
        assert response.json() == {"detail": ADMIN_REQUIRED}


# --- The public token routes ------------------------------------------------


async def test_a_valid_token_shows_the_address_it_was_issued_for(
    admin_client: AsyncClient, api_app: FastAPI
) -> None:
    created = await issue(admin_client, email=INVITEE_EMAIL)

    async with api_transport(api_app) as public:
        response = await public.get(f"/api/invites/{created['token']}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"email", "expires_at"}, "no token, no id, no created_by"
    assert body["email"] == INVITEE_EMAIL
    assert datetime.fromisoformat(body["expires_at"]) == datetime.fromisoformat(
        str(created["expires_at"])
    )


async def test_an_unknown_token_is_404(api_app: FastAPI) -> None:
    async with api_transport(api_app) as public:
        response = await public.get("/api/invites/not-a-real-token")

    assert response.status_code == 404
    assert response.json() == {"detail": INVITE_NOT_FOUND}


async def test_an_expired_token_is_404_on_both_public_routes(
    admin_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    created = await issue(admin_client)
    await expire(api_factory, int(str(created["id"])))

    async with api_transport(api_app) as public:
        looked_up = await public.get(f"/api/invites/{created['token']}")
        accepted = await public.post(
            f"/api/invites/{created['token']}/accept",
            json={"email": INVITEE_EMAIL, "password": INVITEE_PASSWORD},
        )

    assert looked_up.status_code == 404
    assert looked_up.json() == {"detail": INVITE_NOT_FOUND}
    assert accepted.status_code == 404
    assert accepted.json() == {"detail": INVITE_NOT_FOUND}


# --- Accepting --------------------------------------------------------------


async def test_accepting_creates_the_account_and_signs_it_in(
    admin_client: AsyncClient, api_app: FastAPI
) -> None:
    created = await issue(admin_client, email=INVITEE_EMAIL)

    async with api_transport(api_app) as invitee:
        accepted = await invitee.post(
            f"/api/invites/{created['token']}/accept",
            json={"password": INVITEE_PASSWORD, "timezone": "Europe/Berlin"},
        )

        assert accepted.status_code == 201, accepted.text
        body = accepted.json()
        assert body["email"] == INVITEE_EMAIL
        assert body["role"] == "user", "an invite never makes an admin"
        assert body["timezone"] == "Europe/Berlin"
        assert invitee.cookies.get(COOKIE_NAME)

        me = await invitee.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["id"] == body["id"]

    listed = (await admin_client.get("/api/invites")).json()
    assert listed[0]["status"] == "used"
    assert listed[0]["used_at"] is not None


async def test_an_open_invite_needs_an_address(admin_client: AsyncClient, api_app: FastAPI) -> None:
    created = await issue(admin_client)

    async with api_transport(api_app) as invitee:
        without = await invitee.post(
            f"/api/invites/{created['token']}/accept", json={"password": INVITEE_PASSWORD}
        )
        assert without.status_code == 422
        assert without.json() == {"detail": EMAIL_REQUIRED}

        with_address = await invitee.post(
            f"/api/invites/{created['token']}/accept",
            json={"email": "  Open@Arc.Test  ", "password": INVITEE_PASSWORD},
        )
        assert with_address.status_code == 201, with_address.text
        assert with_address.json()["email"] == "open@arc.test"


async def test_a_mismatched_address_is_a_conflict_and_leaves_the_invite_usable(
    admin_client: AsyncClient, api_app: FastAPI
) -> None:
    """409, not 422: the request is well formed, it conflicts with the invite."""
    created = await issue(admin_client, email=INVITEE_EMAIL)

    async with api_transport(api_app) as invitee:
        mismatched = await invitee.post(
            f"/api/invites/{created['token']}/accept",
            json={"email": "someone.else@arc.test", "password": INVITEE_PASSWORD},
        )
        assert mismatched.status_code == 409
        assert mismatched.json() == {"detail": EMAIL_MISMATCH}

        # The failed attempt must not have burned the invite.
        retried = await invitee.post(
            f"/api/invites/{created['token']}/accept",
            json={"email": INVITEE_EMAIL.upper(), "password": INVITEE_PASSWORD},
        )
        assert retried.status_code == 201, retried.text


async def test_an_address_that_already_has_an_account_is_a_conflict(
    admin_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    await add_user(api_factory, INVITEE_EMAIL, INVITEE_PASSWORD)
    created = await issue(admin_client, email=INVITEE_EMAIL)

    async with api_transport(api_app) as invitee:
        response = await invitee.post(
            f"/api/invites/{created['token']}/accept", json={"password": INVITEE_PASSWORD}
        )

    assert response.status_code == 409
    assert response.json() == {"detail": EMAIL_TAKEN}

    async with api_factory() as session:
        invite = await session.get(Invite, int(str(created["id"])))
        assert invite is not None
        assert invite.used_at is None, "a rejected acceptance must roll the claim back"


async def test_a_password_below_the_policy_is_422(
    admin_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    created = await issue(admin_client, email=INVITEE_EMAIL)

    async with api_transport(api_app) as invitee:
        response = await invitee.post(
            f"/api/invites/{created['token']}/accept", json={"password": "short"}
        )

    assert response.status_code == 422
    assert "at least 10" in str(response.json()["detail"])

    async with api_factory() as session:
        assert (await session.scalars(select(User))).all() != [], "the admin still exists"
        invite = await session.get(Invite, int(str(created["id"])))
        assert invite is not None and invite.used_at is None


async def test_a_password_above_the_policy_is_422_and_is_not_echoed_back(
    admin_client: AsyncClient, api_app: FastAPI
) -> None:
    """The 422 body must never contain the password it is rejecting.

    Pydantic's own `max_length` error carries the offending value in `input`,
    which would put a chosen password in the response and in anything that
    logs one; the bound is enforced by `validate_password` instead, whose
    message is a plain string.
    """
    created = await issue(admin_client, email=INVITEE_EMAIL)
    too_long = "z" * (MAX_PASSWORD_LENGTH + 1)

    async with api_transport(api_app) as invitee:
        response = await invitee.post(
            f"/api/invites/{created['token']}/accept", json={"password": too_long}
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": f"password must be at most {MAX_PASSWORD_LENGTH} characters"
    }
    assert too_long not in response.text
    assert "z" * 20 not in response.text


async def test_an_unknown_field_when_accepting_is_422(
    admin_client: AsyncClient, api_app: FastAPI
) -> None:
    created = await issue(admin_client, email=INVITEE_EMAIL)

    async with api_transport(api_app) as invitee:
        response = await invitee.post(
            f"/api/invites/{created['token']}/accept",
            json={"password": INVITEE_PASSWORD, "role": "admin"},
        )

    assert response.status_code == 422, "a `role` field must not be quietly ignored"


async def test_the_bound_address_is_stored_and_returned_normalised(
    admin_client: AsyncClient, api_app: FastAPI
) -> None:
    """What the admin sees in the 201 is what the invitee is matched against."""
    created = await issue(admin_client, email="  Invitee@ARC.test ")

    assert created["email"] == INVITEE_EMAIL

    async with api_transport(api_app) as invitee:
        info = await invitee.get(f"/api/invites/{created['token']}")
        assert info.status_code == 200
        assert info.json()["email"] == INVITEE_EMAIL

        accepted = await invitee.post(
            f"/api/invites/{created['token']}/accept",
            json={"email": "INVITEE@arc.TEST", "password": INVITEE_PASSWORD},
        )

    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["email"] == INVITEE_EMAIL


async def test_the_public_routes_are_rate_limited_per_ip(
    admin_client: AsyncClient, api_app: FastAPI, settings: Settings
) -> None:
    """Unauthenticated, and they answer a question about a secret.

    A 404 that costs nothing is an invitation to ask a great many times, and
    the two routes share one budget so that alternating between them buys
    nothing either.
    """
    budget = settings.invite_rate_limit_per_ip
    unknown = generate_token()

    async with api_transport(api_app) as visitor:
        for attempt in range(budget):
            response = await visitor.get(f"/api/invites/{unknown}")
            assert response.status_code == 404, f"attempt {attempt}: {response.text}"

        blocked = await visitor.get(f"/api/invites/{unknown}")
        assert blocked.status_code == 429
        assert blocked.json() == {"detail": RATE_LIMITED}
        assert int(blocked.headers["retry-after"]) > 0

        # The accept route draws on the same budget.
        also_blocked = await visitor.post(
            f"/api/invites/{unknown}/accept", json={"password": INVITEE_PASSWORD}
        )
        assert also_blocked.status_code == 429

    # The admin routes are untouched: they are behind a session and a role.
    assert (await admin_client.get("/api/invites")).status_code == 200


async def test_an_invite_can_only_be_accepted_once(
    admin_client: AsyncClient, api_app: FastAPI
) -> None:
    created = await issue(admin_client)

    async with api_transport(api_app) as first, api_transport(api_app) as second:
        one = await first.post(
            f"/api/invites/{created['token']}/accept",
            json={"email": "first@arc.test", "password": INVITEE_PASSWORD},
        )
        two = await second.post(
            f"/api/invites/{created['token']}/accept",
            json={"email": "second@arc.test", "password": INVITEE_PASSWORD},
        )

    assert one.status_code == 201, one.text
    assert two.status_code == 404
    assert two.json() == {"detail": INVITE_NOT_FOUND}


async def test_two_simultaneous_acceptances_produce_exactly_one_account(
    admin_client: AsyncClient, api_app: FastAPI, api_factory: SessionFactory
) -> None:
    """The reason the claim is an ``UPDATE … WHERE used_at IS NULL``.

    Two separate clients, two separate database connections, one event loop:
    the second transaction blocks on the row lock, re-reads once the first
    commits, and matches nothing.
    """
    created = await issue(admin_client)
    token = created["token"]

    async with api_transport(api_app) as first, api_transport(api_app) as second:
        responses = await asyncio.gather(
            first.post(
                f"/api/invites/{token}/accept",
                json={"email": "race-a@arc.test", "password": INVITEE_PASSWORD},
            ),
            second.post(
                f"/api/invites/{token}/accept",
                json={"email": "race-b@arc.test", "password": INVITEE_PASSWORD},
            ),
        )

    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 404], [r.text for r in responses]

    async with api_factory() as session:
        created_users = list(
            (await session.scalars(select(User).where(User.role == UserRole.USER))).all()
        )
    assert len(created_users) == 1
    assert created_users[0].email in {"race-a@arc.test", "race-b@arc.test"}


# --- Revoking ---------------------------------------------------------------


async def test_revoking_expires_the_invite_but_keeps_the_record(
    admin_client: AsyncClient, api_app: FastAPI
) -> None:
    created = await issue(admin_client, email=INVITEE_EMAIL)

    revoked = await admin_client.delete(f"/api/invites/{created['id']}")
    assert revoked.status_code == 204

    listed = (await admin_client.get("/api/invites")).json()
    assert len(listed) == 1, "the row is kept as the record of who was invited"
    assert listed[0]["status"] == "expired"

    async with api_transport(api_app) as public:
        assert (await public.get(f"/api/invites/{created['token']}")).status_code == 404
        assert (
            await public.post(
                f"/api/invites/{created['token']}/accept",
                json={"password": INVITEE_PASSWORD},
            )
        ).status_code == 404


async def test_revoking_something_that_is_not_there_is_404(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.delete("/api/invites/999999")

    assert response.status_code == 404
    assert response.json() == {"detail": INVITE_NOT_FOUND}
