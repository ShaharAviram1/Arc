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

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime

#: A UUID in its canonical text form is 36 characters.
DEVICE_ID_LENGTH = 36

#: The lookup the reconciler makes ("this user's rows reported since the
#: cutoff") and the report's TTL purge. Named so the migration test can ask
#: the database for it.
HOLDINGS_USER_REPORTED_INDEX = "ix_device_holdings_user_id_reported_at"


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


__all__ = ["DEVICE_ID_LENGTH", "HOLDINGS_USER_REPORTED_INDEX", "DeviceHolding"]
