from __future__ import annotations

from pathlib import Path

import pytest

from arc.config import ConfigurationError, Settings

ENV_VARS = [
    "ENV",
    "LOG_LEVEL",
    "PUBLIC_URL",
    "DATABASE_URL",
    "SECRET_KEY",
    "FERNET_KEY",
    "MAL_CLIENT_ID",
    "MAL_CLIENT_SECRET",
    "MAL_REDIRECT_URI",
    "ANTHROPIC_API_KEY",
    "QBIT_URL",
    "QBIT_USER",
    "QBIT_PASS",
    "DATA_DIR",
    "MAX_TRANSCODES",
    "TRANSCODE_PRESET",
    "TRANSCODE_CRF",
    "TRANSCODE_TUNE",
    "TRANSCODE_MAXRATE_KBPS",
    "TRANSCODE_BUFSIZE_KBPS",
    "LLM_MATCH_SUGGESTIONS",
    "BOOTSTRAP_ADMIN_EMAIL",
    "BOOTSTRAP_ADMIN_PASSWORD",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.usefixtures("clean_env")
def test_defaults_load_with_a_clean_environment() -> None:
    settings = _settings()

    assert settings.env == "dev"
    assert settings.is_prod is False
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.max_transcodes == 2
    assert settings.llm_match_suggestions is False


@pytest.mark.usefixtures("clean_env")
def test_secrets_default_to_none() -> None:
    settings = _settings()

    assert settings.secret_key is None
    assert settings.fernet_key is None
    assert settings.anthropic_api_key is None
    assert settings.mal_client_id is None


@pytest.mark.usefixtures("clean_env")
def test_require_raises_only_for_missing_secrets() -> None:
    settings = _settings()

    with pytest.raises(ConfigurationError):
        settings.require("anthropic_api_key")

    assert settings.require("qbit_url") == settings.qbit_url


@pytest.mark.usefixtures("clean_env")
def test_secrets_are_not_exposed_by_repr_or_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")

    settings = _settings()

    assert "sk-ant-supersecret" not in repr(settings)
    assert "sk-ant-supersecret" not in settings.model_dump_json()
    assert settings.require("anthropic_api_key") == "sk-ant-supersecret"


@pytest.mark.usefixtures("clean_env")
def test_env_overrides_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("MAX_TRANSCODES", "4")
    monkeypatch.setenv("DATA_DIR", "/data")
    monkeypatch.setenv("LLM_MATCH_SUGGESTIONS", "true")

    settings = _settings()

    assert settings.is_prod is True
    assert settings.max_transcodes == 4
    assert settings.data_dir == Path("/data")
    assert settings.renditions_dir == Path("/data/renditions")
    assert settings.llm_match_suggestions is True


@pytest.mark.usefixtures("clean_env")
def test_the_transcode_quality_defaults() -> None:
    """M15: raised from veryfast/CRF 20, which the owner judged soft."""
    settings = _settings()

    assert settings.transcode_preset == "fast"
    assert settings.transcode_crf == 19
    assert settings.transcode_tune == "animation"
    # A bitrate ceiling is opt-in: CRF alone is the control for one box.
    assert settings.transcode_maxrate_kbps is None
    assert settings.transcode_bufsize_kbps is None


@pytest.mark.usefixtures("clean_env")
def test_the_transcode_knobs_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCODE_PRESET", "veryfast")
    monkeypatch.setenv("TRANSCODE_CRF", "22")
    monkeypatch.setenv("TRANSCODE_TUNE", "")
    monkeypatch.setenv("TRANSCODE_MAXRATE_KBPS", "6000")
    monkeypatch.setenv("TRANSCODE_BUFSIZE_KBPS", "9000")

    settings = _settings()

    assert settings.transcode_preset == "veryfast"
    assert settings.transcode_crf == 22
    # Empty is how an operator turns tuning off; the options object turns it
    # into ``None`` so no ``-tune`` reaches ffmpeg at all.
    assert settings.transcode_tune == ""
    assert settings.transcode_maxrate_kbps == 6000
    assert settings.transcode_bufsize_kbps == 9000


@pytest.mark.usefixtures("clean_env")
def test_an_impossible_crf_is_refused() -> None:
    with pytest.raises(ValueError, match="transcode_crf"):
        Settings(_env_file=None, transcode_crf=99)  # type: ignore[call-arg]
