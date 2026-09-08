"""The MyAnimeList endpoints: link, import, push, log, revert (spec §4.7).

Every route is scoped to the caller. There is no admin view of somebody else's
MAL account and no route takes a user id — a MAL link is the most personal
thing Arc stores, and the shape of the API is where that is enforced rather
than in a check somebody could forget.

**The callback is the odd one.** ``GET /api/mal/callback`` is where the
browser lands coming back from myanimelist.net, so it is reached by a top-level
navigation rather than by the client's fetch layer. Three consequences:

* It still requires a session. ``SameSite=Lax`` sends the cookie on exactly
  this kind of navigation, so the requirement costs nothing and closes the
  hole where a link could be completed by someone who is not signed in.
* It checks the session user against the user inside the ``state``. The state
  is encrypted, which proves *Arc* issued it, not that this browser is the one
  it was issued to; without this check a state pasted into a second browser
  would attach a MyAnimeList account to the wrong Arc user.
* It answers with a redirect rather than JSON, because a person is looking at
  it. Success and every kind of failure land on the same client page with a
  query parameter, so the client has one place to render both.

Nothing in this module writes to MyAnimeList. The link route hands back a URL,
the callback stores tokens, and everything else queues a job — which is what
keeps FR-M7's "one enforcement point" true: the revert below is the third and
last caller of :func:`~arc.services.mal.names.enqueue_mal_push`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.api.anime_schemas import AnimeSummary, MalSyncOut
from arc.api.deps import CurrentUser, SessionDep, SettingsDep
from arc.config import ConfigurationError, Settings
from arc.core.crypto import InvalidToken
from arc.models import (
    Anime,
    ListEntry,
    ListStatus,
    MalLink,
    MalWriteCause,
    MalWriteLog,
    MalWriteStatus,
    UpdatedBy,
)
from arc.services.mal import oauth, writelog
from arc.services.mal.client import MalApiError, needs_relink, store_tokens
from arc.services.mal.factory import client_of, oauth_client
from arc.services.mal.names import (
    enqueue_mal_import,
    enqueue_mal_push,
    enqueue_mal_push_all,
)
from arc.services.mal.sync import SKIP_DISCONNECTED, apply_change, discard_pending

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mal", tags=["mal"])

NOT_CONFIGURED = "MyAnimeList is not configured on this server"
ALREADY_LINKED = "this account is already linked to MyAnimeList"
NOT_LINKED = "this account is not linked to MyAnimeList"
NOT_FOUND = "no such write log entry"
NOT_REVERTIBLE = "this write has been superseded and can no longer be reverted"
NEWER_QUEUED = "a newer change to this field is still queued"

#: Where the client's MAL page lives, relative to ``PUBLIC_URL``. The callback
#: redirects here with either ``?linked=1`` or ``?error=<code>``.
CLIENT_PATH = "/mal"

#: Error codes the callback can put in that query string. Short, stable and
#: machine-readable: the client turns them into a sentence, so changing the
#: wording never means changing this file.
ERROR_STATE = "invalid_state"
ERROR_EXCHANGE = "exchange_failed"

#: Default and maximum rows returned by the log endpoint.
LOG_LIMIT = 50
LOG_MAX_LIMIT = 200


class MalStatusOut(BaseModel):
    """``GET /api/mal/status`` — everything the MAL page's header needs."""

    #: Whether this server has a ``MAL_CLIENT_ID`` at all. False means the
    #: link button is pointless and the client says so rather than offering it.
    configured: bool
    linked: bool
    mal_username: str | None = None
    #: When the current access token lapses. Null while the link needs
    #: re-authorising, since there is no token left to expire.
    expires_at: datetime | None = None
    last_import_at: datetime | None = None
    #: True when MyAnimeList rejected the stored refresh token: the row is
    #: still there, its credentials are not, and only the user can fix it.
    needs_relink: bool = False
    #: Changes Arc still owes MyAnimeList, and fields whose last attempt
    #: failed (:mod:`arc.services.mal.writelog`).
    pending_writes: int = 0
    failed_writes: int = 0


class MalLinkOut(BaseModel):
    """``POST /api/mal/link`` — where to send the browser."""

    authorize_url: str


class MalWriteOut(BaseModel):
    """One row of the user's write log (FR-M5)."""

    id: int
    #: Null only if the cached show has been purged out from under the log.
    anime: AnimeSummary | None = None
    field: str
    old_value: Any = None
    new_value: Any = None
    cause: MalWriteCause
    status: MalWriteStatus
    error: str | None = None
    created_at: datetime
    #: Whether ``POST /api/mal/log/{id}/revert`` would be accepted: this write
    #: succeeded and nothing has overwritten its field since.
    revertible: bool = False


class RevertOut(BaseModel):
    """``POST /api/mal/log/{id}/revert`` — what was queued."""

    anime_id: int
    field: str
    value: Any = None
    job_id: int | None = None


async def _link_of(session: AsyncSession, user_id: int) -> MalLink | None:
    return await session.get(MalLink, user_id)


@router.get("/status", response_model=MalStatusOut, summary="This user's MAL link state")
async def link_status(
    user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> MalStatusOut:
    link = await _link_of(session, user.id)
    return MalStatusOut(
        configured=bool(settings.mal_client_id),
        linked=link is not None,
        mal_username=link.mal_username if link else None,
        expires_at=link.expires_at if link else None,
        last_import_at=link.last_import_at if link else None,
        needs_relink=needs_relink(link) if link else False,
        pending_writes=await writelog.pending_count(session, user_id=user.id),
        failed_writes=await writelog.failed_count(session, user_id=user.id),
    )


@router.post(
    "/link",
    response_model=MalLinkOut,
    summary="Begin the MyAnimeList OAuth handshake (FR-M1)",
    responses={
        409: {"description": ALREADY_LINKED},
        503: {"description": NOT_CONFIGURED},
    },
)
async def begin_link(user: CurrentUser, session: SessionDep, settings: SettingsDep) -> MalLinkOut:
    """Return the URL to send the browser to.

    Nothing is stored: the verifier and the caller's id travel inside the
    encrypted ``state`` (:mod:`arc.services.mal.oauth`), so an abandoned link
    attempt leaves no row to expire.
    """
    if not settings.mal_client_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED)
    existing = await _link_of(session, user.id)
    if existing is not None and not needs_relink(existing):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ALREADY_LINKED)

    verifier = oauth.new_verifier()
    try:
        state = oauth.encode_state(
            settings, user_id=user.id, verifier=verifier, now=datetime.now(UTC)
        )
        return MalLinkOut(
            authorize_url=oauth.authorize_url(settings, state=state, verifier=verifier)
        )
    except ConfigurationError as exc:
        # No FERNET_KEY: the state cannot be sealed, so the handshake cannot
        # start. Same answer as a missing client id — the server is not set up.
        log.error("cannot start a MAL link", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED
        ) from exc


@router.get(
    "/callback",
    response_model=None,
    summary="Where MyAnimeList sends the browser back (FR-M1)",
    responses={302: {"description": "redirect to the client's MAL page"}},
)
async def callback(
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> Response:
    """Complete the handshake and queue the baseline import.

    ``error`` is what MyAnimeList sends when the user presses "Deny"; it is a
    normal outcome, not a failure, and is passed through to the client so it
    can say what happened.
    """
    if error:
        return _back(settings, error=error)
    if not code or not state:
        return _back(settings, error=ERROR_STATE)

    try:
        parsed = oauth.decode_state(settings, state)
    except (oauth.InvalidState, InvalidToken, ConfigurationError) as exc:
        log.info("MAL callback with an unusable state", extra={"error": str(exc)})
        return _back(settings, error=ERROR_STATE)

    if parsed.user_id != user.id:
        # The state was issued to somebody else. Refused rather than honoured:
        # completing it would attach a MyAnimeList account to the wrong Arc
        # user, which is the one mistake this flow must not be able to make.
        log.warning(
            "MAL callback state belongs to another user",
            extra={"session_user": user.id, "state_user": parsed.user_id},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="state is not yours")

    try:
        async with oauth_client(settings) as client:
            tokens = await client.exchange(code=code, verifier=parsed.verifier)
    except (oauth.MalOAuthError, ConfigurationError) as exc:
        log.warning("MAL token exchange failed", extra={"user_id": user.id, "error": str(exc)})
        return _back(settings, error=ERROR_EXCHANGE)

    link = await _link_of(session, user.id) or MalLink(user_id=user.id)
    store_tokens(settings, link, tokens)
    session.add(link)
    await session.flush()

    # Who this is, on MAL's side. Not fatal if it fails: the tokens are good,
    # the import will work, and a missing username is a cosmetic gap that the
    # next import does not even need to fill.
    try:
        async with client_of(settings, link) as api:
            _, username = await api.me()
        link.mal_username = username
    except MalApiError as exc:
        log.warning(
            "could not read the MAL username",
            extra={"user_id": user.id, "error": str(exc)},
        )

    await enqueue_mal_import(session, user_id=user.id)
    await session.commit()
    log.info("MyAnimeList linked", extra={"user_id": user.id, "mal_username": link.mal_username})
    return _back(settings, linked=True)


@router.delete(
    "/link",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink this account from MyAnimeList",
    responses={404: {"description": NOT_LINKED}},
)
async def unlink(user: CurrentUser, session: SessionDep) -> Response:
    """Delete the link row and its tokens.

    The write log stays, and so do the list entries and their ``mal_dirty``
    flags. The log is an audit trail of things that really happened and is not
    the user's to lose by pressing a button (FR-M5); the dirty flags are the
    truthful statement that Arc holds changes MyAnimeList has not been told
    about, which re-linking should push rather than forget.

    The **queued** rows do not stay queued. There is no longer an account to
    send them to and no job that could: ``mal_push`` would find no link and
    abandon them, and until something did, the sync page would go on counting
    changes as owed that nothing was coming back for. They close ``skipped``
    with the reason, which is the honest reading — nothing was attempted, and
    a decision was made. Rows that had already *failed* are left exactly as
    they are: they record an attempt that really happened, and unlinking is not
    a licence to rewrite it.
    """
    link = await _link_of(session, user.id)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_LINKED)
    closed = await discard_pending(session, user_id=user.id, reason=SKIP_DISCONNECTED)
    await session.delete(link)
    await session.commit()
    log.info("MyAnimeList unlinked", extra={"user_id": user.id, "abandoned": closed})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/import",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-import this user's MyAnimeList list now (FR-M2)",
    responses={404: {"description": NOT_LINKED}},
)
async def import_now(user: CurrentUser, session: SessionDep) -> dict[str, int]:
    job = await _queue(session, user_id=user.id, importing=True)
    return {"job_id": job}


@router.post(
    "/push",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send every pending change to MyAnimeList now (FR-M6)",
    responses={404: {"description": NOT_LINKED}},
)
async def push_now(user: CurrentUser, session: SessionDep) -> dict[str, int]:
    job = await _queue(session, user_id=user.id, importing=False)
    return {"job_id": job}


async def _queue(session: AsyncSession, *, user_id: int, importing: bool) -> int:
    """Queue an import or a full push for a linked user; 404 if there is none."""
    if await session.get(MalLink, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_LINKED)
    job = (
        await enqueue_mal_import(session, user_id=user_id)
        if importing
        else await enqueue_mal_push_all(session, user_id=user_id)
    )
    await session.commit()
    return job.id


@router.get(
    "/log",
    response_model=list[MalWriteOut],
    summary="Every write Arc made to MyAnimeList for this user (FR-M5)",
)
async def write_log(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=LOG_MAX_LIMIT)] = LOG_LIMIT,
    status_filter: Annotated[MalWriteStatus | None, Query(alias="status")] = None,
) -> list[MalWriteOut]:
    rows = await writelog.recent(session, user_id=user.id, limit=limit, status=status_filter)
    if not rows:
        return []
    anime_ids = {row.anime_id for row in rows}
    shows = {
        anime.id: anime
        for anime in (await session.scalars(select(Anime).where(Anime.id.in_(anime_ids)))).all()
    }
    # One query for the whole page rather than one existence check per row:
    # ``revertible`` is "this is the newest successful write to its field", and
    # the newest id per (anime, field) answers that for fifty rows at once.
    newest = await writelog.latest_ok_ids(session, user_id=user.id, anime_ids=list(anime_ids))
    # …and one more for the fields with a change still waiting to be sent. A
    # queued row is a newer intention than the newest *successful* write, so
    # offering "revert" on that write would let a click silently supersede a
    # change the user made a moment ago and is still waiting for.
    queued = await writelog.pending_fields(session, user_id=user.id, anime_ids=list(anime_ids))
    statuses = {
        anime_id: entry_status
        for anime_id, entry_status in (
            await session.execute(
                select(ListEntry.anime_id, ListEntry.status).where(
                    ListEntry.user_id == user.id, ListEntry.anime_id.in_(anime_ids)
                )
            )
        ).all()
    }
    return [
        MalWriteOut(
            id=row.id,
            anime=(
                AnimeSummary.from_anime(shows[row.anime_id], statuses.get(row.anime_id))
                if row.anime_id in shows
                else None
            ),
            field=row.field,
            old_value=row.old_value,
            new_value=row.new_value,
            cause=row.cause,
            status=row.status,
            error=row.error,
            created_at=row.created_at,
            revertible=(
                row.status is MalWriteStatus.OK
                and newest.get((row.anime_id, row.field)) == row.id
                and (row.anime_id, row.field) not in queued
            ),
        )
        for row in rows
    ]


@router.post(
    "/log/{log_id}/revert",
    response_model=RevertOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Put a field back to the value this write replaced (FR-M5)",
    responses={
        404: {"description": NOT_FOUND},
        409: {"description": f"{NOT_REVERTIBLE}, or {NEWER_QUEUED}"},
    },
)
async def revert(log_id: int, user: CurrentUser, session: SessionDep) -> RevertOut:
    """Write ``old_value`` back locally and queue the push that carries it up.

    A revert is a change like any other: it sets ``updated_by = arc`` and
    ``mal_dirty``, queues a ``mal_push`` with cause ``revert``, and produces
    its own log row when that push runs. So the log always reads as a history
    rather than as a history with holes in it, and reverting the revert is
    simply the newest row becoming revertible in turn.

    Refused while the field has a **queued** row. "This is the newest
    successful write" is not "this is the newest thing the user asked for": a
    change made while the last one was still being retried is newer than any
    of them, and a revert on top of it would replace it without ever saying
    so. The log's ``revertible`` flag is computed the same way, so the button
    is gone before the 409 is needed.
    """
    row = await session.get(MalWriteLog, log_id)
    if row is None or row.user_id != user.id:
        # The same answer for "no such row" and "not yours": a 403 here would
        # tell a caller which log ids exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    if await session.get(MalLink, user.id) is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_LINKED)
    if not await writelog.is_revertible(session, row):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_REVERTIBLE)
    if await writelog.has_pending_field(
        session, user_id=user.id, anime_id=row.anime_id, field=row.field
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NEWER_QUEUED)

    entry = await session.get(ListEntry, (user.id, row.anime_id))
    if entry is None:
        entry = _recreate(user_id=user.id, row=row)
        if entry is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_REVERTIBLE)
        session.add(entry)
        was: Any = None
    else:
        was = _current(entry, row.field)

    try:
        apply_change(entry, field=row.field, value=row.old_value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    entry.updated_by = UpdatedBy.ARC
    entry.mal_dirty = True
    entry.updated_at = datetime.now(UTC)
    await session.flush()
    # The third of FR-M7's user-originated events, and the third and last
    # caller allowed to queue a write. The row carries ``revert`` so the push
    # knows that lowering progress or clearing a score is exactly what was
    # asked for — a guard that applies to an automatic event does not apply
    # to a button the user pressed.
    await writelog.record_pending(
        session,
        user_id=user.id,
        anime_id=row.anime_id,
        field=row.field,
        old_value=was,
        new_value=row.old_value,
        cause=MalWriteCause.REVERT,
    )
    job = await enqueue_mal_push(session, user_id=user.id, anime_id=row.anime_id)
    await session.commit()
    return RevertOut(
        anime_id=row.anime_id,
        field=row.field,
        value=row.old_value,
        job_id=job.id if job else None,
    )


def _current(entry: ListEntry, field: str) -> Any:
    """What the entry holds for one field name, before the revert replaces it."""
    if field == writelog.FIELD_STATUS:
        return entry.status.value
    if field == writelog.FIELD_SCORE:
        return entry.score
    return entry.progress


def _recreate(*, user_id: int, row: MalWriteLog) -> ListEntry | None:
    """The entry a reverted *removal* puts back, or ``None`` if it cannot.

    Only a status row with a real ``old_value`` describes a whole entry. A
    score or a progress row on a show that is no longer on the list says
    nothing about what status it should come back as, and inventing one would
    be Arc making a claim the user did not.
    """
    if row.field != writelog.FIELD_STATUS or row.old_value is None:
        return None
    try:
        return ListEntry(
            user_id=user_id,
            anime_id=row.anime_id,
            status=ListStatus(str(row.old_value)),
            progress=0,
        )
    except ValueError:  # pragma: no cover - the column only ever held a status
        return None


def _back(settings: Settings, *, linked: bool = False, error: str | None = None) -> Response:
    """Send the browser back to the client's MAL page with the outcome.

    The error is url-encoded because it is not always one of the two codes
    above: MyAnimeList's own ``?error=`` comes back verbatim when the user
    presses "Deny", and a value with an ``&`` or a space in it would otherwise
    become a second query parameter — or a header nobody meant to send.
    """
    query = "linked=1" if linked else urlencode({"error": error or ERROR_STATE})
    return RedirectResponse(
        url=f"{settings.public_url.rstrip('/')}{CLIENT_PATH}?{query}",
        status_code=status.HTTP_302_FOUND,
    )


__all__ = [
    "ALREADY_LINKED",
    "CLIENT_PATH",
    "ERROR_EXCHANGE",
    "ERROR_STATE",
    "LOG_LIMIT",
    "LOG_MAX_LIMIT",
    "NEWER_QUEUED",
    "NOT_CONFIGURED",
    "NOT_FOUND",
    "NOT_LINKED",
    "NOT_REVERTIBLE",
    "MalLinkOut",
    "MalStatusOut",
    "MalSyncOut",
    "MalWriteOut",
    "RevertOut",
    "router",
]
