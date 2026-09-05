"""Authentication: accounts, passwords, cookie sessions, invites.

The rules live here so the routers in :mod:`arc.api` stay thin (CLAUDE.md
conventions). Layout:

* ``users`` — lookup, creation, :func:`authenticate`
* ``sessions`` — issue/resolve/delete the ``arc_session`` cookie's row
* ``invites`` — issue, validate, and single-use acceptance
* ``bootstrap`` — the first admin, from the environment
* ``ratelimit`` — the in-process login budget

The primitives underneath (Argon2, token hashing, origin checks) are in
:mod:`arc.core.security`.
"""

from __future__ import annotations

from arc.services.auth.bootstrap import bootstrap_admin
from arc.services.auth.invites import (
    DEFAULT_EXPIRY_HOURS,
    MAX_EXPIRY_HOURS,
    CreatedInvite,
    InviteEmailMismatch,
    InviteEmailRequired,
    InviteError,
    InviteNotFound,
    accept,
    create_invite,
    get_valid,
    invite_status,
    revoke,
)
from arc.services.auth.ratelimit import LoginRateLimiter, RateLimitWindow
from arc.services.auth.sessions import (
    COOKIE_NAME,
    NewSession,
    create_session,
    delete_session,
    purge_expired,
    resolve_session,
)
from arc.services.auth.users import (
    EmailAlreadyRegistered,
    authenticate,
    count_active_admins,
    create_user,
    get_by_email,
    lock_admin_changes,
    normalize_email,
)

__all__ = [
    "COOKIE_NAME",
    "DEFAULT_EXPIRY_HOURS",
    "MAX_EXPIRY_HOURS",
    "CreatedInvite",
    "EmailAlreadyRegistered",
    "InviteEmailMismatch",
    "InviteEmailRequired",
    "InviteError",
    "InviteNotFound",
    "LoginRateLimiter",
    "NewSession",
    "RateLimitWindow",
    "accept",
    "authenticate",
    "bootstrap_admin",
    "count_active_admins",
    "create_invite",
    "create_session",
    "create_user",
    "delete_session",
    "get_by_email",
    "get_valid",
    "invite_status",
    "lock_admin_changes",
    "normalize_email",
    "purge_expired",
    "resolve_session",
    "revoke",
]
