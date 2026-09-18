"""The qBittorrent Web API, as much of it as Arc needs (FR-A5).

A handful of calls — ``auth/login``, ``torrents/add``, ``torrents/info``,
``torrents/files``, ``torrents/filePrio``, ``torrents/delete``,
``torrents/start``, ``torrents/stop`` and ``app/setPreferences``
(architecture.md §6) — and one piece of arithmetic that is easy to get wrong
and expensive to get wrong: **the path mapping**.

qBittorrent runs in its own container and writes to ``/data/downloads``. The
worker may run on the host and see the same bytes at ``./data/downloads``, or
in a container of its own and see them at ``/data``. So every path the client
reports is a *container* path and has to be translated before anything opens
it. :func:`host_path` is the only place that translation happens, and it
refuses a path that is not under the configured root rather than guessing —
a save path Arc did not choose is a torrent Arc did not add.

Two smaller decisions worth stating.

**Every torrent is filed under one category** (``QBIT_CATEGORY``, default
``arc``) and ``torrents/info`` is always asked for that category. A user's own
downloads in the same client are then invisible to Arc, which matters most for
``torrents/delete``: retention (M10) deletes by hash, and a bug there must not
be able to reach anything Arc did not add — so :meth:`QbitClient.delete` looks
the hashes up in the category listing and sends only the ones that are in it.

**A batch is added as the ``.torrent`` file itself, stopped** (FR-A4's
exception, FR-A11, owner 2026-09-18, the plan's D2). Every single Arc has ever
fetched went in as a magnet, and a magnet cannot work here:
``torrents/filePrio`` is **refused while the client has no metadata**, so the
magnet route has an unavoidable window between the add and the arrival of the
file list in which unwanted bytes are already being fetched. Posting the
``.torrent`` as multipart means the metadata exists at add time, and adding it
``stopped`` means nothing moves until every file has been set to priority 0,
the identified ones back to 1, and the selection read back and verified. So
there is no window at all, which is the whole of the byte guarantee — not one
byte of an unwanted file can ever be fetched. :meth:`QbitClient.add_file`,
:meth:`QbitClient.files`, :meth:`QbitClient.file_priority` and
:meth:`QbitClient.start` are that sequence's four calls, and
:func:`batch_save_path_for` is where its bytes land.

**A 403 means the cookie expired, not that the credentials are wrong.**
qBittorrent's session lasts an hour by default and the worker is long-lived,
so the first call after an idle night gets a 403. :meth:`QbitClient.request`
logs in again and retries once; a second 403 is a real authentication failure
and raises.

**Arc owns the client's seeding and queue policy**
(:meth:`QbitClient.apply_policy`, spec §9). Arc does not seed: the share-ratio
limit is 0 and its action is "stop", the seeding-time limit is 0, and the
upload rate is capped. That is a legal mitigation rather than a tuning knob,
and it is applied to the *client* rather than to each torrent so that it also
covers whatever was added before Arc got there. The queue limits ride along for
a duller reason: qBittorrent's defaults are three concurrent downloads and five
active torrents, a fresh container comes back with them, and a limit Arc did
not write is a limit Arc loses on every restart.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Final, Self

import httpx

from arc.config import Settings

log = logging.getLogger(__name__)

#: The API prefix, unchanged since qBittorrent 4.1.
API: Final[str] = "/api/v2"

TIMEOUT_SECONDS: Final[float] = 20.0

#: qBittorrent states that mean "the data is on disk". ``pausedUP`` is 4.x's
#: name for what 5.x calls ``stoppedUP``; both are listed because the client
#: is whatever the operator pulled.
COMPLETE_STATES: Final[frozenset[str]] = frozenset(
    {
        "uploading",
        "stalledUP",
        "queuedUP",
        "forcedUP",
        "pausedUP",
        "stoppedUP",
        "checkingUP",
        "completed",
    }
)

#: The subset of :data:`COMPLETE_STATES` that means "finished, and still
#: giving something back". These are the states ``poll_qbit`` stops a torrent
#: out of when seeding is off; the paused/stopped ones are where it puts it.
SEEDING_STATES: Final[frozenset[str]] = frozenset(
    {
        "uploading",
        "stalledUP",
        "queuedUP",
        "forcedUP",
    }
)

#: ``max_ratio_act``: what qBittorrent does when a share limit is reached. 0 is
#: *stop the torrent* — 1 removes it, 2 turns on super seeding and 3 removes it
#: with its files (qBittorrent 5.2's ``ShareLimitAction``). Stop, deliberately:
#: what happens to the *data* is retention's decision (FR-T1), and a client
#: that deletes a source the moment it finishes would take the file out from
#: under the transcode that is about to read it.
STOP_AT_SHARE_LIMIT: Final[int] = 0

#: Progress at which a torrent is finished whatever it calls its state. Floats
#: from a JSON API are not compared for equality with 1.0 — 0.9999999 is a
#: finished download, and waiting forever for the last bit would be silly.
COMPLETE_PROGRESS: Final[float] = 0.999

# --- Per-file selection, for a batch (FR-A11) --------------------------------

#: ``torrents/filePrio``'s "do not download this file". The value every index
#: of a freshly added batch is set to first, before any is turned back on.
FILE_OFF: Final[int] = 0

#: And "download it at normal priority". Deliberately not 6 (High) or 7
#: (Maximum): the point of the exception is a small, polite download, and a
#: batch jumping the client's own queue ahead of the singles everybody else is
#: waiting for would be a cost paid by every other episode.
FILE_ON: Final[int] = 1

#: How many files Arc will read a selection out of. A pack of a long-running
#: series is a few dozen files; anything past this is a collection somebody
#: uploaded rather than a season, and mapping a thousand names to episodes is
#: not a thing to attempt on a background job's clock.
MAX_TORRENT_FILES: Final[int] = 500

#: The directory batches are filed under, one level below the downloads root.
BATCH_DIR: Final[str] = "batch"

# --- What ``torrents.qbit_state`` may hold ----------------------------------
#
# Mostly it holds whatever qBittorrent last called the torrent. The four values
# below are Arc's own, and they live here — beside :data:`COMPLETE_STATES` and
# the client that reads the others — because there is exactly one column they
# describe and three different modules write them. Keeping them together is
# what lets :data:`DECIDED_STATES` be a single set rather than a set each
# module assembles from the other two's imports.

#: A person said in review that the delivered file was not this episode
#: (:mod:`arc.services.acquisition.reject`). The client is still holding the
#: file, deliberately: it is in the review queue and retention owns it.
QBIT_REJECTED: Final[str] = "rejected"

#: ``poll_qbit`` gave up on a torrent that was going nowhere and removed it
#: with its files (:func:`arc.services.acquisition.jobs.stall_reason`).
QBIT_STALLED: Final[str] = "stalled"

#: The reconciler cancelled the download because nobody wanted the episode any
#: more (:func:`arc.services.acquisition.wants.cancel_if_unwanted`). Read by
#: the ``qbit_cancel`` job as its mandate, and the row is deleted once the
#: client has been told.
QBIT_CANCELLED: Final[str] = "cancelled"

#: The torrent is not in Arc's category any more and Arc did not do it.
QBIT_MISSING: Final[str] = "missing"

#: A **batch** whose contents Arc could not read, deleted again before it had
#: fetched anything (FR-A11). The row is kept with no ``torrent_files`` rows at
#: all, and it exists for one reason: the pick skips any hash that already has
#: a row, so this is what stops the same unreadable pack being fetched and
#: added again every six hours, for every episode of the show. Only written for
#: a reason that **cannot change** — files that could not be identified, more
#: files than Arc will read a selection out of, a blob whose hash was not the
#: one the feed advertised. A read-back the client disagreed with, or a Nyaa
#: that would not hand the ``.torrent`` over, leave no row: those can change by
#: tomorrow and the pack deserves another look.
QBIT_UNREADABLE: Final[str] = "unreadable"

#: The values ``poll_qbit`` must never overwrite, in either direction: neither
#: with a live state string from the client nor with :data:`QBIT_MISSING`. Each
#: is a *decision* somebody or something made about this download, and the
#: column is the only record of it — overwriting ``stalled`` with ``missing``
#: sixty seconds later, when Arc is the reason it is missing, would lose the
#: answer to "why is this episode unavailable?".
DECIDED_STATES: Final[frozenset[str]] = frozenset(
    {QBIT_REJECTED, QBIT_STALLED, QBIT_CANCELLED, QBIT_UNREADABLE}
)

#: Below what rate, in KiB/s, qBittorrent counts a torrent as "slow" and stops
#: it occupying one of the active slots. 2 rather than 0: the client treats 0
#: as "no threshold", and a torrent moving a kilobyte a second is not
#: downloading in any sense that matters.
SLOW_RATE_KIB: Final[int] = 2

#: And for how long it must have been that slow first. Five minutes, so that a
#: swarm going quiet over lunch does not cost a healthy download its slot.
SLOW_INACTIVE_SECONDS: Final[int] = 300

#: And the subset ``poll_qbit`` will *delete from the client* on sight, because
#: it is the module that decided it and the deletion may not have happened yet
#: (a client that went away between the flush and the request). Only
#: ``stalled``: a ``rejected`` torrent is still holding a file a person is
#: reviewing and retention owns that, and a ``cancelled`` one belongs to the
#: ``qbit_cancel`` job, which deletes the row as well.
DELETE_ON_SIGHT: Final[frozenset[str]] = frozenset({QBIT_STALLED})


class QbitError(RuntimeError):
    """qBittorrent refused something. Not retryable on its own."""


class QbitUnavailable(QbitError):
    """qBittorrent could not be reached, or would not authenticate.

    Raised as a *transient* failure: the job runner's backoff is the retry, and
    the episode stays where it was rather than being marked unavailable —
    "the client is down" is not "there is no release" (FR-A6).
    """


@dataclass(frozen=True, slots=True)
class TorrentInfo:
    """One row of ``torrents/info``, only the fields Arc reads."""

    hash: str
    name: str
    #: 0..1.
    progress: float
    state: str
    #: Where the payload actually is: the file for a single-file torrent, the
    #: directory for a multi-file one. Container-side.
    content_path: str | None = None
    save_path: str | None = None
    #: Epoch seconds, or a negative sentinel while it is still downloading.
    completion_on: int | None = None
    #: Total bytes of the torrent's selected files. Reported to the admin
    #: panel (FR-D3) rather than used by any rule: what Arc keeps on disk is
    #: measured from the files themselves (``retained_usage``), because a
    #: torrent that finished last month may have had its data deleted since.
    size: int = 0
    #: Bytes per second, right now, as the client reports them.
    dlspeed: int = 0
    upspeed: int = 0
    #: How long the client has actually been **working on** this torrent,
    #: seconds. Not its age: a torrent can sit in ``queuedDL`` for a day behind
    #: :attr:`~arc.config.Settings.qbit_max_active_downloads` and have a
    #: ``time_active`` of nothing, which is the difference between a download
    #: that has failed and one that has not started (:func:`stall_reason`).
    #: ``None`` when the client did not report it, and then nothing is a stall.
    time_active: int | None = None
    #: Seeders and peers **this client is connected to right now**. Always
    #: reported, and routinely 0 between announces on a perfectly healthy
    #: torrent, so no rule may read them as "the swarm is dead". Logged.
    num_seeds: int | None = None
    num_leechs: int | None = None
    #: And the whole swarm, as the tracker last reported it. ``-1`` — mapped to
    #: ``None`` here — means "not scraped yet", which is why these two are the
    #: only figures :func:`stall_reason` will call a dead swarm on.
    num_complete: int | None = None
    num_incomplete: int | None = None

    @property
    def complete(self) -> bool:
        return self.progress >= COMPLETE_PROGRESS or self.state in COMPLETE_STATES

    @property
    def swarm_seeds(self) -> int | None:
        """Seeders in the swarm per the tracker, or ``None`` if never scraped."""
        return self.num_complete

    @property
    def swarm_peers(self) -> int | None:
        """Leechers in the swarm per the tracker — a partial peer is a source."""
        return self.num_incomplete

    @property
    def dead_swarm(self) -> bool:
        """Whether the tracker says there is **nobody at all** on this torrent.

        Both figures known and both zero, and nothing else counts. The
        connected counts (``num_seeds``/``num_leechs``) are deliberately not
        consulted: qBittorrent reports those as 0 whenever it happens to hold
        no connections this instant, which a 60 %-downloaded torrent between
        announces does, and treating that as a dead swarm would delete it.
        """
        return self.swarm_seeds == 0 and self.swarm_peers == 0

    @property
    def completed_at(self) -> datetime | None:
        if self.completion_on is None or self.completion_on <= 0:
            return None
        return datetime.fromtimestamp(self.completion_on, UTC)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> TorrentInfo | None:
        info_hash = raw.get("hash")
        if not isinstance(info_hash, str) or not info_hash:
            return None
        completion = raw.get("completion_on")
        return cls(
            hash=info_hash.lower(),
            name=str(raw.get("name") or ""),
            progress=_fraction(raw.get("progress")),
            state=str(raw.get("state") or ""),
            content_path=raw.get("content_path") or None,
            save_path=raw.get("save_path") or None,
            completion_on=int(completion) if isinstance(completion, int | float) else None,
            size=_number(raw.get("size")),
            dlspeed=_number(raw.get("dlspeed")),
            upspeed=_number(raw.get("upspeed")),
            time_active=_count(raw.get("time_active")),
            num_seeds=_count(raw.get("num_seeds")),
            num_leechs=_count(raw.get("num_leechs")),
            num_complete=_count(raw.get("num_complete")),
            num_incomplete=_count(raw.get("num_incomplete")),
        )


@dataclass(frozen=True, slots=True)
class FileInfo:
    """One row of ``torrents/files``: a file inside a torrent (FR-A11).

    :attr:`index` is the only field with any authority in it — it is what
    ``torrents/filePrio`` takes, and it is the client's own numbering rather
    than anything Arc computes, which is why the file list is read back from
    the client instead of bencoded out of the ``.torrent``.
    """

    #: The client's index for this file. ``filePrio``'s ``id``.
    index: int
    #: The path the torrent gives it, relative to the save path. Slashes and
    #: all: a pack usually puts its episodes in a directory of their own.
    name: str
    size: int
    #: What the file's priority currently is: 0 is off, anything above it is
    #: on. Read back after a write, as the byte-safety gate (§5 step 6).
    priority: int
    #: 0..1, this file's own — an episode inside a batch is complete when its
    #: file is, not when the torrent is.
    progress: float

    @property
    def wanted(self) -> bool:
        """Whether the client will fetch this file at all."""
        return self.priority > FILE_OFF

    @property
    def complete(self) -> bool:
        return self.progress >= COMPLETE_PROGRESS

    @classmethod
    def from_json(cls, raw: dict[str, Any], *, position: int) -> FileInfo | None:
        """One row, or ``None`` for a row with no usable name.

        ``position`` is the fallback for :attr:`index`: qBittorrent only
        started sending an explicit ``index`` field in 4.4, and before that the
        position in the array *was* the index — which is also what ``filePrio``
        took then, so the fallback is the right answer rather than a guess.
        """
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            return None
        index = raw.get("index")
        return cls(
            index=int(index)
            if isinstance(index, int) and not isinstance(index, bool)
            else position,
            name=name,
            size=_number(raw.get("size")),
            priority=_number(raw.get("priority")),
            progress=_fraction(raw.get("progress")),
        )


def _fraction(value: Any) -> float:
    """A 0..1 progress out of a JSON field.

    Clamped rather than trusted: the client has been seen to report a hair over
    1.0 on a completed torrent, and the value is rendered straight into a
    progress bar's width.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return min(max(float(value), 0.0), 1.0)


def _number(value: Any) -> int:
    """A non-negative integer out of a JSON field, or 0 for anything else.

    qBittorrent reports ``-1`` for a size it does not know yet (a magnet whose
    metadata has not arrived), and a display field is not worth raising over.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(int(value), 0)


def _count(value: Any) -> int | None:
    """A count out of a JSON field, or ``None`` for "the client did not say".

    Negative is ``None`` too, and that is the whole reason this is not
    :func:`_number`: qBittorrent reports ``num_complete``/``num_incomplete``
    as ``-1`` until the tracker has been scraped, and clamping that to 0 would
    turn "I do not know yet" into "there is nobody there".
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = int(value)
    return None if number < 0 else number


def _added(response: httpx.Response) -> bool:
    """Whether ``torrents/add`` unambiguously accepted a new torrent.

    ``False`` is "ask the client", not "it failed": see :meth:`QbitClient.add`.
    """
    if response.status_code >= 400:
        return False
    body = response.text.strip()
    if not body or body.casefold().startswith("ok"):
        return True  # 4.x
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    # 5.x. ``failure_count`` is the field that means what it says; the id list
    # is checked too because a 5.x that reports neither has told us nothing.
    failures = payload.get("failure_count")
    added = payload.get("added_torrent_ids")
    return failures == 0 and isinstance(added, list) and len(added) > 0


def _added_ids(response: httpx.Response) -> frozenset[str]:
    """The hashes a 5.x ``torrents/add`` says it added, lower-cased.

    Empty for 4.x, which answers ``Ok.`` and names nothing — so an empty
    answer means "this body cannot confirm the identity", not "nothing was
    added", and the caller asks the client instead (:meth:`QbitClient.add_file`).
    """
    if response.status_code >= 400:
        return frozenset()
    try:
        payload = response.json()
    except ValueError:
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    added = payload.get("added_torrent_ids")
    if not isinstance(added, list):
        return frozenset()
    return frozenset(value.lower() for value in added if isinstance(value, str))


def save_path_for(episode_id: int, *, downloads_path: str) -> str:
    """Where episode ``episode_id`` is downloaded to, container-side.

    One directory per episode, named by the id and by nothing else. That is
    what makes :func:`host_path` reversible, what keeps two releases of the
    same episode from overwriting each other's files, and what M10's retention
    deletes: a directory whose name is an id needs no index to explain it.
    """
    return str(PurePosixPath(downloads_path) / str(episode_id))


def batch_save_path_for(info_hash: str, *, downloads_path: str) -> str:
    """Where a batch is downloaded to, container-side (FR-A11).

    ``<downloads>/batch/<info hash>``, and the hash rather than an episode id
    precisely because **every id-from-path inference must fail** for a batch:
    :func:`arc.services.acquisition.reject.episode_id_of` does
    ``int(relative.parts[0])`` on a reported path, and ``batch`` raises a
    ``ValueError`` and answers ``None``. A pack holding episodes 1 to 26 under
    a directory named for one of them is the one shape that could attribute
    another episode's file to the wrong episode; this fails closed instead.

    It is still under ``downloads_path``, so :func:`host_path` and retention's
    root checks are unchanged.
    """
    return str(PurePosixPath(downloads_path) / BATCH_DIR / info_hash.lower())


def host_path(container_path: str, *, downloads_path: str, host_downloads: Path) -> Path:
    """A path qBittorrent reported, as the worker can open it.

    Raises :class:`QbitError` for anything that is not under
    ``downloads_path``: an absolute path from outside the tree Arc configured
    is either a torrent somebody else added or a misconfiguration, and opening
    it on the strength of a string from another process is not something this
    should do.
    """
    reported = PurePosixPath(container_path)
    root = PurePosixPath(downloads_path)
    try:
        relative = reported.relative_to(root)
    except ValueError as exc:
        raise QbitError(
            f"{container_path!r} is not under the configured downloads path {downloads_path!r}"
        ) from exc
    # ``relative_to`` is textual: ``/data/downloads/9/../../etc`` is "under"
    # the root by that test and outside it on disk. Purely defensive — the
    # string comes from qBittorrent, not from a user — and defensive is what
    # this function is for.
    if ".." in relative.parts:
        raise QbitError(f"{container_path!r} climbs out of {downloads_path!r}")
    return host_downloads.joinpath(*relative.parts)


class QbitClient:
    """A logged-in Web API session. One per job; cheap to build."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        category: str,
        downloads_path: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.category = category
        self.downloads_path = downloads_path
        self._username = username
        self._password = password
        self._logged_in = False
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            # qBittorrent checks the Referer against its own origin unless
            # CSRF protection is turned off; sending it means Arc works
            # against a default install.
            headers={"Referer": self.base_url, "Origin": self.base_url},
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> QbitClient:
        """Build one from the environment, raising if it is not configured.

        ``timeout`` is a parameter because the admin panel's status probe
        (:mod:`arc.services.acquisition.status`) waits a fraction of what a
        background job may: a page an admin is staring at cannot hang for
        twenty seconds on a client that is not answering.
        """
        return cls(
            base_url=settings.qbit_url,
            username=settings.require("qbit_user"),
            password=settings.require("qbit_pass"),
            category=settings.qbit_category,
            downloads_path=settings.qbit_downloads_path,
            transport=transport,
            timeout=timeout,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        """Exchange the credentials for the ``SID`` cookie."""
        try:
            response = await self._client.post(
                f"{API}/auth/login",
                data={"username": self._username, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise QbitUnavailable(f"qbittorrent is not reachable: {exc!r}") from exc
        if response.status_code == httpx.codes.FORBIDDEN:
            # qBittorrent's own ban after too many bad attempts looks like this.
            raise QbitUnavailable("qbittorrent refused the login (banned or wrong credentials)")
        if response.status_code >= 400:
            raise QbitUnavailable(f"qbittorrent login answered {response.status_code}")
        # 200 with the body "Fails." is how a wrong password is reported.
        if response.text.strip().casefold().startswith("fail"):
            raise QbitUnavailable("qbittorrent rejected the credentials")
        self._logged_in = True
        log.info("qbittorrent login ok", extra={"url": self.base_url})

    async def request(
        self,
        method: str,
        path: str,
        *,
        allow_status: frozenset[int] = frozenset(),
        **kwargs: Any,
    ) -> httpx.Response:
        """Call the API, logging in first and again on a 403.

        ``allow_status`` lets one caller handle a status this would otherwise
        raise on; :meth:`add` uses it for the 409 that means "already there".
        """
        if not self._logged_in:
            await self.login()
        for attempt in (1, 2):
            try:
                response = await self._client.request(method, f"{API}{path}", **kwargs)
            except httpx.HTTPError as exc:
                raise QbitUnavailable(f"qbittorrent is not reachable: {exc!r}") from exc
            if response.status_code == httpx.codes.FORBIDDEN and attempt == 1:
                log.info("qbittorrent session expired, logging in again")
                self._logged_in = False
                await self.login()
                continue
            if response.status_code >= 400 and response.status_code not in allow_status:
                raise QbitError(
                    f"qbittorrent {method} {path} answered {response.status_code}: "
                    f"{response.text[:200]}"
                )
            return response
        raise QbitUnavailable("qbittorrent kept refusing the session")  # pragma: no cover

    async def version(self) -> str:
        """The client's own version string (``v5.2.0``), for the admin panel.

        ``app/version`` is the cheapest authenticated call qBittorrent has, so
        it doubles as the reachability probe in
        :func:`arc.services.acquisition.status.qbit_status`: an answer means
        the URL, the credentials and the session are all good.
        """
        response = await self.request("GET", "/app/version")
        return response.text.strip()

    async def has(self, info_hash: str) -> bool:
        """Whether the client is holding this torrent, in any category."""
        response = await self.request("GET", "/torrents/info", params={"hashes": info_hash.lower()})
        try:
            payload = response.json()
        except ValueError:
            return False
        return isinstance(payload, list) and len(payload) > 0

    async def add(self, magnet: str, *, episode_id: int, info_hash: str) -> str:
        """Add ``magnet`` for one episode. Returns the save path used.

        **Adding a torrent the client already holds must succeed**, because
        that is what a retry looks like: the handler can crash between the add
        and its commit, and the second run must not fail forever on a download
        that is already running. What "already holds" looks like on the wire
        depends on the version, and neither shape is a plain success:

        * 4.x answers ``200 Ok.`` to both the new add and the duplicate;
        * 5.x answers ``200`` with a JSON summary
          (``{"added_torrent_ids": […], "failure_count": 0, …}``) for the new
          add and **409 Conflict** for the duplicate — and 409 is also what a
          genuinely rejected add gets.

        So anything that is not an unambiguous success is *checked*: ask the
        client whether it is holding the hash. That answers the question the
        status code is too coarse to answer, and it costs one request on a
        path that is already talking to another process.
        """
        save_path = save_path_for(episode_id, downloads_path=self.downloads_path)
        response = await self.request(
            "POST",
            "/torrents/add",
            allow_status=frozenset({httpx.codes.CONFLICT}),
            data={
                "urls": magnet,
                "category": self.category,
                "savepath": save_path,
                "tags": f"arc,episode:{episode_id}",
                "autoTMM": "false",
            },
        )
        if not _added(response):
            if not await self.has(info_hash):
                raise QbitError(
                    f"qbittorrent refused the torrent ({response.status_code}): "
                    f"{response.text.strip()[:200]}"
                )
            log.info(
                "torrent was already in qbittorrent",
                extra={"episode_id": episode_id, "hash": info_hash},
            )
            return save_path
        log.info(
            "torrent added to qbittorrent",
            extra={"episode_id": episode_id, "savepath": save_path, "category": self.category},
        )
        return save_path

    async def add_file(
        self,
        blob: bytes,
        *,
        save_path: str,
        info_hash: str,
        tags: str,
        stopped: bool = True,
    ) -> str:
        """Add a ``.torrent`` **file**, stopped by default. Returns the save path.

        The batch path (FR-A11), and the reason it is a file rather than a
        magnet is the module docstring's: ``torrents/filePrio`` is refused
        while the client has no metadata, so the magnet route has a window in
        which unwanted bytes arrive and this one has none.

        **Both** ``stopped`` and ``paused`` are sent, with the same value. 5.x
        reads the first and 4.x the second, each ignores the other, and getting
        it wrong means a whole season starts downloading the instant it is
        added — which is the one outcome this whole feature exists to prevent.
        ``contentLayout=Original`` keeps the torrent's own directory structure,
        because the names ``torrents/files`` reports have to be the names on
        disk for the mapped path to open.

        Duplicates are idempotent exactly as :meth:`add`'s are, through the
        same :func:`_added` check and the same confirming :meth:`has` call: a
        handler that crashed between the add and its commit must not fail
        forever on a torrent that is already there.

        **The client has to confirm the identity, not just the success.**
        Everything after this call — the file list, the selection, the
        read-back, the ``torrents`` row, the save path the library will open —
        is keyed on ``info_hash``, which came out of a *feed*. If the blob
        behind that feed item is some other torrent, a "200, added one" tells
        Arc nothing it needs to know, and every later call would quietly act on
        a hash the client has never heard of. So the hash must appear in 5.x's
        ``added_torrent_ids`` or :meth:`has` must say the client is holding it;
        neither, and this raises (which the batch pick reads as a refused
        candidate). The cost of a mismatch, stated: the torrent the client
        really did add is left behind, stopped, with nothing selected and
        nothing fetched — visible in the Web UI under Arc's category, which is
        a better ending than a selection written against the wrong torrent.
        4.x names no ids at all, so on that client the confirming request is
        always made; the batch path makes six calls anyway.
        """
        flag = "true" if stopped else "false"
        response = await self.request(
            "POST",
            "/torrents/add",
            allow_status=frozenset({httpx.codes.CONFLICT}),
            files={"torrents": ("release.torrent", blob, "application/x-bittorrent")},
            data={
                "category": self.category,
                "savepath": save_path,
                "tags": tags,
                "autoTMM": "false",
                "contentLayout": "Original",
                "stopped": flag,
                "paused": flag,
            },
        )
        accepted = _added(response)
        if info_hash.lower() not in _added_ids(response) and not await self.has(info_hash):
            raise QbitError(
                f"qbittorrent did not accept the torrent file as {info_hash} "
                f"({response.status_code}): {response.text.strip()[:200]}"
            )
        if not accepted:
            log.info("torrent file was already in qbittorrent", extra={"hash": info_hash})
            return save_path
        log.info(
            "torrent file added to qbittorrent",
            extra={
                "hash": info_hash,
                "savepath": save_path,
                "category": self.category,
                "bytes": len(blob),
                "stopped": stopped,
            },
        )
        return save_path

    async def files(self, info_hash: str) -> list[FileInfo]:
        """Every file in one torrent, as the client indexes them (FR-A11).

        The authority on two things Arc cannot work out for itself: the indices
        :meth:`file_priority` takes, and what a selection actually *is* after
        it has been written — step 6 of the add sequence reads this back and
        refuses the torrent if any index other than the intended ones is on.

        **A listing this cannot fully parse is an error, not a shorter
        listing.** A row with no usable name used to be dropped, which is the
        one way this function could be quietly unsafe: the index behind that
        row would then never be named in the ``filePrio 0`` that turns every
        file off — and a freshly added torrent has every file selected — while
        being absent from both listings the read-back compares, so it would
        download unseen and unrecorded. A pack Arc cannot enumerate is a pack
        Arc does not start, so the count has to match the payload's exactly.
        """
        response = await self.request("GET", "/torrents/files", params={"hash": info_hash.lower()})
        try:
            payload = response.json()
        except ValueError as exc:
            raise QbitError(f"qbittorrent answered unparseable JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise QbitError("qbittorrent torrents/files did not answer with a list")
        rows = [
            FileInfo.from_json(raw, position=position) if isinstance(raw, dict) else None
            for position, raw in enumerate(payload)
        ]
        kept = [row for row in rows if row is not None]
        if len(kept) != len(payload):
            raise QbitError(
                f"qbittorrent listed {len(payload)} files for {info_hash.lower()} and "
                f"{len(kept)} of them could be read"
            )
        return kept

    async def file_priority(self, info_hash: str, indices: Sequence[int], priority: int) -> None:
        """Set ``priority`` on the given file indices of one torrent.

        ``id`` is a ``|``-joined list of indices, which is qBittorrent's own
        shape for this endpoint. Sorted and de-duplicated before it is sent:
        the client does not care about the order, and a request whose body is a
        function of its arguments is a request a test can assert on.

        An empty list makes no request — "select nothing" through this endpoint
        would be a call with an empty ``id``, which the client reads as an
        error rather than as a no-op. Callers turning every file off pass every
        index explicitly (:data:`FILE_OFF`).
        """
        wanted = sorted({int(index) for index in indices})
        if not wanted:
            return
        await self.request(
            "POST",
            "/torrents/filePrio",
            data={
                "hash": info_hash.lower(),
                "id": "|".join(str(index) for index in wanted),
                "priority": str(priority),
            },
        )
        log.info(
            "file priorities written",
            extra={"hash": info_hash.lower(), "count": len(wanted), "priority": priority},
        )

    async def start(self, hashes: list[str]) -> None:
        """Start (resume) torrents — the last step of adding a batch.

        ``torrents/start`` is qBittorrent 5's name for it and 4.x calls the
        same thing ``torrents/resume``, so a refusal that is not "the client is
        unreachable" falls back once, exactly as :meth:`stop` falls back to
        ``torrents/pause``. The client is whatever the operator pulled.

        This is the only call in the batch sequence after which bytes may move,
        which is why it is last: the selection is written and verified first.
        """
        wanted = sorted({value.lower() for value in hashes if value})
        if not wanted:
            return
        data = {"hashes": "|".join(wanted)}
        try:
            await self.request("POST", "/torrents/start", data=data)
        except QbitUnavailable:
            raise
        except QbitError:
            log.info("this qbittorrent has no torrents/start; using torrents/resume")
            await self.request("POST", "/torrents/resume", data=data)
        log.info("torrents started", extra={"count": len(wanted)})

    async def apply_policy(
        self,
        *,
        seeding: bool = False,
        upload_limit_kib: int = 512,
        max_active_downloads: int = 8,
        max_active_torrents: int = 12,
    ) -> dict[str, object]:
        """Write Arc's seeding **and queue** policy to the client. Returns what it sent.

        Three settings for the seeding half, and they are the mitigations spec
        §9 lists rather than performance tuning:

        * ``max_ratio 0`` with ``max_ratio_act`` = :data:`STOP_AT_SHARE_LIMIT`
          — the torrent is stopped as soon as it has finished, because a ratio
          of zero has already been reached the moment there is anything to
          share;
        * ``max_seeding_time 0`` — the same statement in the other unit, so a
          client that disagrees about when a ratio limit counts still stops;
        * ``up_limit`` — a global upload cap in bytes per second, which
          applies *while downloading* too, where a ratio limit cannot.

        ``dht`` and ``pex`` are left exactly as they are: turning them off
        would break the swarms Arc downloads from, and they are not what a
        copyright notice is about.

        With ``seeding`` true only the rate cap is sent. Not "nothing at all":
        an operator who wants to seed still wants a bounded upload, and — more
        to the point — leaving the ratio settings alone means Arc does not
        silently undo a limit the operator set by hand.

        And four for the **queue**, which Arc owns for the same reason: they
        are qBittorrent's defaults otherwise, and its defaults are three
        concurrent downloads and five active torrents. Three slots is nothing
        when a MAL import has produced four hundred wants, and a slot held by a
        torrent that will never finish is a slot held for ever — production
        spent a whole day with its three occupied by dead 2018 uploads while
        everything anybody was actually watching queued behind them.

        * ``queueing_enabled`` — without it the limits below are ignored and
          the client starts everything at once, which is the other way to make
          no progress;
        * ``max_active_downloads`` and ``max_active_torrents`` — the two
          ceilings, downloads inside the wider "active" count;
        * ``dont_count_slow_torrents``, with its three thresholds set
          deliberately (:data:`SLOW_RATE_KIB`, :data:`SLOW_INACTIVE_SECONDS`)
          rather than left at whatever the client shipped — a torrent counts as
          slow only after **five minutes** of moving essentially nothing in
          either direction, so an ordinary lull never costs a download its
          slot, and a dead one stops blocking the queue five minutes in. This
          only changes what *counts*: the queue never removes anything, and
          Arc's own stall rule
          (:func:`~arc.services.acquisition.jobs.stall_reason`) is the only
          thing that gets rid of a torrent that is going nowhere.

        The queue half is sent whatever ``seeding`` says: how many downloads
        run at once is not a statement about uploading.

        Preferences are sent as a JSON blob in a form field named ``json``,
        which is qBittorrent's own shape for this endpoint, and only the keys
        named here are touched.
        """
        prefs: dict[str, object] = {
            "up_limit": upload_limit_kib * 1024,
            "queueing_enabled": True,
            "max_active_downloads": max_active_downloads,
            "max_active_torrents": max_active_torrents,
            "dont_count_slow_torrents": True,
            "slow_torrent_dl_rate_threshold": SLOW_RATE_KIB,
            "slow_torrent_ul_rate_threshold": SLOW_RATE_KIB,
            "slow_torrent_inactive_timer": SLOW_INACTIVE_SECONDS,
        }
        if not seeding:
            prefs |= {
                "max_ratio_enabled": True,
                "max_ratio": 0,
                "max_ratio_act": STOP_AT_SHARE_LIMIT,
                "max_seeding_time_enabled": True,
                "max_seeding_time": 0,
            }
        await self.request("POST", "/app/setPreferences", data={"json": json.dumps(prefs)})
        log.info("qbittorrent policy applied", extra={"seeding": seeding, **prefs})
        return prefs

    async def stop(self, hashes: list[str]) -> None:
        """Stop (pause) torrents the caller has already seen in the listing.

        ``torrents/stop`` is qBittorrent 5's name for it; 4.x calls the same
        thing ``torrents/pause`` and answers 404 to the new name, so a 404 —
        or any other refusal that is not "the client is unreachable" — falls
        back once. The client is whatever the operator pulled.

        **Only hashes Arc is entitled to act on.** Two ways to be sure of that
        and both are in use: a hash that came out of :meth:`torrents`, which is
        filtered to Arc's category — the same guarantee :meth:`delete` buys
        itself with an extra request — or one Arc recorded itself when it added
        the torrent, which is where ``qbit_reselect`` gets the pack it stops
        (FR-A11). What must never be passed is a string from anywhere else.
        """
        wanted = sorted({value.lower() for value in hashes if value})
        if not wanted:
            return
        data = {"hashes": "|".join(wanted)}
        try:
            await self.request("POST", "/torrents/stop", data=data)
        except QbitUnavailable:
            raise
        except QbitError:
            log.info("this qbittorrent has no torrents/stop; using torrents/pause")
            await self.request("POST", "/torrents/pause", data=data)
        log.info("torrents stopped", extra={"count": len(wanted)})

    async def torrents(self) -> list[TorrentInfo]:
        """Everything in Arc's category, as ``hash → state`` rows."""
        response = await self.request("GET", "/torrents/info", params={"category": self.category})
        try:
            payload = response.json()
        except ValueError as exc:
            raise QbitError(f"qbittorrent answered unparseable JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise QbitError("qbittorrent torrents/info did not answer with a list")
        rows = [TorrentInfo.from_json(raw) for raw in payload if isinstance(raw, dict)]
        return [row for row in rows if row is not None]

    async def delete(self, hashes: list[str], *, delete_files: bool = True) -> None:
        """Remove torrents from the client, optionally with their data.

        **Every hash is checked against Arc's own category first**, and one
        that is not in it is dropped with a warning rather than sent. M10's
        retention is the caller, ``deleteFiles`` is normally true, and
        ``torrents/delete`` takes a list of hashes with no notion of a
        category — so the one thing standing between a bad row in ``torrents``
        and somebody else's downloads being deleted is this check. It costs one
        request, on a path that runs at most once a day and is about to make
        another one anyway.
        """
        wanted = {value.lower() for value in hashes if value}
        if not wanted:
            return
        mine = {info.hash for info in await self.torrents()}
        allowed = sorted(wanted & mine)
        for skipped in sorted(wanted - mine):
            log.warning(
                "refusing to delete a torrent that is not in arc's category",
                extra={"hash": skipped, "category": self.category},
            )
        if not allowed:
            return
        await self.request(
            "POST",
            "/torrents/delete",
            data={"hashes": "|".join(allowed), "deleteFiles": "true" if delete_files else "false"},
        )
        log.info(
            "torrents deleted from qbittorrent",
            extra={"count": len(allowed), "delete_files": delete_files},
        )


__all__ = [
    "API",
    "BATCH_DIR",
    "COMPLETE_PROGRESS",
    "COMPLETE_STATES",
    "DECIDED_STATES",
    "DELETE_ON_SIGHT",
    "FILE_OFF",
    "FILE_ON",
    "MAX_TORRENT_FILES",
    "QBIT_CANCELLED",
    "QBIT_MISSING",
    "QBIT_REJECTED",
    "QBIT_STALLED",
    "QBIT_UNREADABLE",
    "SEEDING_STATES",
    "SLOW_INACTIVE_SECONDS",
    "SLOW_RATE_KIB",
    "STOP_AT_SHARE_LIMIT",
    "FileInfo",
    "QbitClient",
    "QbitError",
    "QbitUnavailable",
    "TorrentInfo",
    "batch_save_path_for",
    "host_path",
    "save_path_for",
]
