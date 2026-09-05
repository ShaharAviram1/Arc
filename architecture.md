# Arc — Architecture

> Living document. Update whenever the stack, a component boundary, or an
> integration changes. Last updated: 2026-09-05.
> Companions: [spec.md](spec.md), [roadmap.md](roadmap.md), [CLAUDE.md](CLAUDE.md).

## 1. Stack at a glance

| Layer | Choice | Why |
|---|---|---|
| Server language | Python 3.14 (latest; `uv`-managed) | Best ecosystem for the hard parts: anime filename parsing (`anitopy`), ffmpeg orchestration, qBittorrent client libs, Anthropic SDK. |
| Web framework | FastAPI + Uvicorn | Async, typed, auto-docs; easy streaming/range responses. |
| ORM / migrations | SQLAlchemy 2.x (async) + Alembic | Standard, typed models, migration history. |
| Database | PostgreSQL 18 | Multi-user, concurrent API + worker processes, JSONB for parse results and rec runs. |
| Background jobs | Postgres-backed job table + dedicated worker process (same codebase) | No Redis to run. Jobs are rows claimed with `SELECT … FOR UPDATE SKIP LOCKED`. Scheduler runs inside the worker (APScheduler). |
| Media prep | ffmpeg / ffprobe (subprocess) | Transcode to H.264/AAC HLS with burned-in subtitles. |
| Torrent client | qBittorrent (sidecar container) via Web API | Battle-tested, own debug UI, category/save-path control. |
| Catalogue | AniList GraphQL API (no auth) | Titles, relations, airing schedule, cover art. |
| List sync | MyAnimeList API v2 (OAuth 2.0 PKCE) | Where users' lists live. |
| Releases | Nyaa RSS search feed | Public, no account. |
| LLM | Anthropic API, model `claude-opus-5`, adaptive thinking, structured outputs | Recommendations with argued cases; match suggestions for unsure files. |
| Client | React 19 + TypeScript 6 + Vite | SPA; rich player ecosystem. |
| Client data | TanStack Query + fetch | Cache/invalidation for API data. |
| Player | hls.js (native HLS on Safari) | HLS playback in browser. |
| Styling | Tailwind CSS | Fast, consistent, dark-first UI. |
| Auth | Session cookies (HTTP-only, Secure, SameSite=Lax), Argon2 password hashes | Simple, robust for a small user base. |
| Deploy | Docker Compose: `api`, `worker`, `db`, `qbittorrent`, `caddy` | One-box deploy on any VPS that permits torrent traffic. Caddy terminates TLS and serves the built client. |
| Tests | pytest + pytest-asyncio, httpx test client, Vitest for client | ffmpeg and qBittorrent mocked in CI. |
| Lint/format | ruff, mypy (strict on core packages), typescript-eslint (type-checked rules) + prettier | TypeScript stays on 6.x until typescript-eslint supports 7 (tracked in roadmap M15). |

## 2. System diagram

```
                     ┌──────────────┐
   browser ───HTTPS──▶    caddy     ├── static client (built React)
                     └──────┬───────┘
                            │ /api, /media
                     ┌──────▼───────┐        ┌──────────────┐
                     │   api        │◀──────▶│   postgres   │
                     │  (FastAPI)   │        └──────▲───────┘
                     └──────┬───────┘               │ jobs, state
                            │ shared volume         │
                     ┌──────▼───────┐        ┌──────┴───────┐
                     │ media volume │◀──────▶│   worker     │──▶ ffmpeg
                     │ /data/…      │        │ (scheduler + │──▶ AniList
                     └──────▲───────┘        │  job runner) │──▶ MAL
                            │                └──────┬───────┘──▶ Nyaa RSS
                     ┌──────┴───────┐               │ Web API   ──▶ Anthropic
                     │ qbittorrent  │◀──────────────┘
                     └──────────────┘
```

Two Python processes share one codebase and one database:

- **api** — HTTP only. Auth, CRUD, progress reporting, media serving. Never
  runs ffmpeg or talks to Nyaa directly; it enqueues jobs.
- **worker** — claims jobs from the `jobs` table and runs the scheduler
  (periodic AniList refresh, Nyaa polling, MAL re-import, retention sweep,
  qBittorrent polling). Can be scaled to more than one instance safely
  because of `SKIP LOCKED`.

### Job queue mechanics (M1)
- Claim: one statement, `SELECT … WHERE status='pending' AND run_after <= now()
  ORDER BY priority, run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED`; the same
  transaction sets `running`, `locked_by`, `locked_at`, `started_at`, and
  increments `attempts` (so a crash still burns an attempt).
- Handler runs in its own session; on success the runner commits handler work
  then marks `done`; on exception it rolls back handler work, stores the error
  (truncated, with traceback tail) and either reschedules with backoff
  (10 s, 60 s, 300 s, then ×5, capped at 1 h) or marks `failed` once
  `attempts >= max_attempts`. Unknown job type → `failed` immediately.
- Dedupe: optional `dedupe_key` stored in `payload`; an enqueue with a key
  that matches a `pending`/`running` job of the same type returns that job.
- Crash recovery: `requeue_stale` (at worker start and every 5 min) returns
  `running` jobs whose lock is older than `WORKER_STALE_AFTER` to `pending`,
  or to `failed` if attempts are exhausted. Shutdown drains in-flight jobs up
  to `WORKER_DRAIN_TIMEOUT`, then cancels and resets them to `pending`.
- API: `POST /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs` (admin-only
  from M2). Success status is `done`.

## 3. Repository layout

```
arc/
  server/
    pyproject.toml
    alembic/                     migrations
    arc/
      main.py                    FastAPI app factory
      worker.py                  job loop + scheduler entrypoint
      config.py                  pydantic-settings (env)
      db/                        engine, session, base models
      models/                    SQLAlchemy models (one file per aggregate)
      api/                       routers: auth, users, anime, list, episodes,
                                 progress, media, mal, schedule, recs, admin
      services/
        anilist/                 GraphQL client + cache refresh
        mal/                     OAuth, import, write log, sync rules
        nyaa/                    RSS query builder, candidate parsing, ranking
        qbit/                    qBittorrent Web API client
        library/                 file watcher, parser (anitopy), matcher, scoring
        media/                   ffprobe, transcode plan, HLS packaging
        acquisition/             want computation, window logic
        retention/               cleanup rules
        recs/                    candidate pool, Claude prompt, schema
        jobs/                    job table, claim/run/retry, handlers registry
      core/                      security, sessions, errors, logging
    tests/
      fixtures/release_names.txt corpus of real filenames + expected parse
  client/
    package.json, vite.config.ts   (Tailwind v4: theme lives in src/index.css, no tailwind.config)
    src/
      api/                       typed client (generated from OpenAPI)
      pages/                     Home, Schedule, Search, Show, Player, Mal,
                                 Recs, Review, Admin, Login, Invite
      components/
      player/                    hls.js wrapper, progress reporter
      lib/                       auth context, query hooks
  deploy/
    docker-compose.yml         production stack
    docker-compose.dev.yml     override: db + qbittorrent on localhost for local dev
    Caddyfile
  scripts/
    dev.sh                     make dev: compose db+qbit, then api + worker + vite
  Makefile                     dev / test / lint / fmt / migrate / revision / up / down
  .env.example                 at repo root (compose is invoked with --env-file .env)
  spec.md
  architecture.md
  README.md
```

## 4. Data model (tables)

| Table | Key columns |
|---|---|
| `users` | id, email (unique on lower(email)), password_hash, role, is_active, timezone, created_at |
| `invites` | id, token_hash, email (optional), created_by, expires_at, used_at |
| `sessions` | id (opaque token hash), user_id, expires_at, user_agent |
| `anime` | id (AniList id, PK), mal_id, title_romaji, title_english, title_native, synonyms (JSONB), format, episodes, status, season, season_year, cover_url, banner_url, genres (array), tags (JSONB), studio, relations (JSONB), next_airing (JSONB), refreshed_at |
| `episodes` | id, anime_id, number, title, air_at, state (enum, §6 of spec), state_changed_at, unavailable_reason |
| `media_files` | id, episode_id (nullable until matched), path (unique), size (BIGINT), parsed (JSONB), match_confidence, match_candidates (JSONB), review_state, llm_suggestion (JSONB), created_at |
| `renditions` | id, episode_id (unique), dir, playlist_path, duration, width, height, subtitle_lang, audio_lang, ready_at |
| `list_entries` | user_id, anime_id (PK pair), status, progress, score, updated_at, updated_by (arc/mal), mal_synced_at, mal_dirty |
| `watch_progress` | user_id, episode_id (PK pair), position_s, duration_s, completed, completed_at (set once, drives retention grace), updated_at |
| `mal_links` | user_id (PK), mal_username, access_token_enc, refresh_token_enc, expires_at, last_import_at |
| `mal_write_log` | id, user_id (CASCADE), anime_id (RESTRICT: audit rows must never be deleted by cache pruning), field, old_value, new_value, cause (watch/manual/revert), status, error, created_at |
| `wants` | user_id, episode_id (PK pair), created_at, dropped_at, drop_reason |
| `torrents` | id, episode_id, info_hash (unique), magnet, title, group, resolution, seeders, trusted, qbit_state, progress, added_at, completed_at |
| `jobs` | id, type, payload (JSONB), status, priority (lower runs first), attempts, max_attempts, run_after, locked_by, locked_at, last_error, created_at, started_at, finished_at |
| `rec_runs` | id, user_id, prompt, candidates (JSONB), picks (JSONB), model, created_at |
| `settings` | key (PK), value (JSONB) — admin-editable rules (preferred_groups, resolution, look_ahead_n, grace_days_g, unwatched_days_d, sub_lang, audio_lang) |

## 5. Key flows

### 5.1 Acquisition
1. Scheduler job `compute_wants` (every 15 min and on any list/progress
   change): for each `list_entries` row in watching/planned, take the user's
   furthest completed episode number `p`; want episodes `p+1 … p+N` that
   have aired (or air today). Upsert into `wants`. Remove wants for shows now
   dropped/completed. Apply rule FR-T2 (drop stale wants).
2. For each episode with ≥1 active want and state `not_wanted`/`unavailable`
   (retry due) → state `wanted`, enqueue `search_release`.
3. `search_release`: build Nyaa RSS queries (romaji + english title, episode
   `- 07` / `E07` variants, resolution), fetch, parse each title with the
   parser, keep candidates whose title matches the anime (fuzzy ≥ threshold)
   and episode number equals; rank by rules; pick top; write `torrents`; add
   to qBittorrent with category `arc` and save path `/data/downloads/<episode
   id>/`; state `downloading`. No candidate → schedule retry per FR-A6.
4. `poll_qbit` (every 60 s): sync progress; on completion → state
   `downloaded`, enqueue `ingest_file` with the largest video file.

### 5.2 Ingest and match
1. `ingest_file`: create `media_files`, ffprobe it, parse filename.
2. `match_file`: candidates = expected episode (if Arc downloaded it, prior
   0.6) ∪ local anime cache fuzzy hits ∪ AniList search hits. Score =
   weighted title similarity (token-set ratio over romaji/english/synonyms),
   episode plausibility (number ≤ episode count, season alignment), year and
   format agreement, group/prior. Confidence ≥ 0.85 → link, state `matched`,
   enqueue `transcode`. Otherwise `review_state = pending`; optionally enqueue
   `llm_suggest_match`, which asks Claude for the most likely candidate + one
   line reason using structured output; stored for the review UI only.
3. The parser and scorer are pure functions with a corpus-driven test suite.

### 5.3 Transcode
1. `transcode` (priority = how soon a user will reach it): choose subtitle
   track (configured language, prefer ASS over SRT), choose audio track
   (Japanese default). Run
   `ffmpeg -i src -map v:0 -map a:<idx> -vf subtitles=src:si=<sub idx>
   -c:v libx264 -preset veryfast -crf 20 -c:a aac -b:a 160k -f hls
   -hls_segment_type fmp4 -hls_time 6 -hls_playlist_type vod …`
   into `/data/renditions/<episode id>/`. Parse ffmpeg progress for %.
2. Success → `renditions` row, episode `ready`. Failure → `failed` with
   stderr tail; up to 2 automatic retries, then manual.
3. Fonts: ship a fonts volume; extract attached fonts from MKV
   (`-dump_attachment`) before rendering so ASS styling renders correctly.

### 5.4 Streaming
- `GET /media/{episode_id}/index.m3u8` and `/media/{episode_id}/{segment}`
  are served by the API with session auth; playlists are rewritten so segment
  URLs stay under `/media/…`. Caddy proxies `/media` to the API (no direct
  static exposure). Range requests supported on segments.
- Client uses hls.js with `xhrSetup` sending credentials.

### 5.5 Progress and MAL writes
1. `POST /progress` {episode_id, position, duration} every 10 s and on
   pause/seek/unload. Server upserts `watch_progress`.
2. If position/duration ≥ 0.90 and not yet completed: set completed; if
   episode.number > list_entry.progress → progress = number, `updated_by =
   arc`, `mal_dirty = true`, enqueue `mal_push` for (user, anime).
3. `mal_push`: read the current MAL entry, write only the dirty fields, log
   old/new to `mal_write_log`, clear `mal_dirty`. Never lowers progress from
   an automatic event. Status/score writes come from the explicit list
   endpoints via the same path.
4. `mal_import` (on link and every 6 h): pull full list; for each entry, if
   `mal_dirty` is false → overwrite local from MAL; if dirty → compare
   `updated_at` vs MAL's `updated_at`, newest wins, conflict logged.
5. Revert endpoint replays `old_value` through `mal_push` with cause
   `revert`.

### 5.6 Recommendations
1. Build candidate pool (≤ 40): current season (top by popularity), relations
   of the user's completed/high-scored shows, popular in top-3 genres, minus
   anything on the user's list except planned.
2. Call Claude (`claude-opus-5`, `thinking: {type: "adaptive"}`,
   `output_config.format` JSON schema `{picks: [{anilist_id, title, case}]}`,
   3–5 picks, server-side `fallbacks: "default"` enabled) with a system prompt
   describing the task and a user message containing the history summary, the
   prompt, and the candidate pool with synopsis/genres/tags. Streaming used
   to avoid timeouts. Handle `stop_reason == "refusal"` gracefully.
3. Persist `rec_runs`; client renders picks with covers and add-to-planned.
   Rate limit 10/user/day.

### 5.7 Retention
`retention_sweep` (hourly): for each episode in `ready`/`downloaded`, if no
active wants remain and `max(completed_at of former wanters) + G days <
now` (or no wanter ever existed and file age > G) → delete rendition and
source dirs, remove torrent from qBittorrent (with files), state
`not_wanted`. Stale wants dropped per FR-T2 in `compute_wants`.

## 6. External integrations

| Service | Auth | Rate/limits | Notes |
|---|---|---|---|
| AniList GraphQL `https://graphql.anilist.co` | none | 90 req/min | Queries: `Media` search, `Page(media)` seasonal, `AiringSchedule`. Cache aggressively in `anime`. |
| MAL API v2 `https://api.myanimelist.net/v2` | OAuth 2.0 PKCE (plain), client id + secret in env | modest | Endpoints: `users/@me/animelist`, `anime/{id}/my_list_status` (PATCH/DELETE). Tokens encrypted with Fernet key from env. |
| Nyaa RSS `https://nyaa.si/?page=rss&q=…&c=1_2&f=0` | none | be polite: ≤1 req/2 s, cache 10 min | `c=1_2` = Anime English-translated. `f=2` for trusted only (used as a tie-break, not a filter). |
| qBittorrent Web API | local user/pass in env | n/a | `auth/login`, `torrents/add`, `torrents/info?category=arc`, `torrents/delete`. |
| Anthropic API | `ANTHROPIC_API_KEY` | n/a | Python SDK `anthropic`; model `claude-opus-5`; structured outputs; streaming. |

## 7. Security

- Cookie sessions, CSRF protection on state-changing requests (SameSite=Lax +
  origin check). Argon2id hashes. Login rate-limited per IP.
- Invite tokens: random 32 bytes, stored hashed, 7-day expiry, single use.
- All `/api` and `/media` routes require a session; admin routes check role.
- MAL tokens and any stored secrets encrypted at rest; app secrets via env.
- Media directories not served statically; paths derived from ids, never
  from user input.
- qBittorrent Web UI bound to the internal Docker network only.

## 8. Deployment

`deploy/docker-compose.yml` services:

- `caddy` — TLS (Let's Encrypt), serves `client/dist`, proxies `/api` and
  `/media` to `api`.
- `api` — `uvicorn arc.main:app`, 2 workers.
- `worker` — `python -m arc.worker`, 1 instance (raise for more transcode
  parallelism; ffmpeg concurrency capped by `MAX_TRANSCODES`).
- `db` — postgres:18 with a volume mounted at `/var/lib/postgresql` (18's layout).
- `qbittorrent` — linuxserver/qbittorrent, `/data/downloads` volume shared
  with api/worker, Web UI on internal network only.

Volumes: `/data/downloads`, `/data/renditions`, `/data/fonts`, `pgdata`.

Host requirements (hosting itself is **deferred**, see spec §9): Linux VPS
that permits BitTorrent traffic, ≥ 4 vCPU (software x264 at `veryfast`
transcodes a 24-min 1080p episode in roughly real time on 2 cores),
≥ 200 GB disk, persistent volumes. Hardware encode (VAAPI/NVENC) can be
enabled later by changing the ffmpeg encoder flag.

Local dev: `make dev` (see `scripts/dev.sh`) starts `db` and `qbittorrent`
via the dev compose override, then runs `api` and `worker` with hot reload
and `vite dev` for the client with a proxy to the API. Compose is always
invoked with `--env-file .env` because `.env` lives at the repo root.
Production: `make up` (builds images; `ENV=prod`, `DATABASE_URL`, and
`QBIT_URL` are set in the compose file, so `.env` never needs container
hostnames).

## 9. Configuration (env)

App settings (read by `arc/config.py`; secrets are `SecretStr`, never
logged): `ENV` (dev|prod), `LOG_LEVEL`, `PUBLIC_URL`, `DATABASE_URL`,
`SECRET_KEY`, `FERNET_KEY`, `MAL_CLIENT_ID`, `MAL_CLIENT_SECRET`,
`MAL_REDIRECT_URI`, `ANTHROPIC_API_KEY`, `LLM_MATCH_SUGGESTIONS` (bool),
`QBIT_URL`, `QBIT_USER`, `QBIT_PASS`, `DATA_DIR`, `MAX_TRANSCODES`,
`BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`, `WORKER_CONCURRENCY`
(default 2), `WORKER_POLL_INTERVAL` (seconds, default 1), `WORKER_DRAIN_TIMEOUT`
(seconds to wait for in-flight jobs on shutdown, default 30),
`WORKER_STALE_AFTER` (seconds before a `running` job with a dead worker is
requeued, default 900; must exceed the longest expected job).

Deploy-only (compose/Caddy, not read by the app): `PUBLIC_HOST`,
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `PUID`, `PGID`, `TZ`.
`.env` holds host-side values (`localhost` URLs) used by `make dev`, tests,
and processes run directly; inside Compose the `api`/`worker` services
override `DATABASE_URL` and `QBIT_URL` with the service hostnames
(`db`, `qbittorrent`) structurally, alongside `ENV=prod`.

Rule values (N, G, D, groups, resolution,
languages) live in the `settings` table and are editable by admin, seeded by
the initial migration. `MAX_TRANSCODES` is host capacity and lives only in
env (it is not in the settings table).

## 10. Testing strategy

- Parser/matcher: corpus of ≥ 200 real release names with expected
  (title, episode, season, group, resolution); assert confidence tiers.
- Acquisition window: property tests over progress/N/aired combos.
- MAL rules: table-driven tests proving no write occurs without a
  user-originated event, progress never lowered automatically, revert
  round-trips.
- Retention: time-travel tests with frozen clock.
- API: httpx AsyncClient against a temp Postgres (testcontainers) or SQLite
  fallback for fast runs.
- Media: ffmpeg invoked on a 5-second fixture in a dedicated test marked
  `slow`; mocked elsewhere.

## 11. Decision log

- 2026-09-05 — Chose Python/FastAPI over Node/Go for parsing and media
  tooling; React/Vite client for player ecosystem.
- 2026-09-05 — Postgres-backed job table instead of Redis/Celery to keep the
  deploy to one box with fewer services.
- 2026-09-05 — Full transcode with burned-in subs for every file (owner's
  preference), HLS fMP4 output.
- 2026-09-05 — qBittorrent sidecar over embedded libtorrent.
- 2026-09-05 — Claude `claude-opus-5` with structured outputs for recs and
  match suggestions; server-side fallbacks enabled.
- 2026-09-05 — Hosting provider deferred; design constrained to a single
  Docker Compose host.
- 2026-09-05 — M0 landed. `.env.example` lives at the repo root (compose
  invoked with `--env-file .env`); `scripts/dev.sh` + Makefile added;
  Tailwind v4 is config-less; stdlib logging (JSON in prod) instead of
  structlog; `uv` manages Python, `pnpm` the client; secrets are
  `SecretStr`; compose sets `ENV=prod` structurally.
- 2026-09-05 — Stack moved to latest: Python 3.14, Postgres 18, React 19.
  TypeScript 7 was tried and reverted to 6.x: typescript-eslint refuses TS 7,
  which left the client unlinted; the user chose full lint coverage
  (incl. type-aware rules like no-floating-promises) over the newer
  compiler. Revisit when typescript-eslint supports 7.
- 2026-09-05 — M1 schema decisions: BIGINT identity PKs (anime uses the
  AniList id; composite PKs for list_entries/watch_progress/wants); enums are
  `StrEnum` stored as VARCHAR(32), not native PG enums; JSONB for JSON;
  `jobs` gained `max_attempts` and `started_at`; `watch_progress.completed_at`
  added for retention; unique on lower(email), media_files.path,
  torrents.info_hash; `mal_write_log.anime_id` is RESTRICT; `max_transcodes`
  removed from the settings seed. Extra indexes: anime.mal_id,
  rec_runs(user_id, created_at). Postgres-backed tests run against the dev
  compose db (`arc_test`), gated by the `pg` marker.
- 2026-09-05 — M1 job queue: Postgres `SKIP LOCKED` claim, attempts counted
  at claim time, dedupe via `payload.dedupe_key` (no column), backoff
  10/60/300 s then ×5 capped at 1 h, `WORKER_*` settings, `/api/jobs`
  endpoints (auth deferred to M2), success status named `done`.
- 2026-09-05 — Dropped `DEV_DATABASE_URL`/`DEV_QBIT_URL`: `.env` now holds
  localhost URLs and Compose sets container hostnames in `environment:`.
  Running the API or worker directly from `server/` needs no exports.
