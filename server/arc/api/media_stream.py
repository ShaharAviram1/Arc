"""Authenticated HLS: the playlist and the segments (FR-S1, architecture §5.4).

Two routes, mounted at ``/media`` and deliberately **not** under ``/api``:

* ``GET /media/{episode_id}/index.m3u8``
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
from email.utils import formatdate
from pathlib import Path
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import Headers
from starlette.types import Message, Receive, Scope, Send

from arc.api.deps import CurrentUser, EpisodeId, SessionDep, SettingsDep, get_current_user
from arc.models import Episode, EpisodeState
from arc.services.media.names import output_dir_for
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
SEGMENT_PATTERN: Final[str] = r"\A(init\.mp4|seg_\d{5}\.m4s)\z"

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


def playlist_url(episode_id: int) -> str:
    """Where a client points hls.js for one episode.

    Here rather than in the schema that sends it, so that the URL a response
    promises and the route that answers it are written in one file and move
    together.
    """
    return f"{router.prefix}/{episode_id}/{PLAYLIST_NAME}"


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
    """A strong validator from size and mtime.

    Not a hash of the content: a segment is a megabyte and this is answered on
    every revalidation. Size *and* mtime because a re-encode reuses the name
    and can plausibly produce a file of the same length.
    """
    return f'"{info.st_size:x}-{info.st_mtime_ns:x}"'


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


def _serve(
    request: Request, directory: Path, path: Path, *, media_type: str, cache: str
) -> Response:
    """One file from inside ``directory``, with caching, conditionals, ranges.

    The two filesystem checks come first and are described at the top of this
    module: ``path`` may not be a symlink, and it must resolve to somewhere
    inside ``directory``. Both sides are resolved before they are compared —
    ``DATA_DIR`` is routinely a path with a link in it (``/tmp`` on macOS, a
    mounted volume anywhere), and comparing a resolved target against an
    unresolved root would refuse every legitimate request on such a host.

    ``stat`` is taken here rather than left to ``FileResponse`` for two
    reasons: a missing file has to become a 404 (``FileResponse`` raises
    ``RuntimeError`` and 500s), and the ETag has to exist before the
    ``If-None-Match`` comparison that may mean no file is read at all.
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
        path, media_type=media_type, headers=headers, stat_result=info, validators=headers
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


# HEAD as well as GET, on both. A media element's first act is often to ask how
# big a thing is, and HEAD is defined as GET without the body — Starlette's
# ``FileResponse`` already answers it that way, so the only thing missing was
# the method being allowed. Registered separately and out of the schema rather
# than as ``methods=["GET", "HEAD"]``, which would publish two operations under
# one id and make the generated client types ambiguous.
for _route, _endpoint in (
    ("/{episode_id}/index.m3u8", playlist),
    ("/{episode_id}/{name}", segment),
):
    router.add_api_route(_route, _endpoint, methods=["HEAD"], include_in_schema=False)


__all__ = [
    "NOT_FOUND",
    "PLAYLIST_CACHE",
    "PLAYLIST_TYPE",
    "RANGE_ERRORS",
    "SEGMENT_CACHE",
    "SEGMENT_PATTERN",
    "SEGMENT_TYPE",
    "playlist_url",
    "router",
]
