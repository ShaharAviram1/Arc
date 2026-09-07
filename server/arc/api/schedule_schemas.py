"""Response shapes for the schedule and home endpoints (M4).

Separate from :mod:`arc.api.anime_schemas` because they are a different page's
vocabulary — a week, a season, a "behind by N" — and the same reason applies:
the client's types are generated from exactly these. What they *share* is
reused rather than restated, so a card on the schedule and a card in a search
result are the same ``AnimeSummary``.

Two shapes are worth a note.

``ScheduleEntry.air_time_local`` is a string, ``"HH:MM"``, not a datetime. The
whole entry is one weekly slot, and a slot has a time of day but no date; the
one date that exists (``next_at``) is carried separately as a real instant so
the client can render a countdown from it.

``ScheduleDay.weekday`` is 0–6 with Monday at 0 — :meth:`date.weekday`'s
convention, in the *user's* timezone. The client decides where its week
starts; the server does not reorder the array.
"""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum

from pydantic import BaseModel, Field

from arc.api.anime_schemas import AnimeSummary, EpisodeOut, ListEntryOut
from arc.models import Job, ListStatus, Rendition, Torrent
from arc.services.catalog.progress import BehindRow, NewEpisodeRow
from arc.services.catalog.schedule import DAYS_IN_WEEK, PlacedEntry, WeekPlacement
from arc.services.playback.progress import ContinueRow


class SeasonName(StrEnum):
    """AniList's ``MediaSeason`` vocabulary, which is what ``anime.season``
    holds and what the query parameter accepts. An unknown value is a 422
    rather than an empty schedule."""

    WINTER = "WINTER"
    SPRING = "SPRING"
    SUMMER = "SUMMER"
    FALL = "FALL"


class SeasonRef(BaseModel):
    """A season, as the prev/next links name it."""

    year: int
    season: SeasonName


class ScheduleEntry(BaseModel):
    """One show in the week (FR-C3)."""

    anime: AnimeSummary
    #: Local time of day, ``"HH:MM"`` in the user's timezone. Null only for an
    #: unscheduled entry, which has no slot to render.
    air_time_local: str | None = None
    #: The next episode number, when the source publishes one. Null for a slot
    #: synthesised from a MAL broadcast time, which knows when but not which
    #: (FR-C6), and for a season that has finished airing.
    next_episode: int | None = None
    #: When that episode airs, as an instant.
    next_at: datetime | None = None
    #: Whether ``next_at`` is a guess rather than a published time — the slot
    #: Arc synthesises from a MAL broadcast time during an AniList outage
    #: (FR-C6). The client badges it, the way it badges an estimated episode
    #: date. Always false when ``next_at`` is null.
    next_at_estimated: bool = False
    #: Whether the caller follows this show — watching, planned or on hold.
    #: The schedule highlights these (FR-C3).
    following: bool = False
    #: The caller's own list state, or null. ``following`` is derived from it;
    #: both are sent so the client can render a badge without repeating the
    #: rule.
    list_status: ListStatus | None = None

    @classmethod
    def from_placed(cls, entry: PlacedEntry) -> ScheduleEntry:
        return cls(
            anime=AnimeSummary.from_anime(entry.anime, entry.list_status),
            air_time_local=_hhmm(entry.air_time_local),
            next_episode=entry.next_episode,
            next_at=entry.next_at,
            next_at_estimated=entry.next_at_estimated,
            following=entry.following,
            list_status=entry.list_status,
        )


def _hhmm(at: time | None) -> str | None:
    """``"17:00"``. Seconds are dropped: no broadcast slot has them."""
    return None if at is None else f"{at.hour:02d}:{at.minute:02d}"


class ScheduleDay(BaseModel):
    """One weekday of the grid, in the user's timezone."""

    #: 0 = Monday … 6 = Sunday.
    weekday: int = Field(ge=0, le=DAYS_IN_WEEK - 1)
    #: Sorted by local air time, earliest first.
    entries: list[ScheduleEntry] = Field(default_factory=list)


class SchedulePage(BaseModel):
    """``GET /api/schedule`` — one season, as a week (FR-C3)."""

    year: int
    season: SeasonName
    prev: SeasonRef
    next: SeasonRef
    #: The IANA zone the weekdays and times are in: the caller's
    #: ``users.timezone``, or ``"UTC"`` if that is not a zone this server
    #: knows.
    timezone: str
    #: Always seven, index 0 = Monday.
    days: list[ScheduleDay]
    #: Movies, OVAs, specials, and anything the cache has no air time for.
    unscheduled: list[ScheduleEntry] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        placement: WeekPlacement,
        *,
        year: int,
        season: SeasonName,
        prev: tuple[int, str],
        # Not ``next``: the field is called that because it is the client's
        # vocabulary, but a *parameter* of that name shadows the builtin for
        # the length of the method.
        upcoming: tuple[int, str],
        timezone: str,
    ) -> SchedulePage:
        return cls(
            year=year,
            season=season,
            prev=SeasonRef(year=prev[0], season=SeasonName(prev[1])),
            next=SeasonRef(year=upcoming[0], season=SeasonName(upcoming[1])),
            timezone=timezone,
            days=[
                ScheduleDay(
                    weekday=weekday,
                    entries=[ScheduleEntry.from_placed(entry) for entry in entries],
                )
                for weekday, entries in enumerate(placement.days)
            ],
            unscheduled=[ScheduleEntry.from_placed(entry) for entry in placement.unscheduled],
        )


class BehindEntry(BaseModel):
    """A show with aired episodes the caller has not watched (FR-C4)."""

    anime: AnimeSummary
    entry: ListEntryOut
    #: How many episodes of the show have aired.
    aired: int
    #: How many of those are past the caller's progress — "behind by N".
    behind: int
    #: When the newest aired episode aired; null for a back catalogue whose
    #: dates neither source keeps.
    latest_aired_at: datetime | None = None

    @classmethod
    def from_row(cls, row: BehindRow) -> BehindEntry:
        return cls(
            anime=AnimeSummary.from_anime(row.anime, row.entry.status),
            entry=ListEntryOut.model_validate(row.entry),
            aired=row.aired,
            behind=row.behind,
            latest_aired_at=row.latest_aired_at,
        )


class NewEpisodeEntry(BaseModel):
    """An episode of a followed show that aired in the last seven days.

    The episode is the *same* :class:`~arc.api.anime_schemas.EpisodeOut` the
    show page renders, filled in the same way, which is the point of taking
    ``torrent``, ``rendition`` and ``transcode_job``: an episode that aired
    last night is exactly the one most likely to be downloading or preparing
    right now, and a card that showed only its state — with no percentage, no
    reason, and no way to tell "queued" from "half done" — would be at its
    least useful precisely when it matters most (FR-A7, FR-P4).
    """

    anime: AnimeSummary
    episode: EpisodeOut

    @classmethod
    def from_row(
        cls,
        row: NewEpisodeRow,
        *,
        now: datetime,
        list_status: ListStatus | None = None,
        watched: bool = False,
        torrent: Torrent | None = None,
        rendition: Rendition | None = None,
        transcode_job: Job | None = None,
    ) -> NewEpisodeEntry:
        return cls(
            anime=AnimeSummary.from_anime(row.anime, list_status),
            episode=EpisodeOut.from_episode(
                row.episode,
                now=now,
                anime_status=row.anime.status,
                watched=watched,
                torrent=torrent,
                rendition=rendition,
                transcode_job=transcode_job,
            ),
        )


class ContinueWatchingEntry(BaseModel):
    """An episode started but not finished, most recent first (FR-W1).

    ``position_s`` is what the player seeks to, and it is the *stored*
    position rather than the resume rule's answer: the row is only here at all
    because it is past :data:`~arc.services.playback.progress.
    CONTINUE_MIN_POSITION_S` and not completed, so the two agree except at the
    95 % ceiling — and a card that says "6 minutes left" while the player would
    start from zero is the sort of disagreement worth not having.
    """

    anime: AnimeSummary
    episode: EpisodeOut
    #: Where the player got to, in seconds, and how long the episode is. The
    #: duration is null for a row written before the player knew it.
    position_s: float = 0.0
    duration_s: float | None = None

    @classmethod
    def from_row(
        cls,
        row: ContinueRow,
        *,
        now: datetime,
        list_status: ListStatus | None = None,
        torrent: Torrent | None = None,
        rendition: Rendition | None = None,
        transcode_job: Job | None = None,
    ) -> ContinueWatchingEntry:
        return cls(
            anime=AnimeSummary.from_anime(row.anime, list_status),
            episode=EpisodeOut.from_episode(
                row.episode,
                now=now,
                anime_status=row.anime.status,
                # Never true here by construction: the query that produced this
                # row filters completed rows out. Passed explicitly all the same,
                # so the flag has one source rather than a default.
                watched=False,
                torrent=torrent,
                rendition=rendition,
                transcode_job=transcode_job,
            ),
            position_s=row.position_s,
            duration_s=row.duration_s,
        )


class HomePage(BaseModel):
    """``GET /api/home`` — the three rows of the home page (FR-W1)."""

    #: Started and unfinished, most recently watched first, at most
    #: :data:`~arc.services.playback.progress.CONTINUE_LIMIT`.
    continue_watching: list[ContinueWatchingEntry] = Field(default_factory=list)
    #: Newest aired episode first.
    behind: list[BehindEntry] = Field(default_factory=list)
    #: Newest first, at most :data:`arc.services.catalog.progress.NEW_LIMIT`.
    new_this_week: list[NewEpisodeEntry] = Field(default_factory=list)


__all__ = [
    "BehindEntry",
    "ContinueWatchingEntry",
    "HomePage",
    "NewEpisodeEntry",
    "ScheduleDay",
    "ScheduleEntry",
    "SchedulePage",
    "SeasonName",
    "SeasonRef",
]
