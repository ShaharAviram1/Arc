"""Request and response shapes for the player (FR-S2, FR-S3, FR-S4, FR-W3).

Their own module for the same reason :mod:`arc.api.anime_schemas` and
:mod:`arc.api.schedule_schemas` are: the client's types are generated from
exactly these, and the episode and show shapes are *reused* rather than
restated, so the card the player renders above the video is the same
``EpisodeOut`` the show page renders in a list.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from arc.api.anime_schemas import AnimeSummary, EpisodeOut
from arc.api.deps import MAX_ID, MIN_ID
from arc.models import Episode, EpisodeState, Rendition
from arc.services.playback.next_season import ListState, NextSeason

#: The language a text track is announced with when the source did not say
#: (BCP 47's "undetermined").
UNDETERMINED_LANG = "und"


class EpisodeRef(BaseModel):
    """The neighbour of the episode being played (FR-S5).

    Deliberately thinner than :class:`~arc.api.anime_schemas.EpisodeOut`: the
    player needs to know whether there *is* a next episode and whether it can
    be played, and rendering a whole episode row for a link would mean the
    three side lookups for two episodes nobody is watching.

    ``ready`` is derived from ``state`` and sent anyway, so the client can
    decide whether to offer "next episode" without repeating the rule.

    ``fetchable`` is play on demand's half (FR-A14, M20): the caller may
    press play on this episode and have Arc fetch it, because their window
    covers it **now**, on their real progress (the halfway prefetch counts
    once they are actually past 50 %; the player re-reads ``/play``, so the
    flag flips then). It is what ``demand`` accepts. Always false for a ready
    episode (it needs no fetch) and while the episode being played is itself
    pending: the client offers Next when the episode is ready **or** fetchable.
    """

    id: int
    number: int
    state: EpisodeState
    ready: bool
    fetchable: bool = False

    @classmethod
    def from_episode(cls, episode: Episode, *, fetchable: bool = False) -> EpisodeRef:
        return cls(
            id=episode.id,
            number=episode.number,
            state=episode.state,
            ready=episode.state is EpisodeState.READY,
            fetchable=fetchable,
        )


class NextSeasonRef(BaseModel):
    """The sequel's first episode, offered at a finished season's end (FR-S5 item 3).

    Set only when the episode being played has no following row, its show is
    finished there, and a ``SEQUEL`` relation resolves to a show Arc has.
    ``episode`` is the same :class:`EpisodeRef` as ``next``. Its ``fetchable``
    is what a demand will accept **once the press has touched the entry**:
    for an ``active`` entry that is today's window, for a ``dormant`` or
    ``absent`` one the window the entry will have after the touch (activated,
    or added as Watching). ``list_state`` tells the client which touch to make
    first: none, re-send the entry (FR-A9), or add it as Watching; ``dormant``
    is only ever a watching or planned entry, so that re-send never moves a
    completed one. ``aired`` is false for a sequel that is only announced or
    whose episode has not aired, so the player can say "hasn't aired yet".
    """

    anime_id: int
    title: str
    episode: EpisodeRef
    list_state: ListState
    aired: bool = True

    @classmethod
    def build(cls, found: NextSeason) -> NextSeasonRef:
        return cls(
            anime_id=found.anime.id,
            title=found.title,
            episode=EpisodeRef.from_episode(found.episode, fetchable=found.fetchable),
            list_state=found.list_state,
            aired=found.aired,
        )


class PendingOut(BaseModel):
    """An episode the caller wants that is not ready yet (FR-A14, M20).

    What the player shows while it waits: "Searching", "Downloading 43 % · 12
    seeders", "Preparing". ``progress`` is 0..1 from the download (the file's
    own share for a pack), null before anything has been reported; ``seeders``
    is the release's count when it was chosen, null before there is one.
    """

    state: EpisodeState
    progress: float | None = None
    seeders: int | None = None


class LiveOut(BaseModel):
    """A stream cut from a download still in progress (FR-P8, M20 T6).

    Set while ``/media/{id}/…`` is served from the live directory. The player
    takes ``expected_duration`` as the episode's length (never the media
    element's, which grows with the download), shows ``front_seconds`` as how
    far it may play, and ``download_progress``/``seeders`` while it waits.
    ``ended`` is the playlist having its ``ENDLIST``: the episode is whole.
    """

    front_seconds: float
    expected_duration: float
    download_progress: float
    seeders: int | None = None
    ended: bool = False


class SubtitlesOut(BaseModel):
    """The text track a remuxed rendition carries (FR-S1, FR-S10, M20 T3).

    The master playlist offers it as the default subtitle rendition; the
    player shows a CC button only when this is present. ``lang`` is FR-P2's
    choice, recorded on the rendition (``und`` when the source did not say).
    """

    lang: str

    @classmethod
    def from_rendition(cls, rendition: Rendition | None) -> SubtitlesOut | None:
        """Null for no rendition, a burn-in transcode, or a remux with no track."""
        if rendition is None or not rendition.text_subtitles:
            return None
        return cls(lang=rendition.subtitle_lang or UNDETERMINED_LANG)


class PlayInfo(BaseModel):
    """``GET /api/episodes/{id}/play`` — everything the player opens with.

    One call rather than four: the player needs the episode, the show it
    belongs to, where to point hls.js, how long the file is and where the user
    got to, and asking for those separately would mean a video element that
    can start before it knows whether it is meant to seek.
    """

    episode: EpisodeOut
    anime: AnimeSummary
    #: ``/media/{episode_id}/index.m3u8`` — derived from the id, never from
    #: anything on disk (spec §7). Sent rather than assumed so the client has
    #: one place to read it from. ``null`` exactly when ``offline_only`` or
    #: ``pending`` is set. While ``live`` is set it is the same URL, served
    #: from the download in progress.
    playlist_url: str | None
    #: The rendition's own duration in seconds. ``0`` only for a ``ready``
    #: episode whose rendition row somehow carries no duration, and for an
    #: ``offline_only`` episode the caller has never reported a duration for.
    duration: float
    #: A trip episode that lives only on the caller's devices (FR-A12, M19
    #: T4): not ``ready``, no rendition to stream, and answered only to a user
    #: holding a pending or delivered trip row on it. The player opens the
    #: device's copy, or says to keep it offline first.
    offline_only: bool = False
    #: Set when the episode is not ready but the caller wants it (FR-A14,
    #: M20): it is being searched for, downloaded or prepared, and the player
    #: polls this route until ``playlist_url`` arrives. Null otherwise.
    pending: PendingOut | None = None
    #: Set while the episode plays from its download in progress (FR-P8, M20
    #: T6); ``pending`` is null then and ``duration`` is ``expected_duration``.
    live: LiveOut | None = None
    #: Where to seek on open, or null for the beginning (FR-S2).
    resume_position: float | None = None
    #: When the caller's position for this episode was last written — the
    #: ``watch_progress`` row's ``updated_at`` (a synced offline report carries
    #: the device's moment), or null when there is no row. The client compares
    #: it with the position kept on the device and opens at the newer one
    #: (FR-S2, FR-S9; 2026-10-08).
    resume_at: datetime | None = None
    previous: EpisodeRef | None = None
    next: EpisodeRef | None = None
    #: The sequel's first episode, when ``next`` is null because the season
    #: ended here (FR-S5 item 3); null otherwise.
    next_season: NextSeasonRef | None = None
    #: The rendition's text track (FR-S10), or null when it has none — a
    #: burn-in transcode, a remux of a source with no usable text track, or
    #: an episode with nothing to stream yet.
    subtitles: SubtitlesOut | None = None


class ProgressIn(BaseModel):
    """The body of ``POST /api/progress`` (FR-S3).

    ``extra="forbid"`` because this endpoint is posted to every ten seconds by
    a client Arc ships: a misspelt field is a bug to be told about once, not a
    value to be silently dropped six times a minute.
    """

    model_config = ConfigDict(extra="forbid")

    #: Bounded like every id in a path (:data:`arc.api.deps.MAX_ID`): a number
    #: outside ``bigint`` names no row, and reaches the driver as a 500 rather
    #: than as the 404 it plainly is.
    episode_id: int = Field(ge=MIN_ID, le=MAX_ID)
    #: ``allow_inf_nan=False`` because ``1e400`` parses to ``inf`` — the JSON
    #: literals are refused a step earlier, in :func:`arc.api.playback._body`,
    #: but an overflowing decimal is a plain number until Python rounds it.
    #: Either way the value ends up dividing FR-S4's fraction, and neither
    #: infinity nor a NaN can be stored in a ``double precision`` column and
    #: mean anything afterwards.
    position_s: float = Field(ge=0, allow_inf_nan=False)
    #: The player's own reading of the file's length. Greater than zero: a
    #: report that cannot be turned into a fraction cannot answer FR-S4.
    duration_s: float = Field(gt=0, allow_inf_nan=False)


class ProgressOut(BaseModel):
    """What a progress report, or a manual mark, changed.

    ``newly_completed`` is the one the client acts on — it is what turns "the
    episode ended" into the next-episode prompt (FR-S5) — and it is true for
    exactly one request per episode per user.
    """

    completed: bool
    newly_completed: bool
    #: The show's episode count on the caller's list after the change, or null
    #: when nothing touched the list (every report but the completing one).
    list_progress: int | None = None


#: The most episode ids one ``GET /api/progress/completed`` may ask about.
MAX_COMPLETED_IDS = 200


class CompletedEpisode(BaseModel):
    """One of the caller's completed episodes, and when it was completed."""

    episode_id: int
    #: ``watch_progress.completed_at`` (set once, cleared by an un-mark), or
    #: the row's ``updated_at`` for a completion older than that column.
    completed_at: datetime


class CompletedOut(BaseModel):
    """``GET /api/progress/completed`` — which of the asked episodes the caller has completed.

    Only the caller's own completion rows: an episode watched on the list's
    word alone (FR-W5's ``progress`` source) has no row and is not here. The
    device uses it to mark its copies of episodes completed elsewhere, or
    before it learnt to listen (FR-S9 item 9, owner 2026-10-08).
    """

    completed: list[CompletedEpisode]


__all__ = [
    "MAX_COMPLETED_IDS",
    "CompletedEpisode",
    "CompletedOut",
    "EpisodeRef",
    "NextSeasonRef",
    "PendingOut",
    "PlayInfo",
    "ProgressIn",
    "ProgressOut",
]
