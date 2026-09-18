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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.api.episode_extras import EpisodeRelease
from arc.api.schemas import OverrideOut
from arc.core.text import trim_middle
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    ListEntry,
    ListStatus,
    Rendition,
    Want,
)
from arc.services.acquisition.dormancy import is_dormant
from arc.services.acquisition.wants import SlotView
from arc.services.catalog import preferred_title
from arc.services.catalog.airing import (
    FINISHED,
    RELEASING,
    aired_through,
    is_aired,
    next_airing_at,
    next_airing_episode,
    out_of_order,
)
from arc.services.playback.watched import WatchedSource, watched_source


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
    #: The key visual at AniList's largest size, or null (M15). The client
    #: prefers it and falls back to ``cover_url``: a row filled from MAL has
    #: only the 230 px ``main_picture.large``, which is soft at card size, and
    #: sending the small one under the big one's name would hide that.
    cover_large_url: str | None = None
    #: The caller's own list state for this show, or null if it is not on it.
    list_status: ListStatus | None = None


class AnimeSummary(AnimeCore):
    """A search result or a list row: enough for a card.

    Four of these fields — ``genres``, ``banner_url``, ``backdrop_url`` and
    ``studio`` — are filled from columns no search payload carries, and are the
    one place this schema is not a straight reading of "what a search returned"
    (M15: Home's hero needs the banner, Browse's chips need the genres, My
    List's rows credit the studio). Three of them are **detail** columns;
    ``backdrop_url`` is the TMDB enrichment's alone (§5.8), which is why it is
    the one field here that no catalogue write path can blank.

    They are read off the stored row, which is the only reason it is safe: the
    API renders ``anime`` rows, not payloads, so a row that has ever been
    detail-fetched carries them however the caller arrived at it. A row that
    has only ever been seen in a search page carries none — an empty list and
    two nulls — until somebody opens it or a refresh sweep reaches it. That is
    the honest answer and the client renders the card without them; the
    alternative, asking AniList for genres and studios on every keystroke, is
    twenty extra fields per search page and two hundred per season sweep for a
    chip.

    What must *not* happen is these moving into the summary write path: a
    search payload carries no genres, no banner and no studio, so writing them
    as summary values would blank the detail columns of every row a search
    result passes over (rule 2 in :mod:`arc.services.catalog.cache`).
    """

    #: AniList's total episode count; null while a show airs without one.
    episodes: int | None = None
    #: How many people have the show on a list (AniList ``popularity``, MAL
    #: ``num_list_users``), and the mean score on AniList's 0–100 scale — MAL's
    #: 0–10 is scaled on the way in, so one number means one thing whichever
    #: source answered. Unlike the three fields above these really are summary
    #: columns: both are in AniList's search fragment, so a card carries them
    #: the moment a search returns it.
    #:
    #: Null on a source that publishes neither and on an unaired show with no
    #: rating yet, so a client must not render "0 %" for an absent score.
    popularity: int | None = None
    average_score: int | None = None
    #: From ``anime.genres``; empty on a row no detail fetch has reached.
    genres: list[str] = Field(default_factory=list)
    #: AniList's banner behind Home's and the show page's hero, or null. A
    #: 4.75:1 strip, which is why the client prefers ``backdrop_url`` and only
    #: shows this one where its measured ratio allows it.
    banner_url: str | None = None
    #: TMDB's 16:9 backdrop, or null (owner, 2026-09-13). The art the 21:9
    #: heroes and the 16:9 cards prefer: ``bannerArt`` reads this first and
    #: falls back to ``banner_url``. Null on a row the TMDB enrichment has not
    #: reached, and on every row of a deployment with no ``TMDB_API_KEY`` —
    #: which is why it is a preference and not a replacement.
    backdrop_url: str | None = None
    #: The main studio, credited on a card like an auteur (M15), or null.
    studio: str | None = None

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
            cover_large_url=anime.cover_large_url,
            popularity=anime.popularity,
            average_score=anime.average_score,
            genres=list(anime.genres or []),
            banner_url=anime.banner_url,
            backdrop_url=anime.backdrop_url,
            studio=anime.studio,
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


class CreditOut(BaseModel):
    """One row of the show page's "Made by" block (M15).

    ``role`` is one of the six labels in
    :data:`arc.services.catalog.credits.CREDIT_ORDER` and the list arrives in
    that order, studio first, so the client renders it as it comes rather than
    grouping or sorting it itself. Rows with an unmapped role never reach the
    column, so nothing here has to be filtered out.
    """

    role: str
    name: str

    @classmethod
    def from_blob(cls, raw: dict[str, Any]) -> CreditOut | None:
        """One stored ``anime.credits`` entry, or ``None`` if it is not one.

        JSONB written by a past version of Arc, or by hand, must not be able
        to put a null or an object where the client expects two strings.
        """
        role = raw.get("role")
        name = raw.get("name")
        if not isinstance(role, str) or not isinstance(name, str) or not role or not name:
            return None
        return cls(role=role, name=name)


@dataclass(frozen=True, slots=True)
class RelatedAnime:
    """The columns a franchise-rail card needs, for a relation Arc has cached.

    Not a response shape — it is what the detail route's one lookup hands to
    :meth:`RelationOut.from_blob`. A plain record of six columns rather than
    the ``Anime`` row itself, because a show page can name a dozen relations
    and loading a dozen full rows would drag a dozen synopses and relation
    blobs through the session to render six fields.
    """

    id: int
    cover_url: str | None = None
    cover_large_url: str | None = None
    format: str | None = None
    episodes: int | None = None
    season_year: int | None = None


class RelationOut(BaseModel):
    """A sequel/prequel/side story, as the show page links to it.

    ``id`` is Arc's internal id and is **null** until Arc has a row for the
    related show — which is most of them, since a relation is a title nobody
    has necessarily opened yet. The client links only the ones that have one
    and renders the rest as plain text; the external ids are there so a future
    "add this" can resolve them.

    The artwork and the counts (M15's "The franchise, in order" rail) come from
    that local row and are therefore **null on exactly the relations ``id`` is
    null on**. Nothing is fetched to fill them: a relation is a title nobody
    has necessarily asked for, and a show page that fetched a dozen of them
    would cost twelve upstream requests against a 30/min budget to render a
    rail nobody may scroll to. They fill in when somebody opens the related
    show, or when a sweep reaches it.

    ``format`` is the exception: it is a fact the *source* stated about the
    relation, so it is in the stored blob and is present whether or not Arc has
    a row. The local row only fills it in when the blob did not carry one.
    """

    id: int | None = None
    anilist_id: int | None = None
    mal_id: int | None = None
    relation_type: str
    title: TitleOut
    format: str | None = None
    #: From the cached row, or null when there is none. The rail renders a
    #: 2:3 card, so it prefers ``cover_large_url`` and falls back.
    cover_url: str | None = None
    cover_large_url: str | None = None
    #: "TV · 23 episodes · 2021" — the kind line under a franchise card.
    episodes: int | None = None
    season_year: int | None = None

    @classmethod
    def from_blob(
        cls, raw: dict[str, Any], related: dict[tuple[str, int], RelatedAnime] | None = None
    ) -> RelationOut | None:
        anilist_id = raw.get("anilist_id")
        mal_id = raw.get("mal_id")
        if anilist_id is None and mal_id is None:
            return None
        found = related or {}
        row: RelatedAnime | None = None
        if anilist_id is not None:
            row = found.get(("anilist", int(anilist_id)))
        if row is None and mal_id is not None:
            row = found.get(("mal", int(mal_id)))
        return cls(
            id=row.id if row is not None else None,
            anilist_id=anilist_id,
            mal_id=mal_id,
            relation_type=str(raw.get("relation_type") or "OTHER"),
            title=TitleOut.from_blob(raw.get("title")),
            format=raw.get("format") or (row.format if row is not None else None),
            cover_url=row.cover_url if row is not None else None,
            cover_large_url=row.cover_large_url if row is not None else None,
            episodes=row.episodes if row is not None else None,
            season_year=row.season_year if row is not None else None,
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
    #: Whether this episode is one selected file of a batch (FR-A4, FR-A11).
    #: The group and the title are the *pack's*, which is exactly what they are
    #: for a member of it, and the percentage beside them is the file's own —
    #: so the flag is what keeps "1080p · [Judas]" on episode 7 of a
    #: twenty-six-episode pack from reading as a release of episode 7.
    batch: bool = False

    @classmethod
    def from_release(cls, release: EpisodeRelease) -> ReleaseOut:
        return cls(
            group=release.torrent.group,
            resolution=release.torrent.resolution,
            title=release.torrent.title,
            seeders=release.torrent.seeders,
            batch=release.batch,
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


#: Episode states in which "what did the last search do?" is worth showing
#: (FR-A7). While Arc is still looking — the episode is wanted, or a search is
#: running — and while it has given up and is retrying daily, which is the
#: state a person is most likely to be staring at. In every other state the
#: file's own progress is the answer and the search is history.
SEARCH_STATES: frozenset[EpisodeState] = frozenset(
    {EpisodeState.WANTED, EpisodeState.SEARCHING, EpisodeState.UNAVAILABLE}
)


class SearchOut(BaseModel):
    """What the last search for this episode asked and saw (FR-A7).

    The show page's ``Searching · 6 forms, 0 results · next try 23:26``, and
    the whole of the answer to "it has said Searching all day — is it broken?".
    Three of the four fields are the stamp ``search_release`` leaves on the
    episode; ``next_at`` is the ``run_after`` of the queued job that will look
    again, because FR-A6's retry schedule lives in the job row and not in a
    column.

    Sent only while the episode is in one of :data:`SEARCH_STATES` and only
    once a search has actually run: a row that has never been looked for says
    nothing rather than "0 forms, 0 results", which would read as a failure.
    """

    #: When the last attempt ran.
    at: datetime
    #: How many query forms it ran (:func:`~arc.services.acquisition.nyaa.
    #: queries`, after the dedupe and the cap).
    forms: int
    #: Distinct releases they returned between them, before the filter. Zero is
    #: the interesting number: it means Nyaa has never heard of any name Arc
    #: asked by, which is a query problem rather than a filter problem.
    results: int
    #: When the next attempt is queued for, or null when none is.
    next_at: datetime | None = None


def _search_out(episode: Episode, next_at: datetime | None) -> SearchOut | None:
    """:class:`SearchOut` for an episode Arc is still looking for, or ``None``."""
    if episode.state not in SEARCH_STATES or episode.last_search_at is None:
        return None
    return SearchOut(
        at=episode.last_search_at,
        forms=episode.last_search_forms or 0,
        results=episode.last_search_results or 0,
        next_at=next_at,
    )


class EpisodeOut(BaseModel):
    """One row of the show page's episode list."""

    id: int
    number: int
    title: str | None = None
    #: The 16:9 episode thumbnail, or null (M15). Null is the ordinary case for
    #: an episode that has not aired and for any show whose detail came from
    #: MAL, so the client always has a placeholder for it.
    still_url: str | None = None
    air_at: datetime | None = None
    #: True when ``air_at`` was worked out from a MAL broadcast slot rather
    #: than published per episode (FR-C6), and also when the list itself
    #: contradicts the published date (:func:`~arc.services.catalog.airing.
    #: out_of_order`). The client badges both as estimated; the first is
    #: replaced the next time AniList answers, the second the next time the
    #: source fixes its own typo.
    air_at_estimated: bool = False
    #: Whether the episode has aired *now*, so the client does not have to
    #: compare against a clock the server may disagree with.
    aired: bool
    state: EpisodeState
    #: Whether the caller has watched it (FR-W5). *Either* a completion row of
    #: Arc's own — 90 % or the manual mark (FR-S4, FR-W3) — *or* an episode
    #: number at or below their ``list_entries.progress``, which is how a list
    #: imported from MyAnimeList at episode 9 marks nine episodes watched with
    #: no completion rows behind them. :func:`~arc.services.playback.progress.
    #: watched_source` is the rule; this is every renderer's copy of it.
    watched: bool = False
    #: Which of those two it was, or null when the episode is not watched
    #: (FR-W5). The show page's control reads it: ``arc`` is a mark it can take
    #: back ("Unwatch"), ``progress`` is the list's word and un-marking it
    #: would do nothing, so the control says "Watched" and is not a button.
    watched_source: WatchedSource | None = None
    #: 0..1 while the episode is downloading, null otherwise (FR-A7). A
    #: fraction rather than a percentage: the client formats it, and a server
    #: that already rounded has thrown away the difference between 99.4 % and
    #: 99.6 %.
    download_progress: float | None = None
    #: Why the search gave up, in a sentence (FR-A6, FR-A7). Only ever set
    #: while the state is ``unavailable``.
    unavailable_reason: str | None = None
    #: What the last search asked and saw, while Arc is still looking
    #: (:class:`SearchOut`, :data:`SEARCH_STATES`). Null everywhere else, and
    #: null until the first attempt has run.
    search: SearchOut | None = None
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
        out_of_order: bool = False,
        completed: bool = False,
        list_progress: int = 0,
        release: EpisodeRelease | None = None,
        rendition: Rendition | None = None,
        transcode_job: Job | None = None,
        next_search_at: datetime | None = None,
    ) -> EpisodeOut:
        """``boundary`` is the list's :func:`aired_through`; see that module.

        ``out_of_order`` is the other half the whole list knows and one row
        cannot: whether this episode's published date falls after a
        higher-numbered episode's. Defaulted to false for the callers that
        render a single episode out of its list (the home page's shelves),
        where the stored flag is the only one there is to report.

        ``completed`` and ``list_progress`` are the caller's two halves of
        FR-W5, and **this is the only place that combines them**: a show page,
        a home tile and the player all render the same flag, and a second
        implementation of "watched" is how the show page's tick and the tile's
        come to disagree about episode 6. Both default to the values of a user
        who has no list entry and no completion, which is what "not watched"
        is made of.
        """
        prepare = PrepareState.from_job(transcode_job, episode.state)
        source = watched_source(episode.number, completed=completed, list_progress=list_progress)
        return cls(
            id=episode.id,
            number=episode.number,
            title=episode.title,
            still_url=episode.still_url,
            air_at=episode.air_at,
            air_at_estimated=episode.air_at_estimated or out_of_order,
            aired=is_aired(episode, now=now, anime_status=anime_status, boundary=boundary),
            state=episode.state,
            watched=source is not None,
            watched_source=source,
            download_progress=(
                release.progress
                if release is not None and episode.state in PROGRESS_STATES
                else None
            ),
            unavailable_reason=episode.unavailable_reason,
            search=_search_out(episode, next_search_at),
            release=ReleaseOut.from_release(release) if release is not None else None,
            prepare_progress=prepare.progress,
            failure_reason=prepare.failure_reason,
            rendition=(
                RenditionOut.from_rendition(rendition, notes=prepare.notes)
                if rendition is not None and episode.state is EpisodeState.READY
                else None
            ),
        )


class MalSyncOut(BaseModel):
    """How one show stands with MyAnimeList (FR-M6).

    The badge on the show page. ``state`` is ``synced``, ``pending``,
    ``failed`` or ``unlinked``;
    :func:`arc.services.mal.writelog.sync_state` decides which, and its
    docstring explains the precedence between them.
    """

    state: str
    #: The message from the most recent failed write, when there is one.
    error: str | None = None
    #: When Arc last successfully wrote anything about this show to MAL.
    last_write_at: datetime | None = None


class SampleOut(BaseModel):
    """The caller's live "try episode 1" want on this show (FR-A8).

    Present on a show page only while the sample is live — cancelled, or
    dropped for going unwatched for D days (FR-T2), and it is gone — so the
    client reads its absence as "there is nothing to cancel". The episode's own
    acquisition state is in ``episodes[]`` where it always was; this says who
    asked for it, which no episode row can.
    """

    episode_id: int
    episode_number: int
    requested_at: datetime
    #: The episode's state as of this response — ``wanted`` immediately after a
    #: request, on an episode that was resting. Sent so the client can render
    #: the row it just changed without waiting on a refetch to tell it
    #: something the server already knew (FR-A7, FR-A8).
    state: EpisodeState

    @classmethod
    def build(cls, want: Want, episode: Episode) -> SampleOut:
        return cls(
            episode_id=want.episode_id,
            episode_number=episode.number,
            requested_at=want.created_at,
            state=episode.state,
        )


#: Why a show is waiting, in the order the page should believe them: a stopped
#: reconciler outranks a full cap, because while acquisition is paused or held
#: the cap is not what is keeping this show from fetching.
WaitingReason = Literal["paused", "held", "slot"]


def _waiting_reason(slots: SlotView | None) -> WaitingReason:
    """Which sentence a waiting show's note should carry (FR-A10, FR-T6)."""
    if slots is not None and slots.paused:
        return "paused"
    if slots is not None and slots.held:
        return "held"
    return "slot"


class ListEntryOut(BaseModel):
    """(user, anime) → status, progress, score (FR-W2)."""

    model_config = ConfigDict(from_attributes=True)

    anime_id: int
    status: ListStatus
    progress: int
    score: int | None = None
    updated_at: datetime
    #: When the user first touched this show in Arc, or null (FR-A9).
    activated_at: datetime | None = None
    #: Whether this entry is generating no wants: never touched here, and the
    #: show is not airing (FR-A9). Derived rather than stored, because the
    #: airing half of it changes on its own — a dormant entry stops being
    #: dormant the day its show starts broadcasting, with nothing written.
    dormant: bool = False
    #: Whether the per-user slot cap is holding this show back (FR-A10): the
    #: entry is activated and wanting, it has something to fetch, and K of the
    #: user's shows are already fetching. Derived like ``dormant`` and stored
    #: nowhere — a slot frees itself when an episode becomes ready.
    waiting: bool = False
    #: *Why* it is waiting, when it is: ``"slot"`` (K of the caller's shows are
    #: fetching), ``"paused"`` (an admin stopped acquisition) or ``"held"`` (the
    #: disk is under FR-T6's floor). Null when the show is not waiting. The
    #: page needs it because "Arc starts this one when one of them finishes" is
    #: a promise, and the last two make it a false one — nothing finishing
    #: starts anything while the reconciler is stopped.
    waiting_reason: WaitingReason | None = None
    #: How many of the caller's shows are fetching right now, and K. The two
    #: numbers the show page's waiting note says out loud ("5 of your 5 shows
    #: are fetching"), which no user can read off the admin status endpoint.
    #: Both 0 when the caller did not ask for the slot picture.
    fetching_count: int = 0
    slot_cap: int = 0
    #: MyAnimeList sync state (M9). Populated only on a show page, where one
    #: extra query per response is nothing; a list of fifty rows would be
    #: fifty, and none of them is rendered there.
    mal_sync: MalSyncOut | None = None

    @classmethod
    def build(
        cls,
        entry: ListEntry,
        *,
        anime_status: str | None,
        mal_sync: MalSyncOut | None = None,
        slots: SlotView | None = None,
    ) -> ListEntryOut:
        """One entry, with FR-A9's ``dormant`` worked out from the show.

        ``anime_status`` rather than the ``Anime`` row because that is the only
        field the rule reads, and every caller already has the show in hand —
        these schemas take no session, so a field they had to look up would be
        a query nobody expected.

        ``slots`` is FR-A10's picture of the caller, and it is passed in for
        exactly the same reason: working it out costs a pass over the user's
        list. Like ``mal_sync`` it is populated **only on a show page**, which
        is the one place the waiting note is rendered; everywhere else the
        fields read as "nothing to say" rather than as "not waiting", which is
        the honest answer from a response that did not ask.
        """
        waiting = slots is not None and entry.anime_id in slots.waiting
        out = cls.model_validate(entry)
        return out.model_copy(
            update={
                "dormant": is_dormant(entry, airing=anime_status == RELEASING),
                "waiting": waiting,
                "waiting_reason": _waiting_reason(slots) if waiting else None,
                "fetching_count": 0 if slots is None else slots.fetching,
                "slot_cap": 0 if slots is None else slots.cap,
                "mal_sync": mal_sync,
            }
        )


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


def _entry_out(
    entry: ListEntry | None,
    mal_sync: MalSyncOut | None,
    *,
    anime_status: str | None = None,
    slots: SlotView | None = None,
) -> ListEntryOut | None:
    """A list entry with its MAL badge attached, or ``None`` if there is none.

    The badge is passed in rather than looked up here: these schemas take no
    session, deliberately, so that building a response can never turn into a
    query nobody expected. ``anime_status`` is there for the same reason, and
    carries FR-A9's airing exception; ``slots`` carries FR-A10's cap.
    """
    if entry is None:
        return None
    return ListEntryOut.build(entry, anime_status=anime_status, mal_sync=mal_sync, slots=slots)


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
    #: The "Made by" block, studio first (M15). Empty when nothing is known,
    #: and one row long on a show whose detail came from MAL — which publishes
    #: no staff — so the client renders whatever arrives rather than expecting
    #: six rows. ``studio`` above is the same fact and stays for the meta line.
    credits: list[CreditOut] = Field(default_factory=list)
    banner_url: str | None = None
    #: TMDB's 16:9 backdrop, or null — the same field ``AnimeSummary`` carries,
    #: and what the show page's hero prefers over the banner strip above it.
    backdrop_url: str | None = None
    #: Whether the offline cross-id map can reach this show on TMDB at all
    #: (§5.8's ``_mapped``). Not a claim that pictures exist — it is the
    #: difference between "the stills are on their way" and "there are none to
    #: come", which is the only honest thing an episode list with striped
    #: placeholders can say, and it is what keeps the client from polling a
    #: show TMDB has never heard of (owner, 2026-09-13).
    tmdb_mapped: bool = False
    next_airing: NextAiringOut | None = None
    relations: list[RelationOut] = Field(default_factory=list)
    list_entry: ListEntryOut | None = None
    #: The caller's live sample want, or null (FR-A8). Null is the ordinary
    #: case: a sample is something a user asked for on this one show.
    sample: SampleOut | None = None
    #: This show's per-show rule override (FR-A3), **for an admin only** — null
    #: for everybody else and for a show that follows the global rules (M16,
    #: owner 2026-09-18). It is on the show payload rather than behind a route
    #: of its own so that the page that edits it makes no extra request: the
    #: cost is one ``settings`` lookup on a request an admin was making anyway.
    override: OverrideOut | None = None

    @classmethod
    def build(
        cls,
        anime: Anime,
        *,
        episodes: list[Episode],
        now: datetime,
        list_entry: ListEntry | None = None,
        mal_sync: MalSyncOut | None = None,
        completed: frozenset[int] = frozenset(),
        related: dict[tuple[str, int], RelatedAnime] | None = None,
        releases: dict[int, EpisodeRelease] | None = None,
        renditions: dict[int, Rendition] | None = None,
        transcode_jobs: dict[int, Job] | None = None,
        next_searches: dict[int, datetime] | None = None,
        sample: SampleOut | None = None,
        slots: SlotView | None = None,
        tmdb_mapped: bool = False,
        override: OverrideOut | None = None,
    ) -> AnimeDetail:
        raw_relations = [raw for raw in (anime.relations or []) if isinstance(raw, dict)]
        boundary = aired_through(
            episodes,
            now=now,
            anime_status=anime.status,
            next_airing=anime.next_airing,
        )
        unordered = out_of_order(episodes)
        relations = [
            relation
            for relation in (RelationOut.from_blob(raw, related) for raw in raw_relations)
            if relation is not None
        ]
        credits = [
            credit
            for credit in (
                CreditOut.from_blob(raw) for raw in (anime.credits or []) if isinstance(raw, dict)
            )
            if credit is not None
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
            cover_large_url=anime.cover_large_url,
            list_status=list_entry.status if list_entry is not None else None,
            episode_count=anime.episodes,
            synopsis=anime.description,
            genres=list(anime.genres or []),
            studio=anime.studio,
            credits=credits,
            banner_url=anime.banner_url,
            backdrop_url=anime.backdrop_url,
            tmdb_mapped=tmdb_mapped,
            next_airing=NextAiringOut.from_blob(anime.next_airing),
            relations=relations,
            list_entry=_entry_out(list_entry, mal_sync, anime_status=anime.status, slots=slots),
            sample=sample,
            override=override,
            episodes=[
                EpisodeOut.from_episode(
                    episode,
                    now=now,
                    anime_status=anime.status,
                    boundary=boundary,
                    out_of_order=episode.number in unordered,
                    # FR-W5's two halves: Arc's own completions, and the
                    # progress the list carries — which this method already
                    # holds, so the watched marks cost no query of their own.
                    completed=episode.id in completed,
                    list_progress=list_entry.progress if list_entry is not None else 0,
                    release=(releases or {}).get(episode.id),
                    rendition=(renditions or {}).get(episode.id),
                    transcode_job=(transcode_jobs or {}).get(episode.id),
                    next_search_at=(next_searches or {}).get(episode.id),
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
    "CreditOut",
    "EpisodeOut",
    "ListEntryOut",
    "ListEntryPatch",
    "ListRow",
    "MAX_FAILURE_CHARS",
    "MalSyncOut",
    "PROGRESS_STATES",
    "NextAiringOut",
    "PrepareState",
    "SearchOut",
    "RelatedAnime",
    "RelationOut",
    "ReleaseOut",
    "RenditionOut",
    "SampleOut",
    "SearchPage",
    "TitleOut",
    "WaitingReason",
    "aired_through",
    "is_aired",
]
