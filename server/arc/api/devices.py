"""What a device holds: ``PUT /api/devices/{device_id}/holdings`` (FR-A15, M20).

A device that downloaded episodes with Keep offline reports the ones it still
holds unwatched, so the caller's acquisition window skips them (FR-A1's device
skip). Thin over :mod:`arc.services.acquisition.holdings`.

* Body ``{episode_ids: [...]}``, at most 500. The set **replaces** whatever
  this device reported before, so an empty list says "nothing any more".
* **204** on success. **403** ``demo_account`` for the demo account (it has no
  downloads). **422** when ``device_id`` is not a version-4 UUID, or the body is
  not a list of ids.

``device_id`` is a UUID the device generated; it is stored as a label and never
used as, or in, a path (spec §7). It is parsed and stored in its canonical
form, so two spellings of one UUID are one device.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Response, status
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from arc.api.deps import MAX_ID, MIN_ID, CurrentUser, SessionDep
from arc.services.acquisition.holdings import MAX_HOLDINGS, replace_holdings

log = logging.getLogger(__name__)

router = APIRouter(tags=["devices"])

DEMO_REFUSED: Final[str] = "demo_account"
BAD_DEVICE_ID: Final[str] = "device_id must be a version 4 UUID"

#: A UUID in any of the spellings :class:`uuid.UUID` accepts is at most 38
#: characters (braces); anything longer is refused before it is parsed.
DeviceIdParam = Annotated[
    str, PathParam(min_length=32, max_length=45, description="The device's own UUID (v4).")
]


def now() -> datetime:
    """The clock a report is stamped with; a function so a test can pin it."""
    return datetime.now(UTC)


class HoldingsIn(BaseModel):
    """The body of ``PUT /api/devices/{device_id}/holdings``."""

    model_config = ConfigDict(extra="forbid")

    episode_ids: list[Annotated[int, Field(ge=MIN_ID, le=MAX_ID)]] = Field(max_length=MAX_HOLDINGS)


def parse_device_id(raw: str) -> str:
    """The canonical text of a version-4 UUID, or a 422."""
    try:
        parsed = uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=BAD_DEVICE_ID
        ) from exc
    if parsed.version != 4:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=BAD_DEVICE_ID)
    return str(parsed)


@router.put(
    "/api/devices/{device_id}/holdings",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Replace the set of unwatched episodes one device holds (FR-A15)",
    responses={
        403: {"description": DEMO_REFUSED},
        422: {"description": BAD_DEVICE_ID},
    },
)
async def put_holdings(
    device_id: DeviceIdParam, body: HoldingsIn, user: CurrentUser, session: SessionDep
) -> Response:
    """Store this device's set; 204."""
    if user.is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DEMO_REFUSED)
    canonical = parse_device_id(device_id)
    await replace_holdings(
        session, user_id=user.id, device_id=canonical, episode_ids=body.episode_ids, now=now()
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["BAD_DEVICE_ID", "DEMO_REFUSED", "HoldingsIn", "now", "parse_device_id", "router"]
