"""Building the recommendation chain from settings (§5.6).

Two things live here: the thing that knows how to construct a backend for a
``(provider, model)`` pair, and the thing that reads the configuration into a
chain of them. Everything above — the router, the runs orchestrator, the
tests — holds a :class:`~arc.services.recs.base.RecsModel` and cannot tell a
chain from a single model, which is the point of the protocol.

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
from typing import Any

from arc.config import Settings
from arc.core.config_check import is_placeholder
from arc.services.recs.base import MAX_RETRIES, TIMEOUT_SECONDS, RecsModel
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
        self._backends: dict[tuple[str, str], RecsModel] = {}

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

    def build(self, provider: str, model: str) -> RecsModel:
        """The backend for one entry, built once and reused."""
        cached = self._backends.get((provider, model))
        if cached is not None:
            return cached
        key = key_for(self._settings, provider)
        built: RecsModel
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


def build_recs_model(settings: Settings) -> RecsModel | None:
    """The whole chain, or ``None`` when nothing in it can be called."""
    entries = chain_entries(settings)
    if not entries:
        log.info(
            "recommendations are not configured",
            extra={
                "provider": settings.recs_provider,
                "expected_env": settings.recs_key_env(settings.recs_provider),
            },
        )
        return None

    log.info(
        "recommendation chain built",
        extra={"entries": [f"{entry.provider}/{entry.model}" for entry in entries]},
    )
    return RecsChain(entries, backends=BackendFactory(settings))


__all__ = ["BackendFactory", "build_recs_model", "chain_entries", "key_for"]
