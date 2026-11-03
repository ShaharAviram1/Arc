"""Ending a walk (FR-A1, M21). A leaf: models only, so every writer can reach it.

A walk (``walks``) is where a user's look-ahead on a show starts after a play;
``record_progress`` writes it. It ends by lapsing (untouched for D days, read
by the reconciler), and by being **deleted** here on three user-originated
events — never by reading the list entry after the fact, because FR-S4's own
list advance on finishing an episode stamps the entry in the very instant the
walk moves:

* the user **sets** the show dropped, on hold or completed in Arc
  (``catalog.lists.set_list_entry``; a MyAnimeList-side change ends nothing);
* FR-W5's auto-complete (``playback.progress._advance_list``);
* a rewatch pass ending, by the switch or on its last episode, when the walk
  moved during the pass (``playback.rewatch.stop``/``finish``);
* an undo from the MyAnimeList write log that restores such a status
  (``api.mal.revert``, FR-M7's user-originated revert).

**Accepted limit.** An offline progress report recorded *before* one of
these endings and replayed *after* it (FR-S8) recreates the walk, because the
row it would be compared with is gone. That walk then lives until it lapses
after D days, or until the next ending deletes it again.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import ListStatus, Walk

#: The statuses whose setting by the user in Arc ends their walk on the show.
WALK_ENDING_STATUSES: frozenset[ListStatus] = frozenset(
    {ListStatus.DROPPED, ListStatus.ON_HOLD, ListStatus.COMPLETED}
)


async def end_walk(
    session: AsyncSession, *, user_id: int, anime_id: int, since: datetime | None = None
) -> bool:
    """Delete the user's walk on the show; ``True`` if there was one.

    ``since`` limits it to a walk touched at or after that moment (a rewatch
    pass's start: only the pass's own walk goes with it).
    """
    statement = delete(Walk).where(Walk.user_id == user_id, Walk.anime_id == anime_id)
    if since is not None:
        statement = statement.where(Walk.touched_at >= since)
    result = await session.execute(statement.execution_options(synchronize_session="fetch"))
    return bool(getattr(result, "rowcount", 0))


__all__ = ["WALK_ENDING_STATUSES", "end_walk"]
