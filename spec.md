# Arc — Project Specification

> Living document. Update whenever scope, behaviour, or a decision changes.
> Last updated: 2026-09-09. Companions: [architecture.md](architecture.md), [roadmap.md](roadmap.md), [CLAUDE.md](CLAUDE.md).

## 1. Summary

Arc is a self-hosted, multi-user anime server with a browser client. The
server owns a shared episode library: it acquires episodes from Nyaa based on
what users are watching, matches messy release filenames to real titles,
prepares each episode for browser playback ahead of time, streams it, tracks
per-user progress, and keeps each user's MyAnimeList (MAL) list in step with
what they actually watched. Catalogue data and the airing schedule come from
AniList. Recommendations are produced by an LLM with a short argued case per
pick.

The client is the face: continue watching, what you are behind on, the
seasonal schedule, a show page, an in-browser player, search/add, and (phase
2) recommendations, match review, and admin.

The site will be hosted publicly so that the course professor can log in at
any time, and the owner uses it daily.

## 2. Users and roles

- **Multi-user from day one.** Every user has their own watch history, list
  states (watching / planned / dropped / completed / on hold), MAL link,
  recommendations, and acquisition wants.
- **Auth:** email + password. Registration is invite-only: an admin creates an
  invite link; the invitee sets a password. No open sign-up.
- **Roles:** `admin` and `user`.
  - Admin: manage users and invites, edit acquisition rules (preferred
    groups, resolution, look-ahead N), edit retention settings, view storage
    and job queues, resolve match-review items, delete files.
  - User: everything else (watch, track, add shows, request acquisition via
    their own list, link MAL, get recommendations, resolve match-review
    items — any signed-in user may resolve any item in phase 1; scoping to
    the requesting user is an M14 admin-panel refinement).
- Each user has a timezone (detected at signup from the browser, editable
  from the Schedule page); schedule and air dates render in it.
- Sessions: HTTP-only secure cookie sessions. Passwords hashed with Argon2.

## 3. Domain model (conceptual)

- **Anime** — a title with an internal id and external ids as known
  (AniList id, MAL id; at least one present), titles in romaji/english/native, format, episode count, season, airing status, cover,
  genres, tags, studio, relations, next-airing episode). Cached locally and
  refreshed periodically.
- **Episode** — belongs to an Anime; number, title, air date (from AniList
  airing schedule), and a state machine (see §6).
- **MediaFile** — a downloaded source file on disk, linked to an Episode once
  matched. Holds parse results and match confidence.
- **Rendition** — the browser-ready, transcoded output for an Episode (HLS
  playlist plus segments, subtitles burned in). One per episode.
- **User**, **Invite**, **Session**.
- **ListEntry** — (user, anime) → status, progress (episodes watched), score,
  source of last change (arc / mal), MAL sync state.
- **WatchProgress** — (user, episode) → position seconds, duration, completed
  flag, updated at.
- **MalLink** — per user: OAuth tokens, MAL username, last import/sync time.
- **MalWriteLog** — every write Arc made to MAL: what, when, why, previous
  value, result. Append-only.
- **AcquisitionWant** — (user, episode) derived interest; drives what to
  fetch. Computed from the user's list status and progress (see FR-A1).
- **Torrent** — a Nyaa release chosen for an Episode: magnet/info hash,
  group, resolution, seeders at pick time, qBittorrent state.
- **Job** — background unit of work (scan, match, transcode, mal-sync, fetch,
  cleanup) with status, attempts, error, timestamps.
- **RecommendationRun** — (user, prompt) → list of picks with argued cases,
  the candidate pool used, model, timestamp.

## 4. Functional requirements

### 4.1 Catalogue and schedule (AniList)
- FR-C1 Search AniList by title from the client; show results with cover,
  year, format, episode count.
- FR-C2 Add an anime to the user's list in any status. Adding creates or
  refreshes the local Anime record.
- FR-C3 Seasonal schedule: for the current season (and prev/next), list shows
  grouped by weekday with air time in the user's timezone. Shows the user
  follows are highlighted. "Add to planned / watching" from the schedule.
- FR-C4 Per followed show, know which episodes have aired and how many the
  user has not watched ("behind by N").
- FR-C5 Refresh airing data at least daily and within one hour of a followed
  show's scheduled air time.
- FR-C6 Catalogue fallback: when AniList is unreachable, disabled, or
  failing, search, show pages, list changes, and the season view keep working
  from the MyAnimeList official API (read-only, client-id auth). Air dates
  obtained that way are synthesised from the broadcast slot and marked as
  estimated in the UI until AniList data replaces them. Shows are identified
  internally, with AniList and MAL ids attached as they become known, so a
  show first seen through MAL is the same show once AniList returns.
- FR-C7 The current season's catalogue is pre-cached daily so the schedule
  survives an outage of both sources.

### 4.2 Acquisition (Nyaa via qBittorrent)
- FR-A1 For each user and each show in status `watching` or `planned`, the
  server keeps the next **N** unwatched episodes available (N configurable
  by admin, default 2), counted from that user's furthest watched episode.
  For airing shows, the newest aired episode is fetched on its air day.
  Nothing outside this window is fetched.
- FR-A2 Wants from all users are merged: an episode is fetched once even if
  several users want it.
- FR-A3 Release selection uses ranked, admin-editable rules:
  1. preferred release groups (ordered list),
  2. preferred resolution (default 1080p, fallback 720p),
  3. seeders (more is better),
  4. Nyaa "trusted" flag.
  Per-show overrides for group and resolution are allowed.
- FR-A4 Nyaa is polled via its RSS search feed with a query built from the
  show's titles and episode number. Results are parsed with the same filename
  parser used for the library. Candidates whose parsed title/episode do not
  match are discarded.
- FR-A5 Chosen magnets are added to qBittorrent with a per-episode category
  and save path; the server polls completion and hands the file to the
  library pipeline.
- FR-A6 If no acceptable release exists yet, retry on a backoff schedule
  (every 30 min on air day, then every 6 h). After 14 days flag the episode
  "unavailable"; while someone still wants it, retry once a day (cheap: a
  few RSS requests) and stop only when the want goes away. If a downloaded
  file turns out not to be the episode (ignored in review), the episode is
  flagged unavailable the same way.
- FR-A7 Users can see the acquisition status of each episode on the show page
  (wanted, searching, downloading with %, preparing, ready, unavailable).

### 4.3 Library indexing and matching
- FR-L1 The server watches the download directory and an optional "manual
  drop" directory; new video files create a MediaFile and a match job.
- FR-L2 Filename parsing extracts group, title, season, episode, resolution,
  source, codec, and version (v2 etc.). Parser is deterministic
  (anitopy-style).
- FR-L3 Matching resolves the parsed title against AniList (local cache first,
  then AniList search) and computes a confidence score. Files downloaded by
  Arc for a known episode start with a strong prior for that episode.
- FR-L4 Confidence ≥ high threshold → auto-link. Below it → the file goes to
  the **match-review queue** with the top candidates and is not played until
  a user confirms. The server must say when it is unsure instead of guessing.
  Auto-link additionally requires the title itself to match closely (exact
  normalised title, or similarity above a configurable floor) — a strong
  overall score built on a loose title is still a guess. Movie files match
  as episode 1 of a movie entry.
- FR-L5 For unsure cases an LLM may be asked to propose the most likely
  candidate with a one-line reason; the proposal is shown in the review
  queue, never auto-applied.
- FR-L6 Review UI: confirm a candidate, search for a different title, set
  episode number manually, or mark as "not anime / ignore".

### 4.4 Playback preparation
- FR-P1 As soon as a MediaFile is matched to an episode, a transcode job runs.
  Every file is fully transcoded to H.264 + AAC with the chosen subtitle
  track **burned in**, and packaged as HLS (fMP4 segments, ~6 s).
- FR-P2 Subtitle track choice: prefer the first English (or configured
  language) text track; fall back to no subtitles and flag the episode.
  Audio track choice: prefer Japanese; configurable.
- FR-P3 Preparation is scheduled to finish before the user is likely to ask:
  new downloads are transcoded immediately; the job queue prioritises
  episodes users are closest to reaching.
- FR-P4 Episode state visible to users: `preparing` (with %), `ready`,
  `failed` (with retry).
- FR-P5 The original source file is kept until retention removes it (so a
  transcode can be redone with different settings).

### 4.5 Streaming and player
- FR-S1 The client plays HLS in the browser (hls.js; native HLS on Safari).
  Playlists and segments are served by the server behind auth.
- FR-S2 Resume: on open, the player seeks to the user's last position if it
  is > 10 s and < 95 % of duration.
- FR-S3 Progress is reported every 10 s while playing and on pause/seek/close.
- FR-S4 An episode counts as **watched** when position ≥ 90 % of duration.
  This sets WatchProgress.completed, advances ListEntry.progress if this
  episode number is greater than current progress, and enqueues a MAL sync.
  If the show is not on the user's list, watching it adds it as Watching
  (the act of watching is the user's choice); status is never changed
  automatically beyond that, and un-marking a watched episode never lowers
  list progress or triggers a MAL write.
- FR-S5 Next-episode: at the end of an episode, offer the next one if ready.
- FR-S6 Keyboard shortcuts: space, arrows (±5 s), f fullscreen, m mute.

### 4.6 Watch tracking and list states
- FR-W1 Home shows **Continue watching** (episodes started but not completed,
  most recent first), **Behind on** (followed airing shows with unwatched
  aired episodes), and **New this week** (episodes that aired in the last 7
  days for followed shows, with ready/preparing state).
- FR-W2 Users can set a show to watching / planned / on hold / dropped /
  completed, and set a score (1–10) from the show page.
- FR-W3 Marking an episode as watched manually is allowed (e.g. watched
  elsewhere) and is treated the same as FR-S4.
- FR-W4 Dropped, completed and on-hold shows generate no acquisition wants.

### 4.7 MyAnimeList sync
- FR-M1 Each user links their own MAL account via OAuth 2.0 (PKCE). Tokens
  are stored encrypted and refreshed automatically.
- FR-M2 On link, the user's full MAL list is **imported** as the baseline:
  status, progress, score. Arc list entries are created/updated; MAL is
  authoritative for anything Arc did not change.
- FR-M3 Periodic re-import (default every 6 h) pulls MAL-side changes. If an
  entry changed on both sides since last sync, the more recent change wins
  and the conflict is logged.
- FR-M4 Arc writes to MAL only for changes a user made in Arc: episode
  completed (progress), status change, score change. Progress written to MAL
  never decreases as a result of automatic watch events.
- FR-M5 Every write is recorded in MalWriteLog with the previous value. A
  user can view their log and revert an entry, which writes the previous
  value back (and logs that too).
  The log also records, as non-writes, conflicts where MAL's newer change
  overrode an Arc change (`conflict`/`skipped`) and writes that could not
  apply (no MAL id).
- FR-M6 Writes are idempotent and retried with backoff; failures surface as a
  badge on the show and in the user's sync page.
- FR-M7 "Never write a change I did not make": there is no code path that
  writes to MAL except via a user-originated event or an explicit revert.

### 4.8 Recommendations (phase 2)
- FR-R1 Recommendations page with an optional free-text mood prompt ("something
  short and funny", "like Mushishi").
- FR-R2 The server builds a candidate pool (≤ 40) from the cached catalogue:
  current and next season ranked by popularity, popular/well-scored shows in
  the user's top genres, and relations of the user's completed shows.
  Excludes anything in the user's list except `planned`, recaps/specials/
  shorts, and direct continuations of listed shows (those go to FR-R6).
  Revised 2026-09-10 after a three-model comparison showed pool quality, not
  the model, limited the picks.
- FR-R3 The server sends the user's history summary (top-rated, recently
  completed, dropped with reasons if any), the prompt, and the candidate pool
  to a language model (Gemini's free tier by default; Claude or OpenRouter by
  configuration) and asks for 3–5 picks, each with a **short argued case**
  (2–4 sentences) that references the user's actual history.
- FR-R4 Output is structured (JSON schema) so the client can render picks with
  covers and one-click "add to planned".
- FR-R5 Runs are stored so the page is instant on reload; pressing "Get picks"
  with the box unchanged is the refresh. Rate limit: 10 runs per user per day.
- FR-R6 "New in your franchises": alongside the picks, a deterministic list
  (≤ 8, no model call) of sequels, movies, side stories and spin-offs of shows
  on the user's list that the user has not added, each with a one-line reason
  ("Sequel to X, which you completed"). Added 2026-09-10 (owner decision):
  continuations are worth surfacing but are not the main recommendation.
- FR-R7 The model is behind a provider chain: Gemini free-tier models in
  rotation with a daily-quota cooldown, then a paid OpenRouter fallback
  (`openai/gpt-5-mini`); Anthropic selectable. Every run records which model
  answered.

### 4.9 Retention and cleanup
- FR-T1 An episode's files (source + rendition) are deleted when **all** of
  the following hold: every user who wanted the episode has completed it, and
  a grace period of **G** days has elapsed since the last completion
  (default G = 7).
- FR-T2 Additionally, if a user who wants the episode has not watched it
  within **D** days of it becoming ready (default D = 21) — counted from
  the later of the episode becoming ready and the user's last action on
  that show, so a show the user just came back to is not dropped by the
  next tick — that user's want is dropped for that episode, so a
  dropped-in-practice show does not pin files forever. A dropped want is
  revived only when the user acts on the show again in Arc. When no wants
  remain for any reason (watched past, show set to on hold / dropped /
  completed, removed from the list, or dropped as stale), FR-T1's grace
  applies from that moment; a want never disappears without leaving a
  grace anchor behind.
- FR-T3 Deleting files resets the episode to "not acquired"; if a user later
  rewinds or a new user wants it, it is re-acquired.
- FR-T4 Admin can see disk usage and manually delete or re-fetch.
- FR-T5 All of G, D, N are admin-configurable.

### 4.10 Admin (phase 2 UI; the underlying settings exist from phase 1 via config)
- FR-D1 Users & invites; deactivate a user.
- FR-D2 Acquisition rules (groups, resolution, N), retention (G, D), subtitle
  and audio language preferences.
- FR-D3 Job queue view with retry/cancel; qBittorrent status; disk usage.
- FR-D4 Match-review queue across all users.

## 5. Client pages

| Page | Phase | Contents |
|---|---|---|
| Login / accept invite | 1 | Email + password; invite token flow |
| Home | 1 | Continue watching, Behind on, New this week |
| Schedule | 1 | Weekday grid for the season; prev/next season; add-to-list actions |
| Search / add | 1 | AniList search, add to list in a status |
| Show | 1 | Cover, synopsis, list status + score controls, episode list with acquisition/prep state and watched marks, play buttons |
| Player | 1 | HLS player, resume, next episode, progress reporting |
| MAL link / sync log | 1 | Connect MAL, view write log, revert |
| Recommendations | 2 | Mood prompt, picks with argued cases, add-to-planned |
| Match review | 2 | Queue of unsure files with candidates and LLM suggestion |
| Admin | 2 | Users/invites, rules, jobs, disk, review queue |

The client is responsive; primary target is desktop, but phone layout must be
usable for the home page and player.

## 6. Episode lifecycle

```
not_wanted → wanted → searching → downloading → downloaded
   → matching → (review) → matched → preparing → ready
ready → (retention) → deleted → not_wanted
any of searching/downloading → unavailable (after retry window)
preparing → failed → (retry) → preparing
```

## 7. Non-functional requirements

- **Correctness of MAL writes** is the top priority: no write without a
  user-originated event, all writes logged and revertible.
- **Ahead-of-time readiness:** for an airing show a user follows, the episode
  should be `ready` within 2 h of a suitable release appearing on Nyaa, on
  the intended host hardware.
- **Security:** all API and media routes require a session; media URLs are
  not guessable without auth; secrets (MAL tokens, model API keys) stored
  encrypted at rest / in env; invite tokens single-use and expiring.
- **Observability:** structured logs; every job records duration and outcome;
  admin page exposes queue depths.
- **Tests:** unit tests for the filename parser and matcher (with a corpus of
  real release names), the acquisition window logic, MAL sync rules (esp.
  "never write what I didn't change"), and retention; integration tests for
  the API with a test database; ffmpeg and qBittorrent mocked in CI.
- **Docs:** README with local run and deploy instructions; this spec and
  architecture.md kept current.

## 8. Out of scope (for now)

- Native mobile apps, Chromecast/AirPlay.
- Downloading whole seasons or a general "download anything" UI.
- Subtitle styling fidelity beyond burn-in; user-selectable subtitle tracks
  at play time.
- Per-user private libraries.
- Non-anime media.
- Notifications (email/push). Possible later.

## 9. Open decisions

| Topic | Status | Notes |
|---|---|---|
| MAL API client id | **Done 2026-09-06** | Reused from the owner's AnimeTrack app. Still needed for M9: the client secret and Arc's redirect URL on the MAL app config. |
| Hosting provider / budget | **Decided 2026-09-08**, revised 2026-09-09 | Hetzner Cloud, one account, Arc in its own project: CPX22 (2 vCPU, 4 GB, 80 GB NVMe, Helsinki; the cheaper CX33 was out of stock) + 100 GB volume + IPv4 + backups ≈ $33/mo, plus a WireGuard VPN (~$5/mo) that only the torrent client uses. Public host `arc.atomworks.dev`. Seeding is disabled (stop on completion). Legitimate future products may share the account in separate projects; the VPN keeps torrent traffic off the host's IP. |
| Legal/ToS | Owner's call, mitigated | Copyright notices reach the host via swarm monitoring; mitigations chosen: no seeding, upload capped, qBittorrent bound to a VPN with a kill switch, Arc isolated in its own project. Notices, if any, are the owner's to answer. |
| Retention defaults G=7, D=21 | Provisional | Admin-configurable; revisit after use. |
| Subtitle language default | Provisional: English | Configurable. |
| Hardware transcoding | Open | Depends on host. Software x264 `veryfast` preset assumed. |

## 10. Decision log

- 2026-09-05 — Stack: Python/FastAPI server, React/Vite/TS client, Postgres,
  qBittorrent sidecar, ffmpeg, a language model (Gemini free tier by default;
  Claude or OpenRouter selectable) for recs and match suggestions.
- 2026-09-05 — Multi-user from day one; shared library; per-user wants drive
  acquisition; email+password invite-only auth; admin role.
- 2026-09-05 — Every file is fully transcoded with subtitles burned in
  (owner prefers styling fidelity over transcode cost).
- 2026-09-05 — Acquisition window: next N unwatched (default 2); ranked
  release rules; qBittorrent via Web API.
- 2026-09-05 — MAL: import as baseline on link; auto-write at 90 % watched
  and on explicit state changes; full write log with revert.
- 2026-09-05 — Retention: delete when all interested users finished + G days;
  drop a user's want after D days unwatched.
- 2026-09-05 — Phase 1 pages: Home, Schedule, Show, Player, Search/add, MAL
  link. Phase 2: Recommendations, Match review, Admin.
- 2026-09-05 — Hosting deferred.
- 2026-09-06 — Catalogue fallback (FR-C6/C7): internal anime ids with
  AniList and MAL external ids; MAL official API as read-only secondary
  source when AniList is down; daily season pre-cache. Chosen after an
  all-day AniList outage; MAL official preferred over Jikan because it is
  first-party and its client id is needed for M9 anyway.
- 2026-09-06 — FR-A6 clarified: after the 14-day give-up, unavailable
  episodes are retried daily for as long as a want exists; a downloaded
  file rejected in review flags the episode unavailable.
- 2026-09-07 — FR-S4 clarified: completing an episode of an unlisted show
  adds it as Watching; un-marking never rolls back progress or MAL.
- 2026-09-08 — FR-T2 clarified after M10 review: the D window also runs
  from the user's last action on the show; wants that end because the show
  left watching/planned are dropped (not deleted) so the grace period is
  never skipped; revival needs an Arc-side action.
- 2026-09-10 — M14 as built: admin panel (users/invites, validated rules editor,
  jobs with retry/cancel and worker heartbeat, storage with retention preview
  and per-episode delete/re-fetch, acquisition with qBittorrent status).
  Per-show override editing deferred to M16.
- 2026-09-10 — M13 as built: match suggestions (FR-L5) ride the FR-R7 provider
  chain, can be asked for on demand from the review page, and are stored on
  the file with model and confidence; a failed re-ask never replaces a good
  suggestion. Review UI (FR-L6) shipped with pending/ignored/auto-linked tabs.
- 2026-09-10 — FR-R6 continuations section and FR-R7 provider chain added;
  FR-R2 pool revised (popularity/score ranking, recap and continuation
  exclusions). Owner decisions after the live three-model comparison.
- 2026-09-10 — Recommendations ship on Gemini's free AI Studio tier through its
  OpenAI-compatible endpoint; Anthropic (Claude) and OpenRouter stay selectable
  via `RECS_PROVIDER`. Owner's call: zero cost for the demo, same feature set.
- 2026-09-09 — Host revised to CPX22 + 100 GB volume: the CX line is out of
  stock at Hetzner; CPX32 costs double for headroom Arc does not use.
- 2026-09-09 — Public host is `arc.atomworks.dev` (owner's umbrella domain on
  Cloudflare; Sector Watch will get its own domain).
- 2026-09-08 — Hosting decided: Hetzner CX33 + 250 GB volume in its own
  project on the owner's account, seeding off, torrent client behind a VPN
  (~€30/mo all in). AWS rejected on cost (~5× for this workload) and
  torrent AUP risk; home-tunnel rejected because the Mac would have to
  stay on.
