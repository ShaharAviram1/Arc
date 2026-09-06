"""The qBittorrent Web API, as much of it as Arc needs (FR-A5).

Four calls — ``auth/login``, ``torrents/add``, ``torrents/info`` and
``torrents/delete`` (architecture.md §6) — and one piece of arithmetic that is
easy to get wrong and expensive to get wrong: **the path mapping**.

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

**A 403 means the cookie expired, not that the credentials are wrong.**
qBittorrent's session lasts an hour by default and the worker is long-lived,
so the first call after an idle night gets a 403. :meth:`QbitClient.request`
logs in again and retries once; a second 403 is a real authentication failure
and raises.
"""

from __future__ import annotations

import logging
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

#: Progress at which a torrent is finished whatever it calls its state. Floats
#: from a JSON API are not compared for equality with 1.0 — 0.9999999 is a
#: finished download, and waiting forever for the last bit would be silly.
COMPLETE_PROGRESS: Final[float] = 0.999


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

    @property
    def complete(self) -> bool:
        return self.progress >= COMPLETE_PROGRESS or self.state in COMPLETE_STATES

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
        progress = raw.get("progress")
        completion = raw.get("completion_on")
        return cls(
            hash=info_hash.lower(),
            name=str(raw.get("name") or ""),
            progress=float(progress) if isinstance(progress, int | float) else 0.0,
            state=str(raw.get("state") or ""),
            content_path=raw.get("content_path") or None,
            save_path=raw.get("save_path") or None,
            completion_on=int(completion) if isinstance(completion, int | float) else None,
        )


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


def save_path_for(episode_id: int, *, downloads_path: str) -> str:
    """Where episode ``episode_id`` is downloaded to, container-side.

    One directory per episode, named by the id and by nothing else. That is
    what makes :func:`host_path` reversible, what keeps two releases of the
    same episode from overwriting each other's files, and what M10's retention
    deletes: a directory whose name is an id needs no index to explain it.
    """
    return str(PurePosixPath(downloads_path) / str(episode_id))


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
        cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> QbitClient:
        """Build one from the environment, raising if it is not configured."""
        return cls(
            base_url=settings.qbit_url,
            username=settings.require("qbit_user"),
            password=settings.require("qbit_pass"),
            category=settings.qbit_category,
            downloads_path=settings.qbit_downloads_path,
            transport=transport,
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
    "COMPLETE_PROGRESS",
    "COMPLETE_STATES",
    "QbitClient",
    "QbitError",
    "QbitUnavailable",
    "TorrentInfo",
    "host_path",
    "save_path_for",
]
