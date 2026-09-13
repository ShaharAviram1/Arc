# Arc — Project Specification

> Living document. Update whenever scope, behaviour, or a decision changes.
> Last updated: 2026-09-13. Companions: [architecture.md](architecture.md), [roadmap.md](roadmap.md), [CLAUDE.md](CLAUDE.md).

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
- FR-C1 Search the catalogue by title from the client; show results with
  cover, year, format, episode count. The search order is **cached rows, then
  the offline catalogue, then the live source's page** (M15.5): the shows Arc
  already has are matched locally and listed first, the weekly-imported offline
  database answers next — by any of the thirty-odd names a show has been
  released under — and the live page follows. So a show is findable while the
  catalogue is down whether or not Arc has seen it before, and by any word of
  any of its titles rather than only from the start of one.
- FR-C2 Add an anime to the user's list in any status. Adding creates or
  refreshes the local Anime record.
- FR-C3 Seasonal schedule: for the current season (and prev/next), list shows
  grouped by weekday with air time in the user's timezone. Shows the user
  follows are highlighted. "Add to planned / watching" from the schedule.
- FR-C4 Per followed show, know which episodes have aired and how many the
  user has not watched ("behind by N"). Published air dates that the rest of
  the list contradicts are not believed: a finished show has no future
  episodes, and an episode dated after a later one is shown as an estimate and
  treated as having aired with its neighbour.
- FR-C5 Refresh airing data at least daily and within one hour of a followed
  show's scheduled air time.
- FR-C6 Catalogue fallback: when AniList is unreachable, disabled, or
  failing, search, show pages, list changes, and the season view keep working
  from the MyAnimeList official API (read-only, client-id auth). Air dates
  obtained that way are synthesised from the broadcast slot and marked as
  estimated in the UI until AniList data replaces them. Shows are identified
  internally, with AniList and MAL ids attached as they become known, so a
  show first seen through MAL is the same show once AniList returns.
  Extended 2026-09-11 (M15.5): a weekly-imported offline catalogue (the
  manami anime-offline-database plus Fribb's cross-id map) is consulted first
  for search, matching and id mapping, so those never depend on a live API;
  TMDB, reached through that id map, supplies key art, episode stills and
  credits when AniList has not, and never overwrites AniList-provided values.
  A record no live source has answered for carries a quiet "via offline
  catalogue" caveat wherever it is shown — the counterpart of "via MAL" — and
  that caveat disappears on its own once AniList or MAL fills the record,
  because the UI reads the record's current source and nothing else.
- FR-C7 The current season's catalogue is pre-cached daily so the schedule
  survives an outage of both sources.

### 4.2 Acquisition (Nyaa via qBittorrent)
- FR-A1 For each user and each show in status `watching` or `planned` **whose
  entry is not dormant** (FR-A9), the server keeps the next **N** unwatched
  episodes available (N configurable by admin, default 2), counted from that
  user's furthest watched episode. For airing shows, the newest aired episode
  is fetched on its air day. Nothing outside this window is fetched, and no
  more of it at once than FR-A10's per-user cap allows.
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
  flagged unavailable the same way. A download that makes no progress is
  given up on and rejoins the same schedule: a torrent still asking for its
  metadata after `STALL_METADATA_MINUTES` (default 60), or one that has
  fetched nothing — or that the tracker says has no swarm left — after
  `STALL_NO_BYTES_HOURS` (default 6), is removed from the client with its
  files and the episode is flagged unavailable ("no metadata after 60
  minutes", "no bytes after 6 hours", "no seeders after 6 hours"). Both
  thresholds count **time the client spent downloading it**, not time since
  Arc asked for it: a torrent waiting its turn behind the download limit has
  not failed at anything. A release Arc has already tried is never chosen
  again, and a release with no seeders is never chosen at all. Torrents
  stopped by hand, and torrents queued behind the client's own download
  limit, are never touched by this.
- FR-A7 Users can see the acquisition status of each episode on the show page
  (wanted, searching, downloading with %, preparing, ready, unavailable).
- FR-A8 A user can ask for the first episode of any show as a sample from the
  show page, without changing their list or MAL. Only that one episode is
  fetched; it follows the same states, D-day drop (FR-T2) and retention
  (FR-T1) as any want. Watching it to completion adds a show that is not on
  the list as Watching (FR-S4) and the ordinary window takes over; an existing
  list entry keeps its status.
- FR-A9 A list entry created by a MyAnimeList import generates no acquisition
  wants until the user has touched the show in Arc (status, progress, score,
  Play, or a MAL-log revert); a show that is currently airing is the
  exception and keeps fetching. Activation never expires. "Try episode 1"
  (FR-A8) is allowed on a dormant entry and fetches that one episode without
  activating it — one episode is the point of a sample; activation means the
  window.
- FR-A10 At most K shows per user fetch at once (default 5; 0 = unlimited):
  shows already fetching keep their slot, free slots go to currently airing
  shows first and then to the most recently updated entries, and the rest wait
  visibly on their show page. A show holds no slot once its wanted episodes
  have arrived, or once Arc has given up on finding them (FR-A6). A sample
  (FR-A8) never counts: it may add one episode beyond the cap, and is never
  refused by it.

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
  is > 10 s and < 95 % of duration. The "Resumed from M:SS" notice auto-hides
  after five seconds; Dismiss closes it sooner.
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
- FR-W1 Home shows **Continue watching** (episodes with a saved position that
  is past the start and short of the end, most recent first — whether or not
  the episode is also marked watched, so a rewatch stopped half-way is offered
  and resumes where it stopped; owner, 2026-09-11), **Behind on** (followed airing shows with unwatched
  aired episodes), and **New this week** (episodes that aired in the last 7
  days for followed shows, with ready/preparing state). As shelved since M15
  those are Continue watching, Catch up, and This week plus Ready to watch —
  the latter being the ready, unstarted half of New this week. The page also
  opens with a hero of season recommendations, which is presentation over the
  same caches rather than a requirement of its own (owner, 2026-09-11).
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
- FR-T6 Acquisition holds itself while free space on the data volume is below
  an admin-set floor (default 10 GB): reconciliation, ingest and playback
  continue, no new search starts; it resumes on its own when space is freed.

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
| Home | 1 | Season recommendations hero; Continue watching, Ready to watch, This week, Catch up (behind on), Picked for you |
| Schedule | 1 | Weekday grid for the season; prev/next season; add-to-list actions |
| Search / add | 1 | AniList search, add to list in a status |
| Show | 1 | Cover, synopsis, list status + score controls, episode list with acquisition/prep state and watched marks, play buttons |
| Player | 1 | HLS player, resume, next episode, progress reporting |
| MAL link / sync log | 1 | Connect MAL, view write log, revert |
| Recommendations | 2 | Mood prompt, picks with argued cases, add-to-planned |
| Match review | 2 | Queue of unsure files with candidates and LLM suggestion |
| Admin | 2 | Users/invites, rules, jobs, disk, review queue |
| My List | 2 (M15) | The viewer's list by status with season progress and airing state; the same data the Show page's list control edits |

Navigation as of M15 (owner decisions 2026-09-11, from the design pass and the
sign-off on it): a top toolbar with Watch Now (Home), Browse (Search),
Schedule and the search field; the avatar menu holds My List, MyAnimeList,
Match review (with the pending count), Admin and Log out; Recommendations is a
button inside Browse and a shelf action on Home. On phones a bottom tab bar
(Watch Now · Browse · My List · More) replaces the toolbar nav, with Schedule
at the top of the "More" sheet.

Attribution: where a deployment has a TMDB key (FR-C6), the shell carries
TMDB's required line — "This product uses the TMDB API but is not endorsed or
certified by TMDB." — in the quiet footer under the Home shelves, beside the
API status. Text only; TMDB's terms offer the logo but do not require it. With
no key nothing of theirs is shown and the line is absent.

The client is responsive; primary target is desktop, but phone layout must be
usable for the home page and player. Every horizontal shelf must be scrollable
with a mouse as well as a trackpad or a thumb: it can be dragged, and hovering
it raises an arrow at each end that still has somewhere to go (owner,
2026-09-13).

## 6. Episode lifecycle

```
not_wanted → wanted → searching → downloading → downloaded
   → matching → (review) → matched → preparing → ready
ready → (retention) → deleted → not_wanted
any of searching/downloading → unavailable (after retry window)
any of wanted/searching/unavailable → not_wanted (nobody wants this episode)
downloading → not_wanted (nobody wants this episode: the torrent and its
   partial files are removed from the client)
preparing → failed → (retry) → preparing
```

The last two are the same arrow either side of the point where bytes start
arriving. A want going away unwinds everything up to and including
`downloading`, because nothing has landed; from `downloaded` onwards the file
is retention's to measure and delete (FR-T1), so the episode is left where it
is and the grace period decides.

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
| Hardware transcoding | Open | Depends on host. Software x264 assumed, `fast` preset / CRF 19 / `-tune animation` since 2026-09-11 (~1.5–2× real time on 2 vCPU). |

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
- 2026-09-11 — M15 sign-off (owner, on the built pages): Schedule joins the
  toolbar nav and the top of the phone "More" sheet (the four tabs stay as
  they are). Watch Now's hero stops being "continue watching" and becomes a
  cycling set of up to six season recommendations — at most two slides for the
  latest run's picks for shows airing this season or next, and the rest from
  unfollowed shows of the cached season, ranked by overlap with the viewer's
  top-three genres (weighted by list score) and then by how many people are
  watching, rather than filtered by genre, so the season is always represented
  even where the catalogue has not filled in its genres — with Add to list
  (planned) and Details on it, and hidden entirely when there is nothing to
  offer. Continue watching becomes the first shelf and "Up Next"
  narrows to ready-but-unstarted episodes as "Ready to watch". Presentation
  only: no endpoint, payload or behaviour changed.
- 2026-09-12 — Encoding stays faithful to the source: no denoise pass (owner),
  after a same-episode comparison with a denoised third-party encode; the
  smoother look there comes from removing Crunchyroll's compression noise.
- 2026-09-11 — M15.5 approved: the manami anime-offline-database (weekly
  import) becomes the first stop for search, matching and id mapping, and
  TMDB (via Fribb's anime-lists id map) enriches art, stills and credits —
  both as fallbacks behind AniList, ahead of MAL for art. Kitsu rejected for
  now. Reason: AniList suspended its API for three days this week.
- 2026-09-11 — M15 design accepted (Apple TV grammar, "quiet cinema"): toolbar
  navigation and avatar menu replace the sidebar; new My List page; Recs as a
  Browse button; phone bottom tab bar. Data extension approved so the design
  is not placeholders: key art (`cover_large_url`), staff credits and episode
  titles/stills from AniList. Cour selector, synopses and the "Brand" page are
  not shipped (no data / documentation only). Player subtitles chip dropped
  (subtitles are burned in).
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
- 2026-09-11 — M15 sign-off remarks 8 and 5. Search merges Arc's own cached
  rows in front of the live page (FR-C1), because the owner could not find a
  show Arc was downloading while AniList was disabled upstream; an upstream
  failure with local hits now answers 200 rather than 502. Transcode quality
  defaults raised from `veryfast`/CRF 20 to `fast`/CRF 19/`-tune animation`
  after the owner judged playback soft, and the three knobs plus an optional
  bitrate ceiling became settings.
- 2026-09-11 — M15 sign-off remark on Continue watching. The shelf lists any
  episode with an in-progress position, completed or not (FR-W1), so a rewatch
  left at the midpoint is offered; `completed` keeps every other meaning it
  had. Resume (FR-S2) no longer refuses a completed row either — its 10 s floor
  and 95 % ceiling are the whole rule.
- 2026-09-12 — Resume notice auto-hides after 5 s (owner).
- 2026-09-12 — TMDB attribution text in the shell: the required sentence sits
  in the Home footer next to the API status, shown only when the deployment has
  a `TMDB_API_KEY` (the server publishes `tmdb_enabled` on `/api/health`). The
  footer rather than the avatar menu or the "More" sheet because attribution
  should be visible without opening anything, and the quietest line on the page
  is the right loudness for a credit.
- 2026-09-12 — Offline-sourced records carry a quiet "via offline catalogue"
  caveat (owner).
- 2026-09-12 — M15.5 shipped: offline catalogue (manami + Fribb) is the first stop for search, matching, id mapping and season seeding; TMDB fills key art, stills and credits behind AniList. Fixture re-capture waits for AniList to lift its rate limit.
- 2026-09-12 — Airing sanity rule (owner): a FINISHED show's episodes have all
  aired whatever date they carry, and an episode dated after a higher-numbered
  one is marked estimated and aired with its neighbour (FR-C4, FR-C5). Derived
  only — the source's date is still stored and still shown.
- 2026-09-12 — Interactive AniList calls do not wait out a 429 (owner): a
  search or show page falls back to MAL/offline immediately rather than paying
  `Retry-After`, and a rate limit no longer stands AniList down for five
  minutes (FR-C6). Background jobs still wait.
- 2026-09-12 — "Try episode 1" (FR-A8, owner): acquisition gains one explicit
  exception to FR-A1. A user may ask for a show's first episode as a sample
  from the show page, with no list entry and therefore no MAL write, because
  deciding whether a show is worth adding is what the first episode is for;
  the alternative was adding it as planned and remembering to take it off
  again. Exactly one episode, never a season, and everything after the fetch —
  states, the D-day drop, retention — is an ordinary want's. Nothing else about
  the acquisition rules changes.
- 2026-09-12 — M16 scope (owner): failure banner shows a user their own failures only; the demo account and "How Arc works" framing are part of M16 and appear only on the demo account; the owner dogfoods for a few days first.
- 2026-09-13 — Production stalled after the MAL import (owner + orchestrator):
  414 wants at once, qBittorrent's 3 active-download slots held by dead
  2018 torrents, and a season batch (`Dagashi Kashi 2 - 01 ~ 12`) read as
  episode 1 and fully transcoded. Owner decisions: (1) batch releases
  (episode ranges, BATCH markers) are never picked; (2) stall handling — a
  torrent with no progress is removed, the episode goes `unavailable` on the
  retry schedule and that release is not chosen again; (3) **dormant
  imports** — a MAL import creates no wants by itself; a show fetches once
  the user touches it in Arc (status, progress, Play, Try), with airing
  imported shows as the exception; (4) a **per-user slot cap** K (default 5)
  on shows fetching at once, airing first then most recently updated, the
  rest visibly waiting; (5) a **storage guard** — acquisition holds itself
  when free space on the data volume is below an admin-set floor (default
  10 GB). The FR text for 3–5 is written with each change.
- 2026-09-13 — Hero art (owner): a new `backdrop_url` column, written only by
  the TMDB enrichment for every mapped show, is preferred by the 21:9 heroes
  and the 16:9 cards over AniList's 4.75:1 banner strip, which the design
  never shows as a picture. Chosen over letting TMDB overwrite `banner_url`
  because an AniList refresh would put the strip back. Also fixed the same
  day: the hero's aspect probe loaded lazily inside a 0×0 box and was never
  fetched, so even shows with a proper backdrop stayed on the blurred poster.
- 2026-09-13 — Stalls, cancels and the download queue (owner), the second half
  of the 2026-09-13 production entry above. (1) **Stall handling** (FR-A6): a
  torrent in `metaDL` past `STALL_METADATA_MINUTES` (60), or with no bytes — or
  a swarm the tracker says is empty — past `STALL_NO_BYTES_HOURS` (6), is
  removed from qBittorrent with its files and the episode goes `unavailable`
  onto the ordinary retry schedule. Both clocks measure the client's own
  `time_active`, not the age of Arc's row, and torrents stopped by a person or
  queued behind the download limit are never touched. (2) A release Arc
  has **already tried** is never chosen again, and a release with **no
  seeders** is rejected rather than merely ranked last. (3) A download **nobody
  wants any more** is cancelled: the torrent and its partial files go, and the
  episode returns to `not_wanted` (§6's new edge). Bytes that have landed stay
  retention's, an attempt somebody rejected in review keeps its own file, and
  the cancelled release may be chosen again — a change of mind must not cost
  an episode the best release on Nyaa. "Try episode 1" (FR-A8) cancels the
  same way when the user presses Cancel mid-download. (4) Arc now writes qBittorrent's **queue policy** as well as its
  seeding policy — queueing on, `QBIT_MAX_ACTIVE_DOWNLOADS` (8),
  `QBIT_MAX_ACTIVE_TORRENTS` (12), don't count slow torrents — because the
  limits the owner raised by hand on 2026-09-13 are exactly the ones a
  container restart loses.
- 2026-09-13 — **Dormant imports** (FR-A9, owner), the third of the 2026-09-13
  production decisions. A MyAnimeList import is a baseline (FR-M2), not a
  request, so a `watching`/`planned` entry it created generates no wants until
  the user has touched that show in Arc — a status, progress or score change, a
  completed episode, or a "try episode 1". The moment is stamped on
  `list_entries.activated_at` and is never cleared: activation does not expire,
  because a show somebody asked for in March is still a show they asked for. A
  **currently airing** show is the exception and keeps fetching whether or not
  it has been touched, because "the episode is there on the morning it airs" is
  the weekly use of Arc and a user whose whole list arrived by import should not
  have to press anything for it. **Play counts from the first progress report**,
  not from the completion: thirty seconds in is the user asking for the show,
  and waiting for 90 % would mean the next episode only started arriving after
  they had finished this one. Pressing play never *adds* a show to a list,
  though — FR-S4 keeps that at the completion. **"Try episode 1" (FR-A8) does
  not activate**: it writes its own want for exactly one episode, which is what
  was asked for, and activating would hand the show the whole N-episode window;
  by the same token it is no longer refused on a dormant `watching`/`planned`
  entry, because such an entry has no window and "the next episodes are fetched
  automatically" would be false. The migration backfills every entry that is
  Arc's own (`updated_by = 'arc'`) **or has never been pushed to MyAnimeList**
  (`mal_synced_at IS NULL`, which catches an Arc-made row a later import
  overwrote), so nothing anybody is watching stops; genuinely imported rows
  stay dormant, which is how production's 414 wants are shelved on the first
  reconciliation after the deploy — with their downloads cancelled and their
  landed bytes left to retention's grace. One shape no column can tell from an
  import survives — an Arc-made entry later changed on MAL *and* successfully
  pushed — and it costs one press of "Fetch this show". The alternative
  considered and rejected was a one-off "start from today" cutoff, which would
  have said nothing about the next import.
- 2026-09-13 — **Per-user slot cap** (FR-A10, owner), the fourth of the
  2026-09-13 production decisions, and the owner's answer to "dormant imports
  or a cap?" was **both**. Dormancy (FR-A9) stops an untouched import from
  asking for anything; the cap stops the activated half of a large list from
  asking for all of it on one afternoon — which is what one disk and
  qBittorrent's handful of download slots actually allow. K = 5 by default and
  0 means no cap. A show **occupies a slot** while the user has a live want on
  an episode that is not yet `ready`; a show whose wanted episodes have all
  arrived is waiting to be *watched*, holds nothing, and frees its slot for the
  next show — otherwise a user who let three episodes pile up would stop
  fetching everything else. Occupants keep their slot whatever K says, so
  lowering K cancels nothing and only stops new starts: throwing away bytes to
  obey an ordering would be the one thing a *limit* should not do. Free slots go
  to currently airing shows first (the weekly episode is the one a person
  notices missing) and then to the most recently touched entries. A show that
  has nothing to fetch competes for nothing. The shows over the cap say so on
  their own page — "Waiting for a slot — 5 of your 5 shows are fetching" — with
  no button, because unlike a dormant entry there is nothing the viewer could
  press that would be honest. A sample (FR-A8) neither occupies a slot nor is
  refused by one: one episode somebody asked to try is the smallest thing Arc
  does, so a sample may add one episode beyond the cap. Two states beside
  `ready` hold no slot either, both found in review: an episode FR-A6 has given
  up on (`unavailable`, retried daily — five unfindable shows would otherwise
  freeze a list for ever) and one whose transcode broke (`failed`, which needs
  an admin rather than a slot). And a show waiting for a slot takes **no part**
  in the reconciliation: every want it already holds is left exactly as it is,
  including one FR-T2 dropped, whose `dropped_at` is what retention measures
  the grace period from. A cap on how much is fetched at once decides nothing
  about retention — with the one exception that costs nothing: a live want on
  an episode the user has watched past is still deleted, because the completion
  is the anchor and a live want left behind would make the sweep skip an episode
  the cap had no business keeping.
- 2026-09-13 — **Storage guard** (FR-T6, owner), the fifth. Acquisition holds
  itself while free space on the data volume is under `min_free_gb` (default
  10, admin-editable beside G, D and N). A hold is deliberately narrower than
  the pause: reconciliation still runs, because everything it does when space
  is short — dropping wants, shelving rows, cancelling downloads nobody wants —
  *frees* space; ingest and transcodes still run, because finishing what has
  landed is how a source becomes deletable; only the starting stops, and
  `search_release` requeues itself exactly as it does while paused. Nobody
  presses it and nobody has to clear it, which is the whole difference from the
  pause: the next tick after retention frees room starts fetching again. A
  measurement that cannot be taken never holds.
- 2026-09-13 — Shelves scroll for mouse users (owner): drag-to-scroll with the
  mouse plus hover edge arrows on every shelf, chosen over turning the vertical
  wheel sideways, which would hijack page scrolling whenever the pointer rests
  on a shelf. Trackpad, touch and keyboard behaviour unchanged.
