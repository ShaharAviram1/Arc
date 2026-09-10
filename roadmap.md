# Arc — Roadmap

> Living document. Tick items as they land; add or reorder as reality
> changes. Last updated: 2026-09-10 (M0–M13 done; production at arc.atomworks.dev).
> Companions: [spec.md](spec.md), [architecture.md](architecture.md),
> [CLAUDE.md](CLAUDE.md).

Legend: `[ ]` not started · `[~]` in progress · `[x]` done · `(FR-…)` spec
requirement it satisfies. Each milestone ends with a **Definition of done**
that must be verified before the next milestone starts.

---

## Phase 1 — Add a show, have it downloaded, watch it

### M0 — Repository skeleton and tooling
- [x] Monorepo layout per architecture.md §3 (`server/`, `client/`, `deploy/`)
- [x] `server/pyproject.toml` with FastAPI, SQLAlchemy 2 async, Alembic,
      pydantic-settings, anitopy, rapidfuzz, httpx, anthropic, argon2-cffi,
      cryptography, APScheduler; ruff + mypy config
- [x] `client/` Vite + React + TS + Tailwind + TanStack Query scaffold
- [x] `deploy/docker-compose.yml` (db, qbittorrent, api, worker, caddy),
      `Caddyfile`, `.env.example`
- [x] Config module (env → settings), logging setup, health endpoint
- [x] pytest + Vitest wired; CI script (`make test`) runs both
- [x] README: local run instructions
- **DoD (verified 2026-09-05 by orchestrator):** `docker compose up db qbittorrent` + `uvicorn` + `vite dev` serve a
  hello page behind auth-less `/api/health`; tests pass green on empty suite.

### M1 — Data model and migrations
- [x] SQLAlchemy models for every table in architecture.md §4
- [x] Alembic initial migration; `settings` seeded with defaults (N=2, G=7,
      D=21, 1080p, en subs, ja audio)
- [x] Job table + claim/run/retry loop in `worker.py` (SKIP LOCKED) with a
      handler registry and a no-op job to prove it
- [x] APScheduler inside worker with one heartbeat job
- **DoD (verified 2026-09-05 by orchestrator):** migration applies on a fresh DB; a job enqueued from the API is
  executed by the worker; unit tests for claim/retry semantics.

### M2 — Auth, users, invites
- [x] Argon2 password hashing, cookie sessions, CSRF/origin check
- [x] Endpoints: login, logout, me, create invite (admin), accept invite
- [x] First-admin bootstrap via env (`BOOTSTRAP_ADMIN_EMAIL/PASSWORD`)
- [x] Client: Login page, Accept-invite page, auth context, route guard
- **DoD (verified 2026-09-05 by orchestrator, in-browser):** admin can invite a user by link; invitee sets a password and logs
  in; all `/api/*` except auth routes return 401 without a session. Tests
  cover token single-use and expiry. (spec §2, §7 security)

### M3 — AniList catalogue and list states
- [x] AniList GraphQL client with retry + rate limiting
- [x] Anime cache upsert (titles, synonyms, relations, airing, cover)
- [x] Search endpoint + Search page (FR-C1)
- [x] List endpoints: set status/score, get my list (FR-C2, FR-W2)
- [x] Show page: cover, synopsis, status/score controls, episode list
      (states rendered but mostly `not_wanted` for now)
- [x] Scheduled catalogue refresh (daily + pre-air) (FR-C5)
- **DoD (verified 2026-09-06 by orchestrator: fixture run in-browser, then live AniList after it returned):** user searches "Frieren", adds it as watching, sees the show page
  with correct episode count and air dates.
- [x] Re-capture AniList fixtures with `scripts/capture_anilist.py` and re-run
      the live DoD once the AniList API is back (done 2026-09-06 evening when
      it returned; live capture revealed the episode 1–4 premiere-block gap).

### M3b — Catalogue fallback and internal ids
- [x] Schema: `anime.id` internal identity; `anilist_id` and `mal_id` nullable
      unique; `detail_source`/`summary_source` markers; `episodes.air_at_estimated`
      flag. Migrations squashed into one fresh initial revision (nothing has
      shipped).
- [x] `CatalogSource` interface with `AniListSource` and `MalSource` (MAL API
      v2, `X-MAL-CLIENT-ID` reads): search, by-anilist-id, by-mal-id, season.
- [x] `CatalogService`: primary AniList, fallback MAL on connection error,
      timeout, 5xx or AniList's "disabled" 403; circuit breaker so an outage
      does not cost a timeout per request; results record their source.
- [x] MAL-sourced episodes with air dates synthesised from `start_date` +
      broadcast weekday/time (JST), flagged estimated; AniList overwrites.
- [x] Reconciliation job: rows with `mal_id` but no `anilist_id` looked up on
      AniList by MAL id when it is healthy; uniqueness on `mal_id` prevents
      duplicates across sources.
- [x] Season pre-cache job (daily) so the schedule works offline (FR-C7).
- [x] Admin `GET /api/catalog/status` (source health, breaker state).
- [x] Client: ids opaque; "catalogue via MAL" notice and "estimated" air-date
      badge; clearer error when both sources are down.
- [x] Owner: MAL API client id (reused from the AnimeTrack app) set in `.env`
      (2026-09-06); verified live against `api.myanimelist.net`.
- **DoD (verified 2026-09-06 by orchestrator in-browser against the real MAL and AniList APIs):** with AniList blocked (pointed at a dead URL), search "frieren" via
  MAL still returns results, adding it creates episodes with estimated dates,
  the show page renders with the MAL notice; re-enabling AniList and running
  reconciliation attaches the AniList id and replaces the dates.

### M4 — Schedule and "behind on"
- [x] AniList seasonal + airing schedule fetch, per-episode `air_at`
- [x] Schedule endpoint (season, weekday grouping, user tz) + Schedule page
      with prev/next season and add-to-list actions (FR-C3)
- [x] Behind-by computation per followed show (FR-C4)
- [x] Home page v1: Behind on + New this week (FR-W1, partial)
- **DoD (verified 2026-09-06 by orchestrator in-browser with a live timezone change; frozen-clock behind tests in the suite):** schedule renders current season by weekday in the user's timezone;
  followed shows highlighted; behind count correct in tests with a frozen
  clock.

### M5 — Filename parser and matcher
- [x] Corpus `tests/fixtures/release_names.txt` (≥ 200 real names)
- [x] Parser wrapper over anitopy with normalisation (FR-L2)
- [x] Matcher: candidate generation (prior, local cache, AniList search),
      scoring, confidence thresholds (FR-L3, FR-L4)
- [x] Ingest job: watch `/data/downloads` and a manual-drop dir, ffprobe,
      create `media_files`, run match (FR-L1)
- [x] Review queue endpoints (list, confirm, set manually, ignore) — API only
      in phase 1 (FR-L6); sidebar shows a pending-review count (the per-show
      badge is deferred to the M13 review UI since unmatched files have no
      show yet)
- **DoD (verified 2026-09-06 by orchestrator: corpus 235 names at 100 %, matcher precision 100 % / recall 90.8 %; live drop of a real MKV + two edge files):** parser + matcher pass the corpus at agreed precision; files
  dropped into the manual dir get matched or land in review; nothing is
  auto-linked below threshold.

### M6 — Acquisition (wants → Nyaa → qBittorrent)
- [x] `compute_wants` (window N, aired-only, merge across users, drop on
      dropped/completed) (FR-A1, FR-A2, FR-W4)
- [x] Nyaa RSS client: query builder, parse, filter by parser, rank by rules
      from `settings` with per-show overrides (FR-A3, FR-A4)
- [x] qBittorrent Web API client: add, info, delete (FR-A5)
- [x] `search_release`, `poll_qbit` jobs; retry/backoff and `unavailable`
      (FR-A6)
- [x] Episode state machine + transitions logged (spec §6)
- [x] Show page: per-episode acquisition status with download % (FR-A7)
- **DoD (verified 2026-09-06 by orchestrator: one real episode found on Nyaa, downloaded via qBittorrent, ingested and auto-linked with the prior; show page shows the release):** adding an airing show as watching results in the next aired
  episode being found on Nyaa, downloaded by qBittorrent, and ingested, with
  no manual step. Tests cover window logic and ranking with fixtures;
  Nyaa/qBit mocked.

### M7 — Transcode pipeline
- [x] ffprobe stream analysis; subtitle/audio track selection (FR-P2)
- [x] Font extraction from MKV attachments into a per-job fonts dir
- [x] ffmpeg HLS fMP4 transcode with burned-in subs, progress parsing,
      concurrency cap (FR-P1, FR-P3)
- [x] `renditions` row, `ready`/`failed` states, retries, priority by user
      proximity (FR-P4)
- [x] Slow test on a 5 s fixture; unit tests on plan building
- **DoD (verified 2026-09-06 by orchestrator: real 1080p episode encoded in 4 min, 237 fMP4 segments, subtitle burn-in confirmed by eye on an extracted frame; failure path and 2-encode cap covered by tests and a live bogus-file run):** a matched MKV becomes a playable HLS rendition with subtitles
  visible; failures surface with stderr tail; two transcodes run in parallel
  at most (configurable).

### M8 — Streaming and player
- [x] Authenticated `/media/{episode}/index.m3u8` + segments, playlist URL
      rewriting, range support (FR-S1)
- [x] Client Player page with hls.js (native on Safari), resume, keyboard
      shortcuts, next-episode prompt (FR-S2, FR-S5, FR-S6)
- [x] Progress reporter (10 s, pause/seek/unload) + `/progress` endpoint
      (FR-S3)
- [x] Completion at 90 % → watch_progress.completed, list progress advance
      (FR-S4); manual "mark watched" (FR-W3)
- [x] Home page complete: Continue watching (FR-W1)
- **DoD (verified 2026-09-07 by orchestrator in Chrome on the real M6/M7 episode: played with burned-in subs, seek reported, left and resumed at 5:14, 91 % completion raised list progress to 11, overlay offered/declined the next episode, continue-watching and behind cleared):** end-to-end: add show → auto download → ready → play in browser →
  close → reopen resumes → finish → next episode is offered and progress
  advanced.

### M9 — MyAnimeList sync
- [x] Owner prerequisites: `MAL_CLIENT_SECRET` in `.env` (from the Cloudflare
      worker secrets of the old AnimeTrack app or the MAL app config page;
      leave empty only if the app is registered as public) and
      `http://localhost:8000/api/mal/callback` registered as a redirect URL on
      the MAL app (production adds `<PUBLIC_URL origin>/api/mal/callback`).
      `FERNET_KEY` set (dev key generated 2026-09-07; production needs its
      own, set once before anyone links).
- [x] MAL OAuth PKCE flow, encrypted token storage, refresh (FR-M1)
- [x] Import on link + scheduled re-import with conflict rule (FR-M2, FR-M3)
- [x] `mal_push` with write log, dirty flags, idempotent retries, never
      lowering progress automatically (FR-M4, FR-M6, FR-M7)
- [x] Revert endpoint (FR-M5)
- [x] Client: MAL link page, sync log with revert, sync-failure badge on show
- [x] Table-driven tests proving "no write without a user event"
- **DoD (verified 2026-09-08 by orchestrator LIVE on the owner's MAL account: OAuth consent → import of 783 entries; manual create, watch-driven progress advance, revert and removal each landed on MAL within seconds and the list ended unchanged; re-link through consent repeated per the owner's rule):** watching an episode to the end updates MAL progress within a
  minute; changing status in MAL shows up in Arc after re-import; log lists
  every write; revert works.

### M10 — Retention
- [x] `retention_sweep` with G, and stale-want drop with D (FR-T1, FR-T2)
- [x] Delete source + rendition + qBit torrent; reset state; re-acquire on
      demand (FR-T3)
- [x] Frozen-clock tests
- **DoD (verified 2026-09-08 by orchestrator: frozen-clock rule matrix in the suite; live sweep on a scratch episode deleted rendition, source, rows and reset state while the real episode stayed byte-identical; re-acquire proven at service level since acquisition is paused in dev):** files disappear on schedule and only then; a re-watch request
  re-acquires.

### M11 — Phase 1 hardening and deploy
- [x] Structured logging, job duration metrics, error surfaces in UI
      (shared `ErrorState` with retry on every page; progress-save banner
      after 3 consecutive failures)
- [x] Rate limiting on login (M2); security review of media routes (M8)
- [~] Responsive pass on Home and Player for phone → moved to M15 (UI
      overhaul) by owner decision
- [x] Hosting decided 2026-09-08: Hetzner CX33 + 250 GB volume, own project,
      seeding off, qBittorrent behind gluetun (see spec §9 / architecture §8)
- [x] Production Compose hardened: healthchecks (incl. worker heartbeat),
      restart policies, log rotation, backup service + restore, client baked
      into the Caddy image, security headers, startup config check, VPN
      profiles, seeding policy
- [x] Deployed 2026-09-09 to `https://arc.atomworks.dev` (Hetzner CPX22 +
      100 GB volume, Mullvad WireGuard via gluetun, Caddy TLS). Verified in
      production by the orchestrator: health with 0 config warnings, VPN exit
      IP ≠ host IP, one full loop (want → Nyaa → download → match →
      transcode → HLS playback in Chrome with progress saved; torrent stopped
      at 100 % with 0 upload), MAL link over the production callback and a
      783-row import with acquisition paused. Invite path checked from the
      CLI; the professor's own invite is issued at the end of development
      (owner decision).
- [x] README: deploy + operations (`deploy/README.md` runbook)
- [x] Seed/demo path: `python -m arc.cli {status,invite,warm-catalogue,demo-list}`
- **DoD:** the site is reachable over HTTPS, the professor can log in via an
  invite, and one full loop works in production.

**Phase 1 exit:** all M0–M11 DoDs verified by the orchestrator (see
CLAUDE.md).

---

## Phase 2 — Recommendations, review, admin, UI overhaul, polish

### M12 — Recommendations
- [x] Candidate pool builder (FR-R2): season by popularity, genre matches by
      score, relations; excludes list entries except planned, recaps and
      specials, and franchise continuations; ≤ 40; bounded catalogue fetches
- [x] Model call behind a provider chain (FR-R7): Gemini free-tier models in
      rotation with a daily-quota cooldown, OpenRouter (`openai/gpt-5-mini`)
      as paid fallback, Anthropic selectable (`claude-opus-5`, adaptive
      thinking, `output_config.format`, streaming, server-side fallbacks,
      refusal handling). JSON-schema output, picks re-validated server-side
      (FR-R3, FR-R4)
- [x] `rec_runs` persistence, rate limit 10/day counted from the table (FR-R5)
- [x] Recommendations page: mood prompt (pre-filled from the last run, one
      button), picks with argued cases, add-to-planned, "New in your
      franchises" continuations (FR-R6), admin-only model-chain line (FR-R1)
- [x] Prompt eval: six fixture histories incl. a 40-candidate pool, offline
      property checks; a `live` marker test against the real provider
- **DoD:** a vague prompt returns 3–5 grounded picks in under 30 s; picks
  never include shows already on the list (except planned).
  Verified by the orchestrator 2026-09-10 on the owner's real list (578
  entries): Gemini 3.5 Flash answered in 2.7–15 s with 3 in-pool,
  history-grounded picks; the chain rotated past a spent model live; the
  OpenRouter leg answered on GPT-5 mini; browser run showed picks,
  continuations, add-to-planned and the admin line.

### M13 — Match review UI and LLM suggestions
- [x] Review page: pending/ignored/auto-linked tabs, parsed chips and
      confidence, ranked candidates, search-other-title, manual episode
      number, ignore/reopen (FR-L6)
- [x] `llm_suggest_match` job on the M12 provider chain (generalised to any
      JSON-schema answer); suggestion stored on the file, shown with model
      and confidence, never auto-applied; on-demand ask; toggle via
      `LLM_MATCH_SUGGESTIONS` (FR-L5)
- **DoD:** an unsure file can be resolved in the UI in under a minute; the
  LLM suggestion is present when enabled and clearly marked as a suggestion.
  Verified by the orchestrator 2026-09-10 in dev: an ambiguously named copy
  of a real episode was ingested, scored 0.84 (< 0.85), queued for review
  with a Gemini suggestion 13 s later, and confirmed from the page in one
  click (linked to the right episode). The suggestion itself picked the
  wrong season with "high" confidence — the reason FR-L5 says shown, never
  applied.

### M14 — Admin panel
- [ ] Users & invites management, deactivate (FR-D1)
- [ ] Rules editor: groups, resolution, N, G, D, languages (FR-D2)
- [ ] Jobs view with retry/cancel; qBittorrent status; disk usage; manual
      delete/re-fetch (FR-D3, FR-T4)
- [ ] Global review queue (FR-D4)
- **DoD:** every admin-configurable value in spec is editable without
  touching env or DB.

### M15 — UI overhaul
- [ ] Design pass over every page once all of them exist (after M14):
      visual language (type scale, spacing, colour tokens, cover/poster
      treatment, badges, empty and loading states), consistent components
      (cards, tables, controls, toasts), and a coherent dark theme with a
      light variant if cheap.
- [ ] Layout: sidebar/nav rework incl. phone navigation and logout (the
      current sidebar is hidden on phone widths), a proper page shell, and
      player chrome that matches.
- [ ] Home, Schedule, Search, Show, Player, MAL, Recommendations, Review,
      Admin each reviewed against the design; screenshots before/after in
      `notes/` or a design canvas.
- [ ] Keep every behaviour and test green; no API changes; component
      changes covered by the existing RTL tests.
- **DoD:** the owner signs off on each page in the browser; no functional
  regressions (full suite green); phone layout usable for Home and Player
  (spec §5).

### M16 — Quality and finish
- [ ] Per-show overrides UI for group/resolution
- [ ] Notifications of failures in-app (banner) — email/push remain out of
      scope
- [ ] Accessibility pass (keyboard nav, contrast)
- [ ] Performance: playlist/segment caching headers, DB indexes reviewed
- [ ] Bump TypeScript to 7.x once typescript-eslint supports it (blocked as
      of 2026-09-05; see architecture.md decision log)
- [ ] Responsive/accessibility items not already closed by M15
- [ ] Full test suite green in CI; coverage report
- [ ] Final docs sweep: spec, architecture, roadmap, README all current
- **DoD:** "finished product" — every FR in spec.md is implemented or
  explicitly marked out of scope; owner has used it daily for two weeks
  without manual intervention.

### Demo prep — the professor's walkthrough (after M16, before the review)
Raised 2026-09-10. The reviewer does not know anime and has no MAL account,
so the product has to explain itself. Scope to be decided with the owner
closer to the date; candidates:
- [ ] A demo account seeded by `arc.cli demo-list` with a plausible list, a
      few ready episodes and a recommendation run, so every page has content
      without a MAL link
- [ ] Plain-language framing on each page: what the page does and which
      external service it talks to (AniList, MAL, Nyaa, qBittorrent, Claude),
      one sentence each — the "multiple API calls" the course asks for,
      visible in the UI
- [ ] A guided tour or a short "How Arc works" page with the pipeline
      diagram (list → want → search → download → transcode → play → sync)
- [ ] A live-status panel (jobs, integrations, VPN exit) the reviewer can open
      to see the system working, reusing the M14 admin data

---

## Dependencies at a glance

```
M0 → M1 → M2 → M3 → M3b → M4
              M3b → M5 → M6 → M7 → M8 → M9 → M10 → M11
                                     M8 → M12
                                     M5 → M13
                                     M11 → M14 → M15 → M16
```

M4 (schedule) can proceed in parallel with M5–M6 once M3 lands.
M9 (MAL) can start after M3 for the OAuth/import half; the push half needs M8.

## Deferred / needs decision

- Hosting provider and budget — required by M11.
- Hardware transcoding — optional optimisation after M7 measurements.
