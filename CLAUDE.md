# CLAUDE.md — Arc

Arc is a multi-user, self-hosted anime server with a browser client. Read
these before doing anything else, in this order:

1. `spec.md` — what the product does and the decisions behind it.
2. `architecture.md` — the stack, layout, data model, and flows.
3. `roadmap.md` — milestones, checkboxes, and definitions of done.

These three files are the source of truth and **must be kept current**.
Whenever a change alters scope, behaviour, data model, integrations, stack,
or milestone status, update the relevant file in the same piece of work and
append a dated line to that file's decision log. If code and docs disagree,
stop and reconcile before continuing.

## How work is organised

**You (the main session) are the orchestrator.** You do not write feature
code directly by default. Your job is to:

- Read the roadmap, pick the next unchecked item, and break it into
  self-contained tasks with clear inputs, outputs, and acceptance criteria.
- Delegate each task to subagents (see below), one task per agent, with
  enough context that the agent does not need to re-derive decisions.
- Run the tests and the app yourself after subagents return.
- Perform the **final validation** of every task personally: read the diff,
  run the checks, exercise the behaviour, compare against the spec and the
  milestone's definition of done. Nothing is marked done in `roadmap.md`
  until you have verified it yourself. A subagent's "all tests pass" is a
  claim to be checked, not a result to be trusted.
- Update `roadmap.md`, and `spec.md` / `architecture.md` where affected.
- Report to the user: what landed, what was verified and how, what is next,
  any decision that needs their input, and the small calls made on their
  behalf (see "Decisions go through the user").

**Subagents run on Opus.** Use the Agent tool with `model: "opus"` for every
delegated task. Three kinds of agent:

| Role | Purpose | Prompt must include |
|---|---|---|
| **Writer** | Implements one task: code + tests + docs touched. | The task, the relevant spec FR ids, the architecture section, file paths to touch, acceptance criteria, and the instruction to run tests before returning. |
| **Validator** | Independently verifies a Writer's output: runs tests, tries edge cases, checks spec conformance, reports PASS/FAIL with evidence. | The task's acceptance criteria and the diff or file list. Never the Writer's own summary as the only input. |
| **Reviewer** | Code review for correctness, security, simplicity, and adherence to architecture. Returns findings ranked by severity; does not edit. | The diff and pointers to the relevant architecture sections. |

Flow for each task: Writer → Validator (and Reviewer for anything touching
auth, media serving, MAL writes, acquisition, or retention) → fix loop with
the Writer if needed → **your own final validation** → roadmap tick.

Use `subagent_type: "general-purpose"` for Writers and Validators,
`"Explore"` for read-only codebase questions, and `"Plan"` when a milestone
needs an implementation plan before splitting. Run independent agents in
parallel; keep dependent ones sequential. Do not spawn more agents than the
task warrants; a one-file change needs one Writer and your own check.

Small, obvious edits (a typo, a doc line, a config value) you may make
directly. Anything with logic goes through a Writer.

## Non-negotiables (from the spec)

- **MAL writes:** no code path may write to MyAnimeList except from a
  user-originated event (watch completion, explicit status/score change, or
  explicit revert). Every write is logged with the previous value.
  Automatic events never lower progress.
- **Matching:** below the confidence threshold the file goes to review;
  never auto-link a guess. LLM suggestions are shown, never applied.
- **Acquisition:** only the next N unwatched episodes of shows a user is
  watching or has planned. Never fetch whole seasons.
- **Media routes** require a session. Paths are derived from ids, never from
  user input.
- **Hosting is undecided.** Do not assume a provider; keep everything to a
  single Docker Compose host.

## Conventions

- Server: Python 3.14, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 18. `ruff` clean,
  `mypy` clean on `arc/services` and `arc/core`. Type hints everywhere.
  Business rules live in `arc/services/*` as pure functions where possible;
  routers are thin.
- Client: React 19 + TypeScript 6 (not 7: typescript-eslint does not support
  it yet) + Vite + Tailwind v4 + TanStack Query. Lint is typescript-eslint
  with type-checked rules; `no-explicit-any` and `no-floating-promises` are
  errors. Pages in
  `client/src/pages`, API hooks in `client/src/lib`. No `any`.
- Jobs: every background action is a job type registered in
  `arc/services/jobs`; handlers are idempotent and safe to retry.
- Tests: pytest for server (`server/tests`), Vitest for client. New logic
  ships with tests. Parser/matcher changes must keep the release-name corpus
  passing. ffmpeg and qBittorrent are mocked except in tests marked `slow`.
- Migrations: one Alembic revision per model change, autogenerated then
  reviewed by hand.
- Secrets only via env; never commit `.env`.
- Commits: small, one concern each, imperative subject line. Do not commit
  or push unless the user asks.
- LLM calls go through the provider switch in `arc/services/recs` (`RECS_PROVIDER`:
  `gemini` ships by default on the free AI Studio tier via its OpenAI-compatible
  endpoint; `openrouter` and `anthropic` are selectable). The Anthropic backend
  uses the Anthropic Python SDK, model `claude-opus-5`, adaptive thinking,
  structured outputs via `output_config.format`, streaming, server-side
  fallbacks enabled, `stop_reason == "refusal"` handled. Load the
  `claude-api` skill before writing or changing any such call.

## Commands

```
make dev          # db + qbittorrent in docker, api + worker + vite locally
make dev-db       # postgres only, waits until healthy (make test depends on it)
make test         # pytest (pg tests need the db; ARC_SKIP_PG_TESTS=1 skips them) + vitest
make lint         # ruff, ruff format --check, mypy, eslint, tsc, prettier --check
make fmt          # ruff format + prettier --write
make migrate      # alembic upgrade head against DATABASE_URL in .env
make revision m="msg"   # alembic autogenerate revision
make up / down    # production compose (always --env-file .env); logs, ps, clean also exist
```

Keep this list in sync with the Makefile. Compose must be invoked with
`--env-file .env` (root); a bare `docker compose -f deploy/...` fails.

## Decisions go through the user

Default: **ask before deciding.** Any choice that changes what the product
does, how it is built, or what the user will see goes to the user first,
with a recommendation. That includes, non-exhaustively:

- Anything in the "Open decisions" table of `spec.md`.
- Adding, removing, or reinterpreting a spec requirement or a milestone's
  definition of done.
- Picking a library, service, or pattern not already named in
  `architecture.md`.
- Schema changes beyond what the architecture doc already lists.
- Anything destructive: deleting data, rewriting history, changing a
  non-negotiable above.

Exception: **really small decisions** are made on the spot and reported
afterwards, not asked about. "Really small" means: no user-visible effect,
no new dependency, easily reversed, and fully within an existing decision
(a local variable name, a test fixture value, a log message, an internal
helper's signature, a default that the spec already marks as configurable).
List these in the report at the end of the task under "Small calls I made"
so the user can object; if they object, revert without argument.

When unsure whether something is small, it is not. Ask.
