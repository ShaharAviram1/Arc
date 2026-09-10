"""The recommendations page: read the last run, ask for a new one (FR-R1, FR-R5).

Two routes and no logic worth the name — the rules all live in
:mod:`arc.services.recs`, and this file's job is to turn five service
exceptions into the five status codes the client branches on:

* **429** — the daily limit (FR-R5). The body carries ``retry_after_seconds``
  as well as ``detail`` so the page can say "try again in three hours" without
  parsing a header, and the ``Retry-After`` header is sent too because that is
  what the status code means.
* **503 "not configured"** — nothing in the chain has a key
  (``GEMINI_API_KEY``, ``OPENROUTER_API_KEY``, ``ANTHROPIC_API_KEY``, one per
  provider). Not an error anybody can fix by retrying, and ``GET /api/recs``
  says ``configured: false`` up front so the client can hide the button rather
  than let somebody press it.
* **503 "declined"** — ``stop_reason == "refusal"``.
* **502** — the provider unreachable (every entry in the chain, in turn), or
  an answer that could not be used.
* **409** — nothing to recommend from: a fresh install before the first season
  sweep, or a user who has already listed everything Arc knows about.

``GET`` is deliberately not a run: FR-R5 stores runs precisely so that opening
the page costs nothing and only the refresh button spends the budget.

None of the wording here names a vendor. The backend is switchable
(``RECS_PROVIDER``), so a detail string that said "Claude" would be wrong on
the deployment that actually ships.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from arc.api.anime_schemas import AnimeSummary
from arc.api.deps import CatalogDep, CurrentUser, SessionDep, SettingsDep
from arc.config import Settings
from arc.models import RecRun, UserRole
from arc.services.recs import (
    DAILY_LIMIT,
    RecsEmptyPool,
    RecsFailed,
    RecsModel,
    RecsRateLimited,
    RecsRefused,
    RecsUnavailable,
    build_recs_model,
    remaining_today,
    run_recommendations,
)
from arc.services.recs.runs import anime_for_picks, is_pick, latest_run

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recs", tags=["recs"])

NOT_CONFIGURED = "Recommendations are not configured"
DECLINED = "The model declined this request"
EMPTY_POOL = "Nothing to recommend from: add or rate a few shows first"
UPSTREAM = "The recommendation service is unavailable; try again"

#: Longest mood prompt accepted (FR-R1). Long enough for "something short and
#: funny, ideally not a school setting, like Mushishi but less sad"; short
#: enough that it cannot be used to write the system prompt.
MAX_PROMPT_CHARS = 300


def now() -> datetime:
    """The clock the rate-limit window and a new run are stamped with.

    A function so a test can pin it — ten runs in twenty-four hours is an
    assertion about the present.
    """
    return datetime.now(UTC)


# --- Schemas ----------------------------------------------------------------


class RecRunCreate(BaseModel):
    """The refresh button's body: an optional mood prompt (FR-R1)."""

    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)

    @field_validator("prompt")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        """Whitespace-only is the same as leaving the box empty."""
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


class PickOut(BaseModel):
    """One recommendation: the show as a card, plus the argued case (FR-R4)."""

    anime: AnimeSummary
    case: str


class ContinuationOut(BaseModel):
    """One new entry in a franchise the caller already follows.

    Separate from :class:`PickOut` because it is a different claim: ``case`` is
    a model's argument about a show the user has never seen, ``because`` is a
    fact about one they have. The client renders them as two sections.
    """

    anime: AnimeSummary
    because: str


class RecRunOut(BaseModel):
    """One stored run, as the page renders it."""

    id: int
    prompt: str | None
    created_at: datetime
    model: str | None
    #: How many titles the model chose from. Shown as "picked 4 of 40".
    candidate_count: int
    picks: list[PickOut]
    #: Sequels, films and spin-offs of shows already on the caller's list. Not
    #: the model's work and not counted in ``candidate_count``.
    continuations: list[ContinuationOut] = []


class ChainEntryOut(BaseModel):
    """One ``(provider, model)`` the chain may try, and whether it can today."""

    provider: str
    model: str
    #: False while this entry is on cooldown because its daily free-tier quota
    #: is spent (:mod:`arc.services.recs.chain`).
    available: bool


class RecsIndex(BaseModel):
    """What the page needs on load: the last run and the day's budget.

    ``chain`` is **admin-only and omitted entirely for everyone else** — it
    names the providers and models a deployment pays for, which is operator
    information rather than product information. The omission is done by
    ``response_model_exclude_unset``: every other field is set explicitly on
    every path, so leaving ``chain`` at its default is what drops it from the
    body. Anything added here must be set explicitly too, or it will vanish.
    """

    run: RecRunOut | None
    remaining_today: int
    limit_per_day: int = DAILY_LIMIT
    #: False when no entry in the chain has a key — the button is pointless.
    configured: bool
    chain: list[ChainEntryOut] | None = None


async def _render(session: SessionDep, run: RecRun) -> RecRunOut:
    """A run with its picks resolved to cards.

    A pick whose anime row no longer exists is dropped rather than rendered
    hollow: rows can be pruned, and a card with no title is worse than one
    fewer card. The client's ``ListStatusControl`` renders from
    ``anime.list_status``, which is why the statuses are fetched alongside.
    """
    rows, statuses = await anime_for_picks(session, run)
    picks: list[PickOut] = []
    continuations: list[ContinuationOut] = []
    for entry in run.picks or ():
        anime = rows.get(int(entry.get("anime_id", 0)))
        if anime is None:
            continue
        card = AnimeSummary.from_anime(anime, statuses.get(anime.id))
        if is_pick(entry):
            picks.append(PickOut(anime=card, case=str(entry.get("case") or "")))
        else:
            continuations.append(
                ContinuationOut(anime=card, because=str(entry.get("because") or ""))
            )
    return RecRunOut(
        id=run.id,
        prompt=run.prompt,
        created_at=run.created_at,
        model=run.model,
        candidate_count=len(run.candidates or ()),
        picks=picks,
        continuations=continuations,
    )


# --- The model --------------------------------------------------------------


#: Cached in place of a model on a deployment that has no key. Without it the
#: "not configured" branch would rebuild — and log — on every ``GET /api/recs``,
#: which for a stack that never wanted recommendations is a line per page view
#: forever. ``None`` cannot do the job: it is also "nothing cached yet".
UNCONFIGURED = object()


def recs_model_for(request: Request, settings: Settings) -> RecsModel | None:
    """The app's recommendation model, or ``None`` when its key is unset.

    Which backend that is comes from ``RECS_PROVIDER``
    (:func:`arc.services.recs.build_recs_model`); this router cannot tell them
    apart and does not try.

    Cached on ``app.state`` for the same reason the catalogue is: it owns an
    HTTP client, and one per request would be one connection pool per request.
    The lifespan closes it. Tests set ``app.state.recs_model`` to a fake, which
    is why the attribute is consulted before the settings are.
    """
    existing = getattr(request.app.state, "recs_model", None)
    if existing is UNCONFIGURED:
        return None
    if existing is not None:
        model: RecsModel = existing
        return model
    built = build_recs_model(settings)
    request.app.state.recs_model = built if built is not None else UNCONFIGURED
    return built


# --- Routes -----------------------------------------------------------------


@router.get(
    "",
    response_model=RecsIndex,
    response_model_exclude_unset=True,
    summary="The newest run and today's budget (FR-R5)",
)
async def index(
    request: Request, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> RecsIndex:
    run = await latest_run(session, user_id=user.id)
    model = recs_model_for(request, settings)
    body = RecsIndex(
        run=await _render(session, run) if run is not None else None,
        remaining_today=await remaining_today(session, user_id=user.id, now=now()),
        limit_per_day=DAILY_LIMIT,
        configured=model is not None,
    )
    if user.role is UserRole.ADMIN:
        body.chain = _chain_out(model)
    return body


def _chain_out(model: RecsModel | None) -> list[ChainEntryOut]:
    """The chain's state, if the configured model is one.

    ``getattr`` rather than an isinstance check: a test injects a fake, and a
    fake with no ``status`` should produce an empty list rather than a 500.
    """
    status = getattr(model, "status", None)
    if not callable(status):
        return []
    return [
        ChainEntryOut(provider=row["provider"], model=row["model"], available=row["available"])
        for row in status()
    ]


@router.post(
    "/runs",
    response_model=RecRunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Ask the model for 3–5 picks (FR-R1, FR-R3, FR-R4)",
    responses={
        409: {"description": EMPTY_POOL},
        422: {"description": f"prompt longer than {MAX_PROMPT_CHARS} characters"},
        429: {"description": "daily limit of 10 runs reached"},
        502: {"description": UPSTREAM},
        503: {"description": f"{NOT_CONFIGURED} / {DECLINED}"},
    },
)
async def create(
    body: RecRunCreate,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    catalog: CatalogDep,
) -> RecRunOut | Response:
    model = recs_model_for(request, settings)
    if model is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED)

    try:
        run = await run_recommendations(
            session,
            catalog,
            model,
            user_id=user.id,
            prompt=body.prompt,
            now=now(),
        )
    except RecsRateLimited as exc:
        # Not an HTTPException: the client renders the wait, and a plain
        # ``detail`` string would make it parse a sentence for a number.
        seconds = max(1, math.ceil(exc.retry_after_seconds))
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": str(exc), "retry_after_seconds": seconds},
            headers={"Retry-After": str(seconds)},
        )
    except RecsEmptyPool as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=EMPTY_POOL) from exc
    except RecsRefused as exc:
        log.warning(
            "recommendation refused",
            extra={"user_id": user.id, "stop_details": str(exc.stop_details)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=DECLINED
        ) from exc
    except (RecsUnavailable, RecsFailed) as exc:
        log.warning("recommendation failed", extra={"user_id": user.id, "error": str(exc)})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=UPSTREAM) from exc

    await session.commit()
    return await _render(session, run)


__all__ = [
    "DECLINED",
    "EMPTY_POOL",
    "MAX_PROMPT_CHARS",
    "NOT_CONFIGURED",
    "UNCONFIGURED",
    "UPSTREAM",
    "ChainEntryOut",
    "ContinuationOut",
    "PickOut",
    "RecRunCreate",
    "RecRunOut",
    "RecsIndex",
    "now",
    "recs_model_for",
    "router",
]
