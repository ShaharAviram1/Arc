"""A device's own event log, kept for the operator (owner, 2026-10-30).

The download manager on a device keeps a ring buffer of its decisions — a
download started, resumed, truncated, verified, confirmed, found missing —
and "Send report" posts it (``POST /api/devices/{id}/report``). Episode 15 of
trip 4 was lost on the iPad with no trace of why; the next time, the operator
reads what the device did with ``python -m arc.cli device-report``.

Stored as sent, one row per report: the events are **data**, never
instructions, and nothing in Arc acts on them. Bounded on every insert: at
most :data:`MAX_REPORTS_PER_DEVICE` reports per device (the oldest pruned)
and at most :data:`MAX_REPORT_DEVICES` devices' reports per user (the device
that reported longest ago pruned first), so a client stuck sending, or one
minting new device ids, cannot fill the database.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arc.models import DeviceReport

log = logging.getLogger(__name__)

#: The most reports kept per device; the oldest go first.
MAX_REPORTS_PER_DEVICE: Final[int] = 20

#: The most devices whose reports are kept per user, like the holdings' own
#: :data:`~arc.services.acquisition.holdings.MAX_DEVICES`.
MAX_REPORT_DEVICES: Final[int] = 10

#: The most events one report may carry, and the most bytes its body may be.
MAX_REPORT_EVENTS: Final[int] = 500
MAX_REPORT_BYTES: Final[int] = 256 * 1024


async def store_report(
    session: AsyncSession,
    *,
    user_id: int,
    device_id: str,
    events: Sequence[dict[str, Any]],
    now: datetime,
) -> DeviceReport:
    """Keep one report and prune what the caps leave out. Flushed, not committed."""
    report = DeviceReport(
        user_id=user_id, device_id=device_id, received_at=now, events=list(events)
    )
    session.add(report)
    await session.flush()
    keep = (
        select(DeviceReport.id)
        .where(DeviceReport.user_id == user_id, DeviceReport.device_id == device_id)
        .order_by(DeviceReport.received_at.desc(), DeviceReport.id.desc())
        .limit(MAX_REPORTS_PER_DEVICE)
    )
    await session.execute(
        delete(DeviceReport).where(
            DeviceReport.user_id == user_id,
            DeviceReport.device_id == device_id,
            DeviceReport.id.notin_(keep),
        )
    )
    devices = (
        await session.scalars(
            select(DeviceReport.device_id)
            .where(DeviceReport.user_id == user_id)
            .group_by(DeviceReport.device_id)
            .order_by(func.max(DeviceReport.received_at).desc(), DeviceReport.device_id)
        )
    ).all()
    evicted = list(devices[MAX_REPORT_DEVICES:])
    if evicted:
        await session.execute(
            delete(DeviceReport).where(
                DeviceReport.user_id == user_id, DeviceReport.device_id.in_(evicted)
            )
        )
    await session.flush()
    log.info(
        "device report stored",
        extra={
            "user_id": user_id,
            "device_id": device_id,
            "report_id": report.id,
            "events": len(events),
        },
    )
    return report


async def latest_reports(
    session: AsyncSession, *, device_id: str, last: int = 1
) -> list[DeviceReport]:
    """The device's newest ``last`` reports, newest first."""
    return list(
        (
            await session.scalars(
                select(DeviceReport)
                .where(DeviceReport.device_id == device_id)
                .order_by(DeviceReport.received_at.desc(), DeviceReport.id.desc())
                .limit(max(last, 1))
            )
        ).all()
    )


def format_report(report: DeviceReport) -> list[str]:
    """One report as lines for a terminal, oldest event first. Pure.

    ``at kind [episode N] {detail}``: the detail as compact JSON, printed as
    data. A field of the wrong type is shown as it came rather than refused.
    """
    lines = [
        f"report {report.id} from device {report.device_id} (user {report.user_id}), "
        f"received {report.received_at.isoformat()}, {len(report.events)} events"
    ]
    for event in report.events:
        at = str(event.get("at", "?"))
        kind = str(event.get("kind", "?"))
        parts = [f"  {at}  {kind}"]
        if event.get("episode_id") is not None:
            parts.append(f"episode {event['episode_id']}")
        detail = event.get("detail")
        if detail:
            parts.append(json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str))
        lines.append("  ".join(parts))
    return lines


__all__ = [
    "MAX_REPORTS_PER_DEVICE",
    "MAX_REPORT_BYTES",
    "MAX_REPORT_DEVICES",
    "MAX_REPORT_EVENTS",
    "format_report",
    "latest_reports",
    "store_report",
]
