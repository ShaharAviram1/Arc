"""Building the recommendation chain from settings (§5.6).

Two things live here: the thing that knows how to construct a backend for a
``(provider, model)`` pair, and the thing that reads the configuration into a
chain of them. Everything above — the router, the runs orchestrator, the match
suggestions, the tests — holds a :class:`~arc.services.recs.base.RecsModel` or
a :class:`~arc.services.recs.base.JsonModel` and cannot tell a chain from a
single model, which is the point of the protocols.

``None`` rather than an exception when nothing is configured. An unconfigured
backend is not a failure, it is a state the product has a name for: ``GET
/api/recs`` reports ``configured: false`` so the client can hide the button,
and ``POST`` answers 503. A key missing at boot must never stop the API from
starting — a Gemini key nobody set breaks the recommendations page and nothing
else.

Entries whose provider has no key are dropped rather than kept and skipped, so
"configured" means "some entry can actually be called". A deployment that
holds only an OpenRouter key and names it as the fallback gets a one-entry
chain, and it works.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from arc.config import Settings
from arc.core.config_check import is_placeholder
from arc.services.recs.base import MAX_RETRIES, TIMEOUT_SECONDS, JsonModel
from arc.services.recs.chain import ChainEntry, RecsChain
from arc.services.recs.claude import ClaudeRecsModel
from arc.services.recs.openai_compat import OpenAICompatRecsModel

log = logging.getLogger(__name__)


class BackendFactory:
    """Builds backends, sharing **one HTTP client per provider**.

    A backend instance is bound to one model, but the SDK client underneath it
    is bound only to a host and a key — and it owns a connection pool. Building
    one per model would give a three-model Gemini chain three pools to the same
    host, so the client is cached per provider and the cheap wrapper per
    ``(provider, model)``.

    Both caches are lazy: a chain usually answers on its first entry, and the
    later ones should cost nothing until the day they are needed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str, Any] = {}
        self._backends: dict[tuple[str, str], JsonModel] = {}

    def _client(self, provider: str) -> Any:
        if provider not in self._clients:
            key = key_for(self._settings, provider)
            if provider == "anthropic":
                from anthropic import AsyncAnthropic

                self._clients[provider] = AsyncAnthropic(
                    api_key=key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES
                )
            else:
                from openai import AsyncOpenAI

                self._clients[provider] = AsyncOpenAI(
                    api_key=key,
                    base_url=self._settings.recs_base_url(provider),
                    timeout=TIMEOUT_SECONDS,
                    max_retries=MAX_RETRIES,
                )
        return self._clients[provider]

    def build(self, provider: str, model: str) -> JsonModel:
        """The backend for one entry, built once and reused."""
        cached = self._backends.get((provider, model))
        if cached is not None:
            return cached
        key = key_for(self._settings, provider)
        built: JsonModel
        if provider == "anthropic":
            built = ClaudeRecsModel(api_key=key, model=model, client=self._client(provider))
        else:
            built = OpenAICompatRecsModel(
                api_key=key,
                model=model,
                provider=provider,
                base_url=self._settings.recs_base_url(provider),
                public_url=self._settings.public_url,
                client=self._client(provider),
            )
        self._backends[(provider, model)] = built
        return built

    async def aclose(self) -> None:
        """Close every client that was actually built."""
        for client in self._clients.values():
            closer = getattr(client, "close", None)
            if callable(closer):
                await closer()
        self._clients.clear()
        self._backends.clear()


def key_for(settings: Settings, provider: str) -> str:
    """``provider``'s key, or ``""`` if there is not a usable one.

    Filtered through the same :func:`~arc.core.config_check.is_placeholder` the
    config check uses, so a key still holding ``change-me`` is treated as
    absent. Without that, a half-filled ``.env`` produces a configured-looking
    page whose every run 502s on an auth error, instead of an honest "not
    configured".
    """
    value = settings.recs_key(provider)
    return "" if is_placeholder(settings.recs_key_field(provider), value) else value


def chain_entries(settings: Settings) -> list[ChainEntry]:
    """The chain the configuration describes, minus anything unusable.

    Primary entries first, in the order ``RECS_MODEL`` lists them, then the
    fallback's. An entry whose provider has no key is dropped here rather than
    skipped at call time, so the length of this list is the honest answer to
    "is anything configured".
    """
    entries: list[ChainEntry] = []

    if key_for(settings, settings.recs_provider):
        entries += [
            ChainEntry(provider=settings.recs_provider, model=model)
            for model in settings.recs_models
        ]

    fallback = settings.recs_fallback_provider
    if fallback and key_for(settings, fallback):
        entries += [
            ChainEntry(provider=fallback, model=model, fallback=True)
            for model in settings.recs_fallback_models
        ]

    return entries


def build_model(settings: Settings) -> RecsChain | None:
    """The whole chain, or ``None`` when nothing in it can be called.

    The concrete :class:`~arc.services.recs.chain.RecsChain` rather than a
    protocol, because it satisfies both of them: callers that want picks hold
    it as a :class:`~arc.services.recs.base.RecsModel`, and callers with their
    own schema — M13's ``llm_suggest_match`` — hold it as a
    :class:`~arc.services.recs.base.JsonModel`.
    """
    entries = chain_entries(settings)
    if not entries:
        log.info(
            "no model provider is configured",
            extra={
                "provider": settings.recs_provider,
                "expected_env": settings.recs_key_env(settings.recs_provider),
            },
        )
        return None

    log.info(
        "model chain built",
        extra={"entries": [f"{entry.provider}/{entry.model}" for entry in entries]},
    )
    return RecsChain(entries, backends=BackendFactory(settings))


#: The name M12 gave :func:`build_model`, kept so the recommendations router
#: and the eval read as what they are.
build_recs_model = build_model


@asynccontextmanager
async def model_for(settings: Settings) -> AsyncIterator[RecsChain | None]:
    """A chain for the length of one block, closed on the way out.

    For a one-off caller — a script, an eval, anything that asks once and is
    done. Work that runs repeatedly in a process should use
    :func:`shared_model` instead, so the cooldowns are remembered between
    calls; the whole point of the chain is that a spent model is not asked
    again until tomorrow, and a chain built per call cannot know that.
    """
    model = build_model(settings)
    try:
        yield model
    finally:
        if model is not None:
            await model.aclose()


# --- The worker's chain ------------------------------------------------------
#
# One per process, like the Nyaa client (:mod:`arc.services.acquisition.nyaa`)
# and for the same two reasons: it owns HTTP connection pools, and it holds
# state that is worth keeping — the daily-quota cooldowns. A handler has no
# ``app.state`` to hang it off, and building one per job would mean a queue of
# thirty review files retrying a spent Gemini model thirty times before the
# chain moved on. The API process keeps its own instance on
# ``app.state.recs_model`` (:func:`arc.api.recs.recs_model_for`) and the
# lifespan closes it; the two processes therefore learn cooldowns separately,
# which costs at most one wasted request each.

_shared: RecsChain | None = None
#: Distinguishes "not built yet" from "built, and there is nothing to build" —
#: ``None`` is a valid answer, so it cannot also be the empty state.
_shared_built = False


def shared_model(settings: Settings) -> RecsChain | None:
    """The process's chain, built on first use. ``None`` when unconfigured.

    Not keyed on ``settings``: a process reads its configuration once at start
    and keeps it for its life. Tests reset it between cases (``conftest``),
    the same way they drop the shared Nyaa client, because the SDK clients
    inside are bound to the event loop that built them.
    """
    global _shared, _shared_built
    if not _shared_built:
        _shared = build_model(settings)
        _shared_built = True
    return _shared


def reset_shared_model() -> RecsChain | None:
    """Forget the process's chain and hand it back, unclosed."""
    global _shared, _shared_built
    model, _shared, _shared_built = _shared, None, False
    return model


async def close_shared_model() -> None:
    """Close the process's chain, if one was ever built.

    Called from the worker's shutdown, beside ``close_shared_client``: this is
    the only place that owns it, so it is the only place that can close it.
    """
    model = reset_shared_model()
    if model is not None:
        await model.aclose()


__all__ = [
    "BackendFactory",
    "build_model",
    "build_recs_model",
    "chain_entries",
    "close_shared_model",
    "key_for",
    "model_for",
    "reset_shared_model",
    "shared_model",
]
