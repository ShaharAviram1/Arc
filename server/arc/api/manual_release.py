"""Choosing or changing the release for an episode or a trip by hand (FR-A13).

Four routes, thin over :mod:`arc.services.acquisition.manual`. Every account
except the demo may use them; the work is the service's.

* ``GET /api/episodes/{id}/releases`` → :class:`ReleasesOut` — the ranked
  candidates for an episode the caller wants (a live window, sample or trip
  want of theirs; otherwise 404), each with whether Arc would take it and why
  not, and the download running now. Searching Nyaa happens here, at most once
  a minute per person per episode; a second request inside the minute answers
  the same list.
* ``POST /api/episodes/{id}/release`` ``{candidate_id}`` or ``{link}`` → **202**
  :class:`ReleaseChosenOut`: the chosen release replaces the running one.
* ``GET /api/trips/{id}/releases`` and ``POST /api/trips/{id}/release`` — the
  same for the caller's active trip, packs listed first, and a chosen pack
  replaces what the trip's pending episodes were downloading from.

Refusals carry ``detail: {code, message}``, ``message`` a sentence a person can
be shown: **403** ``demo_account``; **404** ``not_wanted`` / ``trip_not_found``;
**409** ``storage_held``, ``acquisition_paused``, ``packs_off``, ``busy``,
``already_arrived``, ``trip_not_active``, ``nothing_to_fetch``; **422**
``host_not_allowed``, ``bad_link``, ``magnet_unknown``, ``unknown_candidate``,
``pack_selects_nothing``, ``already_tried``, ``already_downloading``,
``wrong_episode``, ``wrong_show`` and the rest; **429** ``search_running`` /
``too_many_searches`` / ``searches_busy``; **502** Nyaa, **503** qBittorrent.

Unlike the admin acquisition routes, the choice does its work **in the
request** (and still answers 202, because the bytes are not here yet): the
refusals the owner asked for — a pack that would select nothing for the caller
— can only be known after the pack's file list has been read, and a person who
pasted a link is waiting to be told. Nothing here touches a list entry or
MyAnimeList.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError

from arc.api.deps import MAX_ID, MIN_ID, CurrentUser, EpisodeId, SessionDep, SettingsDep
from arc.models import EpisodeState
from arc.services.acquisition.manual import (
    BUSY,
    Choice,
    Chosen,
    Current,
    ManualError,
    Releases,
    Scope,
    choose_release,
    discard_added,
    episode_scope,
    list_releases,
    trip_scope,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["acquisition"])

TripId = Annotated[int, PathParam(ge=MIN_ID, le=MAX_ID, description="A trip row id.")]


class ReleaseCandidateOut(BaseModel):
    """One release the sheet lists (FR-A13)."""

    #: A short stable hash of the info hash: what ``POST …/release`` takes.
    id: str
    title: str
    group: str | None = None
    resolution: str | None = None
    #: As Nyaa writes it (``"1.4 GiB"``); shown, never compared.
    size: str | None = None
    seeders: int
    leechers: int
    kind: Literal["single", "batch"]
    trusted: bool
    #: The caller's wanted episode numbers this release would serve; null for a
    #: pack whose name gives no range (its file list decides when chosen).
    covers: list[int] | None = None
    #: Whether Arc would take it on its own terms; ``reason`` says why not.
    acceptable: bool
    reason: str | None = None
    #: The release the episode is downloading now.
    current: bool = False

    @classmethod
    def build(cls, choice: Choice) -> ReleaseCandidateOut:
        return cls(
            id=choice.id,
            title=choice.item.title,
            group=choice.parsed.group,
            resolution=choice.parsed.resolution,
            size=choice.item.size,
            seeders=choice.item.seeders,
            leechers=choice.item.leechers,
            kind=choice.kind,
            trusted=choice.item.trusted,
            covers=None if choice.covers is None else list(choice.covers),
            acceptable=choice.acceptable,
            reason=choice.reason,
            current=choice.current,
        )


class CurrentReleaseOut(BaseModel):
    """The download running now, if there is one."""

    title: str | None = None
    kind: Literal["single", "batch"]
    #: qBittorrent's state string, or one of Arc's decisions.
    state: str | None = None
    #: 0..1, the episode's own file for a pack.
    progress: float | None = None
    #: Chosen by hand (FR-A13).
    manual: bool = False

    @classmethod
    def build(cls, current: Current) -> CurrentReleaseOut:
        return cls(
            title=current.title,
            kind=current.kind,
            state=current.state,
            progress=current.progress,
            manual=current.manual,
        )


class ReleasesOut(BaseModel):
    """``GET …/releases``."""

    scope: Literal["episode", "trip"]
    scope_id: int
    #: The episode number searched for (the trip's first pending one).
    number: int
    candidates: list[ReleaseCandidateOut]
    current: CurrentReleaseOut | None = None
    #: True when this is the list from under a minute ago (one search a minute).
    cached: bool = False
    searched_seconds_ago: int = 0

    @classmethod
    def build(cls, releases: Releases) -> ReleasesOut:
        scope = releases.scope
        return cls(
            scope=scope.kind,
            scope_id=scope.id,
            number=scope.number,
            candidates=[ReleaseCandidateOut.build(choice) for choice in releases.choices],
            current=None if releases.current is None else CurrentReleaseOut.build(releases.current),
            cached=releases.cached,
            searched_seconds_ago=releases.searched_seconds_ago,
        )


class ReleaseChoiceIn(BaseModel):
    """``POST …/release``: a listed candidate, or a pasted link — one of the two."""

    candidate_id: str | None = Field(default=None, max_length=64)
    link: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _one_of(self) -> ReleaseChoiceIn:
        if (self.candidate_id is None) == (self.link is None):
            raise ValueError("give exactly one of candidate_id and link")
        return self


class ChosenEpisodeOut(BaseModel):
    episode_id: int
    number: int
    state: EpisodeState


class ReleaseChosenOut(BaseModel):
    """What a choice did."""

    title: str
    kind: Literal["single", "batch"]
    info_hash: str
    episodes: list[ChosenEpisodeOut]

    @classmethod
    def build(cls, chosen: Chosen) -> ReleaseChosenOut:
        return cls(
            title=chosen.title,
            kind=chosen.kind,
            info_hash=chosen.info_hash,
            episodes=[
                ChosenEpisodeOut(episode_id=episode.id, number=episode.number, state=episode.state)
                for episode in sorted(chosen.episodes, key=lambda row: row.number)
            ],
        )


def _http(exc: ManualError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


_REFUSALS: dict[int | str, dict[str, str]] = {
    403: {"description": "demo_account"},
    404: {"description": "not_wanted | trip_not_found"},
    409: {"description": "storage_held | acquisition_paused | packs_off | busy | …"},
    429: {"description": "search_running | too_many_searches | searches_busy"},
    422: {"description": "host_not_allowed | unknown_candidate | pack_selects_nothing | …"},
    502: {"description": "nyaa_unavailable"},
    503: {"description": "qbit_unavailable"},
}


async def _list(scope: Scope, session: SessionDep, settings: SettingsDep) -> ReleasesOut:
    try:
        releases = await list_releases(session, settings, scope)
    except ManualError as exc:
        raise _http(exc) from exc
    return ReleasesOut.build(releases)


async def _choose(
    scope: Scope, body: ReleaseChoiceIn, session: SessionDep, settings: SettingsDep
) -> ReleaseChosenOut:
    # Every hash this request hands the torrent client: taken back out, with
    # its files, if the choice is refused afterwards or the commit fails, so a
    # rolled-back choice leaves no orphan behind.
    added: list[str] = []
    try:
        chosen = await choose_release(
            session, settings, scope, candidate=body.candidate_id, link=body.link, added=added
        )
        # Built before the commit, while the rows are loaded.
        out = ReleaseChosenOut.build(chosen)
        await session.commit()
    except ManualError as exc:
        # Undo the release of the old download along with everything else: a
        # refused choice leaves the episode exactly as it was.
        await session.rollback()
        await discard_added(settings, added)
        raise _http(exc) from exc
    except IntegrityError as exc:
        # Two choices of one release at once: the loser is told to try again.
        await session.rollback()
        await discard_added(settings, added)
        raise _http(BUSY) from exc
    except BaseException:
        await session.rollback()
        await discard_added(settings, added)
        raise
    return out


@router.get(
    "/api/episodes/{episode_id}/releases",
    response_model=ReleasesOut,
    summary="The releases an episode could be downloaded from (FR-A13)",
    responses=_REFUSALS,
)
async def episode_releases(
    episode_id: EpisodeId, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> ReleasesOut:
    try:
        scope = await episode_scope(session, user, episode_id)
    except ManualError as exc:
        raise _http(exc) from exc
    return await _list(scope, session, settings)


@router.post(
    "/api/episodes/{episode_id}/release",
    response_model=ReleaseChosenOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Download an episode from the release you choose (FR-A13)",
    responses=_REFUSALS,
)
async def choose_episode_release(
    episode_id: EpisodeId,
    body: ReleaseChoiceIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ReleaseChosenOut:
    try:
        scope = await episode_scope(session, user, episode_id)
    except ManualError as exc:
        raise _http(exc) from exc
    return await _choose(scope, body, session, settings)


@router.get(
    "/api/trips/{trip_id}/releases",
    response_model=ReleasesOut,
    summary="The packs and releases a trip could be downloaded from (FR-A13)",
    responses=_REFUSALS,
)
async def trip_releases(
    trip_id: TripId, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> ReleasesOut:
    try:
        scope = await trip_scope(session, user, trip_id)
    except ManualError as exc:
        raise _http(exc) from exc
    return await _list(scope, session, settings)


@router.post(
    "/api/trips/{trip_id}/release",
    response_model=ReleaseChosenOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Download a trip from the pack you choose (FR-A13)",
    responses=_REFUSALS,
)
async def choose_trip_release(
    trip_id: TripId,
    body: ReleaseChoiceIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> ReleaseChosenOut:
    try:
        scope = await trip_scope(session, user, trip_id)
    except ManualError as exc:
        raise _http(exc) from exc
    return await _choose(scope, body, session, settings)


__all__ = [
    "CurrentReleaseOut",
    "ReleaseCandidateOut",
    "ReleaseChoiceIn",
    "ReleaseChosenOut",
    "ReleasesOut",
    "router",
]
