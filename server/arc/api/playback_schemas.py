"""Request and response shapes for the player (FR-S2, FR-S3, FR-S4, FR-W3).

Their own module for the same reason :mod:`arc.api.anime_schemas` and
:mod:`arc.api.schedule_schemas` are: the client's types are generated from
exactly these, and the episode and show shapes are *reused* rather than
restated, so the card the player renders above the video is the same
``EpisodeOut`` the show page renders in a list.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from arc.api.anime_schemas import AnimeSummary, EpisodeOut
from arc.api.deps import MAX_ID, MIN_ID
from arc.models import Episode, EpisodeState


class EpisodeRef(BaseModel):
    """The neighbour of the episode being played (FR-S5).

    Deliberately thinner than :class:`~arc.api.anime_schemas.EpisodeOut`: the
    player needs to know whether there *is* a next episode and whether it can
    be played, and rendering a whole episode row for a link would mean the
    three side lookups for two episodes nobody is watching.

    ``ready`` is derived from ``state`` and sent anyway, so the client can
    decide whether to offer "next episode" without repeating the rule.
    """

    id: int
    number: int
    state: EpisodeState
    ready: bool

    @classmethod
    def from_episode(cls, episode: Episode) -> EpisodeRef:
        return cls(
            id=episode.id,
            number=episode.number,
            state=episode.state,
            ready=episode.state is EpisodeState.READY,
        )


class PlayInfo(BaseModel):
    """``GET /api/episodes/{id}/play`` — everything the player opens with.

    One call rather than four: the player needs the episode, the show it
    belongs to, where to point hls.js, how long the file is and where the user
    got to, and asking for those separately would mean a video element that
    can start before it knows whether it is meant to seek.
    """

    episode: EpisodeOut
    anime: AnimeSummary
    #: Always ``/media/{episode_id}/index.m3u8`` — derived from the id, never
    #: from anything on disk (spec §7). Sent rather than assumed so the client
    #: has one place to read it from.
    playlist_url: str
    #: The rendition's own duration in seconds. ``0`` only for a ``ready``
    #: episode whose rendition row somehow carries no duration.
    duration: float
    #: Where to seek on open, or null for the beginning (FR-S2).
    resume_position: float | None = None
    previous: EpisodeRef | None = None
    next: EpisodeRef | None = None


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


__all__ = ["EpisodeRef", "PlayInfo", "ProgressIn", "ProgressOut"]
