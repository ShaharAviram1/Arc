"""A circuit breaker per catalogue source (architecture.md §5.0).

Without one, every request during an AniList outage pays a full timeout before
falling back to MAL — fifteen seconds a page, on the keystroke path. So a
:class:`SourceUnavailable` opens the source's breaker for
``CATALOG_BREAKER_SECONDS``; while it is open the service skips that source
without calling it, and the first call after the window probes it again.

There is no half-open counting and no failure threshold. One failure is enough
evidence: a catalogue source is either answering or it is not, and the cost of
guessing wrong is one wasted request every five minutes.

State is per process and deliberately not persisted. It is a latency
optimisation, not a fact about the world, and a restart should re-probe.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

#: How long a source stays skipped after a failure, when nothing says otherwise.
DEFAULT_BREAKER_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class SourceState:
    """What the breaker knows about one source, as the admin view renders it."""

    #: ``"open"`` while the source is being skipped, ``"closed"`` otherwise.
    state: str
    #: Last time this source answered successfully.
    healthy_at: datetime | None = None
    #: Last time it failed.
    failed_at: datetime | None = None
    #: Why it failed, as the source described it.
    reason: str | None = None


@dataclass(slots=True)
class _Entry:
    """The mutable half, kept out of the public dataclass."""

    #: Monotonic instant before which the source must not be called.
    open_until: float = 0.0
    healthy_at: datetime | None = None
    failed_at: datetime | None = None
    reason: str | None = None


class Breaker:
    """Per-source open/closed state with a fixed cool-down.

    One instance is shared by everything in a process that talks to the
    catalogue, so an outage discovered by a search is not rediscovered by the
    show page a second later.
    """

    def __init__(self, seconds: float = DEFAULT_BREAKER_SECONDS) -> None:
        self.seconds = max(seconds, 0.0)
        self._entries: dict[str, _Entry] = {}

    def _entry(self, source: str) -> _Entry:
        entry = self._entries.get(source)
        if entry is None:
            entry = _Entry()
            self._entries[source] = entry
        return entry

    def is_open(self, source: str) -> bool:
        """Whether ``source`` should be skipped without being called."""
        entry = self._entries.get(source)
        if entry is None:
            return False
        return time.monotonic() < entry.open_until

    def record_failure(self, source: str, reason: str) -> bool:
        """Open (or re-open) ``source``; return whether this was a transition.

        The return value is what keeps the "catalogue switched to MAL" line out
        of the log on every request: only the call that actually opens the
        breaker gets ``True``, so the warning is written once per outage rather
        than once per page view.
        """
        entry = self._entry(source)
        was_open = time.monotonic() < entry.open_until
        entry.open_until = time.monotonic() + self.seconds
        entry.failed_at = datetime.now(UTC)
        entry.reason = reason
        return not was_open

    def record_success(self, source: str) -> None:
        """Close ``source`` and forget why it was ever open."""
        entry = self._entry(source)
        entry.open_until = 0.0
        entry.healthy_at = datetime.now(UTC)
        entry.reason = None

    def state(self, source: str) -> SourceState:
        """A snapshot of ``source`` for ``GET /api/catalog/status``."""
        entry = self._entries.get(source)
        if entry is None:
            return SourceState(state="closed")
        return SourceState(
            state="open" if self.is_open(source) else "closed",
            healthy_at=entry.healthy_at,
            failed_at=entry.failed_at,
            reason=entry.reason,
        )

    def reset(self) -> None:
        """Forget everything. For tests and for an operator's "try again now"."""
        self._entries.clear()


__all__ = ["DEFAULT_BREAKER_SECONDS", "Breaker", "SourceState"]
