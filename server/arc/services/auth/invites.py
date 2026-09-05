"""Invite-only registration (spec §2, architecture.md §7).

An admin creates an invite; Arc returns the link once and keeps only
``sha256(token)``. The invitee opens the link, sets a password, and gets an
account and a session in the same transaction.

The single-use guarantee is the interesting part. It is not "SELECT, check
``used_at``, then UPDATE" — two acceptances arriving together would both read
``NULL`` and both create an account. Instead :func:`accept` *claims* the row
first with

    UPDATE invites SET used_at = now()
     WHERE token_hash = … AND used_at IS NULL AND expires_at > now()

and treats "no rows updated" as "no such invite". The second transaction
blocks on the row lock, re-reads after the first commits, sees ``used_at``
set, and matches nothing. Because the claim is the first statement of the
same transaction that creates the user, any later failure (duplicate email,
bad password) rolls it back and leaves the invite usable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from arc.core.security import generate_token, hash_token, validate_password
from arc.models import Invite, User, UserRole
from arc.services.auth.sessions import NewSession, create_session
from arc.services.auth.users import DEFAULT_TIMEZONE, create_user, normalize_email

#: Default lifetime of an invite link. Architecture.md §7 says seven days.
DEFAULT_EXPIRY_HOURS = 168
#: Ceiling on what an admin may ask for: 30 days.
MAX_EXPIRY_HOURS = 720


class InviteError(Exception):
    """Base for the ways accepting an invite can fail."""


class InviteNotFound(InviteError):
    """Unknown, already used, or expired.

    One exception for all three on purpose: a public endpoint that told the
    caller *which* it was would confirm that a token had once existed.
    """


class InviteEmailRequired(InviteError):
    """The invite carries no address, so the invitee must supply one."""


class InviteEmailMismatch(InviteError):
    """The address given does not match the one the invite was issued for."""


@dataclass(frozen=True, slots=True)
class CreatedInvite:
    """A new invite plus its raw token — the only time the token is readable."""

    invite: Invite
    token: str


def invite_status(invite: Invite, *, now: datetime | None = None) -> str:
    """``used`` | ``expired`` | ``pending``, in that order of precedence."""
    if invite.used_at is not None:
        return "used"
    if invite.expires_at <= (now or datetime.now(UTC)):
        return "expired"
    return "pending"


async def create_invite(
    db: AsyncSession,
    *,
    created_by: int | None,
    email: str | None = None,
    expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
) -> CreatedInvite:
    """Issue an invite. Flushed, not committed."""
    token = generate_token()
    invite = Invite(
        token_hash=hash_token(token),
        email=normalize_email(email) if email else None,
        created_by=created_by,
        expires_at=datetime.now(UTC) + timedelta(hours=expires_in_hours),
    )
    db.add(invite)
    await db.flush()
    return CreatedInvite(invite=invite, token=token)


async def get_valid(db: AsyncSession, token: str) -> Invite | None:
    """The invite for ``token`` if it is unused and unexpired, else ``None``."""
    statement = select(Invite).where(
        Invite.token_hash == hash_token(token),
        Invite.used_at.is_(None),
        Invite.expires_at > datetime.now(UTC),
    )
    found: Invite | None = await db.scalar(statement)
    return found


async def revoke(db: AsyncSession, invite_id: int) -> bool:
    """Expire an invite immediately. ``False`` if there is no such row.

    Expiring rather than deleting: the row is the record of who was invited
    and by whom, which is worth keeping once it has been issued. A revoked
    invite reads as ``expired`` in the listing and is refused by
    :func:`get_valid` and :func:`accept` from this instant.
    """
    result = cast(
        "CursorResult[Any]",
        await db.execute(
            update(Invite)
            .where(Invite.id == invite_id)
            .values(expires_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        ),
    )
    return bool(result.rowcount)


async def accept(
    db: AsyncSession,
    token: str,
    *,
    password: str,
    email: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    session_ttl: timedelta,
    user_agent: str | None = None,
) -> tuple[User, NewSession]:
    """Consume an invite: create the account and sign it in.

    Everything here belongs to the caller's transaction and nothing is
    committed; the router commits once, so either the invite is used *and* the
    account exists, or neither happened.

    Raises :class:`InviteNotFound`, :class:`InviteEmailRequired`,
    :class:`InviteEmailMismatch`,
    :class:`arc.services.auth.users.EmailAlreadyRegistered`, or
    :class:`arc.core.security.PasswordPolicyError`.
    """
    # Before touching the invite: a password the policy rejects is the
    # invitee's own typo, and should not cost a row lock.
    validate_password(password)

    now = datetime.now(UTC)
    claimed = await db.execute(
        update(Invite)
        .where(
            Invite.token_hash == hash_token(token),
            Invite.used_at.is_(None),
            Invite.expires_at > now,
        )
        .values(used_at=now)
        .returning(Invite.id, Invite.email)
        .execution_options(synchronize_session=False)
    )
    row = claimed.first()
    if row is None:
        raise InviteNotFound(token[:8])

    bound_email: str | None = row.email
    if bound_email is not None:
        if email is not None and normalize_email(email) != bound_email:
            raise InviteEmailMismatch(bound_email)
        address = bound_email
    else:
        if not email or not email.strip():
            raise InviteEmailRequired("this invite requires an email address")
        address = email

    user = await create_user(db, address, password, role=UserRole.USER, timezone=timezone)
    new_session = await create_session(db, user.id, ttl=session_ttl, user_agent=user_agent)
    return user, new_session


__all__ = [
    "DEFAULT_EXPIRY_HOURS",
    "MAX_EXPIRY_HOURS",
    "CreatedInvite",
    "InviteEmailMismatch",
    "InviteEmailRequired",
    "InviteError",
    "InviteNotFound",
    "accept",
    "create_invite",
    "get_valid",
    "invite_status",
    "revoke",
]
