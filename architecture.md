# Arc — Architecture

> Living document. Update whenever the stack, a component boundary, or an
> integration changes. Last updated: 2026-09-09.
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
| `invites` | id, token_hash, email (optional), created_by (SET NULL), created_at, expires_at, used_at |
| `sessions` | id (opaque token hash), user_id, expires_at, user_agent |
| `anime` | id (internal identity PK), anilist_id (unique, nullable), mal_id (unique, nullable), summary_source / detail_source (anilist|mal), title_romaji, title_english, title_native, synonyms (JSONB), description (AniList HTML, stripped on output), format, episodes, status, season, season_year, cover_url, banner_url, genres (array), tags (JSONB), studio, relations (JSONB, anime-only), next_airing (JSONB), refreshed_at |
| `episodes` | id, anime_id, number, title, air_at, air_at_estimated (true when synthesised from a MAL broadcast slot), state (enum, §6 of spec), state_changed_at, unavailable_reason |
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
   `llm_suggest_match`, which asks Claude for the most likely candidate + one
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
  release name), `libx264 veryfast crf 20 yuv420p high@4.1`, AAC 160k
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
  MediaSource (iOS Safari) gets the native `src` path; the player page renders outside the sidebar layout; resume is
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
| `POST /api/jobs`, `GET /api/jobs`, `GET /api/jobs/{id}` | admin | job queue |
| `GET /api/anime/search?q=&page=` | any | live AniList search, results cached |
| `GET /api/catalog/status` | admin | source health and breaker state |
| `GET /api/mal/status`, `POST /api/mal/link`, `GET /api/mal/callback`, `DELETE /api/mal/link`, `POST /api/mal/import`, `POST /api/mal/push`, `GET /api/mal/log`, `POST /api/mal/log/{id}/revert` | any (own account) | MAL link, import, push pending, write log, revert |
| `GET /api/anime/{id}` | any | (internal id) `AnimeDetail` + `anilist_id`, `mal_id`, `source`; `list_entry.mal_sync` state; `relations[]` carry `id` (internal, null when Arc has no row yet) plus `anilist_id`/`mal_id`; episodes carry `air_at_estimated`: summary + synopsis, genres, studio, relations, `next_airing`, `list_entry`, `episode_count`, `episodes[]` (id, number, title, air_at, aired, state, watched) |
| `POST /api/anime/{id}/refresh` | admin | enqueue `anilist_refresh` |
| `PUT /api/list/{anime_id}`, `DELETE /api/list/{anime_id}`, `GET /api/list?status=` | any | list states; PUT sets `updated_by=arc`, `mal_dirty=true`; `completed` sets progress to episode count; `score: null` clears |
| `GET /api/schedule?year=&season=` | any | cache-only season grid: 7 days (0 = Monday in the user's timezone), entries with local time, next episode, `following`; movies/OVAs/specials/music and rows with no known air time in `unscheduled`; `prev`/`next` season refs |
| `GET /api/home` | any | `continue_watching` (started > 10 s, not completed, episode ready, newest first, max 20), `behind` (watching shows with aired episodes above progress, newest first), `new_this_week` (episodes of watching/planned shows aired in the last 7 days, max 50) |
| `POST /api/catalog/season-sweep` | admin | enqueue the season pre-cache now (deduped) |
| `GET /api/review?state=&limit=`, `GET /api/review/summary` | any | match-review queue: files below the auto-link threshold with top candidates and reasons; paths relative to `DATA_DIR`, never absolute |
| `POST /api/review/{id}/confirm`, `…/ignore`, `…/reopen`, `GET …/search?q=` | any | resolve a file: link to (anime, episode) creating the episode row if needed; ignore; reopen an ignored one; search the catalogue for another title |
| `GET`/`HEAD /media/{id}/index.m3u8`, `/media/{id}/{init.mp4\|seg_NNNNN.m4s}` | any (session cookie) | HLS delivery from `DATA_DIR/renditions/<id>/`; name validated by regex, path built from the id; 404 unless the episode is `ready`; playlist `no-cache`, init/segments `immutable` + ETag/304; Range → 206/416 (Starlette native) |
| `GET /api/episodes/{id}/play` | any | `PlayInfo`: episode, anime, playlist URL, rendition duration, `resume_position` (10 s < pos < 95 %, not completed), previous/next refs with `ready` |
| `POST /api/progress` | any | upsert watch progress (also accepts `text/plain` beacons; Origin still required); ≥ 90 % → completed (sticky, `completed_at` once); newly completed → list progress raised if higher (`updated_by=arc`, `mal_dirty=true`; a Watching entry is created if none), then `compute_wants` enqueued |
| `POST`/`DELETE /api/episodes/{id}/watched` | any | manual mark / un-mark (un-mark never lowers list progress or MAL) |
| `POST /api/episodes/{id}/transcode?force=` | admin | enqueue a transcode: retry a `failed`/`matched` episode, or re-encode a `ready` one with `force=true` (409 otherwise) |
| `GET /api/retention/preview`, `POST /api/retention/sweep`, `POST /api/episodes/{id}/delete-files` | admin | what the next sweep would delete (reasons, bytes); run it now; delete one episode's files (404 unknown, 409 while in flight) |
| `POST /api/acquisition/pause`, `POST /api/acquisition/resume`, `GET /api/acquisition/status` | admin | pause/resume acquisition (settings key `acquisition_paused`; while paused `compute_wants` does nothing and `search_release` requeues itself without touching Nyaa or qBittorrent; `poll_qbit` keeps ingesting); status shows paused, active wants, searching, downloading |
| `POST /api/episodes/{id}/search`, `POST /api/acquisition/compute-wants`, `POST /api/acquisition/poll`, `GET /api/acquisition/wants` | admin | trigger a release search / the wants reconciler / a qBittorrent poll (all deduped, 202); list active wants for debugging |

## 6. External integrations

| Service | Auth | Rate/limits | Notes |
|---|---|---|---|
| AniList GraphQL `https://graphql.anilist.co` (`ANILIST_URL`, overridable for tests) | none | documented 90 req/min, enforced ~30/min; client paces requests (`ANILIST_MIN_INTERVAL_MS`, default 700), honours `X-RateLimit-Remaining`/`Retry-After`, retries 429 once and 5xx twice | Queries: `SEARCH` (summary fields only; upsert never sets `refreshed_at`), `MEDIA_BY_ID` (full detail + first aired and upcoming schedule pages + relations + studio), then `AIRED_SCHEDULE_PAGE` follow-ups while `hasNextPage` (cap 20 pages / 2000 episodes, logged if hit). A 429 without `Retry-After` waits 3 s (60 s is only the ceiling for a sent header). Detail is served from cache when `refreshed_at` < 24 h; unreachable AniList with nothing cached → 502 `anilist is unavailable`. |
| MAL API v2 `https://api.myanimelist.net/v2` | reads: `X-MAL-CLIENT-ID` header only; writes (M9): OAuth 2.0 PKCE (plain), client id + secret in env | modest | Catalogue fallback (read): `anime?q=`, `anime/{id}?fields=…`, `anime/season/{year}/{season}`; broadcast weekday/time used to synthesise episode air dates. List sync (M9): `users/@me/animelist`, `anime/{id}/my_list_status` (PATCH/DELETE). Tokens encrypted with Fernet key from env. |
| Nyaa RSS `https://nyaa.si/?page=rss&q=…&c=1_2&f=0` (`NYAA_URL`) | none | ≤1 req/2 s (asyncio-paced), 10-min cache per query, 20 s timeout, one retry | `c=1_2` = Anime English-translated. Up to 5 query forms per episode (romaji and english full titles, plus season-stripped base title with `S<k>`, roman numeral, and plain), ALL run and merged by info hash before ranking, because Nyaa ANDs every word and groups name shows differently (`Mushoku Tensei III: Isekai…` vs `Mushoku Tensei S3`). Items parsed with the same filename parser; kept only when kind=episode, episode number equal, title ≥ 0.90 similar (asymmetric: a release title that *extends* the entry's title with tokens not in any of the entry's own titles is a different show, e.g. a subtitled sequel), season agrees (1 assumed when unmarked on either side), not a remake, hash not already used by another episode. Ranked: preferred groups > preferred/fallback resolution > seeders > trusted; per-show overrides in `settings` key `override:anime:<id>`. |
| qBittorrent Web API (`QBIT_URL`, `QBIT_USER`, `QBIT_PASS`, `QBIT_CATEGORY`=arc, `QBIT_DOWNLOADS_PATH`=/data/downloads container-side) | cookie login, re-login on 403 | n/a | `torrents/add` (magnet, category, savepath `<downloads>/<episode id>`; handles 4.x `Ok.` and 5.x JSON/409-duplicate dialects idempotently), `torrents/info?category=arc`, `torrents/delete` (Arc category only), `app/setPreferences` (seeding policy applied at worker start and daily: ratio limit 0 with action Stop, seeding time 0, upload cap `QBIT_UPLOAD_LIMIT_KIB`), `torrents/stop` (any completed torrent still seeding is stopped by `poll_qbit` unless `QBIT_SEEDING`). Dev compose bind-mounts the repo's `data/downloads` so the host worker sees files; first-run temporary password must be replaced with `QBIT_PASS` (see README). |
| Anthropic API | `ANTHROPIC_API_KEY` | n/a | Python SDK `anthropic`; model `claude-opus-5`; structured outputs; streaming. |

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
`MAL_REDIRECT_URI`, `ANTHROPIC_API_KEY`, `LLM_MATCH_SUGGESTIONS` (bool),
`QBIT_URL`, `QBIT_USER`, `QBIT_PASS`, `DATA_DIR`, `MAX_TRANSCODES`,
`BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`, `MATCH_AUTO_THRESHOLD` (0.85),
`MATCH_MIN_CANDIDATE` (0.40), `MATCH_MIN_TITLE_FOR_AUTO` (0.92; above the
0.88 non-exact cap, so in practice auto-link needs an exact normalised title
or a trusted prior), `LIBRARY_SCAN_INTERVAL_SECONDS` (120),
`LIBRARY_SETTLE_SECONDS` (60), `LIBRARY_SCAN_BATCH` (200),
`LIBRARY_SCAN_COMMIT_EVERY` (25), `VIDEO_EXTENSIONS`, `NYAA_URL`,
`QBIT_CATEGORY` (arc), `QBIT_DOWNLOADS_PATH` (/data/downloads), `FFMPEG_BIN`,
`FFPROBE_BIN`, `FFMPEG_VIDEO_ENCODER` (libx264), `FFMPEG_PRESET` (veryfast),
`FFMPEG_CRF` (20), `HLS_SEGMENT_SECONDS` (6), `TRANSCODE_TIMEOUT_SECONDS`
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
the initial migration. `MAX_TRANSCODES` is host capacity and lives only in
env (it is not in the settings table).

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
  missing; un-mark never rolls back; player outside the sidebar; hls.js
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
