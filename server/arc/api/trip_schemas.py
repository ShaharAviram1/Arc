"""What a client is told about a trip (FR-A12).

:class:`TripOut` is sent by ``POST /api/anime/{id}/trip`` (201), by ``GET
/api/trips/current`` and as the show page's ``trip`` block. Its own module
because both :mod:`arc.api.trips` and :mod:`arc.api.anime_schemas` need it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from arc.api.media_stream import offline_url
from arc.models import TripState
from arc.services.catalog import preferred_title
from arc.services.trips.phase import TripPhase
from arc.services.trips.view import TripFacts


class TripIn(BaseModel):
    """The body of ``POST /api/anime/{id}/trip``.

    ``count`` is range-checked by the service against ``trip_max_episodes``
    (a setting, so a schema bound would be a second, stale copy of it); a
    strict integer here so ``"5"`` or ``true`` is a 422 rather than a trip.
    """

    model_config = ConfigDict(extra="forbid")

    count: StrictInt


class DeliveredIn(BaseModel):
    """The body of ``POST /api/trips/{id}/episodes/{eid}/delivered``.

    ``etag`` is the copy's ETag as the device downloaded it. Optional and only
    logged when it differs: the device has already checked the length.
    """

    model_config = ConfigDict(extra="forbid")

    etag: str | None = Field(default=None, max_length=200)


class TripLimits(BaseModel):
    """What a trip may ask for, on the show payload for every caller (FR-A12).

    ``max_episodes`` is the ``trip_max_episodes`` setting (1..50): the most a
    trip's count stepper may reach. Sent here because ``GET /api/settings`` is
    admin-only.
    """

    max_episodes: int


class TripEpisodeOut(BaseModel):
    """One episode of a trip.

    * ``phase`` — ``searching``, ``downloading``, ``preparing``, ``available``
      (the copy is waiting for the device), ``unavailable`` (given up, or no
      copy and nothing to make one from), ``waiting_space`` (not started: the
      disk is under the floor); ``delivered`` (a device confirmed it holds
      the copy) and ``expired`` (its ``trip_copy_days`` ran out first, or no
      copy was ever made before the trip's deadline).
    * ``progress`` — 0..1 while ``downloading`` or ``preparing``; 1 once
      ``available``; else null.
    * ``size`` — the copy's bytes once ``available``.
    * ``delivered`` — whether a device has confirmed it holds the copy.
    * ``url`` — where the device downloads the copy
      (``/media/{episode_id}/offline.mp4``) while it is ``ready``: phase
      ``available``, or ``delivered`` until the settle deletes it; else null.
    """

    episode_id: int
    number: int
    phase: TripPhase
    progress: float | None = None
    size: int | None = None
    delivered: bool = False
    url: str | None = None


class TripOut(BaseModel):
    """One trip and where each of its episodes stands."""

    id: int
    anime_id: int
    anime_title: str
    first_number: int
    last_number: int
    #: How many episodes the trip took — up to what was asked, fewer when
    #: fewer have aired after the user's progress.
    count: int
    state: TripState
    created_at: datetime
    #: ``created_at + trip_copy_days``.
    deadline_at: datetime
    episodes: list[TripEpisodeOut]

    @classmethod
    def build(cls, facts: TripFacts) -> TripOut:
        trip = facts.trip
        return cls(
            id=trip.id,
            anime_id=trip.anime_id,
            anime_title=preferred_title(facts.anime),
            first_number=trip.first_number,
            last_number=trip.last_number,
            count=trip.count,
            state=trip.state,
            created_at=trip.created_at,
            deadline_at=trip.deadline_at,
            episodes=[
                TripEpisodeOut(
                    episode_id=row.episode_id,
                    number=row.number,
                    phase=row.phase,
                    progress=row.progress,
                    size=row.size,
                    delivered=row.delivered,
                    url=offline_url(row.episode_id) if row.fetchable else None,
                )
                for row in facts.episodes
            ],
        )


__all__ = ["DeliveredIn", "TripEpisodeOut", "TripIn", "TripLimits", "TripOut"]
