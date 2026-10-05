"""``POST /api/sync`` — what a device recorded while it was offline (FR-S8).

Thin, as routers here are: the merge rule and the replay live in
:mod:`arc.services.playback.sync`, which applies every record through the
same functions ``POST /api/progress`` and ``…/watched`` call. This module
validates the envelope, validates each item on its own (so one bad item is
one ``rejected``, never a failed batch), looks up the rendition length a
number-less completion needs, and commits once.

Session and Origin check as every other ``/api`` POST
(:class:`arc.api.csrf.OriginCheckMiddleware`); never exempted, for the same
reason ``/api/progress`` is not — this route writes to watch history and,
through the list advance, to MyAnimeList.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import ValidationError

from arc.api.deps import CurrentUser, SessionDep
from arc.api.episode_extras import renditions_for
from arc.api.sync_schemas import SyncIn, SyncItemIn, SyncItemOut, SyncOut
from arc.services.playback.sync import (
    REASON_INVALID,
    SyncItem,
    SyncResult,
    SyncStatus,
    apply_batch,
    needs_rendition,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["playback"])

WRONG_USER = "these records belong to another account"


def now() -> datetime:
    """The server's clock; a function so a test can pin it."""
    return datetime.now(UTC)


def _client_id(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("client_id")
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


@router.post(
    "/api/sync",
    response_model=SyncOut,
    summary="Replay progress recorded while the device was offline (FR-S8)",
    responses={409: {"description": WRONG_USER}},
)
async def sync(body: SyncIn, user: CurrentUser, session: SessionDep) -> SyncOut:
    """Apply each record in order; answer applied / stale / rejected per record."""
    if body.user_id != user.id:
        # The client never sends another account's records; if it ever does,
        # nothing is applied and nothing is lost — it keeps them for their owner.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=WRONG_USER)

    # Validate one at a time; ``slots[i]`` is either a ready result (refused
    # here) or the index into ``valid`` the service will answer.
    valid: list[SyncItem] = []
    slots: list[SyncResult | int] = []
    for raw in body.items:
        try:
            item = SyncItemIn.model_validate(raw).to_item()
        except ValidationError:
            slots.append(SyncResult(_client_id(raw), SyncStatus.REJECTED, REASON_INVALID))
            continue
        slots.append(len(valid))
        valid.append(item)

    renditions = await renditions_for(session, sorted(needs_rendition(valid)))
    durations = {
        episode_id: rendition.duration or 0.0 for episode_id, rendition in renditions.items()
    }
    applied = await apply_batch(
        session,
        user_id=user.id,
        items=valid,
        now=now(),
        sent_at=body.sent_at,
        rendition_durations=durations,
    )
    await session.commit()

    results = [applied[slot] if isinstance(slot, int) else slot for slot in slots]
    log.info(
        "offline records synced",
        extra={
            "user_id": user.id,
            "items": len(results),
            "applied": sum(1 for r in results if r.status is SyncStatus.APPLIED),
            "stale": sum(1 for r in results if r.status is SyncStatus.STALE),
            "rejected": sum(1 for r in results if r.status is SyncStatus.REJECTED),
            "retry": sum(1 for r in results if r.status is SyncStatus.RETRY),
        },
    )
    return SyncOut(
        results=[
            SyncItemOut(client_id=r.client_id, status=r.status, reason=r.reason) for r in results
        ]
    )


__all__ = ["WRONG_USER", "now", "router"]
