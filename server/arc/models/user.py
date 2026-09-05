"""Accounts: ``users``, ``invites``, ``sessions`` (architecture.md §4)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, column, func, true
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk, created_at
from arc.models.enums import UserRole, enum_column


class User(Base):
    """A person with a login. Registration is invite-only (spec §2)."""

    __tablename__ = "users"
    __table_args__ = (
        # Uniqueness is case-insensitive and enforced by the database, not by
        # a convention the auth service is trusted to follow: ``Bob@x.com``
        # and ``bob@x.com`` are the same account. A functional unique index on
        # ``lower(email)`` is the only way to say that in Postgres — a plain
        # UNIQUE on the column would let both rows in.
        #
        # ``column("email")`` rather than ``User.email``: ``__table_args__`` is
        # evaluated inside the class body, before the mapped attribute exists.
        # Declarative binds the index to the table, so it renders as
        # ``CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))``.
        Index("uq_users_email_lower", func.lower(column("email")), unique=True),
    )

    id: Mapped[int] = bigint_pk()
    #: Stored as the user typed it; compared case-insensitively (see the index
    #: above). The auth service still lowercases on lookup so the index is hit.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole),
        nullable=False,
        default=UserRole.USER,
        server_default=UserRole.USER.value,
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default=true())
    # IANA name; drives the schedule page's weekday grouping (FR-C3).
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    created_at: Mapped[datetime] = created_at()


class Invite(Base):
    """A single-use registration link (architecture.md §7).

    Only the hash of the token is stored, so a database leak does not hand
    anybody a working invite.
    """

    __tablename__ = "invites"

    id: Mapped[int] = bigint_pk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Optional: an invite may be bound to an address or be a generic link.
    email: Mapped[str | None] = mapped_column(String(320))
    # The admin who issued it. Kept as history if that account is deleted.
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class Session(Base):
    """A logged-in browser session.

    The primary key *is* the hash of the cookie value: the raw token is only
    ever in the user's cookie, and a lookup hashes what arrives.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512))
