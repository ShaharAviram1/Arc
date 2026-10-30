"""What a device tells the server about itself (FR-A15, M20; owner 2026-10-30).

``PUT /api/devices/{device_id}/holdings`` — a device that downloaded episodes
with Keep offline (or a trip's copies) reports the ones it still holds
unwatched, so the caller's acquisition window skips them (FR-A1's device
skip). Thin over :mod:`arc.services.acquisition.holdings` and
:mod:`arc.services.trips.lost`.

* Body ``{episode_ids: [...]}`` (the old shape) **or** ``{episodes:
  [{episode_id, etag?, size?, sha256?}, ...]}`` (owner, 2026-10-30), at most
  500. The set **replaces** whatever this device reported before, so an empty
  list says "nothing any more"; the per-episode detail is stored beside it.
* A ``delivered``, unreleased row of an active trip that records **this**
  device, whose episode the report leaves out, is put back to ``pending`` —
  the device lost the copy and fetches it again (:mod:`arc.services.trips.
  lost`).
* **204** on success. **403** ``demo_account`` for the demo account (it has no
  downloads). **422** when ``device_id`` is not a version-4 UUID, or the body is
  neither shape.

``POST /api/devices/{device_id}/report`` — the device's own event log, for the
operator (``arc.cli device-report``): ``{events: [{at, kind, episode_id?,
detail?}]}``, at most 500 events and 256 KiB; **204**, **403** for the demo
account, **413** over the size, **422** otherwise malformed.

``device_id`` is a UUID the device generated; it is stored as a label and never
used as, or in, a path (spec §7). It is parsed and stored in its canonical
form, so two spellings of one UUID are one device.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Self

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.api.deps import MAX_ID, MIN_ID, CurrentUser, SessionDep, SettingsDep
from arc.api.trip_schemas import MAX_COPY_BYTES, SHA256_PATTERN, HeldVariant
from arc.models.devices import HOLDING_ETAG_LENGTH
from arc.services.acquisition.holdings import MAX_HOLDINGS, HoldingDetail, replace_holdings
from arc.services.devices.reports import MAX_REPORT_BYTES, MAX_REPORT_EVENTS, store_report
from arc.services.trips.device import canonical_device_id
from arc.services.trips.lost import repend_lost_copies

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


REPORT_TOO_LARGE: Final[str] = "report_too_large"

EpisodeIdField = Annotated[int, Field(ge=MIN_ID, le=MAX_ID)]


class HeldEpisode(BaseModel):
    """One episode a device holds, with what it knows of the copy (owner, 2026-10-30)."""

    model_config = ConfigDict(extra="forbid")

    episode_id: EpisodeIdField
    etag: str | None = Field(default=None, max_length=HOLDING_ETAG_LENGTH)
    size: int | None = Field(default=None, ge=0, le=MAX_COPY_BYTES)
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    #: The small copy (absent = small) or the full-size file. Either one
    #: counts as held, so neither triggers the lost-copy re-pend.
    variant: HeldVariant | None = None


class HoldingsIn(BaseModel):
    """The body of ``PUT /api/devices/{device_id}/holdings``: one of the two shapes."""

    model_config = ConfigDict(extra="forbid")

    episode_ids: list[EpisodeIdField] | None = Field(default=None, max_length=MAX_HOLDINGS)
    episodes: list[HeldEpisode] | None = Field(default=None, max_length=MAX_HOLDINGS)

    @model_validator(mode="after")
    def _one_shape(self) -> Self:
        if (self.episode_ids is None) == (self.episodes is None):
            raise ValueError("send either episode_ids or episodes")
        return self

    def held(self) -> tuple[list[int], dict[int, HoldingDetail]]:
        """The ids, and the detail each one came with (none in the old shape)."""
        if self.episodes is None:
            return list(self.episode_ids or []), {}
        details = {
            item.episode_id: HoldingDetail(etag=item.etag, size=item.size, sha256=item.sha256)
            for item in self.episodes
        }
        return list(details), details

    @property
    def count(self) -> int:
        return len(self.episodes if self.episodes is not None else self.episode_ids or [])


class ReportEvent(BaseModel):
    """One entry of a device's event log. Stored as sent; never acted on."""

    model_config = ConfigDict(extra="forbid")

    #: ISO 8601, as the device's clock wrote it (``toISOString``); stored as sent.
    at: datetime
    kind: str = Field(min_length=1, max_length=64)
    episode_id: EpisodeIdField | None = None
    detail: dict[str, Any] | None = None


class ReportIn(BaseModel):
    """The body of ``POST /api/devices/{device_id}/report``."""

    model_config = ConfigDict(extra="forbid")

    events: list[ReportEvent] = Field(max_length=MAX_REPORT_EVENTS)


def parse_device_id(raw: str) -> str:
    """The canonical text of a version-4 UUID, or a 422."""
    try:
        return canonical_device_id(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=BAD_DEVICE_ID
        ) from exc


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
    device_id: DeviceIdParam,
    body: HoldingsIn,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Store this device's set, re-pend the trip copies it lost; 204."""
    if user.is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DEMO_REFUSED)
    canonical = parse_device_id(device_id)
    at = now()
    ids, details = body.held()
    await replace_holdings(
        session, user_id=user.id, device_id=canonical, episode_ids=ids, now=at, details=details
    )
    await repend_lost_copies(
        session,
        settings,
        user_id=user.id,
        device_id=canonical,
        held=ids,
        now=at,
        truncated=body.count >= MAX_HOLDINGS,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/api/devices/{device_id}/report",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Keep a device's download-manager event log for the operator",
    responses={
        403: {"description": DEMO_REFUSED},
        413: {"description": REPORT_TOO_LARGE},
        422: {"description": BAD_DEVICE_ID},
    },
)
async def post_report(
    device_id: DeviceIdParam,
    request: Request,
    body: ReportIn,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    """Store the report (pruned to the newest per device); 204."""
    if user.is_demo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DEMO_REFUSED)
    if len(await request.body()) > MAX_REPORT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=REPORT_TOO_LARGE)
    canonical = parse_device_id(device_id)
    await store_report(
        session,
        user_id=user.id,
        device_id=canonical,
        events=[event.model_dump(mode="json", exclude_none=True) for event in body.events],
        now=now(),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = [
    "BAD_DEVICE_ID",
    "DEMO_REFUSED",
    "REPORT_TOO_LARGE",
    "HeldEpisode",
    "HoldingsIn",
    "ReportEvent",
    "ReportIn",
    "now",
    "parse_device_id",
    "router",
]
