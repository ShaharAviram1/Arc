"""The authenticated half of MyAnimeList: one user's list, read and written.

:mod:`arc.services.mal.catalog` is the read-only catalogue fallback and cannot
write anything; this is the module that can, and it is the only one. Every
request it makes carries a user's OAuth bearer token, and every one of them
goes through :meth:`MalClient._request`, which is where token refresh lives —
so no caller ever has to think about expiry (FR-M1).

**Vocabulary.** MAL says ``plan_to_watch``; Arc's :class:`ListStatus` says
``planned``. The other four names coincide. The maps are at the top and are
the only place either spelling appears.

**A score of zero is not a score.** MAL uses ``0`` for "not rated", and Arc
uses ``NULL``; the translation happens here so nothing above ever writes a
zero into ``list_entries.score`` and calls it a rating.

**What "needs relink" means.** ``mal_links`` has no status column, and adding
one for a single boolean would be a migration for something two existing
columns can already say. So a link whose refresh token MyAnimeList has
rejected is stored with **empty** ``access_token_enc`` and
``refresh_token_enc``: the row stays (the username, the last import time and
the audit trail are still true and still the user's) but there is nothing left
to authenticate with, which is exactly the state the user must fix.
:func:`needs_relink` is the one reader of that convention and
``GET /api/mal/status`` is where it surfaces. Writing empty strings also
destroys the dead tokens, which is the right thing to do with a credential
MyAnimeList has already disowned.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.config import Settings
from arc.core.crypto import InvalidToken, decrypt, encrypt
from arc.models import ListStatus, MalLink
from arc.services.mal.oauth import (
    REFRESH_MARGIN,
    MalOAuthClient,
    MalOAuthError,
    MalTokens,
)

log = logging.getLogger(__name__)

#: MAL's list status → Arc's. The four that coincide are written out anyway:
#: a dict is what the reverse map is built from, and half a map would be worse
#: than none.
MAL_TO_STATUS: Mapping[str, ListStatus] = {
    "watching": ListStatus.WATCHING,
    "completed": ListStatus.COMPLETED,
    "on_hold": ListStatus.ON_HOLD,
    "dropped": ListStatus.DROPPED,
    "plan_to_watch": ListStatus.PLANNED,
}

#: …and back. Built from the map above so the two cannot drift.
STATUS_TO_MAL: Mapping[ListStatus, str] = {value: key for key, value in MAL_TO_STATUS.items()}

#: Fields one list page asks for. ``num_episodes`` is on the *node* (the show),
#: the rest are on ``list_status`` (the user's row).
LIST_FIELDS = "list_status{status,score,num_episodes_watched,updated_at},num_episodes"

#: Entries per page. MAL's ceiling for this endpoint is 1000, and a full list
#: is usually one request.
LIST_LIMIT = 1000

#: Refuse to page forever. Twenty pages is twenty thousand entries — an order
#: of magnitude past the largest real list — so hitting this means MAL's
#: ``paging.next`` is looping, not that somebody is a completionist.
MAX_LIST_PAGES = 20

#: How long one authenticated request may take.
TIMEOUT_SECONDS = 20.0


class MalApiError(RuntimeError):
    """A MyAnimeList request that did not succeed.

    ``retryable`` is what the job runner acts on: a timeout or a 5xx comes
    back later, a 400 does not.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class MalNotLinked(MalApiError):
    """This user has no usable MyAnimeList credentials.

    Either they never linked, or the refresh token has been rejected and the
    link is in the "needs relink" state described in the module docstring.
    Never retryable: only the user can fix it.
    """


@dataclass(frozen=True, slots=True)
class MalStatus:
    """One ``my_list_status`` object: the user's state for one show on MAL."""

    status: ListStatus | None
    score: int | None
    progress: int
    updated_at: datetime | None

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any] | None) -> Self | None:
        """``None`` when the show is not on the user's MAL list at all."""
        if not raw:
            return None
        score = raw.get("score")
        return cls(
            status=MAL_TO_STATUS.get(str(raw.get("status") or "")),
            # Zero is MAL's "unrated", not a rating of nothing (FR-M2).
            score=int(score) if score else None,
            progress=int(raw.get("num_episodes_watched") or 0),
            updated_at=_parse_time(raw.get("updated_at")),
        )


@dataclass(frozen=True, slots=True)
class MalListEntry:
    """One row of ``users/@me/animelist``: a show and the user's state for it."""

    mal_id: int
    title: str | None
    episodes: int | None
    status: MalStatus


def _parse_time(value: object) -> datetime | None:
    """MAL's ISO-8601 ``updated_at`` as an aware datetime, or ``None``.

    Naive strings are read as UTC. MAL sends an offset, but a value Arc cannot
    place is far better treated as UTC than compared as if it were local time:
    §5.5's conflict rule is a comparison of two timestamps, and a silent
    offset would decide it wrongly.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def needs_relink(link: MalLink) -> bool:
    """Whether this link has lost its credentials (see the module docstring)."""
    return not link.access_token_enc or not link.refresh_token_enc


def store_tokens(settings: Settings, link: MalLink, tokens: MalTokens) -> None:
    """Write a token pair onto a link row, encrypted (architecture.md §7)."""
    link.access_token_enc = encrypt(settings, tokens.access_token)
    link.refresh_token_enc = encrypt(settings, tokens.refresh_token)
    link.expires_at = tokens.expires_at


def mark_needs_relink(link: MalLink) -> None:
    """Drop the credentials MyAnimeList has disowned, keeping the link row."""
    link.access_token_enc = ""
    link.refresh_token_enc = ""
    link.expires_at = None


class MalClient:
    """One user's authenticated MyAnimeList session.

    Holds the ORM ``link`` row and mutates it when a refresh happens; the
    caller's transaction is what makes the new tokens durable, so a refresh
    that is rolled back is simply a refresh that did not happen (MAL keeps the
    old refresh token valid until the new one is used, so the next attempt
    succeeds either way).

    It also holds that row's **session**, when it has one, and that is not for
    convenience: two jobs for the same user can run at the same moment (a push
    for one show, an import, the push-all button), and without a lock both
    would see the same expiring token and both refresh. MyAnimeList rotates the
    refresh token on use, so the second refresh presents one MAL has already
    retired — and the loser marks the link "needs re-authorising" for a link
    that is perfectly healthy. :meth:`refresh` therefore takes a row lock on
    ``mal_links`` and re-reads before deciding, so the second job finds the
    first's tokens and simply uses them.
    """

    def __init__(
        self,
        settings: Settings,
        link: MalLink,
        *,
        session: AsyncSession | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
        now: datetime | None = None,
    ) -> None:
        self.settings = settings
        self.link = link
        self.url = settings.mal_api_url.rstrip("/")
        self._session = session
        self._now = now
        self._oauth = MalOAuthClient(settings, transport=transport)
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "arc/0.1 (self-hosted)"},
        )

    @classmethod
    async def open(
        cls,
        settings: Settings,
        session: AsyncSession,
        *,
        user_id: int,
        transport: httpx.AsyncBaseTransport | None = None,
        now: datetime | None = None,
    ) -> Self:
        """A client for ``user_id``, or :class:`MalNotLinked`."""
        link = await session.get(MalLink, user_id)
        if link is None:
            raise MalNotLinked("this account is not linked to MyAnimeList")
        if needs_relink(link):
            raise MalNotLinked("the MyAnimeList link needs to be re-authorised")
        return cls(settings, link, session=session, transport=transport, now=now)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._oauth.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _clock(self) -> datetime:
        return self._now or datetime.now(UTC)

    # --- Tokens ---------------------------------------------------------

    def _expiring(self) -> bool:
        """Whether the stored access token is gone, or nearly.

        A missing ``expires_at`` counts as expired: it means Arc does not know
        when the token dies, and refreshing costs one request while guessing
        wrong costs a failed write.
        """
        expires_at = self.link.expires_at
        return expires_at is None or expires_at - REFRESH_MARGIN <= self._clock()

    async def access_token(self) -> str:
        """The current access token, refreshed first if it is about to lapse."""
        if needs_relink(self.link):
            raise MalNotLinked("the MyAnimeList link needs to be re-authorised")
        if self._expiring():
            await self.refresh()
        try:
            return decrypt(self.settings, self.link.access_token_enc)
        except InvalidToken as exc:
            # The Fernet key changed under a stored token. Nothing can read it
            # again, so the honest answer is the same as a rejected refresh.
            mark_needs_relink(self.link)
            raise MalNotLinked("stored MyAnimeList tokens could not be decrypted") from exc

    async def _reread(self, *, lock: bool) -> bool:
        """Re-read the ``mal_links`` row, optionally locking it. ``False`` with no session.

        ``populate_existing`` is what makes this a *re-read*: the attributes of
        ``self.link`` are replaced by what is committed now, rather than the
        identity map handing back the snapshot this client opened with. With
        ``lock``, ``SELECT … FOR UPDATE`` holds the row for the rest of the
        caller's transaction, so a second job blocks here and then sees the
        first one's tokens instead of refreshing again.
        """
        if self._session is None:
            return False
        statement = select(MalLink).where(MalLink.user_id == self.link.user_id)
        if lock:
            statement = statement.with_for_update()
        await self._session.execute(statement.execution_options(populate_existing=True))
        return True

    async def _lock_link(self) -> bool:
        """Take the row lock. Named separately because tests replace it."""
        return await self._reread(lock=True)

    async def refresh(self, *, _retry: bool = True) -> None:
        """Exchange the refresh token for a new pair (FR-M1).

        Serialised on the ``mal_links`` row: whoever gets the lock refreshes,
        and whoever waited re-reads and finds there is nothing left to do. Two
        concurrent pushes therefore cost one refresh, not two — and not one
        refresh plus a spurious "needs re-authorising", which is what a second
        job presenting an already-rotated refresh token would earn.

        A *retryable* failure (MAL down) leaves the link alone and raises: the
        job retries later with the credentials intact. A refusal — MAL saying
        the refresh token is no good — is terminal *unless* the stored token
        has changed underneath, which means somebody else refreshed between
        this call's read and its answer; then it is tried once more with the
        token that is actually current before anybody is asked to link again.
        """
        locked = await self._lock_link()
        if locked and not self._expiring():
            # Another job refreshed while this one waited for the lock. Its
            # tokens are now ours: nothing to do, and nothing to send.
            return
        if needs_relink(self.link):
            raise MalNotLinked("the MyAnimeList link needs to be re-authorised")

        try:
            refresh_token = decrypt(self.settings, self.link.refresh_token_enc)
        except InvalidToken as exc:
            mark_needs_relink(self.link)
            raise MalNotLinked("stored MyAnimeList tokens could not be decrypted") from exc

        try:
            tokens = await self._oauth.refresh(refresh_token=refresh_token, now=self._clock())
        except MalOAuthError as exc:
            if exc.retryable:
                raise MalApiError(f"token refresh failed: {exc}", retryable=True) from exc
            if _retry and await self._token_rotated(refresh_token):
                log.info(
                    "MyAnimeList refused a stale refresh token; another job had rotated it",
                    extra={"user_id": self.link.user_id},
                )
                await self.refresh(_retry=False)
                return
            mark_needs_relink(self.link)
            log.warning(
                "MyAnimeList refused a token refresh; the link needs re-authorising",
                extra={"user_id": self.link.user_id, "error": str(exc)},
            )
            raise MalNotLinked(f"MyAnimeList rejected the stored credentials: {exc}") from exc
        store_tokens(self.settings, self.link, tokens)

    async def _token_rotated(self, presented: str) -> bool:
        """Whether the stored refresh token has changed since ``presented``.

        The belt to the row lock's braces. While the lock is held nothing can
        rotate the token underneath, so this reads "no" and costs one statement
        on a path that was about to fail anyway; it earns its keep on a refresh
        that could not take the lock, where a refusal caused by a token
        somebody else has already spent would otherwise cost the user a
        re-link they do not need.
        """
        if not await self._reread(lock=False):
            return False
        if needs_relink(self.link):
            return False
        try:
            return decrypt(self.settings, self.link.refresh_token_enc) != presented
        except InvalidToken:  # pragma: no cover - answered by the caller's own path
            return False

    # --- Requests -------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        absolute: str | None = None,
    ) -> httpx.Response:
        """One authenticated call, retried once after a refresh on a 401.

        The 401 retry is not belt-and-braces on top of the pre-emptive refresh
        in :meth:`access_token`: a token can be revoked from MyAnimeList's own
        settings page long before it expires, and that is indistinguishable
        from a clock skew until the answer comes back.
        """
        target = absolute or f"{self.url}{path}"
        for attempt in (1, 2):
            token = await self.access_token()
            headers = {"Authorization": f"Bearer {token}"}
            try:
                response = await self._http.request(
                    method, target, params=params, data=data, headers=headers
                )
            except httpx.HTTPError as exc:
                raise MalApiError(f"{type(exc).__name__}: {exc}", retryable=True) from exc

            if response.status_code == httpx.codes.UNAUTHORIZED and attempt == 1:
                await self.refresh()
                continue
            return response
        raise MalApiError("MyAnimeList kept refusing the token", retryable=False)

    @staticmethod
    def _ok(response: httpx.Response, *, what: str) -> Any:
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise MalApiError(f"{what}: HTTP {response.status_code}", retryable=True)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise MalApiError(f"{what}: rate limited", retryable=True)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise MalApiError(f"{what}: HTTP {response.status_code} {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise MalApiError(f"{what}: response was not JSON") from exc

    # --- The API --------------------------------------------------------

    async def me(self) -> tuple[int | None, str | None]:
        """``GET /users/@me`` → the account's id and username."""
        payload = self._ok(await self._request("GET", "/users/@me"), what="whoami")
        if not isinstance(payload, dict):
            raise MalApiError("whoami: response was not a JSON object")
        raw_id = payload.get("id")
        name = payload.get("name")
        return (int(raw_id) if raw_id is not None else None, str(name) if name else None)

    async def animelist(self) -> list[MalListEntry]:
        """The user's whole list, paged (FR-M2).

        ``nsfw=true`` because Arc is asking for *this user's own list*: an
        entry they put there themselves and cannot see back is a silent hole
        in the import, and the import is the baseline everything else is
        compared against.

        Paging follows MAL's own ``paging.next`` URL rather than doing offset
        arithmetic, but only after checking it points at the configured API
        host — a ``next`` pointing elsewhere would send a bearer token to
        whoever asked for it.
        """
        entries: list[MalListEntry] = []
        params: dict[str, Any] | None = {
            "fields": LIST_FIELDS,
            "limit": LIST_LIMIT,
            "offset": 0,
            "nsfw": "true",
        }
        target: str | None = None
        for _page in range(MAX_LIST_PAGES):
            response = await self._request(
                "GET",
                "/users/@me/animelist",
                params=params,
                absolute=target,
            )
            payload = self._ok(response, what="animelist")
            if not isinstance(payload, dict):
                raise MalApiError("animelist: response was not a JSON object")
            entries.extend(_parse_list_page(payload))
            nxt = (payload.get("paging") or {}).get("next")
            if not nxt or not _same_host(str(nxt), self.url):
                return entries
            target, params = str(nxt), None
        log.warning(
            "MyAnimeList list paging hit its page cap",
            extra={"user_id": self.link.user_id, "pages": MAX_LIST_PAGES},
        )
        return entries

    async def my_list_status(self, mal_id: int) -> MalStatus | None:
        """The user's current entry for one show, or ``None`` if it has none.

        Read before every write, so the write log records the value Arc
        actually replaced rather than the one it last saw (FR-M5).
        """
        response = await self._request(
            "GET", f"/anime/{mal_id}", params={"fields": "my_list_status,num_episodes"}
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        payload = self._ok(response, what=f"anime {mal_id}")
        if not isinstance(payload, dict):
            raise MalApiError(f"anime {mal_id}: response was not a JSON object")
        raw = payload.get("my_list_status")
        return MalStatus.from_payload(raw if isinstance(raw, dict) else None)

    async def update_list_status(
        self,
        mal_id: int,
        *,
        status: ListStatus | None = None,
        score: int | None = None,
        progress: int | None = None,
    ) -> MalStatus | None:
        """``PATCH /anime/{id}/my_list_status`` with only the changed fields.

        An empty ``form`` is a programming error rather than a no-op request:
        the caller worked out the diff, and sending a PATCH with no fields
        would be a write nobody asked for.
        """
        form: dict[str, Any] = {}
        if status is not None:
            form["status"] = STATUS_TO_MAL[status]
        if score is not None:
            form["score"] = int(score)
        if progress is not None:
            form["num_watched_episodes"] = int(progress)
        if not form:
            raise ValueError("update_list_status was given nothing to change")
        response = await self._request("PATCH", f"/anime/{mal_id}/my_list_status", data=form)
        payload = self._ok(response, what=f"update {mal_id}")
        return MalStatus.from_payload(payload if isinstance(payload, dict) else None)

    async def delete_list_status(self, mal_id: int) -> bool:
        """Remove the show from the user's MAL list; ``True`` if it was there.

        A 404 is success, not a failure: it means the entry is already gone,
        which is the state the caller asked for. That is what makes the delete
        idempotent, and therefore safe for the queue to retry.
        """
        response = await self._request("DELETE", f"/anime/{mal_id}/my_list_status")
        if response.status_code == httpx.codes.NOT_FOUND:
            return False
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise MalApiError(f"delete {mal_id}: HTTP {response.status_code}", retryable=True)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            # Same as :meth:`_ok`: being told to slow down is not being told no.
            raise MalApiError(f"delete {mal_id}: rate limited", retryable=True)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise MalApiError(f"delete {mal_id}: HTTP {response.status_code}")
        return True


def _parse_list_page(payload: Mapping[str, Any]) -> list[MalListEntry]:
    """One ``animelist`` page into entries, skipping anything malformed."""
    out: list[MalListEntry] = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        node = row.get("node")
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        status = MalStatus.from_payload(row.get("list_status"))
        if status is None or status.status is None:
            # An entry with no list status is not on the list in any sense
            # Arc can import; it is a node MAL echoed back.
            continue
        out.append(
            MalListEntry(
                mal_id=int(node["id"]),
                title=str(node["title"]) if node.get("title") else None,
                episodes=int(node["num_episodes"]) if node.get("num_episodes") else None,
                status=status,
            )
        )
    return out


def _same_host(url: str, base: str) -> bool:
    """Whether ``url`` is on the same origin as the configured API base."""
    return httpx.URL(url).netloc == httpx.URL(base).netloc


__all__ = [
    "LIST_FIELDS",
    "LIST_LIMIT",
    "MAL_TO_STATUS",
    "MAX_LIST_PAGES",
    "STATUS_TO_MAL",
    "TIMEOUT_SECONDS",
    "MalApiError",
    "MalClient",
    "MalListEntry",
    "MalNotLinked",
    "MalStatus",
    "mark_needs_relink",
    "needs_relink",
    "store_tokens",
]
