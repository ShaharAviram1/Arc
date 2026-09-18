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
from typing import Any

from pydantic import BaseModel, Field

from arc.api.anime_schemas import AnimeSummary, EpisodeOut, ListEntryOut
from arc.models import EpisodeState, Job, ListStatus, Rendition, Torrent
from arc.services.catalog.failures import FailureKind, FailureRow
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
    #: Whether the show is in this week because it is on air rather than
    #: because it carries the season being shown — a two-cour show that started
    #: last season, or a long-runner tagged with none (owner, 2026-09-13). The
    #: client turns it into a quiet "Spring 2026" caveat under the slot, read
    #: off ``anime.season``/``anime.season_year``, which is why the flag says
    #: *that it was carried in* and not which season it came from: the season is
    #: already on the summary, and comparing the two client-side would answer
    #: the wrong question on a row the catalogue has no season for. Always false
    #: on a prev/next season view, which takes no such rows.
    carried_over: bool = False
    #: Whether the caller has watched **the episode this slot names**, or null
    #: (owner, 2026-09-17). FR-W5's definition, applied to ``next_episode`` and
    #: to nothing else.
    #:
    #: Null is the ordinary answer, and it means "there is nothing to say":
    #: either the slot names no episode (FR-C6's MAL-synthesised time), or the
    #: episode it names has **not aired yet** — and nobody has watched an
    #: episode that has not been broadcast. That is the bug this field exists
    #: to end: Watch Now drew "Episode 23 ✓ Watched" beside Friday's Slime
    #: broadcast because it read the tick off the show's *latest aired* row
    #: rather than off the episode on the card. The flag now belongs to the
    #: named episode or to no one.
    watched: bool | None = None

    @classmethod
    def from_placed(cls, entry: PlacedEntry, *, watched: bool | None = None) -> ScheduleEntry:
        """``watched`` is the caller's FR-W5 answer for ``entry.next_episode``.

        Passed in rather than derived, and only ever for an episode that has
        already aired: the router is what holds the caller's list progress and
        completions, and the rule that an upcoming slot carries no tick is
        enforced by simply not asking about one (:mod:`arc.api.schedule`).
        """
        return cls(
            anime=AnimeSummary.from_anime(entry.anime, entry.list_status),
            air_time_local=_hhmm(entry.air_time_local),
            next_episode=entry.next_episode,
            next_at=entry.next_at,
            next_at_estimated=entry.next_at_estimated,
            following=entry.following,
            list_status=entry.list_status,
            carried_over=entry.carried_over,
            watched=watched,
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
        # ``anime_id → watched``, and present only for the entries whose named
        # episode has aired. One entry per show in a week, so the show's id
        # identifies the slot; see :attr:`ScheduleEntry.watched`.
        watched: dict[int, bool] | None = None,
    ) -> SchedulePage:
        marks = watched or {}
        return cls(
            year=year,
            season=season,
            prev=SeasonRef(year=prev[0], season=SeasonName(prev[1])),
            next=SeasonRef(year=upcoming[0], season=SeasonName(upcoming[1])),
            timezone=timezone,
            days=[
                ScheduleDay(
                    weekday=weekday,
                    entries=[
                        ScheduleEntry.from_placed(entry, watched=marks.get(entry.anime.id))
                        for entry in entries
                    ],
                )
                for weekday, entries in enumerate(placement.days)
            ],
            unscheduled=[
                ScheduleEntry.from_placed(entry, watched=marks.get(entry.anime.id))
                for entry in placement.unscheduled
            ],
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
            # Built rather than validated, so ``dormant`` is derived here too
            # (FR-A9): every ``ListEntryOut`` in the API answers that field the
            # same way, and the show is in hand.
            entry=ListEntryOut.build(row.entry, anime_status=row.anime.status),
            aired=row.aired,
            behind=row.behind,
            latest_aired_at=row.latest_aired_at,
        )


class NewEpisodeEntry(BaseModel):
    """One episode of a followed show, as an episode shelf renders it.

    Both 16:9 shelves take this shape — ``new_this_week`` and, since
    2026-09-17, ``ready_to_watch`` — because it is the same row: a show and one
    of its episodes. One model rather than two identical ones, for the reason
    :class:`~arc.services.catalog.progress.NewEpisodeRow` is one dataclass.

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
        completed: bool = False,
        list_progress: int = 0,
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
                # FR-W5's two halves, both per viewer: Arc's own completion row
                # and the progress this show's list entry carries. Most of this
                # shelf is watched by the second on a list imported from
                # MyAnimeList, which is the case the owner hit on production.
                completed=completed,
                list_progress=list_progress,
                torrent=torrent,
                rendition=rendition,
                transcode_job=transcode_job,
            ),
        )


class ContinueWatchingEntry(BaseModel):
    """An episode with somewhere left to get to, most recent first (FR-W1).

    ``position_s`` is what the player seeks to, and it is the *stored*
    position rather than the resume rule's answer: the row is only here at all
    because it is past :data:`~arc.services.playback.progress.
    CONTINUE_MIN_POSITION_S` and short of both of the shelf's end bounds — the
    completion mark and :data:`~arc.services.playback.progress.CONTINUE_TAIL_S`
    — each of which is tighter than the resume rule's ceiling, so the player
    always resumes where the card says. A card reading "6 minutes left" while
    the player would start from zero is the sort of disagreement worth not
    having.
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
        list_progress: int = 0,
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
                # A rewatch left half-way is on this shelf (FR-W1), so this is
                # sometimes true. It comes off the row's own ``watch_progress``
                # rather than a second lookup, and it is the same flag the show
                # page's mark reads: the shelf changed which rows it lists, not
                # what "watched" means. ``list_progress`` is FR-W5's other
                # half, and it is why an episode somebody watched elsewhere
                # and then reopened here still carries its tick.
                completed=row.completed,
                list_progress=list_progress,
                torrent=torrent,
                rendition=rendition,
                transcode_job=transcode_job,
            ),
            position_s=row.position_s,
            duration_s=row.duration_s,
        )


class FailureEntry(BaseModel):
    """One of the viewer's **own** failures, as the banner draws it (FR-W6).

    Flat, and one shape for both kinds: the banner is a single strip in a
    single order, so the client renders rows rather than branching into two
    sections. ``kind`` says which half of the model is filled —
    ``episode``/``state``/``episode_number`` for an episode that has stopped,
    ``field``/``old_value``/``new_value``/``log_id`` for a MyAnimeList write
    that did not land.

    Deliberately **not** an :class:`~arc.api.anime_schemas.EpisodeOut`: a
    failure row names an episode, it does not render one, and filling in an
    ``EpisodeOut`` would put this list into
    :mod:`arc.api.episode_extras`' four lookups for a number and a state the
    row already has.
    """

    #: Stable per failure, and different once the failure is a new one: what
    #: the client's per-account dismissal is remembered under
    #: (:mod:`arc.services.catalog.failures` explains the shape).
    key: str
    kind: FailureKind
    anime: AnimeSummary
    #: One sentence, already trimmed server-side — never a stderr wall.
    reason: str
    #: When it happened, or null where nothing dated it.
    since: datetime | None = None
    #: The episode that stopped (``kind = episode``): ``failed`` — a transcode
    #: broke, FR-P4 — or ``unavailable`` — FR-A6 gave up and retries daily.
    episode_id: int | None = None
    episode_number: int | None = None
    state: EpisodeState | None = None
    #: The write that did not land (``kind = mal``): the log row's own id, so
    #: the sync page can be opened knowing which row this was.
    log_id: int | None = None
    field: str | None = None
    old_value: Any = None
    new_value: Any = None

    @classmethod
    def from_row(cls, row: FailureRow, *, list_status: ListStatus | None = None) -> FailureEntry:
        return cls(
            key=row.key,
            kind=row.kind,
            anime=AnimeSummary.from_anime(row.anime, list_status),
            reason=row.reason,
            since=row.since,
            episode_id=None if row.episode is None else row.episode.id,
            episode_number=None if row.episode is None else row.episode.number,
            state=row.state,
            log_id=row.log_id,
            field=row.field,
            old_value=row.old_value,
            new_value=row.new_value,
        )


class HomePage(BaseModel):
    """``GET /api/home`` — the shelves of the home page (FR-W1)."""

    #: Started and unfinished, most recently watched first, at most
    #: :data:`~arc.services.playback.progress.CONTINUE_LIMIT`.
    continue_watching: list[ContinueWatchingEntry] = Field(default_factory=list)
    #: Ready, unstarted and unwatched, whenever the episode aired; the file's
    #: ``ready_at`` newest first, at most
    #: :data:`arc.services.catalog.progress.READY_LIMIT` (owner, 2026-09-17).
    #: A shelf of its own rather than a slice of ``new_this_week``, which is
    #: the fix: the client used to filter the week's episodes for the ready
    #: ones, so a ready episode of an older show was never on the page.
    ready_to_watch: list[NewEpisodeEntry] = Field(default_factory=list)
    #: Newest aired episode first.
    behind: list[BehindEntry] = Field(default_factory=list)
    #: Newest first, at most :data:`arc.services.catalog.progress.NEW_LIMIT`.
    new_this_week: list[NewEpisodeEntry] = Field(default_factory=list)
    #: The viewer's **own** failures, newest first, at most
    #: :data:`arc.services.catalog.failures.MAX_FAILURES` (FR-W6, M16). Here
    #: rather than on an endpoint of its own so that §5.9's live updates carry
    #: it: an episode going ``failed`` already invalidates this query, and a
    #: second route would have needed its own invalidation to say the same
    #: thing. Empty is the ordinary answer and the client draws nothing for it.
    failures: list[FailureEntry] = Field(default_factory=list)


__all__ = [
    "BehindEntry",
    "ContinueWatchingEntry",
    "FailureEntry",
    "HomePage",
    "NewEpisodeEntry",
    "ScheduleDay",
    "ScheduleEntry",
    "SchedulePage",
    "SeasonName",
    "SeasonRef",
]
