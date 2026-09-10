"""Row builders and stub transports shared by the M6 tests.

Kept out of ``conftest.py`` on purpose: these are specific to acquisition, and
a fixture in ``conftest`` is a fixture every test collects.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Anime, Episode, EpisodeState, ListEntry, ListStatus, Setting, User
from arc.services.auth import create_user

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nyaa"

#: Ids well outside anything the other suites use, so a leftover row from a
#: half-rolled-back test cannot be mistaken for one of these.
BASE_ANILIST_ID = 960000


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def now() -> datetime:
    return datetime.now(UTC)


async def make_anime(
    session: AsyncSession,
    *,
    anilist_id: int = BASE_ANILIST_ID,
    romaji: str = "Sousou no Frieren",
    english: str | None = "Frieren: Beyond Journey's End",
    status: str = "RELEASING",
    episodes: int | None = 28,
    synonyms: list[str] | None = None,
    next_airing: dict[str, Any] | None = None,
) -> Anime:
    anime = Anime(
        anilist_id=anilist_id,
        title_romaji=romaji,
        title_english=english,
        synonyms=synonyms,
        status=status,
        episodes=episodes,
        next_airing=next_airing,
    )
    session.add(anime)
    await session.flush()
    return anime


async def make_episodes(
    session: AsyncSession,
    anime: Anime,
    count: int,
    *,
    aired_through: int | None = None,
    state: EpisodeState = EpisodeState.NOT_WANTED,
) -> list[Episode]:
    """``count`` episodes; the first ``aired_through`` of them have aired.

    Air times are an hour apart and in the past/future accordingly, which is
    all :mod:`arc.services.catalog.airing` reads.
    """
    aired = count if aired_through is None else aired_through
    moment = now()
    rows: list[Episode] = []
    for number in range(1, count + 1):
        offset = timedelta(days=number - aired)
        rows.append(
            Episode(
                anime_id=anime.id,
                number=number,
                air_at=moment + offset - timedelta(hours=1),
                state=state,
            )
        )
    session.add_all(rows)
    await session.flush()
    return rows


async def make_user(session: AsyncSession, email: str) -> User:
    user = await create_user(session, email, "password12345")
    await session.flush()
    return user


async def make_entry(
    session: AsyncSession,
    user: User,
    anime: Anime,
    *,
    status: ListStatus = ListStatus.WATCHING,
    progress: int = 0,
) -> ListEntry:
    entry = ListEntry(user_id=user.id, anime_id=anime.id, status=status, progress=progress)
    session.add(entry)
    await session.flush()
    return entry


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    existing = await session.get(Setting, key)
    if existing is None:
        session.add(Setting(key=key, value=value))
    else:
        existing.value = value
    await session.flush()


def acquisition_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings pointing at a throwaway data directory and fake services."""
    values: dict[str, Any] = {
        "env": "test",
        "data_dir": tmp_path,
        "nyaa_url": "https://nyaa.test",
        "qbit_url": "http://qbit.test",
        "qbit_user": "admin",
        "qbit_pass": "adminadmin",
        "qbit_category": "arc",
        "qbit_downloads_path": "/data/downloads",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# --- Stub transports --------------------------------------------------------


class NyaaStub:
    """An ``httpx.MockTransport`` that answers Nyaa RSS from fixtures."""

    def __init__(self, answers: dict[str, str] | None = None, *, default: str | None = None):
        #: query string → XML body. Matched on the ``q`` parameter.
        self.answers = answers or {}
        self.default = default if default is not None else read_fixture("search_empty.xml")
        self.queries: list[str] = []
        self.status: int = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        self.queries.append(query)
        if self.status >= 400:
            return httpx.Response(self.status, text="error")
        return httpx.Response(200, text=self.answers.get(query, self.default))


class QbitStub:
    """A tiny in-memory qBittorrent, enough for the client and the jobs.

    It behaves like the real one where the real one is awkward: ``auth/login``
    hands out a cookie and everything else answers 403 until it has been
    called, and ``torrents/add`` speaks whichever dialect ``api_version`` says
    — ``"4"`` answers ``Ok.`` to everything, ``"5"`` (the default, and what
    qBittorrent 5.2 / Web API 2.15 actually does) answers a JSON summary for a
    new torrent and **409 Conflict** for one it already holds.
    """

    def __init__(
        self,
        *,
        password: str = "adminadmin",
        api_version: str = "5",
        version: str = "v5.2.0",
    ):
        self.api_version = api_version
        #: What ``app/version`` answers — the string the admin panel shows.
        self.version = version
        self.password = password
        self.logged_in = False
        self.logins = 0
        self.added: list[dict[str, str]] = []
        self.deleted: list[dict[str, str]] = []
        #: Every ``app/setPreferences`` body, already decoded from its ``json``
        #: form field.
        self.preferences: list[dict[str, Any]] = []
        #: Hashes passed to ``torrents/stop`` (or, on ``api_version`` 4, to
        #: ``torrents/pause`` — which is what that version calls it, and the
        #: only endpoint of the two it answers).
        self.stopped: list[str] = []
        self.torrents: list[dict[str, Any]] = []
        self.calls: list[str] = []
        #: Set to make the next non-login call answer 403 once, as an expired
        #: session does.
        self.expire_once = False
        #: Set to make every request raise, as an unreachable client does.
        self.down = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def _form(self, request: httpx.Request) -> dict[str, str]:
        body = request.content.decode()
        pairs: dict[str, str] = {}
        for chunk in body.split("&"):
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            pairs[key] = httpx.URL(f"http://x/?{key}={value}").params.get(key, "")
        return pairs

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("qbittorrent is not running", request=request)
        path = request.url.path
        self.calls.append(path)

        if path.endswith("/auth/login"):
            self.logins += 1
            form = self._form(request)
            if form.get("password") != self.password:
                return httpx.Response(200, text="Fails.")
            self.logged_in = True
            return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=abc; Path=/"})

        if not self.logged_in or self.expire_once:
            self.expire_once = False
            self.logged_in = False
            return httpx.Response(403, text="Forbidden")

        if path.endswith("/torrents/add"):
            form = self._form(request)
            self.added.append(form)
            known = self._hash_of(form.get("urls", ""))
            if self.api_version == "4":
                return httpx.Response(200, text="Ok.")
            if known is not None and any(t["hash"] == known for t in self.torrents):
                return httpx.Response(409, text="Conflict")
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "added_torrent_ids": [known or "0" * 40],
                        "failure_count": 0,
                        "pending_count": 0,
                        "success_count": 1,
                    }
                ),
            )
        if path.endswith("/torrents/info"):
            category = request.url.params.get("category")
            hashes = request.url.params.get("hashes")
            rows = [t for t in self.torrents if category is None or t.get("category") == category]
            if hashes is not None:
                wanted = {value.lower() for value in hashes.split("|")}
                rows = [t for t in self.torrents if t["hash"].lower() in wanted]
            return httpx.Response(200, text=json.dumps(rows))
        if path.endswith("/torrents/delete"):
            self.deleted.append(self._form(request))
            return httpx.Response(200, text="")
        if path.endswith("/torrents/stop") or path.endswith("/torrents/pause"):
            # qBittorrent 4.x has only ``pause`` and answers 404 to ``stop``,
            # which is the fallback the client is written for.
            if self.api_version == "4" and path.endswith("/torrents/stop"):
                return httpx.Response(404, text="Not Found")
            hashes = self._form(request).get("hashes", "")
            self.stopped.extend(value for value in hashes.split("|") if value)
            for torrent in self.torrents:
                if torrent["hash"].lower() in hashes.lower().split("|"):
                    torrent["state"] = "stoppedUP" if self.api_version == "5" else "pausedUP"
            return httpx.Response(200, text="")
        if path.endswith("/app/version"):
            return httpx.Response(200, text=self.version)
        if path.endswith("/app/setPreferences"):
            raw = self._form(request).get("json", "{}")
            self.preferences.append(json.loads(raw))
            return httpx.Response(200, text="")
        return httpx.Response(404, text="not found")

    @staticmethod
    def _hash_of(magnet: str) -> str | None:
        """The btih out of a magnet, as the real client would read it."""
        marker = "urn:btih:"
        if marker not in magnet:
            return None
        return magnet.split(marker, 1)[1].split("&", 1)[0].lower()

    def add_torrent(
        self,
        info_hash: str,
        *,
        name: str = "release.mkv",
        progress: float = 0.0,
        state: str = "downloading",
        content_path: str | None = None,
        category: str = "arc",
        completion_on: int = -1,
        size: int = 0,
        dlspeed: int = 0,
        upspeed: int = 0,
    ) -> None:
        self.torrents.append(
            {
                "hash": info_hash,
                "name": name,
                "progress": progress,
                "state": state,
                "content_path": content_path,
                "save_path": content_path,
                "category": category,
                "completion_on": completion_on,
                "size": size,
                "dlspeed": dlspeed,
                "upspeed": upspeed,
            }
        )


def force_transport(cls: type, transport: httpx.MockTransport) -> Callable[..., None]:
    """A replacement ``__init__`` for ``cls`` that pins ``transport=``.

    Both clients build their own ``httpx.AsyncClient``, and the process-wide
    Nyaa one is built where no test can reach the call. Patching the
    constructor is what lets a stub reach it anyway, wherever it is made.
    """
    original = cls.__init__

    def patched(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    return patched


def no_sleep(recorded: list[float]) -> Callable[[float], Any]:
    """A stand-in for ``asyncio.sleep`` that records instead of waiting."""

    async def fake(seconds: float) -> None:
        recorded.append(seconds)

    return fake
