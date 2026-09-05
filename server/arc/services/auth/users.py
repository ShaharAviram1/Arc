"""Accounts: lookup, creation, and password authentication.

Email is the identity. It is stored lowercased and looked up through
``lower(email)`` so the functional unique index on ``users`` (models/user.py)
is the thing enforcing "one account per address" — not a convention this
module is trusted to keep.

**Argon2 never runs on the event loop.** The parameters in
:mod:`arc.core.security` are deliberately expensive — ~50 ms of CPU and 64 MiB
per hash — and a single ``await``-less call would stall *every* concurrent
request for that long, which is a login endpoint that doubles as a
denial-of-service lever. Every call therefore goes through
:func:`anyio.to_thread.run_sync`; argon2-cffi releases the GIL inside its C
implementation, so the threads really do run in parallel.
"""

from __future__ import annotations

from anyio import to_thread
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arc.core.security import (
    dummy_hash,
    hash_password,
    rehash_if_needed,
    verify_password,
)
from arc.models import User, UserRole

DEFAULT_TIMEZONE = "UTC"


class EmailAlreadyRegistered(Exception):
    """An account with that address already exists. Rendered as a 409."""

    def __init__(self, email: str) -> None:
        super().__init__(f"email already registered: {email}")
        self.email = email


def normalize_email(email: str) -> str:
    """Trim and lowercase. The only normalisation Arc does to an address."""
    return email.strip().lower()


async def hash_password_async(password: str) -> str:
    """:func:`arc.core.security.hash_password`, in a worker thread.

    Raises :class:`~arc.core.security.PasswordPolicyError` just as the
    synchronous one does — ``to_thread.run_sync`` re-raises in the caller.
    """
    return await to_thread.run_sync(hash_password, password)


async def verify_password_async(password_hash: str, password: str) -> bool:
    """:func:`arc.core.security.verify_password`, in a worker thread."""
    return await to_thread.run_sync(verify_password, password_hash, password)


async def rehash_if_needed_async(password_hash: str, password: str) -> str | None:
    """:func:`arc.core.security.rehash_if_needed`, in a worker thread."""
    return await to_thread.run_sync(rehash_if_needed, password_hash, password)


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    """The account for ``email``, matched case-insensitively."""
    statement = select(User).where(func.lower(User.email) == normalize_email(email))
    found: User | None = await db.scalar(statement)
    return found


async def create_user(
    db: AsyncSession,
    email: str,
    password: str,
    *,
    role: UserRole = UserRole.USER,
    timezone: str = DEFAULT_TIMEZONE,
) -> User:
    """Create an active account. Flushed, not committed.

    Raises :class:`EmailAlreadyRegistered` for a duplicate and
    :class:`arc.core.security.PasswordPolicyError` for a password the policy
    rejects. The duplicate is checked twice — once with a SELECT for the
    common case and once by catching the unique-index violation — because the
    SELECT alone loses a race between two simultaneous invite acceptances.
    """
    normalized = normalize_email(email)
    if await get_by_email(db, normalized) is not None:
        raise EmailAlreadyRegistered(normalized)

    user = User(
        email=normalized,
        password_hash=await hash_password_async(password),
        role=role,
        is_active=True,
        timezone=timezone,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise EmailAlreadyRegistered(normalized) from exc
    return user


#: Key for the transaction-scoped advisory lock taken around any change that
#: could remove an active admin. An arbitrary but fixed 64-bit constant, high
#: enough that it will not collide with a key some later feature picks by
#: counting from 1. Postgres advisory locks share one namespace per database.
ACTIVE_ADMIN_LOCK_KEY = 0x41524331_41444D4E  # "ARC1ADMN"


async def lock_admin_changes(db: AsyncSession) -> None:
    """Serialise concurrent changes to the set of active admins.

    ``pg_advisory_xact_lock`` rather than ``SELECT … FOR UPDATE``: the rows
    that decide the answer are not the rows being written (demoting B is
    refused because of what is true of A), so a row lock would have to be
    taken on a set that the very same statement is changing, in an order two
    transactions could disagree about. One named lock, released at commit or
    rollback by Postgres itself, has neither problem.

    Blocks until the lock is free. It is held only for the duration of one
    ``PATCH /api/users/{id}``, which is a handful of statements.
    """
    await db.execute(select(func.pg_advisory_xact_lock(ACTIVE_ADMIN_LOCK_KEY)))


async def count_active_admins(db: AsyncSession) -> int:
    """How many accounts are admins *and* enabled, in this transaction's view.

    Call it after the change and before the commit: Arc must never reach a
    state with nobody able to administer it, and the only recovery from one is
    a database console.
    """
    total = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
    )
    return int(total or 0)


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    """The account for these credentials, or ``None``.

    ``None`` means "unknown address", "wrong password" *or* "deactivated"; the
    caller must answer all three with the same 401. The verification always
    runs, against :func:`arc.core.security.dummy_hash` when no account
    matches, so the response time does not reveal whether the address exists.

    A successful login upgrades the stored hash if the Argon2 parameters have
    moved on since it was written. Flushed, not committed — the caller owns
    the transaction (it is about to add a session row to it).
    """
    user = await get_by_email(db, email)
    stored = user.password_hash if user is not None else dummy_hash()
    matched = await verify_password_async(stored, password)

    if user is None or not matched or not user.is_active:
        return None

    upgraded = await rehash_if_needed_async(user.password_hash, password)
    if upgraded is not None:
        user.password_hash = upgraded
        await db.flush()
    return user


__all__ = [
    "ACTIVE_ADMIN_LOCK_KEY",
    "DEFAULT_TIMEZONE",
    "EmailAlreadyRegistered",
    "authenticate",
    "count_active_admins",
    "create_user",
    "get_by_email",
    "hash_password_async",
    "lock_admin_changes",
    "normalize_email",
    "rehash_if_needed_async",
    "verify_password_async",
]
