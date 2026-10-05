"""Request and response shapes for ``POST /api/sync`` (FR-S8).

The envelope is validated by FastAPI; the items are **not**, on purpose. A
batch is a device's whole offline evening, and one record that a buggy build
wrote badly must be answered ``rejected`` on its own rather than turning the
other forty into a 422 the device would retry for ever. So ``items`` arrives
as raw objects and :class:`SyncItemIn` is applied to each one in the router.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.api.deps import MAX_ID, MIN_ID
from arc.services.playback.sync import MAX_BATCH, SyncItem, SyncKind, SyncStatus

#: A client id is a UUID in practice; the bound is only so a broken client
#: cannot make the server echo a megabyte back.
CLIENT_ID_MAX = 64


class SyncItemIn(BaseModel):
    """One record from the device's queue."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=CLIENT_ID_MAX)
    kind: Literal["position", "completion", "unmark"]
    episode_id: int = Field(ge=MIN_ID, le=MAX_ID)
    #: When the device recorded it, ISO-8601. Future values are clamped.
    at: datetime
    position_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    duration_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _numbers_fit_the_kind(self) -> SyncItemIn:
        if self.kind == "position" and (self.position_s is None or self.duration_s is None):
            raise ValueError("a position needs position_s and duration_s")
        if self.kind == "unmark" and (self.position_s is not None or self.duration_s is not None):
            raise ValueError("an unmark carries no position")
        if (self.position_s is None) != (self.duration_s is None):
            raise ValueError("position_s and duration_s come together")
        return self

    def to_item(self) -> SyncItem:
        return SyncItem(
            client_id=self.client_id,
            kind=SyncKind(self.kind),
            episode_id=self.episode_id,
            at=self.at,
            position_s=self.position_s,
            duration_s=self.duration_s,
        )


class SyncIn(BaseModel):
    """The body of ``POST /api/sync``.

    ``user_id`` is the account the device recorded these under. It must be the
    caller's: records made under one account are never applied to another
    (409), and the client keeps them for their owner.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(ge=MIN_ID, le=MAX_ID)
    #: The device's clock when it sent the batch. ``server_now − sent_at`` is
    #: the device's skew, and every item's ``at`` is moved by it before it is
    #: compared with anything (:func:`arc.services.playback.sync.adjust_timestamp`).
    sent_at: datetime
    #: Raw elements, each validated on its own — even a non-object element is
    #: one ``rejected`` item rather than a 422 for the batch.
    items: list[Any] = Field(max_length=MAX_BATCH)


class SyncItemOut(BaseModel):
    #: Echoed from the item; null only when the item had no readable id.
    client_id: str | None
    status: SyncStatus
    reason: str | None = None


class SyncOut(BaseModel):
    """One result per item, in the order the items were sent."""

    results: list[SyncItemOut]


__all__ = ["CLIENT_ID_MAX", "SyncIn", "SyncItemIn", "SyncItemOut", "SyncOut"]
