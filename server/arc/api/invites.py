"""Invites: an admin issues them, anybody with the link accepts one.

Two audiences on one prefix. The three admin routes need
:data:`~arc.api.deps.AdminUser`; the two token routes are public by necessity
— the person following an invite link has no account yet — and are protected
by the token itself, which is 256 bits of randomness stored only as a hash.

Both public routes answer 404 for *anything* wrong with a token: unknown,
already used, revoked, expired. Distinguishing them would tell a stranger
that a token once existed, and there is nothing the invitee can do with the
difference anyway — the answer is always "ask for a new link".

They are also the only unauthenticated routes besides login, so they carry
their own per-IP budget (:func:`check_invite_rate_limit`): a 404 that costs
nothing is an invitation to ask a great many times.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from arc.api.auth import MAX_EMAIL_LENGTH, client_ip, set_session_cookie
from arc.api.deps import AdminUser, SessionDep, SettingsDep
from arc.api.schemas import UserOut
from arc.core.security import PasswordPolicyError
from arc.models import Invite, User
from arc.services.auth import (
    DEFAULT_EXPIRY_HOURS,
    MAX_EXPIRY_HOURS,
    EmailAlreadyRegistered,
    InviteEmailMismatch,
    InviteEmailRequired,
    InviteNotFound,
    RateLimitWindow,
    accept,
    create_invite,
    get_valid,
    invite_status,
    normalize_email,
    revoke,
)
from arc.services.auth.users import DEFAULT_TIMEZONE

router = APIRouter(prefix="/api/invites", tags=["invites"])

INVITE_NOT_FOUND = "invite not found"
RATE_LIMITED = "too many invite requests"
EMAIL_TAKEN = "email already registered"
#: An address given that is not the one the invite was issued to. 409 rather
#: than 422: the request is well-formed, it conflicts with server state — the
#: same reason ``EMAIL_TAKEN`` is a 409.
EMAIL_MISMATCH = "email does not match invite"
EMAIL_REQUIRED = "email is required for this invite"

#: Longest IANA timezone name worth accepting ("America/Argentina/…" is 32).
MAX_TIMEZONE_LENGTH = 64


def check_invite_rate_limit(request: Request) -> None:
    """Per-IP budget for the two public token routes. 429 when it is spent.

    A dependency rather than middleware so it applies to exactly the two
    routes that need it, and so the admin routes on the same prefix — which
    already need a session and a role — are not charged for it.
    """
    limiter: RateLimitWindow = request.app.state.invite_rate_limiter
    ip = client_ip(request)

    wait = limiter.retry_after(ip)
    if wait is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=RATE_LIMITED,
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    limiter.record(ip)


InviteRateLimit = Depends(check_invite_rate_limit)


class InviteCreate(BaseModel):
    """What an admin may choose when issuing an invite."""

    model_config = ConfigDict(extra="forbid")

    #: Bind the invite to one address, or leave it open for any address.
    email: str | None = Field(default=None, max_length=MAX_EMAIL_LENGTH)
    expires_in_hours: int = Field(default=DEFAULT_EXPIRY_HOURS, ge=1, le=MAX_EXPIRY_HOURS)


class InviteCreated(BaseModel):
    """The response to creating an invite — the only sight of the token.

    ``token`` and ``url`` appear here and nowhere else, ever: the database
    holds only ``sha256(token)``, so an admin who loses the link must issue a
    new invite.
    """

    id: int
    email: str | None
    expires_at: datetime
    token: str
    url: str


class InviteOut(BaseModel):
    """An invite in the admin listing. Carries no token, by construction."""

    id: int
    email: str | None
    created_by: int | None
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    status: Literal["pending", "used", "expired"]


class InviteInfo(BaseModel):
    """What the accept page may know before anyone has authenticated."""

    email: str | None
    expires_at: datetime


class InviteAccept(BaseModel):
    """Setting up the account behind an invite."""

    model_config = ConfigDict(extra="forbid")

    #: Required when the invite is not bound to an address; when it is, it may
    #: be omitted, and if given it must match.
    email: str | None = Field(default=None, max_length=MAX_EMAIL_LENGTH)
    #: Both length bounds are left to `validate_password`, which
    #: :func:`arc.services.auth.accept` calls first. A pydantic `max_length`
    #: would answer 422 with the rejected value echoed back in `input` — the
    #: person's chosen password, in the response body and in anything that
    #: logs one.
    password: str = Field(min_length=1)
    timezone: str = Field(default=DEFAULT_TIMEZONE, max_length=MAX_TIMEZONE_LENGTH)


def _rendered(invite: Invite) -> InviteOut:
    return InviteOut(
        id=invite.id,
        email=invite.email,
        created_by=invite.created_by,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        used_at=invite.used_at,
        status=invite_status(invite),
    )


@router.post(
    "",
    response_model=InviteCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Issue an invite link (admin)",
)
async def create(
    body: InviteCreate,
    admin: AdminUser,
    session: SessionDep,
    settings: SettingsDep,
) -> InviteCreated:
    created = await create_invite(
        session,
        created_by=admin.id,
        # Normalised here as well as in the service, so that what an admin
        # sees in the 201 is what the invite is actually bound to: the invitee
        # is matched against the stored, lowercased address.
        email=normalize_email(body.email) if body.email else None,
        expires_in_hours=body.expires_in_hours,
    )
    await session.commit()
    return InviteCreated(
        id=created.invite.id,
        email=created.invite.email,
        expires_at=created.invite.expires_at,
        token=created.token,
        url=f"{settings.public_url.rstrip('/')}/invite/{created.token}",
    )


@router.get("", response_model=list[InviteOut], summary="List invites (admin)")
async def index(admin: AdminUser, session: SessionDep) -> list[InviteOut]:
    rows = await session.scalars(select(Invite).order_by(Invite.id.desc()))
    return [_rendered(invite) for invite in rows]


@router.delete(
    "/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Revoke an invite (admin)",
)
async def delete(invite_id: int, admin: AdminUser, session: SessionDep) -> Response:
    """Expire the invite immediately.

    The row survives — it is the record of who was invited by whom — and reads
    as ``expired`` from here on.
    """
    if not await revoke(session, invite_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=INVITE_NOT_FOUND)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{token}",
    response_model=InviteInfo,
    summary="Check an invite link (public)",
    dependencies=[InviteRateLimit],
    responses={
        404: {"description": INVITE_NOT_FOUND},
        429: {"description": RATE_LIMITED},
    },
)
async def show(
    token: Annotated[str, Path(min_length=1, max_length=128)], session: SessionDep
) -> InviteInfo:
    invite = await get_valid(session, token)
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=INVITE_NOT_FOUND)
    return InviteInfo(email=invite.email, expires_at=invite.expires_at)


@router.post(
    "/{token}/accept",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Accept an invite: create the account and sign in (public)",
    dependencies=[InviteRateLimit],
    responses={
        404: {"description": INVITE_NOT_FOUND},
        409: {"description": f"{EMAIL_TAKEN} / {EMAIL_MISMATCH}"},
        429: {"description": RATE_LIMITED},
    },
)
async def accept_invite(
    token: Annotated[str, Path(min_length=1, max_length=128)],
    body: InviteAccept,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> User:
    """Consume the invite, create the user, and set the session cookie.

    Nothing is committed until all of it has worked, so a rejected password or
    a taken address leaves the invite unused and still followable.
    """
    try:
        user, issued = await accept(
            session,
            token,
            password=body.password,
            email=normalize_email(body.email) if body.email else None,
            timezone=body.timezone,
            session_ttl=settings.session_ttl,
            user_agent=request.headers.get("user-agent"),
        )
    except InviteNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=INVITE_NOT_FOUND
        ) from None
    except InviteEmailMismatch:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=EMAIL_MISMATCH) from None
    except InviteEmailRequired:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=EMAIL_REQUIRED
        ) from None
    except EmailAlreadyRegistered:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=EMAIL_TAKEN) from None
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None

    await session.commit()
    set_session_cookie(response, issued.token, settings)
    return user


__all__ = [
    "EMAIL_MISMATCH",
    "EMAIL_REQUIRED",
    "EMAIL_TAKEN",
    "INVITE_NOT_FOUND",
    "RATE_LIMITED",
    "check_invite_rate_limit",
    "router",
]
