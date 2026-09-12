"""Two AniList fields that need real parsing: ``staff`` and ``streamingEpisodes``.

Both were added for the M15 redesign — the "Made by" block on the show page and
the 16:9 stills on the episode rows and the Up Next shelf — and both arrive as
free text that has to be read rather than copied, which is why they live in a
module of their own instead of inside :func:`arc.services.anilist.client.parse_media`.

**Staff roles.** ``role`` is a free-text credit written by whoever edited the
entry: "Director", "Sound Director", "Chief Animation Director", "Music",
"Theme Song Performance", "Director (ep 4)". Arc wants exactly six credits
(:data:`arc.services.catalog.credits.CREDIT_ORDER`), and AniList's vocabulary
builds new jobs by *adding words to an existing one*: "Action Director" and
"Original Work Assistance" are not the director and not the author. So a role
is matched **whole** against a table of the spellings Arc knows, never as a
substring — a near-miss is a different job, not a fuzzy version of the same
one. Anything that matches nothing is dropped: an unmapped role is not a
credit Arc has a place for, and showing it would be a seventh row the design
has no slot for.

**Streaming episodes.** ``streamingEpisodes`` is a list of links to Crunchyroll
and friends, and the *title* of each is the only statement of which episode it
is: "Episode 12 - The Land Where Souls Rest". So the number is parsed out of
the title, and an entry whose title does not carry one is ignored rather than
guessed at — except in the one case where guessing is safe: nothing parsed at
all and the list is exactly as long as the show, where AniList's own order is
the episode order. That case is common on older entries whose titles are the
episode names alone.

Neither parser fabricates anything. An episode Arc cannot place keeps the null
it already has, which the client renders as the placeholder it has always
rendered.
"""

from __future__ import annotations

import re
from typing import Any

from arc.services.catalog.source import EpisodeArt

#: ``the whole role, normalised → the credit it means``. Every entry is a
#: spelling AniList actually publishes; a role that is not one of these keys is
#: not one of Arc's six, however many of the same words it contains. "Chief
#: Director" is here because a chief director *is* the director, while "Action
#: Director", "Sound Director", "Animation Director", "Original Character
#: Design" and "Original Work Assistance" are deliberately absent — they are
#: other people's jobs, and the old substring match promoting them into the
#: director's and the author's rows is exactly the bug this table fixes.
ROLE_CREDITS: dict[str, str] = {
    "director": "Director",
    "chief director": "Director",
    "general director": "Director",
    "series composition": "Series Composition",
    "character design": "Character Design",
    "music": "Music",
    "original creator": "Original Creator",
    "original story": "Original Creator",
    "original work": "Original Creator",
    "original manga": "Original Creator",
    "original novel": "Original Creator",
    "original light novel": "Original Creator",
}

#: The separators AniList editors use when one person holds two of these jobs
#: ("Director, Series Composition"). Each half is matched whole in its own
#: right, so splitting widens what is recognised without letting a longer job
#: title match a shorter one inside it.
_SEPARATOR = re.compile(r"\s*(?:,|/|&|\band\b)\s*")

#: The parenthetical AniList editors append to a per-episode credit
#: ("Director (eps 1, 14)"). Stripped before matching so that the episode-range
#: note does not make a role unrecognisable.
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")

#: "Episode 12 - The Land Where Souls Rest", and the several dashes people use
#: for it. The title half is optional: "Episode 12" on its own still places a
#: thumbnail. Anchored at the start so a title that merely mentions an episode
#: further along ("Frieren recap: Episode 1 to 4") does not parse.
EPISODE_TITLE = re.compile(
    r"^\s*(?:episode|ep\.?)\s*(\d{1,4})\s*(?:[-–—:]\s*(?P<title>.*))?$",
    re.IGNORECASE,
)


def credit_role(role: str | None) -> str | None:
    """The credit ``role`` means, or ``None`` if it is not one of the six.

    Case-insensitive, blind to the ``(eps 3, 7)`` note AniList editors add, and
    matched against :data:`ROLE_CREDITS` *whole*: "Action Director" is a
    fight choreographer and "Original Character Design" is the manga artist, so
    neither is allowed to answer for the role it merely contains.
    """
    if not role:
        return None
    normalised = " ".join(_PARENTHETICAL.sub("", role).casefold().split())
    if not normalised:
        return None
    for part in _SEPARATOR.split(normalised):
        credit = ROLE_CREDITS.get(part.strip())
        if credit is not None:
            return credit
    return None


def staff_credits(raw: dict[str, Any] | None) -> list[tuple[str, str]]:
    """AniList's ``staff`` connection as ``(credit, name)`` pairs.

    Order is AniList's own (``sort: RELEVANCE``);
    :func:`arc.services.catalog.credits.credits_from` puts them in the
    design's order afterwards. Edges with no mapped role, no node or no full
    name are dropped here rather than passed on as blanks.
    """
    edges = (raw or {}).get("edges") or []
    out: list[tuple[str, str]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        credit = credit_role(edge.get("role"))
        if credit is None:
            continue
        name = ((edge.get("node") or {}).get("name") or {}).get("full")
        if not name:
            continue
        out.append((credit, str(name)))
    return out


def parse_streaming_episodes(
    nodes: list[dict[str, Any]] | None,
    *,
    episodes: int | None = None,
) -> list[EpisodeArt]:
    """AniList's ``streamingEpisodes`` as per-episode titles and stills.

    An entry is placed by the number in its title. The first entry to claim a
    number keeps it — AniList lists the same episode once per streaming service
    on some entries, and the second copy is the same episode with a worse
    thumbnail, never a different one.

    When *nothing* parses and there are exactly ``episodes`` entries, AniList's
    order is taken as the episode order and each title is used whole. That is
    the only guess in this module, and it is conditioned on the count matching
    precisely so that a partial list (a show still airing, an entry with three
    of twelve links) is never slid into the wrong rows.
    """
    entries = [node for node in (nodes or []) if isinstance(node, dict)]
    if not entries:
        return []

    placed: dict[int, EpisodeArt] = {}
    for node in entries:
        title = node.get("title")
        match = EPISODE_TITLE.match(str(title)) if isinstance(title, str) else None
        if match is None:
            continue
        number = int(match.group(1))
        if number <= 0 or number in placed:
            continue
        placed[number] = EpisodeArt(
            number=number,
            title=_clean(match.group("title")),
            still_url=_clean(node.get("thumbnail")),
        )
    if placed:
        return [placed[number] for number in sorted(placed)]

    if episodes is None or len(entries) != episodes:
        return []
    return [
        EpisodeArt(
            number=index,
            title=_clean(node.get("title")),
            still_url=_clean(node.get("thumbnail")),
        )
        for index, node in enumerate(entries, start=1)
    ]


def _clean(value: Any) -> str | None:
    """A trimmed string, or ``None`` for anything empty or not a string."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


__all__ = [
    "EPISODE_TITLE",
    "ROLE_CREDITS",
    "credit_role",
    "parse_streaming_episodes",
    "staff_credits",
]
