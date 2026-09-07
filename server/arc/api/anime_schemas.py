"""Response shapes for the catalogue and list endpoints (M3).

Separate from :mod:`arc.api.schemas` because there are a dozen of them and
they belong together: two routers (``anime`` and ``list``) share
``AnimeSummary``, ``ListEntryOut`` and the episode shape, and the client's
types are generated from exactly these.

Everything here is built by an explicit ``from_*`` classmethod rather than by
``from_attributes``. The models and the API disagree on purpose — the API
groups the three title columns into an object and adds a computed
``preferred``, turns AniList's epoch seconds into a datetime, and decides
``aired`` and ``watched`` against the caller and the clock — and a
constructor makes each of those decisions visible in one place.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arc.core.text import trim_middle
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    Rendition,
    Torrent,
)
from arc.services.catalog import preferred_title
from arc.services.catalog.airing import (
    FINISHED,
    RELEASING,
    aired_through,
    is_aired,
    next_airing_at,
    next_airing_episode,
)


class TitleOut(BaseModel):
    """The three titles plus the one to render."""

    romaji: str | None = None
    english: str | None = None
    native: str | None = None
    #: English if there is one, else romaji. Never null, so the client never
    #: has to write the fallback chain itself.
    preferred: str

    @classmethod
    def from_anime(cls, anime: Anime) -> TitleOut:
        return cls(
            romaji=anime.title_romaji,
            english=anime.title_english,
            native=anime.title_native,
            preferred=preferred_title(anime),
        )

    @classmethod
    def from_blob(cls, raw: dict[str, Any] | None) -> TitleOut:
        """A title stored inside ``anime.relations`` JSONB."""
        raw = raw or {}
        romaji = raw.get("romaji")
        english = raw.get("english")
        native = raw.get("native")
        return cls(
            romaji=romaji,
            english=english,
            native=native,
            preferred=raw.get("preferred") or english or romaji or native or "",
        )


class AnimeCore(BaseModel):
    """The fields a summary and a detail response agree on.

    They differ over one name — ``episodes`` is the episode *count* on a card
    and the episode *list* on a show page — so the shared half is a base class
    rather than the summary itself. Nothing returns an ``AnimeCore``.
    """

    #: Arc's own id. Every route that names an anime takes this one; the
    #: external ids below are for display and for linking out, never for
    #: addressing (FR-C6).
    id: int
    anilist_id: int | None = None
    mal_id: int | None = None
    #: Which catalogue source this record came from ("anilist" | "mal", or null
    #: for a row nothing has filled in yet). The client shows a "catalogue via
    #: MAL" notice on ``"mal"``.
    source: str | None = None
    title: TitleOut
    format: str | None = None
    status: str | None = None
    season: str | None = None
    season_year: int | None = None
    cover_url: str | None = None
    #: The caller's own list state for this show, or null if it is not on it.
    list_status: ListStatus | None = None


class AnimeSummary(AnimeCore):
    """A search result or a list row: enough for a card."""

    #: AniList's total episode count; null while a show airs without one.
    episodes: int | None = None

    @classmethod
    def from_anime(cls, anime: Anime, list_status: ListStatus | None = None) -> AnimeSummary:
        return cls(
            id=anime.id,
            anilist_id=anime.anilist_id,
            mal_id=anime.mal_id,
            # A card renders from the summary columns, so it reports which
            # source wrote *those* — a row whose detail came from AniList can
            # still carry a MAL-sourced cover after an outage.
            source=anime.summary_source,
            title=TitleOut.from_anime(anime),
            format=anime.format,
            episodes=anime.episodes,
            status=anime.status,
            season=anime.season,
            season_year=anime.season_year,
            cover_url=anime.cover_url,
            list_status=list_status,
        )


class SearchPage(BaseModel):
    """One page of catalogue search results (FR-C1)."""

    results: list[AnimeSummary]
    page: int
    has_next: bool


class NextAiringOut(BaseModel):
    """When the next episode airs, if the show is still airing.

    Only AniList publishes this, so a show whose detail came from MAL has none
    even while it is airing; the episode list's ``air_at`` values carry the
    estimate instead.
    """

    episode: int
    at: datetime

    @classmethod
    def from_blob(cls, raw: dict[str, Any] | None) -> NextAiringOut | None:
        """AniList's ``nextAiringEpisode`` as stored, or ``None``.

        Null for the slot Arc synthesises from a MAL broadcast time as well: it
        knows when the next episode airs but not which one, and a show page
        that named an episode number it had guessed would be worse than one
        that says nothing. The schedule renders that case from the raw blob
        instead (:mod:`arc.services.catalog.schedule`).
        """
        episode = next_airing_episode(raw)
        at = next_airing_at(raw)
        if episode is None or at is None:
            return None
        return cls(episode=episode, at=at)


class RelationOut(BaseModel):
    """A sequel/prequel/side story, as the show page links to it.

    ``id`` is Arc's internal id and is **null** until Arc has a row for the
    related show — which is most of them, since a relation is a title nobody
    has necessarily opened yet. The client links only the ones that have one
    and renders the rest as plain text; the external ids are there so a future
    "add this" can resolve them.
    """

    id: int | None = None
    anilist_id: int | None = None
    mal_id: int | None = None
    relation_type: str
    title: TitleOut
    format: str | None = None

    @classmethod
    def from_blob(
        cls, raw: dict[str, Any], internal_ids: dict[tuple[str, int], int] | None = None
    ) -> RelationOut | None:
        anilist_id = raw.get("anilist_id")
        mal_id = raw.get("mal_id")
        if anilist_id is None and mal_id is None:
            return None
        found = internal_ids or {}
        internal = None
        if anilist_id is not None:
            internal = found.get(("anilist", int(anilist_id)))
        if internal is None and mal_id is not None:
            internal = found.get(("mal", int(mal_id)))
        return cls(
            id=internal,
            anilist_id=anilist_id,
            mal_id=mal_id,
            relation_type=str(raw.get("relation_type") or "OTHER"),
            title=TitleOut.from_blob(raw.get("title")),
            format=raw.get("format"),
        )

    @staticmethod
    def external_ids(anime: Anime) -> tuple[set[int], set[int]]:
        """The AniList and MAL ids named by ``anime``'s relations.

        The endpoint looks these up in one query and passes the answers to
        :meth:`from_blob`; doing it per relation would be a round trip each.
        """
        anilist_ids: set[int] = set()
        mal_ids: set[int] = set()
        for raw in anime.relations or []:
            if not isinstance(raw, dict):
                continue
            if raw.get("anilist_id") is not None:
                anilist_ids.add(int(raw["anilist_id"]))
            if raw.get("mal_id") is not None:
                mal_ids.add(int(raw["mal_id"]))
        return anilist_ids, mal_ids


class ReleaseOut(BaseModel):
    """The Nyaa release Arc picked for an episode (FR-A3, FR-A7).

    Shown so a user can see *what* is being downloaded, and so an admin can
    tell at a glance that the ranking rules did what they were meant to. It
    survives the download: the row stays after the file is matched, and
    "which group's encode is this?" is a question people ask of an episode
    they are about to watch.
    """

    #: Release group as the filename parser read it; null when the name had
    #: none, which happens with scene-style releases.
    group: str | None = None
    resolution: str | None = None
    #: The raw release title. The client renders it in a tooltip.
    title: str | None = None
    #: Seeders **at pick time**, not now — it is what the ranker saw.
    seeders: int | None = None

    @classmethod
    def from_torrent(cls, torrent: Torrent) -> ReleaseOut:
        return cls(
            group=torrent.group,
            resolution=torrent.resolution,
            title=torrent.title,
            seeders=torrent.seeders,
        )


class RenditionOut(BaseModel):
    """The browser-ready output for an episode (FR-P1, FR-P2).

    Present only once the episode is ``ready``, and deliberately not a URL:
    M8's ``/media/{episode_id}/index.m3u8`` is derived from the episode id, so
    a path here would be a second way to address the same file and the one a
    client would be tempted to trust (spec §7).

    ``subtitle_lang`` is null when the source had no usable text track and the
    episode was prepared without subtitles — which FR-P2 allows and asks to be
    flagged, and this is the flag. ``notes`` is the sentence behind the flag.
    """

    duration: float | None = None
    width: int | None = None
    height: int | None = None
    subtitle_lang: str | None = None
    audio_lang: str | None = None
    #: What the plan had to settle for, in whole sentences: "the only subtitle
    #: tracks are bitmap (hdmv_pgs_subtitle); nothing was burned in", "no en
    #: subtitle track; used pt instead". Usually empty. Additive and
    #: order-preserving — a client that does not render them loses nothing, and
    #: one that does can explain a rendition with no subtitles without the user
    #: having to ask an admin to read a log.
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def from_rendition(cls, rendition: Rendition, *, notes: Sequence[str] = ()) -> RenditionOut:
        return cls(
            duration=rendition.duration,
            width=rendition.width,
            height=rendition.height,
            subtitle_lang=rendition.subtitle_lang,
            audio_lang=rendition.audio_lang,
            notes=list(notes),
        )


#: Longest failure detail shown on an episode row. The whole stderr tail is in
#: the job payload and the admin queue view; this is the sentence-or-two a
#: person reads on the show page before deciding to press retry (FR-P4).
MAX_FAILURE_CHARS = 500


class PrepareState(BaseModel):
    """What the latest ``transcode`` job says about one episode (FR-P4).

    Progress, the failure tail and the plan's notes live in that job's payload
    rather than in columns (:mod:`arc.services.media.jobs` explains why), so
    this is the one place that knows the payload's shape. Each field is empty
    except in the state it belongs to: a percentage on a ``ready`` episode is
    noise, a stale error under an episode that has since succeeded is a lie,
    and notes are what the *finished* rendition had to settle for, so they are
    read only once there is one.
    """

    progress: float | None = None
    failure_reason: str | None = None
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def from_job(cls, job: Job | None, state: EpisodeState) -> PrepareState:
        payload = job.payload if job is not None else {}
        notes = _notes(payload)
        if state is EpisodeState.PREPARING:
            raw = payload.get("progress")
            value = float(raw) if isinstance(raw, int | float) else 0.0
            return cls(progress=min(max(value, 0.0), 1.0))
        if state is EpisodeState.FAILED:
            detail = payload.get("error_tail")
            if not isinstance(detail, str) or not detail.strip():
                detail = job.last_error if job is not None else None
            if not detail:
                return cls()
            # Both ends, not the last 500 characters. The first line names the
            # failure ("ffmpeg exited 1") and the last lines are ffmpeg's own
            # complaint; a plain suffix would keep the complaint and drop the
            # name, leaving a sentence that begins mid-diagnostic.
            return cls(failure_reason=trim_middle(detail, limit=MAX_FAILURE_CHARS))
        return cls(notes=notes)


def _notes(payload: dict[str, Any]) -> list[str]:
    """The plan notes the last transcode recorded, if it recorded any.

    Written to the payload by the handler on success only, so they describe
    the rendition that is on disk. Anything that is not a list of strings is
    ignored rather than trusted: the payload is JSONB and a hand-edited row
    must not be able to put an object into the API's response.
    """
    raw = payload.get("notes")
    if not isinstance(raw, list):
        return []
    return [note for note in raw if isinstance(note, str)]


#: Episode states whose download percentage means something to a user (FR-A7).
#: Before ``downloading`` there is nothing to report and after ``downloaded``
#: the number would be a permanent 100 % on an episode that is already
#: playable, which reads as "still working" rather than as "done".
PROGRESS_STATES: frozenset[EpisodeState] = frozenset(
    {EpisodeState.DOWNLOADING, EpisodeState.DOWNLOADED}
)


class EpisodeOut(BaseModel):
    """One row of the show page's episode list."""

    id: int
    number: int
    title: str | None = None
    air_at: datetime | None = None
    #: True when ``air_at`` was worked out from a MAL broadcast slot rather
    #: than published per episode (FR-C6). The client badges these as
    #: estimated; they are replaced the next time AniList answers.
    air_at_estimated: bool = False
    #: Whether the episode has aired *now*, so the client does not have to
    #: compare against a clock the server may disagree with.
    aired: bool
    state: EpisodeState
    #: Whether the caller finished it: ``watch_progress.completed`` for this
    #: (user, episode), set at 90 % or by the manual mark (FR-S4, FR-W3).
    watched: bool = False
    #: 0..1 while the episode is downloading, null otherwise (FR-A7). A
    #: fraction rather than a percentage: the client formats it, and a server
    #: that already rounded has thrown away the difference between 99.4 % and
    #: 99.6 %.
    download_progress: float | None = None
    #: Why the search gave up, in a sentence (FR-A6, FR-A7). Only ever set
    #: while the state is ``unavailable``.
    unavailable_reason: str | None = None
    #: The release Arc picked, once it has picked one.
    release: ReleaseOut | None = None
    #: 0..1 while the episode is ``preparing``, null otherwise (FR-P4). Zero
    #: means "queued, or ffmpeg has not reported yet", not "stuck": the
    #: transcode reports every few seconds once it is encoding.
    prepare_progress: float | None = None
    #: The tail of the last transcode failure, trimmed for display. Only ever
    #: set while the state is ``failed`` (FR-P4); the whole thing is in the
    #: job's payload and in the admin queue view.
    failure_reason: str | None = None
    #: The prepared output, once the episode is ``ready`` (FR-P1).
    rendition: RenditionOut | None = None

    @classmethod
    def from_episode(
        cls,
        episode: Episode,
        *,
        now: datetime,
        anime_status: str | None,
        boundary: int = 0,
        watched: bool = False,
        torrent: Torrent | None = None,
        rendition: Rendition | None = None,
        transcode_job: Job | None = None,
    ) -> EpisodeOut:
        """``boundary`` is the list's :func:`aired_through`; see that module."""
        prepare = PrepareState.from_job(transcode_job, episode.state)
        return cls(
            id=episode.id,
            number=episode.number,
            title=episode.title,
            air_at=episode.air_at,
            air_at_estimated=episode.air_at_estimated,
            aired=is_aired(episode, now=now, anime_status=anime_status, boundary=boundary),
            state=episode.state,
            watched=watched,
            download_progress=(
                torrent.progress
                if torrent is not None and episode.state in PROGRESS_STATES
                else None
            ),
            unavailable_reason=episode.unavailable_reason,
            release=ReleaseOut.from_torrent(torrent) if torrent is not None else None,
            prepare_progress=prepare.progress,
            failure_reason=prepare.failure_reason,
            rendition=(
                RenditionOut.from_rendition(rendition, notes=prepare.notes)
                if rendition is not None and episode.state is EpisodeState.READY
                else None
            ),
        )


class ListEntryOut(BaseModel):
    """(user, anime) → status, progress, score (FR-W2)."""

    model_config = ConfigDict(from_attributes=True)

    anime_id: int
    status: ListStatus
    progress: int
    score: int | None = None
    updated_at: datetime


class ListEntryPatch(BaseModel):
    """The body of ``PUT /api/list/{anime_id}``.

    Every field is optional so the same endpoint serves "add as watching",
    "set my score" and "I'm on episode 4". ``status`` is required only when
    the entry does not exist yet, which no schema can express — the service
    raises and the router answers 422.
    """

    model_config = ConfigDict(extra="forbid")

    status: ListStatus | None = None
    progress: int | None = Field(default=None, ge=0)
    #: ``null`` clears the score; omitting the field leaves it alone. Pydantic
    #: cannot tell those apart, so the router checks ``model_fields_set``.
    score: int | None = Field(default=None, ge=1, le=10)


class ListRow(BaseModel):
    """One row of ``GET /api/list``: the show and the caller's state for it."""

    anime: AnimeSummary
    entry: ListEntryOut


class AnimeDetail(AnimeCore):
    """Everything the show page renders.

    ``episodes`` here is the episode *list*, not the count — that is what the
    show page needs under that name, and it is what the M3 API contract
    specifies. The count is still available, as ``episode_count``.

    ``source`` here is the *detail* source: which catalogue filled the synopsis,
    genres and relations, and therefore whether the air dates below are
    published or estimated (FR-C6).
    """

    #: One row per episode, 1..N, in order.
    episodes: list[EpisodeOut] = Field(default_factory=list)
    #: The total episode count; null while a show airs without one. The same
    #: value ``AnimeSummary.episodes`` carries on a card.
    episode_count: int | None = None
    synopsis: str | None = None
    genres: list[str] = Field(default_factory=list)
    studio: str | None = None
    banner_url: str | None = None
    next_airing: NextAiringOut | None = None
    relations: list[RelationOut] = Field(default_factory=list)
    list_entry: ListEntryOut | None = None

    @classmethod
    def build(
        cls,
        anime: Anime,
        *,
        episodes: list[Episode],
        now: datetime,
        list_entry: ListEntry | None = None,
        watched: frozenset[int] = frozenset(),
        relation_ids: dict[tuple[str, int], int] | None = None,
        torrents: dict[int, Torrent] | None = None,
        renditions: dict[int, Rendition] | None = None,
        transcode_jobs: dict[int, Job] | None = None,
    ) -> AnimeDetail:
        raw_relations = [raw for raw in (anime.relations or []) if isinstance(raw, dict)]
        boundary = aired_through(
            episodes,
            now=now,
            anime_status=anime.status,
            next_airing=anime.next_airing,
        )
        relations = [
            relation
            for relation in (RelationOut.from_blob(raw, relation_ids) for raw in raw_relations)
            if relation is not None
        ]
        return cls(
            id=anime.id,
            anilist_id=anime.anilist_id,
            mal_id=anime.mal_id,
            source=anime.detail_source,
            title=TitleOut.from_anime(anime),
            format=anime.format,
            status=anime.status,
            season=anime.season,
            season_year=anime.season_year,
            cover_url=anime.cover_url,
            list_status=list_entry.status if list_entry is not None else None,
            episode_count=anime.episodes,
            synopsis=anime.description,
            genres=list(anime.genres or []),
            studio=anime.studio,
            banner_url=anime.banner_url,
            next_airing=NextAiringOut.from_blob(anime.next_airing),
            relations=relations,
            list_entry=(ListEntryOut.model_validate(list_entry) if list_entry else None),
            episodes=[
                EpisodeOut.from_episode(
                    episode,
                    now=now,
                    anime_status=anime.status,
                    boundary=boundary,
                    watched=episode.id in watched,
                    torrent=(torrents or {}).get(episode.id),
                    rendition=(renditions or {}).get(episode.id),
                    transcode_job=(transcode_jobs or {}).get(episode.id),
                )
                for episode in episodes
            ],
        )


#: ``FINISHED``, ``RELEASING``, ``aired_through`` and ``is_aired`` are
#: re-exported rather than defined here: the aired rule moved into
#: :mod:`arc.services.catalog.airing` when the home page started needing it
#: too (FR-C4), and one rule with two definitions is how a show page and a
#: "behind by N" come to disagree about episode 6.
__all__ = [
    "FINISHED",
    "RELEASING",
    "AnimeCore",
    "AnimeDetail",
    "AnimeSummary",
    "EpisodeOut",
    "ListEntryOut",
    "ListEntryPatch",
    "ListRow",
    "MAX_FAILURE_CHARS",
    "PROGRESS_STATES",
    "NextAiringOut",
    "PrepareState",
    "RelationOut",
    "ReleaseOut",
    "RenditionOut",
    "SearchPage",
    "TitleOut",
    "aired_through",
    "is_aired",
]
