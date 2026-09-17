# Arc — Roadmap

> Living document. Tick items as they land; add or reorder as reality
> changes. Last updated: 2026-09-18 (M0–M15.5 done; M16 in progress; production at arc.atomworks.dev).
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
- [x] Users & invites management, deactivate/reactivate, role change,
      one-time invite links (FR-D1)
- [x] Rules editor: groups, resolutions, N, G, D, languages, pause — validated
      `GET/PUT /api/settings`, defaults shown, reset per field; per-show
      overrides listed read-only (editor in M16) (FR-D2)
- [x] Jobs view with filters, summary and worker heartbeat, retry/cancel;
      qBittorrent status; disk usage and retained bytes; retention preview,
      sweep now, per-episode delete/re-fetch (FR-D3, FR-T4)
- [x] Global review queue (FR-D4): the M13 Review page is already global;
      Admin shows the pending count and links to it
- **DoD:** every admin-configurable value in spec is editable without
  touching env or DB. Verified by the orchestrator 2026-09-10 in dev: all five
  tabs render live data; a rule saved from the page landed in `settings`
  (logged old→new with the admin id) and was reverted the same way; Retry on
  a failed job put it back through the worker.

### M15 — UI overhaul
- [x] Design brief written 2026-09-10 (`design/m15-brief.md`): owner
      decisions (quiet cinema, dark only, phone bottom tab bar, designer
      proposes accent and mark), tokens, components, every screen and state,
      accessibility, deliverables; before-screenshots in `notes/design/before/`.
      The design pass itself runs in Claude Design; implementation follows
      from its output.
- [x] Design received 2026-09-11 (`design/arc-design/`, Apple TV grammar) and
      implemented the same day: tokens + toolbar/avatar-menu/phone tab bar
      shell + `components/ui` primitives; catalogue key art, credits and
      episode stills (server); Watch Now, Browse (with the Recommendations
      button), new My List, Show (hero, episode rows, franchise rail, credits)
      and the custom Player; Schedule, Recs, Review, MAL, Admin, Login, Invite
      restyled from the earlier prototype's structure. Favicon from the new
      mark. Before/after screenshots in `notes/design/{before,after}/`.
- [x] Owner remark rounds 2026-09-11: Schedule in the toolbar; Home hero =
      season recommendations (banner-first, poster-hero fallback, no upscaled
      posters); Continue watching shelf incl. rewatches (server rule); player
      controls regrouped (⟲10 · play · ⟳10 | prev · next · watched | fullscreen),
      translucent low bar so burned subtitles stay visible, 1.5 s auto-hide,
      double-tap fullscreen; local-first catalogue search; encoder defaults
      fast/CRF 19/tune animation (owner: keep and monitor); logo links home.
- [ ] Owner sign-off remarks on navigation and Watch Now (2026-09-11),
      implemented and awaiting the orchestrator's own validation: Schedule
      joins the toolbar nav and the top of the phone "More" sheet (four tabs
      unchanged); Watch Now's hero becomes a cycling set of season
      recommendations (at most two of the latest run's in-season picks, then
      unfollowed season shows ranked by overlap with the viewer's top-3
      genres and then by popularity — never filtered by genre, so a season
      cached without genres is still offered — cap 6, hidden when empty,
      "Add to list" → planned and "Details"); Continue watching
      becomes the first shelf and "Up Next" narrows to ready-but-unstarted
      episodes as "Ready to watch". No server or API change.
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
- [x] Owner sign-off 2026-09-12 after three remark rounds (all pages checked
      in Chrome by the owner and the orchestrator; full suite green: 2567
      server / 523 client). Encoder stays faithful: no denoise (owner).
- **DoD:** the owner signs off on each page in the browser; no functional
  regressions (full suite green); phone layout usable for Home and Player
  (spec §5).

### M15.5 — Catalogue resilience: offline database + TMDB (approved 2026-09-11)
Why: AniList suspends its third-party API during instability (this week: three
days of 403 "temporarily disabled"), which broke search, drifted fixtures,
degraded art through the MAL fallback and starved the redesign of key art.
- [x] Weekly import job of the manami `anime-offline-database` (≈ 40k shows;
      titles, synonyms, type, season, episodes, picture, cross-ids for
      AniList/MAL/Kitsu/AniDB) and Fribb's `anime-lists` id map (adds TMDB
      series + season, TVDB, IMDb) into two tables; replace-on-import,
      versioned by release tag; `arc.cli import-catalogue` for a manual run
- [x] Search and filename matching consult the offline tables first
      (titles + synonyms, all sources), then live AniList, then MAL; internal
      ids attached via the cross-id map so the same show never gets two rows
- [x] Season lists and "what airs this season" seeded from the offline
      database when both live sources are unavailable (air times still come
      from MAL broadcast slots)
- [x] TMDB enrichment (`TMDB_API_KEY`): nightly job fills missing
      `banner_url` (backdrop), `cover_large_url` (poster), episode
      `still_url`/`title`, and `credits` for followed shows via the id map;
      never overwrites AniList-provided values (cache rule 3); attribution in
      the UI footer as TMDB's terms require
- [x] Config check warning for a missing key; the stale-import flag (> 14 days)
      lives on `GET /api/catalogue/offline` and the admin Storage tab, which
      shows the import version and age (needs the database, so not in the
      env check)
- [x] Re-capture AniList fixtures when the API is back (done 2026-09-12 once
      AniList's burst limit cooled; the fresh staff list exposed the keyword
      credit matcher — "Action Director" read as Director, "Original Story"
      unrecognised — now replaced by whole-role matching in
      `anilist/extras.py`). The offline-db fixture slices have their own
      `scripts/capture_offline.py`
- **DoD:** with AniList and MAL both blocked in a test, search for a known
  title, adding it to a list, the season page and the Show page (with art
  from TMDB) all work; the weekly import runs in production and the Show
  page of a followed show without AniList art shows a TMDB backdrop and
  stills.
- Verified 2026-09-12 (orchestrator): full suite 2765 server / 530 client,
  lint clean. Dev stack run with `ANILIST_URL` and `MAL_API_URL` pointed at a
  dead port: search "jobless reincarnation" answered 200 with cached + offline
  rows ("via offline catalogue" caveat), the offline hit was added to a list
  (`PUT /api/list/1504` → 200), the Schedule rendered, Frieren's Show page
  showed TMDB episode stills and titles (28/28 filled by the enrichment job;
  AniList banner and credits untouched), Home footer carries the TMDB
  attribution, Admin → Storage shows both imports (manami 2026-27, 41,537
  rows; Fribb 32,281 rows) with age and next run. Real import: 37 s first
  run, 2.4 s unchanged. Weekly job scheduled Mondays 03:30 UTC; TMDB sweep
  nightly 04:10 UTC. Production run of the import: pending deploy.
- Owner decisions 2026-09-12: offline-sourced records carry a quiet "via
  offline catalogue" caveat; AniList-sourced records stay unlabelled.
- Owner remarks 2026-09-12, all landed and verified before the deploy: airing
  sanity rule (a finished show has no future episodes; out-of-order dates are
  marked estimated), interactive AniList calls no longer wait out a 429, TMDB
  art for current/next-season shows and the hero (68 Summer 2026 backdrops on
  dev), episode cards never crop a wide banner (still → 16:9 banner → framed
  poster), shows being watched qualify for stills, credit roles matched whole,
  httpx logger at WARNING so the TMDB key never lands in a log.

### M16 — Quality and finish
Scope set with the owner 2026-09-12: the demo account and the "How Arc
works" framing move in from the demo checklist and show **only on the demo
account**; the failure banner shows a user **their own** failures only; the
owner uses the site daily for a few days first so the kinks surface before
the finish work (bugs found that way are fixed inside M16).
- [ ] Failure banner on Watch Now: a user's own failed downloads, transcodes
      and MAL writes, dismissable per failure, linking to the episode or job
      (admins see the same view of their own; the Admin jobs tab stays the
      global view) — email/push remain out of scope
- [ ] Demo account seeded by `arc.cli demo-list` (plausible list, a few ready
      episodes, a recommendation run) so every page has content without a MAL
      link; a "How Arc works" page with the pipeline diagram and one sentence
      per external service (AniList, MAL, TMDB, offline catalogue, Nyaa,
      qBittorrent, the LLM) — both visible only when the demo account is
      signed in
- [ ] Kinks from the owner's daily use (tracked here as they come in)
  - [x] "Try episode 1" sample want (FR-A8) — one explicit exception to
        FR-A1, decided by the owner 2026-09-12. Verified 2026-09-13
        (orchestrator): server 2868 / client 550 green, lint clean; on dev
        the button appeared on an unlisted show, the press wrote one
        `sample` want and no list entry or MAL log row, a rolled-back
        `compute_wants` with the pause cleared moved the episode to
        `wanted` and queued its search, and Cancel dropped the row
        ("sample cancelled") and restored the button. Review fix: a sample
        on a show listed as completed/dropped/on hold is shelved once the
        user has watched it, so it cannot pin the file forever. Owner remarks
        2026-09-13, landed and re-verified in the browser: the control is one
        toggle pill ("Try episode N" → "✓ Episode N requested", hover "Cancel
        request", `aria-pressed`) and the routes move the episode themselves
        through the reconciler's shared `start_search`/`release_if_unwanted`
        helpers, so the row reads "Wanted" the moment it is pressed (also
        while acquisition is paused) and "Not fetched" again on cancel.
  - [x] Sample request queues the show's TMDB enrichment so stills arrive with
        the episode. Verified 2026-09-13 (orchestrator): on dev, pressing
        "Try episode 1" on The Villager of Level 999 queued one full
        `tmdb_enrich:148` job and all 12 stills were filled within seconds,
        without opening Watch Now; a show with complete art queues nothing
        (tests)
  - [x] Production acquisition resumed 2026-09-13 after the MAL-import
        pause (Admin → Acquisition), so wanted shows download again
  - [x] Nyaa query builder asks by the title's head before a subtitle (FR-A4),
        unless the entry has a PREQUEL relation (a bare-head release is most
        likely the first season and an unmarked sequel cannot be told apart by
        season agreement). Verified 2026-09-13 (orchestrator): 443 acquisition,
        parser and matcher tests green incl. the corpus at 100 %; live on dev
        the Rakudai Kenja no Gakuin Musou sample went from 0 results on three
        full-title queries to 9 results on the head query, 7 kept, SubsPlease
        1080p chosen and downloading
  - [x] Batch releases (episode ranges incl. `~`/`〜`/`E01-E12`, and `BATCH`/
        `Season Pack` markers; `Complete` only without a single episode
        number) are classified as batches and never picked (FR-A4). Found
        2026-09-12 when production pulled `Dagashi Kashi 2 - 01 ~ 12` as
        episode 1 and transcoded all twelve. Verified 2026-09-13
        (orchestrator): corpus 247/247 on episode+kind and title key, 561
        parser/filter/matcher/ingest tests green, Reviewer's should-fixes
        (wave dash, year pairs, `01 - 100 Poems`) landed; the seven
        production names all parse as batch and the files inside a batch
        directory still parse as episodes
  - [x] Home/Show hero showed the blurred-poster fallback for shows that have
        a proper TMDB backdrop (owner, 2026-09-13): the banner's aspect probe
        rendered a `loading="lazy"` image inside a 0×0 box, which Chrome never
        fetches, so the aspect stayed unknown and the wash stayed for good
        unless another card had cached the banner. `AspectProbe` now loads
        eagerly; regression assertion in `ui.test.tsx`. Verified on dev
        (orchestrator): the probe reports 1280×720 and the hero fills with the
        backdrop; 550 client tests green
  - [x] `backdrop_url` (TMDB only) preferred by heroes and cards over the
        AniList banner strip; AniList refreshes cannot clobber it; a missing
        backdrop counts as a hole for the sweep and the on-demand paths, and
        none of those queue anything without a `TMDB_API_KEY`. Verified
        2026-09-13 (orchestrator): migration `0aa6beaab5ed` applied on dev;
        after a worker restart the sweep filled 59 of 69 Summer 2026 shows
        and all six Watch Now hero slides render a backdrop (Tanya S2,
        Villager of Level 999 and Magilumiere S2 were washed before); 341
        server tests on the touched modules and 556 client tests green
  - [x] Stalled torrents are removed and the episode retried on the FR-A6
        schedule; a release is never chosen twice; 0-seeder releases are
        rejected — verified
  - [x] Downloads nobody wants any more are cancelled; qBittorrent queue
        policy applied by Arc — verified
        (both verified 2026-09-13 by the orchestrator: Reviewer's three
        blockers fixed — stall clock is qBittorrent's `time_active`, dead
        swarm judged on tracker scrape figures only, a decided torrent row
        never drags a retried episode back — 972 acquisition/retention/
        jobs tests green, lint clean; live proof waits for the deploy)
  - [x] Dormant imports (FR-A9): an imported entry fetches nothing until
        touched in Arc (status, any progress report, revert), airing shows
        excepted; Try samples a dormant entry without activating it; Show
        page note + "Fetch this show"; Admin shows the dormant count.
        Verified 2026-09-13 (orchestrator): migration `3b7c41e9d2af` on dev
        leaves 222 of the admin's 784 imported entries dormant (the
        production shape), Admin → Acquisition shows "Dormant imports 222",
        a dormant show's page shows the note and the button; 680 server /
        569 client tests green after the Reviewer's four should-fixes
  - [x] Storage guard (FR-T6): `min_free_gb` (default 10) in the Rules
        editor as "Free space floor"; below it acquisition reconciles but
        starts no search, searches requeue, ingest/transcode/playback go on;
        Admin pill reads "held". Verified 2026-09-13 (orchestrator): setting
        seeded and visible on dev, rule matrix and hold tests in the same
        green run; the held state cannot be shown live on a disk with 150 GB
        free, so it rests on the tests
  - [x] Per-user slot cap K (FR-A10): `slot_cap_k` (default 5, 0 = unlimited)
        in the Rules editor; occupants keep their slot, free slots go airing
        first then most recently updated, a waiting show's rows are left
        alone except a want the user has watched past; `unavailable`/`failed`
        hold no slot; the Show page says why a show waits (slot, paused,
        held); Admin shows "N waiting · cap K". Verified 2026-09-13
        (orchestrator): Reviewer's two blockers fixed (a stale-dropped want
        of a waiting show is no longer deleted; unfindable shows cannot
        freeze a list); final tree green — server 3142, client 576, lint
        clean; seed migration `7c1d5b3ae4f2` applied on dev
  - [x] Shelves scroll for mouse users: drag-to-scroll (mouse only, 6 px
        threshold, click suppressed after a drag, snap restored on release)
        and hover edge arrows (glass chevrons, hidden at the ends and on
        touch, one viewport per press). Verified 2026-09-13 (orchestrator)
        in Chrome on dev: the arrow moved the Catch up shelf one viewport
        per click; the first real drag did nothing because the tiles are
        links wrapping images and the native HTML5 drag cancelled the
        pointer gesture — fixed with one `dragstart` handler on the strip;
        a real drag then moved it 588 px, snapped, without following the
        link; 583 client tests, lint clean
  - [x] Deployed 2026-09-13 as "M16 package 1" (commit `a02e4da`): five
        migrations ran on the host (head `7c1d5b3ae4f2`); production then
        reset "everything except accounts" at the owner's request —
        torrents, files, wants, lists, progress, MAL link, write log,
        recommendations and jobs wiped; users, invites, sessions, settings
        and the catalogue kept. Verified by the orchestrator: health 200
        with 0 config warnings, qBittorrent holding no torrents with the
        queue policy applied (8/12, slow torrents not counted), Admin →
        Acquisition all zeros, 93 GB free. The owner re-links MAL and
        re-imports; imported entries start dormant (FR-A9)
  - [x] Show page crashed ("null is not an object (evaluating 'e.state')")
        after a status change on production (owner, 2026-09-13, Safari):
        the list PUT answers `mal_sync: null` (only the show page computes
        it) and the cache patch handed that null to the MAL indicator, which
        guarded `undefined` only. The patch now keeps the page's own fields
        when the PUT lacks them and the indicator tolerates null; two
        regression tests. Verified 2026-09-13 (orchestrator): 119 tests in
        the two touched files, lint clean
  - [x] Hero shows a known backdrop immediately (TMDB backdrops are 16:9,
        no probe), pre-measures every slide's shape once into a shared cache
        and preloads the next slide, so no wash flash on rotation; the Show
        hero and the 16:9 cards use the same rule. Owner saw translucent
        posters flashing on production in Safari. Verified 2026-09-13
        (orchestrator) in Chrome on dev: all six Watch Now slides paint their
        backdrop on the first frame with zero probe elements and the image
        already complete; 598 client tests, lint clean. Shipped as the
        M16 package 1 hotfix (`53b154e`) with the show-page crash fix
  - **Batch 2 (owner, 2026-09-13, after the package-1 deploy) — to triage
    together before building:**
  - [x] Episode stills missing on some Show pages. First reading: stills
        come only from the TMDB enrichment, which reaches a show when it is
        "watched" (list/progress/ready episode) or when Watch Now's shelves
        or a sample press queue it; a Show page opened for an untouched or
        unmapped show never asks. `GET /api/anime/{id}` now queues
        `enqueue_show_enrichment` behind the same three gates (key, id map,
        a hole left to fill) in the commit it already makes, and answers
        `tmdb_mapped`; the client polls the detail every 5 s for 30 s while a
        mapped show has an aired still-less episode, never polls an unmapped
        one, and the episode list carries one muted "No episode pictures for
        this show" where `tmdb_mapped` is false or `/api/health` reports no
        key. Verified 2026-09-13 (orchestrator): 232 catalogue/TMDB/home/sample
        tests and 620 client tests green, lint clean; on dev, opening
        Victoria of Many Faces (mapped, 0 stills) queued one `tmdb_enrich`,
        all 9 stills landed, and the page picked them up through its short
        poll within 15 s with no reload
  - [x] Nyaa queries also ask in the `SxxEyy` form (One-Room TA on production
        found nothing under "- 01") — `queries()` gains
        `<season-stripped base> S<kk>E<nn>` for both titles, third and fourth
        in the order, and `MAX_QUERIES` rises 6 → 8; the parser already read
        the ToonsHub singles as episodes of season 1 and both Nyaa batches as
        batches, and the four production names joined the corpus (251 names,
        100 % on episode+kind and title_key). 470 tests on the four touched
        modules green, lint clean. Verified 2026-09-14 (orchestrator): One-Room TA now asks
        `One-Room TA S01E01` (the ToonsHub naming Nyaa actually carries);
        parser reads the ToonsHub singles and rejects both batches; 581
        query/parser/corpus/matcher tests green, corpus 251/251
  - [x] Per-episode watched state on the Show page (FR-W5): `EpisodeOut`
        derives `watched` from the list progress as well as Arc's own
        completion rows, and carries `watched_source`, which doubles as "is
        this a button?" — `arc` at or above the list's progress offers
        "Unwatch" (one control, reading "✓ Watched" until hover), and
        `progress` below it is a non-actionable "Watched" with the tooltip
        "Unwatch from the latest watched episode down". The Player toggle
        follows the same field. Verified 2026-09-13 (orchestrator): 556 server tests on the
        touched modules and 605 client tests green, lint clean; on dev,
        Hell's Paradise (imported, progress 7) shows episodes 1–6 as
        non-actionable "✓ Watched" pills with the tooltip, episode 7 as the
        actionable pill with "Unwatch" on hover, 8+ as "Mark watched", meta
        line "7 watched"; the Reviewer's retention-anchor clamp landed
  - [x] Marking episode N watched implies 1…N-1 watched, and Arc shows it
        that way (FR-W5): the manual mark goes down FR-S4's own path, raising
        list progress to N with one MAL progress write that never lowers, and
        writes **no** synthetic completion rows for 1…N-1 — retention instead
        anchors those episodes on the list entry's `updated_at`, clamped to the
        age of the bytes so a re-fetch is not swept within the hour, so their
        files go on the same G-day schedule. Its undo (owner, 2026-09-13,
        superseding the 2026-09-07 clarification): an explicit un-mark of the
        latest watched episode lowers progress to N−1 with one logged `manual`
        progress write — the only lowering write Arc sends, with the FR-M4
        guard still refusing every automatic one — while the auto-completed
        status is never rolled back. Verified 2026-09-13 (orchestrator): 556 server tests on the
        touched modules and 605 client tests green, lint clean; on dev,
        Hell's Paradise (imported, progress 7) shows episodes 1–6 as
        non-actionable "✓ Watched" pills with the tooltip, episode 7 as the
        actionable pill with "Unwatch" on hover, 8+ as "Mark watched", meta
        line "7 watched"; the Reviewer's retention-anchor clamp landed
  - [x] A show whose progress reaches its episode count (12/12) becomes
        `completed` automatically, in Arc and on MAL (FR-W5): one logged
        status write with cause `watch`, in the same transaction and the same
        PATCH as the progress. Only on an advance, only on a FINISHED show
        with a known count, and from **any** status — `on_hold` and `dropped`
        complete too (owner, 2026-09-13); airing shows, unknown counts,
        already-completed entries and rewatches change nothing. Verified 2026-09-13 (orchestrator): 556 server tests on the
        touched modules and 605 client tests green, lint clean; on dev,
        Hell's Paradise (imported, progress 7) shows episodes 1–6 as
        non-actionable "✓ Watched" pills with the tooltip, episode 7 as the
        actionable pill with "Unwatch" on hover, 8+ as "Mark watched", meta
        line "7 watched"; the Reviewer's retention-anchor clamp landed
  - [x] Clicking a show on the Catch up shelf did nothing (regression from
        the shelf drag: pointer capture on the press retargeted the click to
        the strip). Capture is now taken only once the 6 px drag threshold
        is crossed; two tests pin it. Verified 2026-09-13 (orchestrator) in
        Chrome on dev: a real click on a Catch up tile opened its series
        page; 599 client tests, lint clean. Shipped as hotfix 2
  - [x] Some airing shows are missing from the Schedule: Slime Season 4
        airs every Friday (next: episode 23, 2026-09-18 14:00 UTC) but is
        tagged `SPRING 2026` — a two-cour show that started in spring — and
        the schedule grid is built from the shows of the selected season
        only. The current week's grid now includes every `RELEASING`
        weekly-format show with an air time inside 7 days, whatever season it
        is tagged with (`airing_this_week`, merged with the season's own
        rows); the season tag is untouched and the card says "Since Spring
        2026". Prev/next views are unchanged — a catalogue browse, not a
        calendar. Home's "Catch up"/"New this week" needed no change (both
        are list-driven) and a test pins that; the daily and pre-air refresh
        sweeps were already season-blind, also now pinned. Verified 2026-09-13 (orchestrator): 205 schedule/catalogue/home
        tests and 608 client tests green, lint clean; on dev the Summer
        2026 grid now carries 27 airing shows tagged earlier seasons
        (Re:ZERO S4, Pokémon Horizons, LIAR GAME, …), each with a quiet
        "Since Spring 2026" line; prev/next season views unchanged
  - [x] The "New this week" shelf no longer labels tiles with an acquisition
        state (FR-W1, owner 2026-09-13): the card is the broadcast day, the
        time and the episode, plus a "✓ Watched" line when FR-W5 says the
        viewer has seen it — which on an imported list is most of them. The
        Show page keeps FR-A7's per-episode state in full. Verified 2026-09-13 (orchestrator): 556 server tests on the
        touched modules and 605 client tests green, lint clean; on dev,
        Hell's Paradise (imported, progress 7) shows episodes 1–6 as
        non-actionable "✓ Watched" pills with the tooltip, episode 7 as the
        actionable pill with "Unwatch" on hover, 8+ as "Mark watched", meta
        line "7 watched"; the Reviewer's retention-anchor clamp landed
  - [x] A deploy (worker restart) mid-transcode orphaned the job (owner hit
        it 2026-09-13 after hotfix 2; job 927 requeued by hand). Cause: the
        worker's own 30 s drain never ran because the compose service had no
        `stop_grace_period` and Docker killed it at 10 s. Fixed two ways: the
        worker requeues every `running` job locked by another identity at
        start-up (one worker per deployment, so any such job is orphaned),
        and the drain (`WORKER_DRAIN_TIMEOUT`, now 10 s) is paired with a
        15 s `stop_grace_period`, with a test that reads the compose file so
        they cannot drift. Verified 2026-09-13 (orchestrator): 206 job,
        worker, transcode and config tests green, lint clean
  - [x] The site should update itself when an episode becomes ready (and as
        acquisition states move) — no manual refresh to see a new tile on
        "Ready to watch" or a row flip to Ready on the Show page. First
        reading: today Watch Now and the Show page refetch on focus and on a
        fixed interval only. Built (architecture.md §5.9): Postgres
        `LISTEN`/`NOTIFY` on `arc_events`, published from inside the
        committing transaction by `transition()` and by the TMDB enrichment's
        apply step, fanned out by `GET /api/events` (server-sent events, one
        asyncpg `LISTEN` connection per api process, 100 streams each, 25 s
        heartbeat) and read by one `EventSource` per tab in `lib/events.ts`,
        mounted in `Layout.tsx`; events carry ids only and the client
        invalidates Watch Now, the show detail and (for admins) the
        acquisition status, coalesced over 300 ms, with a hidden tab dropping
        the stream after 60 s and catching up on return. Every existing
        polling interval is untouched as the fallback. Caddy proxies the
        stream with `flush_interval -1` and excludes it from `encode`. No push
        notifications, no sound: the page just stays true. Verified 2026-09-14 (orchestrator): 368 events/auth/acquisition/
        catalogue tests and 638 client tests green, lint clean; on dev the
        app opens one stream per tab, Postgres shows a single LISTEN backend
        and no idle transaction behind an open stream (the Reviewer's
        blocker), and a sample request/cancel arrived as `wanted` then
        `not_wanted` events within a second
  - [x] Matching robustness (owner: "failing on this matching is kinda
        embarrassing", 2026-09-13; dealer's choice on scope): movies, OVAs
        and specials are searched by bare title and accepted without an
        episode number; a symbol-stripped query variant (☆ ♪ ! ? : ~);
        the slash-title parser bug (`Fate/Zero` read as "Zero"); dub
        releases parsed and ranked below subs, never chosen over a sub;
        one query per stored synonym (capped); a **query corpus** fixture
        (real entries → real Nyaa names that must be found, run offline);
        and the episode row shows the last search ("searched 6 forms, 0
        results, next try 23:26") instead of a bare "Searching". Landed
        2026-09-14: all seven pieces — films/OVAs asked for by bare title and
        accepted only when the release says it is one (`movie`/`special` kind,
        a strict title comparison that forgives only the type word, year within
        one where both sides have one), a symbol-stripped variant of the two
        full titles at the *end* of the list (`MAX_QUERIES` 8 → 10, the slash
        added to the symbol set, and the whole list reordered so the cap cuts
        speculative forms rather than a marked entry's short forms), the
        slash-title parser fix (an explicit `parse(..., path=)` flag: ingest
        and the match job pass `path=True`, a Nyaa title is never split),
        `ParsedName.dubbed` ranking ahead of all four FR-A3 rules with a log
        line when a dub is taken for lack of anything else, up to two synonym
        queries counted last against the cap and floored at "must be a name",
        `tests/fixtures/query_corpus.txt` + `test_query_corpus.py` (17 real
        cases, offline: forms required, forms forbidden, releases accepted and
        rejected, ranking preferred), and `episodes.last_search_{at,forms,
        results}` (migration `4f2ab7c91d68`) behind `EpisodeOut.search` and the
        row's "Searching · 6 forms, 0 results · next try 23:26". Reviewer's two
        blockers (series packs accepted as films; the basename heuristic) and
        four should-fixes applied the same day. Verified 2026-09-14 (orchestrator): Reviewer's two blockers fixed
        (BD series packs no longer accepted as films — single mode requires
        a movie/special marker and scores strict subsets strictly; slash
        titles split only on real filesystem paths via an explicit flag);
        parser corpus 259/259, query corpus 17 cases; 828 matching tests,
        full client 644 and lint green; migration `4f2ab7c91d68` on dev
  - [x] Absolute episode numbering on sequels (SubsPlease `Jujutsu Kaisen -
        25` for S2 E1): offset from prequel episode counts via relations,
        accepted only when season agreement and the offset both hold — own
        item with a Reviewer, after this batch
       . Verified 2026-09-17 (orchestrator): Reviewer found no blocker; its
        should-fixes landed (only FINISHED prequels count, bare-base
        sequels spend no slots, a malformed relation never fails a search);
        562 matching/corpus/acquisition tests green, query corpus 18 cases,
        parser corpus 259/259, ruff/format/mypy clean

  - [x] Deployed 2026-09-14 as "M16 batch 2" (commit `621dd27`): migration
        `4f2ab7c91d68` ran on the host, `WORKER_DRAIN_TIMEOUT` set to 10 in
        the host env to match the 15 s stop grace, health 200 with 0
        warnings, no job left `running` after the restart, `/api/events`
        answers 401 without a session. Live proof of the matching fix: the
        owner's stuck One-Room TA episodes 1–2 were re-searched right after
        the deploy — 6 forms, ToonsHub `S01E01`/`S01E02` found and chosen,
        both downloading
  - **Batch 3 (owner, 2026-09-17, after the batch-2 deploy):**
  - [x] Player end-of-episode flow: at the 90 % completion mark show a small
        toast "Marked as watched" instead of the next-episode overlay; with
        1:30 or less remaining show the overlay offering "Next episode",
        "Keep watching" and "Back to the show" (owner's wording). Built
        client-only (spec FR-S5 rewritten, architecture §5.4a): the toast is
        raised by the server's `newly_completed` so a rewatch is silent, the
        card is derived from `ended || remaining <= 90 s` with "Keep watching"
        and Escape holding for the rest of the playback, "Next episode" is
        drawn only when the next one is ready, and both render inside the
        fullscreen element. The ⟲10 / ⟳10 glyphs were redrawn in the same
        pass (owner: the arc was on the wrong side) — one rewind, mirrored.
        61 Player tests green. Verified 2026-09-17 (orchestrator) in Chrome on dev: seeking to
        60 s before the end brought up the card ("Episode 2 isn't ready
        yet", Keep watching, Back to the show) with the video still
        playing; 61 Player tests
  - [x] Player: the ±10 s seek controls draw their semicircle on the wrong
        side; the glyphs should read as rewind (arc opening left) and
        forward (mirror) — owner, 2026-09-17.
        Verified 2026-09-17 (orchestrator): the redrawn glyphs read as
        ⟲10 and ⟳10 with the arc opening on the correct side; a test pins
        the forward glyph as the mirror of the back one.

  - [x] Shows the TMDB id map cannot reach (One-Room TA: AniList 205068 has
        no TMDB entry) get no stills or backdrop; their episode rows and
        cards should fall back to the show's poster (or backdrop when there
        is one) instead of the striped placeholder, and the "No episode
        pictures" line goes.
        Built 2026-09-17, client-only: `EpisodeArt` in `pages/Show.tsx` takes
        still → `backdrop_url` → key visual letterboxed in the 16:9 slot
        (`PosterWash` with `ground={null}`, the wash dropped because a show
        page is fifty rows where a shelf is eight); `backdropArt` added to
        `lib/anime.ts`; Home's tiles already had the chain and keep their
        wash; the `NO_STILLS` caption is gone and `tmdb_mapped` stays on the
        API for `awaitingStills`' polling rule. Verified 2026-09-17 (orchestrator) on dev: the unmapped
        Fate/stay night Heaven's Feel III row shows its poster letterboxed
        in the 16:9 slot instead of the stripes; the "no pictures" line is
        gone
  - [x] "Ready to watch" is derived from the New-this-week rows (aired in
        the last 7 days), so a ready episode of an older show (One-Room TA,
        aired 2026-08-27) never appears. Fix: a server-side "ready and not
        started by the viewer" query for the shelf, any air date, newest
        ready first.
        Built 2026-09-17: `progress.ready_to_watch` (ready state, list in
        watching/planned/on_hold, no `watch_progress` past 10 s, no
        completion, number above the list's progress; `renditions.ready_at`
        desc nulls last, cap 20) behind a new `ready_to_watch` array on
        `GET /api/home`; the client renders it and `readyToWatch`'s filter
        over `new_this_week` is gone. Verified 2026-09-17 (orchestrator): server-side `ready_to_watch`
        shelf with 13 new API tests (old ready episode listed, started or
        watched-by-progress or dropped ones not); live proof waits for the
        deploy (One-Room TA on production)
  - [x] The This-week panel shows "✓ Watched" on upcoming appointments
        (Slime episode 23, Mushoku episode 13): the flag is derived from the
        show's latest aired row and drawn beside the next episode's number.
        A future episode never carries a tick; the tick applies only to the
        aired episode the tile actually names.
        Built 2026-09-17: `ScheduleEntry.watched` (`bool | null`) on
        `GET /api/schedule`, answered by `api/schedule.watched_marks` — FR-W5
        for `next_episode`, and only where `next_at` is already past, so an
        upcoming slot sends null and two queries run only when a slot has
        aired; Home's `appointments()` reads it and `watchedThisWeek`'s
        lookup by show is gone. Verified 2026-09-17 (orchestrator) on dev: the eight upcoming
        appointments carry no tick; the API sends `watched` only for an
        aired named episode; 290 home/schedule/catalogue/playback tests
  - [x] Absolute episode numbering on sequels — approved by the owner; the
        item above moves into this batch with its own Reviewer.
        Built 2026-09-17 (spec FR-A4, architecture §5.1a/§6/§10):
        `nyaa.absolute_offset` sums the `PREQUEL` chain's episode counts
        through cached `anime` rows and **declines** the entry entirely on an
        uncached prequel, a null count, a prequel that has not finished airing
        (an announced total is the number likeliest to be wrong), two countable
        prequels at one hop, a loop, >10 hops or a format it cannot classify;
        films/OVAs in the chain are skipped (so *Jujutsu Kaisen 0* costs season
        two nothing). `queries()` then asks `Jujutsu Kaisen - 25` and `Jujutsu
        Kaisen 25` behind the romaji short forms — and only where the season
        marker left a shorter base behind, so an unmarked sequel spends no
        slots on a form nobody writes — `acceptable()` takes `N + offset` only
        from a release naming no season, and `filter_items()` drops every
        absolute candidate when a season-marked one for the same episode came
        back too. `jobs._prequel_offset` is the DB half and can never fail a
        search (a malformed relation blob declines and logs at WARNING);
        `search_release` logs at INFO which offset it used; `rank` explains the
        pick with "absolute numbering: release 25 = episode 1". Reviewer's
        three should-fixes and two nits applied the same day. 177 nyaa tests,
        query corpus 18 cases, 121 acquisition-job tests, ruff/mypy clean.
        Verified 2026-09-17 (orchestrator, shipped in 8cd03e4): full server
        suite and query corpus green, Reviewer fixes read; the marker below
        was left unticked by mistake and corrected 2026-09-18
  - [x] Schedule redesign (owner, 2026-09-17, replacing the dimming idea):
        show three days at a time instead of seven, starting with today;
        arrows on the day-and-date bar move through the week (the season's
        prev/next stays for browsing other seasons); rows are roomier with
        full show names and legible air times; today is marked; shows the
        viewer follows carry a small highlight. Long-runners and carried-in
        shows stay as they are — the room is what fixes the clutter.
        Built 2026-09-17, client-only (`pages/Schedule.tsx`, `weekDates` in
        `lib/schedule.ts`): today plus the next two days, a day-and-date bar
        ("Wed 17 Sep") with `GLASS_CIRCLE` chevrons and arrow-key support that
        stop at the week's ends, an accent underline and a "Today" chip on
        today, un-clamped titles with 15px times and 56px thumbs, and an ember
        left rule plus sr-only "On your list" on a followed row. A browsed
        prev/next season shows weekday names alone — no dates, no today, and
        the window opens on Monday — because only the live season's grid is a
        real week. Verified 2026-09-17 (orchestrator) on dev: three days from today,
        "Today" chip and ember rule, arrows at both ends, full titles and
        15 px times, five followed shows highlighted with sr-only "On your
        list"; a browsed season shows weekday names only; 61 Schedule tests
  - [x] Deployed 2026-09-17 as "M16 batch 3" (commit `8cd03e4`): no
        migration; health 200 with 0 warnings; the worker restarted cleanly
        and its first transcode is its own claim (no orphan). Live proof on
        production: "Ready to watch" now lists One-Room TA episodes 3 and 4
        (aired 2026-08-27, impossible under the old 7-day rule) beside the
        current-season episodes, and the This-week panel shows no tick on
        any upcoming appointment
  - **Batch 4 (owner, 2026-09-17, after the batch-3 deploy):**
  - [x] Hero art for long-runners (One Piece on production: AniList strip
        only, `backdrop_url` null, mapped to TMDB 37854, and NO enrichment
        job ever ran). Cause: the nightly art pass and Watch Now's on-demand
        hero enrichment are scoped to shows tagged this/next season, while
        the hero pool and the Schedule now carry in every airing show
        whatever season it started. Fix: the art passes target the same set
        the hero can pick — current/next season OR `RELEASING` with a
        recent air time — so a carried-in show is enriched the first time
        it is offered; backfill runs on the next sweep.
        Built 2026-09-17: one predicate, `tmdb/jobs.py::hero_pool_members`
        = `(current OR next season) OR on_air_this_week`, with the second
        clause extracted out of `catalog/schedule.py` and composed rather
        than restated (so the schedule's grid and the art passes cannot
        drift). Both art paths use it — the sweep's pass 2
        (`_needs_hero_pool_art`, art-only, popularity desc) and
        `enqueue_hero_art` (cap 12) — behind the unchanged `_mapped`,
        `_missing_key_art`/`_missing_hero_art` and `TMDB_API_KEY` gates;
        `sweep_candidates` now returns `SweepCandidate(anime_id, art_only,
        carried_in)` and the sweep logs `carried_in`. No migration, no
        backfill script: both queries are popularity-ordered, so One Piece
        is first on the next sweep and the next Watch Now load. 11 new
        tmdb-jobs tests and 2 new home tests; 219 pass across
        test_tmdb_jobs / test_home_api / test_schedule_api /
        test_catalog_jobs, ruff + mypy clean. Verified 2026-09-18 (orchestrator): 219 TMDB/home/schedule/catalogue
        tests green; on dev, one Watch Now load queued art-only jobs for the
        carried-in long-runners and Star Detective Precure, The Drops of God
        and BEYBLADE X had backdrops within seconds; One Piece on production
        gets its backdrop on the first Home load after the deploy
  - [x] Continue watching must not offer an episode that is effectively
        finished: past the completion mark or with less than 3 minutes
        left it leaves the shelf (and the next episode takes its place via
        Ready to watch).
        Built 2026-09-17: `continue_watching`'s end bound is now
        `position < duration × COMPLETION_FRACTION` **and**
        `position <= duration − CONTINUE_TAIL_S` (180 s, documented beside
        the resume ceiling), replacing `least(95 %, duration − 60 s)`;
        `CONTINUE_END_MARGIN_S` deleted. The hand-over needed no code — a
        completion row and the FR-S4 advance are Ready to watch's own two
        exclusions — and is asserted end to end: 89 % with 4:24 left listed,
        91 % not, 85 % with 2:30 left not, exactly 3:00 left listed, and a
        92 %-watched episode on neither shelf with its successor on Ready to
        watch. 161 playback + home API tests. Verified 2026-09-18 (orchestrator): 225 playback/home/TMDB tests green; on dev, a row seeded at 92 % left the shelf and one with exactly 3:00 left stayed, checked through GET /api/home
  - [x] The player starts playback when the page loads instead of waiting
        for a click (subject to the browser's autoplay policy: muted-start
        fallback or a one-click prompt only when the browser refuses).
        Built 2026-09-17, client-only (`Player.tsx` `beginPlayback`): the
        attempt is made after the resume seek, once per page behind a ref —
        audible, then one muted retry that raises a quiet "Tap to unmute"
        pill (retired by the pill or by `m`, through `onVolumeChange`), then
        nothing, leaving the existing play button and no error. `?paused=1`
        deliberately not added: no route opens the player without wanting it
        to play. Four autoplay tests (resolves / rejects-then-muted /
        rejects twice / unmuted from the keyboard). Verified 2026-09-18 (orchestrator): 679 client tests green; on dev, opening an episode from the Continue watching card resumed at 4:43 and played unmuted with no click, progress POSTs held back during the check
  - [x] The player's mark-watched control shows ✕ once the episode is
        watched, with hover/label "Mark unwatched"; ✓ with "Mark watched"
        before.
        Built 2026-09-17: one `WatchedGlyph` with a `cross` variant, so the
        glyph is the action and the fill/outline pair stays the Show page's
        pressed-pill treatment; the `watched_source == 'progress'` pill keeps
        the non-actionable filled ✓ and its tooltip. 65 Player tests. Verified 2026-09-18 (orchestrator): on dev an unwatched episode shows ✓ "Mark watched", an Arc-completed one ✕ "Mark unwatched" (label and hover), and one covered by list progress keeps the non-actionable ✓ with the FR-W5 tooltip
- [ ] Per-show overrides UI for group/resolution
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
- [x] Moved into M16 (2026-09-12): the demo account and the "How Arc works"
      page with the pipeline diagram and one sentence per external service —
      demo-account only
- [ ] A live-status panel (jobs, integrations, VPN exit) the reviewer can open
      to see the system working, reusing the M14 admin data

---

## Dependencies at a glance

```
M0 → M1 → M2 → M3 → M3b → M4
              M3b → M5 → M6 → M7 → M8 → M9 → M10 → M11 → … → M15 → M15.5 → M16
                                     M8 → M12
                                     M5 → M13
                                     M11 → M14 → M15 → M16
```

M4 (schedule) can proceed in parallel with M5–M6 once M3 lands.
M9 (MAL) can start after M3 for the OAuth/import half; the push half needs M8.

## Deferred / needs decision

- Hosting provider and budget — required by M11.
- Hardware transcoding — optional optimisation after M7 measurements.
