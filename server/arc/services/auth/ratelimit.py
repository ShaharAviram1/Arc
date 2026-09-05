"""Login rate limiting (architecture.md §7: "login rate-limited per IP").

A sliding-window counter held **in process memory**. That is a deliberate
limitation, not an oversight:

* it is per-process, so two uvicorn workers each allow the configured budget
  (which is why the production stack runs the api with ``--workers 1``);
* it is per-instance, so a restart forgets everything.

Both are acceptable for the shape of Arc — a handful of accounts on one box —
and neither is acceptable for anything larger. M11 (hardening) is where this
moves behind a shared store (a Postgres table, or Redis if one is ever added);
the interface here is deliberately small so that swapping the implementation
touches one file.

Two independent budgets, checked together:

* **per IP** — stops one host grinding through a password list;
* **per email** — stops a distributed attempt from concentrating on one
  account. The per-email budget is the tighter of the two.

Attempts are counted whether or not they succeed. Counting only failures is
the friendlier choice, but it lets an attacker who has one valid account reset
nothing and probe forever between successful logins.

**Memory is bounded.** A dictionary keyed by "every address that has ever
tried" is an unauthenticated memory leak: the keys come from whoever is
calling. :class:`RateLimitWindow` therefore sweeps keys whose newest event has
fallen out of the window every :data:`SWEEP_EVERY` records, and hard-caps
itself at :data:`MAX_KEYS`, evicting the least recently seen. The cap can only
be reached by an attacker generating keys faster than the sweep clears them,
and evicting the *oldest* means the keys being evicted are the ones closest to
expiring anyway.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque

#: Records between sweeps. Cheap enough to be unnoticeable (one pass over the
#: keys per 256 attempts) and frequent enough that the dictionary tracks the
#: number of *recent* callers rather than the number of callers ever.
SWEEP_EVERY = 256

#: Hard ceiling on tracked keys. Reached only under a flood; ~10k deques of a
#: handful of floats is a few megabytes, which is the point — it is a bound,
#: not a budget anyone should hit.
MAX_KEYS = 10_000


class RateLimitWindow:
    """One sliding window: at most ``limit`` events per ``window`` seconds.

    Keys are arbitrary strings — an IP address, an email, anything the caller
    wants a separate budget for. Insertion-ordered so that eviction under the
    cap drops the least recently *touched* key.
    """

    __slots__ = ("limit", "window", "_events", "_since_sweep")

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._since_sweep = 0

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            return deque()
        cutoff = now - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            # Keep the dict from growing one entry per address ever tried.
            del self._events[key]
            return deque()
        return events

    def sweep(self, now: float | None = None) -> int:
        """Drop every key whose newest event has left the window.

        Returns how many keys went. Called automatically every
        :data:`SWEEP_EVERY` records; exposed so a test (or a later admin
        endpoint) can force one.
        """
        moment = time.monotonic() if now is None else now
        cutoff = moment - self.window
        stale = [key for key, events in self._events.items() if not events or events[-1] <= cutoff]
        for key in stale:
            del self._events[key]
        return len(stale)

    def retry_after(self, key: str, now: float | None = None) -> float | None:
        """Seconds until the next attempt is allowed, or ``None`` if it is now."""
        moment = time.monotonic() if now is None else now
        events = self._prune(key, moment)
        if len(events) < self.limit:
            return None
        # The window frees a slot when its oldest event falls out of it.
        return max(0.0, events[0] + self.window - moment)

    def record(self, key: str, now: float | None = None) -> None:
        """Count one event against ``key``, sweeping and capping as needed."""
        moment = time.monotonic() if now is None else now

        self._since_sweep += 1
        if self._since_sweep >= SWEEP_EVERY:
            self._since_sweep = 0
            self.sweep(moment)

        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
        else:
            self._events.move_to_end(key)
        events.append(moment)

        while len(self._events) > MAX_KEYS:
            # Oldest touched first: under a key flood these are the entries
            # nearest to expiring, so the budgets that survive are the ones
            # being actively used.
            self._events.popitem(last=False)

    def clear(self) -> None:
        self._events.clear()
        self._since_sweep = 0

    def __len__(self) -> int:
        """How many keys are currently tracked. For tests and diagnostics."""
        return len(self._events)


class LoginRateLimiter:
    """The two budgets a login attempt is checked against.

    One instance per application (``app.state.login_rate_limiter``), so tests
    get a fresh limiter per app and cannot leak counts into each other.
    """

    __slots__ = ("_by_ip", "_by_email")

    def __init__(self, *, per_ip: int, per_email: int, window_seconds: float) -> None:
        self._by_ip = RateLimitWindow(per_ip, window_seconds)
        self._by_email = RateLimitWindow(per_email, window_seconds)

    def retry_after(self, *, ip: str, email: str, now: float | None = None) -> float | None:
        """Seconds the caller must wait, or ``None`` if the attempt may proceed.

        The stricter of the two budgets wins, so a caller is never told to wait
        less than it actually has to.
        """
        moment = time.monotonic() if now is None else now
        waits = [
            wait
            for wait in (
                self._by_ip.retry_after(ip, moment),
                self._by_email.retry_after(email, moment),
            )
            if wait is not None
        ]
        return max(waits) if waits else None

    def record(self, *, ip: str, email: str, now: float | None = None) -> None:
        """Count one attempt against both budgets."""
        moment = time.monotonic() if now is None else now
        self._by_ip.record(ip, moment)
        self._by_email.record(email, moment)

    def clear(self) -> None:
        """Forget every recorded attempt. For tests and for an admin reset."""
        self._by_ip.clear()
        self._by_email.clear()


__all__ = ["MAX_KEYS", "SWEEP_EVERY", "LoginRateLimiter", "RateLimitWindow"]
