"""The worker's liveness file and ``python -m arc.worker --check`` (M11).

This is what the ``worker`` container's healthcheck runs, so the thing worth
testing is the *decision*: fresh means 0, missing or stale means 1, and a
worker that stopped on purpose does not leave a file behind that keeps
reporting health for another minute and a half.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arc.config import Settings
from arc.worker import (
    HEARTBEAT_FILENAME,
    HEARTBEAT_STALE_AFTER,
    check_heartbeat,
    heartbeat_path,
    main,
    touch_heartbeat,
)


@pytest.fixture
def worker_settings(tmp_path: Path) -> Settings:
    """Settings whose DATA_DIR is a directory this test owns."""
    return Settings(_env_file=None, env="test", data_dir=tmp_path)  # type: ignore[call-arg]


def test_the_heartbeat_lives_under_the_data_directory(worker_settings: Settings) -> None:
    """The one directory the deployment guarantees the worker can write to."""
    assert heartbeat_path(worker_settings) == worker_settings.data_dir / HEARTBEAT_FILENAME


def test_a_worker_that_has_never_run_is_not_healthy(worker_settings: Settings) -> None:
    assert not heartbeat_path(worker_settings).exists()
    assert check_heartbeat(worker_settings) is False


def test_a_fresh_heartbeat_is_healthy(worker_settings: Settings) -> None:
    touch_heartbeat(worker_settings)

    assert check_heartbeat(worker_settings) is True
    # The file says when, in a form a human reading `cat` can use.
    assert heartbeat_path(worker_settings).read_text().startswith("20")


def test_the_directory_is_created_if_it_is_missing(tmp_path: Path) -> None:
    """A first boot on a fresh volume must not need the directory to exist."""
    settings = Settings(_env_file=None, env="test", data_dir=tmp_path / "nested" / "data")  # type: ignore[call-arg]

    touch_heartbeat(settings)

    assert check_heartbeat(settings) is True


def test_a_stale_heartbeat_is_not_healthy(worker_settings: Settings) -> None:
    """Three missed beats, which is what a wedged event loop looks like."""
    touch_heartbeat(worker_settings)
    path = heartbeat_path(worker_settings)
    old = path.stat().st_mtime - HEARTBEAT_STALE_AFTER - 1
    os.utime(path, (old, old))

    assert check_heartbeat(worker_settings) is False


def test_the_boundary_is_inclusive(worker_settings: Settings) -> None:
    """Exactly at the limit still counts as alive.

    The cost of a false negative is Docker restarting a worker in the middle
    of a three-hour transcode, so the boundary leans towards patience.
    """
    touch_heartbeat(worker_settings)
    path = heartbeat_path(worker_settings)
    stat = path.stat()
    os.utime(path, (stat.st_mtime, stat.st_mtime))

    assert check_heartbeat(worker_settings, now=stat.st_mtime + HEARTBEAT_STALE_AFTER) is True
    assert check_heartbeat(worker_settings, now=stat.st_mtime + HEARTBEAT_STALE_AFTER + 1) is False


def test_check_exits_zero_or_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m arc.worker --check`` — the container healthcheck itself."""
    settings = Settings(_env_file=None, env="test", data_dir=tmp_path)  # type: ignore[call-arg]
    monkeypatch.setattr("arc.worker.get_settings", lambda: settings)

    assert main(["--check"]) == 1
    assert "missing or stale" in capsys.readouterr().err

    touch_heartbeat(settings)

    assert main(["--check"]) == 0
    assert "fresh" in capsys.readouterr().out


def test_check_does_not_touch_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file check, deliberately — see the comment on ``main``.

    A database probe would report Postgres's health rather than the worker's,
    and a worker wedged with a dead event loop and a healthy database would
    pass. Pointing DATABASE_URL at nothing at all must not change the answer.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        env="test",
        data_dir=tmp_path,
        database_url="postgresql+asyncpg://nobody:nobody@203.0.113.1:5432/nope",
    )
    monkeypatch.setattr("arc.worker.get_settings", lambda: settings)
    touch_heartbeat(settings)

    assert main(["--check"]) == 0
