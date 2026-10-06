"""Authenticated HLS and episode downloads (FR-S1, FR-S7, architecture §5.4).

Four routes, mounted at ``/media`` and deliberately **not** under ``/api``:

* ``GET /media/{episode_id}/index.m3u8``
* ``GET /media/{episode_id}/episode.mp4`` — the whole episode as one file
* ``GET /media/{episode_id}/offline.mp4`` — the small offline copy (FR-P6), a
  real file served exactly like a segment, with the copy's own access rule
  (:func:`~arc.services.media.copies.may_fetch_copy`)
* ``GET /media/{episode_id}/{init.mp4 | seg_NNNNN.m4s}``

They are outside ``/api`` because they are not part of the JSON API — nothing
here answers with a document, hls.js asks for them by URL rather than through
the client's query layer, and Caddy proxies the prefix as a unit
(architecture.md §5.4). They still take the same session dependency as every
other route: media behind auth is a non-negotiable (spec §7), and an
unauthenticated request gets the same 401 JSON as anywhere else rather than a
redirect a media element could not follow. The CSRF middleware is untouched by
this module — it guards ``/api/`` and only unsafe methods, and both routes are
``GET``.

**The download is the same files, concatenated.** ``episode.mp4`` is
``init.mp4`` followed by every segment the playlist names, streamed back to
back with no ffmpeg and no temporary file — fMP4 makes that a valid MP4 on its
own (:mod:`arc.services.media.download` has the reasoning and the pure
arithmetic). It answers ranges, ``If-Range`` and ``If-None-Match`` itself,
against an ETag over every part, and refuses the demo account with a 403.
Every part it sends has passed the same name, symlink and confinement checks
as a segment request, described next.

**Paths are derived from the id and a closed vocabulary of names.** The
episode id decides the directory (:func:`~arc.services.media.names.
output_dir_for`) and the file name has to match :data:`SEGMENT_PATTERN`
exactly — ``init.mp4`` or ``seg_`` plus five digits plus ``.m4s``. Anything
else is a 422 from FastAPI's own path validation before this module runs, so
there is no point at which a user-supplied string is joined onto a directory
and hoped about. ``..``, encoded or not, is simply not one of the names.

**The name is not the whole of it, because the filesystem has its own
opinions.** A closed vocabulary settles what a *request* may say; it settles
nothing about what the bytes at that name turn out to be. ``renditions/697/
seg_00001.m4s`` is a name this router will serve, and it is also a name a
symbolic link can carry — and a symlink is resolved by the kernel, under the
server's uid, long after every check on the string has passed. Anything that
can write inside ``DATA_DIR`` (a broken transcode, an unpacked archive, a
container sharing the volume, an operator's stray ``ln -s``) could therefore
turn a segment request into ``/etc/hosts`` or into another user's file.

So :func:`_serve` refuses two things beyond the name. It refuses a path whose
resolved location is not inside the rendition directory — both sides resolved,
so a ``DATA_DIR`` that itself lives under a link still compares equal — and it
refuses a symlink outright, by ``lstat``, before anything follows it. Outright
because the encoder writes plain files and nothing else: there is no rendition
in which a link is legitimate, so "is it a link?" is a complete answer rather
than a heuristic. :func:`_ready_dir` asks the same of the episode directory,
which is the other half of the same hole.

**No playlist rewriting is needed.** ffmpeg writes the segment URIs relative
(``seg_00000.m4s``, and ``#EXT-X-MAP:URI="init.mp4"``), so a player resolving
them against ``/media/{id}/index.m3u8`` lands back on this router by
construction. §5.4's "playlists are rewritten so segment URLs stay under
/media/…" is satisfied by the encoder never writing an absolute one; a rewrite
pass here would be a second place for that to be true.

**Range requests are Starlette's.** ``FileResponse`` in the installed version
parses ``Range``, answers 206 with ``Content-Range``, and 416 with
``bytes */<size>`` for an unsatisfiable one. What it does *not* do is
conditional requests, so :func:`_serve` computes the ``ETag`` and answers 304
itself. The ETag matters more than it looks: a segment's name is stable for
the life of a rendition, but ``force=true`` re-encodes an episode into the
*same* names (FR-P5), so "immutable" is a promise about a rendition rather
than about a URL and the validator is what keeps a year-long cache honest
across a re-encode.

Starlette writes its two range errors — 400 for a malformed header, 416 for an
unsatisfiable range — as ``text/plain``, from inside ``FileResponse.__call__``
rather than by raising, so no exception handler can reach them.
:class:`_JSONRangeErrors` intercepts the ASGI messages instead: every route in
Arc answers a refusal with ``{"detail": …}``, and a client that has to parse
one body for a 404 and sniff another for a 416 is a client with a bug waiting
in it.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import formatdate
from pathlib import Path
from typing import IO, Annotated, Any, Final, NoReturn

import anyio
import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import or_, update
from starlette.datastructures import Headers
from starlette.types import Message, Receive, Scope, Send

from arc.api.deps import CurrentUser, EpisodeId, SessionDep, SettingsDep, get_current_user
from arc.models import Anime, Episode, EpisodeState, OfflineCopy, OfflineCopyState
from arc.services.catalog import preferred_title
from arc.services.media.copies import may_fetch_copy, served_recently, touch_interval
from arc.services.media.download import (
    MAX_PLAYLIST_BYTES,
    PART_NAME_PATTERN,
    ByteSpan,
    PartRead,
    PlaylistError,
    RangeMalformed,
    RangeNotSatisfiable,
    content_disposition,
    download_etag,
    if_range_matches,
    parse_range,
    playlist_parts,
    slice_parts,
    stat_etag,
)
from arc.services.media.names import offline_path_for, output_dir_for
from arc.services.media.plan import PLAYLIST_NAME

log = logging.getLogger(__name__)

#: The session dependency is declared on the router rather than as a parameter
#: on each handler. Media behind auth is a non-negotiable (spec §7), and a
#: route added to this file later must not be able to forget it — which a
#: handler whose only use of the user is to satisfy the annotation invites,
#: because it reads like an unused argument and gets tidied away.
router = APIRouter(prefix="/media", tags=["media"], dependencies=[Depends(get_current_user)])

#: The only file names a caller may ask for. ffmpeg's HLS muxer writes exactly
#: these (``hls_fmp4_init_filename`` and ``hls_segment_filename`` in
#: :mod:`arc.services.media.plan`), so the vocabulary is closed and can be
#: enforced by the router rather than by a check somebody has to remember.
#:
#: Anchored with ``\A`` and ``\z`` rather than ``^`` and ``$``, because ``$``
#: also matches *before* a trailing newline and a path parameter is percent
#: decoded before it gets here — ``seg_00000.m4s%0A`` would otherwise pass a
#: check that reads as if it could not. ``\z`` and not Python's ``\Z``: this
#: pattern is compiled by pydantic-core, whose engine is the Rust ``regex``
#: crate, and the two spell end-of-input differently. Switching pydantic to
#: ``python-re`` would raise here at import rather than quietly widen the
#: pattern, which is the right way round for a check that guards the disk.
#:
#: The constant itself lives in :mod:`arc.services.media.download`, which
#: checks every name a playlist hands the whole-episode download against the
#: same vocabulary — one definition, so the two cannot drift apart.
SEGMENT_PATTERN: Final[str] = PART_NAME_PATTERN

#: Apple's type for an HLS playlist. ``application/x-mpegURL`` is the older
#: spelling and hls.js accepts either; this is the one RFC 8216 registers.
PLAYLIST_TYPE: Final[str] = "application/vnd.apple.mpegurl"

#: fMP4 segments and the init segment are both MP4.
SEGMENT_TYPE: Final[str] = "video/mp4"

#: A year, and ``immutable`` so a browser does not even revalidate on reload.
#: ``private`` throughout: these responses are per-user authorised and must
#: never be held by a shared cache (Caddy proxies them; nothing else may).
SEGMENT_CACHE: Final[str] = "private, max-age=31536000, immutable"

#: The playlist is small, is the one file a re-encode changes the *content* of,
#: and is where a player discovers everything else. ``no-cache`` means "hold a
#: copy but revalidate", which with the ETag below costs a 304.
PLAYLIST_CACHE: Final[str] = "private, no-cache"

#: One message for every miss — an unknown episode, one that is not ``ready``,
#: a rendition directory that has been swept away. Which of the three it is
#: says something about another user's library, and the answer to all three is
#: the same to this caller anyway.
NOT_FOUND: Final[str] = "not found"

#: The two statuses Starlette answers a bad ``Range`` with, and what Arc says
#: instead. Both are about the header rather than about the file, so neither
#: leaks anything a 200 would not have.
RANGE_ERRORS: Final[dict[int, str]] = {
    status.HTTP_400_BAD_REQUEST: "range header is malformed",
    status.HTTP_416_RANGE_NOT_SATISFIABLE: "that range is not inside the file",
}


#: The whole-episode download's name (FR-S7). Not in :data:`SEGMENT_PATTERN`,
#: so it can never be mistaken for a part, and registered before the segment
#: route so the literal wins the match.
DOWNLOAD_NAME: Final[str] = "episode.mp4"

#: Revalidate rather than reuse: the file is a view over a rendition that a
#: ``force`` re-encode can replace under the same URL, and the ETag makes
#: revalidation a 304. ``private`` for the same reason as everything else here.
DOWNLOAD_CACHE: Final[str] = "private, no-cache"

#: How much of a part one read takes. Bounded, so a download costs one chunk of
#: memory however long the episode is; a megabyte is about one segment.
DOWNLOAD_CHUNK: Final[int] = 1 << 20

#: The demo account may watch but not take files away (owner, 2026-10-04).
DEMO_REFUSED: Final[str] = "downloads are turned off for the demo account"

#: The small offline copy's name (FR-P6). A literal like :data:`DOWNLOAD_NAME`,
#: registered before the segment route so it wins the match, and outside the
#: segment vocabulary so it is never mistaken for a part.
OFFLINE_NAME: Final[str] = "offline.mp4"


def playlist_url(episode_id: int) -> str:
    """Where a client points hls.js for one episode.

    Here rather than in the schema that sends it, so that the URL a response
    promises and the route that answers it are written in one file and move
    together.
    """
    return f"{router.prefix}/{episode_id}/{PLAYLIST_NAME}"


def download_url(episode_id: int) -> str:
    """Where a client downloads one ready episode as a single MP4 (FR-S7).

    Beside :func:`playlist_url` for the same reason: the URL the show page is
    sent and the route that answers it live in one file.
    """
    return f"{router.prefix}/{episode_id}/{DOWNLOAD_NAME}"


def offline_url(episode_id: int) -> str:
    """Where a client downloads one episode's small offline copy (FR-P6)."""
    return f"{router.prefix}/{episode_id}/{OFFLINE_NAME}"


def _miss() -> NoReturn:
    """The one answer to every reason this router will not serve a file."""
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)


def _is_symlink(path: Path) -> bool:
    """Whether ``path`` *itself* is a symbolic link.

    ``lstat`` rather than ``stat``, which is the whole point: ``stat`` follows
    the link and answers about the target, so it cannot tell a segment from a
    pointer at ``/etc/hosts``. A path that does not exist is not a link — the
    caller's own ``stat`` turns that into the same 404 a moment later.
    """
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


async def _ready_dir(session: SessionDep, settings: SettingsDep, episode_id: int) -> Path:
    """The rendition directory of a ``ready`` episode, or 404.

    The state check is the point: an episode that is downloading or preparing
    may well have a half-written directory on disk (the transcode stages into
    a sibling and renames, so it should not — but retention, a crash, or a
    force re-encode can all leave one), and ``ready`` is the one word that
    means "the playlist on disk is complete".

    The directory itself must also be a real directory rather than a link to
    one. Otherwise the confinement check in :func:`_serve` is satisfied by a
    root that has been moved: every file under a symlinked ``renditions/697``
    resolves neatly inside whatever that link points at.
    """
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.state is not EpisodeState.READY:
        _miss()
    directory = output_dir_for(settings, episode_id)
    if _is_symlink(directory):
        log.warning(
            "rendition directory is a symlink; refusing",
            extra={"episode_id": episode_id, "path": str(directory)},
        )
        _miss()
    return directory


def _etag(info: os.stat_result) -> str:
    """A strong validator from size and mtime (:func:`~arc.services.media.download.stat_etag`)."""
    return stat_etag(info.st_size, info.st_mtime_ns)


def _matches(header: str | None, etag: str) -> bool:
    """Whether ``If-None-Match`` names ``etag`` (RFC 9110 §13.1.2).

    ``*`` matches anything that exists. Otherwise the header is a comma list of
    validators, each possibly weak (``W/"…"``); weak comparison is the right
    one for a conditional GET, so the prefix is stripped before comparing.
    """
    if not header:
        return False
    if header.strip() == "*":
        return True
    for candidate in header.split(","):
        value = candidate.strip()
        if value.startswith("W/"):
            value = value[2:]
        if value == etag:
            return True
    return False


class _JSONRangeErrors(FileResponse):
    """``FileResponse`` whose range refusals come back as Arc's JSON.

    Starlette answers a malformed ``Range`` with a 400 and an unsatisfiable one
    with a 416, and it writes both as ``text/plain`` from inside ``__call__``
    rather than raising — an exception handler, however narrowly scoped, never
    sees them. So this intercepts the ASGI messages: everything else is passed
    straight through (the 200 and the 206 stream as they always did), and one
    of those two statuses is swallowed and answered again as ``{"detail": …}``.

    The re-issued response carries :attr:`validators` — the ``ETag`` and the
    caching rules the 200 would have carried — and, for a 416, the
    ``Content-Range`` Starlette computed, which is the header that tells the
    player how long the file actually is. It deliberately does **not** carry
    the file's ``Content-Length`` or ``Content-Type``: the body is now a JSON
    document, and repeating the file's would describe something else entirely.
    """

    def __init__(self, *args: Any, validators: dict[str, str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.validators = validators

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        refused: dict[str, Any] = {}

        async def intercept(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] in RANGE_ERRORS:
                refused["status"] = message["status"]
                refused["headers"] = Headers(raw=message["headers"])
                return
            if refused:
                # The plain-text body that went with the status just swallowed.
                return
            await send(message)

        await super().__call__(scope, receive, intercept)
        if not refused:
            return

        code: int = refused["status"]
        headers = dict(self.validators)
        sent: Headers = refused["headers"]
        content_range = sent.get("content-range")
        if content_range is not None:
            headers["Content-Range"] = content_range
        await JSONResponse({"detail": RANGE_ERRORS[code]}, status_code=code, headers=headers)(
            scope, receive, send
        )


def _checked_stat(directory: Path, path: Path) -> os.stat_result:
    """``stat`` of a plain file inside ``directory``, or 404.

    The two filesystem checks described at the top of this module: ``path``
    may not be a symlink, and it must resolve to somewhere inside
    ``directory``. Both sides are resolved before they are compared —
    ``DATA_DIR`` is routinely a path with a link in it (``/tmp`` on macOS, a
    mounted volume anywhere), and comparing a resolved target against an
    unresolved root would refuse every legitimate request on such a host. Then
    it has to exist and be a regular file. Shared by :func:`_serve` and the
    whole-episode download, which asks it of every part.
    """
    if _is_symlink(path):
        log.warning("refusing a symlink under a rendition", extra={"path": str(path)})
        _miss()
    root = directory.resolve(strict=False)
    target = path.resolve(strict=False)
    if not target.is_relative_to(root):
        log.warning(
            "refusing a path that resolves outside its rendition",
            extra={"path": str(path), "resolved": str(target), "root": str(root)},
        )
        _miss()

    try:
        info = path.stat()
    except OSError:
        _miss()
    if not stat.S_ISREG(info.st_mode):
        _miss()
    return info


def _serve(
    request: Request,
    directory: Path,
    path: Path,
    *,
    media_type: str,
    cache: str,
    extra: dict[str, str] | None = None,
) -> Response:
    """One file from inside ``directory``, with caching, conditionals, ranges.

    The filesystem checks come first (:func:`_checked_stat`). ``stat`` is
    taken there rather than left to ``FileResponse`` for two reasons: a
    missing file has to become a 404 (``FileResponse`` raises ``RuntimeError``
    and 500s), and the ETag has to exist before the ``If-None-Match``
    comparison that may mean no file is read at all.

    ``extra`` headers go on the file's own answers (200, 206) and nowhere
    else — the offline copy's ``Content-Disposition`` describes a body, and a
    304 or a range refusal has none.
    """
    info = _checked_stat(directory, path)
    etag = _etag(info)
    headers = {
        "Cache-Control": cache,
        "ETag": etag,
        "Accept-Ranges": "bytes",
        # Set here rather than left to ``FileResponse`` (which only
        # ``setdefault``s it, so this wins and stays consistent) because the
        # 304 below has to carry it too: RFC 9110 §15.4.5 asks a 304 for the
        # validators it would have sent with a 200, and a cache that got
        # ``Last-Modified`` once must not lose it on revalidation.
        "Last-Modified": formatdate(info.st_mtime, usegmt=True),
    }
    if _matches(request.headers.get("if-none-match"), etag):
        # 304 carries the validator and the caching rules and no body; a
        # ``Content-Length`` would be a lie about a body that is not there.
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return _JSONRangeErrors(
        path,
        media_type=media_type,
        headers={**headers, **(extra or {})},
        stat_result=info,
        validators=headers,
    )


@router.get(
    "/{episode_id}/index.m3u8",
    summary="The HLS playlist for one episode (FR-S1)",
    response_class=FileResponse,
    responses={
        304: {"description": "the caller's copy is current"},
        401: {"description": "not authenticated"},
        404: {"description": NOT_FOUND},
    },
)
async def playlist(
    episode_id: EpisodeId,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Serve ``index.m3u8`` as written by the transcode.

    Registered before the segment route so the literal name wins the match: it
    is not in :data:`SEGMENT_PATTERN`, and the other order would answer a
    playlist request with a 422 about a file name.

    ``user`` is asked for by name here — the router already requires a session
    — because the log line below is per playback and names who is watching.
    """
    directory = await _ready_dir(session, settings, episode_id)
    response = _serve(
        request,
        directory,
        directory / PLAYLIST_NAME,
        media_type=PLAYLIST_TYPE,
        cache=PLAYLIST_CACHE,
    )
    # One line per playback, not per segment: 237 segments an episode makes a
    # per-file log a way to lose the interesting line rather than to keep it.
    log.info(
        "playlist served",
        extra={"user_id": user.id, "episode_id": episode_id, "status": response.status_code},
    )
    return response


# --- The whole episode as one file (FR-S7) --------------------------------------


class _PartChanged(RuntimeError):
    """A part is no longer the file the response's headers were computed from.

    Raised mid-body, after the status and ``Content-Length`` have gone out, so
    there is no clean answer left: the exception aborts the connection, and a
    client that checks the length it was promised (the offline downloader
    does) sees a short body rather than a silently spliced one.
    """


@dataclass(frozen=True, slots=True)
class _Part:
    """One file of the concatenation, as it was when the headers were computed.

    ``dev`` and ``ino`` as well as size and mtime: a part replaced by another
    file of the same length and the same timestamp (a restore, a ``cp -p``) is
    a different file, and only its identity says so.
    """

    name: str
    size: int
    mtime_ns: int
    dev: int
    ino: int


@dataclass(slots=True)
class _Rendition:
    """An open rendition directory and the parts the playlist names.

    The directory is opened once — ``O_DIRECTORY | O_NOFOLLOW`` — and every
    file below it is opened *relative to that descriptor* (``dir_fd=``) with
    ``O_NOFOLLOW``. No path is resolved again for the life of the response, so
    a rendition directory renamed, swapped or replaced by a link twenty minutes
    into a download cannot redirect the reads still to come: they go to the
    directory that was checked, or fail. Whoever holds one must call
    :meth:`close`, on every path out.
    """

    fd: int
    parts: list[_Part]

    def close(self) -> None:
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            os.close(fd)


def _open_at(dir_fd: int, name: str) -> int:
    """A read-only descriptor for ``name`` inside ``dir_fd``; never a link."""
    return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)


def _regular_at(dir_fd: int, name: str) -> os.stat_result:
    """``lstat`` of ``name`` inside ``dir_fd`` if it is a plain file, else 404.

    The segment route's checks, relative to the open directory: a link is
    refused outright, and so is anything that is not a regular file.
    Confinement needs no resolving here — every name has been matched against
    the closed vocabulary, which admits no separator, so a name looked up
    under ``dir_fd`` is inside it by construction.
    """
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        _miss()
    if stat.S_ISLNK(info.st_mode):
        log.warning("refusing a symlink under a rendition", extra={"part": name})
        _miss()
    if not stat.S_ISREG(info.st_mode):
        _miss()
    return info


def _read_playlist(dir_fd: int) -> str:
    """``index.m3u8`` under ``dir_fd``, at most :data:`MAX_PLAYLIST_BYTES`."""
    with os.fdopen(_open_at(dir_fd, PLAYLIST_NAME), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise PlaylistError("the playlist is not a regular file")
        raw = handle.read(MAX_PLAYLIST_BYTES + 1)
    if len(raw) > MAX_PLAYLIST_BYTES:
        raise PlaylistError("the playlist is larger than any the encoder writes")
    return raw.decode("utf-8")


def _open_rendition(directory: Path, episode_id: int) -> _Rendition:
    """Open the rendition directory and check every part; or 404.

    Blocking (a few hundred ``stat`` calls and one small read), so the handler
    runs it in a worker thread. The directory is opened without following a
    link (:func:`_ready_dir` has refused one already; this closes the window
    after it), the playlist is read through it, every name the playlist hands
    back has been matched against the router's vocabulary by
    :func:`playlist_parts`, and each part gets :func:`_regular_at`. Any
    refusal is the router's one 404 and a warning in the log — never a file
    with a hole in it. The descriptor is closed here on every refusal; on
    success it belongs to the caller.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        log.warning(
            "refusing a download: the rendition directory cannot be opened as one",
            extra={"episode_id": episode_id},
        )
        _miss()
    try:
        try:
            names = playlist_parts(_read_playlist(dir_fd))
        except (OSError, UnicodeDecodeError, PlaylistError) as exc:
            log.warning(
                "refusing a download: the playlist does not describe a whole rendition",
                extra={"episode_id": episode_id, "reason": str(exc)},
            )
            _miss()

        parts: list[_Part] = []
        for name in names:
            try:
                info = _regular_at(dir_fd, name)
            except HTTPException:
                log.warning(
                    "refusing a download: a part is missing or refused",
                    extra={"episode_id": episode_id, "part": name},
                )
                raise
            parts.append(
                _Part(
                    name=name,
                    size=info.st_size,
                    mtime_ns=info.st_mtime_ns,
                    dev=info.st_dev,
                    ino=info.st_ino,
                )
            )
    except BaseException:
        os.close(dir_fd)
        raise
    return _Rendition(fd=dir_fd, parts=parts)


def _open_part(dir_fd: int, part: _Part) -> IO[bytes]:
    """Open one part and confirm it is still the file the headers describe."""
    handle = os.fdopen(_open_at(dir_fd, part.name), "rb")
    try:
        info = os.fstat(handle.fileno())
        now = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if now != (part.dev, part.ino, part.size, part.mtime_ns):
            raise _PartChanged(part.name)
    except BaseException:
        handle.close()
        raise
    return handle


async def _stream_parts(
    rendition: _Rendition, reads: list[PartRead], episode_id: int
) -> AsyncGenerator[bytes]:
    """The bytes of ``reads``, in bounded chunks, without blocking the loop.

    Opens, seeks and reads all run in worker threads (``anyio.wrap_file``), one
    part open at a time, at most :data:`DOWNLOAD_CHUNK` bytes in hand. A
    client that goes away stops it: Starlette's ``StreamingResponse`` cancels
    the iteration on ``http.disconnect``, and :class:`_PartsResponse` closes
    the generator, whose ``finally`` closes the file that was open.

    The open is shielded from that cancellation. A cancelled
    ``to_thread.run_sync`` still waits for its thread, and the thread still
    opens the file — but the handle it returns would then have nowhere to go.
    Shielded, it always reaches the ``try`` that closes it, and the
    cancellation lands at the next await inside it.
    """
    try:
        for read in reads:
            part = rendition.parts[read.index]
            with anyio.CancelScope(shield=True):
                handle = await anyio.to_thread.run_sync(_open_part, rendition.fd, part)
            try:
                source = anyio.wrap_file(handle)
                await source.seek(read.offset)
                remaining = read.length
                while remaining:
                    chunk = await source.read(min(DOWNLOAD_CHUNK, remaining))
                    if not chunk:
                        raise _PartChanged(part.name)
                    remaining -= len(chunk)
                    yield chunk
            finally:
                # Synchronously, not ``await source.aclose()``: on a disconnect
                # this runs inside a cancelled scope, where any await is
                # cancelled again before the close happens. Closing a file
                # opened for reading does not block.
                handle.close()
    except (_PartChanged, OSError) as exc:
        log.warning(
            "a rendition changed during a download; aborting the response",
            extra={"episode_id": episode_id, "reason": repr(exc)},
        )
        raise


class _PartsResponse(StreamingResponse):
    """``StreamingResponse`` that cleans up however the response ends.

    Starlette cancels the iteration when the client disconnects but leaves the
    suspended generator for the garbage collector, and with it the open file
    handle. ``aclose`` runs the generator's ``finally`` now instead. The
    rendition's directory descriptor is closed here rather than in the
    generator, because a generator that never started — a disconnect before
    the first chunk — never runs its ``finally`` at all.
    """

    def __init__(
        self, content: AsyncGenerator[bytes], *, rendition: _Rendition, **kwargs: Any
    ) -> None:
        super().__init__(content, **kwargs)
        self._generator = content
        self._rendition = rendition

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self._generator.aclose()
            finally:
                self._rendition.close()


@router.get(
    f"/{{episode_id}}/{DOWNLOAD_NAME}",
    summary="The whole episode as one MP4 file (FR-S7)",
    response_class=StreamingResponse,
    responses={
        200: {"content": {SEGMENT_TYPE: {}}, "description": "the whole file"},
        206: {"description": "a byte range of the file"},
        304: {"description": "the caller's copy is current"},
        400: {"description": RANGE_ERRORS[status.HTTP_400_BAD_REQUEST]},
        401: {"description": "not authenticated"},
        403: {"description": DEMO_REFUSED},
        404: {"description": NOT_FOUND},
        416: {"description": RANGE_ERRORS[status.HTTP_416_RANGE_NOT_SATISFIABLE]},
    },
)
async def download(
    episode_id: EpisodeId,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Serve ``init.mp4`` and every segment, concatenated, as one MP4 file.

    No ffmpeg and no temporary file: fMP4's init segment followed by its
    fragments is already a valid MP4 (:mod:`arc.services.media.download`), so
    the response is computed from the parts' sizes and streamed from the
    files themselves. Range, ``If-Range`` and ``If-None-Match`` are answered
    here, against a strong ETag over every part, because there is no single
    file for ``FileResponse`` to do it with — and because the in-app offline
    downloader that will consume this route resumes 8 MB ranges and restarts
    whenever that ETag moves.

    The demo account is refused before anything is looked up, so the 403 says
    nothing about which episodes exist.
    """
    if user.is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DEMO_REFUSED)
    directory = await _ready_dir(session, settings, episode_id)
    # Both already loaded or one primary-key lookup away; :func:`_ready_dir`
    # has established the episode exists and is ready.
    episode = await session.get(Episode, episode_id)
    anime = await session.get(Anime, episode.anime_id) if episode is not None else None
    if episode is None or anime is None:
        _miss()
    disposition = content_disposition(preferred_title(anime), episode.number)
    # Give the connection back before the body starts: FastAPI closes a
    # ``yield`` dependency only after the response has been *sent*, and a
    # download is sent over minutes. Nothing below touches the database, and
    # the dependency's own close later is a no-op on a closed session.
    await session.close()
    rendition = await anyio.to_thread.run_sync(_open_rendition, directory, episode_id)
    # The directory descriptor is closed here on every answer that does not
    # stream (304, 400, 416, HEAD, an exception), and by the response itself
    # once it has been handed one.
    try:
        response = _download_response(request, rendition, disposition, user.id, episode_id)
    except BaseException:
        rendition.close()
        raise
    if not isinstance(response, _PartsResponse):
        rendition.close()
    return response


def _download_response(
    request: Request, rendition: _Rendition, disposition: str, user_id: int, episode_id: int
) -> Response:
    """The answer for an open, checked rendition: 200, 206, 304, 400 or 416."""
    parts = rendition.parts
    sizes = [part.size for part in parts]
    total = sum(sizes)
    etag = download_etag([(part.name, part.size, part.mtime_ns) for part in parts])
    validators = {"Cache-Control": DOWNLOAD_CACHE, "ETag": etag, "Accept-Ranges": "bytes"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=validators)

    span: ByteSpan | None = None
    if if_range_matches(request.headers.get("if-range"), etag):
        try:
            span = parse_range(request.headers.get("range"), total)
        except RangeMalformed:
            code = status.HTTP_400_BAD_REQUEST
            return JSONResponse(
                {"detail": RANGE_ERRORS[code]}, status_code=code, headers=validators
            )
        except RangeNotSatisfiable:
            code = status.HTTP_416_RANGE_NOT_SATISFIABLE
            return JSONResponse(
                {"detail": RANGE_ERRORS[code]},
                status_code=code,
                headers={**validators, "Content-Range": f"bytes */{total}"},
            )

    headers = {**validators, "Content-Disposition": disposition}
    if span is None:
        code = status.HTTP_200_OK
        span = ByteSpan(first=0, last=total - 1, total=total)
    else:
        code = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Range"] = span.content_range()
    headers["Content-Length"] = str(max(span.length, 0))

    # One line per request: a resuming downloader makes a few dozen of them
    # for one episode (8 MB at a time), far fewer than the segments a playback
    # fetches, and each says which bytes went to whom.
    log.info(
        "episode download",
        extra={
            "user_id": user_id,
            "episode_id": episode_id,
            "status": code,
            "range": headers.get("Content-Range"),
        },
    )
    if request.method == "HEAD":
        return Response(status_code=code, headers=headers, media_type=SEGMENT_TYPE)
    return _PartsResponse(
        _stream_parts(rendition, slice_parts(sizes, span), episode_id),
        rendition=rendition,
        status_code=code,
        headers=headers,
        media_type=SEGMENT_TYPE,
    )


# --- The small offline copy (FR-P6) ------------------------------------------


@router.get(
    f"/{{episode_id}}/{OFFLINE_NAME}",
    summary="The small offline copy of one episode as one MP4 file (FR-P6)",
    response_class=FileResponse,
    responses={
        200: {"content": {SEGMENT_TYPE: {}}, "description": "the whole file"},
        206: {"description": "a byte range of the file"},
        304: {"description": "the caller's copy is current"},
        400: {"description": RANGE_ERRORS[status.HTTP_400_BAD_REQUEST]},
        401: {"description": "not authenticated"},
        403: {"description": DEMO_REFUSED},
        404: {"description": NOT_FOUND},
        416: {"description": RANGE_ERRORS[status.HTTP_416_RANGE_NOT_SATISFIABLE]},
    },
)
async def offline_copy(
    episode_id: EpisodeId,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Serve ``offline/<id>.mp4``, the copy a device keeps instead of ``episode.mp4``.

    A real file on disk, so everything :func:`_serve` does for a segment it
    does here: the symlink and confinement refusals, the strong ETag, the 304,
    ``Range`` and ``If-Range`` (Starlette's, which answers a mismatched
    ``If-Range`` with the whole file), and the JSON 400/416. Revalidated
    rather than cached for ever — a copy is remade under the same name — and
    named for the person saving it like the full-size download.

    The demo account is refused before anything is looked up. Then the copy
    must exist and be ``ready``, and :func:`~arc.services.media.copies.
    may_fetch_copy` must say this caller may have it; every other answer is
    the router's one 404. ``last_served_at`` is touched at most hourly, for
    the idle rule (``offline_idle_days``), and only when bytes go out.
    """
    if user.is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DEMO_REFUSED)
    episode = await session.get(Episode, episode_id)
    copy = await session.get(OfflineCopy, episode_id)
    if episode is None or copy is None or copy.state is not OfflineCopyState.READY:
        _miss()
    if not await may_fetch_copy(session, user, episode, copy):
        _miss()
    anime = await session.get(Anime, episode.anime_id)
    if anime is None:  # pragma: no cover - the foreign key says otherwise
        _miss()

    response = _serve(
        request,
        settings.offline_dir,
        offline_path_for(settings, episode_id),
        media_type=SEGMENT_TYPE,
        cache=DOWNLOAD_CACHE,
        extra={"Content-Disposition": content_disposition(preferred_title(anime), episode.number)},
    )
    interval = touch_interval(episode)
    if _sends_bytes(request, response, copy.size) and not served_recently(copy, interval=interval):
        # A Core UPDATE rather than an ORM write: no flush of anything else in
        # the session, and a row retention deleted a moment ago is zero rows
        # touched rather than a 500. The hour is checked again in SQL so two
        # ranged requests landing together write once.
        moment = datetime.now(UTC)
        await session.execute(
            update(OfflineCopy)
            .where(
                OfflineCopy.episode_id == episode_id,
                or_(
                    OfflineCopy.last_served_at.is_(None),
                    OfflineCopy.last_served_at < moment - timedelta(seconds=interval),
                ),
            )
            .values(last_served_at=moment)
        )
        await session.commit()
    # As for ``episode.mp4``: the body goes out over minutes and needs no
    # connection, so give it back now rather than when the response ends.
    await session.close()
    log.info(
        "offline copy request",
        extra={
            "user_id": user.id,
            "episode_id": episode_id,
            "status": response.status_code,
            "range": request.headers.get("range"),
        },
    )
    return response


def _sends_bytes(request: Request, response: Response, size: int | None) -> bool:
    """Whether this answer to a GET will carry some of the file.

    Not a 304, not a HEAD, and not a ``Range`` Starlette is about to refuse
    (400/416): only those count as the copy being *fetched* for the idle rule.
    The range is judged the way :func:`~arc.services.media.download.
    parse_range` judges it, which is close enough to Starlette's that the
    disagreement can only ever cost one hourly touch.
    """
    if request.method != "GET" or response.status_code != status.HTTP_200_OK:
        return False
    if size is None:
        return True
    try:
        parse_range(request.headers.get("range"), size)
    except RangeMalformed, RangeNotSatisfiable:
        return False
    return True


@router.get(
    "/{episode_id}/{name}",
    summary="One init or media segment (FR-S1)",
    response_class=FileResponse,
    responses={
        206: {"description": "a byte range of the segment"},
        304: {"description": "the caller's copy is current"},
        401: {"description": "not authenticated"},
        404: {"description": NOT_FOUND},
        416: {"description": "the requested range is not inside the file"},
        422: {"description": "not a name this rendition can contain"},
    },
)
async def segment(
    episode_id: EpisodeId,
    name: Annotated[
        str,
        PathParam(pattern=SEGMENT_PATTERN, description="``init.mp4`` or ``seg_NNNNN.m4s``"),
    ],
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Serve one segment, honouring ``Range`` and ``If-None-Match``.

    ``name`` has already been matched against :data:`SEGMENT_PATTERN` by the
    time this runs, which is what makes the join below a derivation rather
    than a path traversal: the pattern admits no separator, no ``..``, and no
    name the encoder did not write. What it is *not* is a guarantee about the
    file that turns up at that name — :func:`_serve` is where that is checked.

    No ``user`` parameter: the session is required by the router (241 segments
    an episode, and nothing here has anything to say about who asked).
    """
    directory = await _ready_dir(session, settings, episode_id)
    return _serve(
        request, directory, directory / name, media_type=SEGMENT_TYPE, cache=SEGMENT_CACHE
    )


# HEAD as well as GET, on all three. A media element's first act is often to
# ask how big a thing is, and HEAD is defined as GET without the body —
# Starlette's ``FileResponse`` already answers it that way, and the download
# handler answers it itself, so the only thing missing was the method being
# allowed. Registered separately and out of the schema rather
# than as ``methods=["GET", "HEAD"]``, which would publish two operations under
# one id and make the generated client types ambiguous.
for _route, _endpoint in (
    ("/{episode_id}/index.m3u8", playlist),
    (f"/{{episode_id}}/{DOWNLOAD_NAME}", download),
    (f"/{{episode_id}}/{OFFLINE_NAME}", offline_copy),
    ("/{episode_id}/{name}", segment),
):
    router.add_api_route(_route, _endpoint, methods=["HEAD"], include_in_schema=False)


__all__ = [
    "DEMO_REFUSED",
    "DOWNLOAD_CACHE",
    "DOWNLOAD_NAME",
    "NOT_FOUND",
    "OFFLINE_NAME",
    "PLAYLIST_CACHE",
    "PLAYLIST_TYPE",
    "RANGE_ERRORS",
    "SEGMENT_CACHE",
    "SEGMENT_PATTERN",
    "SEGMENT_TYPE",
    "download_url",
    "offline_url",
    "playlist_url",
    "router",
]
