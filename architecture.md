# Arc — Architecture

> Living document. Update whenever the stack, a component boundary, or an
> integration changes. Last updated: 2026-09-11.
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
| Lint/format | ruff, mypy (strict on core packages), typescript-eslint (type-checked rules) + prettier | TypeScript stays on 6.x until typescript-eslint supports 7 (tracked in roadmap M16). |

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
- Priorities (lower first): `mal_push` 10, `transcode` 0–500 by user
  distance, `poll_qbit` 50, `match_file` and the catalogue sweep
  schedulers 100, `compute_wants` 120, `search_release` 150, imports,
  catalogue refreshes and library scans 200 — so a write a person is
  waiting for never queues behind bulk work. Bulk jobs hold a slot for
  about two minutes at most (import chunks of 50 spaced 2 s).
- API: `POST /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs` (admin-only
  from M2). Success status is `done`. M14 adds the admin queue view's controls:
  `GET /api/jobs/summary` (depths per status, pending work per type, and the
  worker's liveness), `POST /api/jobs/{id}/retry` (`failed`/`cancelled` →
  `pending`, attempts reset, `last_error` kept) and `POST /api/jobs/{id}/cancel`
  (`pending` → `cancelled`; a `running` job cannot be stopped safely from
  another process, so that is a 409). Both are status-guarded UPDATEs in
  `arc/services/jobs/queue.py` (`WHERE id = … AND status IN (…)`, 409 when no row
  matches), so a worker claiming the row between the read and the write cannot
  end up with executing work marked cancelled; the worker's next poll acts on
  whatever the table then says.
- The worker's liveness file (`$DATA_DIR/worker.heartbeat`, rewritten every
  30 s, stale after 90 s) lives in `arc/services/jobs/heartbeat.py` so that the
  API can `stat` it without importing the worker; `arc.worker` re-exports the
  names the container healthcheck (`python -m arc.worker --check`) uses.

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
        recs/                    candidate pool, continuations, model prompt,
                                 schema, provider chain
        jobs/                    job table, claim/run/retry, handlers registry,
                                 worker heartbeat (read by the API's summary)
        settings.py              validation + reads/writes of the rules table
      core/                      security, sessions, errors, logging
    tests/
      fixtures/release_names.txt corpus of real filenames + expected parse
  client/
    package.json, vite.config.ts   (Tailwind v4: theme lives in src/index.css, no tailwind.config)
    src/
      api/                       typed client (generated from OpenAPI)
      pages/                     Home, Schedule, Search, Show, Player, Mal,
                                 Recs, Review, Admin, Login, Invite
      components/                Layout (the app shell), route guards, shared
                                 pieces (CoverThumb, ListStatusControl, …)
        ui/                      design primitives (M15): Artwork, Button,
                                 Chip, Row, Shelf, Segmented, Eyebrow,
                                 HeroFrame, Skeleton, EmptyState + styles.ts
      player/                    hls.js wrapper, progress reporter
      lib/                       auth context, query hooks, media queries
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

### Client shell and design tokens (M15)

The chrome is a **68px top toolbar**, not the 240px left sidebar it replaces:
the logo, `Watch Now` (`/`), `Browse` (`/search`) and `Schedule`
(`/schedule`) in the centre, then the search field and a 36px avatar.
Everything that is a destination but not a daily one — My List (`/list`),
MyAnimeList, Review (with its pending count), Admin for admins, the account
email, "Acquisition paused" for an admin, and Log out — lives behind the
avatar, as a disclosure rather than an ARIA menu so its links keep the link
role. `/recs` keeps working and is reached from Browse and from Home's
"Picked for you" shelf.

Below 768px (`useIsPhone`, `client/src/lib/media.ts`) the same set becomes a
bottom tab bar — Watch Now · Browse · My List · More — with Schedule at the
top of the "More" sheet above the account items, and the toolbar collapsed to
the mark plus a search control that expands inline. The tab bar stays at four
tabs: a fifth crowds them at phone widths, and the sheet is the design's own
"one more tap" answer. That branch is JavaScript rather than `hidden md:flex`
because the two are different elements, and rendering both would put a second
`<nav>`, a second search field and a duplicate set of account links into the
accessibility tree on every page. The player renders outside the shell and has
no chrome at all.

Tokens live in `client/src/index.css` (`:root` for the `--arc-*` values,
`@theme inline` for the Tailwind namespaces) per the design handoff in
`design/arc-design/design_handoff_arc_apple_tv/README.md`: a blue-black
window with translucent-white surfaces, 0.5px hairlines, radii named for what
they belong to, the `rise` and `arcpulse` keyframes and a reduced-motion rule.
Two rules are load-bearing: the arc gradient (`--arc-progress`) marks season
progress in My List and nothing else — playback progress is plain white — and
`--arc-accent` / `--arc-accent-contrast` survive only as transitional aliases
for pages not yet restyled. A primary action is `--arc-action` (white), a
focus ring is `--arc-focus`.

The reduced-motion rule in `index.css` turns off every CSS animation and
transition, but it cannot reach movement driven by a timer. Anything that
advances itself — today only Watch Now's hero — asks
`usePrefersReducedMotion` (`client/src/lib/media.ts`, alongside `useIsPhone`)
and simply never starts the timer.

## 4. Data model (tables)

| Table | Key columns |
|---|---|
| `users` | id, email (unique on lower(email)), password_hash, role, is_active, timezone, created_at |
| `invites` | id, token_hash, email (optional), created_by (SET NULL), created_at, expires_at, used_at |
| `sessions` | id (opaque token hash), user_id, expires_at, user_agent |
| `anime` | id (internal identity PK), anilist_id (unique, nullable), mal_id (unique, nullable), summary_source / detail_source (anilist|mal), title_romaji, title_english, title_native, synonyms (JSONB), description (AniList HTML, stripped on output), format, episodes, status, season, season_year, cover_url, cover_large_url (AniList `coverImage.extraLarge`, a *summary* column; null on a MAL-filled row, whose biggest picture is 230 px), banner_url, genres (array), tags (JSONB), studio, credits (JSONB `[{role, name}]`, studio first then Director / Series Composition / Character Design / Music / Original Creator from AniList staff; studio-only from MAL), relations (JSONB, anime-only), next_airing (JSONB), refreshed_at, popularity, average_score |
| `episodes` | id, anime_id, number, title, still_url (AniList `streamingEpisodes.thumbnail`; title and still are both written **only where null**, so a confirmed manual title survives every refresh), air_at, air_at_estimated (true when synthesised from a MAL broadcast slot), state (enum, §6 of spec), state_changed_at, unavailable_reason |
| `media_files` | id, episode_id (nullable until matched), path (unique), size (BIGINT), parsed (JSONB), match_confidence, match_candidates (JSONB), review_state, llm_suggestion (JSONB), created_at |
| `renditions` | id, episode_id (unique), dir, playlist_path, duration, width, height, subtitle_lang, audio_lang, ready_at |
| `list_entries` | user_id, anime_id (PK pair), status, progress, score, updated_at, updated_by (arc/mal), mal_synced_at, mal_dirty |
| `watch_progress` | user_id, episode_id (PK pair), position_s, duration_s, completed, completed_at (set once, drives retention grace), updated_at |
| `mal_links` | user_id (PK), mal_username, access_token_enc, refresh_token_enc, expires_at, last_import_at |
| `mal_write_log` | id, user_id (CASCADE), anime_id (RESTRICT: audit rows must never be deleted by cache pruning), field, old_value, new_value (JSONB), cause (watch/manual/revert, plus `conflict` which is never a write), status (pending/ok/failed/skipped), error, created_at |
| `wants` | user_id, episode_id (PK pair), created_at, dropped_at, drop_reason |
| `torrents` | id, episode_id, info_hash (unique), magnet, title, group, resolution, seeders, trusted, qbit_state, progress, added_at, completed_at |
| `jobs` | id, type, payload (JSONB), status, priority (lower runs first), attempts, max_attempts, run_after, locked_by, locked_at, last_error, created_at, started_at, finished_at |
| `rec_runs` | id, user_id, prompt, candidates (JSONB), picks (JSONB), model, created_at |
| `settings` | key (PK), value (JSONB) — admin-editable rules (preferred_groups, resolution, look_ahead_n, grace_days_g, unwatched_days_d, sub_lang, audio_lang, acquisition_paused), plus per-show overrides under `override:anime:<id>`. Written only through `arc/services/settings.py`, which validates every value (`validate` is a pure function, so the matrix is testable without HTTP) and logs one line per changed key with its previous value. The rule *readers* stay lenient by design — a hand-edited row is ignored with a warning rather than raising, because one bad row must not stop acquisition or shorten a grace period. |

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

### 5.1a Acquisition as built (M6)
- `compute_wants` (every 15 min, after any list change, and after a watch
  completion): for each `watching`/`planned` entry, `p` = max(furthest
  completed episode, list progress); wants `p+1 … p+N` (`look_ahead_n`,
  capped at 10) that have aired. A want whose show leaves watching/planned
  (on_hold/dropped/completed/off-list) is dropped with reason "show no
  longer watching or planned" (not deleted) so retention keeps a grace
  anchor; only a want the user watched past is deleted. Episodes with a live want in `not_wanted` (or
  `unavailable` after a 1-day retry delay) → `wanted` + `search_release`.
  A `wanted`/`unavailable` episode with no live want returns to
  `not_wanted`; `searching` onwards is never touched by the reconciler.
- `search_release`: `wanted→searching`; Nyaa search + filter + rank; top
  pick → `torrents` row (reuse on duplicate hash) → qBittorrent add →
  `downloading`. No candidate → requeue itself (same dedupe key): every 30
  min while the episode aired < 24 h ago, else every 6 h; after 14 days →
  `unavailable` "no acceptable release found". qBittorrent unreachable →
  backoff, stays `searching`.
- `poll_qbit` (every 60 s): syncs progress/state for Arc-category torrents;
  on completion → `downloaded`, largest video file under the mapped host
  directory is ingested with `expected=[anime_id, number]` so the matcher's
  prior applies → `matching` → `matched` via `link()`. A torrent that
  vanished from the client → `unavailable` "removed from client".
- `episodes.state` is written only by `acquisition/states.transition()`,
  which enforces the spec §6 table and logs every edge. Extra edges beyond
  the diagram: `searching → not_wanted` (want vanished mid-search),
  `matching → unavailable` (delivered file rejected in review),
  `wanted|unavailable → not_wanted`. One process-wide Nyaa client keeps the
  pacing and cache shared across concurrent searches.

### 5.2 Ingest and match
1. `ingest_file`: create `media_files`, ffprobe it, parse filename.
2. `match_file`: candidates = expected episode (if Arc downloaded it, prior
   0.6) ∪ local anime cache fuzzy hits ∪ AniList search hits. Score =
   weighted title similarity (token-set ratio over romaji/english/synonyms),
   episode plausibility (number ≤ episode count, season alignment), year and
   format agreement, group/prior. Confidence ≥ 0.85 → link, state `matched`,
   enqueue `transcode`. Otherwise `review_state = pending`; optionally enqueue
   `llm_suggest_match`, which asks a model for the most likely candidate + one
   line reason using structured output; stored for the review UI only.
3. The parser and scorer are pure functions with a corpus-driven test suite.

### 5.2a Matching rules as built (M5)
- Parser: `anitopy` plus normalisation into `ParsedName` (title, `title_key`,
  episode/range, season, part, version, group, resolution, kind:
  episode|batch|movie|special|nc|unknown). Pinned by
  `tests/fixtures/release_names.txt` (235 names, 100 % on episode+kind and
  title_key).
- Candidates: expected-episode prior (Arc-downloaded files; applied as a
  bounded bonus so it can never beat a title that says otherwise), fuzzy
  search over the local cache, and a catalogue search (AniList → MAL).
- Score weights: title 0.55, episode plausibility 0.20, season/sequel
  agreement 0.15, format 0.05, year 0.05. Non-exact title matches are
  capped at 0.88; an exact `title_key` match scores 1.0. Confidence is the
  best score, reduced to ≤ 0.80 when the runner-up is within 0.05.
  Absolute numbering above the episode count is re-based onto cached
  sequels with a penalty. Auto-link at ≥ `MATCH_AUTO_THRESHOLD` and only when the title is exact or
  its similarity ≥ `MATCH_MIN_TITLE_FOR_AUTO`; the expected-episode bonus
  applies only when the prior's own title similarity ≥ 0.60; an existing
  link is never cleared by a re-run; otherwise
  review with the top candidates and reasons; NC files are ignored; batches
  go to review. Acceptance fixture: 98 labelled cases, precision 100 %,
  recall ≥ 85 % (90.8 % achieved after the title floor), review cases carry the answer in the top 3.
- `library.link.link()` is the single place a file is attached to an
  episode (auto-link and review confirm); it sets `matched` without
  downgrading `preparing`/`ready`. M7 enqueues the transcode right after it.
- Ingest: `library_scan` every `LIBRARY_SCAN_INTERVAL_SECONDS` and at worker
  start walks `DATA_DIR/downloads` and `DATA_DIR/manual`, skips partials
  (`.part`, `.!qB`), hidden files, and files modified within
  `LIBRARY_SETTLE_SECONDS`; new files get a `media_files` row with the parse
  and (when `ffprobe` exists) probe summary, then a `match_file` job.
  Relative `DATA_DIR` is made absolute by `make`/`scripts/dev.sh` before the
  processes start from `server/`.

### 5.2b Match suggestions as built (M13, FR-L5)
- **The chain, not a model.** `llm_suggest_match` calls the same M12 provider
  chain (§5.6) through its general layer: `base.JsonModel.complete(system,
  user, schema, name) -> JsonResult` is "any JSON schema in, a parsed object
  out"; `RecsModel.recommend` is now a one-line adapter over it
  (`base.recommend_via`), so the rotation, the daily-quota cooldowns, the
  20 s timeouts, the single retry and the refusal handling are shared rather
  than reimplemented. `factory.build_model` (alias `build_recs_model`) returns
  the chain. **One per process, on both sides.** The API keeps its instance on
  `app.state.recs_model` and the lifespan closes it; the worker holds a
  module-level one (`factory.shared_model`, closed by `close_shared_model()`
  in the worker's shutdown beside the Nyaa client) because a handler has no
  `app.state` to hang it off. It has to be shared rather than built per job:
  the chain is where the daily-quota cooldowns live, and a queue of thirty
  review files would otherwise rediscover a spent Gemini model thirty times.
  The two processes learn cooldowns separately, which costs at most one wasted
  request each. `factory.model_for` remains for one-off callers (a script, an
  eval) that ask once and close.
- **The prompt** (`services/library/suggest.py`, pure): the parse (filename,
  title, episode, season, group, resolution, kind), the expected-episode prior
  when Arc downloaded the file — derived from the save path
  (`downloads/<episode id>/`), not from a job payload, so an ask days later
  still has it — and ≤ 8 numbered candidates taken from the stored
  `match_candidates` and resolved to `anime` rows (both titles, format,
  episode count, season/year, the matcher's score and reasons). Candidates
  carry `anime_id=` on their own line because that is the currency the answer
  is given in.
- **The schema**: `{anime_id: integer|null, episode_number: integer|null,
  reason: string, confidence: high|medium|low}`, `additionalProperties:
  false`, every field required. Nullables are `anyOf: [{type: …}, {type:
  "null"}]` — the structured-output subset has no `nullable`. A null
  `anime_id` is a real answer ("none of these"), which is what a reviewer
  needs before reaching for the search box.
- **Validation** (`suggest.validate`): an `anime_id` not among the candidates
  becomes null — the model chooses from the shortlist, it does not extend it;
  an `episode_number` below 1, or above the chosen show's episode count where
  the catalogue knows it, becomes null; the reason is trimmed and cut to 300
  characters; an unrecognised confidence becomes `low`. Only a payload that is
  not an object is rejected outright.
- **The job**: payload `{media_file_id, force?}`, dedupe key per file,
  priority 150 (behind everything — every job ahead of it has somebody waiting
  for an episode). Enqueued from `_review()` inside `match_file`, so every one
  of the paths into the queue asks and no other path does, and only when
  `LLM_MATCH_SUGGESTIONS` is on, a provider is configured, **and there is a
  shortlist**: a batch file and a "no good candidates" item reach the queue
  with nothing to choose between, so the question has no meaning and only a
  person can ask by hand. Idempotent: it skips a row that is gone or no longer
  `pending`, and one that already has a suggestion unless the payload says
  `force`. Since the dedupe key is the file rather than the file and the flag,
  `enqueue_suggestion(force=True)` writes `force` into a job already **pending**
  for that file (a running one is left alone — it has read its payload and is
  producing a fresh answer), or the person's "ask again" would be swallowed by
  the automatic ask.
- **Failures are stored, and never destroy an answer.** `RecsUnavailable` is
  raised so the queue's retry/backoff handles it. A refusal, an unusable
  answer, an empty shortlist and an unconfigured chain are *stored*: on a row
  with no answer as `llm_suggestion = {error, model, created_at}`, and on a row
  that already has one as `last_error` **beside** the answer, which stands.
  `force` has to be safe to press, and a re-ask that traded a usable
  suggestion for "the model declined" would make it a gamble. A later success
  replaces the whole blob, dropping `last_error` with it — it is a note about
  an attempt, not about the file. `SuggestionOut` therefore never sets both:
  a row with an answer renders as the answer with `error: null`.
- **It never links.** There is no call to `link()` in the handler and there
  must never be one (FR-L5). The only write is `media_files.llm_suggestion =
  {anime_id, episode_number, reason, confidence, model, provider, created_at}`,
  a column the review API renders and nothing else reads. Confirming behaves
  identically whether or not a suggestion exists.

### 5.3 Transcode
1. `transcode` (priority = how soon a user will reach it): choose subtitle
   track (configured language, prefer ASS over SRT), choose audio track
   (Japanese default). Run
   `ffmpeg -i src -map v:0 -map a:<idx> -vf subtitles=src:si=<sub idx>
   -c:v libx264 -preset fast -crf 19 -tune animation -c:a aac -b:a 160k
   -f hls -hls_segment_type fmp4 -hls_time 6 -hls_playlist_type vod …`
   into `/data/renditions/<episode id>/`. Parse ffmpeg progress for %.
2. Success → `renditions` row, episode `ready`. Failure → `failed` with
   stderr tail; up to 2 automatic retries, then manual.
3. Fonts: ship a fonts volume; extract attached fonts from MKV
   (`-dump_attachment`) before rendering so ASS styling renders correctly.

### 5.3a Transcode as built (M7)
- Trigger: `link()` landing an episode in `matched` enqueues `transcode`
  (dedupe `transcode:<id>`, priority 10 × distance to the nearest wanting
  user's progress, capped 500, default 100); a worker-start sweep queues
  any `matched` episode without a rendition and re-queues `failed` ones
  with attempts left.
- Plan (pure, `media/plan.py`): video = first non-attached-picture stream;
  audio = first stream matching `audio_lang` (default ja) else first;
  subtitle = text tracks only (ass/ssa/srt/webvtt/mov_text), prefer
  `sub_lang` (en), ASS over SRT, non-forced, "full/dialogue" over
  "signs/songs"; bitmap subs → prepared without subtitles and flagged.
- Run: attachments dumped to a temp `fonts/` dir, chosen subtitle track
  extracted to a temp file, then one ffmpeg pass with
  `subtitles=<tmp>:fontsdir=<tmp>/fonts` (no filter-path escaping of the
  release name), `libx264 fast crf 19 -tune animation yuv420p high@4.1`,
  AAC 160k
  stereo, keyframes forced every `HLS_SEGMENT_SECONDS` (6), fMP4 HLS VOD
  with independent segments → `DATA_DIR/renditions/<id>/{index.m3u8,
  init.mp4, seg_%05d.m4s}`. Playlist URIs are bare filenames (no rewrite
  needed for streaming). Progress from `-progress pipe:1` and the last 40
  stderr lines are written into the job payload; the `renditions` row is
  created only after the playlist validates with ffprobe. Source kept.
- Two encodes per worker process (`MAX_TRANSCODES`); the handler heartbeats
  `locked_at` every 60 s and `WORKER_STALE_AFTER` is 7200 s so a long
  encode is never requeued under itself, and the handler also heartbeats
  while waiting for a slot. A transaction-level advisory lock keyed on the
  episode makes a second claim of the same transcode a no-op, and the
  encode writes into a per-job temp dir renamed into place on success, so
  two claims can never share an output directory. Partial output is removed
  on any failure or cancellation. Idempotent: a valid rendition + row
  short-circuits the job. `ready → preparing` is taken by `force` and by
  the handler's self-heal when a row exists but its directory no longer
  validates. Plan notes (bitmap-only subs, no subtitle in the preferred
  language) are surfaced as `rendition.notes`. Home and detail routes
  populate the same episode fields (progress, failure, release, rendition)
  from one batched lookup; the latest transcode job per episode is found
  via a partial expression index on `jobs (payload->>'episode_id') WHERE
  type = 'transcode'` (`ix_jobs_transcode_episode`, migration 2 — the
  squashed initial revision plus a straight chain from here on).
- Local dev needs an ffmpeg with libass; Homebrew's `ffmpeg` lacks it, so
  `ffmpeg-full` (keg-only) is used via `FFMPEG_BIN`/`FFPROBE_BIN`. The
  Docker image's Debian ffmpeg has libass. The runner fails fast with a
  named error when libass is missing.

**Encoder settings and the CPU trade-off (M15).** `TRANSCODE_PRESET` (fast),
`TRANSCODE_CRF` (19), `TRANSCODE_TUNE` (animation, empty for none) and the
optional `TRANSCODE_MAXRATE_KBPS`/`TRANSCODE_BUFSIZE_KBPS` are settings, not
constants: `Settings` → `media/jobs.encode_options` → `plan.EncodeOptions` →
`plan.encode_args`. `-tune` is passed only when set (an empty `-tune` is an
unknown tune, and ffmpeg exits on it); the VBV pair is passed only when a
maxrate is set, and a maxrate with no bufsize gets twice the maxrate.
`-profile:v high -level 4.1 -pix_fmt yuv420p` are unconditional.

The cost is CPU, as a multiple of real time on the 2-vCPU production budget
for a 1080p episode: `veryfast` ≈ 1×, `fast` ≈ 1.5–2×, `medium` ≈ 2.5–3×.
Measured locally on a 60 s excerpt (M1, `-threads 2`, subtitles burned in),
`fast` + `-tune animation` cost 2.6× the CPU of `veryfast`/CRF 20 for 3% more
bitrate, so budget nearer the top of the `fast` range: a 24-minute episode
moves from roughly 25 to roughly 60 minutes, well inside the three-hour
`TRANSCODE_TIMEOUT_SECONDS`. Episodes are prepared ahead of being watched
(acquisition fetches the next N unwatched), so the preset is a queue-depth
question, not a playback one; a host that cannot keep up sets `veryfast` back.

**The burn-in path is not what made playback look soft.** Nothing in the graph
scales: Arc serves one rendition at the source's own resolution, the only
filter is `ass=`/`subtitles=`, and the dev source is already 8-bit `yuv420p`,
so swscale is never asked to resample or convert. There is therefore no
`-sws_flags`: the flag would describe a resize that does not happen. The ASS
scripts Arc sees declare `PlayResX/Y` of 640×360 against a 1920×1080 picture,
but libass rasterises glyphs at the final frame size rather than upscaling a
360p render, so the typesetting is not the softness either. The softness was
`veryfast`, which gives up most of x264's analysis and shows it on flat cels
and gradients.

### 5.4 Streaming
- `GET /media/{episode_id}/index.m3u8` and `/media/{episode_id}/{segment}`
  are served by the API with session auth; playlists are rewritten so segment
  URLs stay under `/media/…`. Caddy proxies `/media` to the API (no direct
  static exposure). Range requests supported on segments.
- Client uses hls.js with `xhrSetup` sending credentials.

### 5.4a Streaming and playback as built (M8)
- No playlist rewriting: ffmpeg writes bare relative URIs, so
  `/media/{id}/…` resolves by construction. Every media request carries the
  session cookie (same origin behind Caddy; Vite proxies `/media` in dev);
  hls.js is configured with `withCredentials`. `SameSite=Lax` means a
  cross-site `<video src>` embed would not carry the cookie, which is fine.
- Client: hls.js is a lazily loaded chunk, used whenever MediaSource exists
  (Chrome answers "maybe" to the HLS mime check yet cannot play a playlist
  natively, so `canPlayType` alone is not a signal); only a browser with no
  MediaSource (iOS Safari) gets the native `src` path; the player page renders outside the app shell; resume is
  automatic with a dismissible "Resumed from m:ss" notice; a framework-free
  `ProgressReporter` posts every 10 s while playing, on pause, on seek
  (debounced), and on `pagehide` via `sendBeacon`, with a 2 s floor between
  ordinary reports; the end-of-episode overlay offers the next episode when
  it is ready.
- `newly_completed` is derived from the upsert's `RETURNING old.completed`
  (PostgreSQL 18), so it fires exactly once per (user, episode) even when
  concurrent reports share a timestamp; this pins the database floor at 18. Media routes resolve the target path and refuse anything not
  inside the rendition directory or that is a symlink. All path ids are
  bounded to int64 (422 beyond). A partial index on in-progress watch rows
  backs continue-watching (migration 3).
- Continue watching is decided by the saved position alone and ignores
  `completed`: a row is listed when its position is ≥ 30 s and short of the
  tighter of 95 % of the duration and duration − 60 s, newest `updated_at`
  first, so a rewatch stopped half-way is offered and resumes where it
  stopped (`/play` carries `resume_position` for completed rows too), while an
  episode watched to the end falls off by the ceiling rather than by the flag.
  Nothing else reads the shelf's rule: completion still drives the MAL push,
  the once-only list advance, and the watched marks. The query no longer
  matches migration 3's `completed = false` predicate, so it is served by the
  full `(user_id, updated_at)` index instead.

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

### 5.5a MAL sync as built (M9)
- Link: PKCE `plain`; the `state` is Fernet-encrypted `{user_id, verifier,
  nonce}` with a 10-minute validity (stateless); the callback is the API
  route `/api/mal/callback` (register `<API origin>/api/mal/callback` on
  the MAL app), requires the session, checks the state's user, exchanges
  the code, stores tokens Fernet-encrypted, queues an import, and redirects
  to `PUBLIC_URL/mal`. Unlink destroys tokens and keeps the log. A failed
  refresh empties the token columns (`needs_relink`).
- Import (on link, every `MAL_IMPORT_INTERVAL_HOURS`, or on demand): pages
  the list; unknown MAL ids resolved via the catalogue 50 per run with a
  follow-up job; clean local rows overwritten; dirty rows: newer change
  wins, and when MAL wins the lost Arc change is logged `cause=conflict,
  status=skipped`; shows absent on MAL are kept, never deleted.
- Push: the write log is the queue. Each user event writes one `pending`
  log row per changed field, with its own cause, in the same transaction
  as the local change — list set/remove (`manual`), watch completion
  (`watch`, progress only), revert (`revert`). The push job (deduped per
  user+anime; the rows are the state, so nothing is lost on dedupe) loads
  the pending rows, coalesces per field to the latest value (older rows
  `skipped: superseded`), GETs MAL's current entry, applies the FR-M4 guards
  per field from that field's cause (a `watch` progress below MAL's number
  or a `watch` null score → `skipped`, never sent), sends one PATCH or
  DELETE, and marks rows `ok`. A retryable MAL error keeps the rows
  `pending` (with the error as a note) and retries with backoff; only when
  the job's attempts are exhausted, or on a non-retryable 4xx, do rows go
  `failed`, with `mal_dirty` kept so an import never overwrites the change
  while Arc's is newer. "Push pending" reopens failed rows. `mal_dirty` is
  cleared only when a pair has neither pending nor failed rows. A removal
  closes never-sent edit rows `skipped`; unlink closes pending rows
  `skipped`; an event landing during a running push queues a follow-up;
  revert is refused while the field has a queued row. "Push pending" runs the
  same routine for every pair with pending rows; `mal_import_all` never
  writes; a show with no MAL id logs `skipped`. Token refresh takes a row
  lock so concurrent jobs refresh once.
- Revert: allowed on the newest ok row per (user, anime, field); it applies
  the old value locally and pushes with cause `revert` (a revert is itself
  revertible).

### 5.6 Recommendations
`arc/services/recs/`: `base` (protocol, exceptions, retry), `chain` +
`factory` (which model answers), `claude` + `openai_compat` (the two
backends), `pool`, `continuations`, `history`, `prompt`, `schema`, `runs`.

1. **Candidate pool** (≤ 40, `pool.py`, FR-R2). Three sources, in order,
   de-duplicated by internal `anime_id`, each with a share of the forty so a
   season cannot fill it on its own (20 season / 12 relations / the rest
   genres; unused shares go back to the season):
   *(a)* cached `anime` rows in the current and next season, most popular
   first (see **Ranking** below);
   *(b)* `anime.relations` of completed shows scored ≥ 8 (all completed if the
   user scores nothing), resolved against local rows, with at most 10 misses
   fetched through the catalogue and upserted — every fetch is optional and a
   failure skips the title, never the run;
   *(c)* local rows sharing ≥ 2 of the user's top-3 genres (weighted by score
   over completed + watching; an unscored entry counts 5).
   Every source is bounded: the season query is `LIMIT 4×40`, relation seeds
   are cut to the best 50 completed shows before flattening, and the optional
   catalogue fetches stop at 10 calls **or** a 5 s wall clock, whichever comes
   first — the deadline is what saves a page from a catalogue that is answering
   slowly rather than failing, since the breaker never trips. Both the pool and
   the continuations section share one `resolve_relations`, so those bounds are
   spent once between them.

   **Ranking.** The season orders by `popularity DESC NULLS LAST, id` — a
   season is more titles than the pool takes and "what everyone else is
   watching" is the only honest signal a source with no personal input has. The
   genre source orders by `average_score DESC NULLS LAST` instead, and that is
   the one place the two differ: it has already matched on the user's taste, so
   the question left is "is it good", not "is it known". Both columns are
   nullable and fill in on a row's next refresh; `NULLS LAST` puts an unranked
   row where it belongs meanwhile, so no backfill is needed.

   **Exclusions**, beyond FR-R2's "anything on the list but planned":
   - formats outside `TV`, `TV_SHORT`, `ONA`, `MOVIE` — an allow-list, because
     the tail of AniList's vocabulary (OVA, SPECIAL, MUSIC, MANGA) is not
     something anyone starts watching on a recommendation;
   - titles matching recap/special/short patterns (`recap`, `theater`,
     `theatre`, `mini`, `special`, `picture drama`, `omake`, `daze`, `pv`) on
     **word boundaries** — a substring test would drop *Administrator* for
     containing "mini";
   - **direct continuations of listed shows** (relations of type `SEQUEL`,
     `PREQUEL`, `SIDE_STORY`, `SPIN_OFF`, `ALTERNATIVE`, `PARENT`, `SUMMARY`).
     A sequel scores well on every signal the pool has and needs none of them,
     so it goes to its own section instead — see below.
   Each candidate carries id, title, genres, ≤ 6 tag names, a plain-text
   synopsis truncated to ~400 chars, season, format, episode count, and a
   `why` naming the source.

1b. **Continuations** (`continuations.py`) — "new in your franchises",
   deterministic and **without a model call**, because "season two of the show
   you finished" is a fact rather than an argument. Sources are shows the user
   is watching, has completed, or has planned; relations counted are `SEQUEL`,
   `SIDE_STORY`, `SPIN_OFF`, `ALTERNATIVE` and anything in `MOVIE` format
   (`PREQUEL`/`PARENT`/`SUMMARY` point backwards and are merely excluded from
   the pool). Anything already on the list is dropped — including planned,
   unlike the pool — as are recaps and disallowed formats. Ordered by the
   source show's score, then newest first by season; capped at 8. Each carries
   a finished sentence: "Sequel to Frieren, which you completed", "Movie in the
   Assassination Classroom series (on your planned list)".

2. **The prompt.** The mood text (FR-R1) is the only span a stranger writes,
   so it is fenced between `<mood>`/`</mood>` and the system prompt says in as
   many words that the span is a preference to satisfy and never an instruction
   to follow. Defence in depth rather than the defence: step 4 checks the picks
   against the pool afterwards, so a run that obeyed a hostile mood could still
   only return shows it was offered.
2b. **History summary** (`history.py`, FR-R3): top-rated (≤ 10), recently
   completed (≤ 10), watching with progress (≤ 10), dropped (≤ 5), planned
   titles (≤ 20). A pure function over the `(anime, entry)` rows the run
   already loaded.
3. **The call — a chain, not a model.** Gemini's free tier allows about 20
   requests per day *per model for the whole deployment*, against Arc's own
   limit of 10 runs per user per day, so one model is a countdown rather than a
   setting. `chain.py` holds `[(provider, model), …]` — the primary provider's
   models in `RECS_MODEL` order, then the fallback provider's — and is itself a
   `RecsModel`, so nothing above it knows there is more than one. `base.py`
   holds what the backends share (the protocols, exceptions, parsers, timing,
   the single retry). **Two layers since M13:** `JsonModel.complete(system,
   user, schema, name) -> JsonResult` is the general one — the streaming,
   refusal, truncation and retry logic all live there, and the chain's walk
   and its cooldowns are on `complete` rather than on `recommend`, so a second
   feature shares them (a Gemini model spent by a suggestion is spent for a
   recommendation too); `RecsModel.recommend -> RecsResult` is a one-line
   adapter (`base.recommend_via`) that validates the object as `Picks`
   *outside* the retry, since a well-formed answer that is not the schema is a
   real answer and asking again spends quota. Match suggestions (§5.2b) are
   the second caller. `factory.build_model` is the neutral name
   (`build_recs_model` remains as an alias) and `factory.model_for` is the
   per-job context manager the worker uses.
   `factory.py` reads the config into a chain, sharing **one
   HTTP client per provider** and dropping entries whose provider has no key,
   so the chain's length is the honest answer to "is anything configured"
   (empty → `None` → `configured: false` → 503).
   - **What advances the chain.** Only `RecsUnavailable`. A `RecsRefused` or a
     `RecsFailed` **stops** it and raises: a refusal is a judgement about the
     request and a schema mismatch is a prompt problem, so every later entry
     would answer the same way and walking on would spend a paid fallback to be
     told no twice.
   - **Daily-quota cooldown.** A 429 is ambiguous — "too fast" or "that is your
     lot for today" — and Gemini distinguishes them in the body, so
     `is_daily_quota` reads it: `RESOURCE_EXHAUSTED` with a `PerDay` quota id,
     or `generate_content_free_tier_requests` with `limit: 20`. A daily one
     puts the entry on cooldown until the next **08:00 UTC**; Google's free
     quotas reset at midnight US-Pacific, which is 08:00 UTC in standard time
     and 07:00 in daylight time, and the later is chosen deliberately (coming
     off an hour early costs one wasted request, an hour late costs nothing
     because the next entry answers). A per-minute 429 just moves on. Cooldowns
     live in memory on the chain, which lives as long as the app; losing them on
     a restart costs one request. Anything unrecognised is treated as
     transient. A spent daily quota also suppresses the backend's own retry —
     retrying it is certain to fail, and the live rotation check showed it
     costing a second and a round trip before the chain moved on anyway.
   - **Budgets, both backends.** `max_tokens` 16000 with reasoning turned down
     (`reasoning_effort: "low"` / `output_config.effort: "low"`). The one number
     that must not be guessed: on both providers the reasoning is drawn from the
     *same* budget as the answer, so a small `max_tokens` does not shorten the
     answer, it deletes it — measured on Gemini at 50 tokens (`finish_reason:
     length`, no content, zero completion tokens) and again at 6000 (828
     characters of JSON ending mid-string, *without* reporting `length`).
   - **Timeouts and retry.** `TIMEOUT_SECONDS` 20 per attempt, SDK
     `max_retries=0`, one retry of Arc's own after 1 s — retrying
     unavailability and truncated/empty streams, never a refusal or a schema
     mismatch. So a chain entry costs at most ~41 s before the next is tried.
   - `openai_compat.py` (**gemini**, **openrouter**):
     `AsyncOpenAI.chat.completions.create` against the provider's base URL,
     `response_format` = `{type: json_schema, json_schema: {name, schema,
     strict: true}}`, streamed inside `async with` with
     `stream_options.include_usage` (usage arrives on a final choice-less
     chunk; its absence is tolerated). `reasoning_effort` goes to both — it is
     the portable spelling, and Google's own `thinking_config` passthrough is
     awkward (it nests under a body field itself named `extra_body`; the obvious
     `{google: …}` is a 400) *and* rejected outright by `gemini-2.5-flash`.
     `finish_reason` `length`, or unparseable JSON on a stream that never said
     `stop`, → `RecsFailed("truncated")`; `content_filter` or a non-empty
     `refusal` → `RecsRefused`; empty content on a clean stop →
     `RecsFailed("empty")`; an `error` field on a chunk (a gateway failing
     mid-stream at HTTP 200) → `RecsUnavailable`. OpenRouter also gets
     `HTTP-Referer`/`X-Title`.
   - `claude.py` (**anthropic**): `client.beta.messages.stream` with `thinking:
     {type: "adaptive"}` (never `budget_tokens` — a 400 on Claude 5),
     `output_config` carrying **both** the JSON schema and `effort: "low"`,
     `fallbacks: "default"` + beta `server-side-fallback-2026-07-01`.
   Both map their SDK's **base** error class (`openai.APIError` /
   `anthropic.APIError`) to `RecsUnavailable`, so a subclass neither names is a
   502 rather than a 500. `RecsResult` carries `provider` as well as `model`
   (the id the *server* reported), and the run logs both. Tests inject fakes;
   no backend is reached from `make test`.
4. **Validation** (`runs.validate_picks`): a pick whose `anime_id` was not in
   the pool, or which is on the user's list with any status but `planned`, is
   dropped; duplicates collapse; more than 5 are truncated; the stored title is
   the catalogue's. Fewer than 3 survivors are logged and stored as-is — the
   model is never called twice for one run.
4b. **Transactions.** The run commits after the pool is built and *before* the
   model is called. Building the pool can insert `anime` rows (a relation the
   catalogue had to fetch), and holding those row locks across a 20-second call
   to a third party would stall every writer touching them. The consequence —
   a fetched relation survives a failed run — is the right way round: it is
   cache. "A `rec_runs` row exists only on success" still holds; the row is
   written after the picks are validated.
5. **Persistence and rate limit** (FR-R5): one `rec_runs` row per run carrying
   the prompt, the pool as sent, one tagged list holding both kinds of entry
   (`{kind: "pick", anime_id, title, case}` and `{kind: "continuation",
   anime_id, title, because}` — one JSONB column, so no migration; a row
   written before the tag existed has no `kind` and reads as a pick), and the
   model
   that answered (truncated to the column's 64 characters; the provider goes to
   the log rather than a new column). Ten runs per user per 24 h, counted from
   `rec_runs(user_id, created_at)` — no new column; the wait is measured from
   the oldest run in the window. The rate limit is checked before the pool is
   built, so a refused run costs nothing. Check-then-act without a lock, so two
   simultaneous requests can land an eleventh run; accepted, because the limit
   bounds cost rather than stating an invariant.
6. **API**: `GET /api/recs` returns the newest run, the remaining budget,
   `configured`, and — **for admins only, the field is omitted entirely for
   everyone else** — `chain`, so an operator can see how much of the day's free
   tier is left; `POST /api/recs/runs` creates one. `RecRunOut` splits the
   stored list back into `picks` (the model's, with `case`) and
   `continuations` (deterministic, with `because`). Errors map 429 (limit, with
   `retry_after_seconds` and `Retry-After`), 503 (the selected provider's key
   is unset, or the model refused), 502 (upstream), 409 (empty pool), 422
   (prompt over 300 chars).
   Without the selected provider's key the API answers 503 and `config_check`
   logs one WARNING naming the provider and the variable it wanted (not an
   error: a deployment that never wanted recommendations is valid, and
   `/api/health`'s count must still reach zero). The 503 refusal detail is
   provider-neutral — "The model declined this request".

### 5.7 Retention
`retention_sweep` (hourly, priority 200, first run 10 min after worker
start): candidates are `ready|downloaded|matched|failed` episodes with no
live want. Grace anchor = latest of every completion (`watch_progress.
completed_at`, any user) and every `wants.dropped_at`; a want that ends
because the show left watching/planned is dropped, not deleted, so it
always leaves an anchor; only a want the user watched past is deleted (its
completion is the anchor). With no anchor at all (manual drops), the
file's own age (`renditions.ready_at`, else `media_files.created_at`).
Deletable when anchor + G < now. Deletion: qBittorrent torrent with files
(Arc category only; "not in client" is a no-op), rendition dir, source
dir `downloads/<id>` or the manual-drop file, then the rows, then
`→ not_wanted` (edges added for `downloaded|matched|failed`). Every path
is resolved and re-checked against `renditions`/`downloads`/`manual` roots
right before removal; symlinks and out-of-root paths are refused; a live
want is re-checked immediately before deleting; `RETENTION_DRY_RUN` logs
the plan only. A `ready` episode with no files on disk is reset to
`not_wanted` with reason "no files on disk". The deletion also removes the
episode's leftover dropped want rows, except stale ones (they prevent a
fetch → drop → delete loop). `retained_bytes` measures `renditions.dir`
when set. FR-T2 in `compute_wants`: a live want on a `ready`
episode is dropped ("unwatched for D days") when
`greatest(ready_at, list_entries.updated_at) < now − D` and the user has
not completed it; a stale-dropped want is revived only by an Arc-side action
(`updated_by = arc` and `updated_at > dropped_at`); a MAL import keeps
the row's `updated_at` when MAL omits one and nothing changed, so an
unchanged six-hourly import can never keep a want alive. A qBittorrent
outage skips that episode for this sweep only. Admin:
`GET /api/retention/preview`, `POST /api/retention/sweep`,
`POST /api/episodes/{id}/delete-files`; `retained_bytes` on
`GET /api/acquisition/status`.

### 5.0 Catalogue sources and fallback (M3b)
- `CatalogSource` protocol: `search(q, page)`, `by_anilist_id(id)`,
  `by_mal_id(id)`, `season(year, season)`; implementations `AniListSource`
  and `MalSource`. Both return the same `CatalogMedia` dataclass with
  `source` set.
- `CatalogService` tries AniList first and falls back to MAL on connection
  error, timeout, 5xx, or AniList's "temporarily disabled" 403. A circuit
  breaker opens after a failure and skips AniList for 5 minutes (probing on
  the next call after that), so an outage never costs a timeout per request.
  If both fail: 502 `catalogue is unavailable`.
- **Search is local first, then live** (`catalog/local.py`, M15). The cached
  `anime` rows are matched before the upstream call and put in front of the
  live page: the query is split on whitespace and every word must appear, as a
  case-insensitive `ILIKE` substring, in `title_romaji`, `title_english` or the
  `synonyms` JSONB cast to text — not necessarily the same field for each word.
  `%` and `_` in the query are escaped. Ordering: exact title match, then
  prefix, then the shows the caller follows, then `popularity DESC NULLS LAST`,
  then id; capped at 20. The merge is by internal id, local hit winning, and
  happens on `page=1` only — the local hits are not paginated, so repeating
  them under `page=2` would show the same cards twice. An upstream failure with
  local hits is a 200, not a 502; only an empty local result on a failed
  upstream is a 502. Why: with AniList disabled, MAL's search matches whole
  words from the start of a title, so "jobless reincarnation" found nothing
  while the show sat cached, on the owner's list, with an episode downloading.
- Breakers are per API process (on `app.state`) and per job run (a fresh
  breaker per scheduled job, so each sweep re-probes a source that was down).
- A cached row is served when a source reports the id as not found, so a
  show already on someone's list never 404s because one source dropped it.
- MAL premiere rule: episode 1 airs on `start_date` at the broadcast time
  (the weekday is ignored for the premiere; later episodes are weekly from
  there). A missing broadcast slot defaults to 23:00 JST so the calendar
  day survives west of Japan. Partial `start_date` values (year or
  year-month) yield no synthesised dates. `num_episodes: 0` (uncounted
  airing show) yields no episode rows from MAL.
- When AniList's schedule starts above episode 1 (premiere blocks), the
  estimated or undated rows below its first published episode are back-filled weekly
  backwards from that episode and stay flagged estimated; published dates
  always win and no two episodes share an instant.
- Upserts insert inside a savepoint and retry via lookup on a unique-key
  collision, so two concurrent first-time upserts of one show (e.g. a search
  racing the reconcile job) converge on one row.
- Identity: `anime.id` is internal. Upserts look up by `anilist_id`, then by
  `mal_id`; AniList payloads carry `idMal`, so an AniList upsert attaches to
  a MAL-first row instead of creating a second one. `detail_source` records
  which source last filled the detail columns; MAL data never overwrites
  AniList-sourced detail, AniList always overwrites MAL-sourced detail.
- MAL-sourced episodes: `air_at` synthesised from `start_date` + broadcast
  weekday/time (JST) for 1..num_episodes, `air_at_estimated = true`; AniList
  schedule data replaces them and clears the flag.
- Jobs: `catalog_reconcile` (hourly when AniList is healthy: fill missing
  `anilist_id` via `Media(idMal:)`), `catalog_season_sweep` (daily at 03:30
  UTC, and at worker start when the current season has no cached rows:
  upsert the current and next season's summaries from whichever source is
  up, including `nextAiringEpisode`; MAL-sourced airing shows get a
  synthesised `next_airing` from their broadcast slot, marked
  `estimated`, episode number unknown).
  After the summary upsert the sweep enqueues a detail refresh (which
  brings the airing schedule) for every current-season TV/TV_SHORT/ONA row
  with no `next_airing` and no episode rows, spaced 5 s, max 60 per sweep,
  so most of the season gets a weekday within the hour.
- An estimated (MAL) `next_airing` never replaces an existing blob on a row
  whose summary or detail came from AniList, and never replaces a published
  one; AniList blobs always replace MAL ones. The schedule exposes
  `next_at_estimated` so the UI can mark synthesised times.
- Schedule placement: a `next_airing` older than 7 days is ignored (hiatus)
  and the row falls back to its last real episode air time. The aired rule
  (`air_at <= now`, estimated dates count, RELEASING boundary, FINISHED
  fallback) lives in one place, `catalog/airing.py`, used by the show page
  and by behind-by.

## 5b. API surface (kept current)

All routes require a session unless marked public. Errors are JSON `{"detail"}`.
Mutating requests must carry an allowed `Origin`.

| Route | Who | Purpose |
|---|---|---|
| `GET /api/health` | public | liveness |
| `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` | public / any | session |
| `POST /api/invites`, `GET /api/invites`, `DELETE /api/invites/{id}` | admin | invite management |
| `GET /api/invites/{token}`, `POST /api/invites/{token}/accept` | public (rate-limited) | invite flow |
| `GET /api/users`, `PATCH /api/users/{id}` | admin | user management (zero-admin guard) |
| `PATCH /api/users/me` | any | change own timezone (IANA, validated) |
| `POST /api/jobs`, `GET /api/jobs`, `GET /api/jobs/{id}` | admin | job queue; the listing takes `status=`, `type=`, `limit=` (≤ 200), `offset=`, newest id first |
| `GET /api/jobs/summary` | admin | `{by_status: {pending, running, done, failed, cancelled}, by_type_pending: {type: count}, worker: {heartbeat_at, alive}}` — `alive` is the heartbeat file younger than 90 s, the same decision the container healthcheck makes |
| `POST /api/jobs/{id}/retry`, `POST /api/jobs/{id}/cancel` | admin | 200 `JobOut`. Retry: `failed`/`cancelled` → `pending`, `attempts` 0, `run_after` now, lock and finish timestamps cleared, `last_error` kept. Cancel: `pending` → `cancelled`. 409 for any other status (a `running` job cannot be stopped safely), 404 unknown |
| `GET /api/settings`, `PUT /api/settings` | admin | the rules editor (FR-D2, FR-T5): `{values, defaults, overrides[{anime_id, title, preferred_groups, resolution}]}` for every `DEFAULT_SETTINGS` key. PUT takes a partial object of those keys and writes only what it names; 422 `{detail: [{loc: ["body", key], msg, type}]}` for an unknown key or a refused value (resolutions ∈ 2160p/1080p/720p/480p and fallback ≠ preferred — enforced only when the patch names one of the two, so a hand-edited collision does not block unrelated edits — N 0..10, G and D 0..365, ≤ 20 groups of ≤ 64 chars de-duplicated case-insensitively, languages 2–8 lowercase letters/dashes). Writing `acquisition_paused` goes through the same `set_paused` the pause button uses, and clearing it enqueues `compute_wants` in the same transaction, so unpausing from the editor and from the button do the same thing. Overrides are read-only here; editing them is M16 |
| `GET /api/anime/search?q=&page=` | any | **local hits first, then the live search** (§5.0): cached rows matching every word of `q` in a title or synonym, ordered exact/prefix title → followed → popularity, capped at 20 and merged on `page=1` only; the live results follow, de-duplicated by internal id, and are cached. `page`/`has_next` describe the live half. Local hits with the catalogue down is a 200; 502 only when the local half is empty *and* upstream failed. Each `AnimeSummary` carries `cover_large_url` (nullable) beside `cover_url`, so a card prefers the sharp key art and falls back, plus `popularity` and `average_score` (both nullable; real summary columns, straight off the search — `average_score` is 0–100 whichever source answered, MAL's 0–10 scaled on the way in), plus `genres[]`, `banner_url` and `studio` — **read off the cached row, not the search payload**: AniList's search fragment does not carry them, so they are empty/null on a show no detail fetch has reached and fill in the moment one does. They are deliberately *not* in the summary write path; a search payload's empty genres would otherwise blank them (cache rule 2) |
| `GET /api/catalog/status` | admin | source health and breaker state |
| `GET /api/mal/status`, `POST /api/mal/link`, `GET /api/mal/callback`, `DELETE /api/mal/link`, `POST /api/mal/import`, `POST /api/mal/push`, `GET /api/mal/log`, `POST /api/mal/log/{id}/revert` | any (own account) | MAL link, import, push pending, write log, revert |
| `GET /api/recs`, `POST /api/recs/runs` | any (own runs) | recommendations (FR-R1…FR-R5): GET returns `{run, remaining_today, limit_per_day, configured}` with the newest run (`RecRunOut` = `{id, prompt, created_at, model, candidate_count, picks[{anime, case}], continuations[{anime, because}]}`), plus `chain: [{provider, model, available}]` **for admins only** (the field is absent for everyone else); POST `{prompt}` (trimmed, ≤ 300 chars) creates one → 201 `RecRunOut` `{id, prompt, created_at, model, candidate_count, picks[{anime, case}], continuations[{anime, because}]}`. 429 `{detail, retry_after_seconds}` + `Retry-After` at 10 runs/24 h; 503 unconfigured or refused; 502 upstream; 409 empty pool |
| `GET /api/anime/{id}` | any | (internal id) `AnimeDetail` + `anilist_id`, `mal_id`, `source`; `list_entry.mal_sync` state; `relations[]` carry `id` (internal, null when Arc has no row yet) plus `anilist_id`/`mal_id`, and — for the relations Arc *has* cached — `cover_url`, `cover_large_url`, `episodes`, `season_year` for M15's franchise rail (all null when the row is not cached; nothing is fetched to fill them, and `format` comes from the stored relation blob so it is present either way). Resolved in one query over named columns, not one per relation; episodes carry `air_at_estimated`: summary + synopsis, genres, studio, `credits[]` (`{role, name}`, studio first — M15's "Made by" block; one row long on a MAL-sourced show), `cover_large_url` (nullable key art), relations, `next_airing`, `list_entry`, `episode_count`, `episodes[]` (id, number, title, `still_url` (nullable), air_at, aired, state, watched) |
| `POST /api/anime/{id}/refresh` | admin | enqueue `anilist_refresh` |
| `PUT /api/list/{anime_id}`, `DELETE /api/list/{anime_id}`, `GET /api/list?status=` | any | list states; PUT sets `updated_by=arc`, `mal_dirty=true`; `completed` sets progress to episode count; `score: null` clears. Rows are `{anime: AnimeSummary, entry}`, so each carries `cover_large_url`, `genres[]`, `banner_url` and `studio` (M15: My List credits the studio per row) |
| `GET /api/schedule?year=&season=` | any | cache-only season grid: 7 days (0 = Monday in the user's timezone), entries with local time, next episode, `following`; movies/OVAs/specials/music and rows with no known air time in `unscheduled`; `prev`/`next` season refs |
| `GET /api/home` | any | `continue_watching` (started > 10 s, not completed, episode ready, newest first, max 20), `behind` (watching shows with aired episodes above progress, newest first), `new_this_week` (episodes of watching/planned shows aired in the last 7 days, max 50). Every row embeds an `AnimeSummary` and an `EpisodeOut`, so the hero's `banner_url`, the shelves' `studio`/`genres[]`/`cover_large_url` and the Up Next tiles' `still_url` all arrive in this one call (M15) |
| `POST /api/catalog/season-sweep` | admin | enqueue the season pre-cache now (deduped) |
| `GET /api/review?state=&limit=`, `GET /api/review/summary` | any | match-review queue: files below the auto-link threshold with top candidates and reasons; paths relative to `DATA_DIR`, never absolute. Each item may carry `suggestion` = `{anime_id, anime, episode_number, reason, confidence: high\|medium\|low, model, created_at, error}` (FR-L5; when `error` is set the rest may be null and the client shows "no suggestion: &lt;error&gt;"). The page carries `suggestions_enabled` = `LLM_MATCH_SUGGESTIONS` **and** a configured provider chain |
| `POST /api/review/{id}/confirm`, `…/ignore`, `…/reopen`, `GET …/search?q=` | any | resolve a file: link to (anime, episode) creating the episode row if needed; ignore; reopen an ignored one; search the catalogue for another title. Confirm is unaffected by any suggestion — it reads only its body |
| `POST /api/review/{id}/suggest` | any | ask a model which candidate this file is (FR-L5) → 202 `{job_id, status: "pending"}`, enqueuing `llm_suggest_match` with `force` (deduped per file). 404 unknown; 409 unless the file is `pending`; 503 `Suggestions are not enabled` when the flag is off or no provider is configured. **Never links anything** — the answer is stored for the queue to show |
| `GET`/`HEAD /media/{id}/index.m3u8`, `/media/{id}/{init.mp4\|seg_NNNNN.m4s}` | any (session cookie) | HLS delivery from `DATA_DIR/renditions/<id>/`; name validated by regex, path built from the id; 404 unless the episode is `ready`; playlist `no-cache`, init/segments `immutable` + ETag/304; Range → 206/416 (Starlette native) |
| `GET /api/episodes/{id}/play` | any | `PlayInfo`: episode (the same `EpisodeOut` the show page renders, so `title` and `still_url` come with it), anime (an `AnimeSummary`, so `cover_large_url` too), playlist URL, rendition duration, `resume_position` (10 s < pos < 95 %, not completed), previous/next refs with `ready` |
| `POST /api/progress` | any | upsert watch progress (also accepts `text/plain` beacons; Origin still required); ≥ 90 % → completed (sticky, `completed_at` once); newly completed → list progress raised if higher (`updated_by=arc`, `mal_dirty=true`; a Watching entry is created if none), then `compute_wants` enqueued |
| `POST`/`DELETE /api/episodes/{id}/watched` | any | manual mark / un-mark (un-mark never lowers list progress or MAL) |
| `POST /api/episodes/{id}/transcode?force=` | admin | enqueue a transcode: retry a `failed`/`matched` episode, or re-encode a `ready` one with `force=true` (409 otherwise) |
| `GET /api/retention/preview`, `POST /api/retention/sweep`, `POST /api/episodes/{id}/delete-files` | admin | what the next sweep would delete (reasons, bytes); run it now; delete one episode's files (404 unknown, 409 while in flight) |
| `GET /api/retention/disk` | admin | `{data_dir: {total, used, free}, retained: {sources, renditions, total}, episodes_retained}` — `shutil.disk_usage` on `DATA_DIR` (the nearest existing parent when it has not been created yet; the GET never creates it) beside Arc's own share, from the same `retained_usage` the acquisition status reports |
| `POST /api/acquisition/pause`, `POST /api/acquisition/resume`, `GET /api/acquisition/status` | admin | pause/resume acquisition (settings key `acquisition_paused`; while paused `compute_wants` does nothing and `search_release` requeues itself without touching Nyaa or qBittorrent; `poll_qbit` keeps ingesting); status shows paused, active wants, searching, downloading |
| `POST /api/episodes/{id}/search`, `POST /api/acquisition/compute-wants`, `POST /api/acquisition/poll`, `GET /api/acquisition/wants` | admin | trigger a release search / the wants reconciler / a qBittorrent poll (all deduped, 202); list active wants for debugging |
| `GET /api/acquisition/qbit` | admin | `{reachable, version, error, torrents[{hash, name, state, progress, size, dlspeed, upspeed, episode_id}]}`. The only route that calls qBittorrent inside a request (`app/version` + `torrents/info?category=arc`, read-only, `asyncio.timeout` bounding the **whole probe** at 5 s — the client logs in and retries a 403 once, so a per-request budget would be six of them) and the only one that **never fails**: down, wrong password or unconfigured is `reachable: false` with the reason in `error`, because that is the answer the admin came for. `episode_id` comes from Arc's `torrents` rows, not from the client's tags |

## 6. External integrations

| Service | Auth | Rate/limits | Notes |
|---|---|---|---|
| AniList GraphQL `https://graphql.anilist.co` (`ANILIST_URL`, overridable for tests) | none | documented 90 req/min, enforced ~30/min; client paces requests (`ANILIST_MIN_INTERVAL_MS`, default 700), honours `X-RateLimit-Remaining`/`Retry-After`, retries 429 once and 5xx twice | Queries: `SEARCH` (summary fields only; upsert never sets `refreshed_at`), `MEDIA_BY_ID` (full detail + first aired and upcoming schedule pages + relations + studio + `staff(sort: RELEVANCE, perPage: 12)` and `streamingEpisodes` for M15's credits and episode stills — detail-only, so a search page and a season sweep never pay for them), then `AIRED_SCHEDULE_PAGE` follow-ups while `hasNextPage` (cap 20 pages / 2000 episodes, logged if hit). A 429 without `Retry-After` waits 3 s (60 s is only the ceiling for a sent header). Detail is served from cache when `refreshed_at` < 24 h; unreachable AniList with nothing cached → 502 `anilist is unavailable`. |
| MAL API v2 `https://api.myanimelist.net/v2` | reads: `X-MAL-CLIENT-ID` header only; writes (M9): OAuth 2.0 PKCE (plain), client id + secret in env | modest | Catalogue fallback (read): `anime?q=`, `anime/{id}?fields=…`, `anime/season/{year}/{season}`; broadcast weekday/time used to synthesise episode air dates. List sync (M9): `users/@me/animelist`, `anime/{id}/my_list_status` (PATCH/DELETE). Tokens encrypted with Fernet key from env. |
| Nyaa RSS `https://nyaa.si/?page=rss&q=…&c=1_2&f=0` (`NYAA_URL`) | none | ≤1 req/2 s (asyncio-paced), 10-min cache per query, 20 s timeout, one retry | `c=1_2` = Anime English-translated. Up to 5 query forms per episode (romaji and english full titles, plus season-stripped base title with `S<k>`, roman numeral, and plain), ALL run and merged by info hash before ranking, because Nyaa ANDs every word and groups name shows differently (`Mushoku Tensei III: Isekai…` vs `Mushoku Tensei S3`). Items parsed with the same filename parser; kept only when kind=episode, episode number equal, title ≥ 0.90 similar (asymmetric: a release title that *extends* the entry's title with tokens not in any of the entry's own titles is a different show, e.g. a subtitled sequel), season agrees (1 assumed when unmarked on either side), not a remake, hash not already used by another episode. Ranked: preferred groups > preferred/fallback resolution > seeders > trusted; per-show overrides in `settings` key `override:anime:<id>`. |
| qBittorrent Web API (`QBIT_URL`, `QBIT_USER`, `QBIT_PASS`, `QBIT_CATEGORY`=arc, `QBIT_DOWNLOADS_PATH`=/data/downloads container-side) | cookie login, re-login on 403 | n/a | `torrents/add` (magnet, category, savepath `<downloads>/<episode id>`; handles 4.x `Ok.` and 5.x JSON/409-duplicate dialects idempotently), `torrents/info?category=arc`, `torrents/delete` (Arc category only), `app/setPreferences` (seeding policy applied at worker start and daily: ratio limit 0 with action Stop, seeding time 0, upload cap `QBIT_UPLOAD_LIMIT_KIB`), `torrents/stop` (any completed torrent still seeding is stopped by `poll_qbit` unless `QBIT_SEEDING`). Dev compose bind-mounts the repo's `data/downloads` so the host worker sees files; first-run temporary password must be replaced with `QBIT_PASS` (see README). |
| Gemini (AI Studio) `https://generativelanguage.googleapis.com/v1beta/openai/` (`GEMINI_BASE_URL`) | `GEMINI_API_KEY` | **free tier: ~20 requests/day/model for the whole deployment** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`), plus per-minute limits; 429 and 503 "high demand" are both common, and a busy model can end a stream after one chunk | The primary provider (`RECS_PROVIDER=gemini`). `RECS_MODEL` lists several models tried in turn — extra daily quota rather than better answers; 3.5 leads because it was the most *available* when measured. Python SDK `openai` (3.11); streamed chat completions, `response_format` json_schema, `reasoning_effort: low`. Reasoning tokens come out of `max_tokens` (16000). A daily-quota 429 puts that model on cooldown until 08:00 UTC. |
| OpenRouter `https://openrouter.ai/api/v1` (`OPENROUTER_BASE_URL`) | `OPENROUTER_API_KEY` | per account, paid | The fallback (`RECS_FALLBACK_PROVIDER=openrouter`), used once every Gemini model is spent for the day — it is the thing that still works when the free tier does not. Same code path; `RECS_FALLBACK_MODEL` is a `vendor/model` slug. Sends `HTTP-Referer`/`X-Title` for attribution. |
| Anthropic API | `ANTHROPIC_API_KEY` | n/a | Selectable as either chain end (`RECS_PROVIDER` or `RECS_FALLBACK_PROVIDER` = `anthropic`, `RECS_MODEL=claude-opus-5`). Python SDK `anthropic` (1.4.0); `client.beta.messages.stream` with adaptive thinking, `output_config.format` JSON schema, `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). Also M13's match suggestions, whatever the recs provider is. |
| Offline catalogue (M15.5, planned): manami `anime-offline-database` weekly release (`anime-offline-database.jsonl.zst`, ≈ 62 MB / 6 MB) + Fribb `anime-lists` (`anime-list-full.json`, ≈ 7.5 MB) | none | one download each per week (GitHub releases / raw) | Loaded by a weekly job into `offline_anime` and `offline_ids` (replace-on-import, release tag stored). First stop for search, filename matching and cross-id mapping (AniList ↔ MAL ↔ Kitsu ↔ AniDB ↔ TMDB series+season). Seeds season lists when live sources are unavailable. |
| TMDB `https://api.themoviedb.org/3` (M15.5, planned) | `TMDB_API_KEY` (free) | ~50 req/s; enrichment job paces at 4 req/s | Nightly enrichment of followed shows via the id map: backdrop → `banner_url`, poster → `cover_large_url`, episode stills/titles → `episodes.still_url`/`title`, credits. Fills only what AniList has not provided (cache rule 3). UI footer carries TMDB attribution. |

## 7. Security

- Cookie sessions (`arc_session`, HttpOnly, SameSite=Lax, Secure in prod,
  30-day sliding TTL re-issued to the browser when the server extends it;
  token stored as sha256 hash, deleted on logout, purged hourly by the
  worker). CSRF: every mutating `/api/*` request must carry an
  `Origin` (or `Referer`) matching `PUBLIC_URL` or, in dev, localhost:5173/8000;
  otherwise 403. Argon2id hashes; unknown emails run a dummy verify so timing
  does not reveal existence. Login rate-limited per IP (10/15 min) and per
  email (5/15 min) and on the public invite routes (20/15 min per IP),
  in-process (M11 may move it to Postgres). Behind Caddy, uvicorn trusts
  `X-Forwarded-For` only from the compose network CIDR (never `*`, which
  would let clients spoof their IP), runs a single worker until the limiter
  is shared, and has its access log off (invite tokens are path segments;
  Caddy redacts `/api/*` URIs in its log). Argon2 runs in a thread, not on
  the event loop. `/docs`, `/redoc`, `/openapi.json` are disabled in prod.
  The last active admin cannot be deactivated or demoted.
- Invite tokens: random 32 bytes, stored hashed, 7-day default expiry (max 30
  days), single use enforced by `UPDATE … WHERE used_at IS NULL`; revoke sets
  `expires_at = now()`. Accept creates a `user`-role account and logs in.
- All `/api` and `/media` routes require a session; admin routes check role.
- MAL tokens and any stored secrets encrypted at rest; app secrets via env.
- Media directories not served statically; paths derived from ids, never
  from user input.
- qBittorrent Web UI bound to the internal Docker network only.

## 8. Deployment

`deploy/docker-compose.yml` services (all `restart: unless-stopped`,
json-file logs 20 MB × 5, healthchecks: api via `/api/health`, worker via
`python -m arc.worker --check` on a heartbeat file the scheduler rewrites
every 30 s, qbittorrent via its WebUI, caddy via its admin API, backup via
dump freshness; `depends_on` uses `service_healthy`):

- `caddy` — image `arc-web` built from `client/Dockerfile` (node build stage
  → SPA baked into the Caddy image, no host Node needed); TLS via Let's
  Encrypt; security headers (HSTS only over https, nosniff, referrer
  policy, frame deny, `Server` stripped); `/assets/*` cached a day,
  `index.html` no-cache; proxies `/api` and `/media` to `api` with Range
  passed through.
- `backup` — `postgres:18` running `deploy/backup.sh`: gzipped `pg_dump`
  on start and every `BACKUP_INTERVAL_SECONDS` (86400) into the separate
  `backups` volume, kept `BACKUP_KEEP_DAYS` (14, never the newest);
  `make backup`, `make backups`, `make restore file=…`.
- `gluetun` — WireGuard VPN sidecar (`qmcgaw/gluetun:v3`, kill switch by
  default, exposes the WebUI port on the backend network under the alias
  `qbittorrent`). Compose profiles select the torrent client:
  `COMPOSE_PROFILES=vpn` (default via the Makefile) runs `qbittorrent-vpn`
  with `network_mode: service:gluetun`; `novpn` runs a plain `qbittorrent`
  on the backend network. `QBIT_URL=http://qbittorrent:8080` is identical in
  both modes. Dev always uses `novpn`.
- `api` — `uvicorn arc.main:app`, 2 workers.
- `worker` — `python -m arc.worker`, 1 instance (raise for more transcode
  parallelism; ffmpeg concurrency capped by `MAX_TRANSCODES`).
- `db` — postgres:18 with a volume mounted at `/var/lib/postgresql` (18's layout).
- `qbittorrent` — linuxserver/qbittorrent, `/data/downloads` volume shared
  with api/worker, Web UI on internal network only.

Volumes: `arc_data` (`/data`: downloads, renditions, manual, fonts, worker
heartbeat), `pgdata`, `backups`, `qbit_config`, `caddy_data`,
`caddy_config`. `make up` = build → `alembic upgrade head` in a one-off
`api` container → up. Ops runbook: `deploy/README.md`; CLI:
`python -m arc.cli {status,invite,warm-catalogue,demo-list}`.

Host (decided 2026-09-08, revised 2026-09-09 when Hetzner's cost-optimised
line went out of stock): Hetzner Cloud CPX22 (2 AMD vCPU, 4 GB, 80 GB NVMe;
Helsinki), Ubuntu 26.04, a 100 GB volume auto-mounted by Hetzner under
`/mnt/HC_Volume_<id>`; `ARC_DATA_DIR` points at a directory on it and
`deploy/docker-compose.host.yml` (added by `make` when the key is set) binds
the `arc_data` volume there. Postgres stays on the NVMe, which Hetzner's
backups snapshot; the volume holds only re-acquirable media. 2 GB swap,
`MAX_TRANSCODES=1`. Hetzner firewall: inbound 22/80/443 + ICMP only. A
primary IPv4, Hetzner backups on. Arc lives in its own Hetzner
project on the owner's account. Domain (decided 2026-09-09): `atomworks.dev`
on Cloudflare Registrar, the owner's umbrella for hobby projects; Arc is
`arc.atomworks.dev` (`PUBLIC_HOST`), an A record with the Cloudflare proxy
off (DNS only) so Caddy terminates TLS and streams directly. Measured: software x264 `veryfast`
transcodes a 24-min 1080p episode in ~4 min on an M-series laptop; expect
5–8 min on the CX33. Hardware encode is not available on CX; not needed.

Torrent isolation: qBittorrent runs with `network_mode: service:gluetun`
behind a `gluetun` container holding a WireGuard config from the VPN
provider (kill switch on, so a tunnel drop stops qBittorrent's traffic
instead of leaking to the host IP); `api`/`worker` reach its WebUI through
gluetun's exposed port on the backend network. qBittorrent preferences set
at startup by Arc: stop seeding on completion (ratio limit 0, action
pause), upload rate capped low during transfer. Nothing else uses the VPN.

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
`MAL_REDIRECT_URI`, `RECS_PROVIDER` (gemini|openrouter|anthropic, default
gemini), `RECS_MODEL` (comma-separated, tried in order; default
`gemini-3.5-flash,gemini-3.6-flash,gemini-2.5-flash`),
`RECS_FALLBACK_PROVIDER` (blank = no fallback), `RECS_FALLBACK_MODEL`
(comma-separated; defaults to `openai/gpt-5-mini` when the fallback is
openrouter), `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`
(all optional — a model name that does not match its provider, a missing
primary key, and a fallback named without its key are each a startup WARNING),
`GEMINI_BASE_URL`, `OPENROUTER_BASE_URL` (blank = the provider's default),
`LLM_MATCH_SUGGESTIONS` (bool, default false — match suggestions ride the same
provider chain as the recommendations, so it needs no key of its own; on in
production with nothing in the chain configured is a startup ERROR),
`QBIT_URL`, `QBIT_USER`, `QBIT_PASS`, `DATA_DIR`, `MAX_TRANSCODES`,
`BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`, `MATCH_AUTO_THRESHOLD` (0.85),
`MATCH_MIN_CANDIDATE` (0.40), `MATCH_MIN_TITLE_FOR_AUTO` (0.92; above the
0.88 non-exact cap, so in practice auto-link needs an exact normalised title
or a trusted prior), `LIBRARY_SCAN_INTERVAL_SECONDS` (120),
`LIBRARY_SETTLE_SECONDS` (60), `LIBRARY_SCAN_BATCH` (200),
`LIBRARY_SCAN_COMMIT_EVERY` (25), `VIDEO_EXTENSIONS`, `NYAA_URL`,
`QBIT_CATEGORY` (arc), `QBIT_DOWNLOADS_PATH` (/data/downloads), `FFMPEG_BIN`,
`FFPROBE_BIN`, `FFMPEG_VIDEO_ENCODER` (libx264), `TRANSCODE_PRESET` (fast),
`TRANSCODE_CRF` (19), `TRANSCODE_TUNE` (animation; empty means no `-tune`),
`TRANSCODE_MAXRATE_KBPS` / `TRANSCODE_BUFSIZE_KBPS` (both unset; a maxrate
with no bufsize gets twice the maxrate), `HLS_SEGMENT_SECONDS` (6), `TRANSCODE_TIMEOUT_SECONDS`
(10800), `RETENTION_DRY_RUN` (false), `BACKUP_INTERVAL_SECONDS` (86400),
`BACKUP_KEEP_DAYS` (14), `QBIT_UPLOAD_LIMIT_KIB` (512), `QBIT_SEEDING`
(false), `COMPOSE_PROFILES` (vpn|novpn), `VPN_PROVIDER`, `WIREGUARD_PRIVATE_KEY`,
`WIREGUARD_ADDRESSES`, `WIREGUARD_PUBLIC_KEY`, `WIREGUARD_ENDPOINT_IP`,
`WIREGUARD_ENDPOINT_PORT`, `VPN_SERVER_COUNTRIES/CITIES` (deploy-only, read
by gluetun), `WORKER_CONCURRENCY`
(default 2), `WORKER_POLL_INTERVAL` (seconds, default 1), `WORKER_DRAIN_TIMEOUT`
(seconds to wait for in-flight jobs on shutdown, default 30),
`WORKER_STALE_AFTER` (seconds before a `running` job with a dead worker is
requeued, default 7200; transcodes heartbeat their lock),
`SESSION_TTL_DAYS` (30), `LOGIN_RATE_LIMIT_PER_IP` (10),
`LOGIN_RATE_LIMIT_PER_EMAIL` (5), `LOGIN_RATE_WINDOW_SECONDS` (900),
`CORS_ALLOWED_ORIGINS` (comma list, optional; dev origins are added
automatically when `ENV` is not prod), `MAL_OAUTH_URL`
(https://myanimelist.net, the authorize/token host), `MAL_IMPORT_INTERVAL_HOURS`
(6). `FERNET_KEY` must be set before anyone links MAL and is not rotatable
without re-linking every account. In prod, `arc.main` and `arc.worker` log
an ERROR per production-required key that is missing or still a
placeholder, and `/api/health` reports `config_warnings` (a count).

Deploy-only (compose/Caddy, not read by the app): `PUBLIC_HOST`,
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `PUID`, `PGID`, `TZ`.
`.env` holds host-side values (`localhost` URLs) used by `make dev`, tests,
and processes run directly; inside Compose the `api`/`worker` services
override `DATABASE_URL` and `QBIT_URL` with the service hostnames
(`db`, `qbittorrent`) structurally, alongside `ENV=prod`.

Rule values (N, G, D, groups, resolution,
languages) live in the `settings` table and are editable by admin, seeded by
the initial migration. **Every key in `DEFAULT_SETTINGS` is admin-editable at
runtime** through `GET`/`PUT /api/settings` — nothing in that table needs a
redeploy, a shell or a database console to change, which is M14's definition
of done. `MAX_TRANSCODES` is host capacity and lives only in env (it is not in
the settings table).

Configuration problems are checked at startup by both the api and the worker
(`arc/core/config_check.py`) and logged at two levels. **ERROR** means the
deployment is broken -- a missing `SECRET_KEY`, a localhost `PUBLIC_URL` -- and
is what `GET /api/health` counts as `config_warnings`; a healthy deploy reports
0 — `LLM_MATCH_SUGGESTIONS` on with no provider in the chain is one of these,
because the flag is an operator saying the feature should be on. **WARNING**
means an optional feature is off and does *not* count: today, the
recommendations key being unset, and a `RECS_MODEL` that does not look like
the selected `RECS_PROVIDER`'s. "Is anything configured" is
`config_check.model_chain_configured`, a deliberate restatement of
`recs.factory.chain_entries` (the factory imports `is_placeholder` from
config_check, so importing it back would be a cycle) with a test pinning the
two together.

## 10. Testing strategy

- Parser/matcher: corpus of ≥ 200 real release names with expected
  (title_key, episode, season, kind, group, version); assert confidence tiers.
- Acquisition window: property tests over progress/N/aired combos.
- MAL rules: table-driven tests proving no write occurs without a
  user-originated event, progress never lowered automatically, revert
  round-trips.
- Retention: time-travel tests with frozen clock.
- API: httpx AsyncClient against a temp Postgres (testcontainers) or SQLite
  fallback for fast runs.
- Media: ffmpeg invoked on a 5-second fixture in a dedicated test marked
  `slow`; mocked elsewhere.
- Recommendations: every backend is behind a `RecsModel` protocol and is a
  fake everywhere except `server/tests/recs_eval`, whose `live` test calls
  whichever provider `RECS_PROVIDER` names (and skips when that provider has
  no key). `live` is excluded by pytest `addopts` so `make test` never
  spends money; run it with `uv run pytest -m live` and an exported key. The
  eval itself is five frozen runs (list + pool + recorded answer) asserting
  what FR-R3/FR-R4 imply: 3–5 picks, all from the pool, none already watched,
  every case naming a show from the user's history.

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
- 2026-09-05 — M2 auth: `invites.created_at` added (migration 2); email
  mismatch on accept is 409; bootstrap admin from env at API startup, never
  overwrites; session purge is an hourly scheduler task in the worker, not a
  job row; `/api/jobs` is admin-only.
- 2026-09-05 — M2 security review fixes: forwarded-IP trust limited to the
  compose subnet; one uvicorn worker until the rate limiter is shared;
  bootstrap tolerates the multi-worker race; CORS outermost; sliding session
  cookie re-issued; rate-limiter memory bounded; Argon2 off the loop;
  `PUBLIC_URL` derived from `PUBLIC_HOST` in compose; zero-admin guard;
  invite routes rate-limited; docs hidden in prod; `.env.example` ships no
  admin password; `ix_sessions_expires_at` (migration 3).
- 2026-09-06 — M3 catalogue: `anime.description` column (migration 4);
  `AnimeDetail.episodes` is the episode list and `episode_count` the AniList
  count; preferred title = english else romaji; relations filtered to anime;
  episode rows created from the airing schedule (never deleted, state never
  downgraded); refresh jobs: daily 04:00 UTC sweep of followed/releasing
  shows plus hourly pre-air sweep (−6 h … +90 min). AniList was disabled
  upstream (403) during M3, so fixtures are hand-built from public data and
  `scripts/capture_anilist.py` must be re-run when it returns.
- 2026-09-06 — M3 review fixes: aired schedule paged past 100 episodes;
  `aired` also inferred from the highest aired number for NULL air dates;
  search upsert is one multi-row statement; refresh sweeps space children
  5 s apart starting after the last queued refresh; job names live in a
  handler-free `anilist/names.py`; `scripts/` is linted.
- 2026-09-06 — M3b: internal anime ids with AniList/MAL external ids; MAL
  official API as read-only fallback behind a `CatalogSource` interface and
  a circuit breaker; estimated air dates from broadcast slots; migrations
  squashed into a single initial revision since nothing has shipped.
- 2026-09-06 — M4: schedule and home endpoints read the cache only; behind
  counts only `watching` shows; new-this-week covers watching+planned; the
  season sweep carries `nextAiringEpisode`; stale next-airing ignored after
  7 days.
- 2026-09-06 — M4 review fixes: estimated next-airing never overwrites
  AniList data; `next_at_estimated` on schedule entries; sweep schedules
  detail refreshes for unplaced season rows; `PATCH /api/users/me` lets a
  user change their timezone (spec §2 users now have an editable timezone).
- 2026-09-06 — M5: parser corpus + matcher acceptance fixtures are the
  specification; prior is a bounded bonus; non-exact cap 0.88; review API
  open to any signed-in user (admin-only scoping deferred to M14);
  `ffprobe` optional locally (now installed via brew).
- 2026-09-06 — M5 review fixes: two-bar auto-link rule (confidence AND
  close title / trusted prior); prior needs title ≥ 0.60; movies match as
  episode 1 of a MOVIE entry; recap `.5` files always review; an existing
  link is never cleared automatically; scans batch and commit incrementally.
- 2026-09-06 — M6 acquisition: on_hold generates no wants; `p` is the max of
  completed and list progress; season assumed 1 when unmarked on either
  side; unavailable episodes retry after 1 day; per-show overrides in the
  settings table (no schema change); qBittorrent 5 add dialect handled;
  live DoD run downloaded one episode end to end.
- 2026-09-06 — M7 transcode: progress/failure in the job payload (no
  schema change); subtitle + fonts extracted to temp files before the
  single encode pass; `ready → preparing` edge for force re-encode;
  ffmpeg-full for local dev (libass); live encode of a real 1080p episode
  verified by eye (subtitle burned in).
- 2026-09-06 — M7 review fixes: advisory lock + temp-dir rename around the
  encode; heartbeat while queued for a slot; failure reason keeps its head;
  partial output cleaned on failure; home route carries episode progress
  fields; `rendition.notes`; expression index migration for transcode-job
  lookups.
- 2026-09-06 — Nyaa search runs every query form and merges by hash; the
  first live pick (Erai-raws) was correct over a partial pool that missed
  the more-seeded SubsPlease release because the search stopped at the
  first form that returned anything.
- 2026-09-07 — M8 streaming/player: session-gated HLS with native Range,
  ETag/304 and immutable segment caching; 90 % completion raises list
  progress only (status untouched) and creates a Watching entry when
  missing; un-mark never rolls back; player outside the app shell; hls.js
  lazy chunk; beacon reporting.
- 2026-09-07 — M9 MAL sync: stateless encrypted OAuth state; `conflict`
  cause and `skipped` status added to the write log (never writes);
  watch-cause pushes never lower progress or clear a score; import never
  deletes local data; revert = newest ok row per field.
- 2026-09-07 — M9 review fix: per-field causes live in pending log rows
  written at event time, not in the job payload (dedupe was dropping the
  cause and could let a watch event lower MAL progress); push consumes the
  rows.
- 2026-09-08 — Live MAL link verified end to end on the real account (784
  entries imported in chunks; create, watch advance, revert and delete all
  landed on MAL and were undone). The first import immediately wanted ~30
  episodes of the imported Watching/Planned shows and saturated the worker
  with searches while a MAL push waited; added the acquisition pause switch
  (migration 4 seeds the key) and job priorities. Acquisition is paused in
  dev until the owner resumes it.
- 2026-09-08 — M10 retention: grace anchored on completions from
  watch_progress plus dropped wants (want rows vanish when a user watches
  past them); the D window also runs from the last list edit so a revived
  want is not re-dropped by the same run; stale drops are sticky until the
  user acts; manual-drop root deletable; drop_reason stored as the spec's
  literal text.
- 2026-09-08 — M10 review fixes: wants that end because the show left
  watching/planned are dropped, not deleted; MAL imports keep `updated_at`
  for undated unchanged rows; stale-drop revival needs an Arc-side action;
  qBittorrent outage skips per episode; no-files episodes reset.
- 2026-09-09 — Host revised to CPX22 + 100 GB volume (CX line out of stock);
  `ARC_DATA_DIR` + `deploy/docker-compose.host.yml` bind media to the volume.
- 2026-09-09 — Domain: `atomworks.dev` (Cloudflare), Arc at
  `arc.atomworks.dev`, DNS only (no proxy); MAL callback moves to that host.
- 2026-09-08 — Hosting: Hetzner CX33 + 250 GB volume, own project; gluetun
  VPN for qBittorrent only; seeding disabled at startup via preferences.
- 2026-09-08 — M11 readiness: healthchecks incl. worker heartbeat file,
  backup service + restore targets, client baked into the Caddy image,
  security headers (HSTS bug over http caught and fixed), startup config
  check, CLI (`status`, `invite`, `warm-catalogue`, `demo-list`),
  `deploy/README.md` runbook.
- 2026-09-08 — gluetun sidecar behind compose profiles (`vpn` default,
  `novpn`); seeding disabled via qBittorrent preferences + a stop-on-seed
  poll; dev's 697 torrent now stopped rather than seeding.
- 2026-09-10 — M12 server: `arc/services/recs/` (pool, history, prompt,
  schema, claude, runs) and `GET`/`POST /api/recs`. Picks are identified by
  Arc's internal `anime_id` rather than §5.6's original `anilist_id` — the
  catalogue moved to internal ids in M3b and a cached row may have no AniList
  id. The pool gives each source a share of the forty (20/12/rest) because a
  season alone is larger than the pool; `anime` has no popularity column, so
  seasonal order is insertion (`id`) order, which is the order the
  `POPULARITY_DESC` sweep wrote. 3–5 picks and the pool-membership rule are
  enforced in code, not in the schema (structured outputs support neither
  `minItems` nor string lengths). `RECS_MODEL` added (default
  `claude-opus-5`); a missing `ANTHROPIC_API_KEY` in prod is a WARNING rather
  than an ERROR and does not count towards `/api/health`'s
  `config_warnings`, so a phase 1 deploy still reports zero. Rate limit reads
  `rec_runs` directly — no migration. Prompt eval in `server/tests/recs_eval`
  with five recorded runs; the live test is marked `live` and excluded from
  `make test` by `addopts`.
- 2026-09-10 — Recommendations ship on Gemini free tier via its
  OpenAI-compatible endpoint; Anthropic and OpenRouter selectable by
  RECS_PROVIDER. One `RecsModel` protocol, two implementations
  (`claude.py`, `openai_compat.py`) and a `build_recs_model` factory that
  returns `None` — never raises — when the selected provider has no key.
  `LLM_API_KEY`/`LLM_BASE_URL` added; `RECS_MODEL` default becomes
  `gemini-3.8-flash` and is not interchangeable between providers. Three
  things were established against the live endpoint and contradict how the
  options are usually written up: Gemini takes its `thinking_config` under
  a body field itself named `extra_body` (`{google: …}` at the top level is
  a 400); its reasoning tokens are drawn from `max_tokens`, so 6000 returned
  half-written JSON and the budget is now 16000; and a truncated answer can
  arrive without `finish_reason: length`, so unparseable JSON on a stream
  that never said `stop` is treated as truncation. The refusal 503 detail is
  now provider-neutral ("The model declined this request").
  **Open for the owner:** the Gemini free tier allows 20 requests per day
  per model for the whole deployment, while FR-R5's limit is 10 runs per
  *user* per day — so a third active user can exhaust the provider before
  Arc's own limit binds, and they see a 502. Options: lower FR-R5's limit,
  add a deployment-wide daily cap, or pay for a Gemini tier.
- 2026-09-10 -- M12 review pass. `arc/services/recs/base.py` now holds the
  `RecsModel` protocol, the exception hierarchy, `parse_picks` and the retry,
  so neither backend imports the other. Both budgets raised to 16000 tokens
  with reasoning turned down, because on **both** providers the reasoning is
  drawn from the same budget as the answer and a small one deletes it rather
  than shortening it. Per-attempt timeout 20 s, SDK retries off, one retry of
  our own after 1 s -- retrying unavailability and truncated/empty streams,
  never a refusal or a schema mismatch. Gemini's `thinking_config` passthrough
  replaced by the portable top-level `reasoning_effort: low`:
  `gemini-2.5-flash` rejects the Google form outright (400 "Thinking level is
  not supported for this model"), and an OpenAI-compatible provider that does
  not understand the portable one ignores it. Default `RECS_MODEL` is now
  `gemini-3.5-flash` on measurement -- 3.7 and 3.8 were both under "high
  demand" (early-terminated streams, a 503, one 47 s answer) while 3.5 answered
  a full forty-candidate prompt in about four seconds. The run commits after
  building the pool and before calling the model, so no Postgres row locks are
  held across a third-party call; a fetched relation therefore survives a
  failed run, which is correct (it is cache). The mood prompt is fenced and the
  system prompt states it is data. Pool sources bounded (season `LIMIT 4x cap`,
  50 relation seeds, 10 fetches or a 5 s deadline). `config_check` gained a
  `RECS_MODEL`/`RECS_PROVIDER` mismatch warning, and the factory treats a
  placeholder key as no key.
- 2026-09-10 — Recommendation model chain: Gemini free-tier models in rotation
  with a daily-quota cooldown, OpenRouter as the paid fallback (owner
  decision). `RECS_MODEL` and `RECS_FALLBACK_MODEL` are comma-separated lists
  tried in order; keys are per provider (`GEMINI_API_KEY`,
  `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`) because the chain holds two
  providers at once, which `LLM_API_KEY` could not express. Only
  `RecsUnavailable` advances the chain — a refusal or an unusable answer stops
  it, since neither is about availability. A 429 is classified from its body:
  a per-day quota id (or the free tier's `limit: 20`) means the model is spent
  and it is skipped until the next 08:00 UTC; anything else is transient and
  merely moves on. Cooldowns are in memory on the chain object, so a restart
  costs one wasted request. `GET /api/recs` exposes the chain's state to admins
  only.
- 2026-09-10 — Continuations shown as a deterministic secondary section; main
  pool ranks by AniList popularity/score and excludes recaps, specials and
  franchise continuations (owner decision). `anime` gained `popularity` and
  `average_score` (migration `95d81ee797af`, nullable and unindexed, filled
  from AniList's `popularity`/`averageScore` and MAL's
  `num_list_users`/`mean`×10); existing rows fill in on their next refresh
  rather than by a backfill, since `NULLS LAST` sorts an unranked row where it
  belongs anyway. Continuations reuse the pool's bounded relation resolution
  and are stored in `rec_runs.picks` beside the picks with a `kind` tag, so
  neither the column nor the API's existing pick shape changed.
- 2026-09-10 — M13: match suggestions ride the M12 provider chain; shown in the
  review queue, never applied. `base.py` grew a general layer — `JsonModel`
  with `complete(system, user, schema, name) -> JsonResult` — and
  `RecsModel.recommend` became a one-line adapter over it, so the rotation,
  the daily-quota cooldowns, the retry and the refusal handling are shared
  rather than reimplemented; `build_recs_model` is now an alias of
  `build_model`, and the chain is one per process on both sides — the API's on
  `app.state`, the worker's a module-level `shared_model` closed at shutdown —
  so the daily-quota cooldowns outlive a single job. `LLM_MATCH_SUGGESTIONS`
  now means
  "the M12 chain is configured" rather than "there is an Anthropic key", and
  the config check names the flag rather than `ANTHROPIC_API_KEY`.
- 2026-09-10 — M14: settings API (validated), job retry/cancel + summary,
  qBittorrent status, disk usage; overrides editor deferred to M16.
- 2026-09-11 — M15 sign-off (owner): Schedule joins the toolbar nav (and the
  top of the phone "More" sheet; the four tabs are unchanged). Watch Now's
  hero becomes a cycling set of season recommendations built client-side —
  **at most two** slides for the latest run's picks whose show airs this
  season or next (banner-first among them), then the rest of the six from
  unfollowed shows of the cached season, *ranked* by overlap with the viewer's
  top-3 genres (weighted by list score, dropped entries excluded) rather than
  filtered by it, then by `popularity` (nulls last), then by having a banner,
  then by the grid's own order — hidden only when there is genuinely nothing.
  Revised the same day after the owner saw a hero of nothing but last night's
  picks: season rows cached through the MAL fallback carry no genres until a
  detail fetch reaches them, so a ≥ 2-genre *filter* excluded the whole season
  while the "list has no genres" escape hatch stayed shut. The hero ranks on
  the `popularity` that `AnimeSummary` gained the same day (see the entry
  below) rather than on the order the grid was built in — the same key the
  recommendation pool has always used; the client ranks by `popularity` and
  `average_score` and renders neither. It otherwise reads four caches that
  already exist
  (`/api/home`, `/api/schedule`, `/api/recs`, `/api/list`). Continue watching
  becomes its own shelf and "Up Next" narrows to ready-but-unstarted episodes,
  renamed "Ready to watch".
- 2026-09-12 — M15 hero framing (owner: "too zoomed in"): a banner hero sizes
  its *frame* to the banner instead of cropping every banner to 21:9. AniList
  banners are ~1900 × 400 (≈4.75:1) and `object-fit: cover` in a 2.33:1 frame
  showed only the middle half of one. `HeroFrame` reads the image's intrinsic
  size on load and sets `aspect-ratio` inline, clamped to [21/9, 3.6], with a
  200 ms transition; 21:9 stays the class-level default until the image loads,
  and a 16:9 backdrop from any later source still lands on 21:9. Poster heroes
  stay 21:9 — the wash fills the frame, it does not shape it. `Artwork` gained
  `aspect` (measured ratio, inline) and `onNaturalSize` (intrinsic size on
  load, reported from both the ref and `onLoad` so a cached image still
  counts).
- 2026-09-11 — M15 hero art (owner: "hero art is extremely low quality"): a
  hero never scales a poster up to fill its 21:9 frame. `components/ui/
  HeroFrame` picks between two frames — AniList's ~1900 px `banner_url`
  cropped to the frame, or, when there is none (the ordinary MAL case), a
  *poster hero*: the cover blurred (40 px, scale 1.15, brightness 0.5,
  saturate 1.2) as a colour wash behind the scrim, with the crisp 2:3 poster
  laid beside the title (sized from the frame's height, ≈180 px on a laptop).
  Both Watch Now's carousel and the show page use it, and hero images are
  `loading="eager" decoding="async"` (`Artwork` gained an `eager` prop).
  Watch Now's hero set now also prefers shows that *have* a banner: candidates
  are partitioned banner-first, and banner-less ones are only admitted when
  fewer than three banners qualified. `anime.ts` gains `bannerArt`/`hasBanner`;
  `heroArt` (banner → key visual) stays, for episode stills with none of their
  own. The toolbar mark is now a `<Link to="/">` labelled "Arc — Watch Now"
  with a decorative `<img>` (owner).
- 2026-09-11 — M15.5 approved (owner): offline catalogue import (manami DB +
  Fribb id map) as the first stop for search/matching/ids; TMDB enrichment for
  art, stills and credits behind AniList. Kitsu not adopted.
- 2026-09-11 — M15 shell: the left sidebar becomes a top toolbar with an avatar
  menu, and phones get a bottom tab bar with a "More" sheet (owner decisions,
  2026-09-11). The palette in `client/src/index.css` is replaced wholesale by
  the design handoff's tokens; `--arc-accent`/`--arc-accent-contrast` stay as
  aliases of ember until every page is restyled, and `--arc-ok/warn/error` are
  re-tuned for the new ground (all ≥ 4.5:1). Shared primitives land in
  `client/src/components/ui/`; `CoverThumb` becomes a thin wrapper over
  `Artwork` so untouched pages keep rendering. `Search.tsx` now follows `?q=`
  rather than only seeding from it, because the toolbar owns the search field.
- 2026-09-11 — M15 data: key art (extraLarge cover), staff credits and episode
  stills/titles from AniList for the redesigned Show and Home; MAL fallback
  carries studio only. Three nullable columns (`anime.cover_large_url`,
  `anime.credits`, `episodes.still_url`), one revision, no backfill: every row
  fills in on its next catalogue refresh. `staff` and `streamingEpisodes` go in
  `DETAIL_SELECTION` and not in the summary fragment, so search and the season
  sweep stay as cheap as they were; `coverImage.extraLarge` was already in the
  fragment, so `cover_large_url` is a summary column and a card gets it free.
  Episode titles and stills are written **only where null**
  (`coalesce(episodes.<col>, excluded.<col>)`), so a title confirmed in the
  match queue outranks every later refresh, and an entry whose
  "Episode N - Title" does not parse is ignored rather than placed by guess —
  the one exception being a list exactly as long as the show, where AniList's
  order is the episode order.
- 2026-09-11 — Cache rule 3 generalised: the *fallback* source may fill nulls
  but never overwrite a non-null value the *primary* wrote, and that now covers
  the **summary** columns as well as the detail ones, judged against
  `summary_source` and `detail_source` respectively. Strength is read off
  `SOURCE_NAMES` (the order `CatalogService` tries them), so equal ranks are
  not outranked — a source always corrects its own earlier answer — and a third
  source would need no new branch. A fill-only pass leaves the source column
  alone, so it does not quietly become the author of columns it did not write.
  Found because a MAL-fallback refresh was replacing AniList's 1900 px cover
  with MAL's 230 px one, which the M15 redesign renders at card and hero size.
  The racing `INSERT … ON CONFLICT` in `_insert_new` stopped writing summary
  columns from `excluded` at the same time — it now assigns the arbiter column
  to itself, a no-op that still returns the row — so `_apply` is the only place
  precedence is expressed.
- 2026-09-11 — `AnimeSummary` gains `genres[]`, `banner_url` and `studio`
  (existing columns, no migration) for Home's hero, Browse's chips and My
  List's rows. They are *detail* columns served on a summary, read off the
  cached row: empty/null on a show only a search has touched, filled on its
  next detail fetch. Adding them to the AniList summary fragment was rejected —
  twenty extra field sets per keystroke and two hundred per season sweep for a
  chip — and adding them to the summary *write* path would blank them on every
  search that passed over a detailed row.
- 2026-09-11 — `AnimeDetail.relations[]` gained `cover_url`, `cover_large_url`,
  `episodes` and `season_year` for the Show page's "The franchise, in order"
  rail, read from the cached `anime` row in the same single query that already
  resolved the internal id (now selecting named columns into a `RelatedAnime`
  record rather than loading whole rows). Null on a relation Arc has no row
  for, and deliberately so: fetching a dozen relations per show page would be a
  dozen upstream requests against a 30/min budget to fill a rail nobody may
  scroll to. They fill in when somebody opens the related show or a sweep
  reaches it. The internal id stays `id` — the field that has always carried
  it — rather than gaining a second name.
- 2026-09-11 — `GET /api/anime/search` merges local catalogue hits in front of
  the live results (`catalog/local.py`): every word of the query must appear as
  a case-insensitive substring of `title_romaji`, `title_english` or the
  synonyms, ordered exact/prefix title → followed → popularity, capped at 20,
  page 1 only, de-duplicated with the live page by internal id. An upstream
  failure with local hits is now a 200 rather than a 502. The owner searched
  "jobless reincarnation" during the AniList outage and got nothing back for a
  show that was cached, on their list and downloading, because the MAL fallback
  matches whole words from the start of a title.
- 2026-09-11 — Transcode defaults raised to preset fast / CRF 19 / tune
  animation after the owner judged veryfast/CRF 20 too soft; configurable
  (`TRANSCODE_PRESET`, `TRANSCODE_CRF`, `TRANSCODE_TUNE`, and an optional
  `TRANSCODE_MAXRATE_KBPS`/`TRANSCODE_BUFSIZE_KBPS` VBV ceiling, replacing
  `FFMPEG_PRESET`/`FFMPEG_CRF`). The subtitle burn-in was ruled out as the
  cause: nothing in the filter graph scales or converts, so there is no
  `-sws_flags` to add. Measured cost on a 60 s 1080p excerpt with subtitles
  burned in: 2.6x the CPU of the old settings for 3% more bitrate.
- 2026-09-11 — Continue watching lists any episode with an in-progress
  position, completed or not (rewatches). The shelf's rule is the saved
  position alone: ≥ 30 s in and short of the tighter of 95 % and the last
  minute, ordered by `updated_at` desc, and `/api/episodes/{id}/play` now
  returns `resume_position` for a completed row as well. The owner rewatched a
  watched episode, stopped at the midpoint, and Home had nothing to offer.
  Completion's other meanings — the MAL push, the once-only list advance, the
  watched marks — are untouched.
- 2026-09-11 — `AnimeSummary` also gained `popularity` and `average_score`
  (existing columns, no migration). Unlike `genres`/`banner_url`/`studio` these
  are real summary columns — both are in AniList's search fragment — so a card
  carries them straight off a search. Noted while doing it: the three captured
  AniList fixtures predate the 2026-09-10 fragment change and carry neither, so
  they read as null in every AniList-driven test until
  `scripts/capture_anilist.py` can run again (AniList has been 403
  "temporarily disabled" all day).
