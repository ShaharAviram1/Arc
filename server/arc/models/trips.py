"""Trips: ``trips`` and ``trip_episodes`` (spec FR-A12, architecture.md §4, §5.4e).

A trip is one user's "prepare the next X episodes of this show for a trip"
(owner, 2026-10-05): the next X **aired** episodes after their progress, each
made into a small offline copy (FR-P6) for the device to download. Episodes
beyond the user's ordinary window live only on the device — the server makes
the copy, never an HLS rendition, and deletes the source as soon as the copy
is made.

A trip does not create its own kind of want. Its episodes are wanted through
``wants`` like any other (``wants.trip`` marks the rows that exist *only*
because of a trip, rewritten by the reconciler every run), so merging across
users (FR-A2), the search, the download and the matcher are the ones every
other episode goes through.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from arc.db import Base
from arc.models._columns import TZDateTime, bigint_pk, created_at
from arc.models.enums import TripEpisodeState, TripState, enum_column

#: The one-active-trip-per-user invariant (owner, 2026-10-05). A partial
#: unique index rather than a check in code because the create path's own
#: check and a second request racing it can both pass; the database is the
#: only place both agree.
ONE_ACTIVE_TRIP_INDEX = "ux_trips_one_active_per_user"
ONE_ACTIVE_TRIP_PREDICATE = "state = 'active'"

#: "Which trips hold this episode?" — asked by the reconciler, the copy hook
#: and the media route (M19 T4).
TRIP_EPISODE_INDEX = "ix_trip_episodes_episode_id"


class Trip(Base):
    """One user's trip on one show (FR-A12)."""

    __tablename__ = "trips"
    __table_args__ = (
        Index(
            ONE_ACTIVE_TRIP_INDEX,
            "user_id",
            unique=True,
            postgresql_where=text(ONE_ACTIVE_TRIP_PREDICATE),
        ),
    )

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    anime_id: Mapped[int] = mapped_column(
        ForeignKey("anime.id", ondelete="CASCADE"), nullable=False
    )
    #: The episode numbers the trip took, and how many: up to the requested
    #: count of aired episodes after the user's progress, so fewer than asked
    #: when fewer have aired.
    first_number: Mapped[int] = mapped_column(Integer, nullable=False)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[TripState] = mapped_column(
        enum_column(TripState),
        nullable=False,
        default=TripState.ACTIVE,
        server_default=TripState.ACTIVE.value,
    )
    created_at: Mapped[datetime] = created_at()
    #: ``created_at + trip_copy_days``: an episode with no copy by then is
    #: given up on (M19 T4's sweep).
    deadline_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    #: When it stopped being active, for any of the three reasons.
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class TripEpisode(Base):
    """One episode of one trip, and where it stands (FR-A12)."""

    __tablename__ = "trip_episodes"
    __table_args__ = (Index(TRIP_EPISODE_INDEX, "episode_id"),)

    trip_id: Mapped[int] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[TripEpisodeState] = mapped_column(
        enum_column(TripEpisodeState),
        nullable=False,
        default=TripEpisodeState.PENDING,
        server_default=TripEpisodeState.PENDING.value,
    )
    #: When this episode's copy first became ready for this trip — the start
    #: of the ``trip_copy_days`` clock (M19 T4).
    available_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: When the device confirmed it holds the copy (M19 T4). Set and not yet
    #: released, it takes the episode out of this user's window (FR-A1).
    delivered_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: When the device said it deleted its copy (M19 T4).
    released_at: Mapped[datetime | None] = mapped_column(TZDateTime)


__all__ = [
    "ONE_ACTIVE_TRIP_INDEX",
    "ONE_ACTIVE_TRIP_PREDICATE",
    "TRIP_EPISODE_INDEX",
    "Trip",
    "TripEpisode",
]
