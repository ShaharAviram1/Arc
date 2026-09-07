"""Shortening text that still has to say what went wrong.

One rule, used in two places. A transcode failure is a *sentence* — ``ffmpeg
exited 1``, or whatever :class:`~arc.services.media.plan.PlanError` complained
about — followed by forty lines of ffmpeg's own stderr. Both ends matter and
the middle does not: the sentence names the failure, the last stderr lines say
what ffmpeg was doing when it hit it, and the fifty lines of stream layout in
between are what a person scrolls past.

So neither end is trimmed. ``detail[:limit]`` would keep the sentence and throw
away the complaint; ``detail[-limit:]`` — which is what this replaced — keeps
the complaint and throws away the sentence, which is how a 500-character
``failure_reason`` came to start half way through a line of x264 diagnostics
with no clue that ffmpeg had exited at all. The middle goes instead, and
:data:`ELISION` marks where.

Written here rather than in either caller because both ends of the wire need
it: :mod:`arc.services.media.jobs` composes the string when it stores it, and
:mod:`arc.api.anime_schemas` shortens it again, harder, when it renders it.
"""

from __future__ import annotations

from typing import Final

#: What stands in for the dropped middle. On its own line so that neither the
#: sentence above it nor the stderr line below it is run together with it.
ELISION: Final[str] = "\n…\n"


def keep_head_and_tail(head: str, tail: str, *, limit: int) -> str:
    """``head``, then as much of the *end* of ``tail`` as ``limit`` allows.

    Both are stripped and joined with a newline. Under ``limit`` the whole
    thing is returned unchanged, so the common case — a one-line failure with
    no stderr behind it — is exactly the sentence and nothing else. Over it,
    ``head`` is kept whole and ``tail`` loses its beginning.

    A ``head`` longer than ``limit`` on its own is truncated and the tail is
    dropped: at that point there is no room for two things, and the sentence is
    the one worth having.
    """
    head = head.strip()
    tail = tail.strip()
    if not tail:
        return head[:limit]
    joined = f"{head}\n{tail}"
    if len(joined) <= limit:
        return joined
    room = limit - len(head) - len(ELISION)
    if room <= 0:
        return head[:limit]
    return f"{head}{ELISION}{tail[-room:]}"


def trim_middle(text: str, *, limit: int) -> str:
    """:func:`keep_head_and_tail` applied to something already joined.

    The head is the first line — which is what
    :mod:`arc.services.media.jobs` puts there, and what a
    :class:`~arc.services.media.transcode.TranscodeError` message is — and the
    tail is everything after it. Trimming an already-trimmed string is safe:
    the elision simply moves.
    """
    head, _, tail = text.strip().partition("\n")
    return keep_head_and_tail(head, tail, limit=limit)


__all__ = ["ELISION", "keep_head_and_tail", "trim_middle"]
