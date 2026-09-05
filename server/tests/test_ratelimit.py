"""The sliding-window rate limiter, and the bound on what it remembers.

The counting behaviour is exercised end-to-end in ``test_auth.py``; what is
tested here is the part that has no HTTP surface — that a dictionary keyed by
whatever an anonymous caller puts in a header cannot grow without limit.
"""

from __future__ import annotations

from arc.services.auth.ratelimit import MAX_KEYS, SWEEP_EVERY, LoginRateLimiter, RateLimitWindow

WINDOW = 900.0


def test_the_window_counts_and_then_refuses() -> None:
    limiter = RateLimitWindow(limit=3, window=WINDOW)

    for _ in range(3):
        assert limiter.retry_after("1.2.3.4", now=100.0) is None
        limiter.record("1.2.3.4", now=100.0)

    wait = limiter.retry_after("1.2.3.4", now=100.0)
    assert wait == WINDOW
    # A different key has its own budget.
    assert limiter.retry_after("5.6.7.8", now=100.0) is None


def test_a_key_frees_up_when_its_oldest_event_leaves_the_window() -> None:
    limiter = RateLimitWindow(limit=1, window=WINDOW)
    limiter.record("k", now=100.0)

    assert limiter.retry_after("k", now=100.0) == WINDOW
    assert limiter.retry_after("k", now=100.0 + WINDOW) is None


def test_a_sweep_drops_every_key_whose_events_have_expired() -> None:
    """5,000 one-shot callers must not stay in memory for the next 5,000."""
    limiter = RateLimitWindow(limit=10, window=WINDOW)

    for index in range(5_000):
        limiter.record(f"10.0.{index // 256}.{index % 256}", now=100.0)

    # The automatic sweep fires every SWEEP_EVERY records, but every key is
    # still inside the window at this point, so nothing has been dropped yet.
    assert len(limiter) == 5_000

    dropped = limiter.sweep(now=100.0 + WINDOW + 1)

    assert dropped == 5_000
    assert len(limiter) == 0


def test_recording_sweeps_by_itself_without_anyone_asking() -> None:
    """No caller has to remember to call `sweep`; `record` does it."""
    limiter = RateLimitWindow(limit=10, window=WINDOW)

    for index in range(SWEEP_EVERY):
        limiter.record(f"old-{index}", now=100.0)
    assert len(limiter) == SWEEP_EVERY

    # One record far enough in the future that every key above has expired.
    # It is also the SWEEP_EVERY-th since the last sweep, so a sweep runs.
    for index in range(SWEEP_EVERY):
        limiter.record(f"new-{index}", now=100.0 + WINDOW + 1)

    assert len(limiter) == SWEEP_EVERY, "the old keys went; the new ones stayed"


def test_the_key_count_is_hard_capped() -> None:
    """A flood faster than the sweep still cannot exhaust memory."""
    limiter = RateLimitWindow(limit=10, window=WINDOW)

    for index in range(MAX_KEYS + 500):
        # All at the same instant, so nothing is ever expired and only the
        # cap can hold the size down.
        limiter.record(f"key-{index}", now=100.0)

    assert len(limiter) == MAX_KEYS
    # The oldest are the ones evicted; the newest are all still counted.
    assert limiter.retry_after("key-0", now=100.0) is None
    assert limiter.retry_after(f"key-{MAX_KEYS + 499}", now=100.0) is None


def test_the_login_limiter_takes_the_stricter_of_its_two_budgets() -> None:
    limiter = LoginRateLimiter(per_ip=10, per_email=2, window_seconds=WINDOW)

    for _ in range(2):
        limiter.record(ip="1.2.3.4", email="a@arc.test", now=100.0)

    # The IP budget has eight left, but the address has none.
    assert limiter.retry_after(ip="1.2.3.4", email="a@arc.test", now=100.0) == WINDOW
    assert limiter.retry_after(ip="1.2.3.4", email="b@arc.test", now=100.0) is None

    limiter.clear()
    assert limiter.retry_after(ip="1.2.3.4", email="a@arc.test", now=100.0) is None
