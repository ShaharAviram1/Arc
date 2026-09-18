# Arc — Project Specification

> Living document. Update whenever scope, behaviour, or a decision changes.
> Last updated: 2026-09-18. Companions: [architecture.md](architecture.md), [roadmap.md](roadmap.md), [CLAUDE.md](CLAUDE.md).

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
  The **current** week's grid includes every airing show with a known air
  time, whatever season it started in — a two-cour show or a long-runner
  belongs to the week it airs in — and names that season on the card; the
  prev/next views stay the shows of the season being browsed.
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
  credits when AniList has not, and never overwrites AniList-provided values,
  and the art it is asked for covers every show the home page's hero can offer
  — including the long-runners carried into the week by being on air rather
  than by a season tag (owner, 2026-09-17: One Piece had never once been asked
  for a backdrop);
  opening a show's page asks for its missing pictures (they appear without a
  reload), and where the id map cannot reach the show — or the deployment has
  no TMDB key — an episode row or card with no still of its own falls back to
  the show's backdrop and then to its key visual, framed in the 16:9 slot
  rather than left as a placeholder (owner, 2026-09-17; the caption that used
  to explain the empty box is gone, since the artwork is a better answer than a
  sentence about the absence of it).
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
  Per-show overrides for group and resolution are allowed; an admin edits them
  **on the show page** ("Release rules for this show", a control only an admin
  sees) and in **Admin → Rules**, where every existing override can be changed
  or removed (M16, owner 2026-09-18). An override naming neither field is the
  same thing as having none. **An English dub
  ranks below every subbed candidate** and is never chosen while one exists
  (owner, 2026-09-14): a dub is not a worse copy of the episode, it is the
  episode in the wrong language, so it outranks all four rules above — both of
  the releases Arc picked on 2026-09-13 won on seeders. It stays a ranking
  rather than a filter, because when nothing else was found a file somebody can
  watch beats a fortnight of "searching"; the choice is logged when it is made.
  A dual-audio release is not a dub: it carries the original track too.
- FR-A4 Nyaa is polled via its RSS search feed with a query built from the
  show's titles and episode number. Results are parsed with the same filename
  parser used for the library. Candidates whose parsed title/episode do not
  match are discarded. **Several forms of the query are asked, because a
  release group names a show by neither the whole catalogue title nor the same
  language**: the full titles, the episode number written the Western way
  (`S01E07`), a later season's marker written the three ways groups write it,
  the head of a subtitled title, the bare title, and up to two of the show's
  other names — each of them also **with its symbols taken out**
  (`Yarichin☆Bitch-bu` → `Yarichin Bitch-bu`, `Love Live! Superstar!!` → `Love
  Live Superstar`), since Nyaa matches whole words and a star glues two of them
  together. A **film, or a one-episode OVA/ONA, is asked for by name with no
  episode number at all** and accepted as that entry's only episode (owner,
  2026-09-14): three films sat unfetched for a day because Arc was asking for
  "- 01" of them. Such a release has to **say** it is a film or a one-off, and
  to name at least as much of the title as the catalogue does: a whole-series
  Blu-ray pack names neither an episode nor a film, and *Kizumonogatari* is
  three films with one name. A whole-season batch is still never picked,
  whatever form found it, and where a release cannot be told from a series pack
  by anything in its name Arc leaves the episode unfetched and says so — a
  missing file is visible and fixable, the wrong film plays as though it were
  right. **Some groups never restart the count on a sequel** (owner,
  2026-09-17): where the catalogue's own `PREQUEL` chain adds up to a number
  Arc can be sure of — every prequel cached, **every one of them finished
  airing** (an airing season's episode count is an announcement, and one short
  puts the absolute number inside that season's own run), every episode count
  published, films and OVAs skipped because groups do not count them — a later
  season is also asked for by its **absolute** number (`Jujutsu Kaisen - 25`
  for episode 1 of a second season that follows 24), and a release carrying
  that number is accepted as that episode. Only ever above the prequel's own total, only from
  a release that names no season at all, and never while an explicitly
  season-marked release for the same episode came back in the same search: a
  group that wrote `S2` has already answered the question the arithmetic is
  asking. An entry whose chain Arc cannot add up keeps exactly the behaviour it
  had before, because an offset that is inferred rather than read is the wrong
  file rather than a missing one. The absolute *query* is only asked where the
  show has a short name to ask it under — a sequel whose title names an arc
  rather than a season has none, and nine words followed by a running number is
  a form nobody writes — though a release like that is still accepted if one of
  the other forms finds it.
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
  While Arc is still looking, the row also says **what the last search did**
  (owner, 2026-09-14): how many query forms it asked, how many releases came
  back before the filter, and when the next attempt runs — "Searching · 6
  forms, 0 results · next try 23:26", in the viewer's own timezone. A row that
  says only "searching" for six hours says nothing, and the pair of numbers is
  what separates a query that matches nothing from a filter that keeps nothing.
  An unavailable row keeps its reason instead.
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
  Audio track choice: prefer Japanese (configurable); otherwise the
  **original** — any track that is not an English dub, the muxer's default
  track first; otherwise the muxer's default; and the first track only as a
  last resort (owner, 2026-09-18). A track with no language tag counts as
  "not English". Every choice below the first rule is noted on the job, naming
  the language and the rule that produced it, because the rendition is the
  only copy kept and a dub burned in is only discoverable by ear.
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
  **The player starts playing on its own** (owner, 2026-09-17): opening an
  episode is the decision, and the page presses play as soon as the source is
  attached and the resume seek (FR-S2) has been made. The browser's autoplay
  policy has the last word, so there are exactly two attempts and no loop — if
  audible playback is refused, one muted retry with a small, quiet "Tap to
  unmute" pill over the control bar that turns the sound on without
  interrupting playback; if even muted playback is refused, the existing play
  button is the answer and no error is shown, because nothing went wrong.
- FR-S2 Resume: on open, the player seeks to the user's last position if it
  is > 10 s and < 95 % of duration. The "Resumed from M:SS" notice auto-hides
  after five seconds; Dismiss closes it sooner.
- FR-S3 Progress is reported every 10 s while playing and on pause/seek/close.
- FR-S4 An episode counts as **watched** when position ≥ 90 % of duration.
  This sets WatchProgress.completed, advances ListEntry.progress if this
  episode number is greater than current progress, and enqueues a MAL sync.
  If the show is not on the user's list, watching it adds it as Watching
  (the act of watching is the user's choice); status is never changed
  automatically beyond FR-W5's auto-complete.
  **Un-marking** an episode (revised by the owner 2026-09-13, superseding the
  2026-09-07 clarification) is a user-originated event: it clears the
  completion row and, when the user's list progress *equals* that episode's
  number, lowers progress to N−1 with one logged progress write carrying the
  previous value. This is the only path on which Arc lowers MAL progress, and
  only because the user asked; the FR-M4 guard that refuses a lowering
  progress write still refuses every automatic one. An un-mark of any other
  episode clears the row alone — above the progress there is nothing to lower,
  and below it a rollback would claim something about the episodes in between
  that the user never said. The status is never rolled back: a show FR-W5
  completed stays completed, because "completed" is the user's word (FR-W2).
  Un-marking never creates a list entry.
- FR-S5 **The end of an episode is two moments** (owner, 2026-09-17, replacing
  the single overlay at the completion mark).
  1. *Crossing the completion mark* (FR-S4's 90 %, unchanged) is bookkeeping,
     and gets a receipt: a small, quiet "Marked as watched" toast over the
     picture for about four seconds, dismissable by a click, carrying no
     action. Nothing is offered and no overlay appears — the decision about
     what to watch next is ten minutes away. A rewatch crosses the same mark
     and says nothing, because the completion is not new.
  2. *The end itself* — with 1:30 or less remaining, and again when the media
     ends — gets the overlay: **Next episode** (offered only when there is a
     next episode and it is ready; when it exists but is not prepared, or the
     episode was the last, the overlay says so instead of drawing a dead
     button), **Keep watching** (puts the overlay away for the rest of that
     playback; Escape does the same) and **Back to the show**. The episode
     keeps playing underneath, the overlay takes no keyboard focus so the
     shortcuts of FR-S6 keep working, and nothing auto-advances. Both the
     toast and the overlay render inside the fullscreen element.
- FR-S6 Keyboard shortcuts: space, arrows (±5 s), f fullscreen, m mute.

### 4.6 Watch tracking and list states
- FR-W1 Home shows **Continue watching** (episodes with a saved position that
  is past the start and short of the end, most recent first — whether or not
  the episode is also marked watched, so a rewatch stopped half-way is offered
  and resumes where it stopped; owner, 2026-09-11). **Short of the end means
  two things, and the first of them to happen wins** (owner, 2026-09-17): an
  episode leaves the shelf once the viewer is past the **completion mark**
  (FR-S4's 90 %) or once it has **less than three minutes left**, whichever
  comes first. Both are tighter than the resume ceiling of FR-S2 on purpose —
  an episode may still be *resumed* into its last minutes, because the viewer
  chose it, but Arc will not *offer* a credits roll as something left to watch.
  What takes its place is the next episode, under Ready to watch, which the
  FR-S4 advance has just made eligible. Home also shows **Behind on** (followed airing shows with unwatched
  aired episodes), and **New this week** (episodes that aired in the last 7
  days for followed shows). As shelved since M15
  those are Continue watching, Catch up, and This week plus **Ready to watch**:
  every episode Arc holds a playable file for, on a show the viewer is
  watching, has planned or has on hold, that they have neither started nor
  watched (FR-W5) — **whatever it aired, and whether it aired at all**, newest
  file first (owner, 2026-09-17). It was the ready, unstarted half of New this
  week until then, which silently made it a shelf about the last seven days of
  broadcasting rather than about the files Arc is holding. The This-week
  shelf carries **no acquisition state** (owner, 2026-09-13): the episode, its
  air day, and a watched tick when the viewer has watched it (FR-W5). The tick
  belongs to **the episode the tile names** and to no other: a slot whose
  broadcast has not happened never carries one, whatever the viewer has watched
  of the show (owner, 2026-09-17). What Arc
  is doing about the file belongs to the show page, where FR-A7's per-episode
  state lives in full. The page also
  opens with a hero of season recommendations, which is presentation over the
  same caches rather than a requirement of its own (owner, 2026-09-11). The
  page **updates itself as episodes change state** — a tile appearing, a row
  flipping, a still arriving — with no notifications and no sound (owner,
  2026-09-13).
- FR-W2 Users can set a show to watching / planned / on hold / dropped /
  completed, and set a score (1–10) from the show page.
- FR-W3 Marking an episode as watched manually is allowed (e.g. watched
  elsewhere) and is treated the same as FR-S4: it records the completion for
  that episode **and** raises ListEntry.progress to its number if lower, with
  one MAL progress write that never lowers. It writes no completion rows for
  the episodes below it — FR-W5's progress half already counts them. Its undo
  is FR-S4's un-mark, which lowers the progress by one when the episode is the
  latest watched.
  In the player it is **one control with the action as its glyph** (owner,
  2026-09-17): ✓ and "Mark watched" before, ✕ and "Mark unwatched" (label and
  hover) once the episode is watched, by either half of FR-W5. The one episode
  that keeps a ✓ is the one with nothing to press — watched by list progress
  alone, where FR-W5 says the control is not a button.
- FR-W4 Dropped, completed and on-hold shows generate no acquisition wants.
- FR-W5 **What "watched" means, and when a show completes itself** (owner,
  2026-09-13). An episode counts as watched for a user when its number is at
  or below that user's ListEntry.progress **or** the user has a completed
  WatchProgress row for it. Every place Arc shows a watched state reads that
  one definition: the show page's per-episode rows and its "Watched N / M"
  line, the home page's tiles, and the "play the next thing" pick. Each
  episode also carries **which** of the two said so, which is the same thing as
  whether the user can take it back (FR-S4): an episode *at or above* the
  list's progress is `arc` and offers "Unwatch" — at the progress because the
  un-mark lowers it, above because there is a completion row to clear — and an
  episode *below* the progress is `progress`, a plain non-actionable "Watched"
  with a tooltip pointing at where the undo is. One value, not a second flag:
  the only question the client has is whether the control is a button.
  For retention (FR-T1) an episode at or below a user's progress counts as
  watched by that user, with the grace period anchored on the list entry's
  last change; so a file the user skipped past is deleted on the same G-day
  schedule as one watched in Arc.
  When a progress advance brings progress to the show's episode count **and**
  the catalogue says the show has FINISHED airing **and** that count is known,
  the entry's status becomes `completed` in the same transaction, as one
  logged, user-originated status write to MAL alongside the progress (FR-M4,
  FR-M7). This applies **whatever status the entry had**, `on_hold` and
  `dropped` included (owner, 2026-09-13): finishing the last episode of a show
  you had dropped is the clearest statement anybody makes about a list entry.
  A show still airing, a show with an unknown episode count, and a rewatch of
  an already-completed show are never auto-completed.

- FR-W6 **Own failures on Watch Now** (M16, owner 2026-09-12: "a user's own
  failures only"). The home page carries, above the hero, a quiet banner of the
  things that have stopped **for this viewer**: an episode they hold a live want
  on — the window's (FR-A1) or a sample of their own (FR-A8) — whose state is
  `failed` (a transcode broke, FR-P4) or `unavailable` (FR-A6 found no release
  and retries daily), and their own MyAnimeList writes that did not land
  (FR-M6). Each row says what stopped, which show and episode, one sentence of
  why — the tail of the transcode's own complaint, or FR-A6's retry promise,
  never a page of ffmpeg output — and links to where it can be acted on: the
  show page for an episode, the sync log for a write. Nothing here is global: a
  want is per user and the log is per user, so an **admin sees exactly their
  own**, and the Admin jobs tab (FR-D3) remains the whole-queue view. Each row
  is **dismissable on its own**, and the dismissal is remembered per account in
  that browser rather than on the server — putting a row away is a statement
  about one reader's attention, not about the failure, which is still there and
  still on the show page. A failure that happens *again* comes back: the daily
  retry of an `unavailable` episode is new news, and a dismissal covers the
  failure it was pressed on and no other. A show with nothing wrong renders no
  banner at all. Notifications (email/push) stay out of scope (§8).

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
  completed (progress), status change, score change, and an explicit un-mark
  (progress, FR-S4). Progress written to MAL never decreases as a result of
  automatic watch events — the explicit un-mark is the one write that lowers
  it, and the guard tells the two apart by the cause recorded on each queued
  row rather than by the endpoint.
- FR-M5 Every write is recorded in MalWriteLog with the previous value. A
  user can view their log and revert an entry, which writes the previous
  value back (and logs that too).
  The log also records, as non-writes, conflicts where MAL's newer change
  overrode an Arc change (`conflict`/`skipped`) and writes that could not
  apply (no MAL id).
- FR-M6 Writes are idempotent and retried with backoff; failures surface as a
  badge on the show and in the user's sync page.
- FR-M7 "Never write a change I did not make": there is no code path that
  writes to MAL except via a user-originated event (a list edit, a watch
  completion, an explicit un-mark) or an explicit revert.

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
  (default G = 7). "Completed" here is FR-W5's definition — an episode at or
  below a user's list progress counts as completed by that user, anchored on
  that entry's last change — so an episode somebody skipped past is deleted on
  the same schedule as one they watched in Arc.
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
  and audio language preferences. The per-show group/resolution overrides of
  FR-A3 are listed here too, and editable: change or remove one from the rules
  tab, add one from the show's own page (M16, owner 2026-09-18).
- FR-D3 Job queue view with retry/cancel; qBittorrent status; disk usage.
- FR-D4 Match-review queue across all users.
- FR-D5 **Demo account** (M16, owner 2026-09-18). An account may be flagged as
  the demo one (`users.is_demo`), from the admin Users tab or by
  `arc.cli demo-list --demo`. The flag gates **presentation and nothing else**:
  a fourth top-nav entry, **How Arc works** (and the same entry at the top of
  the phone "More" sheet), plus a one-line dismissable strip above Watch Now's
  hero pointing at it — dismissal is remembered per account in that browser.
  The page is informational: the pipeline from a list entry to a MAL write, one
  sentence per external service, the three rules that matter (the acquisition
  window, the review queue, MAL writes), and where to look. It offers no
  action, reads no per-user data, and the **route** is open to any signed-in
  account so that a shared link opens rather than 404s; only the nav entry and
  the strip are gated. The demo account has **no MyAnimeList link**, so no MAL
  write can originate from it whatever it does; its list, progress and
  recommendation run are seeded by `arc.cli demo-list` and `arc.cli recs`, and
  a seeded entry is active rather than dormant (FR-A9) so acquisition runs for
  it exactly as for anybody else.

## 5. Client pages

| Page | Phase | Contents |
|---|---|---|
| Login / accept invite | 1 | Email + password; invite token flow |
| Home | 1 | Season recommendations hero; Continue watching, Ready to watch, This week (broadcast times and a watched tick, no acquisition state — FR-W1), Catch up (behind on), Picked for you. Above the hero, the viewer's **own** failures (FR-W6): a quiet row per broken episode or MyAnimeList write, each dismissable on its own and remembered in that browser, linking to the show page or the sync log; nothing when nothing is wrong |
| Schedule | 1 | Three days at a time, starting with today: a day-and-date bar ("Wed 17 Sep") with chevron arrows at both ends that walk the window through the Mon–Sun week a day at a time (arrow keys too, stopping at the week's ends); today carries an accent underline and a "Today" chip; roomy rows with the whole show name, the air time at 15px, the episode number and the "Since Spring 2026" caveat; a show the viewer follows carries a quiet accent left rule and "On your list" for a screen reader; prev/next season — a browsed season's bar carries weekday names alone, no dates and no today, because it is a set of weekday slots rather than this week; add-to-list actions; the unscheduled block |
| Search / add | 1 | AniList search, add to list in a status |
| Show | 1 | Cover, synopsis, list status + score controls, episode list with acquisition/prep state and FR-W5's watched marks ("Unwatch" for Arc's own completion, a non-actionable "Watched" for one the list vouches for), play buttons. For an admin only, beside the episodes heading: **Release rules for this show** (M16) — a chip with a one-line summary of the override in force ("Overrides: SubsPlease · 720p") that opens an inline form for the preferred groups and the resolution, with Save and Clear |
| Player | 1 | HLS player, autoplay on load (muted fallback with a "Tap to unmute" pill), resume, progress reporting; a ✓/✕ mark-watched control; a "Marked as watched" toast at the completion mark and an end-of-episode overlay (Next episode · Keep watching · Back to the show) with 1:30 left — FR-S5's two moments |
| MAL link / sync log | 1 | Connect MAL, view write log, revert |
| Recommendations | 2 | Mood prompt, picks with argued cases, add-to-planned |
| Match review | 2 | Queue of unsure files with candidates and LLM suggestion |
| Admin | 2 | Users/invites, rules (including the per-show overrides, each editable and removable in its own row — M16), jobs, disk, review queue |
| My List | 2 (M15) | The viewer's list by status with season progress and airing state; the same data the Show page's list control edits |
| How Arc works | 2 (M16) | Informational, demo account only (FR-D5): the lede, the eight-step pipeline as a strip that stacks on narrow screens, one sentence per external service, the three rules, and "where to look". Reached from the nav entry and the Watch Now strip that only an `is_demo` account sees; the route itself is open to any session |

Navigation as of M15 (owner decisions 2026-09-11, from the design pass and the
sign-off on it): a top toolbar with Watch Now (Home), Browse (Search),
Schedule and the search field; the avatar menu holds My List, MyAnimeList,
Match review (with the pending count), Admin and Log out; Recommendations is a
button inside Browse and a shelf action on Home. On phones a bottom tab bar
(Watch Now · Browse · My List · More) replaces the toolbar nav, with Schedule
at the top of the "More" sheet. The **demo account** carries one more
destination in both chromes — How Arc works, after Schedule (FR-D5); no other
account sees it, and the four phone tabs are unchanged.

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
- 2026-09-13 — Watched state (owner, M16 batch 2): an episode counts as
  watched when its number is at or below the user's list progress OR the
  user has an Arc completion for it; the Show page's per-episode control
  reads "Watched" and offers "Unwatch"; the "New this week" shelf shows no
  acquisition state. A manual "mark watched" of episode N raises list
  progress to N (one MAL progress write, never lowering) and records no
  synthetic completion rows for 1…N-1 — but retention treats episodes at or
  below progress as watched by that user (anchored on the list entry's
  progress change), so their files are deleted on the same grace schedule
  as episodes watched in Arc. When progress reaches the episode count of a
  FINISHED show the entry becomes `completed` (one logged, user-originated
  status write to MAL); an airing show or one with an unknown count is
  never auto-completed; a completed show marked again stays completed. The
  FR text for all of this is **FR-W5**, with FR-S4, FR-W1, FR-W3 and FR-T1
  amended to point at it.
- 2026-09-13 — Watched state, second pass (owner, after the M16 batch-2
  review). Two revisions. (1) **Un-watch lowers progress**, superseding the
  2026-09-07 FR-S4 clarification: since FR-W5 derives the marks from list
  progress, clearing a completion row alone left the viewer no way to correct
  a mark at all — the tick stayed and the button did nothing. An explicit
  `DELETE …/watched` of episode N is a user-originated event and lowers
  progress to N−1 when the list stands at N, with one logged progress write
  (cause `manual`, carrying the previous value). It is the **only** path on
  which Arc lowers MyAnimeList's progress; the FR-M4 guard is unchanged and
  still refuses every *automatic* lowering, because the cause is recorded per
  queued row rather than per endpoint. Above the progress the un-mark clears
  the row alone; below it nothing moves, because a rollback there would claim
  something about the episodes in between that nobody said — so `watched_source`
  is `arc` (actionable) at or above the progress and `progress`
  (non-actionable, tooltip "Unwatch from the latest watched episode down")
  below it, one value rather than a separate `unwatchable` flag. The status is
  never rolled back: a show FR-W5 auto-completed stays completed, because
  "completed" is the user's word. (2) **Any status auto-completes** — `on_hold`
  and `dropped` flip to `completed` too when a finished show's count is
  reached, because finishing the last episode of a show you had dropped is the
  clearest statement anybody makes about a list entry.
- 2026-09-13 — **Schedule membership** (FR-C3, owner, M16 batch 2). The
  current week's grid is a calendar, not a season listing: it includes every
  `RELEASING` show with a known air time in that week, whatever `season` the
  catalogue tags it with, and a long-runner tagged with no season at all is
  the same case. Previous/next season views are unchanged — they are a
  catalogue browse, so they stay exactly the shows of the season being looked
  at. The season tag on the row is never rewritten; a show carried into the
  week says which season it started in ("Since Spring 2026") under its slot.
  A `RELEASING` show with no known air time anywhere is still listed as
  unscheduled on its own season's page, and is not carried in. Evidence: on
  production, That Time I Got Reincarnated as a Slime Season 4 is `RELEASING`
  with episode 23 airing Friday 2026-09-18 14:00 UTC, is tagged `SPRING 2026`
  (a two-cour show that started in spring), and was therefore missing from
  the Summer 2026 grid the owner reads. Home's "Catch up" and "New this week"
  were checked and needed no change — both are driven by the caller's list
  and the episodes' own dates, never by a season — and a test now pins that.
- 2026-09-13 — **The page updates itself** (FR-W1, FR-A7, owner, M16 batch 2).
  A signed-in tab learns about episode state changes for the shows that matter
  to it without a manual refresh: a "Ready to watch" tile appearing, a show
  page row flipping to Ready or Downloading, a still arriving. No push
  notifications and no sound — the page simply stays true, and there is no
  "live" indicator, because that would be a promise about something the spec
  deliberately treats as an improvement on latency rather than a guarantee.
  Polling remains the fallback: everything reachable this way is also
  reachable by asking again, so a stream that cannot connect costs a viewer
  seconds and nothing else. Mechanism in architecture.md §5.9.
- 2026-09-14 — **Matching robustness** (FR-A3, FR-A4, FR-A7, owner, M16 batch
  2: "failing on this matching is kinda embarrassing"). A day of production
  logs, not a code review, and six answers to the same shape of problem: Arc
  was asking Nyaa for names nobody writes. **Films, OVAs and one-episode
  specials** were asked for as "- 01" and are now asked for by name, and
  accepted as the one episode the catalogue holds for them; three of them had
  sat in "searching" for a day. **Symbols** a keyboard does not reach —
  `Yarichin☆Bitch-bu`, `Love Live! Superstar!!`, `Fate/Zero` — get a second,
  flattened form of the query, because Nyaa matches whole words and a star
  between two of them makes one word out of both. A **slash** was being read as
  a directory separator, so *Fate/Zero* parsed as a show called "Zero" and
  every release of it was rejected as the wrong show; which kind of slash it is
  is now the caller's answer rather than a guess. **Dubs** rank below every
  subbed release and are only taken for lack of anything else, which is the one
  rule here that changes what a user *gets* rather than what Arc finds: two
  shows were quietly delivered in English yesterday because the dub had the
  most seeders. Up to two **synonyms** earn a query of their own. And the show
  page now says **what the search actually did** — "6 forms, 0 results · next
  try 23:26" — because the honest answer to "it has said searching all day" is
  a number, not a spinner. The claims behind each form are written down in a
  query corpus of real cases (architecture.md §10) rather than in whichever
  unit test happened to find them. **Absolute episode numbering is deliberately
  not part of this** and remains its own item: a release numbered 40 of a
  two-season franchise needs the relation graph, and guessing at it means a
  wrong file rather than a missing one.
- 2026-09-17 — **The end of an episode is two moments** (FR-S5, owner, M16
  batch 3: "the overlay of the next episode at the completion pct mark isn't
  really good"). The overlay used to arrive with the completion at 90 %, which
  put a decision about the next episode on screen while two and a half minutes
  of this one were still running — and did it over a blacked-out picture. The
  two things are now separated by what they are for. The completion mark is
  Arc's bookkeeping and the viewer only needs a receipt, so it gets a quiet
  "Marked as watched" toast for four seconds with nothing to press and nothing
  to decide; a rewatch, whose completion is not new, gets silence. The *end* is
  the decision, and arrives with 1:30 left — a card offering the next episode
  when one is ready, "Keep watching" for the viewer who is not finished with
  this one (Escape says the same, and it holds for the rest of that playback),
  and the way back to the show. The video keeps playing underneath, nothing
  takes focus away from the playback shortcuts, and nothing auto-advances: the
  next episode is still a thing the viewer asks for. FR-S4's rule, the server's
  once-only completion and every MAL write behind it are untouched — this is
  what the client says about them, not when they happen.
- 2026-09-17 — **The schedule shows three days, not seven** (FR-C3, owner,
  M16 batch 3). Once the current week's grid started carrying every airing
  show — long-runners and two-cour carry-ins included (2026-09-13) — seven
  columns of the 1180px measure were 158px each, and a 158px column is a list
  of abbreviations rather than a schedule. The page now shows **today,
  tomorrow and the day after**, named with their dates ("Wed 17 Sep"), with
  chevron arrows at the ends of the day-and-date bar that walk the window
  through the same Monday–Sunday week the API already returns, one day at a
  time, stopping at its ends; left/right arrow keys do the same. Season
  prev/next is untouched — it is a different axis, and browsing Spring 2026 is
  not the same gesture as looking at Thursday. The room bought by three
  columns goes into the rows: the **whole** title, never clamped, the air time
  at 15px, a 56px key visual, the "Since Spring 2026" caveat where the server
  sends one. Today carries an accent underline and a "Today" chip; a show the
  viewer follows carries a quiet accent left rule and "On your list" for a
  screen reader — "a little highlight", not a badge. **Dates and today belong
  to the live season only**: a prev/next view is a catalogue browse, its grid
  is the shows of that season by weekday rather than a week, and printing this
  week's dates over Spring 2026's shows would be a claim about when they air.
  There the bar carries weekday names alone, nothing is marked today, and the
  window opens on Monday. Nothing about *which* shows are in the week changed.
- 2026-09-17 — **Ready to watch is about the file, and the week's tick is
  about the episode** (FR-W1, FR-W5, owner, M16 batch 3; both diagnosed on
  production). Two shelves on Watch Now were deriving an answer from a
  neighbour's data, and both were wrong in the same way — the neighbour's
  question had a clause theirs did not.
  (1) **Ready to watch** was the client filtering New this week for its ready,
  unstarted rows, so the shelf inherited "and it aired in the last seven days".
  One-Room TA finished airing on 2026-08-27 with two episodes ready, and Watch
  Now offered neither. The shelf is now its own server-side answer: ready file,
  show watching / planned / on hold, not started and not watched, any air date,
  newest file first. On hold is included here where New this week excludes it —
  a paused show is exactly the one "the file is here" might restart, and it
  costs one tile rather than a week of broadcasts.
  (2) **The This-week tick** was looked up by *show* in the same New this week
  list, which answers about that show's newest aired episode — so Friday's
  Slime slot, which names episode 23, drew "✓ Watched" from episode 22. The
  tick now belongs to the episode the tile names, and an appointment whose
  broadcast has not happened never carries one: the server answers FR-W5 for
  that episode only where it has already aired, and sends nothing at all
  otherwise.
  Also in the same pass: an episode row or card with **no still** falls back to
  the show's backdrop, then to its key visual framed in the 16:9 slot, instead
  of the striped placeholder — and the "No episode pictures for this show"
  caption is gone, because the fallback is a better answer than a sentence
  explaining an empty box. Stills come from TMDB alone, reached through the
  offline id map, so a show the map cannot reach (One-Room TA, AniList 205068)
  would have kept fourteen stripes for ever.
- 2026-09-17 — **Absolute episode numbering on sequels** (FR-A4, owner, M16
  batch 3, and the item deliberately held back from the 2026-09-14 matching
  pass). Some groups never restart the count: SubsPlease released *Jujutsu
  Kaisen* season two as `- 25` through `- 47` while AniList numbers that entry
  1–23, so `Jujutsu Kaisen S2 - 01` matched nothing and `Jujutsu Kaisen - 01`
  was season *one's* first episode. Both endings are bad and the second is the
  one the non-negotiables forbid, which is why this waited for its own item:
  "a release numbered 40 of a two-season franchise needs the relation graph,
  and guessing at it means a wrong file rather than a missing one."
  So it is not guessed. The offset is the **sum of the episode counts of the
  entry's `PREQUEL` chain**, walked through rows Arc has already cached, and
  the whole rule **declines** — leaving the entry exactly as it was before —
  the moment any part of that sum is unknown: a prequel that is not cached, one
  with no published episode count, one **still airing** (its total is a promise
  and an off-by-one lands inside its own band, where the season check is blind),
  two countable prequels at one hop (which of them the group was counting is the
  very thing that must not be inferred), a chain that loops or runs past ten. Films, OVAs and specials in the chain are
  skipped rather than counted, because no group counts *Jujutsu Kaisen 0* — an
  assumption, and written down as one.
  With an offset, two more query forms are asked (`Jujutsu Kaisen - 25` and the
  dashless `Jujutsu Kaisen 25`, behind the romaji short forms, because that is
  the shape the group that numbers this way writes), and a release carrying
  that number is accepted as the episode the catalogue numbers 1 — but only
  from a release that **names no season**, and never while a season-marked
  release for the same episode is in the same pool. An explicit answer beats an
  inferred one; `- 24`, season one's last, is still season one's last; and the
  chosen release's log line says `absolute numbering: release 25 = episode 1`,
  because a file named 25 landing in an episode row numbered 1 is the one pick
  nobody would otherwise be able to explain.
- 2026-09-17 — **Three corrections from the owner's own use** (FR-W1, FR-S1,
  FR-W3, owner, M16 batch 4).
  (1) **Continue watching must not offer an episode that is effectively
  finished.** It was listing episodes at 94 % — past the point at which Arc
  itself has recorded them as watched and told MyAnimeList so — because the
  shelf's end bound was 95 % or the last minute. It is now the completion mark
  (90 %) *or* three minutes left, whichever comes first, and the episode that
  takes its place is the next one under Ready to watch. Resuming into the last
  minutes is still allowed when the viewer asks for that episode themselves:
  what changed is what Arc *offers* unprompted.
  (2) **The player starts playing when the page loads.** Choosing the episode
  was the decision; a play button waiting to be found afterwards is a second
  one. The browser's autoplay policy is respected rather than fought: one
  audible attempt, one muted retry with a quiet "Tap to unmute" pill, and then
  the ordinary play button, with no error shown for a refusal that is the
  browser's prerogative.
  (3) **The mark-watched control says what pressing it does.** It was a tick
  that stayed a tick, so on a watched episode the only control on the bar with
  two states looked like an invitation to watch it again. It is now ✓ "Mark
  watched" before and ✕ "Mark unwatched" after. The exception is the one
  episode where there is nothing to press — watched because the list's progress
  has passed it (FR-W5) — which keeps the ✓ and the tooltip pointing at where
  the undo lives.
- 2026-09-18 — **The demo account** (FR-D5, owner, M16). Four decisions, in the
  owner's words. (1) *"A dedicated demo account, invite-created by the owner,
  that the professor — who does not know anime and has no MyAnimeList account —
  will log into."* (2) *"Arc knows it is the demo account through a new
  `users.is_demo` boolean column (NOT NULL, default false)."* (3) *"The demo
  list is seeded with the existing CLI (`python -m arc.cli demo-list`), which
  must grow enough to seed a plausible list."* (4) *"'How Arc works' lives as a
  top-nav entry (demo account only) plus a one-line dismissable first-visit
  strip on Watch Now (demo account only) pointing at it."*
  A column rather than a configured address, because which account is the
  professor's is a fact about the deployment and the database is where a
  deployment keeps facts — and because it has to be removable from the Users
  tab when the course ends. Presentation only, and deliberately: the page reads
  no per-user data and offers no action, so there is nothing for the flag to
  get wrong. The *route* is not gated even though the entry is, because a link
  that 404s for the person it was sent to is worse than a page one extra
  account can read. `demo-list` grew `--status`, `--progress` and `--demo`
  (one status group and one progress per invocation, because that is the shape
  of a plausible list) and a sibling `recs` command produces one recommendation
  run for an account through the same service `POST /api/recs/runs` uses — so
  every page of the demo account has content without anybody signing in as it.
  Nothing about acquisition, matching or MAL changed: a seeded entry goes
  through `set_list_entry` like any other, which is what stamps FR-A9's
  activation, and the demo account has no MAL link for a write to reach.
- 2026-09-18 — **Audio track choice prefers the original, not the first track**
  (FR-P2, owner, M16). In the owner's words: *"series default language is jap,
  and sub is en. if there is no jap and the default is korean so be it, but jap
  is preferred."* The order is therefore: the configured language (Japanese),
  then any track that is not an English dub with the muxer's default first,
  then the muxer's default, then the first track. Only the last rule is what
  Arc used to do after the first one failed, and it was wrong often enough to
  matter: a dual-audio release lists English first as often as not, so an
  episode whose original track was untagged — or whose original language is
  Korean or Chinese, which is most webtoon adaptations and every donghua — had
  the dub burned into the only rendition Arc keeps. "Not English" is a proxy
  for "the original" rather than a fact about the file, and it is the best one
  available from an ffprobe payload: the original audio of a show Arc acquires
  is essentially never English. An untagged (`und`) track counts as not
  English for the same reason. Subtitles are untouched (English text track,
  already correct), and so is acquisition, which has ranked English dubs below
  every subbed release since the FR-A3 amendment of 2026-09-14.
- 2026-09-18 — **Own failures on Watch Now** (FR-W6, owner's M16 scope line of
  2026-09-12, built today). Two kinds of failure reach the page a person
  actually opens: an episode they are waiting for that has stopped — FR-P4's
  broken transcode, FR-A6's "no release found" — and their own MyAnimeList
  writes that did not land (FR-M6). Until now both were only discoverable by
  going to look: the show page for the episode, the sync page for the write,
  and the owner's own fortnight of use is what turned "it is in the UI
  somewhere" into a requirement. Four decisions inside it. **A live want is the
  definition of "mine"**, rather than the list: a dropped want (FR-T2) is
  somebody who stopped waiting, and a broken episode nobody wants is an admin's
  business and not a user's. **The banner is quiet** — no colour on the strip,
  no icon, the reason in the muted voice — because an episode that cannot play
  is a fact, and shouting it does not fix it. **Dismissal is per failure, per
  account, in that browser**, with no server round trip and nothing written
  anywhere else: one reader's attention is not a property of the failure, which
  stays on the show page either way, and a "dismissed" column would have been a
  second source of truth about something the episode row already knows.
  **A failure that recurs comes back**: every row carries a key built from the
  episode, its state and the moment it changed, so FR-A6's daily retry mints a
  new row and yesterday's dismissal cannot hide today's news. Nothing global
  and nothing new in the database: the admin jobs tab (FR-D3) is still the
  whole-queue view, an admin gets the same page about their own shows as
  everybody else, and notifications stay out of scope (§8).
- 2026-09-18 — **Per-show override editor** (FR-A3, FR-D2, owner, M16). The
  override the ranker has honoured since M6 became something the product can
  write. It lives in two places for one reason: the question it answers ("why
  does this show keep arriving from the wrong group?") is asked on the **show
  page**, which is also the only screen that knows which show is meant — so
  that is where an override is created, in a control no non-admin ever sees.
  Admin → Rules keeps the list and gains the two things one does to a rule that
  already exists, Edit and Remove; it deliberately has no "add", because adding
  needs a show picker and there is already a search page. Three smaller calls.
  **The groups field is comma-separated text** rather than the global list's
  ordered pills: an override is typically one group, and a box you can paste
  into beats three clicks for that. **An override that names neither field is a
  deletion**, so "back to the global rules" is one state rather than two — an
  empty stored row would appear in the admin table as a show not following the
  global rules while following them exactly. And **nothing is re-ranked**: like
  every other rule change, it applies to the next search and leaves anything
  already downloading alone.
