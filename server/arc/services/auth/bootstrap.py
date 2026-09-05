"""First-boot admin (roadmap M2).

Registration is invite-only, which leaves the chicken-and-egg problem of the
first account. ``BOOTSTRAP_ADMIN_EMAIL`` and ``BOOTSTRAP_ADMIN_PASSWORD``
solve it: on API startup, if both are set and no account has that address, an
admin is created and the fact is logged.

It is idempotent and it never touches an existing account — not the password,
not the role, not ``is_active``. Leaving the variables in the environment
after first boot must therefore be harmless, because people do; a bootstrap
that reset the password on every restart would pin a production admin to
whatever is in ``.env``. An empty ``BOOTSTRAP_ADMIN_PASSWORD`` (the shipped
default) means "no bootstrap", and says so in the log.
"""

from __future__ import annotations

import logging

from arc.config import Settings
from arc.core.security import PasswordPolicyError
from arc.db import SessionFactory
from arc.models import User, UserRole
from arc.services.auth.users import EmailAlreadyRegistered, create_user, get_by_email

log = logging.getLogger(__name__)


async def bootstrap_admin(factory: SessionFactory, settings: Settings) -> User | None:
    """Create the first admin if it is configured and missing.

    Returns the created user, or ``None`` when nothing was done — not
    configured, already present, lost the race to another process, or the
    password fails the policy. Never raises for a configuration problem: a bad
    bootstrap password must be a loud log line, not an API that refuses to
    start. Every ``None`` says in the log which of those it was.
    """
    email = settings.bootstrap_admin_email
    secret = settings.bootstrap_admin_password
    password = secret.get_secret_value() if secret is not None else ""
    if not email or not password:
        # The normal state after first boot, and the default in .env.example:
        # info, not a warning. Named so an operator who *expected* an admin to
        # appear can see why one did not.
        log.info(
            "bootstrap admin not configured; skipping",
            extra={"has_email": bool(email), "has_password": bool(password)},
        )
        return None

    async with factory() as db:
        existing = await get_by_email(db, email)
        if existing is not None:
            log.info(
                "bootstrap admin already exists; leaving it untouched",
                extra={"email": existing.email, "user_id": existing.id},
            )
            return None

        try:
            user = await create_user(db, email, password, role=UserRole.ADMIN)
        except PasswordPolicyError as exc:
            log.error("bootstrap admin not created", extra={"email": email, "reason": str(exc)})
            return None
        except EmailAlreadyRegistered:
            # Another API process ran the same bootstrap between the SELECT
            # above and this INSERT — `--workers N`, or an api container
            # restarting alongside its replacement. The account exists, which
            # is the whole point, so this is an outcome and not a failure.
            log.info(
                "bootstrap admin created by another process; leaving it untouched",
                extra={"email": email},
            )
            return None

        await db.commit()

    log.info("bootstrap admin created", extra={"email": user.email, "user_id": user.id})
    return user


__all__ = ["bootstrap_admin"]
