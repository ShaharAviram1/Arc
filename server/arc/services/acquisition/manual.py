"""Choosing or changing the release for an episode or a trip by hand (FR-A13).

The owner's request of 2026-10-06: a person who can see that Arc picked a dead
release, or that a better pack exists, can point the episode — or their whole
trip — at a release of their choosing. Every account except the demo may do it.
It is **not** a "download anything" door (spec §8): it only redirects an
episode or a trip the caller has already asked for, and the release it takes
goes through the same add paths and the same byte guarantee as an automatic
pick.

Two halves.

**Listing** (:func:`list_releases`): the same Nyaa forms and the same request
budget ``search_for_episode`` spends — title forms, the group-narrowed forms for
a finished show, and the batch forms — asked on demand, with every result of
this show classified instead of filtered: :func:`classify` keeps a release Arc
would not take *beside* the sentence it would not take it for (wrong season, no
seeders, a thinly seeded pack for a trip, a release Arc already tried). Results
whose title is another show are left out. A person may search one episode (or
one trip) once a minute; a second request inside that answers the cached list.

**Choosing** (:func:`choose_release`): a candidate from that list, or a pasted
magnet / Nyaa link (:func:`parse_link`; nyaa.si and the configured Nyaa host
only). The chosen release **replaces** what the episode — or every pending
episode of the trip that the pack holds — was downloading from: a single is
marked ``cancelled`` and removed with its partial files by ``qbit_cancel``, a
pack's claim is given back through ``claims.release_files`` and
``qbit_reselect``. Then the release is taken through the existing paths:

* a **single** is added as a magnet like ``search_release`` adds one;
* a **pack** is fetched as a ``.torrent``, added stopped and read
  (``jobs._add_stopped``), and handed to ``jobs._take_batch`` as an already-held
  pack, which writes the selection, verifies it and only then starts it
  (FR-A11) — with the caller's wanted episodes of the show (window, sample and
  trip) as the only episodes it may select;
* a pack Arc **already has** is attached to, file by file, like
  ``batch.claim_existing`` does.

A pasted link Arc finds no file of the episode in is refused (422) and the
torrent removed before a byte moved. A pasted **magnet** is only accepted when
the search pool holds its info hash: a magnet carries no file list, and taking
an unknown one would fetch its whole payload. The row is marked ``manual`` so
nothing automatic replaces it on its own; the stall rule still applies and says
"the release you chose stalled".

Nothing here reads or writes a list entry, a want, or anything of MyAnimeList.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import re
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Literal
from urllib.parse import parse_qs, urlparse

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import (
    Anime,
    Episode,
    EpisodeState,
    Job,
    Torrent,
    TorrentFile,
    TorrentKind,
    Trip,
    TripEpisode,
    TripEpisodeState,
    TripState,
    User,
    Want,
)
from arc.services.acquisition import batch, jobs
from arc.services.acquisition import nyaa as nyaa_module
from arc.services.acquisition.claims import enqueue_reselect, live_claim, release_files
from arc.services.acquisition.nyaa import (
    MAX_REQUESTS,
    Candidate,
    NyaaClient,
    NyaaItem,
    NyaaUnavailable,
    Ranked,
)
from arc.services.acquisition.qbit import (
    DECIDED_STATES,
    QBIT_CANCELLED,
    QBIT_MISSING,
    QBIT_REJECTED,
    QBIT_STALLED,
    QBIT_UNREADABLE,
    FileInfo,
    QbitClient,
    QbitError,
    QbitUnavailable,
)
from arc.services.acquisition.rules import (
    Rules,
    batch_fallback,
    is_paused,
    is_storage_held,
    load_rules,
)
from arc.services.acquisition.states import transition
from arc.services.acquisition.wants import enqueue_cancel
from arc.services.jobs.registry import JobContext
from arc.services.library.parser import ParsedName, parse
from arc.services.trips.rules import needs_rendition, trip_only_episode_ids, trip_pack_min_seeders

log = logging.getLogger(__name__)

# --- Refusals ----------------------------------------------------------------


class ManualError(Exception):
    """Why a release cannot be listed or chosen: a code and a sentence.

    ``code`` is stable for a client to branch on; ``message`` is what a person
    is shown, already a sentence.
    """

    status: int = 400

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ManualForbidden(ManualError):
    status = 403


class ManualNotFound(ManualError):
    status = 404


class ManualConflict(ManualError):
    status = 409


class ManualInvalid(ManualError):
    status = 422


class ManualUpstream(ManualError):
    """Nyaa could not be reached (502)."""

    status = 502


class ManualUnavailable(ManualError):
    """qBittorrent could not be reached (503)."""

    status = 503


class ManualTooMany(ManualError):
    """Too many live searches (429)."""

    status = 429


DEMO = ManualForbidden("demo_account", "The demo account cannot change releases.")
NOT_WANTED_EPISODE = ManualNotFound(
    "not_wanted", "You have not asked for this episode, so there is no release to change."
)
TRIP_NOT_FOUND = ManualNotFound("trip_not_found", "No such trip.")
TRIP_NOT_ACTIVE = ManualConflict("trip_not_active", "This trip has ended.")
NOTHING_TO_FETCH = ManualConflict(
    "nothing_to_fetch", "Every episode of this trip has already arrived."
)
ALREADY_ARRIVED = ManualConflict(
    "already_arrived", "This episode has already arrived; there is no download to change."
)
STORAGE_HELD = ManualConflict(
    "storage_held", "The server is short of disk space, so Arc is not fetching anything new."
)
PAUSED = ManualConflict("acquisition_paused", "An admin has paused fetching.")
PACKS_OFF = ManualConflict("packs_off", "An admin has switched packs off, so Arc cannot take one.")
BUSY = ManualConflict(
    "busy", "Arc is choosing a release for this right now. Try again in a minute."
)
UNKNOWN_CANDIDATE = ManualInvalid(
    "unknown_candidate", "That release is not in the last list. Open the list again."
)
NO_CHOICE = ManualInvalid("no_choice", "Choose a release from the list or paste a link.")
BAD_LINK = ManualInvalid(
    "bad_link",
    "That is not a magnet link or a Nyaa page or .torrent link.",
)
HOST_NOT_ALLOWED = ManualInvalid(
    "host_not_allowed", "Arc only takes links to Nyaa ({hosts}), or a magnet link."
)
WRONG_SHOW = ManualInvalid(
    "wrong_show", "That release is another show, so Arc will not take it for this one."
)
SEARCH_RUNNING = ManualTooMany(
    "search_running", "Arc is already searching for you. Wait for that list first."
)
SEARCHES_PER_MINUTE = ManualTooMany(
    "too_many_searches", "That is three searches in a minute. Try again shortly."
)
SEARCHES_BUSY = ManualTooMany(
    "searches_busy", "Arc is searching Nyaa for others right now. Try again in a moment."
)
MAGNET_UNKNOWN = ManualInvalid(
    "magnet_unknown",
    "Arc cannot see what that magnet holds before downloading it. "
    "Paste its Nyaa page link instead.",
)


# --- Links --------------------------------------------------------------------


#: Nyaa's own host. Always on the allowlist, beside the configured one.
NYAA_HOST: Final[str] = "nyaa.si"

_NYAA_PATH = re.compile(r"^/(?:view|download)/([0-9]{1,12})(?:\.torrent)?/?$")
_HEX_HASH = re.compile(r"^[0-9a-fA-F]{40}$")
_B32_HASH = re.compile(r"^[A-Za-z2-7]{32}$")


@dataclass(frozen=True, slots=True)
class Link:
    """A pasted link, understood: a magnet's info hash, or a Nyaa torrent id."""

    info_hash: str | None = None
    nyaa_id: int | None = None


def allowed_hosts(nyaa_url: str) -> frozenset[str]:
    """nyaa.si and the host Arc is configured to search (``NYAA_URL``)."""
    configured = (urlparse(nyaa_url).hostname or "").lower()
    return frozenset(host for host in (NYAA_HOST, configured) if host)


def parse_link(raw: str, *, nyaa_url: str) -> Link:
    """A magnet URI or a Nyaa ``/view/<id>`` / ``/download/<id>.torrent`` URL.

    Pure. The URL must be ``https`` (or the configured Nyaa URL's own scheme)
    on :func:`allowed_hosts`; anything else is refused with a sentence before
    any request is made. A link's path is never fetched as given: only the
    numeric id is kept, and the ``.torrent`` URL is rebuilt on the configured
    host (:func:`torrent_url`).
    """
    text = raw.strip()
    if text.lower().startswith("magnet:"):
        query = parse_qs(urlparse(text).query)
        for value in query.get("xt", []):
            if not value.lower().startswith("urn:btih:"):
                continue
            digest = value[len("urn:btih:") :]
            if _HEX_HASH.match(digest):
                return Link(info_hash=digest.lower())
            if _B32_HASH.match(digest):
                try:
                    return Link(info_hash=base64.b32decode(digest.upper()).hex())
                except binascii.Error:
                    break
        raise BAD_LINK
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.hostname:
        raise BAD_LINK
    hosts = allowed_hosts(nyaa_url)
    host = parsed.hostname.lower()
    if host not in hosts:
        raise ManualInvalid(
            HOST_NOT_ALLOWED.code, HOST_NOT_ALLOWED.message.format(hosts=", ".join(sorted(hosts)))
        )
    schemes = {"https", urlparse(nyaa_url).scheme.lower()}
    if parsed.scheme.lower() not in schemes:
        raise BAD_LINK
    match = _NYAA_PATH.match(parsed.path)
    if match is None:
        raise BAD_LINK
    return Link(nyaa_id=int(match.group(1)))


def torrent_url(nyaa_url: str, nyaa_id: int) -> str:
    """The ``.torrent`` URL for a Nyaa id, on the configured host."""
    return f"{nyaa_url.rstrip('/')}/download/{nyaa_id}.torrent"


def nyaa_id_of(url: str) -> int | None:
    """The Nyaa torrent id out of a feed item's ``<link>``, if it has one."""
    match = _NYAA_PATH.match(urlparse(url).path)
    return int(match.group(1)) if match else None


# --- A .torrent's identity ----------------------------------------------------


_DIGITS = re.compile(rb"[0-9]{1,9}")

#: Most values one ``.torrent`` may hold before Arc stops reading it. The blob
#: is at most 1 MiB, so this bounds the work whatever the bytes say.
MAX_BENCODE_VALUES: Final[int] = 200_000


def _length_at(blob: bytes, start: int) -> tuple[int, int]:
    """A string's ``(length, payload start)``: ASCII digits only, then ``:``."""
    colon = blob.find(b":", start, start + 11)
    if colon <= start or not _DIGITS.fullmatch(blob[start:colon]):
        raise ValueError(f"bad bencode string length at byte {start}")
    length = int(blob[start:colon])
    if colon + 1 + length > len(blob):
        raise ValueError("bencode string runs past the end")
    return length, colon + 1


def _bencode_end(blob: bytes, start: int) -> int:
    """Where the bencoded value starting at ``start`` ends (exclusive).

    Iterative, strict and bounded: lengths are ASCII digits only (no sign,
    underscore or space, which ``int()`` would accept), every step moves
    forward, nesting is capped at 64 and the number of values at
    :data:`MAX_BENCODE_VALUES`. Anything else raises ``ValueError``.
    """
    position = start
    depth = 0
    seen = 0
    while True:
        seen += 1
        if seen > MAX_BENCODE_VALUES:
            raise ValueError("bencode holds too many values")
        head = blob[position : position + 1]
        before = position
        if head == b"i":
            end = blob.find(b"e", position + 1)
            if end < 0 or not re.fullmatch(rb"-?[0-9]+", blob[position + 1 : end]):
                raise ValueError(f"bad bencode integer at byte {position}")
            position = end + 1
        elif head in (b"l", b"d"):
            depth += 1
            if depth > 64:
                raise ValueError("bencode nested too deeply")
            position += 1
        elif head == b"e" and depth > 0:
            depth -= 1
            position += 1
        else:
            length, payload = _length_at(blob, position)
            position = payload + length
        if position <= before:  # pragma: no cover - every branch advances
            raise ValueError("bencode made no progress")
        # Closing markers of the containers this value opened.
        while depth > 0 and blob[position : position + 1] == b"e":
            depth -= 1
            position += 1
        if depth == 0:
            return position
        if position >= len(blob):
            raise ValueError("bencode ended inside a container")


def _dict_items(blob: bytes, start: int) -> Iterable[tuple[bytes, int, int]]:
    """``(key, value start, value end)`` for each entry of the dict at ``start``."""
    if blob[start : start + 1] != b"d":
        raise ValueError("not a bencoded dict")
    position = start + 1
    while blob[position : position + 1] != b"e":
        if position >= len(blob):
            raise ValueError("bencode ended inside a dict")
        length, payload = _length_at(blob, position)
        key = blob[payload : payload + length]
        value_start = payload + length
        value_end = _bencode_end(blob, value_start)
        if value_end <= position:  # pragma: no cover - _bencode_end advances
            raise ValueError("bencode made no progress")
        yield key, value_start, value_end
        position = value_end


def torrent_identity(blob: bytes) -> tuple[str, str]:
    """``(info hash, name)`` of a v1 ``.torrent``. Pure; raises ``ValueError``.

    The SHA-1 of the bencoded ``info`` dict, byte for byte as it sits in the
    file — which is what a client computes, and what ``add_file`` then has the
    client confirm. Nothing else is decoded: the file list is the client's to
    report (``torrents/files``), as it is for every pack.
    """
    if len(blob) > nyaa_module.MAX_TORRENT_BYTES:
        raise ValueError("torrent too large")
    for key, start, end in _dict_items(blob, 0):
        if key != b"info":
            continue
        name = ""
        for inner, value_start, _value_end in _dict_items(blob, start):
            if inner == b"name":
                length, payload = _length_at(blob, value_start)
                name = blob[payload : payload + length].decode("utf-8", errors="replace")
        return hashlib.sha1(blob[start:end], usedforsecurity=False).hexdigest(), name
    raise ValueError("the torrent has no info dictionary")


# --- Classifying a pool ---------------------------------------------------------

Kind = Literal["single", "batch"]


@dataclass(frozen=True, slots=True)
class Tried:
    """What Arc already has under one info hash."""

    state: str | None
    kind: TorrentKind
    episode_id: int | None


@dataclass(frozen=True, slots=True)
class Choice:
    """One release as the list shows it, and everything a take needs.

    ``covers`` is the caller's wanted episode numbers the release would serve:
    one number for a single, the wanted numbers inside a pack's named range for
    a pack, and ``None`` for a pack that names no range (its file list says).
    ``listed`` is false for a result of another show, which is kept only so a
    pasted link can still be resolved.
    """

    id: str
    item: NyaaItem
    parsed: ParsedName
    kind: Kind
    acceptable: bool
    reason: str | None
    covers: tuple[int, ...] | None
    offset: int = 0
    span: tuple[int, ...] = ()
    similarity: float = 0.0
    listed: bool = True
    current: bool = False
    #: Built from a pasted ``.torrent`` rather than a feed item: its kind is
    #: unknown, so it always takes the pack path, file by file (FR-A11).
    from_file: bool = False
    #: The ``.torrent`` itself, when it was already fetched (from-file).
    blob: bytes | None = field(default=None, repr=False)

    @property
    def info_hash(self) -> str:
        return self.item.info_hash

    def ranked(self) -> Ranked:
        """The :class:`~arc.services.acquisition.nyaa.Ranked` the add paths take."""
        return Ranked(
            candidate=Candidate(
                item=self.item,
                parsed=self.parsed,
                title_similarity=self.similarity,
                offset=self.offset,
                covers=self.span if self.kind == "batch" else (),
            ),
            group_rank=0,
            resolution_rank=0,
            seeders=self.item.seeders,
            trusted=self.item.trusted,
            reasons=("chosen by hand",),
        )


def candidate_id(info_hash: str) -> str:
    """A short stable id for one release: a hash of its info hash."""
    return hashlib.sha256(info_hash.lower().encode()).hexdigest()[:16]


#: The sentence for the release an episode is downloading now.
DOWNLOADING_NOW: Final[str] = "this is the release downloading now"

#: The sentence for a release Arc has already decided about.
TRIED: Final[Mapping[str, str]] = {
    QBIT_STALLED: "Arc already tried this release and it stalled",
    QBIT_REJECTED: "its file was rejected in review",
    QBIT_CANCELLED: "Arc is removing this release right now",
    QBIT_UNREADABLE: "Arc could not identify the files in this pack",
    QBIT_MISSING: "this release went missing from the torrent client",
}


def tried_reason(tried: Tried, target_ids: Collection[int]) -> tuple[bool, str] | None:
    """``(choosable, sentence)`` for a release Arc already has a row for, or ``None``.

    A pack Arc holds and has not decided about is choosable and needs no
    sentence (the take attaches to it).
    """
    if tried.state in batch.UNATTACHABLE_STATES:
        return False, TRIED.get(tried.state or "", "Arc already tried this release")
    if tried.kind is TorrentKind.SINGLE:
        if tried.episode_id in target_ids:
            return False, DOWNLOADING_NOW
        return False, "another episode is downloading this release"
    return None


def _span_covers(span: Sequence[int], offset: int, wanted: Collection[int]) -> tuple[int, ...]:
    held = set(span)
    return tuple(sorted(number for number in set(wanted) if number + offset in held))


def classify(
    items: Iterable[NyaaItem],
    *,
    anime: Anime,
    number: int,
    rules: Rules,
    offset: int | None,
    wanted_numbers: Collection[int],
    tried: Mapping[str, Tried],
    target_ids: Collection[int],
    thin_floor: int | None,
    packs_first: bool,
) -> list[Choice]:
    """Every result of this show, best first, each with whether Arc would take it.

    Pure. The judgement is :func:`~arc.services.acquisition.nyaa.acceptable`'s
    with batches allowed, so "acceptable" here means what it means to the
    automatic pick, plus three facts the feed cannot know: a release Arc
    already tried (:func:`tried_reason`), a pack below a trip's seeder floor
    (``thin_floor``), and the release downloading now. Results whose **title**
    is another show are kept but not listed.

    Order: acceptable releases by FR-A3's ranking (singles before packs, or
    packs first for a trip), then the rest by seeders.
    """
    titles = nyaa_module.anime_titles(anime)
    season = nyaa_module.anime_season(anime)
    single_entry = nyaa_module.is_single(anime)
    accepted: list[tuple[Candidate, NyaaItem]] = []
    refused: list[Choice] = []
    for item in items:
        why: list[str] = []
        found = nyaa_module.acceptable(
            item,
            titles=titles,
            number=number,
            season=season,
            single=single_entry,
            year=anime.season_year,
            offset=offset,
            batches=True,
            why=why,
        )
        if found is not None:
            accepted.append((found, item))
            continue
        parsed = parse(item.title)
        # Taken as one file only when its name says it is one episode; anything
        # else — a range, a BATCH marker, a name with no number at all (the
        # 20 GB "[Judas] Show [BD 1080p]") — is taken file by file (FR-A11).
        kind: Kind = (
            "single"
            if parsed.kind == "episode"
            and parsed.episode is not None
            and parsed.episode_end is None
            and not parsed.is_batch
            else "batch"
        )
        refusal = why[-1] if why else "Arc would not take this release"
        span = parsed.episode_span
        refused.append(
            Choice(
                id=candidate_id(item.info_hash),
                item=item,
                parsed=parsed,
                kind=kind,
                acceptable=False,
                reason=refusal,
                covers=(
                    (_span_covers(span, 0, wanted_numbers) if span else None)
                    if kind == "batch"
                    else ((parsed.episode,) if parsed.episode is not None else (number,))
                ),
                span=span,
                listed=not refusal.startswith("title "),
            )
        )

    ranked = nyaa_module.rank(
        [found for found, _ in accepted], rules, wanted_numbers=wanted_numbers
    )
    good: list[Choice] = []
    for entry in ranked:
        candidate = entry.candidate
        kind = "batch" if candidate.is_batch else "single"
        if kind == "batch":
            covers: tuple[int, ...] | None = (
                _span_covers(candidate.covers, candidate.offset, wanted_numbers)
                if candidate.covers
                else None
            )
        else:
            episode = candidate.parsed.episode
            covers = (episode - candidate.offset,) if episode is not None else (number,)
        acceptable = True
        reason: str | None = None
        current = False
        known = tried.get(entry.item.info_hash)
        if known is not None:
            verdict = tried_reason(known, target_ids)
            if verdict is not None:
                acceptable, reason = verdict
                current = reason == DOWNLOADING_NOW
        if acceptable and kind == "batch" and thin_floor is not None:
            if entry.item.seeders < thin_floor:
                acceptable = False
                reason = (
                    f"a thinly seeded pack ({entry.item.seeders} seeders; "
                    f"trips take {thin_floor} or more)"
                )
        good.append(
            Choice(
                id=candidate_id(entry.item.info_hash),
                item=entry.item,
                parsed=candidate.parsed,
                kind=kind,
                acceptable=acceptable,
                reason=reason,
                covers=covers,
                offset=candidate.offset,
                span=candidate.covers,
                similarity=candidate.title_similarity,
                current=current,
            )
        )
    # Tried releases that the filter also refused still say why they were tried.
    refused = [_with_tried(choice, tried.get(choice.info_hash), target_ids) for choice in refused]

    def bucket(choice: Choice) -> int:
        if packs_first:
            return 0 if choice.kind == "batch" else 1
        return 0 if choice.kind == "single" else 1

    taken_ok = [choice for choice in good if choice.acceptable]
    taken_ok.sort(key=bucket)  # stable: FR-A3's order inside each bucket
    rest = [choice for choice in good if not choice.acceptable] + refused
    rest.sort(key=lambda choice: (bucket(choice), -choice.item.seeders))
    return taken_ok + rest


def _with_tried(choice: Choice, known: Tried | None, target_ids: Collection[int]) -> Choice:
    """A refused choice, with the sentence for a release Arc already tried."""
    verdict = None if known is None else tried_reason(known, target_ids)
    if verdict is None or verdict[0]:
        return choice
    return replace(choice, reason=verdict[1], current=verdict[1] == DOWNLOADING_NOW)


# --- Scopes ---------------------------------------------------------------------

#: Episode states a manual choice may act on: nothing has landed yet.
REPLACEABLE: Final[frozenset[EpisodeState]] = frozenset(
    {
        EpisodeState.NOT_WANTED,
        EpisodeState.WANTED,
        EpisodeState.SEARCHING,
        EpisodeState.DOWNLOADING,
        EpisodeState.UNAVAILABLE,
    }
)

ScopeKind = Literal["episode", "trip"]


@dataclass(slots=True)
class Scope:
    """What a list or a choice is about: one episode, or one trip's pending episodes."""

    kind: ScopeKind
    id: int
    user: User
    anime: Anime
    #: The episodes a choice may replace the download of.
    targets: list[Episode]
    #: The caller's wanted episode numbers of the show: what a pack may select.
    wanted_numbers: tuple[int, ...]
    #: The number the search asks for.
    number: int
    #: ``trip_pack_min_seeders`` when the scope is a trip's (or a trip-only
    #: episode's); a pack below it is listed as thinly seeded.
    thin_floor: int | None = None

    @property
    def target_ids(self) -> set[int]:
        return {episode.id for episode in self.targets}


async def _caller_wanted(session: AsyncSession, user_id: int, anime_id: int) -> list[Episode]:
    """The caller's live wants on this show (window, sample and trip), as episodes."""
    rows = await session.scalars(
        select(Episode)
        .join(Want, Want.episode_id == Episode.id)
        .where(
            Want.user_id == user_id,
            Want.dropped_at.is_(None),
            Episode.anime_id == anime_id,
        )
        .order_by(Episode.number)
    )
    return list(rows.all())


async def episode_scope(session: AsyncSession, user: User, episode_id: int) -> Scope:
    """One episode the caller wants; demo 403, not wanted 404."""
    if user.is_demo:
        raise DEMO
    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise NOT_WANTED_EPISODE
    wanted = await _caller_wanted(session, user.id, episode.anime_id)
    if episode.id not in {row.id for row in wanted}:
        raise NOT_WANTED_EPISODE
    anime = await session.get(Anime, episode.anime_id)
    if anime is None:  # pragma: no cover - the foreign key forbids it
        raise NOT_WANTED_EPISODE
    trip_only = episode.id in await trip_only_episode_ids(session, [episode.id])
    return Scope(
        kind="episode",
        id=episode.id,
        user=user,
        anime=anime,
        targets=[episode],
        wanted_numbers=tuple(row.number for row in wanted),
        number=episode.number,
        thin_floor=await trip_pack_min_seeders(session) if trip_only else None,
    )


async def trip_scope(session: AsyncSession, user: User, trip_id: int) -> Scope:
    """The caller's active trip: its pending episodes that have not landed."""
    if user.is_demo:
        raise DEMO
    trip = await session.get(Trip, trip_id)
    if trip is None or trip.user_id != user.id:
        raise TRIP_NOT_FOUND
    if trip.state is not TripState.ACTIVE:
        raise TRIP_NOT_ACTIVE
    anime = await session.get(Anime, trip.anime_id)
    if anime is None:  # pragma: no cover - the foreign key forbids it
        raise TRIP_NOT_FOUND
    pending = list(
        (
            await session.scalars(
                select(Episode)
                .join(TripEpisode, TripEpisode.episode_id == Episode.id)
                .where(
                    TripEpisode.trip_id == trip.id,
                    TripEpisode.state == TripEpisodeState.PENDING,
                )
                .order_by(Episode.number)
            )
        ).all()
    )
    targets = [episode for episode in pending if episode.state in REPLACEABLE]
    if not targets:
        raise NOTHING_TO_FETCH
    return Scope(
        kind="trip",
        id=trip.id,
        user=user,
        anime=anime,
        targets=targets,
        wanted_numbers=tuple(episode.number for episode in targets),
        number=targets[0].number,
        thin_floor=await trip_pack_min_seeders(session),
    )


# --- What is downloading now ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Current:
    """The download an episode (or most of a trip) is running now."""

    title: str | None
    kind: Kind
    state: str | None
    progress: float | None
    manual: bool
    info_hash: str


async def _current_for(session: AsyncSession, episode: Episode) -> Current | None:
    if episode.state not in (EpisodeState.DOWNLOADING, EpisodeState.DOWNLOADED):
        return None
    claim = await live_claim(session, episode.id)
    if claim is not None:
        pack = await session.get(Torrent, claim.torrent_id)
        if pack is None:  # pragma: no cover - the foreign key forbids it
            return None
        return Current(
            title=pack.title,
            kind="batch",
            state=pack.qbit_state,
            progress=claim.progress,
            manual=pack.manual,
            info_hash=pack.info_hash,
        )
    single = await session.scalar(
        select(Torrent)
        .where(
            Torrent.episode_id == episode.id,
            or_(Torrent.qbit_state.is_(None), Torrent.qbit_state.not_in(sorted(DECIDED_STATES))),
        )
        .order_by(Torrent.id.desc())
        .limit(1)
    )
    if single is None:
        return None
    return Current(
        title=single.title,
        kind="single",
        state=single.qbit_state,
        progress=single.progress,
        manual=single.manual,
        info_hash=single.info_hash,
    )


async def current_download(session: AsyncSession, scope: Scope) -> Current | None:
    """The episode's download, or the one serving most of a trip's episodes."""
    found = [
        current
        for episode in scope.targets
        if (current := await _current_for(session, episode)) is not None
    ]
    if not found:
        return None
    counts: dict[str, int] = {}
    for current in found:
        counts[current.info_hash] = counts.get(current.info_hash, 0) + 1
    best = max(counts, key=lambda key: counts[key])
    return next(current for current in found if current.info_hash == best)


# --- Searching, and the per-user cache ------------------------------------------

#: How often one person may make Nyaa search one episode (or trip) for them.
SEARCH_EVERY: Final[float] = 60.0

#: How long a list is kept for a choice to be made from it.
KEEP_FOR: Final[float] = 3600.0

_now = time.monotonic


@dataclass(slots=True)
class Searched:
    """One person's last list for one scope."""

    at: float
    choices: list[Choice]
    pool: dict[str, NyaaItem] = field(default_factory=dict)
    requests: int = 0


#: ``(user id, scope kind, scope id) → their last list``. Process-local, like
#: the login rate limiter (the API runs one worker); lossy by design — a
#: forgotten list costs one more search.
_SEARCHES: dict[tuple[int, str, int], Searched] = {}


def _key(scope: Scope) -> tuple[int, str, int]:
    return (scope.user.id, scope.kind, scope.id)


def _remember(scope: Scope, searched: Searched) -> None:
    moment = _now()
    for key in [key for key, value in _SEARCHES.items() if moment - value.at > KEEP_FOR]:
        del _SEARCHES[key]
    _SEARCHES[_key(scope)] = searched


def forget_searches() -> None:
    """Clear every remembered list and search limit (tests)."""
    _SEARCHES.clear()
    _IN_FLIGHT.clear()
    _USER_RUNNING.clear()
    _USER_RECENT.clear()
    _RUNNING[0] = 0


async def gather_pool(
    client: NyaaClient,
    anime: Anime,
    number: int,
    rules: Rules,
    *,
    offset: int | None,
) -> tuple[dict[str, NyaaItem], int]:
    """Every result the automatic search's forms return, merged by info hash.

    The forms and the ceiling are ``search_for_episode``'s: the title forms,
    the group-narrowed forms for a finished show (stopping
    :data:`~arc.services.acquisition.nyaa.MAX_BATCH_QUERIES` short of the
    ceiling), then the batch forms — all inside
    :data:`~arc.services.acquisition.nyaa.MAX_REQUESTS`, paced and cached by the
    shared client. Unlike the automatic search, the batch forms are asked
    whether or not a single was found: a person choosing wants to see both.
    """
    merged: dict[str, NyaaItem] = {}
    requests = 0
    single = nyaa_module.is_single(anime)

    async def ask(query: str) -> None:
        nonlocal requests
        for item in await client.search(query):
            merged.setdefault(item.info_hash, item)
        requests += 1

    reserve = 0 if single else nyaa_module.MAX_BATCH_QUERIES
    for query in nyaa_module.queries(anime, number, offset=offset):
        if requests >= MAX_REQUESTS - reserve:
            break
        await ask(query)
    if nyaa_module.deep_search(anime):
        titles = nyaa_module.anime_titles(anime)
        for query in nyaa_module.group_queries(anime, number, rules):
            if requests >= MAX_REQUESTS - reserve:
                break
            kept = nyaa_module.filter_items(
                merged.values(),
                titles=titles,
                number=number,
                season=nyaa_module.anime_season(anime),
                single=single,
                year=anime.season_year,
                offset=offset,
            )
            if len(kept) >= nyaa_module.ENOUGH_CANDIDATES:
                break
            await ask(query)
    if not single:
        for query in nyaa_module.batch_queries(anime):
            if requests >= MAX_REQUESTS:
                break
            await ask(query)
    return merged, requests


async def _tried(session: AsyncSession, hashes: Collection[str]) -> dict[str, Tried]:
    if not hashes:
        return {}
    rows = await session.execute(
        select(Torrent.info_hash, Torrent.qbit_state, Torrent.kind, Torrent.episode_id).where(
            Torrent.info_hash.in_(sorted(hashes))
        )
    )
    return {
        info_hash: Tried(state=state, kind=kind, episode_id=episode_id)
        for info_hash, state, kind, episode_id in rows.all()
    }


@dataclass(frozen=True, slots=True)
class Releases:
    """What ``GET …/releases`` answers."""

    scope: Scope
    choices: list[Choice]
    current: Current | None
    cached: bool
    searched_seconds_ago: int


#: Live searches in flight per scope key, so a second request for the same
#: list waits for the first rather than asking Nyaa again.
_IN_FLIGHT: dict[tuple[int, str, int], asyncio.Future[Searched]] = {}
#: Users with a live search running (one each), when each user's recent live
#: searches started, and how many run process-wide.
_USER_RUNNING: set[int] = set()
_USER_RECENT: dict[int, list[float]] = {}
_RUNNING: list[int] = [0]

#: One live search per user at a time, three a minute across their scopes, and
#: two process-wide (FR-A13): the request holds a person waiting, and Nyaa is a
#: volunteer site whose pacing is shared with the worker's searches.
USER_SEARCHES_PER_MINUTE: Final[int] = 3
MAX_RUNNING_SEARCHES: Final[int] = 2


def _admit(user_id: int) -> None:
    """Take a live-search slot for ``user_id``, or refuse with 429."""
    if user_id in _USER_RUNNING:
        raise SEARCH_RUNNING
    moment = _now()
    recent = [at for at in _USER_RECENT.get(user_id, []) if moment - at < SEARCH_EVERY]
    if len(recent) >= USER_SEARCHES_PER_MINUTE:
        _USER_RECENT[user_id] = recent
        raise SEARCHES_PER_MINUTE
    if _RUNNING[0] >= MAX_RUNNING_SEARCHES:
        raise SEARCHES_BUSY
    _USER_RECENT[user_id] = [*recent, moment]
    _USER_RUNNING.add(user_id)
    _RUNNING[0] += 1


def _release_slot(user_id: int) -> None:
    _USER_RUNNING.discard(user_id)
    _RUNNING[0] = max(0, _RUNNING[0] - 1)


async def _search(session: AsyncSession, settings: Settings, scope: Scope) -> tuple[Searched, bool]:
    """The scope's list: remembered if under a minute old, else a live search.

    The live search holds **no transaction**: what it needs from the database
    is read first, the session's transaction is ended, and only then is Nyaa
    asked — paced requests can take a minute, and nothing may be locked for
    it. A second request for a scope already being searched waits for that
    search; otherwise :func:`_admit` applies the per-user and process limits.
    """
    key = _key(scope)
    known = _SEARCHES.get(key)
    if known is not None and _now() - known.at < SEARCH_EVERY:
        return known, True
    running = _IN_FLIGHT.get(key)
    if running is not None:
        return await asyncio.shield(running), True
    _admit(scope.user.id)
    future: asyncio.Future[Searched] = asyncio.get_running_loop().create_future()
    _IN_FLIGHT[key] = future
    try:
        searched = await _search_live(session, settings, scope)
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
            future.exception()  # retrieved: no "never retrieved" warning
        raise
    else:
        future.set_result(searched)
        return searched, False
    finally:
        _IN_FLIGHT.pop(key, None)
        _release_slot(scope.user.id)


async def _search_live(session: AsyncSession, settings: Settings, scope: Scope) -> Searched:
    rules = await load_rules(session, scope.anime.id)
    offset = await jobs._prequel_offset(session, scope.anime)
    # Reads only so far: end the transaction before the paced Nyaa requests.
    await session.commit()
    client = nyaa_module.shared_client(settings.nyaa_url)
    try:
        pool, requests = await gather_pool(client, scope.anime, scope.number, rules, offset=offset)
    except NyaaUnavailable as exc:
        raise ManualUpstream("nyaa_unavailable", "Nyaa could not be reached. Try again.") from exc
    choices = classify(
        pool.values(),
        anime=scope.anime,
        number=scope.number,
        rules=rules,
        offset=offset,
        wanted_numbers=scope.wanted_numbers,
        tried=await _tried(session, pool),
        target_ids=scope.target_ids,
        thin_floor=scope.thin_floor,
        packs_first=scope.kind == "trip",
    )
    searched = Searched(at=_now(), choices=choices, pool=pool, requests=requests)
    _remember(scope, searched)
    log.info(
        "manual release list searched",
        extra={
            "user_id": scope.user.id,
            "scope": scope.kind,
            "scope_id": scope.id,
            "anime_id": scope.anime.id,
            "number": scope.number,
            "requests": requests,
            "results": len(pool),
            "acceptable": sum(1 for choice in choices if choice.acceptable),
        },
    )
    return searched


async def list_releases(session: AsyncSession, settings: Settings, scope: Scope) -> Releases:
    """The ranked candidates for an episode or a trip, and what runs now (FR-A13).

    The release an episode is downloading now is marked ``current`` — read at
    every request, not from the remembered list, since it is the one thing a
    choice changes. For a trip the pack serving it stays choosable: choosing it
    again attaches the pending episodes it holds and is not yet serving.
    """
    searched, cached = await _search(session, settings, scope)
    current = await current_download(session, scope)
    choices = [choice for choice in searched.choices if choice.listed]
    if current is not None and scope.kind == "episode":
        choices = [
            replace(choice, current=True, acceptable=False, reason=DOWNLOADING_NOW)
            if choice.info_hash == current.info_hash
            else choice
            for choice in choices
        ]
    return Releases(
        scope=scope,
        choices=choices,
        current=current,
        cached=cached,
        searched_seconds_ago=int(_now() - searched.at),
    )


# --- Choosing -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chosen:
    """What a choice did: the release taken and the episodes it now serves."""

    title: str
    kind: Kind
    info_hash: str
    episodes: list[Episode]


async def _resolve(
    session: AsyncSession,
    settings: Settings,
    scope: Scope,
    *,
    candidate: str | None,
    link: str | None,
) -> Choice:
    """The release a request names: a listed candidate, or a pasted link.

    A result the list classified as another show (``listed`` false) is never
    taken, however it is named — by id, by magnet or by its Nyaa link.
    """
    choice = await _find(session, settings, scope, candidate=candidate, link=link)
    if not choice.listed:
        raise WRONG_SHOW
    return choice


async def _find(
    session: AsyncSession,
    settings: Settings,
    scope: Scope,
    *,
    candidate: str | None,
    link: str | None,
) -> Choice:
    if candidate:
        known = _SEARCHES.get(_key(scope))
        if known is not None:
            for choice in known.choices:
                if choice.id == candidate:
                    return choice
        raise UNKNOWN_CANDIDATE
    if not link:
        raise NO_CHOICE
    parsed = parse_link(link, nyaa_url=settings.nyaa_url)
    searched, _ = await _search(session, settings, scope)
    if parsed.info_hash is not None:
        for choice in searched.choices:
            if choice.info_hash == parsed.info_hash:
                return choice
        raise MAGNET_UNKNOWN
    assert parsed.nyaa_id is not None
    for choice in searched.choices:
        if nyaa_id_of(choice.item.link) == parsed.nyaa_id:
            return choice
    # Not in the pool: fetch the .torrent (on the configured host) for its hash
    # and name, and take it file by file, whatever its name says.
    url = torrent_url(settings.nyaa_url, parsed.nyaa_id)
    await session.commit()  # reads only so far; no transaction across the fetch
    try:
        blob = await nyaa_module.shared_client(settings.nyaa_url).torrent_file(url)
    except NyaaUnavailable as exc:
        raise ManualInvalid(
            "torrent_unavailable", "Arc could not fetch that torrent from Nyaa."
        ) from exc
    try:
        info_hash, name = torrent_identity(blob)
    except ValueError as exc:
        raise ManualInvalid("bad_torrent", "That link is not a torrent Arc can read.") from exc
    item = NyaaItem(title=name or f"nyaa {parsed.nyaa_id}", link=url, info_hash=info_hash)
    parsed_name = parse(item.title)
    # ``plan_files`` is generous about a file's title once the *release* name
    # has cleared the floor; a pasted torrent never went through that filter,
    # so its own name must (another show's "- 05 [1080p].mkv" is not ours).
    titles = nyaa_module.anime_titles(scope.anime)
    similarity = nyaa_module.title_score(parsed_name.title_key, titles)
    return Choice(
        id=candidate_id(info_hash),
        item=item,
        parsed=parsed_name,
        kind="batch",
        acceptable=True,
        reason=None,
        covers=None,
        similarity=similarity,
        listed=similarity >= nyaa_module.TITLE_THRESHOLD,
        from_file=True,
        blob=blob,
    )


async def _lock(session: AsyncSession, episodes: Sequence[Episode]) -> list[Episode]:
    """The episodes no other transaction holds, locked and re-read."""
    if not episodes:
        return []
    rows = (
        await session.scalars(
            select(Episode)
            .where(Episode.id.in_([episode.id for episode in episodes]))
            .order_by(Episode.number)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [row for row in rows if row.state in REPLACEABLE]


REPLACED: Final[str] = "a user chose another release"


async def _release(session: AsyncSession, episodes: Iterable[Episode]) -> None:
    """Give up what these episodes are downloading and leave them ``wanted``.

    A pack's claim goes back through :func:`~arc.services.acquisition.claims.
    release_files` (``qbit_reselect`` writes the selection and decides the
    pack's fate); a single is marked ``cancelled`` and ``qbit_cancel`` removes
    it with its partial files — the reconciler's own two paths.
    """
    cancelled: list[int] = []
    for episode in episodes:
        if episode.state is EpisodeState.DOWNLOADING:
            claim = await live_claim(session, episode.id)
            if claim is not None:
                await release_files(session, (claim,))
            else:
                singles = (
                    await session.scalars(select(Torrent).where(Torrent.episode_id == episode.id))
                ).all()
                for torrent in singles:
                    if torrent.qbit_state not in DECIDED_STATES:
                        torrent.qbit_state = QBIT_CANCELLED
                        cancelled.append(episode.id)
        if episode.state in (
            EpisodeState.DOWNLOADING,
            EpisodeState.SEARCHING,
        ):
            transition(episode, EpisodeState.NOT_WANTED, reason=REPLACED)
        transition(episode, EpisodeState.WANTED, reason=REPLACED)
    await session.flush()
    for episode_id in sorted(set(cancelled)):
        await enqueue_cancel(session, episode_id)


def _context(session: AsyncSession, settings: Settings) -> JobContext:
    """A context for the add paths, which are written for a job's.

    The job row is transient and never added: the request's own session is the
    one that commits, and the paths read nothing off the job but its id for a
    log line.
    """
    return JobContext(
        job=Job(id=0, type="manual_release", payload={}),
        session=session,
        settings=settings,
        log=log,
    )


def _held(
    anime: Anime,
    ranked: Ranked,
    listed: Sequence[FileInfo],
    numbers: Iterable[int],
    offset: int | None,
) -> list[int]:
    """Which of ``numbers`` this pack has exactly one file for. Pure.

    The same :func:`~arc.services.acquisition.batch.plan_files` the take runs,
    asked once per number, so "holds it" means what the take will require.
    """
    return [
        number
        for number in sorted(set(numbers))
        if batch.plan_files(
            anime, [number], listed, required=number, offset=batch.plan_offset(ranked, offset)
        ).refused_reason
        is None
    ]


def _qbit_down() -> ManualUnavailable:
    return ManualUnavailable("qbit_unavailable", "The torrent client could not be reached.")


async def _take_single(
    session: AsyncSession, settings: Settings, scope: Scope, choice: Choice, added: list[str]
) -> list[Episode]:
    number = choice.covers[0] if choice.covers else scope.number
    target = next((episode for episode in scope.targets if episode.number == number), None)
    if target is None:
        raise ManualInvalid(
            "wrong_episode",
            f"That release is episode {number}, which "
            + ("this episode is not." if scope.kind == "episode" else "this trip does not need."),
        )
    locked = await _lock(session, [target])
    if not locked:
        raise BUSY
    target = locked[0]
    await _release(session, [target])
    transition(target, EpisodeState.SEARCHING, reason="a release chosen by hand")
    item = choice.item
    torrent = Torrent(
        episode_id=target.id,
        info_hash=item.info_hash,
        magnet=item.magnet,
        title=item.title,
        group=choice.parsed.group,
        resolution=choice.parsed.resolution,
        seeders=item.seeders,
        trusted=item.trusted,
        manual=True,
    )
    session.add(torrent)
    try:
        await session.flush()
    except IntegrityError as exc:  # somebody chose the same release a moment ago
        raise BUSY from exc
    added.append(item.info_hash)
    try:
        async with QbitClient.from_settings(settings) as qbit:
            await qbit.add(item.magnet, episode_id=target.id, info_hash=item.info_hash)
            if not await needs_rendition(session, target.id):
                try:
                    await qbit.bottom_prio(item.info_hash)
                except QbitError:
                    log.warning(
                        "could not move a trip torrent down", extra={"hash": item.info_hash}
                    )
    except QbitUnavailable as exc:
        raise _qbit_down() from exc
    except QbitError as exc:
        raise ManualInvalid(
            "client_refused", f"The torrent client would not take that release: {exc}"
        ) from exc
    torrent.qbit_state = "added"
    torrent.progress = 0.0
    transition(target, EpisodeState.DOWNLOADING, reason=f"downloading {item.title}")
    await session.flush()
    return [target]


async def _attach(
    session: AsyncSession, scope: Scope, choice: Choice, pack: Torrent
) -> list[Episode]:
    """Turn on this pack's files for the scope's episodes (a pack Arc already has)."""
    rows = list(
        (
            await session.scalars(
                select(TorrentFile).where(
                    TorrentFile.torrent_id == pack.id,
                    TorrentFile.episode_id.in_(scope.target_ids),
                )
            )
        ).all()
    )
    by_episode = {row.episode_id: row for row in rows if row.episode_id is not None}
    if scope.kind == "episode" and scope.targets[0].id not in by_episode:
        raise ManualInvalid(
            "pack_selects_nothing", f"That pack has no file for episode {scope.number}."
        )
    already = {row.episode_id for row in rows if row.wanted}
    if scope.kind == "episode" and scope.targets[0].id in already:
        raise ManualInvalid("already_downloading", "This episode is already downloading that pack.")
    wanted = [episode for episode in scope.targets if episode.id in by_episode]
    wanted = [episode for episode in wanted if episode.id not in already]
    if not wanted:
        raise ManualInvalid(
            "pack_selects_nothing", "That pack has no file for any episode this trip needs."
        )
    locked = await _lock(session, wanted)
    if not locked:
        raise BUSY
    await _release(session, locked)
    for episode in locked:
        row = by_episode[episode.id]
        row.wanted = True
        row.completed_at = None
        row.progress = None
        batch.start_downloading(episode, f"downloading {pack.title or choice.item.title}")
    pack.manual = True
    await session.flush()
    await enqueue_reselect(session, pack.id)
    return locked


async def _free_riders(
    session: AsyncSession, scope: Scope, exclude: Collection[int]
) -> dict[int, Episode]:
    """The caller's other wanted episodes of the show a pack may also select.

    The automatic search's free-rider rule (``jobs._attachable``) applied to
    the caller's own wants only: ``wanted``, and holding no live claim.
    """
    if scope.kind != "episode":
        return {}
    others = [
        episode
        for episode in await _caller_wanted(session, scope.user.id, scope.anime.id)
        if episode.id not in exclude
    ]
    return {
        number: episode for number, episode in (await jobs._attachable(session, others)).items()
    }


async def _take_pack(
    session: AsyncSession, settings: Settings, scope: Scope, choice: Choice, added: list[str]
) -> list[Episode]:
    """Fetch, then lock; add stopped, read; release what it holds; select, verify, start.

    The ``.torrent`` is fetched **before** any lock is taken and with no
    transaction open (a paced Nyaa request), once.
    """
    ctx = _context(session, settings)
    ranked = choice.ranked()
    offset = await jobs._prequel_offset(session, scope.anime)
    blob = choice.blob
    if blob is None:
        await session.commit()  # reads only so far
        try:
            blob = await nyaa_module.shared_client(settings.nyaa_url).torrent_file(
                ranked.torrent_url
            )
        except NyaaUnavailable as exc:
            raise ManualInvalid(
                "torrent_unavailable", "Arc could not fetch that pack's .torrent from Nyaa."
            ) from exc
    locked = await _lock(session, scope.targets)
    if not locked or (scope.kind == "episode" and locked[0].id != scope.targets[0].id):
        raise BUSY
    first = locked[0]
    try:
        async with QbitClient.from_settings(settings) as qbit:
            added.append(choice.info_hash)
            taken = await jobs._add_stopped(
                ctx, qbit, first, scope.anime, ranked, None, False, blob=blob
            )
            if isinstance(taken, int):
                owner = await session.scalar(
                    select(Torrent.id).where(Torrent.info_hash == choice.info_hash)
                )
                if owner is not None:  # recorded by somebody else meanwhile
                    raise BUSY
                raise ManualInvalid(
                    "pack_unreadable",
                    "Arc could not fetch that pack or read its files, so nothing was added.",
                )
            torrent, listed, _save_path = taken
            held = _held(
                scope.anime, ranked, listed, [episode.number for episode in locked], offset
            )
            if not held or (scope.kind == "episode" and first.number not in held):
                reason = (
                    f"no file in that pack is episode {first.number}"
                    if scope.kind == "episode"
                    else "no file in that pack is an episode this trip needs"
                )
                await jobs._abandon(ctx, qbit, torrent, reason, permanent=False)
                raise ManualInvalid("pack_selects_nothing", f"{reason[0].upper()}{reason[1:]}.")
    except QbitUnavailable as exc:
        raise _qbit_down() from exc
    except batch.BatchTaken as exc:
        raise BUSY from exc

    serving = [episode for episode in locked if episode.number in held]
    await _release(session, serving)
    required = serving[0]
    transition(required, EpisodeState.SEARCHING, reason="a release chosen by hand")
    await session.flush()
    attachable: dict[int, Episode] = {episode.number: episode for episode in serving}
    for number, episode in (
        await _free_riders(session, scope, {episode.id for episode in serving})
    ).items():
        attachable.setdefault(number, episode)
    trip = jobs.TripPass(
        cover=(),
        need=0,
        held={choice.info_hash: jobs._Held(torrent=torrent, listed=listed)},
    )
    try:
        attempt = await jobs._take_batch(
            ctx,
            required,
            scope.anime,
            [ranked],
            attachable=attachable,
            offset=offset,
            trip=trip,
        )
    except QbitUnavailable as exc:
        raise _qbit_down() from exc
    if not attempt.started or attempt.torrent is None:
        reason = attempt.reason or "the pack could not be selected"
        raise ManualInvalid("pack_selects_nothing", f"{reason[0].upper()}{reason[1:]}.")
    attempt.torrent.manual = True
    await session.flush()
    return [episode for episode in attachable.values() if episode.state is EpisodeState.DOWNLOADING]


async def choose_release(
    session: AsyncSession,
    settings: Settings,
    scope: Scope,
    *,
    candidate: str | None = None,
    link: str | None = None,
    added: list[str] | None = None,
) -> Chosen:
    """Replace what the scope is downloading with the chosen release (FR-A13).

    Refusals raise :class:`ManualError`; on any refusal after a write the
    caller rolls the session back, which undoes the release of the old
    download as well (nothing reaches qBittorrent for it until the queued jobs
    run after the commit). The caller commits on success. ``added`` collects
    every hash this call handed the torrent client, so a caller whose commit
    fails can take them back out (:func:`discard_added`).
    """
    added = [] if added is None else added
    if await is_paused(session):
        raise PAUSED
    if await is_storage_held(session, settings):
        raise STORAGE_HELD
    if scope.kind == "episode" and scope.targets[0].state not in REPLACEABLE:
        raise ALREADY_ARRIVED
    choice = await _resolve(session, settings, scope, candidate=candidate, link=link)

    existing = await session.scalar(select(Torrent).where(Torrent.info_hash == choice.info_hash))
    if existing is not None:
        verdict = tried_reason(
            Tried(state=existing.qbit_state, kind=existing.kind, episode_id=existing.episode_id),
            scope.target_ids,
        )
        if verdict is not None:
            _, reason = verdict
            code = "already_downloading" if reason == DOWNLOADING_NOW else "already_tried"
            raise ManualInvalid(code, f"{reason[0].upper()}{reason[1:]}.")
    if choice.kind == "batch" or (existing is not None and existing.kind is TorrentKind.BATCH):
        if not await batch_fallback(session):
            raise PACKS_OFF

    if existing is not None and existing.kind is TorrentKind.BATCH:
        episodes = await _attach(session, scope, choice, existing)
        kind: Kind = "batch"
    elif choice.kind == "single" and not choice.from_file:
        episodes = await _take_single(session, settings, scope, choice, added)
        kind = "single"
    else:
        episodes = await _take_pack(session, settings, scope, choice, added)
        kind = "batch"

    log.info(
        "release chosen by hand",
        extra={
            "user_id": scope.user.id,
            "scope": scope.kind,
            "episode_id": scope.id if scope.kind == "episode" else None,
            "trip_id": scope.id if scope.kind == "trip" else None,
            "anime_id": scope.anime.id,
            "info_hash": choice.info_hash,
            "title": choice.item.title,
            "kind": kind,
            "episodes": sorted(episode.number for episode in episodes),
            "pasted": link is not None and not candidate,
        },
    )
    # The list is about the world before this choice; the next open searches.
    _SEARCHES.pop(_key(scope), None)
    return Chosen(title=choice.item.title, kind=kind, info_hash=choice.info_hash, episodes=episodes)


async def discard_added(settings: Settings, hashes: Sequence[str]) -> None:
    """Best effort: remove torrents a failed choice handed the client, with files.

    Only hashes added by this request (never a pack Arc already held, which is
    attached to rather than added). A client that cannot be reached is logged;
    the torrent is then an orphan under Arc's category, stopped or nearly
    empty, and visible in the client.
    """
    if not hashes:
        return
    try:
        async with QbitClient.from_settings(settings) as qbit:
            await qbit.delete(list(hashes), delete_files=True)
    except Exception:  # noqa: BLE001 - cleanup must never mask the real error
        log.warning("could not remove a refused manual release", extra={"hashes": list(hashes)})
    else:
        log.info("refused manual release removed from the client", extra={"hashes": list(hashes)})


__all__ = [
    "KEEP_FOR",
    "NYAA_HOST",
    "REPLACEABLE",
    "SEARCH_EVERY",
    "Choice",
    "Chosen",
    "Current",
    "Link",
    "ManualConflict",
    "ManualError",
    "ManualForbidden",
    "ManualInvalid",
    "ManualNotFound",
    "ManualTooMany",
    "ManualUnavailable",
    "ManualUpstream",
    "Releases",
    "Scope",
    "Tried",
    "allowed_hosts",
    "candidate_id",
    "choose_release",
    "classify",
    "current_download",
    "discard_added",
    "episode_scope",
    "forget_searches",
    "gather_pool",
    "list_releases",
    "parse_link",
    "torrent_identity",
    "torrent_url",
    "trip_scope",
]
