"""Reading the configuration into a chain (§5.6).

Three providers, one protocol, and one rule that matters more than the
selection itself: nothing configured is ``None`` rather than an exception. The
API has a name for that state — ``configured: false``, then 503 — and a key
nobody set must never stop the process from booting.

The other rule worth stating: an entry whose provider has no key is **dropped**
rather than kept and skipped at call time, so the length of the chain is the
honest answer to "is anything configured".
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arc.config import PROVIDER_BASE_URLS, Settings
from arc.services.recs import build_recs_model, chain_entries
from arc.services.recs.base import RecsModel
from arc.services.recs.chain import RecsChain
from arc.services.recs.claude import ClaudeRecsModel
from arc.services.recs.factory import BackendFactory
from arc.services.recs.openai_compat import OpenAICompatRecsModel

GEMINI_KEY = "AIza-a-real-looking-key"
OPENROUTER_KEY = "sk-or-a-real-looking-key"
ANTHROPIC_KEY = "sk-ant-a-real-looking-key"


def settings_with(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def pairs(settings: Settings) -> list[tuple[str, str]]:
    return [(e.provider, e.model) for e in chain_entries(settings)]


@pytest.fixture
async def built() -> AsyncIterator[list[RecsModel]]:
    """Closes anything a test builds.

    ``build_recs_model`` constructs real SDK clients, which own connection
    pools. Left open, each is a resource warning and an event loop that will
    not close cleanly.
    """
    models: list[RecsModel] = []
    yield models
    for model in models:
        closer = getattr(model, "aclose", None)
        if closer is not None:
            await closer()


def build(models: list[RecsModel], settings: Settings) -> RecsModel | None:
    made = build_recs_model(settings)
    if made is not None:
        models.append(made)
    return made


# --- Which entries the configuration describes -------------------------------


def test_the_primary_models_are_tried_in_the_order_they_are_listed() -> None:
    settings = settings_with(gemini_api_key=GEMINI_KEY, recs_model="a-one, a-two ,a-three")

    assert pairs(settings) == [("gemini", "a-one"), ("gemini", "a-two"), ("gemini", "a-three")]


def test_the_fallback_comes_after_the_primary() -> None:
    settings = settings_with(
        gemini_api_key=GEMINI_KEY,
        openrouter_api_key=OPENROUTER_KEY,
        recs_model="gemini-3.5-flash",
        recs_fallback_provider="openrouter",
        recs_fallback_model="google/gemini-2.5-flash",
    )

    assert pairs(settings) == [
        ("gemini", "gemini-3.5-flash"),
        ("openrouter", "google/gemini-2.5-flash"),
    ]
    assert [e.fallback for e in chain_entries(settings)] == [False, True]


def test_the_default_chain_is_three_gemini_models() -> None:
    """What a deployment with only a Gemini key gets, out of the box."""
    settings = settings_with(gemini_api_key=GEMINI_KEY)

    assert pairs(settings) == [
        ("gemini", "gemini-3.5-flash"),
        ("gemini", "gemini-3.6-flash"),
        ("gemini", "gemini-2.5-flash"),
    ]


def test_an_openrouter_fallback_with_no_model_gets_the_documented_default() -> None:
    """Naming a provider and no model is a half-finished edit, not an intent."""
    settings = settings_with(
        gemini_api_key=GEMINI_KEY,
        openrouter_api_key=OPENROUTER_KEY,
        recs_fallback_provider="openrouter",
    )

    assert pairs(settings)[-1] == ("openrouter", "openai/gpt-5-mini")


def test_a_fallback_provider_with_no_key_contributes_nothing() -> None:
    settings = settings_with(
        gemini_api_key=GEMINI_KEY,
        recs_fallback_provider="openrouter",
        recs_model="gemini-3.5-flash",
    )

    assert pairs(settings) == [("gemini", "gemini-3.5-flash")]


def test_a_primary_provider_with_no_key_contributes_nothing() -> None:
    """…and the fallback still stands on its own."""
    settings = settings_with(
        openrouter_api_key=OPENROUTER_KEY,
        recs_fallback_provider="openrouter",
        recs_fallback_model="google/gemini-2.5-flash",
    )

    assert pairs(settings) == [("openrouter", "google/gemini-2.5-flash")]


def test_a_placeholder_key_counts_as_no_key() -> None:
    """A half-filled .env must read as "not configured", not as a page that
    502s on every run."""
    assert chain_entries(settings_with(gemini_api_key="change-me")) == []
    assert chain_entries(settings_with(gemini_api_key="   ")) == []


def test_nothing_configured_is_an_empty_chain() -> None:
    assert chain_entries(settings_with()) == []


# --- What gets built ---------------------------------------------------------


async def test_a_configured_deployment_gets_a_chain(built: list[RecsModel]) -> None:
    model = build(built, settings_with(gemini_api_key=GEMINI_KEY))

    assert isinstance(model, RecsChain)
    assert [row["model"] for row in model.status()] == [
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-2.5-flash",
    ]
    assert all(row["available"] for row in model.status())


async def test_nothing_configured_is_none_not_an_exception() -> None:
    assert build_recs_model(settings_with()) is None


async def test_a_key_for_a_provider_not_in_the_chain_does_not_configure_it() -> None:
    """The failure this whole design exists to make visible."""
    assert build_recs_model(settings_with(anthropic_api_key=ANTHROPIC_KEY)) is None


async def test_each_provider_gets_the_right_backend(built: list[RecsModel]) -> None:
    factory = BackendFactory(
        settings_with(
            gemini_api_key=GEMINI_KEY,
            openrouter_api_key=OPENROUTER_KEY,
            anthropic_api_key=ANTHROPIC_KEY,
        )
    )
    built.append(factory)  # type: ignore[arg-type]

    gemini = factory.build("gemini", "gemini-3.5-flash")
    openrouter = factory.build("openrouter", "google/gemini-2.5-flash")
    anthropic = factory.build("anthropic", "claude-opus-5")

    assert isinstance(gemini, OpenAICompatRecsModel)
    assert isinstance(openrouter, OpenAICompatRecsModel)
    assert isinstance(anthropic, ClaudeRecsModel)
    # OpenRouter's attribution headers, and Gemini's absence of them.
    assert gemini.extra_headers is None
    assert openrouter.extra_headers is not None


async def test_one_http_client_is_shared_by_a_provider_s_models(built: list[RecsModel]) -> None:
    """Three Gemini models must not mean three connection pools to one host."""
    factory = BackendFactory(settings_with(gemini_api_key=GEMINI_KEY))
    built.append(factory)  # type: ignore[arg-type]

    first = factory.build("gemini", "gemini-3.5-flash")
    second = factory.build("gemini", "gemini-3.6-flash")

    assert first is not second
    assert first._client is second._client  # type: ignore[attr-defined]


async def test_a_backend_is_built_once_per_entry(built: list[RecsModel]) -> None:
    factory = BackendFactory(settings_with(gemini_api_key=GEMINI_KEY))
    built.append(factory)  # type: ignore[arg-type]

    assert factory.build("gemini", "m") is factory.build("gemini", "m")


async def test_the_base_url_follows_the_provider_unless_overridden(
    built: list[RecsModel],
) -> None:
    default = settings_with(gemini_api_key=GEMINI_KEY)
    override = settings_with(gemini_api_key=GEMINI_KEY, gemini_base_url="http://proxy/v1")

    assert default.recs_base_url("gemini") == PROVIDER_BASE_URLS["gemini"]
    assert override.recs_base_url("gemini") == "http://proxy/v1"
    assert default.recs_base_url("anthropic") is None
