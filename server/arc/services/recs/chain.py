"""Trying several models in turn, and remembering which are spent (§5.6).

Production runs on Gemini's free tier, where each model allows about twenty
requests a day **for the whole deployment** — against Arc's own limit of ten
runs per user per day. One model is therefore not a configuration, it is a
countdown. The chain is the answer: a list of ``(provider, model)`` entries
tried in order, the primary provider's models first and a paid fallback
provider's after them.

Three rules decide what happens when an entry fails, and the distinction
between them is the whole design:

* :class:`~arc.services.recs.base.RecsUnavailable` — the provider could not
  answer. **Move to the next entry.** This is what the chain is for.
* :class:`~arc.services.recs.base.RecsRefused` — the model declined. **Stop and
  raise.** A refusal is a judgement about the request, and every other model
  would be asked the same thing; walking the chain would spend a paid fallback
  to be told no a second time.
* :class:`~arc.services.recs.base.RecsFailed` — an answer arrived and was
  unusable, after that backend already retried once. **Stop and raise.** A
  schema mismatch or a truncation is a prompt or a budget problem, not an
  availability one, and the next model will do the same thing.

**The daily-quota cooldown.** A 429 is ambiguous: it can mean "too fast" (worth
trying again in a minute) or "that is your lot for today" (worth nothing until
tomorrow). Gemini distinguishes them in the error body, so
:func:`is_daily_quota` reads it, and an entry that has spent its day is put on
cooldown until the next reset instead of being retried on every page view. The
cooldown lives in memory on this object, which lives as long as the app: it is
a cache of a fact about today, and losing it on a restart costs one wasted
request.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from arc.services.recs.base import RecsModel, RecsResult, RecsUnavailable, is_daily_quota

log = logging.getLogger(__name__)

#: When Google's free-tier daily quotas reset, in UTC. They roll over at
#: midnight US-Pacific, which is 08:00 UTC during Pacific Standard Time and
#: 07:00 during daylight time. The later of the two is used deliberately: an
#: entry that comes off cooldown an hour early costs one wasted request and
#: goes straight back on, while one that comes off an hour late costs nothing
#: at all — the next entry in the chain answers meanwhile.
QUOTA_RESET_HOUR_UTC = 8


def next_quota_reset(now: datetime) -> datetime:
    """The next :data:`QUOTA_RESET_HOUR_UTC` after ``now``."""
    today = now.astimezone(UTC).replace(
        hour=QUOTA_RESET_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    # ``>=`` so a call at exactly the reset instant gets *this* reset rather
    # than tomorrow's — otherwise an entry checked on the stroke of eight would
    # sit out another whole day.
    return today if today >= now else today + timedelta(days=1)


@dataclass(frozen=True, slots=True)
class ChainEntry:
    """One ``(provider, model)`` the chain may try."""

    provider: str
    model: str
    #: True for an entry that came from ``RECS_FALLBACK_*`` rather than from
    #: the primary provider. Only used for logging — a fallback answering is
    #: worth a line, because it usually means the free tier is spent.
    fallback: bool = False


class BackendBuilder(Protocol):
    """Makes a backend for one entry, reusing one HTTP client per provider."""

    def build(self, provider: str, model: str) -> RecsModel:  # pragma: no cover - protocol
        ...

    async def aclose(self) -> None:  # pragma: no cover - protocol
        ...


def utcnow() -> datetime:
    return datetime.now(UTC)


class RecsChain:
    """A :class:`~arc.services.recs.base.RecsModel` that is several of them.

    Holds the cooldowns, so it must outlive a request: the app builds one and
    keeps it on ``app.state`` for the process's life.
    """

    provider = "chain"

    def __init__(
        self,
        entries: Sequence[ChainEntry],
        *,
        backends: BackendBuilder,
        now: Any = utcnow,
    ) -> None:
        self.entries = list(entries)
        self._backends = backends
        self._now = now
        #: ``entry -> when it may be tried again``. Only daily quotas go in
        #: here; a per-minute 429 is not worth remembering.
        self._cooldowns: dict[ChainEntry, datetime] = {}

    # --- cooldowns --------------------------------------------------------

    def _available(self, entry: ChainEntry) -> bool:
        until = self._cooldowns.get(entry)
        return until is None or self._now() >= until

    def _mark_exhausted(self, entry: ChainEntry) -> None:
        until = next_quota_reset(self._now())
        self._cooldowns[entry] = until
        log.info(
            "recommendation model exhausted for the day",
            extra={
                "provider": entry.provider,
                "model": entry.model,
                "cooldown_until": until.isoformat(),
            },
        )

    def status(self) -> list[dict[str, Any]]:
        """The chain as it stands, for the admin view of ``GET /api/recs``.

        One row per entry, in the order they are tried, so an operator can see
        at a glance how much of the day's free tier is left.
        """
        return [
            {
                "provider": entry.provider,
                "model": entry.model,
                "available": self._available(entry),
                "cooldown_until": self._cooldowns.get(entry),
            }
            for entry in self.entries
        ]

    # --- the walk ---------------------------------------------------------

    async def recommend(self, *, system: str, user: str, schema: dict[str, Any]) -> RecsResult:
        """The first entry that answers.

        Only :class:`RecsUnavailable` advances the chain; a refusal or an
        unusable answer stops it, because neither is about availability and
        the next model would produce the same one.
        """
        last: RecsUnavailable | None = None
        skipped = 0

        for entry in self.entries:
            if not self._available(entry):
                skipped += 1
                continue
            backend = self._backends.build(entry.provider, entry.model)
            try:
                result = await backend.recommend(system=system, user=user, schema=schema)
            except RecsUnavailable as exc:
                last = exc
                if is_daily_quota(exc):
                    self._mark_exhausted(entry)
                else:
                    log.info(
                        "recommendation model unavailable; trying the next",
                        extra={
                            "provider": entry.provider,
                            "model": entry.model,
                            "error": str(exc)[:200],
                        },
                    )
                continue

            if entry.fallback:
                log.info(
                    "recommendation served by fallback",
                    extra={"provider": entry.provider, "model": result.model or entry.model},
                )
            return result

        if last is not None:
            raise last
        raise RecsUnavailable(
            "every recommendation model is on cooldown until its daily quota resets"
            if skipped
            else "no recommendation models are configured"
        )

    async def aclose(self) -> None:
        await self._backends.aclose()


__all__ = [
    "QUOTA_RESET_HOUR_UTC",
    "BackendBuilder",
    "ChainEntry",
    "RecsChain",
    "is_daily_quota",
    "next_quota_reset",
    "utcnow",
]
