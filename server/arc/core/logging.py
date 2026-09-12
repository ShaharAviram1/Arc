"""Logging setup: JSON lines in production, human-readable in dev.

Uses the standard library only — no extra dependency — so that anything that
logs (including third-party libraries) lands in the same stream with the same
shape.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from arc.config import Settings

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    # uvicorn attaches an ANSI-decorated copy of its own message; we format
    # colour ourselves, so drop it rather than print the escape codes.
    "color_message",
}

_LEVEL_COLOURS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    """Fields passed via ``logger.info(..., extra={...})``."""
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, suitable for log shipping."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class PrettyFormatter(logging.Formatter):
    """Short, coloured lines for a developer's terminal."""

    def __init__(self, *, colour: bool = True) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        colour = _LEVEL_COLOURS.get(level, "") if self.colour else ""
        reset = _RESET if colour else ""
        when = self.formatTime(record, self.datefmt)
        line = f"{when} {colour}{level:<8}{reset} {record.name} — {record.getMessage()}"
        extras = _extras(record)
        if extras:
            line += " " + " ".join(f"{k}={v!r}" for k, v in extras.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(settings: Settings) -> None:
    """Configure the root logger for this process. Idempotent."""
    formatter: logging.Formatter
    if settings.is_prod:
        formatter = JsonFormatter()
    else:
        formatter = PrettyFormatter(colour=sys.stderr.isatty())

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Uvicorn installs its own handlers; make them defer to ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # httpx logs every request URL at INFO, query string included — and the
    # TMDB v3 key travels as ``?api_key=`` (M15.5). Arc's own clients log what
    # matters about a call on their own loggers, so httpx's line is noise at
    # best and a secret in the worker's log at worst. WARNING keeps its real
    # complaints.
    logging.getLogger("httpx").setLevel(logging.WARNING)
