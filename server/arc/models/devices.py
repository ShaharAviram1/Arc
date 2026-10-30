"""What a user's devices report holding: ``device_holdings`` (FR-A15, M20 T4).

A device (an iPad, a phone, a laptop browser) that downloaded episodes with
Keep offline tells the server which of them it still holds unwatched. The
acquisition window then skips those episodes for that user (FR-A1's device
skip, owner 2026-10-13): fetching and preparing an episode that is already
sitting on the user's iPad is the waste M20 exists to stop.

``device_id`` is a UUID the device generates once and keeps; it is a **label**
and never a path (spec §7). A device replaces its whole set on each report, so
a row says "as of ``reported_at`` this device held this episode". A report
older than :data:`~arc.services.acquisition.wants.DEVICE_HOLDING_DAYS` no
longer counts, which is what stops a wiped iPad suppressing fetches for ever.

Trip copies are recorded on ``trip_episodes`` (``delivered_at`` /
``released_at``), not here; the window reads both.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk
from arc.models.media import SHA256_HEX_LENGTH

#: A UUID in its canonical text form is 36 characters.
DEVICE_ID_LENGTH = 36

#: The lookup the reconciler makes ("this user's rows reported since the
#: cutoff") and the report's TTL purge. Named so the migration test can ask
#: the database for it.
HOLDINGS_USER_REPORTED_INDEX = "ix_device_holdings_user_id_reported_at"

#: The newest reports of one device, which is how the operator reads them and
#: how the insert prunes them.
DEVICE_REPORTS_INDEX = "ix_device_reports_device_id_received_at"

#: The longest ETag a holdings report may carry (the trip confirmation's own
#: bound): the server's are far shorter.
HOLDING_ETAG_LENGTH = 200


class DeviceHolding(Base):
    """(user, device, episode): this device reported holding this episode unwatched."""

    __tablename__ = "device_holdings"
    __table_args__ = (Index(HOLDINGS_USER_REPORTED_INDEX, "user_id", "reported_at"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    device_id: Mapped[str] = mapped_column(String(DEVICE_ID_LENGTH), primary_key=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    #: When the device last reported its set. The skip honours a report for
    #: ``DEVICE_HOLDING_DAYS`` and no longer.
    reported_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    #: What the device says it holds of the episode's copy (owner, 2026-10-30),
    #: when its report carries it: the ETag it downloaded, the bytes and the
    #: SHA-256 it computed over them. Null for a report in the old shape (ids
    #: only) and for a download that is not a small copy. Recorded for the
    #: operator; nothing decides on them yet.
    etag: Mapped[str | None] = mapped_column(String(HOLDING_ETAG_LENGTH))
    size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(SHA256_HEX_LENGTH))


class DeviceReport(Base):
    """One event log a device sent for the operator (owner, 2026-10-30).

    The download manager keeps a ring buffer of its own decisions (what it
    started, resumed, truncated, confirmed, found missing); "Send report"
    posts it here so the operator can read what a device did with a copy
    (``arc.cli device-report``). ``events`` is stored as sent — data, never
    instructions — and at most :data:`~arc.services.devices.reports.
    MAX_REPORTS_PER_DEVICE` reports are kept per device, the oldest pruned
    on insert.
    """

    __tablename__ = "device_reports"
    __table_args__ = (Index(DEVICE_REPORTS_INDEX, "device_id", "received_at"),)

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(DEVICE_ID_LENGTH), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)


__all__ = [
    "DEVICE_ID_LENGTH",
    "DEVICE_REPORTS_INDEX",
    "HOLDINGS_USER_REPORTED_INDEX",
    "HOLDING_ETAG_LENGTH",
    "DeviceHolding",
    "DeviceReport",
]
