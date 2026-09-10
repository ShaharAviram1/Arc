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
    # RECS_PROVIDER defaults to gemini, so this is the key that stack needs.
    "gemini_api_key": "AIza-a-real-gemini-key",
}


def prod(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})  # type: ignore[arg-type]


def keys(settings: Settings) -> set[str]:
    """The ERROR-level keys. The one warning-level check has its own test."""
    return {warning.key for warning in config_check.errors(settings)}


def test_a_fully_configured_production_stack_has_no_errors() -> None:
    assert config_check.errors(prod()) == []
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
    warnings = config_check.errors(prod(mal_redirect_uri="http://localhost:8000/api/mal/callback"))

    assert [w.key for w in warnings] == ["MAL_REDIRECT_URI"]
    # The message has to say what to do, because the fix is in two places: the
    # variable, and the MAL application's own configuration.
    assert "api/mal/callback" in warnings[0].message
    assert "MAL application" in warnings[0].message


def test_match_suggestions_are_only_an_error_when_the_flag_is_on() -> None:
    """A deployment that never asked for them is finished, not broken (M13)."""
    assert keys(prod(**blank_keys())) == set()
    assert keys(prod(llm_match_suggestions=True, **blank_keys())) == {"LLM_MATCH_SUGGESTIONS"}


def test_match_suggestions_ride_the_recommendations_chain() -> None:
    """M13: the flag means "the M12 chain", not "an Anthropic key".

    Before M13 this check named ``ANTHROPIC_API_KEY`` specifically. Suggestions
    now go through the same provider chain the recommendations do, so *any*
    configured provider satisfies it — and an Anthropic key on a Gemini
    deployment no longer does.
    """
    assert keys(prod(llm_match_suggestions=True)) == set()  # GOOD has the Gemini key
    assert keys(
        prod(
            llm_match_suggestions=True,
            recs_provider="gemini",
            **{**blank_keys(), "anthropic_api_key": "sk-ant-real"},
        )
    ) == {"LLM_MATCH_SUGGESTIONS"}
    # …and a fallback provider alone is enough, because the chain would hold it.
    assert (
        keys(
            prod(
                llm_match_suggestions=True,
                recs_fallback_provider="openrouter",
                **{**blank_keys(), "openrouter_api_key": "sk-or-real"},
            )
        )
        == set()
    )


def test_the_suggestions_error_says_which_variable_to_set() -> None:
    found = [w for w in config_check.errors(prod(llm_match_suggestions=True, **blank_keys()))]

    assert [w.key for w in found] == ["LLM_MATCH_SUGGESTIONS"]
    assert "GEMINI_API_KEY" in found[0].message


# --- The recommendations backend (M12) --------------------------------------


def recs_warnings(settings: Settings) -> list[config_check.ConfigWarning]:
    """The warning-level lines, which is where the backend's key is reported."""
    return [w for w in config_check.warnings(settings) if w.level == "warning"]


#: The model each provider is expected to be paired with. Switching provider
#: without switching model is itself a warning now, so a test about *keys* has
#: to set both or it is testing two things at once.
MODELS = {
    "gemini": "gemini-3.5-flash",
    "openrouter": "anthropic/claude-opus-5",
    "anthropic": "claude-opus-5",
}

#: The key field each provider reads, for the same reason.
KEYS = {
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
    "anthropic": "anthropic_api_key",
}


def blank_keys() -> dict[str, None]:
    """Every provider key unset — the starting point for a "no key" test."""
    return dict.fromkeys(KEYS.values())


@pytest.mark.parametrize(
    ("provider", "key"),
    [
        ("gemini", "GEMINI_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
    ],
)
def test_a_missing_backend_key_is_a_warning_naming_the_provider(provider: str, key: str) -> None:
    """One line, so an operator who *meant* to set it sees it.

    It must not be an error: ``/api/health`` publishes the error count and a
    deploy smoke test asserts it is zero.
    """
    settings = prod(recs_provider=provider, recs_model=MODELS[provider], **blank_keys())

    found = recs_warnings(settings)

    assert [w.key for w in found] == [key]
    assert provider in found[0].message
    assert "503" in found[0].message
    assert config_check.count(settings) == 0


@pytest.mark.parametrize("provider", ["gemini", "openrouter", "anthropic"])
def test_the_right_key_silences_it(provider: str) -> None:
    settings = prod(
        recs_provider=provider,
        recs_model=MODELS[provider],
        **{**blank_keys(), KEYS[provider]: "a-real-looking-key"},
    )

    assert config_check.warnings(settings) == []


def test_the_wrong_provider_s_key_does_not_silence_it() -> None:
    """The failure worth catching: the right key for the provider not selected."""
    on_gemini = recs_warnings(
        prod(
            recs_provider="gemini",
            recs_model=MODELS["gemini"],
            **{**blank_keys(), "anthropic_api_key": "sk-ant-real"},
        )
    )
    on_anthropic = recs_warnings(
        prod(
            recs_provider="anthropic",
            recs_model=MODELS["anthropic"],
            **{**blank_keys(), "gemini_api_key": "AIza-real"},
        )
    )

    assert [w.key for w in on_gemini] == ["GEMINI_API_KEY"]
    assert [w.key for w in on_anthropic] == ["ANTHROPIC_API_KEY"]


def test_a_key_for_an_unselected_provider_is_not_itself_a_problem() -> None:
    """LLM_API_KEY set while RECS_PROVIDER=anthropic is fine and says nothing."""
    settings = prod(
        recs_provider="anthropic",
        recs_model=MODELS["anthropic"],
        anthropic_api_key="sk-ant-real",
        gemini_api_key="AIza-a-real-looking-key",
    )

    assert config_check.warnings(settings) == []


def test_the_match_feature_and_the_backend_are_reported_separately() -> None:
    """Two features, one missing key, two levels.

    They share the chain (M13) but not their severity: an unconfigured
    recommendations page is a valid deployment, and ``LLM_MATCH_SUGGESTIONS``
    on with nothing behind it is an operator's intention that cannot be met.
    """
    settings = prod(
        recs_provider="gemini",
        recs_model=MODELS["gemini"],
        llm_match_suggestions=True,
        **blank_keys(),
    )

    found = config_check.warnings(settings)

    assert [(w.key, w.level) for w in found] == [
        ("LLM_MATCH_SUGGESTIONS", "error"),
        ("GEMINI_API_KEY", "warning"),
    ]
    assert config_check.count(settings) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        blank_keys(),
        {**blank_keys(), "gemini_api_key": "AIza-real"},
        {**blank_keys(), "gemini_api_key": "change-me"},
        {**blank_keys(), "gemini_api_key": "AIza-real", "recs_model": ""},
        {**blank_keys(), "anthropic_api_key": "sk-ant-real", "recs_provider": "anthropic"},
        {
            **blank_keys(),
            "openrouter_api_key": "sk-or-real",
            "recs_fallback_provider": "openrouter",
        },
        {**blank_keys(), "recs_fallback_provider": "openrouter"},
        {
            **blank_keys(),
            "openrouter_api_key": "sk-or-real",
            "recs_fallback_provider": "openrouter",
            "recs_fallback_model": "",
        },
    ],
)
def test_configured_agrees_with_the_chain_the_factory_would_build(
    overrides: dict[str, object],
) -> None:
    """The one restatement in this module, pinned.

    :func:`config_check.model_chain_configured` cannot import
    ``chain_entries`` (the factory imports ``is_placeholder`` from here), so
    the rule is written twice. This is what stops the two drifting.
    """
    from arc.services.recs.factory import chain_entries

    settings = prod(**overrides)

    assert config_check.model_chain_configured(settings) is bool(chain_entries(settings))


@pytest.mark.parametrize(
    ("provider", "model"),
    [("gemini", "claude-opus-5"), ("anthropic", "gemini-3.5-flash")],
)
def test_a_model_that_does_not_match_the_provider_is_a_warning(provider: str, model: str) -> None:
    """Asking Gemini for claude-opus-5 is a 404 on the first run and silence
    before it, so the mismatch is worth one line at boot."""
    settings = prod(
        recs_provider=provider,
        recs_model=model,
        **{**blank_keys(), KEYS[provider]: "a-real-looking-key"},
    )

    found = recs_warnings(settings)

    assert [w.key for w in found] == ["RECS_MODEL"]
    assert model in found[0].message
    assert config_check.count(settings) == 0


def test_a_matching_model_says_nothing() -> None:
    assert (
        config_check.warnings(
            prod(
                recs_provider="gemini",
                recs_model="gemini-3.5-flash,gemini-2.5-flash",
                gemini_api_key="AIza-real",
            )
        )
        == []
    )
    assert (
        config_check.warnings(
            prod(
                recs_provider="anthropic",
                recs_model="claude-opus-5",
                **{**blank_keys(), "anthropic_api_key": "sk-ant-real"},
            )
        )
        == []
    )


def test_openrouter_model_names_are_not_checked() -> None:
    """It serves every vendor, so there is no prefix that could be wrong."""
    for model in ("anthropic/claude-opus-5", "google/gemini-3.5-flash", "meta/llama"):
        assert (
            config_check.warnings(
                prod(
                    recs_provider="openrouter",
                    recs_model=model,
                    **{**blank_keys(), "openrouter_api_key": "sk-or-real"},
                )
            )
            == []
        )


def test_a_fallback_provider_without_its_key_is_its_own_warning() -> None:
    """The operator has said what happens when the free tier runs out, and it
    will not."""
    settings = prod(
        recs_provider="gemini",
        recs_model=MODELS["gemini"],
        recs_fallback_provider="openrouter",
        recs_fallback_model="google/gemini-2.5-flash",
        **{**blank_keys(), "gemini_api_key": "AIza-real"},
    )

    found = recs_warnings(settings)

    assert [w.key for w in found] == ["OPENROUTER_API_KEY"]
    assert "RECS_FALLBACK_PROVIDER" in found[0].message
    assert config_check.count(settings) == 0


def test_a_fully_configured_chain_says_nothing() -> None:
    settings = prod(
        recs_provider="gemini",
        recs_model="gemini-3.5-flash,gemini-2.5-flash",
        recs_fallback_provider="openrouter",
        recs_fallback_model="google/gemini-2.5-flash",
        gemini_api_key="AIza-real",
        openrouter_api_key="sk-or-real",
    )

    assert config_check.warnings(settings) == []


def test_no_fallback_configured_is_not_a_warning() -> None:
    """Blank means "no fallback", which is a choice rather than a mistake."""
    settings = prod(recs_provider="gemini", recs_model=MODELS["gemini"], gemini_api_key="AIza-real")

    assert config_check.warnings(settings) == []


def test_the_fallback_model_list_is_checked_per_entry() -> None:
    settings = prod(
        recs_provider="gemini",
        recs_model=MODELS["gemini"],
        recs_fallback_provider="anthropic",
        recs_fallback_model="claude-opus-5,gemini-3.5-flash",
        gemini_api_key="AIza-real",
        anthropic_api_key="sk-ant-real",
    )

    found = recs_warnings(settings)

    assert [w.key for w in found] == ["RECS_FALLBACK_MODEL"]
    assert "gemini-3.5-flash" in found[0].message


def test_every_primary_model_in_the_list_is_checked() -> None:
    settings = prod(
        recs_provider="gemini",
        recs_model="gemini-3.5-flash,claude-opus-5,gemini-2.5-flash",
        gemini_api_key="AIza-real",
    )

    found = recs_warnings(settings)

    assert [w.key for w in found] == ["RECS_MODEL"]
    assert "claude-opus-5" in found[0].message


def test_a_warning_is_logged_at_warning_level(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="arc.core.config_check"):
        errors = config_check.log_warnings(prod(**blank_keys()), component="api")

    assert errors == 0
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert caplog.records[0].config_key == "GEMINI_API_KEY"  # type: ignore[attr-defined]


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
