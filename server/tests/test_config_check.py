"""Production configuration warnings (roadmap M11, arc/core/config_check.py).

The point of these tests is the shipped ``.env.example``: a stack deployed by
copying it and changing only ``PUBLIC_HOST`` must light up every line, because
that is exactly the deployment somebody will make at 2 a.m.
"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from arc.config import Settings
from arc.core import config_check
from arc.main import create_app

#: Everything a production stack needs, filled in. The starting point for the
#: "one thing is wrong" tests below.
GOOD = {
    "env": "prod",
    "public_url": "https://arc.example.com",
    "secret_key": "a-real-secret-key-not-an-example",
    "fernet_key": "3Yq8kK1kQfQ2s5w8n2Zx0aB6cD9eF1gH3iJ5kL7mN9o=",
    "mal_client_id": "abcdef0123456789",
    "mal_client_secret": "fedcba9876543210",
    "mal_redirect_uri": "https://arc.example.com/api/mal/callback",
    "qbit_pass": "a-real-qbittorrent-password",
}


def prod(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})  # type: ignore[arg-type]


def keys(settings: Settings) -> set[str]:
    return {warning.key for warning in config_check.warnings(settings)}


def test_a_fully_configured_production_stack_has_no_warnings() -> None:
    assert config_check.warnings(prod()) == []
    assert config_check.count(prod()) == 0


def test_nothing_is_checked_outside_production(settings: Settings) -> None:
    """Dev defaults are defaults, not problems — that is the whole point.

    The test settings are as unconfigured as it gets (no MAL, no qBittorrent
    password) and must still produce nothing.
    """
    assert config_check.warnings(settings) == []
    assert config_check.count(settings) == 0
    # …and the same values *would* be a pile of problems in production.
    assert config_check.count(settings.model_copy(update={"env": "prod"})) > 0


def test_the_shipped_example_env_fails_every_check() -> None:
    """A stack deployed from .env.example with only PUBLIC_HOST changed.

    ``SECRET_KEY`` is the literal example value, ``FERNET_KEY`` is blank, the
    MAL pair is blank, ``QBIT_PASS`` is ``adminadmin``, and the redirect URI
    still points at localhost. Every one of those is a real production
    failure, and each must be its own line so an operator can fix them in one
    pass rather than one restart at a time.
    """
    example = Settings(  # type: ignore[call-arg]
        _env_file=None,
        env="prod",
        public_url="https://arc.example.com",
        secret_key="dev-only-not-secret-change-me",
        qbit_pass="adminadmin",
    )

    assert keys(example) == {
        "SECRET_KEY",
        "FERNET_KEY",
        "MAL_CLIENT_ID",
        "MAL_CLIENT_SECRET",
        "QBIT_PASS",
        "MAL_REDIRECT_URI",
    }


@pytest.mark.parametrize(
    ("field", "key"),
    [
        ("secret_key", "SECRET_KEY"),
        ("fernet_key", "FERNET_KEY"),
        ("mal_client_id", "MAL_CLIENT_ID"),
        ("mal_client_secret", "MAL_CLIENT_SECRET"),
        ("qbit_pass", "QBIT_PASS"),
    ],
)
def test_each_required_key_is_reported_on_its_own(field: str, key: str) -> None:
    assert keys(prod(**{field: None})) == {key}
    assert keys(prod(**{field: ""})) == {key}
    assert keys(prod(**{field: "change-me"})) == {key}
    # Case does not save it, and neither does padding it out.
    assert keys(prod(**{field: "TODO-CHANGE-ME-later"})) == {key}


def test_the_shipped_qbittorrent_password_counts_as_missing() -> None:
    """``adminadmin`` is in .env.example and in every qBittorrent guide."""
    assert keys(prod(qbit_pass="adminadmin")) == {"QBIT_PASS"}
    assert keys(prod(qbit_pass="admin")) == {"QBIT_PASS"}
    # A password that merely *contains* the word is somebody's real password.
    assert keys(prod(qbit_pass="adminadmin-but-longer-and-actually-chosen")) == set()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "https://arc.localhost",
        "http://0.0.0.0",
        "not a url at all",
    ],
)
def test_a_local_public_url_is_a_production_problem(url: str) -> None:
    assert "PUBLIC_URL" in keys(prod(public_url=url))


def test_a_local_mal_redirect_uri_is_a_production_problem() -> None:
    """The shipped default, and the usual reason a real MAL link fails."""
    warnings = config_check.warnings(
        prod(mal_redirect_uri="http://localhost:8000/api/mal/callback")
    )

    assert [w.key for w in warnings] == ["MAL_REDIRECT_URI"]
    # The message has to say what to do, because the fix is in two places: the
    # variable, and the MAL application's own configuration.
    assert "api/mal/callback" in warnings[0].message
    assert "MAL application" in warnings[0].message


def test_the_anthropic_key_is_only_required_when_the_feature_is_on() -> None:
    assert keys(prod(anthropic_api_key=None)) == set()
    assert keys(prod(anthropic_api_key=None, llm_match_suggestions=True)) == {"ANTHROPIC_API_KEY"}
    assert keys(prod(anthropic_api_key="sk-ant-real", llm_match_suggestions=True)) == set()


def test_the_bootstrap_admin_is_not_a_warning() -> None:
    """It is needed for one boot and then deliberately blanked.

    Warning about it forever would train an operator to ignore the list.
    """
    assert keys(prod(bootstrap_admin_email=None, bootstrap_admin_password=None)) == set()


def test_every_problem_is_logged_as_its_own_error(caplog: pytest.LogCaptureFixture) -> None:
    broken = prod(secret_key=None, fernet_key=None)

    with caplog.at_level(logging.ERROR, logger="arc.core.config_check"):
        count = config_check.log_warnings(broken, component="api")

    assert count == 2
    assert len(caplog.records) == 2
    assert {record.config_key for record in caplog.records} == {"SECRET_KEY", "FERNET_KEY"}  # type: ignore[attr-defined]
    assert {record.component for record in caplog.records} == {"api"}  # type: ignore[attr-defined]
    # The values themselves never reach the log — only the names.
    assert "dev-only" not in caplog.text


def test_a_healthy_production_stack_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="arc.core.config_check"):
        assert config_check.log_warnings(prod(), component="worker") == 0

    assert caplog.records == []


# --- the health endpoint ----------------------------------------------------


async def test_health_omits_the_count_outside_production(client: AsyncClient) -> None:
    """A ``0`` in dev would read as "checked, and fine", which it is not."""
    body = (await client.get("/api/health")).json()

    assert "config_warnings" not in body


async def test_health_reports_the_count_in_production(settings: Settings) -> None:
    """The count and nothing else: this endpoint needs no session.

    "FERNET_KEY is unset" on a public URL is a map of where to push.
    """
    broken = settings.model_copy(
        update={"env": "prod", "public_url": "http://localhost:5173", "fernet_key": None}
    )
    app = create_app(broken)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")

    body = response.json()
    assert response.status_code == 200
    assert body["config_warnings"] == config_check.count(broken) > 0
    # No key names, no values, no list.
    assert "FERNET_KEY" not in response.text
    assert "public_url" not in response.text


async def test_health_reports_zero_for_a_configured_production_stack(settings: Settings) -> None:
    """What a post-deploy smoke test asserts."""
    app = create_app(settings.model_copy(update=GOOD))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/api/health")).json()

    assert body["config_warnings"] == 0
    assert body["env"] == "prod"
