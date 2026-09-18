"""Row builders and stub transports shared by the M6 tests.

Kept out of ``conftest.py`` on purpose: these are specific to acquisition, and
a fixture in ``conftest`` is a fixture every test collects.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.models import Anime, Episode, EpisodeState, ListEntry, ListStatus, Setting, User
from arc.services.acquisition.rules import BYTES_PER_GB
from arc.services.auth import create_user
from arc.services.storage import DiskUsage

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
    activated: bool = True,
) -> ListEntry:
    """One list entry, **activated** by default (FR-A9).

    A test that says "this user is watching this show" means a user who chose
    to, which is what ``activated_at`` records. ``activated=False`` is the
    other kind of row — one a MyAnimeList import created and nobody has touched
    — and it is the tests about dormancy that ask for it.
    """
    entry = ListEntry(
        user_id=user.id,
        anime_id=anime.id,
        status=status,
        progress=progress,
        activated_at=now() if activated else None,
    )
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


#: What the stub pretends the data volume is: a 100 GB disk with as much free
#: as the test asks for. Only ``free`` is read by the guard; the other two are
#: there so the admin status endpoint has something coherent to report.
STUB_DISK_TOTAL = 100 * BYTES_PER_GB


def fake_free_space(
    monkeypatch: Any, module: Any, free_bytes: int | None, *, total: int = STUB_DISK_TOTAL
) -> None:
    """Make ``module``'s ``disk_usage`` report ``free_bytes`` free (FR-T6).

    ``None`` is the unmeasurable path — no answer at all, which the guard must
    read as "not held" rather than as zero free.

    Stubbed rather than driven by a floor bigger than the disk, because the
    floor is capped at :data:`~arc.services.acquisition.rules.MAX_MIN_FREE_GB`
    (1 TB) and a machine with more than that free would quietly stop testing
    the rule. ``module`` is whichever one imported the function — the guard
    (:mod:`arc.services.acquisition.rules`) or the status endpoint
    (:mod:`arc.api.acquisition`) — because both hold their own reference.
    """
    usage = (
        None
        if free_bytes is None
        else DiskUsage(total=total, used=total - free_bytes, free=free_bytes)
    )
    monkeypatch.setattr(module, "disk_usage", lambda _path: usage)


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
    """An ``httpx.MockTransport`` that answers Nyaa RSS from fixtures.

    ``answers`` is keyed by the **whole** query string, which is all a narrowed
    form is: ``"Kimetsu no Yaiba - 10"`` and ``"Kimetsu no Yaiba - 10
    HorribleSubs"`` are two keys and two different feeds, exactly as they are
    two different questions to Nyaa (2026-09-18).

    It also answers the ``.torrent`` behind an item (FR-A11), and **not** into
    :attr:`queries`: a blob fetch is not a search, and "how many times was Nyaa
    asked for a feed?" is the assertion half these tests are built on —
    ``queries == []`` is what proves a second episode attached to a batch for
    free. A URL whose last path component is a 40-character hash answers
    :func:`torrent_blob` of it, so a feed that writes the hash into its
    ``<link>`` needs no registration at all; :attr:`blobs` overrides one by URL,
    and anything else is a 404, which is what a candidate Arc cannot fetch
    looks like.
    """

    def __init__(self, answers: dict[str, str] | None = None, *, default: str | None = None):
        #: query string → XML body. Matched on the ``q`` parameter.
        self.answers = answers or {}
        self.default = default if default is not None else read_fixture("search_empty.xml")
        self.queries: list[str] = []
        #: URL → the exact body ``torrent_file`` should get back, for the tests
        #: that care what the bytes are.
        self.blobs: dict[str, bytes] = {}
        #: Every ``.torrent`` URL asked for, in order.
        self.fetched: list[str] = []
        self.status: int = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path.endswith(".torrent"):
            self.fetched.append(url)
            if self.status >= 400:
                return httpx.Response(self.status, text="error")
            registered = self.blobs.get(url)
            if registered is not None:
                return httpx.Response(200, content=registered)
            stem = request.url.path.rsplit("/", 1)[-1].removesuffix(".torrent")
            if len(stem) == 40 and all(char in "0123456789abcdef" for char in stem.lower()):
                return httpx.Response(200, content=torrent_blob(stem))
            return httpx.Response(404, text="not found")
        query = request.url.params.get("q", "")
        self.queries.append(query)
        if self.status >= 400:
            return httpx.Response(self.status, text="error")
        return httpx.Response(200, text=self.answers.get(query, self.default))


def torrent_blob(info_hash: str) -> bytes:
    """A stand-in for a bencoded ``.torrent`` whose hash the stub can read.

    A real client reads the info hash out of the bencoded ``info`` dict, which
    means a test would have to bencode one to say *which* torrent it uploaded.
    This carries the hash in a single field instead, and
    :meth:`QbitStub._blob_hash` reads it back — so an ``add_file`` test asserts
    on the same identity the client passed, without a bencode encoder in the
    fixtures for the sake of one string.
    """
    folded = info_hash.lower().encode()
    return b"d4:hash" + str(len(folded)).encode() + b":" + folded + b"e"


class QbitStub:
    """A tiny in-memory qBittorrent, enough for the client and the jobs.

    It behaves like the real one where the real one is awkward: ``auth/login``
    hands out a cookie and everything else answers 403 until it has been
    called, and ``torrents/add`` speaks whichever dialect ``api_version`` says
    — ``"4"`` answers ``Ok.`` to everything, ``"5"`` (the default, and what
    qBittorrent 5.2 / Web API 2.15 actually does) answers a JSON summary for a
    new torrent and **409 Conflict** for one it already holds.

    ``api_version`` also decides the endpoint names: 4.x has ``torrents/pause``
    and ``torrents/resume`` and answers **404** to the 5.x ``torrents/stop``
    and ``torrents/start``, which is the fallback both client methods exist
    for.

    The batch half (FR-A11) is ``torrents/add`` with a **multipart** body — the
    uploaded blob is kept in :attr:`uploaded` and the torrent is registered
    stopped, so a read-back reports ``stoppedDL`` and no progress —
    ``torrents/files``, seeded per hash with :meth:`add_files`, and
    ``torrents/filePrio``, which records every call in :attr:`priorities`
    **in order** and applies it to the seeded list so the read-back the byte
    guarantee depends on reflects what was written.
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
        #: One entry per **multipart** add: the fields and the uploaded bytes.
        self.uploaded: list[dict[str, Any]] = []
        self.deleted: list[dict[str, str]] = []
        #: Every ``app/setPreferences`` body, already decoded from its ``json``
        #: form field.
        self.preferences: list[dict[str, Any]] = []
        #: Hashes passed to ``torrents/stop`` (or, on ``api_version`` 4, to
        #: ``torrents/pause`` — which is what that version calls it, and the
        #: only endpoint of the two it answers).
        self.stopped: list[str] = []
        #: And to ``torrents/start`` / ``torrents/resume``.
        self.started: list[str] = []
        self.torrents: list[dict[str, Any]] = []
        #: hash → the rows ``torrents/files`` answers, in index order.
        self.file_lists: dict[str, list[dict[str, Any]]] = {}
        #: Every ``torrents/filePrio`` call, in the order they were made:
        #: ``{"hash": …, "indices": [0, 1, …], "priority": 0}``. The order is
        #: the assertion the byte guarantee needs — all files off *before* the
        #: wanted ones on.
        self.priorities: list[dict[str, Any]] = []
        #: hash → indices whose priority this client silently refuses to
        #: change. A client that accepted ``filePrio`` and did not honour it is
        #: exactly what step 6's read-back exists to catch, and it is the one
        #: failure a stub has to be able to stage: without it the byte-safety
        #: gate is code no test ever reaches.
        self.ignores_prio: dict[str, list[int]] = {}
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

    @staticmethod
    def _multipart(request: httpx.Request) -> tuple[dict[str, str], bytes | None]:
        """A ``multipart/form-data`` body as (text fields, the uploaded file).

        Parsed by hand rather than with a library: the only bodies that reach
        here are the ones :meth:`QbitClient.add_file` builds, and a parser that
        understands exactly those is shorter than a dependency.
        """
        content_type = request.headers.get("content-type", "")
        _, _, marker = content_type.partition("boundary=")
        boundary = b"--" + marker.strip('"').encode()
        fields: dict[str, str] = {}
        blob: bytes | None = None
        for part in request.content.split(boundary):
            head, separator, body = part.partition(b"\r\n\r\n")
            if not separator:
                continue
            headers = head.decode(errors="replace")
            if 'name="' not in headers:
                continue
            name = headers.split('name="', 1)[1].split('"', 1)[0]
            payload = body.rstrip(b"\r\n-")
            if "filename=" in headers:
                blob = payload
            else:
                fields[name] = payload.decode()
        return fields, blob

    def _blob_hash(self, blob: bytes) -> str | None:
        """The hash out of a :func:`torrent_blob`, as the client reads its own."""
        marker = b"4:hash"
        if marker not in blob:
            return None
        length, _, rest = blob.split(marker, 1)[1].partition(b":")
        try:
            return rest[: int(length)].decode()
        except ValueError:
            return None

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
            if request.headers.get("content-type", "").startswith("multipart/form-data"):
                return self._add_file(request)
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
            form = self._form(request)
            self.deleted.append(form)
            # Gone is gone: the next listing must not still show it, or a test
            # of "what happens on the poll after a removal" would be testing
            # nothing.
            removed = {value.lower() for value in form.get("hashes", "").split("|") if value}
            self.torrents = [t for t in self.torrents if t["hash"].lower() not in removed]
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
        if path.endswith("/torrents/files"):
            info_hash = (request.url.params.get("hash") or "").lower()
            return httpx.Response(200, text=json.dumps(self.file_lists.get(info_hash, [])))
        if path.endswith("/torrents/filePrio"):
            form = self._form(request)
            info_hash = form.get("hash", "").lower()
            indices = [int(value) for value in form.get("id", "").split("|") if value]
            priority = int(form.get("priority", "0"))
            self.priorities.append({"hash": info_hash, "indices": indices, "priority": priority})
            # Applied, not just recorded: step 6 of the batch sequence reads the
            # list back and refuses the torrent if it disagrees, so a stub that
            # did not apply a write would make that gate untestable.
            stubborn = set(self.ignores_prio.get(info_hash, ()))
            for row in self.file_lists.get(info_hash, []):
                if row.get("index") in indices and row.get("index") not in stubborn:
                    row["priority"] = priority
            return httpx.Response(200, text="")
        if path.endswith("/torrents/start") or path.endswith("/torrents/resume"):
            # 4.x has only ``resume`` and answers 404 to ``start``, the other
            # half of the pair ``stop``/``pause`` are in.
            if self.api_version == "4" and path.endswith("/torrents/start"):
                return httpx.Response(404, text="Not Found")
            hashes = self._form(request).get("hashes", "")
            wanted = [value for value in hashes.split("|") if value]
            self.started.extend(wanted)
            for torrent in self.torrents:
                if torrent["hash"].lower() in {value.lower() for value in wanted}:
                    torrent["state"] = "downloading"
            return httpx.Response(200, text="")
        if path.endswith("/app/version"):
            return httpx.Response(200, text=self.version)
        if path.endswith("/app/setPreferences"):
            raw = self._form(request).get("json", "{}")
            self.preferences.append(json.loads(raw))
            return httpx.Response(200, text="")
        return httpx.Response(404, text="not found")

    def _add_file(self, request: httpx.Request) -> httpx.Response:
        """``torrents/add`` with a ``.torrent`` in the body (FR-A11).

        ``stopped`` is honoured — 5.x's field, falling back to 4.x's
        ``paused``, which is why the client sends both — and the registered
        torrent is therefore ``stoppedDL`` at 0 progress: a batch that reported
        any progress out of this would mean the byte guarantee had been broken.
        """
        fields, blob = self._multipart(request)
        self.added.append(dict(fields))
        self.uploaded.append({"fields": dict(fields), "blob": blob})
        known = self._blob_hash(blob) if blob else None
        if known is not None and any(t["hash"].lower() == known for t in self.torrents):
            if self.api_version == "4":
                return httpx.Response(200, text="Ok.")
            return httpx.Response(409, text="Conflict")
        if known is not None:
            stopped = (fields.get("stopped") or fields.get("paused") or "").lower() == "true"
            self.add_torrent(
                known,
                name=fields.get("savepath", "").rsplit("/", 1)[-1] or "release",
                progress=0.0,
                state="stoppedDL" if stopped else "downloading",
                content_path=fields.get("savepath"),
                category=fields.get("category", "arc"),
            )
        if self.api_version == "4":
            return httpx.Response(200, text="Ok.")
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
        time_active: int = 0,
        num_seeds: int = 0,
        num_leechs: int = 0,
        num_complete: int = -1,
        num_incomplete: int = -1,
        report_counts: bool = True,
    ) -> None:
        """One torrent in the client's listing, as ``torrents/info`` shapes it.

        The defaults are what a *real* client says about a torrent it has just
        started: connected to nobody yet (``num_seeds``/``num_leechs`` 0, which
        it always reports), tracker not scraped yet (``num_complete``/
        ``num_incomplete`` ``-1``, which means *unknown* and must never be read
        as an empty swarm), and no time on the clock. ``time_active`` is what
        the stall rule measures, so a test about a stall says so explicitly.

        ``report_counts=False`` omits all four, which is what an older client
        that does not send them looks like.
        """
        row: dict[str, Any] = {
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
            "time_active": time_active,
        }
        if report_counts:
            row |= {
                "num_seeds": num_seeds,
                "num_leechs": num_leechs,
                "num_complete": num_complete,
                "num_incomplete": num_incomplete,
            }
        self.torrents.append(row)

    def add_files(
        self,
        info_hash: str,
        names: Sequence[str],
        *,
        size: int = 400_000_000,
        priority: int = 1,
        progress: float = 0.0,
    ) -> None:
        """Seed ``torrents/files`` for one hash: one row per name, in order.

        The defaults are what the client says about a freshly added torrent it
        has not been told anything about yet — every file selected at normal
        priority and nothing downloaded — which is precisely the state the add
        sequence's ``filePrio 0`` for every index exists to replace. A test
        wanting an odd row (no ``index``, a missing ``size``) writes
        ``stub.file_lists[hash]`` itself.
        """
        self.file_lists[info_hash.lower()] = [
            {
                "index": index,
                "name": name,
                "size": size,
                "priority": priority,
                "progress": progress,
            }
            for index, name in enumerate(names)
        ]


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
