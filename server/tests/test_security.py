"""The auth primitives: origins, password policy, and where Argon2 runs.

Nothing here touches the database or the app — these are the pure functions in
:mod:`arc.core.security` plus the thin async wrappers that keep Argon2 off the
event loop, and they are cheap enough to test exhaustively.
"""

from __future__ import annotations

import threading

import pytest

from arc.core.security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    hash_password,
    is_origin_allowed,
    origin_of,
    validate_password,
    verify_password,
)
from arc.services.auth.users import verify_password_async

# --- origin_of --------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arc.example/invite/abc?x=1", "https://arc.example"),
        # Case is not part of an origin: a browser lowercases the scheme and
        # host before it sends one, so Arc has to compare the same way.
        ("HTTP://LOCALHOST:5173", "http://localhost:5173"),
        ("HTTPS://Arc.Example", "https://arc.example"),
        # A default port is never in the Origin header a browser sends.
        ("https://x:443", "https://x"),
        ("https://x", "https://x"),
        ("http://x:80", "http://x"),
        # A non-default port is.
        ("http://x:8000", "http://x:8000"),
        ("https://x:8443", "https://x:8443"),
        # Userinfo is not part of an origin, and reading it as one is how
        # `https://arc.example@evil.test` gets mistaken for arc.example.
        ("https://arc.example@evil.test", "https://evil.test"),
        # IPv6 keeps its brackets.
        ("http://[::1]:5173", "http://[::1]:5173"),
        # Not origins at all.
        ("null", None),
        ("", None),
        ("/just/a/path", None),
        ("https://", None),
        ("https://[oops", None),
        ("https://x:notaport", None),
    ],
)
def test_origin_of_canonicalises(url: str, expected: str | None) -> None:
    assert origin_of(url) == expected


def test_two_spellings_of_one_origin_compare_equal() -> None:
    assert origin_of("HTTPS://Arc.Example:443/x") == origin_of("https://arc.example")


# --- is_origin_allowed ------------------------------------------------------

ALLOWED = frozenset({"https://arc.example"})


def test_an_allowed_origin_passes() -> None:
    assert is_origin_allowed("https://arc.example", None, ALLOWED)


def test_a_referer_stands_in_only_when_origin_is_absent() -> None:
    assert is_origin_allowed(None, "https://arc.example/login", ALLOWED)
    assert is_origin_allowed("", "https://arc.example/login", ALLOWED)


def test_a_present_but_unparseable_origin_is_authoritative() -> None:
    """`null` plus a friendly Referer is exactly what an attacker would send.

    A sandboxed iframe, a `data:` document or a cross-origin redirect all
    produce `Origin: null`; falling through to `Referer` there would hand the
    decision to a second header the same page controls.
    """
    assert not is_origin_allowed("null", "https://arc.example/login", ALLOWED)
    assert not is_origin_allowed("https://evil.test", "https://arc.example/", ALLOWED)


def test_neither_header_is_refused() -> None:
    assert not is_origin_allowed(None, None, ALLOWED)


# --- Password policy --------------------------------------------------------


def test_the_policy_bounds_both_ends() -> None:
    assert validate_password("x" * MIN_PASSWORD_LENGTH)
    assert validate_password("x" * MAX_PASSWORD_LENGTH)

    with pytest.raises(PasswordPolicyError):
        validate_password("x" * (MIN_PASSWORD_LENGTH - 1))
    with pytest.raises(PasswordPolicyError):
        validate_password("x" * (MAX_PASSWORD_LENGTH + 1))


def test_a_malformed_hash_is_a_failed_login_not_an_error() -> None:
    """Every argon2-cffi outcome funnels into a bool (`VerificationError`)."""
    assert verify_password("not-a-hash", "whatever") is False
    assert verify_password("", "whatever") is False

    stored = hash_password("correct-horse-battery")
    assert verify_password(stored, "correct-horse-battery") is True
    assert verify_password(stored, "wrong") is False


# --- Argon2 stays off the event loop ----------------------------------------


async def test_verification_runs_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """~50 ms of CPU on the loop would stall every other request for it."""
    threads: list[int] = []

    def spy(password_hash: str, password: str) -> bool:
        threads.append(threading.get_ident())
        return True

    monkeypatch.setattr("arc.services.auth.users.verify_password", spy)

    assert await verify_password_async("hash", "password") is True
    assert threads and threads[0] != threading.get_ident()
