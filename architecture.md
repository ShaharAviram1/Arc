# Arc — Architecture

> Living document. Update whenever the stack, a component boundary, or an
> integration changes. Last updated: 2026-09-17.
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
| Live updates | Postgres `LISTEN`/`NOTIFY` → server-sent events (`GET /api/events`) → `EventSource` | The write is in the worker and the tab is on the api, so the event has to cross a process boundary; the database is the one thing both already hold a connection to. No broker, no dependency. Polling stays as the fallback (§5.9). |
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

One arrow the diagram does not draw: the worker's writes **announce
themselves** over Postgres `LISTEN`/`NOTIFY` on the `arc_events` channel, and
the api fans them out to open browsers as server-sent events
(`GET /api/events`, §5.9). It is the only path from worker to browser, and
nothing depends on it — every page that reacts to an event also polls.

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
- Crash recovery, two rules with different questions (`jobs/runner.py`):
  - **By identity, at start-up.** `requeue_orphans(session, identity)` runs
    once, before the first claim, and returns every `running` job whose
    `locked_by` is not this process's identity (or is null) to `pending`
    (`run_after` = now, `last_error` "worker restarted while the job was
    running"), or to `failed` if attempts are exhausted. It ignores
    `locked_at` entirely: Arc runs **one worker per deployment** (§8), so a
    lock held by another identity is held by a process that is gone, whether
    it was taken two hours or two seconds ago. This is the one part of the
    queue that is not safe with two live workers — a second worker's
    in-flight rows would be requeued underneath it.
  - **By age, periodically.** `requeue_stale` (every 5 min, and once at
    start-up after the reclaim) returns `running` jobs whose lock is older
    than `WORKER_STALE_AFTER` (2 h). It is the backstop for the case identity
    cannot see: *this* worker's own in-process task dying without the loop
    noticing. Transcodes push `locked_at` forward while they work, so it
    bounds silence rather than work.
- Shutdown: on SIGTERM/SIGINT the loop stops claiming (rechecked after the
  concurrency slot is taken, so no job is started after the signal), in-flight
  jobs get `WORKER_DRAIN_TIMEOUT` (10 s) to finish, and anything still running
  is cancelled and reset to `pending`, due at once. That budget has to fit
  inside the worker container's `stop_grace_period` (15 s, §8): Docker's
  SIGKILL lands at the end of the grace whatever the drain is doing.
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
        acquisition/             want computation, window logic, samples (FR-A8)
        catalog/                 source protocol, AniList+MAL service, breaker,
                                 cache, seasons, list states, local search
          offline/               M15.5: the weekly manami + Fribb import
        tmdb/                    M15.5: key art, episode stills and credits
                                 for what AniList has not filled
                                 (download, parse, importer, job)
        playback/                resume, completion, what watching costs a list
                                 entry; `watched.py` is the leaf holding
                                 FR-W5's "what counts as watched", which the
                                 API and the wants reconciler both read
        retention/               cleanup rules
        storage.py               free space on the data volume (FR-T4, FR-T6);
                                 a leaf read by both retention's disk page and
                                 acquisition's storage guard
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
                                 HeroFrame, PosterWash, AspectProbe, Skeleton,
                                 EmptyState + styles.ts, aspect.ts (the
                                 remembered shape of every picture measured)
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

**Schedule shows a three-day window** (`pages/Schedule.tsx`, owner
2026-09-17). The API is unchanged — it still answers with the whole
Monday–Sunday week grouped by weekday in the viewer's zone — and the page
renders three of those days at a time, starting on today: `DayWindow` holds
the chevrons and the three `DayColumn`s, and the row of column headings, with
an arrow at each end, *is* the day-and-date bar (a `role="group"` labelled
"Days shown"). The state it keeps is an **offset from today**, not a start
index: the zone arrives with the response, so today is unknown on the first
render, and an offset lets the window land on today the moment the answer does
and follow it across midnight with the existing one-minute tick (`useClock`,
which now returns the weekday *and* the week's dates from that one interval).
The offset is clamped so the window never runs off either end of the week the
server sent. Dates come from `weekDates` in `lib/schedule.ts` — pure, read in
the schedule's own zone, arithmetic at noon UTC so a clock change cannot move
a day, and composed as "17 Sep" rather than formatted whole because `en-GB`
spells this month "Sept". Season prev/next is a different axis and is
untouched — but **only the live season's grid gets dates and a today**: the
response says which season it is, not whether that is the current one, so
`currentSeason` / `isCurrentSeason` (also in `lib/schedule.ts`) repeat the
server's quarter arithmetic from `services/catalog/seasons.py`, in UTC as it
does, and the page compares that with `data.year`/`data.season` rather than
with the URL — a pinned `?year=2026&season=FALL` in Fall 2026 is the live week
however it was arrived at. A browse shows weekday names alone and opens on
Monday, because there is no today in a season that is over. The room three columns buy (~350px against 158px) goes into the
rows: the whole title, never clamped, a 15px air time, a 56px key visual, and
for a followed show a quiet ember left rule with an `sr-only` "On your list".

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

**Artwork is never cropped past recognition.** Anime's native art is a 2:3
poster, and the two frames that are wider than they are tall pick what to put
in them by the *shape* of the picture, not only by its presence:

- The hero (`ui/HeroFrame.tsx`) is **always 21:9** — one fixed frame, so Watch
  Now's carousel does not change height between slides (owner, 2026-09-12).
  It fills the frame with the show's wide art **only if its natural ratio is
  ≤ 2.6** (a 16:9 backdrop loses a little off the top and bottom; a ~4.75:1
  AniList strip would lose half of itself); otherwise the poster treatment
  below, using that art itself as the wash ground when the show has no poster.
  With none at all it is the poster treatment as well.
- The 16:9 episode card on Watch Now (`EpisodeArt` in `pages/Home.tsx`) takes
  the episode's `still_url`; failing that the show's wide art **only if its
  natural ratio is ≤ 2.2** — the same rule one size down, with a tighter
  threshold because the frame is narrower (owner, 2026-09-12); failing that,
  the poster treatment.
- **"The show's wide art" is `backdrop_url` first, then `banner_url`**
  (`heroArt` / `bannerArt` in `lib/anime.ts`; owner, 2026-09-13). TMDB's
  backdrop is 16:9 and passes both tests; AniList's banner is a 1900×400 strip
  and passes neither, so a show whose only wide art is a banner renders the
  poster treatment however it arrived. The fallback stays for the deployment
  with no TMDB key and for the row the enrichment has not reached yet.

**The wash is the last resort, not the first frame** (owner, 2026-09-13, in
Safari on production: "semi-transparent posters", flickering on every rotation
of a season where every show has a backdrop). Shape used to be measured per
frame and kept in that frame's own state, so each of Watch Now's six slides
began at "shape unknown" — the wash — and swapped to the picture a moment
later. Three rules, all in `ui/aspect.ts`, which owns everything a frame knows
about a picture it has not drawn:

1. **A known shape is never measured.** `heroArt(anime)` returns the url *and*
   its ratio — `16/9` when the art came from `backdrop_url`, null for a banner
   — and `HeroFrame` takes it as `bannerAspect`, filling the frame on the first
   render with no probe and no wash; the 16:9 card (`EpisodeArt`) reads the
   same pair against its own threshold. `trustedAspect` recognises TMDB's own
   CDN (`image.tmdb.org/t/p/…`) for a caller that passes a bare url. Nothing
   else is trusted: AniList serves the strip and the 2:3 cover from one host.
2. **A measured shape is remembered for the tab.** `rememberAspect` /
   `useKnownAspect` are a module-level `Map<url, ratio>` with subscribers, so a
   slide coming round again, a show page opening on art Watch Now already
   measured, a card scrolled past and back, and a second visit to Home are all
   instant — no frame keeps a ratio in its own state any more. Watch Now probes
   *every* slide's art once when the line-up is known (`HeroArtProbes`), not
   one rotation at a time.
3. **Only the unknown waits.** `ui/AspectProbe.tsx` renders an off-frame copy
   of the art and reports `Artwork`'s `onNaturalSize` into the store (from both
   the ref callback and `onLoad`, because Safari fires no `load` for an image
   that was already cached), and the poster treatment is what shows until it
   answers — so neither frame ever flashes a zoomed strip on its way to the
   right answer, and a probe that never answers leaves the wash rather than a
   blank frame.

On top of the shapes, the carousel warms **the next slide's** pixels one
interval ahead (`new Image().src`, the one image that slide's frame will
paint): knowing a picture's shape is not having it, and the swap itself was
otherwise a moment of empty frame. One slide, not all six.

The **poster treatment** is one component (`ui/PosterWash.tsx`), shared by
both: the poster blurred (40 px, scale 1.15, brightness 0.5, saturate 1.2) and
scrimmed to fill the frame as a colour wash — a ground, not a picture, so its
resolution stops mattering — with the crisp poster laid over it at its own 2:3
ratio. A poster is never scaled up to fill a wide frame.

The reduced-motion rule in `index.css` turns off every CSS animation and
transition, but it cannot reach movement driven by a timer. Anything that
advances itself — today only Watch Now's hero — asks
`usePrefersReducedMotion` (`client/src/lib/media.ts`, alongside `useIsPhone`)
and simply never starts the timer.

**A shelf (`ui/Shelf.tsx`) can be scrolled with whatever is in the viewer's
hand.** The strip keeps its hidden scrollbar and its mandatory snap points, and
gains two things for a mouse (owner, 2026-09-13): a **drag** — pointer events,
`pointerType === 'mouse'` only, so touch and pen keep the platform's own
scrolling and inertia — where 6 px of travel is the line between a drag and a
click, the click that ends a real drag is swallowed once in the capture phase
so a card is not followed by accident, the cursor is `grab`/`grabbing` while
the content overflows, and scroll-snap is turned off inline for the duration
(mandatory snap fights a live drag) so the rail settles on a tile at release,
and `dragstart` is refused on the scroller — the tiles are links wrapping
images, so a few pixels in Chrome started an HTML5 drag and cancelled the
pointer, which killed the gesture before it began (owner, in Chrome, the same
day); one handler on the strip rather than `draggable={false}` on every tile;
and **edge arrows**, the shared `GLASS_CIRCLE` from `styles.ts` — the same
control as the hero's ‹ › — laid over the ends of the strip, each rendered only
when there is rail in that direction (a `scroll` listener and a
`ResizeObserver`, one measurement per frame), raised on hover or `focus-within`
and only under `@media (hover: hover)`, never on a phone. An arrow moves 90 %
of the visible width, smoothly. Both arrows are `position: absolute` over the
strip, so nothing on the page moves when they appear.

## 4. Data model (tables)

| Table | Key columns |
|---|---|
| `users` | id, email (unique on lower(email)), password_hash, role, is_active, timezone, created_at |
| `invites` | id, token_hash, email (optional), created_by (SET NULL), created_at, expires_at, used_at |
| `sessions` | id (opaque token hash), user_id, expires_at, user_agent |
| `anime` | id (internal identity PK), anilist_id (unique, nullable), mal_id (unique, nullable), summary_source / detail_source (anilist|mal), title_romaji, title_english, title_native, synonyms (JSONB), description (AniList HTML, stripped on output), format, episodes, status, season, season_year, cover_url, cover_large_url (AniList `coverImage.extraLarge`, a *summary* column; null on a MAL-filled row, whose biggest picture is 230 px), banner_url (AniList's 4.75:1 strip), backdrop_url (TMDB's 16:9 backdrop — **only** the TMDB enrichment ever writes it, §5.8, and it is what the 21:9 heroes and 16:9 cards prefer; null until the enrichment reaches the row), genres (array), tags (JSONB), studio, credits (JSONB `[{role, name}]`, studio first then Director / Series Composition / Character Design / Music / Original Creator from AniList staff; studio-only from MAL), relations (JSONB, anime-only), next_airing (JSONB), refreshed_at, popularity, average_score |
| `episodes` | id, anime_id, number, title, still_url (AniList `streamingEpisodes.thumbnail`; title and still are both written **only where null**, so a confirmed manual title survives every refresh), air_at, air_at_estimated (true when synthesised from a MAL broadcast slot), state (enum, §6 of spec), state_changed_at, unavailable_reason, last_search_at / last_search_forms / last_search_results (FR-A7, 2026-09-14: when `search_release` last asked Nyaa about this episode, how many query forms ran and how many distinct releases they returned between them *before* the filter — smallints, nullable, no backfill, written on every attempt that reaches Nyaa and never cleared. Three columns rather than a blob because all three are rendered on one line of the show page, and because "6 forms, 0 results" is what separates a query that matches nothing from a filter that keeps nothing) |
| `media_files` | id, episode_id (nullable until matched), path (unique), size (BIGINT), parsed (JSONB), match_confidence, match_candidates (JSONB), review_state, llm_suggestion (JSONB), created_at |
| `renditions` | id, episode_id (unique), dir, playlist_path, duration, width, height, subtitle_lang, audio_lang, ready_at |
| `list_entries` | user_id, anime_id (PK pair), status, progress, score, updated_at, updated_by (arc/mal), mal_synced_at, mal_dirty, activated_at (when the user first touched this show **in Arc** — FR-A9's dormancy stamp; null on a row a MyAnimeList import created and nobody has acted on since, write-once and never cleared, written only by `PUT /api/list/{id}`, the watch-completion path and `request_sample`, and never by any MAL path) |
| `watch_progress` | user_id, episode_id (PK pair), position_s, duration_s, completed, completed_at (set once, drives retention grace), updated_at |
| `mal_links` | user_id (PK), mal_username, access_token_enc, refresh_token_enc, expires_at, last_import_at |
| `mal_write_log` | id, user_id (CASCADE), anime_id (RESTRICT: audit rows must never be deleted by cache pruning), field, old_value, new_value (JSONB), cause (watch/manual/revert, plus `conflict` which is never a write), status (pending/ok/failed/skipped), error, created_at |
| `wants` | user_id, episode_id (PK pair), created_at, dropped_at, drop_reason, sample (bool, `false` by default — the want a user asked for by hand, "try episode 1" / FR-A8, rather than one the reconciler derived from their list) |
| `torrents` | id, episode_id, info_hash (unique), magnet, title, group, resolution, seeders, trusted, qbit_state, progress, added_at, completed_at |
| `jobs` | id, type, payload (JSONB), status, priority (lower runs first), attempts, max_attempts, run_after, locked_by, locked_at, last_error, created_at, started_at, finished_at |
| `rec_runs` | id, user_id, prompt, candidates (JSONB), picks (JSONB), model, created_at |
| `settings` | key (PK), value (JSONB) — admin-editable rules (preferred_groups, resolution, look_ahead_n, grace_days_g, unwatched_days_d, sub_lang, audio_lang, acquisition_paused, min_free_gb — FR-T6's storage floor in whole GB, default 10, 0..1000, 0 turning the guard off — slot_cap_k — FR-A10's per-user cap on shows fetching at once, default 5, 0..50, 0 meaning *unlimited*, which is the opposite of what 0 means for look_ahead_n), plus per-show overrides under `override:anime:<id>`. Written only through `arc/services/settings.py`, which validates every value (`validate` is a pure function, so the matrix is testable without HTTP) and logs one line per changed key with its previous value. The rule *readers* stay lenient by design — a hand-edited row is ignored with a warning rather than raising, because one bad row must not stop acquisition or shorten a grace period. |
| `offline_anime` | id (surrogate BIGINT PK), anilist_id / mal_id (indexed, **not** unique — it is somebody else's file), kitsu_id, anidb_id, title, synonyms (JSONB), type, episodes, status, season, season_year, picture, thumbnail, studios (JSONB), tags (JSONB), score (`score.arithmeticMean`, 0–10), duration_seconds (normalised from `duration.{value,unit}`), related (JSONB, the `relatedAnime` source URLs as given), search_text (title + synonyms, lowercased, joined by `" \| "`, with a **pg_trgm GIN index** so `ILIKE '%q%'` over 41k rows is an index scan). Composite index on (season_year, season). Replaced whole by the weekly import (§5.0a) |
| `offline_ids` | id (surrogate PK), anidb_id, anilist_id (indexed), mal_id (indexed), kitsu_id, tmdb_tv_id, tmdb_movie_id, tmdb_season, tvdb_id, tvdb_season, imdb_id (the first when the entry carries several), type. Fribb's `anime-lists`, and the only route from an Arc show to a **TMDB** id — AniList publishes none. An entry with neither an AniList nor a MAL id is dropped on import: nothing could ever reach it |
| `offline_imports` | source (PK: `manami` \| `fribb`), version (manami's release tag out of the header's `$schema`; Fribb's `ETag`/`Last-Modified`/download date), imported_at, rows, checksum (sha256 of the downloaded file — an unchanged file skips the parse and the replace entirely) |

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
- **Dormant imports (FR-A9, 2026-09-13).** Before the window there is a
  question of whether the entry counts at all. A `watching`/`planned` entry
  with `list_entries.activated_at IS NULL` whose show is not `RELEASING` is
  **dormant**: `compute_wants` skips it entirely — it contributes nothing to
  `desired` and its (user, show) does **not** join `wanting` — so any want it
  left behind is shelved (`dormancy.REASON_DORMANT`, "imported; not touched in
  Arc yet") rather than deleted, retention keeps its grace anchor, and a
  download in flight for it is cancelled by the ordinary
  `cancel_if_unwanted` + `qbit_cancel` path. The reason is distinct from
  `REASON_NOT_WANTING` only in what it *says* — an admin reading the wants
  table after an import needs "never picked up" told apart from "put on hold" —
  and both revive unconditionally, so touching the show brings the row straight
  back. The rule itself is `arc/services/acquisition/dormancy.py`, a leaf with
  no service imports: `catalog.lists` writes the stamp and cannot import
  `acquisition.wants` without closing a cycle through `catalog.airing`, so
  `activate()` and `is_dormant()` live where both sides can reach them.
  `is_dormant` takes *whether the show is airing* rather than the show, because
  that is the catalogue's question and the answer is a boolean by the time this
  rule cares. The writers of the stamp and no others: `set_list_entry` (every
  successful PUT, including one that re-sends the status the show already has —
  which is what the Show page's "Fetch this show" button does), **every**
  progress report for a show that has an entry
  (`playback.progress._activate_entry`, so Play is a touch from thirty seconds
  in rather than from 90 %; it never *creates* an entry, because "pressed play"
  is not "is watching this show" — FR-S4 draws that line at the completion, and
  the entry it creates there is activated) and `arc.api.mal.revert` (FR-M7's
  third user-originated event, including the entry a reverted removal
  recreates). Every MyAnimeList-driven path — import, push, conflict resolution
  — leaves it exactly as it found it. **`request_sample` deliberately does
  not**: "Try episode 1" writes one want for one episode, which is what was
  asked for, and activating would hand the show the whole N-episode window. For
  the same reason its `AlreadyFollowing` refusal now asks about the *window*
  rather than the status — a **dormant** watching/planned entry has no window,
  so a sample is allowed on one and behaves exactly like a sample on an
  unlisted show (the reconciler does not put the pair in `wanting` either).
- **Slot cap (FR-A10, 2026-09-13).** After the window and before `desired`,
  per user: at most K shows may be fetching at once (`slot_cap_k`, default 5,
  0 = no cap). `arc/services/acquisition/slots.py` is a leaf holding the pure
  `assign_slots(shows, k) -> (admitted, waiting)` over `SlotShow(anime_id,
  airing, updated_at, fetching, hungry)`; the two flags come from the
  reconciler, which is the only place that knows the wants and the window.
  **fetching** (an occupant) is a live, non-`sample` want of that user on an
  episode whose state is not one of `slots.SETTLED` — `ready` (arrived, waiting
  to be *watched*, so a user who let three episodes pile up does not stop
  fetching everything else), `unavailable` (FR-A6 gave up and retries daily;
  five unfindable shows would otherwise freeze a list for ever, and the retry
  needs no slot because it is not competing for the disk or the download queue)
  and `failed` (an admin's problem, not a slot's). **hungry** is a window
  episode with no live want of theirs: a show with nothing to ask for competes
  for nothing, which is what makes "one of the five finished, so the sixth
  starts" true on the next tick rather than handing the slot back to the show
  that has finished. Occupants are admitted whatever K says (lowering K cancels
  nothing and only stops new starts), free slots go to `RELEASING` first and
  then by `list_entries.updated_at` descending, ties keeping the reconciler's
  (user, anime) order. A show held back contributes **nothing**, and the pairs
  go to `_reconcile` as a set of rows it must not touch — not shelve, restamp,
  revive or delete, with one exception: a **live, non-sample** want whose
  episode is at or below the user's progress takes the ordinary "left the
  window" delete, because the user has watched past it and the completion in
  `watch_progress` is the anchor retention measures FR-T1's grace from, so the
  row has nothing left to say. Without that, a want the user had finished would
  sit live until the show next won a slot — and a live want makes the sweep skip
  an episode, so the cap would end up pinning files to the disk. Dropped rows,
  rows still inside the window and samples are untouched. The first shape of this was "its window intersected
  with the wants it already has", which a review broke: a want FR-T2 dropped on
  a `ready` episode belongs to a show that is hungry but not an occupant, so
  contributing only the *live* rows left that key out of `desired` while the
  show was still in `wanting`, and `_reconcile` deletes such a row — losing the
  `dropped_at` retention measures FR-T1's grace from, and handing the row back
  live the moment a slot freed, bypassing FR-T2's Arc-side-touch revival. A show
  in **neither** of `assign_slots`'s lists (settled: not fetching, not hungry)
  is not held back and still contributes its window, which for it is exactly the
  rows it already has. `WantsResult.waiting` counts the (user, show) pairs held
  back;
  `wants.slot_view(session, user_id)` is the same computation narrowed to one
  user, which is what `ListEntryOut.waiting` and the show page's note read, so
  "this show is waiting" on the page and "this show creates no wants" in the
  next reconciliation are one predicate. It carries `paused` and `held` with it,
  because "Arc starts this one when one of them finishes" is a promise and a
  stopped or held reconciler cannot keep it — `ListEntryOut.waiting_reason`
  (`slot` | `paused` | `held`) is what the page switches its sentence on.
  Nothing is persisted and nothing cached — a slot frees itself when an episode
  becomes ready, which no request is there to see. For one user the episode read
  is narrowed to `number > progress` per show, which is exact rather than
  approximate: `aired_through` is a max over dated past episodes, so dropping
  the ones at or below `progress` can only lower the boundary to `<= progress`,
  and `is_aired`'s `number <= boundary` is false for every episode the window
  considers either way. N and K come from one `rules.look_ahead_and_cap` read.
  Dormancy comes first: an untouched import is not a show
  waiting for room. Seeded by migration `7c1d5b3ae4f2`.
- **Storage guard (FR-T6, 2026-09-13).** `rules.is_storage_held` measures
  `shutil.disk_usage(DATA_DIR)` through `arc/services/storage.py` (a leaf that
  walks up to the nearest existing parent, and answers `None` rather than zeros
  when nothing in the chain can be read) and compares the free bytes against
  `min_free_gb` with the pure `rules.storage_hold`. While held: `compute_wants`
  **still reconciles** — dropping, shelving, releasing and cancelling all free
  space — and only `_start_searches`'s starting half is skipped (an episode with
  a live want is skipped, not released: it is still wanted); `search_release`
  requeues itself on `PAUSED_RETRY` through the same `_requeue_paused` the pause
  uses, with `HELD_LOG` naming the disk instead of the switch; `poll_qbit` and
  the transcodes are untouched, because finishing what has landed is how a
  source becomes deletable. A floor of 0 never holds, exactly *at* the floor is
  not held, and a measurement that fails never holds (logged once, then
  quietly). `compute_wants` takes `settings` for this and nothing else; without
  one there is no measurement and therefore no hold, which is the same answer a
  failed measurement gives. Nobody presses a hold and nobody clears it: the
  next tick after retention frees room starts fetching again.
- `compute_wants` (every 15 min, after any list change, and after a watch
  completion): for each **non-dormant** `watching`/`planned` entry, `p` = max(furthest
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
- **Stalls (FR-A6, 2026-09-13).** The same handler gives up on a torrent that
  is going nowhere, which before this it never did: `poll_qbit` reacted only to
  a torrent that had *vanished*, so a dead magnet held one of qBittorrent's
  download slots until a person noticed. `jobs.stall_reason` is a pure function
  of one `torrents/info` row, and it says *stalled* for a torrent in
  `metaDL`/`forcedMetaDL` past `STALL_METADATA_MINUTES` (60), or one in
  `downloading`/`forcedDL`/`stalledDL` past `STALL_NO_BYTES_HOURS` (6) that has
  either fetched nothing ("no bytes after 6 hours") or whose swarm the tracker
  says is empty ("no seeders after 6 hours"). Then: the row is marked
  `torrents.qbit_state = "stalled"` and the episode → `unavailable` with that
  sentence, **the session is flushed**, and only then `torrents/delete` **with
  files** — so a client that stops answering mid-poll leaves a coherent
  database and one torrent to tidy up, which the next poll does (a `stalled`
  row still present in the listing is deleted again, `qbit.DELETE_ON_SIGHT`).
  FR-A6's ordinary retry schedule takes the episode from there.
  Four exclusions, each of which was or would have been a bug:
  - **the clock is the client's own `time_active`**, never `torrents.added_at`.
    Now that Arc bounds the queue, a torrent sits in `queuedDL` for hours and
    then starts downloading already nine hours old with nothing fetched —
    against the row's age that is a stall on its first poll, and `stalledDL`
    (which means only "no bytes this instant") would have made it one. A client
    that does not report `time_active` stalls nothing.
  - **an empty swarm is only ever the tracker's two figures**
    (`num_complete`/`num_incomplete`, both scraped and both 0 —
    `TorrentInfo.dead_swarm`). `num_seeds`/`num_leechs` are the peers this
    client holds *right now*, are reported as 0 all the time on healthy
    torrents between announces, and reading them would delete a 60 %-complete
    download. `-1` on either tracker figure means "not scraped yet" and is
    mapped to `None`.
  - a state outside the allow-list (`queuedDL` is most of the queue for hours
    after a list import, `stoppedDL`/`pausedDL` is a person's own decision —
    production had 297 of those), and a *complete* torrent.
  - anything with `dlspeed > 0`: bytes are arriving, which settles it.

  A `stalled` row outlives the torrent, and `poll_qbit` **never moves an
  episode on account of a row that already carries a decision**. Rows are
  polled oldest first, so without that guard the stalled attempt — gone from
  the client by Arc's own hand — dragged an episode that had since been
  retried from `downloading` back to `unavailable` on every tick, and the
  release that was working was never handed to the library.
- **Absolute numbering on sequels (FR-A4, 2026-09-17).** `nyaa.absolute_offset`
  is how many episodes ran *before* a catalogue entry, and it is the one input
  to the rule that must never be inferred. It walks `anime.relations` →
  `PREQUEL` through **cached `anime` rows**, adding `episodes` at each hop, and
  answers `None` — declining the whole feature for that entry, which is to say
  leaving it exactly as it behaved before — when any of these holds: the entry
  is a single (`is_single`); a prequel edge that is not a
  `MOVIE`/`OVA`/`SPECIAL`/`MUSIC` (`ABSOLUTE_SKIPPED_FORMATS`) resolves to no
  cached row; that row's format is not `TV`/`ONA` (`ABSOLUTE_COUNTED_FORMATS`
  — a `TV_SHORT`, a null format, anything unclassifiable) or its `episodes` is
  null or zero; that row's `status` is not `FINISHED`
  (`ABSOLUTE_FINISHED_STATUS`; every source normalises into AniList's
  vocabulary on the way in, so there is one spelling — a `RELEASING` season's
  count is an *announced* total, the number likeliest to be wrong, and being
  one out puts the absolute number inside the prequel's own band where the
  season check cannot see it); **two** countable prequel edges sit at one hop;
  the chain revisits a row or exceeds `MAX_PREQUEL_HOPS` (10). Films and OVAs in the chain
  are *skipped* (the walk stops at one rather than reaching around it), which is
  what lets *Jujutsu Kaisen* season two — `PREQUEL` edges to season one **and**
  to *Jujutsu Kaisen 0* — be answered 24 instead of abandoned. Zero is never
  returned: "nothing came before" and "do not use this" are one answer.
  The function is pure and takes an injected async resolver, so the query corpus
  still runs offline; `jobs._prequel_offset` is the database half (AniList id
  first, MAL id second, never one `OR` over both) and `search_release` computes
  it once before the first request, logs at INFO whether an offset applies and
  which, and hands it to `search_for_episode`. `_prequel_offset` **cannot fail
  a search**: `relations` is JSONB written by whichever source answered, so an
  id that is not a number is skipped without a query being issued and anything
  else is caught once, logged at WARNING, and answered `None` — an entry with no
  offset is the documented ordinary outcome, and an exception here would put the
  episode back on the retry schedule over a field the search never needed.
  With an offset, `queries()` adds `"<season-stripped base> - <N+offset>"` and
  the dashless `"<base> <N+offset>"` **directly behind the romaji short forms**
  (padded against the *combined* episode count, so a franchise past 100 is
  written the way a long show is) — but **only where stripping the season
  marker actually shortened the title**: an unmarked sequel whose title names an
  arc (*Made in Abyss: Retsujitsu no Ougonkyou*) has no marker to strip, so its
  "base" is the whole nine-word name and the pair would spend two of ten slots
  on a form nobody writes. The acceptance route below still applies to such an
  entry if one of the forms that *are* asked returns an absolute release.
  `acceptable()` gains one route to a
  candidate: a release whose parsed episode is exactly `N + offset`, that
  **names no season at all**, and whose title clears the same 0.90 bar. A
  release that names a season is judged by the ordinary per-season rule and
  nothing else. `filter_items` then applies the rule a single release cannot
  state: where any season-marked candidate for this episode survives, every
  absolute one is dropped — explicit beats inferred, and a pool holding both
  would let seeders settle it. `Candidate.offset` carries the arithmetic into
  `rank`, whose reasons say `absolute numbering: release 25 = episode 1`.
- **A release is chosen once.** `jobs._pick` skips any candidate whose info
  hash already has a `torrents` row — any episode, any state. A row only
  exists because an earlier attempt *committed* (`search_release` is one
  transaction), so the row means "tried, and it did not produce the episode";
  re-choosing it would re-add the same dead magnet. With nothing left to try
  the episode takes the ordinary "no release yet" path and looks again
  tomorrow. Paired with the filter's new 0-seeder rejection (§6, Nyaa row):
  zero seeders is not a worse candidate, it is a file that cannot be fetched.
- **Cancelling (2026-09-13).** The reconciler's release step has a second half.
  `wants.release_if_unwanted` returns a `wanted`/`searching`/`unavailable`
  episode to `not_wanted`; `wants.cancel_if_unwanted` does the same for a
  `downloading` one — marking every one of its `torrents` rows `cancelled` and
  taking the `downloading → not_wanted` edge — and the client-side removal is a
  **`qbit_cancel` job per episode**, queued after the flush. A job rather than
  a loop inside `compute_wants` for two reasons: the reconciliation holds a
  transaction and must not hold it across an HTTP call, and a client that is
  not answering must not be able to fail it (the job carries that risk, with
  the runner's backoff behind it). The job deletes only the hashes whose row
  says `cancelled`, so an episode wanted again in the seconds in between keeps
  the release its new search has just chosen — and, for the same reason, the
  reconciler marks only rows that do *not* already carry a decision: an older
  `rejected` attempt beside the live one is holding the file somebody is
  looking at in review, and `qbit_cancel` deletes with files. After a
  successful delete the job **deletes the `torrents` rows** it just cancelled,
  because a row bars its release from `_pick` for ever: right for `stalled`
  and `rejected` (tried, failed) and wrong for a cancel, where a change of
  mind a minute later would otherwise have cost the episode the best release
  on Nyaa. Nothing references them by then — the episode is `not_wanted`, the
  torrent is out of the client and its files are gone. Episodes from
  `downloaded` onwards are untouched: those bytes have landed and are
  retention's (FR-T1). `qbit_state` values that are Arc's own decisions
  (`rejected`, `stalled`, `cancelled`; `qbit.DECIDED_STATES`) are never
  overwritten by a poll, in either direction. `samples.cancel_sample` calls
  the same two helpers, so pressing Cancel mid-download stops it now rather
  than at the next tick.
- **Samples (FR-A8, M16).** `POST /api/anime/{id}/sample` writes one `wants`
  row with `sample = true` on the show's lowest-numbered episode, **starts the
  search there and then** and enqueues `compute_wants`; nothing else about
  acquisition changes, and no list entry or MAL write is involved. The route
  does not copy the reconciler's rule: the per-episode halves of
  `_start_searches` are the public `start_search()` (STARTABLE check, the
  `UNAVAILABLE_RETRY` gate — skipped here with `retry_now`, because a person
  who has just asked is not the fifteen-minute sweep — the `wanted`
  transition and the deduped `search_release`) and `release_if_unwanted()`
  (RELEASABLE and no live want of any user → `not_wanted`), and both the
  reconciler and the two sample routes call them. So the episode row tells the
  truth as soon as the page re-reads instead of after the next tick — and for
  ever, if acquisition happened to be paused. Paused still means paused: the
  episode reads `wanted` and `search_release` requeues itself without touching
  Nyaa. `DELETE` drops the row(s) and calls `release_if_unwanted`, so an
  episode nobody else wants goes back to `not_wanted` immediately while one
  that is already downloading is untouched. `arc/services/acquisition/samples.py` owns the
  three refusals (no episodes cached, episode 1 unaired per
  `catalog.airing`, the show already `watching`/`planned` — the window covers
  it) and hands each up as the sentence the 409 carries. A second press with a
  sample already live answers that same sample and writes nothing (one sample
  per (user, show): "the lowest-numbered episode" is not a stable answer, and
  Cancel drops every live sample row on the show rather than one). The reconciler reads
  a sample off the row rather than off a list: a **live** sample whose
  (user, show) is not watching/planned **and whose episode its user has not
  completed** is added to `desired` as it stands, so it is never shelved as
  "no longer watching"; a sample on a followed show is governed entirely by
  the window like any other want; a sample its user has *watched* leaves
  `desired` and is shelved, which is the only ending available on a show that
  is on the list as `completed`/`dropped`/`on_hold` (FR-S4 leaves an existing
  entry's status alone, so the show never joins `wanting`, and FR-T2 will not
  drop a want whose user has watched the episode — without this the bytes
  would be pinned by a live want forever); and a **dropped**
  sample outside the window is left exactly as it is — cancelled
  ("sample cancelled") or stale (FR-T2) — because FR-T2 is a sample's
  intended end and re-labelling it `REASON_NOT_WANTING` would hand it back
  that reason's unconditional revival. Pressing the button again is the only
  way back, and it clears the drop itself.
- `episodes.state` is written only by `acquisition/states.transition()`,
  which enforces the spec §6 table and logs every edge. Extra edges beyond
  the diagram: `searching → not_wanted` (want vanished mid-search),
  `matching → unavailable` (delivered file rejected in review),
  `wanted|unavailable → not_wanted`, and `downloading → not_wanted`
  ("nobody wants this episode": the cancel above, the one place the reconciler
  reaches into work in flight). One process-wide Nyaa client keeps the
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
  episode/range, season, part, version, group, resolution, year, `dubbed`, kind:
  episode|batch|movie|special|nc|unknown). Pinned by
  `tests/fixtures/release_names.txt` (259 names, 100 % on episode+kind and
  title_key).
- **A slash is a title character** (2026-09-14). `rsplit("/", 1)` read
  `[SubsPlease] Fate/Zero - 12 (1080p)` as a show called `Zero` with no release
  group — a title no catalogue entry and no query can match, so the acquisition
  filter rejected every release of *Fate/Zero*, *Fate/strange Fake* and
  *Fate/kaleid liner* and the library sent every file of them to review.
  Nothing in the string says whether a slash is a separator or a name, so
  **the caller says**: `parse(name, *, path: bool = False)`. The ingest scanner
  and the match job pass `path=True` and get the last component and only that
  one (no path component can contain a slash, so the last separator is always
  the one before the filename); a Nyaa title, a corpus line and anything a
  person typed pass `path=False` and are never split. A first version of this
  guessed — "a separator with a non-alphanumeric beside it, in a string that
  looks like a path, with balanced brackets on both halves" — and a guess is
  exactly what a `" / "` inside a tag defeats. One boolean at three call sites
  replaces all of it. **What the flag cannot rescue**: a torrent named
  `[SubsPlease] Fate/Zero - 12` lands on disk as a *directory*
  `[SubsPlease] Fate` holding a file `Zero - 12`, because no filesystem holds
  the slash — so that file goes to review with the title it really has, and the
  place the show's name matters (the acquisition filter, reading the Nyaa
  title) is the place that keeps it.
- **Dubs** (`ParsedName.dubbed`, FR-A3, 2026-09-14): `English Dub`, `Eng Dub`,
  `[Dubbed]`, `(Dub)`, `[DUB]`, `[EN DUB]` — the word standing on its own — or a
  release **group whose name ends in `dub`/`dubs`** (`[KaiDubs]`, which says it
  no other way). `Dual Audio` is deliberately **not** a dub: it carries the
  original track too, so it is an ordinary candidate that happens to be bigger.
  Only a release with nothing but the dub is the one the ranker puts last.
- **Batches (FR-A4).** An episode *range* at the episode position (`01 ~ 12`,
  `01~12`, `01 - 12`, `01-12`, `01〜12`, `01～12`, `E01-E12`, `S01E01-E12`,
  `001-024`) or a batch
  marker anywhere in the name (`BATCH`, `Batch`, `Season Pack`) makes the
  file a `batch`, whatever else the name says — including a first episode
  number, which is all anitopy reports of a range. The `Complete` family
  (`Complete`, `Complete Series`, `S1 Complete`) is read **only when the name
  names no single episode**: on a run it means the whole show, but on one
  numbered file it is what a group shouts on the last episode, the same claim
  as `END`, so `[Group] Show - 12 [COMPLETE]` is episode 12. Both ends of a
  range must be zero-padded (or `E`-prefixed), count up from at least 1, not
  both be plausible years (1950–2099, so `Cowboy Bebop 1998-1999` is an airing
  span) and have nothing but a tag behind them (so `- 01 - 100 Poems` is
  episode 1 with an episode title) — which is what keeps `Ranma 1-2`,
  `1920x1080`, `10-bit`, `01v2`, a scene date and a title ending in a number
  (`Mob Psycho 100 - 07`) out. The last two conditions apply to anitopy's own
  range answers as well, not only to the pattern. A run anitopy cannot read
  (`01〜12`) is also removed from the title, by a pattern built from the two
  numbers the file was found to hold, so *86* and *07-Ghost* keep their names. A batch never reaches disk by acquisition (§6) and, when one arrives
  through `manual/`, goes to review; the individual files *inside* a batch
  directory are ordinary episode names and parse as episodes.
- Candidates: expected-episode prior (Arc-downloaded files; applied as a
  bounded bonus so it can never beat a title that says otherwise), fuzzy
  search over the local cache, then (M15.5) the **offline catalogue** — up to
  `OFFLINE_HITS` = 12 hits, materialised as `anime` rows — and a live catalogue
  search (AniList → MAL) **only when the offline search returned nothing**.
  Offline first because release groups write in manami's synonym vocabulary:
  "Mushoku Tensei S3" is one of its names and is a string no live search
  matches. The offline pool is wider than the live one (12 vs 5) because its
  ranking leads with exact-name holders and a franchise's earlier seasons all
  list the base title as a synonym, so five hits cut the numbered sequel off.
  The scoring, the threshold and the "below it, review — never auto-link a
  guess" rule are untouched; `origin` is recorded in the reasons, never scored.
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
  ordinary reports.
- **The end of an episode, as built (M16 batch 3, FR-S5, owner 2026-09-17).**
  Two moments, and the client tells them apart by *which fact it is reacting
  to* rather than by a threshold of its own.
  - The **completion receipt** is raised by the one progress response whose
    `newly_completed` is true — the server's once-per-(user, episode) answer
    (below) — so a rewatch never raises it and the client carries no rule
    about rewatches at all. It is a `role="status" aria-live="polite"` pill of
    the same glass the resume notice wears, bottom-left and 108 px up so it
    clears the control bar whether or not the bar has faded, reading "Marked
    as watched". Four seconds on a timer keyed to the flag, a click on the
    pill puts it away sooner, and it carries **no button**: nothing is being
    asked. The manual "mark watched" control raises no toast — it flips its
    own glyph, which is a receipt already.
  - The **end-of-episode card** is derived, not stored: it is up while
    `ended || (total > 90 && total − position <= 90)` and the viewer has not
    dismissed it. Deriving it means a seek backwards out of the window takes
    it away by the same rule that brought it, and the only thing that has to
    be remembered is the viewer's "no". *Keep watching* — and `Escape`, which
    is deliberately not `preventDefault`ed so the browser can still leave
    fullscreen on the same key — sets that flag for the rest of the playback;
    the page is keyed on the episode id, so the next episode starts with it
    cleared. The card is a glass panel above the control bar over a bottom
    gradient, not the black wash it replaces: the episode is still playing
    during the last ninety seconds, and the scrim is `pointer-events-none` so
    a click on the picture still pauses it. **Next episode** is the primary
    and is drawn only when there is a next episode and it is ready; when it
    exists but is unprepared, or there is none, the card's line says which and
    no dead button is drawn under a sentence that has already explained
    itself. Nothing is focused on mount, so FR-S6's window shortcuts keep
    answering, and `ended` advances nothing on its own. Both the pill and the
    card are children of the element the page hands to `requestFullscreen`,
    which is what keeps them visible in the mode an episode is most likely to
    be finished in. The `>90 s` guard keeps a file shorter than the window
    itself from opening with its own end-of-episode card; such a file still
    gets it on `ended`.
  - The ⟲10 / ⟳10 skip glyphs were redrawn in the same pass (owner: the arc
    was on the wrong side). One drawing — a ring open on the **left**, running
    the long way over the top, with a corner arrowhead at the top of the
    opening pointing down into it, which is anticlockwise — mirrored about the
    vertical for the forward control. The digits sit outside the mirrored
    group so "10" never comes out backwards.
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
   an automatic event — the guard is decided from the **cause on each queued
   row**, so the `manual` row `DELETE …/watched` writes (§5.5a) is the one
   lowering progress write Arc sends, and a `watch` row below MAL's number is
   still refused and logged `skipped`. Status/score writes come from the
   explicit list endpoints via the same path — and from FR-W5's auto-complete,
   which is the one status write a watch event may make (§5.5a).
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

**Watched state, the manual mark and auto-complete (FR-W5, M16, owner
2026-09-13).**

- **One definition of watched**, in `playback/watched.py` — a leaf that imports
  nothing from Arc, because the acquisition reconciler needs it and
  `playback/progress.py` reaches back into acquisition for FR-A9's stamp.
  `watched_source(number, completed=, list_progress=)`: `arc` when the caller
  has a completed `watch_progress` row, else `progress` when `number <=
  list_entries.progress`, else `None`. Its sibling `watched_through(list
  progress, highest completion)` is the whole-show form the acquisition window
  starts after (`wants._plan_wants`), so "how far has this user got" has one
  implementation; the two shapes disagree only where progress sits below a
  completion, and that module's docstring says why the right answers there
  differ. Every renderer gets it from `EpisodeOut.from_episode`, which
  takes the two facts (`completed`, `list_progress`) and sets both `watched`
  and `watched_source`; nothing else in the API computes watchedness. The
  callers supply the two halves: `AnimeDetail.build` reads the progress off the
  list entry it already holds and the completions from one
  `completed_episode_ids` query; `GET /api/home` adds one
  `list_progress_for(user, anime_ids)` query for every shelf at once; `/play`
  reads the caller's entry by primary key.
- **`watched_source` is also "is this a button?"** (owner, 2026-09-13, second
  pass). `DELETE …/watched` lowers `list_entries.progress` by one when the
  episode is the latest watched, so the episode **at** the progress is
  actionable whether or not Arc holds a completion row, and one **above** it is
  actionable when it does. Both are `arc`. An episode **below** the progress is
  `progress`: nothing there would move, because the un-mark only ever takes the
  line down by one, and the Show page renders a non-actionable "Watched" with
  the tooltip "Unwatch from the latest watched episode down". A progress of
  zero means nothing is watched, whatever the numbering (a show starting at
  episode 0). One field, not a second `unwatchable` boolean beside it: the
  client's only question is whether the pill is a button, the two could never
  legitimately disagree, and a flag that always tracks another field eventually
  does not. A payload with no `watched_source` at all (a cache from before M16)
  is treated as `arc`, which is what `watched` could only have meant then.
- **The un-mark** (`progress._retreat_list`) is the mirror of the advance and
  deliberately narrower: it moves the number by one and only from the episode
  the viewer pressed. `updated_by = arc`, `mal_dirty`, `activated_at` stamped
  (FR-A9 — it is a progress change the user made here), one `compute_wants`
  (FR-T3: rewinding re-acquires), and one `progress` write log row with cause
  **`manual`** carrying the previous value. That cause is the whole of FR-M4
  here: `sync._guard` refuses a lowering progress write whose cause is `watch`
  and lets an explicit edit through, so this is the one path in Arc that lowers
  MyAnimeList's progress and no automatic path can reach it without writing a
  row that claims to be a user's edit — which `tests/test_mal_guard.py`
  polices by AST. The **status is never rolled back**: a show the auto-complete
  finished stays `completed`, by the mirror of the argument that let Arc set it.
- **The manual mark is the completion path, not a parallel one.** `POST
  …/watched` calls the same `record_progress(force_complete=True)`, so marking
  episode N writes N's completion row, raises `list_entries.progress` to N if
  lower with `updated_by = arc` / `mal_dirty`, queues one `progress` write log
  row with cause `watch` and one `compute_wants`. Below the current progress it
  writes the completion row and nothing else — nothing moved, so nothing is
  owed. **No synthetic completion rows for 1…N-1**: the progress number is what
  says they were watched, and eight fabricated `completed_at` values would be
  eight wrong answers to "when".
- **Auto-complete** (`progress._auto_completes`) fires inside the same
  transaction as an advance, and only on an advance: `anime.status ==
  "FINISHED"` **and** `anime.episodes` known **and** `entry.progress >= count`
  **and** the entry not already `completed` — any other status, `on_hold` and
  `dropped` included, does complete (owner, 2026-09-13: finishing the last
  episode of a show you had dropped is the clearest statement anybody makes
  about a list entry). It sets `status = completed` and
  queues a second write log row (`status`, cause `watch`, `old_value` the
  previous status or `null` for an entry this path just created), so the one
  queued `mal_push` sends status and progress in a single PATCH. A `RELEASING`
  show is never completed (its count is a projection), an unknown count has no
  end to reach, and a rewatch advances nothing.
- **Retention** (§5.7) gains the matching anchor, so the files of an episode
  somebody skipped past are deleted on the same schedule as one they watched —
  clamped to the age of the bytes, or a re-fetched episode under a months-old
  imported progress would be swept within the hour.

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
completed_at`, any user), every `list_entries.updated_at` of a user whose
`progress` covers the episode's number (FR-W5, 2026-09-13 — one grouped join
on `(anime_id, number <= progress)`; the column is an approximation, since it
moves for any change to the row and nothing records "when progress passed
episode 4", and it errs *late*, which for a deletion is the right direction —
and it is **clamped to `file_age_from`**, because an imported stamp can be
months older than a file FR-T3/FR-T4 re-fetched this morning and the sweep
would otherwise delete it before anybody could play it)
and every `wants.dropped_at`; a want that ends
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
- **Search is local, then offline, then live** (`catalog/local.py` +
  `catalog/offline/search.py`, M15 / M15.5). The cached `anime` rows are
  matched before the upstream call and put in front of the live page: the
  query is split on whitespace and every word must appear, as a
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
  **Then the offline catalogue** (M15.5): the same word-by-word rule over
  `offline_anime.search_text` (title + every synonym, lowercased, trigram GIN),
  ranked exact name → prefix → `score DESC NULLS LAST` → type (TV/MOVIE/ONA
  ahead of OVA/SPECIAL) → episodes → id. Exact/prefix are judged against the
  whole name list, not the title alone, because manami publishes one romaji
  title and the English name is a synonym. Those hits are **materialised as
  `anime` rows** (`offline/materialise.py`) so each has an internal id its card
  can link to, and entries carrying neither an AniList nor a MAL id are
  dropped. The two local blocks together are capped at 20 (`PRE_LIVE_LIMIT`),
  so the offline block only fills what the cache left. A 502 now needs *both*
  local tables empty on a failed upstream.
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
  a MAL-first row instead of creating a second one. Since M15.5 a payload
  carrying only one of the two ids has the other **filled from the offline id
  map before the lookup** (`offline/ids.py`, Fribb first then manami's parsed
  cross-ids), so a MAL-found show lands on the row AniList made — and the
  other way round — with no network at all. Only a null id is ever filled, and
  an id two map rows disagree about is declined rather than guessed.
  `detail_source` records which source last filled the detail columns; MAL data
  never overwrites AniList-sourced detail, AniList always overwrites
  MAL-sourced detail.
- Source strength for rule 3 is `SOURCE_STRENGTH` = AniList, MAL, **offline**
  (`catalog/source.py`); `SOURCE_NAMES` stays the two *live* sources
  `CatalogService` tries and reports on. So an offline-materialised row fills
  nulls only, never sets `refreshed_at` or `detail_source` (opening it still
  triggers a real fetch), and the first live payload for the show overwrites
  every column it wrote. `synonyms` and `studio` are written by the
  materialiser where the row has none — the synonym list is what makes the
  next local search and the filename matcher find the show.
- MAL-sourced episodes: `air_at` synthesised from `start_date` + broadcast
  weekday/time (JST) for 1..num_episodes, `air_at_estimated = true`; AniList
  schedule data replaces them and clears the flag.
- Jobs: `catalog_reconcile` (hourly; since M15.5 it first fills missing
  `anilist_id` from the offline id map for up to 500 MAL-only rows with no
  network and no health gate — the pass that works *during* the outage that
  created those rows — and only then, when AniList is healthy, asks
  `Media(idMal:)` about the leftovers), `catalog_season_sweep` (daily at 03:30
  UTC, and at worker start when the current season has no cached rows:
  upsert the current and next season's summaries from whichever source is
  up, including `nextAiringEpisode`; MAL-sourced airing shows get a
  synthesised `next_airing` from their broadcast slot, marked
  `estimated`, episode number unknown). When **both live sources fail** for a
  season the sweep seeds it from `offline_season()` instead (M15.5): the
  titles, as `source="offline"` summaries with no `next_airing` and no
  episodes, so the season page lists them and the schedule shows them
  unscheduled until a source answers again (FR-C7). The fallback lives in the
  sweep rather than inside `CatalogService`, which is a composite of two HTTP
  sources and holds no session — and is the only caller of `season()`.
  After the summary upsert the sweep enqueues a detail refresh (which
  brings the airing schedule) for every current-season TV/TV_SHORT/ONA row
  with no `next_airing` and no episode rows, spaced 5 s, max 60 per sweep,
  so most of the season gets a weekday within the hour.
- An estimated (MAL) `next_airing` never replaces an existing blob on a row
  whose summary or detail came from AniList, and never replaces a published
  one; AniList blobs always replace MAL ones. The schedule exposes
  `next_at_estimated` so the UI can mark synthesised times.
- **Season grid membership (2026-09-13).** `catalog/schedule.py` builds two
  statements and the router runs them. `season_members(year, season)` is the
  catalogue's own answer — the rows tagged with that season — and is the whole
  rule for a prev/next view, which is a catalogue browse. The **current**
  season's grid is a calendar, so it adds `airing_this_week(now)`: `anime`
  rows with `status = RELEASING`, a weekly format (TV/TV_SHORT/ONA), and a
  known air time within `AIRING_WINDOW` (7 days either side of now) — the
  cached `next_airing` blob's `airingAt`, compared as epoch seconds in SQL,
  or, only where the row has no blob at all, an `EXISTS` over `episodes.air_at`
  in the same window. One extra query, merged by `anime.id` with the season's
  own rows winning; still cache-only, no live call. The trailing edge is the
  same 7 days as the staleness rule below, so a row is never pulled in for a
  slot placement then discards. A `RELEASING` row with no air time anywhere is
  not carried in — nothing would place it on a weekday — and its own season's
  page still lists it as unscheduled. Non-weekly formats are excluded so the
  current season's `unscheduled` list stays its own films and OVAs. The season
  tag is never rewritten; `ScheduleEntry.carried_over` says the row is here for
  being on air, and the client renders "Since Spring 2026" from the summary's
  own `season`/`season_year` (nothing for a row with no season year, e.g. a
  long-runner). Found on That Time I Got Reincarnated as a Slime Season 4:
  `RELEASING`, episode 23 on Friday 2026-09-18, tagged `SPRING 2026`, absent
  from the Summer 2026 grid. Home's `behind`/`new_this_week` were checked and
  are season-agnostic by construction (they start from the caller's list and
  the episodes' dates); a test pins it. `catalog_refresh_all` and
  `catalog_pre_air` were already season-blind — `status = RELEASING` and a
  `next_airing` in the hour — so the carried-in rows stay as fresh as any
  other; tests pin that too.
- Schedule placement: a `next_airing` older than 7 days is ignored (hiatus)
  and the row falls back to its last real episode air time (the *highest-
  numbered* episode with one, not `max(air_at)`). The aired rule
  (`air_at <= now`, estimated dates count, RELEASING boundary, FINISHED
  fallback) lives in one place, `catalog/airing.py`, used by the show page
  and by behind-by.
- **Airing sanity (2026-09-12).** Two rules in `catalog/airing.py` sit over the
  stored dates, because both sources publish ones that cannot be true: a
  `FINISHED` show has no future episodes (every episode counts as aired
  whatever its `air_at`), and an episode dated *after* a higher-numbered one is
  non-monotonic — it is reported `air_at_estimated` (the client's "est."
  marker) and counts as aired by the earlier of its own date and the next
  episode's. Derived, never written: the row keeps the date the source
  published, so the next refresh can still correct it. Every reader of
  aired-ness gets both for free — show page rows, behind-by, the acquisition
  window, `aired_episodes` — since all of them already go through
  `aired_through`/`is_aired`. Found on One Room 3rd Season (AniList 205068,
  MAL 64683): `FINISHED`, episode 3 dated 2026-09-27 between episodes that
  aired in August and early September.

### 5.0a Offline catalogue (M15.5)

Two public datasets, imported weekly into `offline_anime` and `offline_ids`,
so that search, filename matching and id mapping have a source that **cannot be
unreachable** (FR-C6). It exists because AniList suspended its third-party API
for three days in September 2026 and took all three with it.

- **Sources.** manami's `anime-offline-database` — one zstd-compressed JSONL
  release asset, ≈ 6 MB compressed / 62 MB raw, ≈ 41.5k anime, every
  alternative title each one has ever been released under — and Fribb's
  `anime-lists` (`anime-list-full.json`, ≈ 7.5 MB), the cross-id map that adds
  TMDB series + season, TVDB and IMDb. Neither needs a key. `OFFLINE_MANAMI_URL`
  points at `releases/latest`, deliberately: the point of a weekly job is that
  it picks up the week's release.
- **Download** (`catalog/offline/download.py`) streams to a temporary file
  under `DATA_DIR/offline`, hashing as it goes (sha256, 60 s timeouts, 1 MiB
  chunks), and deletes it afterwards whatever happens. Nothing holds either
  file in memory. The reader sniffs zstd's frame magic rather than trusting a
  file name, which is what lets the test fixture be a plain `.jsonl` slice.
- **Parse** (`parse.py`) is pure functions, and the hard part is leniency:
  every field in both files is optional in practice. `animeSeason.year` is null
  for 1,557 titles; `themoviedb_id` is `{"tv": N}`, `{"movie": [N, …]}`, null,
  or a bare integer in older entries; `imdb_id` is a list now and was a string
  before; manami carries records with no MyAnimeList source at all. Ids are
  parsed out of the entry's `sources` URLs (anilist.co, myanimelist.net,
  kitsu.app **and** the old kitsu.io, anidb.net; the other six hosts are
  ignored). An unreadable line is skipped, not fatal. The only thing that
  disqualifies a record is having no title.
- **Replace-on-import** (`importer.py`), in **one transaction** per source:
  `DELETE` every row, insert the new ones in chunks of 2000, upsert the
  `offline_imports` row, commit. `DELETE` rather than `TRUNCATE` on purpose —
  `TRUNCATE` takes an `ACCESS EXCLUSIVE` lock and would block every concurrent
  reader; this way a reader sees last week's rows until the commit and this
  week's after it, never a half-replaced table. An import whose checksum
  matches the loaded one is skipped entirely (unless the table is empty, which
  is a state to repair rather than preserve). Measured on the real files: 37 s
  for a full import of 41,537 + 32,281 rows, 2.4 s when nothing changed.
- **Independent sources.** `import_offline_catalogue` imports manami and Fribb
  in separate transactions with separate error handling: one failing keeps its
  existing rows and does not stop the other, and the job raises only when
  *both* failed — a job that always succeeded would never be retried, and one
  that failed when half worked would redo the half that did.
- **Schedule.** Weekly cron, Monday 03:30 UTC (`arc/worker.py`), plus once at
  start-up when `offline_imports` is empty: a deployment that has never
  imported would otherwise have no offline catalogue for up to seven days,
  which is exactly the window it exists to cover. `python -m arc.cli
  import-catalogue` runs it inline for an operator who does not want to wait
  (and exits 1 if *either* source failed — stricter than the job, because a
  person is watching).
- **Staleness.** `GET /api/catalogue/offline` reports the loaded versions, the
  row counts and `stale` — true when manami has never been imported or its
  import is older than `OFFLINE_CATALOGUE_STALE_DAYS` (14, two missed weekly
  runs). The admin **Storage tab** shows it: one row per source with its
  version (Fribb's ETag truncated to 12 characters), row count and relative
  age, the weekly schedule, and — when `stale` — the line that names
  `python -m arc.cli import-catalogue`.
- **What reads them** (M15.5 bullets 2 and 3, implemented): `offline/search.py`
  (`offline_search` for the search endpoint and the matcher, `offline_season`
  for the sweep), `offline/materialise.py` (offline row → `anime` row, as the
  weakest source), `offline/ids.py` (the AniList ↔ MAL map used by the cache
  upsert and by `catalog_reconcile`). manami's vocabulary is mapped on the way
  in: `ONGOING`→`RELEASING`, `UPCOMING`→`NOT_YET_RELEASED`, `UNKNOWN`→null,
  season `UNDEFINED`→null, `episodes: 0`→null, `score`×10 → `average_score`,
  `picture`→`cover_url`, `studios[0]`→`studio` (title-cased only when the
  dataset lowercased the whole name). The title goes to `title_romaji` and
  `title_english` is left null — nothing guesses which synonym is the English
  one. The TMDB enrichment the id map also exists for (bullet 4) is separate.

### 5.8 TMDB enrichment (M15.5)

AniList is the best catalogue Arc has and it is still missing things: a show
that arrived through MAL during an outage has a 230 px cover and no banner, and
even an AniList row often has no episode stills and no staff beyond the studio.
TMDB has all three. It is reached **by id only** — Arc never searches it — over
the cross-id map of §5.0a: `anime` → `offline_ids` (by AniList id, else MAL id)
→ `tmdb_tv_id` (+ `tmdb_season`) or `tmdb_movie_id`. AniList publishes no TMDB
id at all, which is why the offline import is a prerequisite for this.

- **What is filled.** `anime.backdrop_url` and `anime.banner_url` from the
  backdrop (`w1280`),
  `anime.cover_large_url` from the poster (`w780`), `episodes.still_url` from
  each episode's still (`w300`) and `episodes.title` from its name, and
  `anime.credits` from the series crew mapped onto the same six roles the
  AniList path produces (`services/catalog/credits.py`). Image sizes and the
  `https://image.tmdb.org/t/p/` base are constants, not a `/configuration`
  call: that answer has not changed in a decade and asking would be a request
  per sweep to learn a constant.
- **`backdrop_url` is TMDB's own column** (owner, 2026-09-13) and the one
  exception to the rule below: it is written whenever TMDB has a backdrop and
  the row does not already hold that same URL, so an older TMDB answer is
  replaced and a re-run of a filled row still plans nothing. Nobody else writes
  it — the AniList/MAL/offline upserts do not name it (§5.0), which is the
  whole reason it exists as a column rather than as a TMDB value in
  `banner_url`: rule 3 would keep the enrichment out of every row AniList had
  filled, and an AniList refresh would overwrite what did land with the
  4.75:1 strip no frame can show.
- **Never overwrite** (cache rule 3, one source further down). TMDB is the
  weakest source Arc has, so it fills nulls and touches nothing else.
  `cover_url` is not its column — MAL's 230 px cover stays. `credits` is
  filled where there is nothing and *completed* where a weaker source left only
  the studio row; a column `detail_source = anilist` has filled is AniList's
  outright, however short its answer. An episode title or still is written only
  where there is none, and no episode row is ever created (rule 4).
- **Which season.** `offline_ids.tmdb_season` when the series has it. Failing
  that: the season whose `air_date` year is `anime.season_year`, and among
  those the one whose `episode_count` is closest to `anime.episodes`; failing
  *that*, a series with exactly one (non-special) season whose count agrees.
  Anything else resolves to no season, and the show gets its backdrop and
  poster with no stills — a still from the wrong cour is worse than none.
- **Crew.** TMDB's job strings are an exact-match table
  (`tmdb/enrich.py::CREW_JOBS`), separate from AniList's
  (`anilist/extras.py::ROLE_CREDITS`) only because the two spell the same jobs
  differently ("Original Music Composer" vs "Music"); both match the job
  *whole*, since "Music Director" is neither the director nor the composer.
  `aggregate_credits` rather than `credits`, because its per-job
  `episode_count` is what tells the series director (38 episodes) from the
  twenty-two people credited as "Director" on two each. Two names per role.
- **Jobs and pacing.** `tmdb_enrich` (one show) and `tmdb_enrich_all` (nightly
  at 04:10 UTC, after the catalogue sweep). The sweep runs **two passes** in
  order, capped at 300 shows between them and spacing its children 2 s apart:
  1. **Watched** shows that the id map can reach and that still have a hole.
     Full enrichment: three requests, art + stills + credits. "Watched" is
     three ways in, any one of which is enough (`_worth_enriching`): a list
     entry in `FOLLOWED_STATUSES`, **any user's `watch_progress` on an episode
     of the show**, or **an episode Arc holds ready** (`episodes.state =
     ready`, or a `renditions` row with `ready_at` set). The last two were
     added on 2026-09-12: Arc plays what it holds whether or not the show was
     ever added to a list, and a show watched off-list could otherwise never
     gain a single episode still.
  2. **This season's and next season's** shows (`catalog/seasons.py`) that the
     id map can reach and that are missing any of the three art columns, most
     popular first (`popularity DESC NULLS LAST`). **Art only**: one
     `/tv/{id}`, backdrop + poster, no season and no credits call —
     `{"anime_id": N, "art_only": true}` in the payload, honoured by `_fetch`
     and by `plan_enrichment(..., art_only=True)`.

  Pass 2 exists because the Home hero offers shows the viewer does *not*
  follow, so under the followed-only rule nothing it showed could ever be
  enriched (owner, 2026-09-12). The client paces at 4 req/s and shares one
  process-wide breaker, so a 429 or a 5xx stops the rest of the night instead
  of timing out three hundred times.
- **What counts as a hole.** A mapped row missing *any* of `backdrop_url`,
  `banner_url` or `cover_large_url` (`_missing_key_art`), or with an aired
  episode that has no still, or — in the nightly sweep only — with no credits
  beyond the studio row. Adding `backdrop_url` to that list is what fills the
  rows that predate the column: the first night after the migration queues up
  to 300 of them and the rest follow on the nights after (owner, 2026-09-13).
- **On demand.** `catalog_refresh` queues one enrichment when it leaves a
  followed show short of any of the three art columns, or with an aired episode
  that has no still. `GET /api/home` queues two kinds, in this order and
  waiting for neither:
  1. **Full** enrichments (`enqueue_episode_stills`, up to `STILL_LIMIT` = 8)
     for the shows on the page's 16:9 shelves — Continue watching, then Ready
     to watch, then the week's episodes — whose card episode has no
     `still_url` and which the id map can reach. (The third is no longer a
     16:9 card since the This-week shelf became a broadcast time and a 2:3
     thumb, but it is still the week's episodes, which are the ones a still is
     most likely to be missing for; the order is the page's, so the eight the
     limit allows go to the cards nearest the top.) The nightly sweep reaches these shows too, but "tonight" is the
     wrong answer for the card somebody is looking at now (owner, 2026-09-12).
  2. **Art-only** enrichments for up to 12 shows of this or next season with
     no `backdrop_url` (`_missing_hero_art`) — the pool the client's hero picks
     its six slides from, ranked the same way. Until 2026-09-13 the test was
     "no artwork at all", which this subsumes: a row with nothing has no
     backdrop either, and the rows it adds are the ones the hero could only
     ever wash (an AniList strip and a poster).

  And `POST /api/anime/{id}/sample` queues a **full** enrichment for that one
  show (`enqueue_show_enrichment`, FR-A8, owner 2026-09-13): the episode it is
  about to fetch is a 16:9 card, and before this its still arrived with the
  nightly sweep — or whenever the viewer next happened to open Watch Now, since
  the Home shelves were the only thing that asked. The show page never did. The
  single-row form of the same query: the id map must reach the show and a hole
  must remain (missing art in any of the three columns, or an aired episode
  with no still — credits are the sweep's business, since nobody presses a
  button for a staff list), and it never fails the sample, which does not wait
  on it.

  **And `GET /api/anime/{id}` now does the same** (owner, 2026-09-13 — the
  fourth trigger, and the one that closes the hole: a series nobody follows,
  that no shelf carries and nobody has sampled, was reached by *none* of the
  three above, so its episode rows kept the striped placeholder until the sweep
  happened to get to it). The route first asks whether the offline id map
  reaches the show at all (`tmdb_ids_for`, the row-level twin of `_mapped`) —
  one SELECT, and the answer goes on the response as `tmdb_mapped` — and calls
  `enqueue_show_enrichment` only when it does, so an unmapped page costs one
  query and a page whose art is complete costs two and queues nothing. The job
  row is committed with the refreshed catalogue row, in the commit the route
  already makes; nothing waits on the fetch, and the page renders the art it
  already holds.

  The client closes the loop on `tmdb_mapped` (`useAnime`, `Show.tsx`): while a
  **mapped** show has an aired episode with no `still_url`, the show page
  re-asks every 5 s for 30 s (`STILL_POLL_MS`, `STILL_POLL_WINDOW_MS` — six
  tries), which is how the stills the open just queued appear without a reload;
  whichever of that and the acquisition poll (FR-A7) wants the sooner answer
  wins. It never polls an unmapped show — TMDB cannot answer for one.

  There is **no caption** about missing pictures any more (owner, 2026-09-17).
  A row or card with no `still_url` falls back to the show's `backdrop_url`
  (16:9 by construction, so it fills the slot untouched) and then to the key
  visual framed inside it, so a show the id map cannot reach — One-Room TA,
  AniList 205068 — carries its own artwork down the list instead of fourteen
  stripes under a sentence explaining them. `tmdb_mapped` stays on the response
  for the polling rule above, which is the only thing that still reads it.
  The two surfaces differ in the *frame*, not in the chain: Home's 16:9 tiles
  keep `PosterWash`'s blurred ground (a shelf is eight tiles), and the show
  page's rows pass `ground={null}` for a plain letterboxed poster (a page is
  fifty rows, and fifty 40 px blurs buy a ground nobody looks at).

  Each is one SELECT that also filters out anything already queued, and
  returns nothing once the artwork is in. Everything deduplicates on
  `tmdb_enrich:<anime_id>`, so a show refreshed hourly (or a home page opened
  every ten minutes) does not queue an enrichment an hour; where two callers
  want the same show the richer job is the one that stands, which is why the
  watched pass runs before the season pass and the stills before the hero's
  art.
- **No key, no feature.** The on-demand enqueues above ask first
  (`tmdb_configured`, owner 2026-09-13) and queue nothing without a key, since
  the handler would only log a skip and on a keyless deployment every row stays
  a hole — a page load would otherwise write twenty job rows, and the next one
  twenty more. Both handlers log one INFO line and return when
  `TMDB_API_KEY` is unset; `config_check` says so once at startup in prod
  (warning, not error). `GET /api/health` publishes `tmdb_enabled`, and the
  client shows TMDB's required attribution line under the Home shelves only
  when it is true.

### 5.9 Live updates (M16)

The site updates itself when episodes change state: a new "Ready to watch" tile
on Watch Now, a row flipping to Ready or Downloading on a show page, a still
arriving. No notifications and no sound — the page just stays true. Polling
(§5.8, FR-A7) stays exactly as it is, as the fallback.

- **Source: Postgres `LISTEN`/`NOTIFY`.** The write happens in the worker and
  the tab is talking to the api (§2), so an in-process bus cannot carry it and
  the one thing both processes already hold a connection to is the database.
  `arc/services/events.py` publishes on one channel, `arc_events`.
- **Published from the write, sent by the commit.** Two publishers, both the
  single writer of the thing they announce: `transition()` (every episode state
  change, §5.1a) and `apply_enrichment()` (artwork landing, §5.8, and only when
  it actually wrote something). Neither sends anything itself — `publish()`
  *stages* the payload on the session's `info`, and a `before_commit` listener
  on SQLAlchemy's `Session` turns the staged list into `pg_notify` calls
  **inside the committing transaction**. Postgres delivers a notification only
  if that transaction commits, so a failed job or a rolled-back handler cannot
  tell a browser about a row that does not exist. Staging is also what lets
  `transition()` stay synchronous: it holds an ORM object, and
  `object_session()` is enough to reach the transaction it belongs to. The
  `pg_notify` goes through `session.connection().execute` rather than
  `session.execute`, so it cannot autoflush and turn a caller's ordinary
  `IntegrityError` into a `PendingRollbackError` raised from a notification. A
  no-op transition publishes nothing; a failure to `pg_notify` is logged and
  swallowed, because taking down the commit that persisted a transcode for the
  sake of a notification is the wrong trade.
- **A rollback drops the stage — a *savepoint* rollback does not.** One
  listener, on `after_soft_rollback`, guarded by `session.in_transaction()`.
  Both rollback events also fire for `begin_nested()`, which
  `catalog/cache.py` uses to swallow a racing insert and carry on: there the
  outer transaction is alive and about to commit, so clearing the stage would
  throw away events the surrounding work is going to make true. The soft hook
  is the one used because it fires for a rollback that emitted no SQL at all
  *and* fires last — after the unwinding, which is the only point at which
  `in_transaction()` separates the two cases.
- **Payload: ids, and nothing else.**
  `{"kind": "episode_state" | "art", "anime_id": N, "episode_id": N | null,
  "state": "…", "ts": iso}` — about 120 bytes, capped at 7,900
  (`MAX_PAYLOAD_BYTES`; Postgres refuses 8,000). A notification is broadcast to
  every listener and read by every signed-in tab, so it carries no titles, no
  paths and no user ids. The client re-asks the endpoints it already had, with
  its own session, and the server answers as it always did.
- **Fan-out: `GET /api/events`**, server-sent events, `CurrentUser` like every
  other route. One `EventBroker` per api process owns one dedicated asyncpg
  connection (the same DSN as the engine, driver name dropped), `LISTEN`s once
  and pushes each payload into a queue per open stream. Built lazily by the
  first stream and closed by the lifespan, so a process nobody streams from
  holds no connection and `uvicorn --reload` cannot leave a listener behind.
  The connection is supervised and re-opened with backoff (1 s → 30 s) when it
  drops; while it is down the streams stay open and silent and polling covers
  the gap. There is **no per-user filtering**: which rows matter to which
  viewer is a question the client answers out of its own cache, and asking it
  here would be a query per event per stream.
- **A stream holds no database connection.** The route asks for its session
  explicitly and `await session.close()`s it before returning the response.
  FastAPI tears a dependency stack down only once the response body is
  finished, and a stream's body finishes when the tab does — so the session
  `CurrentUser` was resolved on, left `idle in transaction` by
  `resolve_session`'s read, would otherwise stay checked out of the pool for as
  long as the tab is open, and a dozen tabs would stall the API. Nothing
  downstream needs it: the user is reduced to an id for one log line and the
  stream talks only to the broker. A pg test asserts against `pg_stat_activity`
  that an open stream holds no unfinished transaction.
- **Limits.** 100 concurrent streams per process (`MAX_STREAMS`), 503 `too many
  live connections` above it — checked in the handler, which is the only place
  a status code is still available, while the queue itself is taken inside the
  response generator so that a client disconnecting before the first read
  cannot leak one against the cap. 200 queued events per stream, and above that
  the **oldest** is dropped with a log line: every payload means "ask again", so
  the newest subsumes the ones in front of it and a stream whose last word is
  the most out of date is the one case where a live update is worse than none.
  A comment line (`: ping`) every 25 s so a proxy does not close an idle
  connection; `Cache-Control: no-cache, no-store, must-revalidate` and
  `X-Accel-Buffering: no`. A frame containing a newline is dropped — it could
  only come from something else `NOTIFY`ing the channel, and a newline is a
  frame boundary. The listener's supervisor catches `Exception`, not a named
  list of driver errors: a half-closed socket raises `asyncpg.InterfaceError`
  or `InternalClientError`, neither of which is an `OSError` or a
  `PostgresError`, and an unlisted exception must mean "reconnect", never "stop
  listening until the next deploy".
- **Client: `lib/events.ts`.** `useLiveEvents()` is mounted once, in
  `components/Layout.tsx` — the one component mounted exactly once for the
  signed-in app — and opens one `EventSource('/api/events')` per tab.
  `EventSource`'s own retry is the reconnect. It **invalidates, never
  patches**: `episode_state` marks `HOME_QUERY_KEY`, the show's detail query
  and (for an admin, the only one who can see it) the acquisition status stale;
  `art` marks the show's detail query stale; an unknown kind marks nothing, so
  a third kind does not make an old client refetch the world. Keys collect for
  300 ms and go out once, because a `compute_wants` tick writes a dozen state
  changes and a nightly enrichment hundreds; whatever is still collecting is
  flushed rather than dropped when the stream pauses or the shell unmounts. A
  hidden tab keeps its stream for 60 s and then closes it — a browser with
  forty Arc tabs must not hold forty of a hundred streams, and the timer is
  also started at mount for a tab that was *already* hidden, since
  `visibilitychange` never fires for a state that was already true — and
  coming back re-opens it and invalidates once:
  Watch Now plus the show *details* by predicate, never the searches that share
  the `anime` key prefix. No `EventSource` in the environment (jsdom, an old
  browser) means the hook does nothing and the intervals are all there is.

## 5b. API surface (kept current)

All routes require a session unless marked public. Errors are JSON `{"detail"}`.
Mutating requests must carry an allowed `Origin`.

| Route | Who | Purpose |
|---|---|---|
| `GET /api/health` | public | liveness |
| `GET /api/events` | any | the live event stream (M16, §5.9): `text/event-stream`, a comment line on open and every 25 s, `data:` frames of `{kind: episode_state\|art, anime_id, episode_id, state, ts}` — ids only, no user data, no per-user filtering. `Cache-Control: no-cache, no-store, must-revalidate` and `X-Accel-Buffering: no`; Caddy proxies it with `flush_interval -1` and excludes it from `encode` (§8). 503 `too many live connections` above 100 streams per api process. Nothing depends on it: every page that reacts to an event also polls |
| `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` | public / any | session |
| `POST /api/invites`, `GET /api/invites`, `DELETE /api/invites/{id}` | admin | invite management |
| `GET /api/invites/{token}`, `POST /api/invites/{token}/accept` | public (rate-limited) | invite flow |
| `GET /api/users`, `PATCH /api/users/{id}` | admin | user management (zero-admin guard) |
| `PATCH /api/users/me` | any | change own timezone (IANA, validated) |
| `POST /api/jobs`, `GET /api/jobs`, `GET /api/jobs/{id}` | admin | job queue; the listing takes `status=`, `type=`, `limit=` (≤ 200), `offset=`, newest id first |
| `GET /api/jobs/summary` | admin | `{by_status: {pending, running, done, failed, cancelled}, by_type_pending: {type: count}, worker: {heartbeat_at, alive}}` — `alive` is the heartbeat file younger than 90 s, the same decision the container healthcheck makes |
| `POST /api/jobs/{id}/retry`, `POST /api/jobs/{id}/cancel` | admin | 200 `JobOut`. Retry: `failed`/`cancelled` → `pending`, `attempts` 0, `run_after` now, lock and finish timestamps cleared, `last_error` kept. Cancel: `pending` → `cancelled`. 409 for any other status (a `running` job cannot be stopped safely), 404 unknown |
| `GET /api/settings`, `PUT /api/settings` | admin | the rules editor (FR-D2, FR-T5): `{values, defaults, overrides[{anime_id, title, preferred_groups, resolution}]}` for every `DEFAULT_SETTINGS` key. PUT takes a partial object of those keys and writes only what it names; 422 `{detail: [{loc: ["body", key], msg, type}]}` for an unknown key or a refused value (resolutions ∈ 2160p/1080p/720p/480p and fallback ≠ preferred — enforced only when the patch names one of the two, so a hand-edited collision does not block unrelated edits — N 0..10, K 0..50, G and D 0..365, ≤ 20 groups of ≤ 64 chars de-duplicated case-insensitively, languages 2–8 lowercase letters/dashes). Writing `acquisition_paused` goes through the same `set_paused` the pause button uses, and clearing it enqueues `compute_wants` in the same transaction, so unpausing from the editor and from the button do the same thing. Overrides are read-only here; editing them is M16. `min_free_gb` is FR-T6's storage floor: whole GB, 0..1000, 0 turning the guard off; validated and clamped at the same ceiling the reader uses, so what the panel accepts and what acquisition acts on cannot differ. `slot_cap_k` is FR-A10's K: 0..50, 0 meaning no cap, clamped at the same ceiling for the same reason |
| `GET /api/anime/search?q=&page=` | any | **local hits first, then the live search** (§5.0): cached rows matching every word of `q` in a title or synonym, ordered exact/prefix title → followed → popularity, capped at 20 and merged on `page=1` only; the live results follow, de-duplicated by internal id, and are cached. `page`/`has_next` describe the live half. Local hits with the catalogue down is a 200; 502 only when the local half is empty *and* upstream failed. Each `AnimeSummary` carries `cover_large_url` (nullable) beside `cover_url`, so a card prefers the sharp key art and falls back, plus `popularity` and `average_score` (both nullable; real summary columns, straight off the search — `average_score` is 0–100 whichever source answered, MAL's 0–10 scaled on the way in), plus `genres[]`, `banner_url`, `backdrop_url` (TMDB's 16:9 backdrop, which the heroes and 16:9 cards prefer — §5.8) and `studio` — **read off the cached row, not the search payload**: AniList's search fragment does not carry them, so they are empty/null on a show no detail fetch has reached and fill in the moment one does. They are deliberately *not* in the summary write path; a search payload's empty genres would otherwise blank them (cache rule 2) |
| `GET /api/catalog/status` | admin | source health and breaker state |
| `GET /api/catalogue/offline` | admin | offline-catalogue import status (M15.5, §5.0a): `{sources: [{source, version, imported_at, rows, checksum}] (newest first), stale, anime_rows, id_rows}`. `stale` is manami's alone — the id map without the titles is not a catalogue — and is true when it has never been imported or is older than `OFFLINE_CATALOGUE_STALE_DAYS`. Reads three counts and nothing else; the import itself is a job |
| `GET /api/mal/status`, `POST /api/mal/link`, `GET /api/mal/callback`, `DELETE /api/mal/link`, `POST /api/mal/import`, `POST /api/mal/push`, `GET /api/mal/log`, `POST /api/mal/log/{id}/revert` | any (own account) | MAL link, import, push pending, write log, revert. The revert writes `updated_by=arc`, `mal_dirty=true` and stamps `activated_at` if null (FR-A9: it is FR-M7's third user-originated event), on the entry it recreates as well as the one it edits |
| `GET /api/recs`, `POST /api/recs/runs` | any (own runs) | recommendations (FR-R1…FR-R5): GET returns `{run, remaining_today, limit_per_day, configured}` with the newest run (`RecRunOut` = `{id, prompt, created_at, model, candidate_count, picks[{anime, case}], continuations[{anime, because}]}`), plus `chain: [{provider, model, available}]` **for admins only** (the field is absent for everyone else); POST `{prompt}` (trimmed, ≤ 300 chars) creates one → 201 `RecRunOut` `{id, prompt, created_at, model, candidate_count, picks[{anime, case}], continuations[{anime, because}]}`. 429 `{detail, retry_after_seconds}` + `Retry-After` at 10 runs/24 h; 503 unconfigured or refused; 502 upstream; 409 empty pool |
| `GET /api/anime/{id}` | any | (internal id) `AnimeDetail` + `anilist_id`, `mal_id`, `source`; `list_entry.mal_sync` state and, on this endpoint only, `list_entry.waiting` / `waiting_reason` (`slot` | `paused` | `held`) / `fetching_count` / `slot_cap` — FR-A10's picture of the caller, from `wants.slot_view(session, user_id, settings=…)`, computed only when the entry is watching/planned and stored nowhere; `relations[]` carry `id` (internal, null when Arc has no row yet) plus `anilist_id`/`mal_id`, and — for the relations Arc *has* cached — `cover_url`, `cover_large_url`, `episodes`, `season_year` for M15's franchise rail (all null when the row is not cached; nothing is fetched to fill them, and `format` comes from the stored relation blob so it is present either way). Resolved in one query over named columns, not one per relation; episodes carry `air_at_estimated`: summary + synopsis, genres, studio, `credits[]` (`{role, name}`, studio first — M15's "Made by" block; one row long on a MAL-sourced show), `cover_large_url` (nullable key art), `banner_url` and `backdrop_url` (the hero prefers the second), `tmdb_mapped` (whether the offline cross-id map reaches this show on TMDB — §5.8: not a promise of pictures, but what separates "the stills are on their way", since opening the page queues the enrichment, from "there are none to come"; since 2026-09-17 it is read only by the client's still-poll gate — the episode rows no longer say anything out loud, they fall back to the show's backdrop and then its key visual), relations, `next_airing`, `list_entry`, `episode_count`, `episodes[]` (id, number, title, `still_url` (nullable), air_at, aired, state, `watched` and `watched_source` — FR-W5: watched is `number <= list_entry.progress` **or** a completed `watch_progress` row of the caller's, and the source says which (`arc` | `progress` | null) so the client offers "Unwatch" only where there is a row to clear), plus `search` = `{at, forms, results, next_at} | null` — FR-A7's search summary (2026-09-14), sent only while the episode is `wanted`/`searching`/`unavailable` and only once a search has run, with `next_at` read from the pending `search_release` job's `run_after` in the same per-page pass as the torrents, renditions and transcode jobs (`api/episode_extras.py`, one query for the whole list) because FR-A6's retry schedule is a job row and not a column), and `sample` = the caller's own live "try episode 1" want (`{episode_id, episode_number, requested_at, state}`) or null (FR-A8) |
| `POST /api/anime/{id}/refresh` | admin | enqueue `anilist_refresh` |
| `POST /api/anime/{id}/sample` | any | "try episode 1" (FR-A8): wants the show's lowest-numbered episode as a sample, with **no list change and no MAL write** → 202 `SampleOut` = `{episode_id, episode_number, requested_at, state}` — `state` is the episode's state *after* the call, because the route starts the search itself through the reconciler's shared `start_search()` (with the `UNAVAILABLE_RETRY` gate skipped) and also enqueues `compute_wants` for everything else. It queues the show's **TMDB enrichment** too (§5.8), so the episode's still arrives with the episode rather than with the nightly sweep. A **dormant** watching/planned entry (FR-A9) is no longer refused — it has no window, so "the next episodes are fetched automatically" would be false — and no entry of any status is activated by a sample: one episode is what was asked for. Idempotent: with a sample already live it answers that one and writes nothing, and pressing it after a stale drop (FR-T2) clears that drop. 404 unknown anime; 409 with the reason as plain English — "this show has no episodes yet", "episode 1 has not aired yet", "you are already following this show; the next episodes are fetched automatically" |
| `DELETE /api/anime/{id}/sample` | any | cancel it: every live sample want of the caller on this show is **dropped** (`sample cancelled`) rather than deleted, so retention keeps its grace anchor, and the route releases the episode to `not_wanted` through the reconciler's shared `release_if_unwanted()` unless somebody else still wants it (`compute_wants` is enqueued as well). 204; 404 when there is no live sample (a second press, or one FR-T2 already closed) |
| `PUT /api/list/{anime_id}`, `DELETE /api/list/{anime_id}`, `GET /api/list?status=` | any | list states; PUT sets `updated_by=arc`, `mal_dirty=true` and **stamps `activated_at` if it is null** — the PUT *is* FR-A9's touch, including one that re-sends the status the show already has, which is what the Show page's "Fetch this show" button sends; `completed` sets progress to episode count; `score: null` clears. Rows are `{anime: AnimeSummary, entry}`, so each carries `cover_large_url`, `genres[]`, `banner_url`, `backdrop_url` and `studio` (M15: My List credits the studio per row). Every `ListEntryOut` carries `activated_at` and a derived `dormant: bool` (null stamp **and** the show not `RELEASING`), which is what the Show page's note and My List's "imported" badge read |
| `GET /api/schedule?year=&season=` | any | cache-only season grid: 7 days (0 = Monday in the user's timezone), entries with local time, next episode, `following`; movies/OVAs/specials/music and rows with no known air time in `unscheduled`; `prev`/`next` season refs. Each entry also carries `watched` (`bool | null`) — FR-W5 for the episode `next_episode` names, and **only** where `next_at` is already past (`api/schedule.watched_marks`, owner 2026-09-17): an upcoming slot sends null, so Watch Now's appointment card cannot draw a tick beside a broadcast that has not happened. Three extra queries, and only when at least one slot has aired. `prev`/`next` season refs. The **current** season's grid also holds every `RELEASING` weekly-format show with an air time inside 7 days, whatever season it is tagged with (a two-cour show, a long-runner) — flagged `carried_over` so the card can name the season it started in; a prev/next view is exactly the shows of that season and carries none (§5.0) |
| `GET /api/home` | any | `continue_watching` (started > 10 s, not completed, episode ready, newest first, max 20), `ready_to_watch` (episodes in state `ready` on a watching/planned/**on-hold** list, with no `watch_progress` row past `RESUME_MIN_S` = 10 s and none completed, and a number above the entry's progress — FR-W5 read backwards; ordered by `renditions.ready_at DESC NULLS LAST, episodes.id DESC`, max `READY_LIMIT` = 20; **any air date**, which is the point: it was the client filtering `new_this_week` until 2026-09-17, so a ready episode of a show that stopped airing a fortnight ago could not reach the page), `behind` (watching shows with aired episodes above progress, newest first), `new_this_week` (episodes of watching/planned shows aired in the last 7 days, max 50). Every row's `EpisodeOut` carries FR-W5's `watched`/`watched_source`, from one extra `list_progress_for` query over the page's shows plus the completions of the `new_this_week` episodes — which is why an imported list with no completion rows still ticks (the This-week shelf shows that tick and no acquisition state at all). Every row embeds an `AnimeSummary` and an `EpisodeOut`, so the hero's `backdrop_url`/`banner_url`, the shelves' `studio`/`genres[]`/`cover_large_url` and the Up Next tiles' `still_url` all arrive in this one call (M15). Every visit also queues, cheaply and deduped, the art the page found missing: a full enrichment for the shelf cards with no still and an art-only one for the season shows with no `backdrop_url` (§5.8) |
| `POST /api/catalog/season-sweep` | admin | enqueue the season pre-cache now (deduped) |
| `GET /api/review?state=&limit=`, `GET /api/review/summary` | any | match-review queue: files below the auto-link threshold with top candidates and reasons; paths relative to `DATA_DIR`, never absolute. Each item may carry `suggestion` = `{anime_id, anime, episode_number, reason, confidence: high\|medium\|low, model, created_at, error}` (FR-L5; when `error` is set the rest may be null and the client shows "no suggestion: &lt;error&gt;"). The page carries `suggestions_enabled` = `LLM_MATCH_SUGGESTIONS` **and** a configured provider chain |
| `POST /api/review/{id}/confirm`, `…/ignore`, `…/reopen`, `GET …/search?q=` | any | resolve a file: link to (anime, episode) creating the episode row if needed; ignore; reopen an ignored one; search the catalogue for another title. Confirm is unaffected by any suggestion — it reads only its body |
| `POST /api/review/{id}/suggest` | any | ask a model which candidate this file is (FR-L5) → 202 `{job_id, status: "pending"}`, enqueuing `llm_suggest_match` with `force` (deduped per file). 404 unknown; 409 unless the file is `pending`; 503 `Suggestions are not enabled` when the flag is off or no provider is configured. **Never links anything** — the answer is stored for the queue to show |
| `GET`/`HEAD /media/{id}/index.m3u8`, `/media/{id}/{init.mp4\|seg_NNNNN.m4s}` | any (session cookie) | HLS delivery from `DATA_DIR/renditions/<id>/`; name validated by regex, path built from the id; 404 unless the episode is `ready`; playlist `no-cache`, init/segments `immutable` + ETag/304; Range → 206/416 (Starlette native) |
| `GET /api/episodes/{id}/play` | any | `PlayInfo`: episode (the same `EpisodeOut` the show page renders, so `title`, `still_url` and FR-W5's `watched`/`watched_source` come with it), anime (an `AnimeSummary`, so `cover_large_url` too), playlist URL, rendition duration, `resume_position` (10 s < pos < 95 %, not completed), previous/next refs with `ready` |
| `POST /api/progress` | any | upsert watch progress (also accepts `text/plain` beacons; Origin still required); ≥ 90 % → completed (sticky, `completed_at` once); **every** report stamps `activated_at` on an existing entry if it is null (FR-A9: Play is a touch, from the first report rather than the one that crosses 90 %; it never creates an entry); newly completed → list progress raised if higher (`updated_by=arc`, `mal_dirty=true`; a Watching entry is created if none, activated), the entry auto-completed when that advance reaches the episode count of a FINISHED show (FR-W5), then `compute_wants` enqueued |
| `POST`/`DELETE /api/episodes/{id}/watched` | any | manual mark / un-mark (FR-W3, FR-W5). POST is FR-S4's own path with `force_complete`: it writes the episode's completion row, raises `list_entries.progress` to its number **if lower** (`updated_by=arc`, `mal_dirty`, one `progress` write log row with cause `watch`, never lowering), writes **no** rows for the episodes below it, and auto-completes the entry when the advance reaches the episode count of a FINISHED show, whatever status it had (a second `status` row, same push). DELETE clears the completion row and its `completed_at`, keeps the position, and — when `list_entries.progress` **equals** this episode's number — lowers it to N−1 with `updated_by=arc`, `mal_dirty`, `activated_at` stamped, a queued `compute_wants` and one `progress` write log row with cause **`manual`** carrying the previous value: the only lowering progress write Arc sends, and only because a person pressed it (owner, 2026-09-13, superseding the 2026-09-07 clarification). Above the progress it clears the row alone; below it nothing moves. The status is never rolled back. Never creates a list entry. Answers `ProgressOut` with `list_progress` set when the number moved |
| `POST /api/episodes/{id}/transcode?force=` | admin | enqueue a transcode: retry a `failed`/`matched` episode, or re-encode a `ready` one with `force=true` (409 otherwise) |
| `GET /api/retention/preview`, `POST /api/retention/sweep`, `POST /api/episodes/{id}/delete-files` | admin | what the next sweep would delete (reasons, bytes); run it now; delete one episode's files (404 unknown, 409 while in flight) |
| `GET /api/retention/disk` | admin | `{data_dir: {total, used, free}, retained: {sources, renditions, total}, episodes_retained}` — `shutil.disk_usage` on `DATA_DIR` (the nearest existing parent when it has not been created yet; the GET never creates it) beside Arc's own share, from the same `retained_usage` the acquisition status reports |
| `POST /api/acquisition/pause`, `POST /api/acquisition/resume`, `GET /api/acquisition/status` | admin | pause/resume acquisition (settings key `acquisition_paused`; while paused `compute_wants` does nothing and `search_release` requeues itself without touching Nyaa or qBittorrent; `poll_qbit` keeps ingesting); status shows `paused`, active wants, searching, downloading, `retained_bytes`, plus (2026-09-13) `storage_held`/`free_bytes`/`min_free_bytes` — FR-T6's guard, read from the same measurement and the same `storage_hold` rule the guard itself uses, with an unmeasurable path reading as zeros and *not* held — `dormant_entries`, the count of `watching`/`planned` entries with `activated_at IS NULL` whose show is not `RELEASING` (FR-A9), and `waiting_shows`/`slot_cap_k` — FR-A10's cap, the (user, show) pairs it is holding back right now and K itself, counted by the reconciler's own `wants.slot_totals(session)` (one pass over every list, K returned with the count) so the panel cannot disagree with the next tick. No upstream call |
| `POST /api/episodes/{id}/search`, `POST /api/acquisition/compute-wants`, `POST /api/acquisition/poll`, `GET /api/acquisition/wants` | admin | trigger a release search / the wants reconciler / a qBittorrent poll (all deduped, 202); list active wants for debugging |
| `GET /api/acquisition/qbit` | admin | `{reachable, version, error, torrents[{hash, name, state, progress, size, dlspeed, upspeed, episode_id}]}`. The only route that calls qBittorrent inside a request (`app/version` + `torrents/info?category=arc`, read-only, `asyncio.timeout` bounding the **whole probe** at 5 s — the client logs in and retries a 403 once, so a per-request budget would be six of them) and the only one that **never fails**: down, wrong password or unconfigured is `reachable: false` with the reason in `error`, because that is the answer the admin came for. `episode_id` comes from Arc's `torrents` rows, not from the client's tags |

## 6. External integrations

| Service | Auth | Rate/limits | Notes |
|---|---|---|---|
| AniList GraphQL `https://graphql.anilist.co` (`ANILIST_URL`, overridable for tests) | none | documented 90 req/min, enforced ~30/min; client paces requests (`ANILIST_MIN_INTERVAL_MS`, default 700), honours `X-RateLimit-Remaining`/`Retry-After`, retries 429 once and 5xx twice | Queries: `SEARCH` (summary fields only; upsert never sets `refreshed_at`), `MEDIA_BY_ID` (full detail + first aired and upcoming schedule pages + relations + studio + `staff(sort: RELEVANCE, perPage: 12)` and `streamingEpisodes` for M15's credits and episode stills — detail-only, so a search page and a season sweep never pay for them), then `AIRED_SCHEDULE_PAGE` follow-ups while `hasNextPage` (cap 20 pages / 2000 episodes, logged if hit). A 429 without `Retry-After` waits 3 s (60 s is only the ceiling for a sent header). **Interactive calls do not wait on 429**: the app's catalogue is built with `wait_on_rate_limit=False` (`create_catalog`), so a 429 on a search or a show page raises `SourceRateLimited` at once and falls straight through to MAL/offline instead of holding the request open — and, being a burst limit rather than an outage, it leaves the breaker closed, only noting a per-client "rate-limited until" so the next interactive call inside the window skips AniList without a request. The worker's `catalog_for()` keeps the wait. Detail is served from cache when `refreshed_at` < 24 h; unreachable AniList with nothing cached → 502 `anilist is unavailable`. |
| MAL API v2 `https://api.myanimelist.net/v2` | reads: `X-MAL-CLIENT-ID` header only; writes (M9): OAuth 2.0 PKCE (plain), client id + secret in env | modest | Catalogue fallback (read): `anime?q=`, `anime/{id}?fields=…`, `anime/season/{year}/{season}`; broadcast weekday/time used to synthesise episode air dates. List sync (M9): `users/@me/animelist`, `anime/{id}/my_list_status` (PATCH/DELETE). Tokens encrypted with Fernet key from env. |
| Nyaa RSS `https://nyaa.si/?page=rss&q=…&c=1_2&f=0` (`NYAA_URL`) | none | ≤1 req/2 s (asyncio-paced), 10-min cache per query, 20 s timeout, one retry | `c=1_2` = Anime English-translated. Up to **10** query forms per episode, in order: romaji full title, english full title, then the **`SxxEyy` form of both titles** — `<season-stripped base> S<kk>E<nn>`, the season the entry's own title names or 1, the episode padded to two digits (so `S01E1089`, never `S01E089`) — then (only for an entry whose own title names a season) the season-stripped base with `S<k>`, roman numeral and plain, then — only where `absolute_offset` answered **and** the season marker left a shorter base behind — the **absolute pair** `<season-stripped base> - <N+offset>` and the dashless `<base> <N+offset>` (§5.1a: SubsPlease numbered *Jujutsu Kaisen* season two 25–47, and it writes the romaji franchise name, which is why the pair sits ahead of every english form; an unmarked sequel whose base *is* its whole title gets neither form, since nine words plus a running number is a query with no answer, and only a **finished** prequel chain produces an offset at all), then the **head of the romaji title and the head of the english title** — the text in front of the first subtitle separator (`:`, ` - `, ` – `, ` — `, `~`, `〜`, counted only with whitespace on one side so `Re:Zero` stays whole), derived from the season-stripped title so a marked entry's head repeats a short form and dedupes away — then the bare `<romaji> <NN>` for an unmarked entry, then the **english** `SxxEyy` form and the english short forms, then the **symbol-stripped variants of the two full titles**, and finally **up to two synonyms** (`anime.synonyms`, season-stripped, kept only when neither the synonym nor its own head repeats a title or a head already asked for — so *Mushoku Tensei: Isekai Ittara Honki Dasu 3rd Season* earns nothing — and only when it is a *name*: two words or six characters, never a bare season marker, since the list is somebody else's free-text field and holds entries like `"Season 2"` and `"2"`). **The order is what the cap cuts**, which is the whole of its design (2026-09-14): every romaji form comes before every english one and the speculative forms come last, so a marked, subtitled title — *Kimetsu no Yaiba: Katanakaji no Sato-hen 2nd Season*, fourteen forms for ten slots — keeps all four of its romaji short forms and loses a variant instead. The **symbol-stripped variant** (`☆ ★ ♪ ♥ ! ? : ; ~ 〜 ～ · ・ — /` each become a space, the runs collapse) turns `Yarichin☆Bitch-bu - 01` into `Yarichin Bitch-bu - 01`, `Love Live! Superstar!! - 03` into `Love Live Superstar - 03` and `Fate/Zero - 12` into `Fate Zero - 12`, because Nyaa matches tokens and a symbol glued between two words makes one token out of both; it is built for the **full titles only** — a variant of an abbreviation is a guess about a guess — and the ordinary hyphen is deliberately not in the set, since it is what separates the number from the title. The set is a short, evidenced subset of the parser's own punctuation class (`_PUNCT_RE` flattens everything, because both sides of a comparison go through it and cost nothing; each character here costs a request). **A film, or an OVA/ONA the catalogue gives one episode** (`nyaa.is_single`), is a different and shorter list (2026-09-14): the **bare titles** and their symbol-stripped variants and synonyms, with no number attached to any of them — nothing on Nyaa writes `Servamp Movie: Alice in the Garden - 01`, which is why *that* film, *The Royal Tutor Movie* and *SAO the Movie: Progressive* all sat in `searching` for a day. **Head forms are not asked for an entry with a `PREQUEL` relation** (`anime.relations[].relation_type`, matched case-insensitively; no relations stored is not evidence and keeps them), because a release named by the bare head is most likely the first season and an unmarked sequel cannot be told apart from it by season agreement. ALL run and merged by info hash before ranking, because Nyaa ANDs every word and groups name shows by neither the whole title nor the same language: `Mushoku Tensei III: Isekai…` vs `Mushoku Tensei S3`, and `Rakudai Kenja no Gakuin Musou: Nidome no Tensei, S-Rank Cheat Majutsushi Bouken-roku` vs `Rakudai Kenja no Gakuin Musou`. The `SxxEyy` forms are third and fourth because a show whose groups name it that way has *nothing* under the dash forms (`One-Room TA - 01` → 0 results, `One-Room TA S01E01` → the seven ToonsHub singles), and they need no `PREQUEL` gate: built from the base rather than the head, a subtitled sequel is asked for by its whole name, and where the base is bare the form carries the season explicitly. Ten is the ceiling (ten paced requests, 2 s apart — it rose 8 → 10 for the symbol variants, which sit behind their own form and would otherwise push a marked entry's english short forms off the end); the dedupe keeps a typical show at three or four. The broad head form is safe only because of the filter below — never weaken it. Items parsed with the same filename parser; kept only when kind=episode, episode number equal — **or equal to `N + offset` on a release that names no season at all**, the absolute rule of §5.1a, which is then dropped outright if any season-marked candidate for the same episode also survived — title ≥ 0.90 similar (asymmetric: a release title that *extends* the entry's title with tokens not in any of the entry's own titles is a different show, e.g. a subtitled sequel), season agrees (1 assumed when unmarked on either side), not a remake, **at least one seeder** (`MIN_SEEDERS` = 1; zero is not a worse candidate but a file that cannot be fetched, and taking one used to put a magnet into qBittorrent that sat in `metaDL` for hours), and **the info hash has no `torrents` row at all** — any episode, any state, because a row means Arc already tried that release and it did not produce the episode (§5.1a). **A batch is never picked** (FR-A4): a release whose name carries an episode range (`01 ~ 12`, `01-02`, `E01-E12`) or a batch marker (`BATCH`, `Season Pack`, or a `Complete` that names no single episode) parses as kind=batch (§5.2a) and is rejected by the filter before its episode number is even compared, so the ranker never sees one — a batch's low end *is* the number Arc asked for, which is how 6.3 GB of *Dagashi Kashi* season 2 was fetched for two wanted episodes. **A single is filtered differently** (2026-09-14, the other half of the film fix), and in three parts. The release must **say what it is** — the parser's `movie` or `special` (`SINGLE_KINDS`) — because "names no episode" was the first version of this test and it accepted three whole-series Blu-ray packs as films: `[Judas] Sword Art Online [BD 1080p]` carries no number, no range and no batch marker, and it is 20 GB of the franchise. Its title must reach 0.90 under the **strict** comparison, which scores a release that names *less* than the entry with `token_sort_ratio` and forgives only the type word itself (`TYPE_WORDS` — the parser strips a trailing `Movie` from the title it reports while the catalogue keeps it): `kizumonogatari` is a subset of all three parts of *Kizumonogatari* and used to score 1.00 against every one of them. And the **year**, when both sides have one, must agree within one — a franchise reboot carries the original's name exactly, a December premiere is a January disc — with a missing year on either side counting as *no evidence rather than agreement*, which is why the strict title rule is unconditional rather than a fallback. It is then episode 1, the row the catalogue holds for it. A numbered release under a one-episode entry is refused unless the parser read it as the film itself (`[SubsPlease] Yuru Camp - 01` is not *Yuru Camp Specials*), a creditless opening is still ignored, and a batch is still a batch. The cost, stated rather than hidden: a BD rip that names nothing but the franchise (`[Coalgirls] Kizumonogatari [BD 1080p]`) is refused, because nothing in its name distinguishes it from a series pack — a missing file is visible and fixable, the wrong film plays as though it were right. Every rejection is logged with the sentence it was rejected by. Ranked: **a dub below every subbed candidate** (FR-A3, 2026-09-14: `ParsedName.dubbed`, and it sorts ahead of all four rules because a dubbed release is not a worse copy of the episode but the episode in the wrong language; it is a ranking and not a filter, so a dub is still chosen when nothing else was found, with a log line saying so), then preferred groups > preferred/fallback resolution > seeders > trusted; per-show overrides in `settings` key `override:anime:<id>`. The query forms and the filter are pinned offline by `tests/fixtures/query_corpus.txt` — one block per real production case: the entry, the episode, the forms that must be built, the forms that must **not** be, and real release names that must be accepted and rejected (§10). |
| qBittorrent Web API (`QBIT_URL`, `QBIT_USER`, `QBIT_PASS`, `QBIT_CATEGORY`=arc, `QBIT_DOWNLOADS_PATH`=/data/downloads container-side) | cookie login, re-login on 403 | n/a | `torrents/add` (magnet, category, savepath `<downloads>/<episode id>`; handles 4.x `Ok.` and 5.x JSON/409-duplicate dialects idempotently), `torrents/info?category=arc`, `torrents/delete` (Arc category only), `app/setPreferences` (seeding **and queue** policy applied at worker start and daily: ratio limit 0 with action Stop, seeding time 0, upload cap `QBIT_UPLOAD_LIMIT_KIB`, plus `queueing_enabled`, `max_active_downloads` = `QBIT_MAX_ACTIVE_DOWNLOADS` (8), `max_active_torrents` = `QBIT_MAX_ACTIVE_TORRENTS` (12) and `dont_count_slow_torrents` — the client's own defaults are 3 and 5, and a container restart is what loses a limit Arc did not write; the queue half is sent whatever `QBIT_SEEDING` says), `torrents/stop` (any completed torrent still seeding is stopped by `poll_qbit` unless `QBIT_SEEDING`). `dont_count_slow_torrents` carries its own thresholds — `slow_torrent_dl_rate_threshold`/`slow_torrent_ul_rate_threshold` 2 KiB/s and `slow_torrent_inactive_timer` 300 s — so a torrent stops occupying a slot only after five minutes of moving essentially nothing, and an ordinary lull costs a healthy download nothing. The queue only ever changes what *counts*: nothing is removed by it, and the stall rule of §5.1a is the only thing that gets rid of a torrent going nowhere. `torrents/info` is also read for `time_active` (the stall clock), `dlspeed`, and the four peer counts: `num_complete`/`num_incomplete` are the tracker's last scrape of the swarm, where `-1` means "not scraped yet" and is never read as zero, while `num_seeds`/`num_leechs` are only the peers connected this instant — routinely 0 on a healthy torrent, so no rule may read them as an empty swarm. Dev compose bind-mounts the repo's `data/downloads` so the host worker sees files; first-run temporary password must be replaced with `QBIT_PASS` (see README). |
| Gemini (AI Studio) `https://generativelanguage.googleapis.com/v1beta/openai/` (`GEMINI_BASE_URL`) | `GEMINI_API_KEY` | **free tier: ~20 requests/day/model for the whole deployment** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`), plus per-minute limits; 429 and 503 "high demand" are both common, and a busy model can end a stream after one chunk | The primary provider (`RECS_PROVIDER=gemini`). `RECS_MODEL` lists several models tried in turn — extra daily quota rather than better answers; 3.5 leads because it was the most *available* when measured. Python SDK `openai` (3.11); streamed chat completions, `response_format` json_schema, `reasoning_effort: low`. Reasoning tokens come out of `max_tokens` (16000). A daily-quota 429 puts that model on cooldown until 08:00 UTC. |
| OpenRouter `https://openrouter.ai/api/v1` (`OPENROUTER_BASE_URL`) | `OPENROUTER_API_KEY` | per account, paid | The fallback (`RECS_FALLBACK_PROVIDER=openrouter`), used once every Gemini model is spent for the day — it is the thing that still works when the free tier does not. Same code path; `RECS_FALLBACK_MODEL` is a `vendor/model` slug. Sends `HTTP-Referer`/`X-Title` for attribution. |
| Anthropic API | `ANTHROPIC_API_KEY` | n/a | Selectable as either chain end (`RECS_PROVIDER` or `RECS_FALLBACK_PROVIDER` = `anthropic`, `RECS_MODEL=claude-opus-5`). Python SDK `anthropic` (1.4.0); `client.beta.messages.stream` with adaptive thinking, `output_config.format` JSON schema, `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). Also M13's match suggestions, whatever the recs provider is. |
| Offline catalogue (M15.5, **implemented — import, search, filename matching, id mapping and season seeding; TMDB follows**): manami `anime-offline-database` weekly release (`anime-offline-database.jsonl.zst`, ≈ 6 MB / 62 MB raw, 41,537 entries) + Fribb `anime-lists` (`anime-list-full.json`, ≈ 7.5 MB, 32,281 mappable entries) | none | one download each per week (Monday 03:30 UTC), 60 s timeouts, streamed to disk | Loaded by `import_offline_catalogue` into `offline_anime` and `offline_ids` (replace-on-import in one transaction; manami's release tag and Fribb's ETag stored in `offline_imports`, and a matching sha256 skips the work). Decompressed with the stdlib `compression.zstd` — no dependency. One source failing keeps its own rows and does not stop the other. `GET /api/catalogue/offline` reports version, age and counts; `arc.cli import-catalogue` runs it now. It **is** the first stop for search (§5.0, behind the cached rows and ahead of the live page), for filename matching (§5.2a, ahead of the live catalogue search), and for cross-id mapping (AniList ↔ MAL, filled into the cache upsert and into `catalog_reconcile` with no network); and it seeds the season when both live sources fail (FR-C7). Hits are materialised as `anime` rows written as the weakest source, so a live payload overwrites everything they wrote. The TMDB ids it also carries (series + season) are bullet 4. |
| TMDB `https://api.themoviedb.org/3` (M15.5, **implemented**) | `TMDB_API_KEY` (free, v3, sent as `api_key=`) | ~50 req/s; the client paces at 4 req/s, one process-wide breaker on 429/5xx/401, two retries on 5xx | Nightly `tmdb_enrich_all` (04:10 UTC; watched shows in full — on a list, with playback progress, or holding a ready episode — then this and next season's shows art-only, most popular first) + on-demand from `catalog_refresh` and from `GET /api/home` (full, for the shelf cards missing a still; art-only for the hero's pool), reached by id through `offline_ids` (§5.0a). Three calls per full enrichment and one per art-only one: `/tv/{id}` (or `/movie/{id}`), `/tv/{id}/season/{n}`, `/tv/{id}/aggregate_credits`. Fills `backdrop_url` and `banner_url` (backdrop w1280), `cover_large_url` (poster w780), `episodes.still_url` (w300) / `title`, and `credits` — only where they are null, and never a column AniList filled (cache rule 3, §5.8); `backdrop_url` is the exception, TMDB's own column, written whenever its answer differs from what is stored. Season from `offline_ids.tmdb_season`, else the year + episode-count heuristic; no plausible season means art only. `/api/health` publishes `tmdb_enabled` and the Home footer carries TMDB's attribution line. |

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
- The live event stream (`GET /api/events`, §5.9) is no exception: it takes
  `CurrentUser` and answers 401 without a cookie. It is a GET, so the origin
  check — which guards mutations — does not look at it, and it needs nothing
  from it: the payloads carry ids and nothing else, so there is nothing on the
  stream a signed-in client could not already ask for by id, and nothing
  identifying any user. Not cacheable (`no-cache, no-store,
  must-revalidate`), capped at 100 streams per process, and the listening
  connection uses the same DSN as the engine.
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
  passed through. `/api/events` (§5.9) is its own, more specific `handle`
  with `flush_interval -1` so frames are written straight through, and it is
  excluded from `encode` by a named matcher (`@compressible not path
  /api/events`): `text/event-stream` is inside Caddy's default encode match
  list, and a compressor on a response that never ends is exactly where
  "encode it" and "send it now" are in tension. Both are belt and braces —
  Caddy infers the flush interval for event streams itself — and both are
  spelled out because the failure is silent: a page that looks configured and
  updates only on reload. Validate a change to either with the `caddy
  validate` line at the top of `deploy/Caddyfile`.
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
- `worker` — `python -m arc.worker`, **exactly 1 instance** (ffmpeg
  concurrency is raised with `MAX_TRANSCODES`, not with a second container:
  the start-up reclaim of §2 treats any lock that is not its own as orphaned,
  so two live workers would requeue each other's work). `stop_grace_period:
  15s`, which must stay above `WORKER_DRAIN_TIMEOUT` (10 s) — the worker uses
  the difference to cancel what the drain could not finish and put those rows
  back to `pending`.
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
(false), `QBIT_MAX_ACTIVE_DOWNLOADS` (8), `QBIT_MAX_ACTIVE_TORRENTS` (12),
`STALL_METADATA_MINUTES` (60), `STALL_NO_BYTES_HOURS` (6) — the four of §5.1a's
queue and stall policy; the two stall thresholds count the client's own
`time_active`, and the queue limits only decide what *counts* as active (a
torrent is counted out after 5 min under 2 KiB/s) while the stall rule is the
only thing that removes a dead torrent — `COMPOSE_PROFILES` (vpn|novpn), `VPN_PROVIDER`, `WIREGUARD_PRIVATE_KEY`,
`WIREGUARD_ADDRESSES`, `WIREGUARD_PUBLIC_KEY`, `WIREGUARD_ENDPOINT_IP`,
`WIREGUARD_ENDPOINT_PORT`, `VPN_SERVER_COUNTRIES/CITIES` (deploy-only, read
by gluetun), `WORKER_CONCURRENCY`
(default 2), `WORKER_POLL_INTERVAL` (seconds, default 1), `WORKER_DRAIN_TIMEOUT`
(the shutdown grace: seconds to wait for in-flight jobs on SIGTERM before they
are cancelled and requeued, default 10 — it must stay below the worker
container's `stop_grace_period`, 15 s, or Docker's SIGKILL arrives mid-drain
and the row stays `running`; a test pins the two together),
`WORKER_STALE_AFTER` (seconds before a `running` job with a dead worker is
requeued *by age*, default 7200; transcodes heartbeat their lock, and a worker
*restart* no longer waits for it — the next worker reclaims by identity at
start-up, §2),
`SESSION_TTL_DAYS` (30), `LOGIN_RATE_LIMIT_PER_IP` (10),
`LOGIN_RATE_LIMIT_PER_EMAIL` (5), `LOGIN_RATE_WINDOW_SECONDS` (900),
`CORS_ALLOWED_ORIGINS` (comma list, optional; dev origins are added
automatically when `ENV` is not prod), `MAL_OAUTH_URL`
(https://myanimelist.net, the authorize/token host), `MAL_IMPORT_INTERVAL_HOURS`
(6), `OFFLINE_MANAMI_URL` / `OFFLINE_FRIBB_URL` (the two public datasets of
§5.0a; overridable for a mirror or a test), `OFFLINE_CATALOGUE_STALE_DAYS` (14), `TMDB_API_KEY` (unset; a free v3 key from
themoviedb.org/settings/api turns on the enrichment of §5.8 — without it shows
render whatever art AniList and MAL provided, and no attribution line is
shown). `FERNET_KEY` must be set before anyone links MAL and is not rotatable
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
recommendations key being unset, a `RECS_MODEL` that does not look like
the selected `RECS_PROVIDER`'s, and `TMDB_API_KEY` being unset (§5.8: key art
and stills are simply not enriched, which is invisible without the line). "Is anything configured" is
`config_check.model_chain_configured`, a deliberate restatement of
`recs.factory.chain_entries` (the factory imports `is_placeholder` from
config_check, so importing it back would be a cycle) with a test pinning the
two together.

## 10. Testing strategy

- Parser/matcher: corpus of ≥ 200 real release names with expected
  (title_key, episode, season, kind, group, version); assert confidence tiers.
  `tests/fixtures/release_names.txt` stands at 259 names, 100 % on episode+kind
  and title_key, with a floor in `test_parser_corpus.py` so the cases added for
  a bug cannot be deleted along with the fix.
- **Query corpus** (`tests/fixtures/query_corpus.txt`, 2026-09-14): the other
  half of a match, and the half no parser corpus can pin. One block per real
  case — the catalogue entry (titles, synonyms, format, episode count, year,
  whether it has a prequel), the episode being searched for, the query forms
  `queries()` must build, the forms it must **not** build, real nyaa.si release
  names `acceptable()` must accept and reject, and which of the accepted ones
  `rank()` must prefer. A block may also declare `prequel_episodes`, which the
  reader feeds through the real `absolute_offset()` walk (2026-09-17) so the
  absolute forms and the absolute acceptance rule are asserted from the block's
  own counts rather than from a catalogue fact the file cannot show.
  Eighteen cases, every one of them a show that sat in
  `searching` on production (or a film that quietly stood in for one): Rakudai
  (the head form), Mushoku Tensei S3 (the
  short forms), Frieren S1 and S2, One-Room TA (`SxxEyy` and its two batches),
  the *Dagashi Kashi* season pack, Made in Abyss S2 (the head form that must
  not be asked), Cowboy Bebop's year pair, Yarichin☆Bitch-bu and Love Live!
  Superstar!! (symbols), Fate/Zero (the slash), two dub-versus-sub pairs, three
  films and the *Kizumonogatari* trilogy, whose three parts share one name and
  carry no episode number — the case the strict single title rule exists for —
  and *Jujutsu Kaisen* season two, whose releases are numbered 25–47 where the
  catalogue numbers them 1–23. It runs **offline** — `queries`, `acceptable` and `rank` are
  pure functions of an `anime` row — so a query form is a claim about what
  release groups write, dated and written down where it can be argued with.
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
- Offline catalogue (M15.5): parsing is tested against a **real slice** of both
  files — `tests/fixtures/offline/{manami-slice.jsonl,fribb-slice.json}`, 29
  records captured from release `2026-27` by `scripts/capture_offline.py`,
  chosen for coverage (the five Mushoku Tensei entries a matcher has to tell
  apart, Frieren, three films, an entry with no season year, one with no
  MyAnimeList source at all, a film whose TMDB id is a list, and an entry with
  no TMDB id). A fixture invented from the documentation would agree with the
  documentation; these files' interesting shapes are all things somebody else
  chose. The import, the job and the CLI run against a real Postgres with the
  download behind an `httpx.MockTransport`; nothing in the suite fetches
  either dataset.

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
- 2026-09-12 — **Superseded the same day** by the fixed-frame entry at the end
  of this log. M15 hero framing (owner: "too zoomed in"): a banner hero sized
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
- 2026-09-12 — M15.5 bullets 2 and 3 as built: the offline catalogue answers
  search (behind the cached rows, ahead of the live page), is the *first*
  candidate source for filename matching (the live search runs only when it
  finds nothing), fills the missing AniList/MAL id in every cache upsert and in
  `catalog_reconcile`, and seeds a season when both live sources fail. Its rows
  are materialised into `anime` as the **weakest** source
  (`SOURCE_STRENGTH = anilist, mal, offline`): nulls only, no `refreshed_at`,
  no `detail_source`, so opening one still fetches and the first live payload
  overwrites everything it wrote. The season fallback lives in
  `catalog_season_sweep` rather than in `CatalogService`, which composes two
  HTTP sources, holds no session, and has exactly one caller of `season()`.
  Offline gets its own strength tuple rather than joining `SOURCE_NAMES` so
  that `GET /api/catalog/status` keeps reporting the two live sources only.
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
- 2026-09-12 — M15.5 part 1 landed: the offline catalogue import (§5.0a).
  Three tables (`offline_anime`, `offline_ids`, `offline_imports`), a weekly
  job, a Monday 03:30 UTC cron with a run-now-if-never-imported startup probe,
  `arc.cli import-catalogue`, and `GET /api/catalogue/offline`. Decisions made
  in the doing: **replace with `DELETE`, not `TRUNCATE`** (an `ACCESS
  EXCLUSIVE` lock would block the readers this table exists for); **sha256 of
  the downloaded file** as the skip condition rather than the release tag,
  because a tag tells you nothing about a file served from a branch; **the two
  sources fail independently** and the job raises only when both did;
  **`compression.zstd` from the 3.14 standard library**, so a 62 MB dataset
  costs no new dependency; **`search_text` denormalised with a pg_trgm GIN
  index** (`CREATE EXTENSION` in the migration — measured: an `ILIKE '%…%'`
  over 41,537 rows is a 1.9 ms bitmap index scan); and **`offline_ids` kept
  separate from `offline_anime`**, because they are two files with two
  cadences and two coverages, and joining them at import time would mean
  choosing which side's absence wins. Nothing reads either table yet.
- 2026-09-12 — M15.5 part 2 landed: TMDB enrichment (§5.8, `services/tmdb/`).
  Reached by id through `offline_ids` only — AniList publishes no TMDB id — and
  held to cache rule 3 one source further down: it fills `banner_url`,
  `cover_large_url`, `episodes.still_url`/`title` and `credits` where they are
  null, never touches `cover_url`, and never a column `detail_source = anilist`
  has filled, however short that answer is. Season from
  `offline_ids.tmdb_season`, else the air-date year plus the closest episode
  count; no plausible season means art without stills, because a still from the
  wrong cour is worse than none. Crew from `aggregate_credits` with an
  exact-match job table and an episode-count ranking, since TMDB credits
  twenty-two people as "Director" on two episodes each. Nightly
  `tmdb_enrich_all` at 04:10 UTC plus an on-demand enqueue from
  `catalog_refresh`, deduplicated on `tmdb_enrich:<anime_id>`. `TMDB_API_KEY`
  unset is a *warning*, not an error: the job no-ops and the deployment is
  complete without it. `/api/health` gained `tmdb_enabled`, which is what the
  Home footer's TMDB attribution line follows.

- 2026-09-12 — AniList staff roles are matched **whole** against a table of
  known spellings (`anilist/extras.py::ROLE_CREDITS`), replacing the keyword +
  qualifier-blocklist matcher. AniList's vocabulary builds new jobs by adding
  words to an existing one, so a blocklist is a race the credit always loses:
  a re-capture of the Frieren fixture brought "Action Director" (read as the
  director) and "Original Work Assistance" (read as the author), neither of
  which the list knew. A role AniList spells in a way the table does not know
  is dropped, as an unmapped role always was; a combined credit
  ("Director, Series Composition") is split on `,` `/` `&` `and` and each half
  matched in its own right. The six credits and their order are unchanged.
- 2026-09-12 — **TMDB art for the hero, not only for followed shows** (owner:
  "make sure we are using the new art from TMDB, the hero posters are still
  bad"). The nightly sweep only ever selected *followed* shows, and the Home
  hero offers shows the viewer does not follow — recommendation picks and the
  current season — so on the dev database 203 of Summer 2026's 210 rows had no
  banner and all 210 had no `cover_large_url`, 68 of them mapped to a TMDB id
  nothing would ever fetch. `tmdb_enrich_all` now runs a second pass over this
  and next season's mapped shows that lack key art, ranked by `popularity DESC
  NULLS LAST`, and `GET /api/home` queues the same thing for the top 12 shows
  of the hero's own pool that have no artwork at all. Both use a new
  **art-only** mode (`{"art_only": true}`): one `/tv/{id}` for the backdrop and
  the poster, no season and no credits call, because stills and crew are for a
  page somebody opens rather than for a frame they scroll past. `SWEEP_LIMIT`
  200 → 300 so a season (~200 shows) cannot be crowded out by the followed
  pass. Rule 3 is untouched: TMDB still only fills nulls, and the one dedupe
  key per show means the richer followed job always wins. The client needed no
  change — `HeroFrame` already prefers `banner_url` → `cover_large_url` →
  `cover_url` and clamps a 16:9 backdrop to its 21:9 floor, which crops it
  rather than letterboxing it.
- 2026-09-12 — **Airing sanity rule** (§5.0, owner). `catalog/airing.py` now
  reads two impossible-data rules over the stored dates: a `FINISHED` show has
  no future episodes, and an episode dated after a higher-numbered one is
  flagged estimated and airs with its neighbour. Both are derivations — the
  stored `air_at` is never rewritten, so a corrected upstream still lands — and
  both reach every caller through `aired_through`/`is_aired`, which the show
  page, behind-by, the acquisition window and `aired_episodes` already shared.
  Two places that read air times *outside* that pair were moved onto the same
  reading: behind-by's `latest_aired_at` (through `effective_air_at`) and the
  schedule's weekday placement, which now takes the last episode's date rather
  than `max(air_at)` so a stray date cannot move a show to another weekday. The
  cost is one behaviour change: an episode genuinely delayed past a later one
  is now wanted with it rather than left behind, which is the better failure —
  broadcasts do not overtake each other, so the case is a typo far more often
  than it is a delay.
- 2026-09-12 — **Interactive AniList calls do not sleep off a 429** (§6, owner).
  `AniListClient(wait_on_rate_limit=False)` raises `AniListRateLimited` instead
  of waiting out `Retry-After`; `AniListSource` maps it to a new
  `SourceRateLimited(SourceUnavailable)`, which `CatalogService` falls back on
  like any unavailability but deliberately does **not** open the breaker for
  300 s — a burst limit clears in seconds and costs no timeout to discover, so
  standing AniList down for five minutes would be the larger outage. The client
  keeps a per-instance "rate-limited until" so the next interactive call inside
  the window skips the request entirely. `arc/main.py` passes `False` (a user
  is waiting on every call the app's catalogue makes); the worker's
  `catalog_for()` keeps the wait, having nowhere better to be. Found when a
  search sat for 3 s before falling back to MAL.
- 2026-09-12 — The httpx logger is held at WARNING: it prints request URLs with query strings, and the TMDB key rides in one (orchestrator, small call).
- 2026-09-12 — **The Continue watching card frames a poster instead of zooming
  into a banner** (owner: "continue watching posters need adjustment"; §2,
  §5.8). The 16:9 episode card fell back to the show's `banner_url` when the
  episode had no still, and an AniList banner is ~1900 × 399: `object-cover` in
  a 279 × 157 box showed a 3× zoom of a sliver of it. The card's art order is
  now still → banner *only if its measured ratio is ≤ 2.2* (a TMDB backdrop
  passes, an AniList strip does not) → the poster treatment, which is
  `HeroFrame`'s poster hero extracted into `ui/PosterWash.tsx` and shared by
  both. The ratio comes from `Artwork`'s `onNaturalSize` on an off-frame copy,
  and the poster treatment holds the card until it is known, so nothing flashes.
  The root cause of the missing still was the enrichment's eligibility rule:
  TMDB stills only ever reached shows on somebody's *list*, and this one was
  being watched off-list. A show is now worth a full enrichment if anyone has
  `watch_progress` on an episode of it or Arc holds a ready episode of it, and
  `GET /api/home` queues a full `tmdb_enrich` (bounded at 8, same dedupe key,
  before the hero's art-only pass) for each shelf card whose episode has no
  still. `lib/anime.ts::heroArt` went with it — nothing else used it.
- 2026-09-12 — **The hero is one fixed 21:9 frame on every slide** (owner: "all
  heroes in the homepage need to be in the same size"; §2). This reverses the
  adaptive frame decided earlier the same day: sizing the frame to the banner's
  own ratio (clamped to [21/9, 3.6]) meant a 16:9 backdrop landed on 21:9 and
  an AniList strip on 3.6:1, so Watch Now's carousel changed height between
  slides. The frame is 21:9 again, and the *treatment inside it* absorbs the
  difference, exactly as the episode card already did: a banner with a measured
  ratio ≤ 2.6 fills the frame with `object-cover`, anything wider takes the
  `PosterWash` treatment (the banner itself becomes the wash ground when the
  show has no poster). The ratio is measured off-frame, so the wash — not a
  zoomed strip — is what holds the frame until it is known. The off-frame
  measurement moved out of `pages/Home.tsx` into `ui/AspectProbe.tsx` and is
  now shared by the hero and the episode card; it reports a ratio rather than a
  size, which is a number React's state can bail out of re-rendering for.
  `Artwork.aspect` (the inline measured ratio) had no callers left and was
  removed with the rest of the adaptive code; `onNaturalSize` stays, since the
  probe is what reads it. `PosterWash` gained `ground` (what to blur, when it
  is not the poster). The show page hero keeps the same behaviour — it uses the same
  component and has no reason to differ.
- 2026-09-12 — "Try episode 1" (FR-A8, M16) is a **flag on `wants`**, not a
  table of its own. A sample is an ordinary want in every respect that costs
  anything — one row per (user, episode), merged across users, the same episode
  state machine, the same FR-T2 drop, the same FR-T1 grace — and the only thing
  that differs is who justifies it: the reconciler derives every other want
  from a list it can re-read, and cannot derive this one from anything, so the
  row says so itself. A `samples` table would have meant a second source of
  "does this episode have a live want?" in `_start_searches` and in retention,
  which is the one question those two must never answer differently. The two
  routes live on the anime router (it is the show page's button) over
  `services/acquisition/samples.py`, which is import-light enough for a request
  path: it reaches `wants` and `catalog.airing` and nothing heavier.
- 2026-09-13 — The sample routes act on the episode inside the request (owner,
  on seeing it in the browser), through two helpers factored out of
  `_start_searches` — `start_search()` and `release_if_unwanted()` — rather
  than by enqueueing `compute_wants` alone. The enqueue-only version was
  correct and useless: the row a person had just pressed said "Not fetched" for
  up to fifteen minutes, and for ever while acquisition was paused. Extracting
  the reconciler's own per-episode decisions instead of copying them keeps one
  answer to "may this episode be searched for yet", which matters most for the
  `UNAVAILABLE_RETRY` gate — the sample path deliberately skips it
  (`retry_now`), and that is a decision visible at the one call site rather
  than a second copy of the rule that drifts. `SampleOut` gained `state` so the
  client can render the row from the response it already has; the mutation
  writes it into the cached detail and the invalidation behind it confirms it.
- 2026-09-13 — The Nyaa query builder asks by the **head of the title** as well
  as the whole of it (FR-A4), and `MAX_QUERIES` rises 5 → 6 so an unmarked
  title with a subtitle keeps its bare form behind the two head forms. The
  evidence: on 2026-09-12 22:20 the worker ran all three of its forms for
  *Rakudai Kenja no Gakuin Musou: Nidome no Tensei, S-Rank Cheat Majutsushi
  Bouken-roku* (english *From Overshadowed to Overpowered: Second
  Reincarnation of a Talentless Sage*, no season marker) and every one returned
  0 results, so episode 1 went onto the six-hour retry schedule; by hand the
  same night `Rakudai Kenja no Gakuin Musou 01` and `Rakudai Kenja no Gakuin
  Musou - 01` each returned 9 releases and `From Overshadowed to Overpowered
  01` returned 0. Nyaa ANDs every word and groups name a show by its head, so a
  nine-word catalogue title is not a query at all — the `_short_forms` fix only
  covered the case where the *season marker* is what diverges, and this is the
  case where the *subtitle* is. A separator counts only with whitespace on one
  side, which is what keeps `Re:Zero kara Hajimeru Isekai Seikatsu` from being
  asked for as `Re`. Nothing in the filter changed: the 0.90 asymmetric
  `title_score` rejects a release that merely shares the head (its extra tokens
  are in none of the entry's own titles) and the season-agreement check rejects
  a later season, and those two are the entire safety of a broad query. The one
  thing they cannot catch is the reverse direction, so head forms are not asked
  for an entry with a `PREQUEL` relation: a release named by the bare head is
  most likely the first season and an unmarked sequel — one whose title names
  an arc rather than a season, *Made in Abyss: Retsujitsu no Ougonkyou* — cannot
  be told apart from it by season agreement, while the shorter release name is
  exactly what `title_score` reads as a group abbreviating an official title. An
  entry with no relations stored keeps the head forms: a missing list is not
  evidence of a prequel, and those are the rows Arc knows least about.
- 2026-09-13 — **A batch is never picked, and a range or a marker is what makes
  one** (§5.2a, §6, FR-A4). One production acquisition run fetched seven batches,
  the first of them 6.3 GB of *Dagashi Kashi* season 2 — all twelve episodes
  transcoded for the one or two somebody wanted. Every one of them parsed as
  `kind=episode`, episode 1, and the filter keeps exactly that. Two holes, both
  in the parser: anitopy reports `- 01 ~ 12` as episode `01` and stops, and the
  loose range re-read was swallowed by the show's own trailing number (the
  regex matched `2 - 01` of `Dagashi Kashi 2 - 01 ~ 12` first, a range counting
  down, and `finditer` resumed past the real one); and a `[BATCH]` marker was
  consulted only when the name carried no episode number at all, which is never
  true of a batch that names the first episode of its run. Now a dedicated
  episode-range pattern is read at the episode position *before* anitopy's
  answer is trusted, and a batch marker (`BATCH`, `Season Pack`) is a batch
  whatever the numbers say. The `Complete` family is gated on the name naming
  no single episode (owner follow-up the same day): the word is also what a
  fansub shouts on the last episode of a run — the same claim `END` makes, and
  the reason `_END_RE` carries it — so `[Group] Show - 12 [COMPLETE]` is
  episode 12 and `[Group] Show [Complete]` is a batch. Both ends of a range must
  be zero-padded or `E`-prefixed and count up from at least 1 — which is what
  keeps `Ranma 1-2` a name, `1920x1080` a resolution, `10-bit` a bit depth,
  `01v2` a version, `2024-05-10` a date and `Mob Psycho 100 - 07` an episode —
  and the range has to be the last thing before the tags and not a pair of
  years, so `Chihayafuru - 01 - 100 Poems` is episode 1 and `Cowboy Bebop
  1998-1999` is an airing span; those last two also gate anitopy's own range
  answers, which is where both of those had been read as runs. The separator
  set includes the wave dash `〜` and the fullwidth `～` a Japanese raw writes,
  and a run written that way is taken off the title too (anitopy leaves it
  there), targeted by the file's own two numbers so *86* keeps its name. A padded two-episode pack **is** a batch
  (`[RH] Fukigen na Mononokean - 01-02`): it is an episode nobody asked for.
  `MIN_BATCH_SPAN` still governs the unpadded fallbacks only. The corpus carries
  all seven names plus four single-episode controls from the same groups and
  shows (247 names at the time, 100 % on episode+kind and title_key), and `acceptable()`
  now logs the sentence behind every rejection, the batch one first.
- 2026-09-13 — `AspectProbe` loads its off-frame copy **eagerly**. The probe
  sits in a 0×0 box, and Chrome does not fetch a `loading="lazy"` image that
  has no box to scroll into view, so the hero's banner aspect was never
  reported and every hero whose banner was not already cached by another
  card stayed on the blurred-poster fallback (owner spotted it on Watch Now,
  Saga of Tanya the Evil II, which has a 1280×720 TMDB backdrop). One
  attribute; the rule that decides banner-vs-wash is unchanged.
- 2026-09-13 — **`anime.backdrop_url`: a column TMDB owns, because the design
  cannot show AniList's banner** (owner; spec.md §10, "Hero art"; §4, §5.8,
  §5b). AniList's `bannerImage` is a 1900×400 strip — 4.75:1 — and nothing in
  the M15 grammar renders one as a picture: `HeroFrame` refuses a banner wider
  than 2.6:1 and `EpisodeArt` one wider than 2.2:1, both falling back to the
  blurred-poster wash. TMDB's backdrop is 16:9 and is what both frames want,
  but the enrichment could only write `banner_url` where it was null (cache
  rule 3) and an AniList refresh would overwrite it with the strip anyway — so
  every show AniList reached first sat on the wash for good (dev: Tanya S2,
  Villager of Level 999, Magilumiere S2). The new column is written **only** by
  the TMDB enrichment, and there whenever TMDB's answer differs from what is
  stored — the one place TMDB may overwrite itself, since no other source can
  put anything there to lose. `bannerArt` in `lib/anime.ts` returns it ahead of
  `banner_url` and every hero and card already went through that one function,
  so no component changed. A null `backdrop_url` on a mapped show is a hole
  (`_missing_key_art`), which is what backfills the existing rows through the
  nightly sweep, the refresh hook and the sample route; the Home hero's own
  enqueue now asks only about the backdrop (`_missing_hero_art`), replacing
  "no artwork at all" — a strip and a poster was art the frame could not use.
  Chosen over letting TMDB overwrite `banner_url`, which the refresh would
  undo, and over teaching the frames to letterbox a strip, which is a 4.75:1
  picture in a 21:9 hole either way. Migration `0aa6beaab5ed`, nullable, no
  backfill.
- 2026-09-13 — **Stalls, one attempt per release, cancels, and the download
  queue** (owner, spec §10). Four changes to acquisition, all of them the same
  lesson twice: Arc knew what it had *started* and nothing about whether it was
  getting anywhere.
  1. `poll_qbit` now gives up on a torrent that is going nowhere
     (`jobs.stall_reason`, §5.1a): `metaDL` past `STALL_METADATA_MINUTES` (60),
     or no bytes / an empty swarm past `STALL_NO_BYTES_HOURS` (6). Row marked
     `stalled` and episode `unavailable` onto FR-A6's schedule, flushed, then
     deleted with its files — that order so a failed delete is retried rather
     than forgotten. The rule needed no new column: it reads the client's own
     `time_active` (**not** `torrents.added_at`, which with a bounded queue
     makes a torrent's first active minute look like nine hours of failure),
     and it calls a swarm empty only on the tracker's `num_complete`/
     `num_incomplete` (the connected `num_seeds`/`num_leechs` are 0 all the
     time on healthy torrents). Guarded further by an **allow-list of states**
     plus `dlspeed == 0`, because `queuedDL` (most of the queue for hours after
     an import) and `stoppedDL` (297 in production) make no progress correctly.
     A row carrying a decision never moves its episode again, or a stalled
     attempt would drag the retry that replaced it back to `unavailable`.
  2. `_pick` skips any candidate whose hash already has a `torrents` row, and
     the Nyaa filter rejects 0 seeders outright. `search_release` is one
     transaction, so a row *means* "tried and failed"; without this the same
     dead magnet was re-added on every retry.
  3. A `downloading` episode whose last want has gone is cancelled: new state
     edge `downloading → not_wanted`, torrents marked `cancelled`, and one
     `qbit_cancel` job per episode to do the deleting. The job rather than a
     loop in `compute_wants` because the reconciler holds a transaction and
     because an unreachable client must not fail a reconciliation; the job
     deletes only rows marked `cancelled`, so a re-wanted episode's new
     release survives, and the reconciler leaves alone any row that already
     carries a decision (a `rejected` one is holding a file in review).
     After a successful delete the rows themselves go, because `_pick` bars
     every hash that has one and a cancel is not a failed attempt.
     `downloaded` onwards stays retention's (FR-T1), and `cancel_sample`
     (FR-A8) goes through the same helpers.
  4. `qbit_apply_policy` writes the **queue** policy alongside the seeding one
     (`queueing_enabled`, `QBIT_MAX_ACTIVE_DOWNLOADS` 8,
     `QBIT_MAX_ACTIVE_TORRENTS` 12, and `dont_count_slow_torrents` with
     deliberate thresholds — 2 KiB/s either way, 300 s — so an ordinary lull
     costs no slot and a dead torrent stops blocking the queue five minutes
     in; the queue still only *counts*, never removes). The owner had
     raised these by hand on the production client; a container restart would
     have silently put them back to qBittorrent's 3 and 5.
- 2026-09-13 — **Dormant imports as built** (FR-A9). `list_entries.activated_at`
  (nullable, TZ, write-once, never cleared) is the stamp; the rule is a leaf
  module, `arc/services/acquisition/dormancy.py`, with `activate()`,
  `is_dormant(entry, *, airing)` and `REASON_DORMANT`. A leaf because
  `catalog.lists` — one of its writers — cannot import
  `acquisition.wants` without closing an import cycle through `catalog.airing`,
  and `is_dormant` takes a boolean rather than the `Anime` row for the same
  reason: "is it airing?" is the catalogue's question. `compute_wants` skips a
  dormant entry before the window and keeps its (user, show) out of `wanting`,
  so its wants shelve, its searches release and its downloads cancel through
  the paths that already existed. Migration `3b7c41e9d2af` adds the column and
  backfills `activated_at = updated_at` where `updated_by = 'arc' OR
  mal_synced_at IS NULL` — the first clause for a row whose last change was
  Arc's, the second for an Arc-made row a later import overwrote (`updated_by`
  only remembers the last writer, and an import does not set `mal_synced_at`).
  The one shape neither clause can tell from an import — Arc-made, then changed
  on MAL, then successfully pushed — goes dormant and costs one press of "Fetch
  this show", which is a far smaller error than the other direction.
  `ListEntryOut` gained `activated_at` and a derived `dormant`, built through
  `ListEntryOut.build(entry, anime_status=…)` rather than `model_validate` at
  every call site (the list index and PUT, the show detail, the schedule's
  "behind on" rows), because the airing half of the rule is not on the entry.
- 2026-09-13 — **Storage guard as built** (FR-T6). `arc/services/storage.py` is
  a new leaf holding the one free-space measurement (the walk-up rule moved out
  of `arc/api/retention.py`'s private `_disk_usage`, which a service could not
  import), answering `None` rather than zeros when the filesystem cannot be
  read at all — "no free space" and "I could not look" are different facts, and
  only the first may hold acquisition. `acquisition.rules` gained the
  `min_free_gb` reader, the pure `storage_hold(free, floor)` and
  `is_storage_held(session, settings)` (the syscall on a worker thread, the
  unmeasurable case logged once). `compute_wants` now takes an optional
  `settings` — the only thing it is for — and passes a `held` flag to
  `_start_searches`, which skips the *starting* branch and nothing else;
  `search_release` reuses `_requeue_paused` with `HELD_LOG`. Seeded by
  migration `5e0a92c7b431`, editable in the rules editor, reported on
  `GET /api/acquisition/status` as `storage_held`/`free_bytes`/`min_free_bytes`
  from the same measurement and the same rule.
- 2026-09-13 — **Per-user slot cap as built** (FR-A10). The owner asked for
  "both" — dormant imports *and* a cap on what an activated list starts at
  once. `compute_wants`'s first half became `_plan_wants`, one pass that reads
  the entries, the episodes, the completions and the live wants and answers a
  `_Plan` (desired, wanting, dormant, waiting-per-user, fetching-per-user, K);
  `compute_wants` reconciles it and `slot_view` narrows it to one user for the
  API, so the show page's "waiting" and the reconciler's "creates no wants" are
  the same predicate rather than two that agree most of the time. The rule
  itself is the pure `slots.assign_slots`, a leaf beside `dormancy` for the same
  reason: both sides ask it the same question. The two definitions that took the
  argument: a slot is occupied by a live non-sample want on an episode that is
  neither `ready` nor `unavailable` nor `failed` (so neither a pile of
  watched-later episodes nor five shows FR-A6 has given up on can hold a slot
  for ever — the second was a review blocker), and only a **hungry** show
  competes for a free one (so an all-`ready` show hands its slot to the next
  show rather than keeping it for nothing). The other review blocker: a held-back
  show contributes nothing and `_reconcile` is told not to touch its rows,
  rather than the first version's "window ∩ live wants", which deleted a
  stale-dropped want and with it retention's grace anchor. The API pays one pass
  over the caller's list on the show page only — the same bargain `mal_sync`
  makes — and `GET /api/list`, the schedule and the home page leave the three
  new `ListEntryOut` fields at their "nothing to say" defaults rather than
  paying it per row. `GET /api/acquisition/status` gained
  `waiting_shows`/`slot_cap_k`; the rules editor gained "Shows fetching at once
  (per user)"; the show page gained one muted line and deliberately **no
  button**, because unlike a dormant entry there is nothing the viewer could
  press that would be honest.
- 2026-09-13 — **Shelves scroll for mouse users** (owner, dogfooding in
  Chrome). A `Shelf` was an `overflow-x-auto snap-x snap-mandatory` strip with
  a hidden scrollbar: perfect with a trackpad or a thumb, and with a mouse
  there was nothing to grab — the wheel scrolls the page and only Shift+wheel
  moved the rail. Rejected: hijacking the wheel (a vertical gesture that moves
  something sideways is the one scroll behaviour people hate, and it steals the
  page's own scroll on a tall page), and a visible scrollbar (a 12px grey bar
  under every rail is not this design). Chosen: **drag-to-scroll for mouse
  pointers plus arrows at the two edges on hover**, both in the shared
  component, so Watch Now, the show page's franchise rail and anything added
  later get them at once. Details in §3's client-shell section; the hero's
  glass circle moved to `styles.GLASS_CIRCLE` so the arrows and the carousel's
  ‹ › are literally the same control.
- 2026-09-13 — **The hero shows a known backdrop immediately** (owner,
  dogfooding in Safari on production). Every show in the season now has a TMDB
  backdrop, and the hero still flashed the blurred-poster wash — "semi-
  transparent posters" — on every one of its eight-second rotations. Cause:
  shape was measured per frame (`AspectProbe`) and held in that frame's own
  `useState`, so each slide, and each re-visit of Home, started at "shape
  unknown", which is the wash, and swapped to the picture once the probe
  answered. Three fixes, all client-side. **Trust the column**: `heroArt` in
  `lib/anime.ts` returns the url *and* `16/9` when the art came from
  `backdrop_url`, and `HeroFrame` takes it as `bannerAspect` — no probe, no
  wash, right on the first render; `trustedAspect` recognises
  `image.tmdb.org/t/p/…` for a caller that still passes a bare url. The show
  page's hero and Watch Now's 16:9 episode card (`EpisodeArt`) go through the
  same pair, so neither washes a poster for one paint over art the catalogue
  has already described. **Remember measurements**: the per-frame `useState`
  became a module-level `Map<url, ratio>` with subscribers (`ui/aspect.ts`,
  read synchronously on the first render), and Watch Now probes all six slides
  when the line-up is known rather than one per rotation. **Warm the pixels**:
  the next slide's image is fetched an interval ahead, one slide only.
  Rejected: dropping the probe and trusting every url's host (AniList serves
  the 4.75:1 strip and the 2:3 cover from one host, so the strip would be
  back in the frame), and keying the frame per slide to force a fresh mount
  (a remount paints an empty frame, which is the flash again). The wash
  stays the fallback for art nobody has measured and for a probe that never
  answers — a frame is never blank. Details in §3's client-shell section.
- 2026-09-13 — **A worker restart no longer orphans the job it was running**
  (owner, dogfooding on production; M16 batch 2). Transcode job 927 started
  13:30, the worker container was restarted for a deploy at 13:43, its ffmpeg
  died with it, and the row stayed `running` under the dead container's lock
  (`locked_by = "<old container id>:1"`). Nothing was going to touch it:
  `requeue_stale` only reclaims locks older than `WORKER_STALE_AFTER` (2 h)
  and the start-up sweep used the same threshold, so the episode would have
  sat in `preparing` until 15:43. The orchestrator requeued it by hand. Two
  changes, both in §2. **Reclaim by identity, not by age**: at start-up,
  before the first claim, `requeue_orphans` returns every `running` job whose
  `locked_by` is not this process's identity — regardless of `locked_at` —
  because Arc runs one worker per deployment (§8) and any other lock is held
  by a process that no longer exists. `requeue_stale` stays as the periodic
  backstop for a crash of the *current* worker's in-process task, and both now
  share one `_reclaim` helper that returns the ids it moved so the log names
  them. **Make the graceful shutdown actually run**: the drain already
  cancelled in-flight jobs and reset their rows, but `WORKER_DRAIN_TIMEOUT`
  was 30 s while the worker container had no `stop_grace_period` at all —
  Docker's own default is 10 s, so the SIGKILL landed mid-drain and none of it
  happened. The grace is now 10 s against a `stop_grace_period` of 15 s, with
  a test reading the compose file so the pair cannot drift; the claim loop also
  rechecks the stop event after taking a concurrency slot, so no job is started
  after the signal. A `running` row with a null `locked_by` is reclaimed too —
  it should not exist, and the age sweep needs a `locked_at` to compare, so
  nothing else would ever pick it up. Transcode needed no change: each run
  sweeps every `<episode>.tmp-*` for the episode before it encodes, so a
  re-run of the *same* job id after an orphan discards its own half-written
  staging directory rather than encoding into it (now pinned by a test).
  Rejected: a shorter `WORKER_STALE_AFTER` (it bounds silence during a
  three-hour encode and cannot also be a deploy's reaction time), and a
  separate `WORKER_SHUTDOWN_GRACE_SECONDS` (`WORKER_DRAIN_TIMEOUT` already
  *is* the shutdown grace; two knobs for one window is how they disagree).
- 2026-09-13 — **Watched state derived from list progress** (FR-W5, owner,
  M16 batch 2; §5.5a, §5.7, §5b). `EpisodeOut` is the single point at which
  the rule is applied — `watched = completed row OR number <= list progress` —
  and it now also carries `watched_source` (`arc` | `progress` | null),
  because only Arc's own completion can be un-marked and a "Unwatch" button
  that clears nothing was what the owner pressed on an imported list. The
  callers hand it the two facts and nothing else recomputes them: the show
  page from the entry it already loaded, Home from one extra
  `list_progress_for` query for every shelf at once, `/play` from a
  primary-key read. Retention gains the matching anchor —
  `list_entries.updated_at` of any user whose progress covers the episode —
  which is deliberately an approximation (the column moves for any change to
  the row and nothing records *when* progress passed episode 4), errs late,
  which for a deletion is the right way to err, and is clamped to the age of
  the bytes so a re-fetched episode gets a grace period of its own rather than
  being swept within the hour under a months-old imported stamp. The rule
  itself lives in `playback/watched.py`, a leaf both the API and the
  acquisition reconciler import, so the per-episode mark and the window's
  boundary cannot drift apart. The manual mark keeps going
  down `record_progress`'s own path rather than a parallel one, so "mark N"
  and "reach 90 % of N" are the same event including the auto-complete; and
  auto-complete writes the status only on an advance that reaches a **known**
  count on a **FINISHED** show, which is what keeps Arc from telling
  MyAnimeList a still-airing season is over. Considered and rejected: writing
  synthetic completion rows for 1…N-1 (eight wrong `completed_at` values, and
  retention measures its grace from that column), and a new
  `progress_passed_at` column on `list_entries` (a migration and a write on
  every import for a day or two of accuracy on episodes nobody will watch).
  One consequence worth writing down, from the review: the un-mark no longer
  stops FR-T1's clock on an episode the list still covers — clearing
  `completed_at` removes that user's completion, but the progress anchor
  remains, which is the honest reading of "un-marking never lowers progress".
- 2026-09-13 — **Un-watch lowers progress; any status auto-completes** (owner,
  after the batch-2 review; §5.5, §5.5a, §5b, spec FR-S4/FR-M4/FR-M7/FR-W5).
  The first is a consequence of FR-W5 that only showed up once the marks came
  off the list: with `watched` derived from `list_entries.progress`, clearing a
  completion row changed nothing a viewer could see, so there was no way to
  correct a mark. `DELETE …/watched` now runs `progress._retreat_list` —
  progress to N−1 when the list stands at N, `updated_by = arc`, `mal_dirty`,
  `activated_at`, one `compute_wants`, and one `progress` write log row with
  cause **`manual`** and the previous value. `sync._guard` is **unchanged**:
  it refuses a lowering progress write whose cause is `watch` and allows an
  explicit edit, so the exception is a property of the row rather than of the
  endpoint, and `tests/test_mal_guard.py` still holds the set of modules that
  may write such a row to four events. `watched_source` absorbs the
  actionability question — `arc` at or above the progress, `progress` below it
  — rather than gaining an `unwatchable` sibling, because the client asks one
  question and a boolean that always tracks another field eventually does not.
  Rejected: lowering to N−1 on *any* un-mark (it would assert something about
  the episodes in between that nobody said) and rolling the auto-completed
  status back with it (the mirror of the argument that lets Arc set it: the
  word is the viewer's). The second decision is the existing behaviour made
  explicit and tested — `on_hold` and `dropped` complete too.
- 2026-09-13 — **The current week's grid is a calendar** (FR-C3, M16 batch 2).
  The schedule's membership rule was one query — the rows tagged with the
  season being viewed — and that is the wrong question for the current week: a
  two-cour show carries the season it *started* in, so Slime Season 4 (episode
  23 on Friday 2026-09-18, `SPRING 2026`) was nowhere on the Summer grid. The
  current season now merges a second, bounded query, `airing_this_week` —
  `RELEASING`, weekly format, an air time within 7 days from the cached
  `next_airing` blob or from `episodes.air_at` — with the season's own rows.
  Prev/next views are left alone, because they are a catalogue browse and
  "what is on this week" is meaningless three seasons back. The two statements
  live in `arc/services/catalog/schedule.py` beside the placement rule (the
  module's promise is now "pure functions, statements built and never
  executed"), so membership and placement are read in one place and the router
  stays a session. Rejected: filtering the season query by air time instead
  (it would drop the season's announced and finished rows, which the page is
  also for); rewriting `anime.season` on a continuation (the catalogue's fact,
  and the show page and the recommender both read it); and deriving the caveat
  client-side by comparing the row's season with the page's (it answers the
  wrong question for a row with no season, and the server already knows which
  rows it carried in — hence `ScheduleEntry.carried_over`).
- 2026-09-13 — **The show page asks for its own episode stills** (§5.8, M16
  batch 2). Stills come only from the TMDB enrichment, and the three things
  that asked for one on demand were Watch Now's shelves, the sample button and
  the nightly sweep — none of which reaches a series the viewer neither
  follows nor has a file for. Opening its page showed fourteen striped
  placeholders and asked nobody anything (owner, on several series pages).
  `GET /api/anime/{id}` now queues the same `enqueue_show_enrichment` the
  sample route does, behind the same three gates (a `TMDB_API_KEY`, the id map
  reaching the show, a hole left to fill), in the commit the route already
  makes, and waits for nothing. It also answers `tmdb_mapped`, from one lookup
  it needs anyway: the client polls the detail every 5 s for 30 s while a
  *mapped* show has an aired episode with no still — so the pictures the open
  just queued arrive without a reload — and never polls an unmapped one, and
  the episode list carries one muted line, "No episode pictures for this
  show", exactly where `tmdb_mapped` is false or `/api/health` reports no key.
  Rejected: filling the rows with the show's own art (the same banner cropped
  fourteen times is fourteen pictures of nothing, which is why `stillArt` has
  no fallback); waiting on the enrichment inside the request (three TMDB round
  trips in a page load, to save a five-second poll); polling on a count of
  tries rather than a window (the same half-minute, with state to keep); and
  letting the client derive "no pictures" from a null still alone, which is
  true of every unaired episode and of every show whose enrichment has simply
  not run yet.
- 2026-09-13 — **The site updates itself when episodes change state** (§5.9,
  M16 batch 2). A signed-in tab learns about the shows that matter to it
  without a manual refresh: a new "Ready to watch" tile, a row flipping to
  Ready or Downloading, a still arriving. No notifications and no sound, and
  **polling stays** — every interval in `anime.ts` is untouched, so the stream
  is an improvement on the latency and never the only way a page learns
  something. Postgres `LISTEN`/`NOTIFY` for the source, because the write is
  in the worker and the tab is on the api: an in-process bus cannot cross that
  and the database is the one thing both already hold a connection to. The
  `pg_notify` is issued **inside the committing transaction** (staged by
  `publish()` on the session, emitted by a `before_commit` listener), so an
  event exists if and only if the write does — which is also what lets the
  synchronous `transition()` publish at all. Fan-out is `GET /api/events`,
  server-sent events behind the ordinary session dependency, one asyncpg
  `LISTEN` connection per api process built lazily and closed by the lifespan.
  Payloads are ids only (`kind`, `anime_id`, `episode_id`, `state`, `ts`): a
  notification reaches every listener, so the client re-asks the endpoints it
  already had rather than being told anything. Rejected: WebSockets (a
  dependency and a protocol for a one-way feed of five fields); a message
  broker (a fifth service on a one-box deploy, for events that must not
  outlive the transaction that caused them); per-user filtering on the server
  (a query per event per stream, to answer a question the client's own cache
  already answers); patching the caches from the event instead of invalidating
  (the client guessing at "is *this* viewer behind", which only the server
  knows); and polling faster, which is what this replaces.
- 2026-09-13 — The Nyaa query builder also asks in the **`SxxEyy` form**, and
  `MAX_QUERIES` rises 6 → 8 so the two new forms do not push a marked entry's
  season short forms off the end (§6, FR-A4). The evidence: *One-Room TA*
  (AniList 205068, romaji *Wollum Jogyonim*, 7 episodes, `FINISHED`) was set to
  watching on production; episodes 1 and 2 went `searching`, all five query
  forms returned 0 results, and the six-hour retry would have repeated that
  forever. Nyaa has the show — `[ToonsHub] One-Room TA S01E02 1080p VIKI
  WEB-DL …`, `[ToonsHub] One-Room TA S01E07 …` and five more singles — named
  the Western way. By hand the same day: `One-Room TA - 01` → 0,
  `One-Room TA 01` → 0, `One-Room TA` → 9. Nyaa ANDs every word of a query and
  `01` is not a word of `S01E01`, so this is a third way the existing forms
  miss: `_short_forms` covers the case where the **season marker** diverges,
  the head forms the case where the **subtitle** does, and this the case where
  the **episode number's own notation** does. The form is `<season-stripped
  base> S<kk>E<nn>` for both the romaji and the english title — so *Mushoku
  Tensei III: Isekai Ittara Honki Dasu* asks `Mushoku Tensei S03E11` and
  *One-Room TA* asks `One-Room TA S01E01` — placed third and fourth, in front
  of the short and head forms, because a show whose groups use this notation
  has nothing at all under the two forms ahead of them. The episode is padded
  to two digits rather than through `pad()`: `SxxEyy` is a scene convention
  with its own width and One Piece is `S01E1089`, never `S01E089`. Nothing in
  the filter changed and nothing needed to: the parser already read `One-Room
  TA S01E07` as episode 7 of season 1 with `title_key` `one room ta` (the
  corpus gained all four production names and stands at 251 at 100 %), the
  0.90 asymmetric `title_score` scores the `(Wollum Jogyonim, Multi-Subs)`
  parenthetical at 1.00 because the extra tokens are the entry's *own* other
  title, and both of the batches Nyaa also carries — `S01E01-03` (a range) and
  `- S01 … [BATCH]` (a season pack) — are rejected as batches before their
  episode number is compared. No `PREQUEL` gate is needed on these forms,
  unlike the head forms: the base is not the head, so a subtitled sequel is
  asked for by its whole name, and where the base *is* bare the form carries
  the season explicitly, which is what the season-agreement check reads.
- 2026-09-14 — **Matching robustness** (M16 batch 2, FR-A3, FR-A4, FR-A7,
  §5.2a, §6, §10). Six changes and one column, all of them from a day of
  production logs rather than from a review of the code.
  (1) **A film is not episode 1 of anything, and a series pack is not a
  film.** `queries()` asked Nyaa for
  `Servamp Movie: Alice in the Garden - 01`, which is a query with no answer,
  and three films — that one, *The Royal Tutor Movie* and *Sword Art Online
  the Movie: Progressive* — sat in `searching` for a day. A `MOVIE` entry, or
  an OVA/ONA the catalogue gives one episode (`nyaa.is_single`), is now asked
  for by **bare title** and filtered as "is this one release of this title":
  the parser's `movie` kind, or no episode number and not a batch, accepted as
  episode 1. The **filter** half of it took two attempts: "no episode number
  and not a batch" is not a claim to be a film, and three whole-series Blu-ray
  packs (`[Judas] Sword Art Online [BD 1080p]`, `[Coalgirls] Servamp (1920x1080
  Blu-ray FLAC)`, `[Coalgirls] Kizumonogatari [BD 1080p]`) said nothing at all
  and were accepted as one — each with a *shorter* title than the entry's,
  which the ordinary asymmetric comparison scores 1.00. So a single now
  requires the parser's `movie`/`special` kind **and** a strict title
  comparison that forgives only the type word, and the year — which most
  releases do not carry — is a bonus check rather than the margin. `episodes ==
  1` guards the OVA half only: a film is a single whatever its count, an OVA
  *series* of four is four numbered releases like any other show. The cost is a
  BD rip that names nothing but its franchise, which is refused; a missing file
  is visible and fixable and the wrong film plays as though it were right.
  (2) **Every full title gets a symbol-stripped variant, at the end.** Nyaa
  ANDs the *tokens* of a query, so `Yarichin☆Bitch-bu` is one token nothing on
  the site holds and `Love Live! Superstar!!` is two that few uploads write;
  `☆ ★ ♪ ♥ ! ? : ; ~ 〜 ～ · ・ — /` each become a space and the runs collapse.
  `MAX_QUERIES` rose 8 → 10 for them. **Where** they sit was the review's
  correction: the first version put each variant immediately behind its own
  form, which reads well and spends the budget on guesses — *Kimetsu no Yaiba:
  Katanakaji no Sato-hen 2nd Season* builds fourteen forms for ten slots and
  lost all four romaji short forms, which are names the catalogue actually
  holds, to variants of the full titles, which are guesses about spelling. So
  the list is now ordered by *how likely a group wrote it*: romaji full,
  english full, romaji `SxxEyy`, romaji short forms, head forms, bare romaji,
  then the english `SxxEyy` and short forms, then the variants of the two full
  titles, then synonyms — and the cap cuts the speculative end. Variants are
  built for the full titles only, for the same reason. The slash is in the
  symbol set although the owner's list did not name it, because a franchise
  written with one is written both ways and the corpus case made it obvious;
  the ordinary hyphen is not, since it is what separates the number from the
  title.
  (3) **A slash is a title character in the parser too** (§5.2a). This was the
  worse half of the same bug: `[SubsPlease] Fate/Zero - 12` parsed as a show
  called `Zero` with no release group, so the 0.90 title filter rejected every
  release of the show and the library sent every file of it to review. The
  first fix *inferred* which kind of slash it was looking at; the review was
  right that an inference here is a bug waiting to happen, so `parse()` takes a
  **`path` flag** and the caller — the only thing that knows — says. Three call
  sites: ingest and the match job pass `path=True`, the Nyaa filter does not.
  (4) **A dub ranks below every subbed candidate** (FR-A3) and sorts *ahead*
  of all four of FR-A3's rules, because a dubbed release is not a worse copy of
  the episode but the episode in the wrong language — and both of the releases
  production picked yesterday (`[Yameii] … [English Dub]` for *SAO*,
  `[KaiDubs] …` for *BOFURI*) won on seeders, which is the third rule doing
  exactly what it says. A ranking and not a filter: when nothing else was
  found, a file somebody can watch beats fourteen days of `searching`, and
  `_pick` logs the sentence when it happens. `Dual Audio` is not a dub — it has
  the original track — and a group whose name *ends* in "dub"/"dubs" is one,
  which is the only thing `[KaiDubs]` ever says about its audio.
  (5) **Up to two synonyms** earn a query of their own, last against the cap,
  and only when neither the synonym nor its own head repeats something already
  asked for. manami's vocabulary is what groups write; the rest of the list is
  a dozen transliterations of the same three words — and, as the review
  noticed, entries like `"Season 2"`, `"Part 2"` and `"2"`, a query for the
  last of which is a query for a quarter of Nyaa. So a synonym also has to be
  a *name*: two words or six characters, and never a bare season marker. The
  floor drops a short native-script synonym too (`進撃の巨人` is five
  characters), which is the same decision the builder already makes about
  `title_native` — one of the names the *filter* compares against, and not one
  Arc asks the english-translated category for.
  (6) **The show page says what the search did** (FR-A7): three nullable
  columns on `episodes` — `last_search_at`, `last_search_forms`,
  `last_search_results` — written on every attempt that reaches Nyaa, and an
  `EpisodeOut.search` object whose `next_at` is the pending `search_release`
  job's `run_after`, read in the same per-page pass as the torrents and
  renditions. The row reads "Searching · 6 forms, 0 results · next try 23:26",
  time in the viewer's zone and 24-hour like the schedule's own, with the same
  sentence as a tooltip; an `unavailable` row keeps its reason instead. The
  pair of counts is the whole point: **zero results is a query problem and a
  full pool with nothing kept is a filter problem**, and a row that says only
  `Searching` for six hours cannot tell an owner which he is looking at. It is
  written even when the episode then goes `unavailable`, which is where
  somebody is most likely to read it, and not at all by a paused or
  storage-held run, which asked nothing.
  And (7) the claims behind all of this now live in
  `tests/fixtures/query_corpus.txt` (§10) rather than in the shape of one unit
  test each: seventeen real cases, offline, each naming the forms that must be
  built, the forms that must not be, and the releases that must be accepted and
  rejected — which is also what caught the series packs of (1) and the
  ordering of (2). **Absolute episode numbering is still out of scope** and is its own
  roadmap item: episode 40 of a two-season franchise really is episode 15 of
  the sequel, and working that out needs the relation graph the matcher walks.
- 2026-09-17 — **The player's end-of-episode flow is two moments, not one**
  (§5.4a, FR-S5, M16 batch 3). The next-episode overlay was raised by
  `newly_completed` — the 90 % completion — so on a 24-minute episode it
  blacked out the picture and asked what to watch next with two and a half
  minutes still to run. Splitting it costs one derived boolean and one
  remembered "no": the completion keeps the client reaction it deserves, a
  four-second `role="status"` pill saying "Marked as watched" with nothing to
  press, and the card moves to where the decision actually is — 1:30 left, or
  the media ending. The card is a panel over a gradient rather than a wash,
  because the episode is still playing underneath it, and `pointer-events-none`
  on the scrim keeps the click-to-pause gesture working through it. Keep
  watching (and Escape) holds for the rest of that playback and no longer; the
  page is keyed on the episode id, so nothing carries into the next one. **No
  server change and no change to FR-S4**: `newly_completed` is still the
  server's once-only answer, which is also why a rewatch says nothing without
  the client owning a rule about rewatches. Nothing auto-advances — the next
  episode stays something the viewer asks for. The ⟲10 / ⟳10 glyphs were
  redrawn in the same pass: one rewind, mirrored, with the digits outside the
  mirrored group.

- 2026-09-17 — **The schedule is three days wide** (FR-C3, owner, M16 batch
  3; client-only, `pages/Schedule.tsx` and two pure helpers in
  `lib/schedule.ts`). Seven columns of the 1180px measure are 158px each, and
  since the current week started carrying every airing show (2026-09-13) that
  was a page of abbreviations. Three columns, starting on today, with chevron
  arrows — the hero's own `GLASS_CIRCLE` — at the ends of the day-and-date
  bar, and left/right arrow keys doing the same from anywhere in the group
  except inside a field, where the arrows belong to the `<select>`. **The API
  did not change**: the whole Monday–Sunday week still arrives in one
  response, the window is a slice of it, and the arrows stop at its ends
  rather than asking for an eighth day. Two things are worth writing down.
  (1) The page keeps an **offset from today**, not a start index, because the
  timezone arrives with the response: a start pinned on the first render would
  be Monday for ever, and an offset lands on today the moment the answer does
  and follows it past midnight on the tick that already existed. (2) `useToday`
  became `useClock` and returns the weekday and the week's dates together —
  they answer the same question and two intervals would tick apart. Dates are
  read in the schedule's zone (at 23:00 UTC on a Sunday a Tokyo viewer is
  already in next week), computed at noon UTC so a clock change cannot move a
  day, and composed as "17 Sep" because `en-GB` spells September "Sept", which
  is a character wider than every other month in a heading that has to line up
  three times across. (3) **A browsed season has no dates and no today**
  (owner, 2026-09-17, correcting the first build): only the live season's grid
  is a real week — it is the one the server fills with every show on air —
  and this week's dates printed over Spring 2026's shows would be a claim
  about when they air. The response does not say whether the season on screen
  is the current one, so `currentSeason`/`isCurrentSeason` repeat
  `services/catalog/seasons.py`'s quarter arithmetic in UTC and the page
  compares seasons rather than reading the URL, which keeps a pinned
  `?year=2026&season=FALL` in Fall 2026 the live week. The list-status control
  keeps its `lg:hidden` rule: the columns are roomier, not infinite, and the
  design's answer for the wide grid is still one tap on the show page.
- 2026-09-17 — **Two Watch Now shelves stop borrowing another shelf's answer**
  (§5b, §5.8, spec FR-W1/FR-W5, owner, M16 batch 3; both found on production).
  The bug is the same twice: a shelf derived from a neighbour's data inherits
  the neighbour's `WHERE` clause.
  **Ready to watch** was `home.new_this_week.filter(ready && !watched &&
  !started)` in `Home.tsx`, so the shelf silently also required "aired in the
  last seven days" — One-Room TA (AniList 205068) finished airing on
  2026-08-27 with two episodes ready, and the page could not show either. It
  is now `services/catalog/progress.ready_to_watch`: `episodes.state = ready`,
  the entry in watching/planned/**on_hold**, `NOT EXISTS` a `watch_progress`
  row past `RESUME_MIN_S` or completed, and `episodes.number >
  list_entries.progress` — FR-W5 read backwards, so the schema's `completed`
  argument is a constant `False` rather than a third round trip. Ordered by
  `renditions.ready_at DESC NULLS LAST, episodes.id DESC` (the *file's* clock,
  since the shelf is about the file) and capped at 20, on a new
  `ready_to_watch` array. The 10 s floor is deliberately looser than continue
  watching's 30 s: `RESUME_MIN_S < CONTINUE_MIN_POSITION_S`, so the two shelves
  can never both offer one episode, and the client's own exclusion by
  `continue_watching` id is gone rather than duplicated. On-hold is in this
  set where `NEW_STATUSES` excludes it — a paused show is exactly the one "the
  file is here" might restart, and it costs one tile, not a week of slots.
  **The This-week tick** was `new_this_week.find(entry => entry.anime.id ===
  id).episode.watched` — an answer about that show's newest aired episode,
  drawn beside a card naming the *next* one, which is why Friday's Slime slot
  read "Episode 23 ✓ Watched" on a list standing at 22. `ScheduleEntry` now
  carries `watched: bool | null`, filled by `api/schedule.watched_marks` only
  for slots whose `next_at` is already past: three queries (the episode ids for
  the `(anime_id, number)` pairs, then `completed_episode_ids` and
  `list_progress_for`) and none at all in the ordinary case, where every slot
  is in the future. `watched_source` is still the only implementation of
  FR-W5. Rejected: computing the tick client-side from `new_this_week` matched
  on episode *number* (it works only while the slot is inside the seven-day
  window, which is a coincidence of `STALE_NEXT_AIRING` being the same seven
  days), and adding `list_progress_for` unconditionally to the schedule route
  (a season is two hundred shows and the answer is wanted for one of them).
  Also this pass: episode art gains its fallback chain (still → `backdrop_url`
  → key visual framed in the 16:9 slot) on both the show page's rows and
  Home's tiles, and the "No episode pictures for this show" caption is gone —
  see §5.8. `PosterWash` gained a `radius` prop and a documented
  `ground={null}` form so the show page can letterbox without the blurred
  ground a fifty-row list would pay for fifty times.
- 2026-09-17 — **Absolute episode numbering on sequels** (FR-A4, M16 batch 3;
  §5.1a, §6, §10). Built as an *offset read off the catalogue*, never an
  inference from the release in front of it: `nyaa.absolute_offset` sums the
  episode counts of the entry's `PREQUEL` chain through cached `anime` rows and
  answers `None` — declining the whole feature for that entry — at the first
  thing it cannot be sure of (an uncached prequel, a null count, a prequel that
  has not finished airing, two countable prequels at one hop, a loop, more than
  ten hops, a format it cannot classify). Films and OVAs in the chain are skipped rather than counted, since
  no group counts *Jujutsu Kaisen 0*, and the walk stops at one rather than
  reaching past it. With an offset the search asks two more forms (`Jujutsu
  Kaisen - 25`, `Jujutsu Kaisen 25`, behind the romaji short forms) and accepts
  a release numbered `N + offset` **only** when it names no season, and never
  when a season-marked release for the same episode came back in the same
  search — an explicit answer beats an inferred one, which is a rule about a
  result *set* and therefore lives in `filter_items` rather than `acceptable`.
  Three shapes were rejected. Resolving the offset inside `acceptable` from the
  numbers alone ("25 > 23 episodes, so subtract"): that is the guess the
  2026-09-14 entry refused, and it turns a missing file into a wrong one.
  Reusing `matcher.offset_candidates`: it is the *ingest* side, one hop, over a
  candidate pool, scored with a penalty — a different question with different
  inputs, and coupling them would make each one's fix the other's regression.
  And giving `nyaa` a database session for the walk: the resolver is injected
  instead (`jobs._prequel_offset` supplies the SQLAlchemy one), which keeps
  every function in that module a pure function of an `anime` row and a release
  name — the property the offline query corpus is built on.
