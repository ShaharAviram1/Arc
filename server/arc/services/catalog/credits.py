"""``anime.credits``: who made this, in the order the show page reads them.

The redesigned show page (M15) has a "Made by" block that treats the studio as
an auteur credit and lists the five people beside it whose names a viewer might
actually recognise: the director, the series composer, the character designer,
the composer, and whoever wrote the thing in the first place.

One module for two sources. AniList publishes a staff connection with free-text
roles; MAL publishes a studio and nothing else. Both end up here so that the
studio is the first row either way and a role has one spelling across the
catalogue — otherwise a show whose detail came from MAL during an outage would
render a differently-ordered, differently-labelled credits block from its
neighbour, for reasons no user could see.

The role *labels* are fixed strings rather than an enum: they are rendered
verbatim and stored in JSONB, so an enum would buy a conversion at both ends
and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The label the studio row carries. Not a role anybody holds — it is the one
#: credit that is an organisation — which is why it is named here rather than
#: coming out of a source's role string.
STUDIO_ROLE = "Studio"

#: Every label a credit row may carry, in the order the show page lists them.
#: Used by the tests and by :func:`credits_from` to sort, so that two sources
#: describing the same show produce the same block.
CREDIT_ORDER: tuple[str, ...] = (
    STUDIO_ROLE,
    "Director",
    "Series Composition",
    "Character Design",
    "Music",
    "Original Creator",
)

_RANK = {role: index for index, role in enumerate(CREDIT_ORDER)}


def credits_from(
    studio: str | None,
    staff: Iterable[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    """``[{role, name}]`` for one show: the studio, then the mapped staff.

    ``staff`` is ``(label, name)`` pairs already mapped onto
    :data:`CREDIT_ORDER` by the source adapter — the mapping is AniList's
    problem, since MAL has no staff to map — and is sorted here rather than
    trusted, because AniList returns them in relevance order and relevance puts
    the composer above the director about a third of the time.

    Duplicates are dropped on ``(role, name)`` and co-credits are kept: two
    people really do share a character design, and picking one of them would
    be a silent editorial decision. A row with no name is dropped; a show with
    no studio and no staff comes back as an empty list, which is what a
    "nothing known" credits block is.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(role: str, name: str | None) -> None:
        cleaned = (name or "").strip()
        if not cleaned or (role, cleaned) in seen:
            return
        seen.add((role, cleaned))
        rows.append({"role": role, "name": cleaned})

    add(STUDIO_ROLE, studio)
    for role, name in staff:
        if role in _RANK:
            add(role, name)
    # ``sorted`` is stable, so co-credits keep the source's own order within a
    # role while the roles themselves come out in the design's order.
    return sorted(rows, key=lambda row: _RANK[str(row["role"])])


__all__ = ["CREDIT_ORDER", "STUDIO_ROLE", "credits_from"]
