"""Credentials that travel in a URL never reach the log."""

import logging

from arc.core.logging import RedactSecretUrls

ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


def _record(msg: str, args: tuple[object, ...]) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)


def test_an_invite_token_in_access_args_is_redacted() -> None:
    record = _record(
        ACCESS_FORMAT, ("1.2.3.4:0", "GET", "/api/invites/NmnZk9qm-secret_token", "1.1", 200)
    )
    assert RedactSecretUrls().filter(record) is True
    line = record.getMessage()
    assert "secret_token" not in line
    assert '"GET /api/invites/REDACTED HTTP/1.1" 200' in line


def test_the_accept_call_is_redacted_too() -> None:
    record = _record(
        ACCESS_FORMAT, ("1.2.3.4:0", "POST", "/api/invites/abcDEF123/accept", "1.1", 201)
    )
    RedactSecretUrls().filter(record)
    assert "abcDEF123" not in record.getMessage()
    assert "/api/invites/REDACTED" in record.getMessage()


def test_the_mal_callback_loses_its_code_and_state() -> None:
    record = _record(
        ACCESS_FORMAT,
        ("1.2.3.4:0", "GET", "/api/mal/callback?code=SECRETCODE&state=SECRETSTATE", "1.1", 303),
    )
    RedactSecretUrls().filter(record)
    line = record.getMessage()
    assert "SECRETCODE" not in line
    assert "SECRETSTATE" not in line
    assert '"GET /api/mal/callback?REDACTED HTTP/1.1" 303' in line


def test_other_paths_and_the_invite_list_are_untouched() -> None:
    for path in ("/api/invites", "/api/anime/12", "/api/mal/status"):
        record = _record(ACCESS_FORMAT, ("1.2.3.4:0", "GET", path, "1.1", 200))
        RedactSecretUrls().filter(record)
        assert f'"GET {path} HTTP/1.1"' in record.getMessage()


def test_a_preformatted_message_is_redacted() -> None:
    record = _record("GET /api/invites/tok123 then /api/anime/7", ())
    RedactSecretUrls().filter(record)
    assert record.getMessage() == "GET /api/invites/REDACTED then /api/anime/7"
