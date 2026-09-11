# Arc — M15 UI overhaul: design brief

> Written 2026-09-10 for the design pass (Claude Design). Companion to
> [spec.md](../spec.md) §5 (client pages), [architecture.md](../architecture.md)
> §2 (client layout) and [roadmap.md](../roadmap.md) M15. Owner decisions are
> marked **(owner)**; everything else is the orchestrator's reading of the spec
> and the built product. Before-screenshots of every page are in
> `notes/design/before/` (local, not committed).

## 1. What Arc is, in one paragraph

Arc is a self-hosted, invite-only anime server for a handful of people. A
person keeps a list of shows (imported from MyAnimeList or built in Arc); Arc
fetches the next few unwatched episodes of what they are watching, prepares
them for the browser, and plays them with subtitles burned in. Watching an
episode updates the list, and the list is pushed back to MyAnimeList. A model
recommends what to watch next and helps resolve files it could not match. The
owner uses it daily; a course reviewer who does not know anime will log in
once and click through everything.

## 2. Decisions already made

| Question | Decision |
|---|---|
| Overall look | **Quiet cinema (owner).** Dark-first, poster-forward, muted surfaces, one restrained accent. Covers carry the page; text stays calm. A personal media server, not a fan site. |
| Light theme | **Dark only (owner).** One theme, tuned properly. Tokens are still named so a light variant could be added later without renaming. |
| Phone | **Bottom tab bar on phones (owner).** Sidebar stays on desktop. Player goes full-screen landscape with minimal chrome. |
| Accent and identity | **Designer proposes (owner).** Accent hue, wordmark and a small logo mark come from the design pass. The current indigo `#7c8cff` and the text-only "Arc" are the starting point, not a constraint. |
| Stack | React 19, Tailwind v4 (`@theme` tokens in `client/src/index.css`), no component library. Design must be expressible as Tailwind utilities plus CSS variables. |
| Scope | Visual and layout only. **No API changes, no behaviour changes.** Every existing behaviour and test stays green; component changes are covered by the existing React Testing Library tests. |
| Copy | Existing page copy is kept unless the design needs it shorter. Tone: plain, second person, no exclamation marks, no marketing words. Anime jargon is allowed where a fan expects it, but every page must be understandable to the reviewer (see §9). |

## 3. Design principles

1. **Posters first, chrome second.** Cover art is the strongest visual asset Arc has. Surfaces around it are quiet: low-contrast borders, few boxes, generous spacing.
2. **One accent, used for one thing.** The accent means "the primary action here" (Play, Get picks, Confirm). Status uses its own small palette (ok / warn / error / muted) and never the accent.
3. **State is visible, not loud.** Every episode and file has a state (wanted, downloading, preparing, ready, failed, in review). States are shown as small, consistent badges with the same colour meaning everywhere.
4. **Calm empty and loading states.** Empty states say what would fill the page and how. Loading uses skeletons in the shape of the content, not spinners, except inside buttons.
5. **Density scales with the page.** Home and Show are poster-forward; Schedule is a grid; Review and Admin are information-dense tables and forms and may use a smaller type step.
6. **Nothing hides on phones.** Every action reachable on desktop is reachable on a phone, even if it takes one more tap.

## 4. Tokens to deliver

Deliver as a table of CSS variables (names below are the current ones; keep
them, add as needed) plus a type scale and a spacing scale.

- Colour: `--arc-bg`, `--arc-surface`, `--arc-surface-raised`, `--arc-border`,
  `--arc-text`, `--arc-text-muted`, `--arc-accent`, `--arc-accent-contrast`,
  `--arc-ok`, `--arc-warn`, `--arc-error`. Add: `--arc-info` (neutral status),
  `--arc-focus` (focus ring), hover/pressed variants of accent and surface, an
  overlay scrim for the player and dialogs.
- Type: one UI family (system stack acceptable; a webfont is fine if it is a
  single variable font ≤ 100 KB), a display step for page titles, body, small,
  and a monospace step for filenames and job ids. Line heights and weights per
  step.
- Spacing: 4-px base scale; page gutter, section gap, card padding, control
  height (desktop and touch: ≥ 44 px on phone).
- Radius: card, control, badge, poster. Shadows: at most two levels.
- Poster: aspect 2:3 everywhere; sizes S (thumb in rows), M (cards), L (Show
  header); a placeholder treatment for missing covers; an optional subtle
  "via MAL" marker when the catalogue fell back.
- Motion: durations and easings for hover, tab change, toast in/out; reduced
  motion respected.

## 5. Components to design

Each with default, hover, focus, disabled, loading and (where relevant) error
states.

| Component | Where used | Notes |
|---|---|---|
| App shell | every page | Desktop: left sidebar (brand, primary nav, phase-2 nav, "Acquisition paused" note for admins, account + log out). Phone: top bar with brand and account, **bottom tab bar** (Home, Schedule, Search, More). "More" opens a sheet with MAL, Recs, Review, Admin, log out. Player hides the shell. |
| Page header | every page | Title, one-paragraph explanation (kept short), optional right-side actions. |
| Poster card | Home, Search, Recs, Show relations | Cover, title, secondary line (format · eps · year), list-status control, optional badge. |
| Poster row | Home | Horizontal scroll on phone, grid on desktop; section title with count. |
| Episode row | Show | Number, title, air date (+ "est."), aired flag, state badge, watched mark, Play button. |
| State badge | Show, Review, Admin, Home | not wanted · wanted · searching · downloading (with %) · downloaded · matching · preparing (with %) · ready · failed · unavailable · in review. One shape, colour by meaning. |
| List-status control | cards, Show, Recs, continuations | Select: Not on list / Watching / Planned / Completed / On hold / Dropped; score select on Show. Must work as a touch target. |
| Buttons | everywhere | Primary (accent), secondary, danger (ignore, delete, deactivate), small/inline, icon-only with tooltip. Inline "arm then confirm" pattern for destructive actions (used in Admin). |
| Forms | Login, Invite, Recs mood, Review confirm, Admin rules/invites | Inputs, textarea with counter, number with range hint, select, tag input (release groups), field error text, form-level error. |
| Tables | Review (auto), Admin (users, invites, jobs, torrents) | Horizontal scroll inside the table on narrow screens; sticky header optional; row actions right-aligned. |
| Tabs | Review, Admin | Segmented buttons with counts; URL-synced. |
| Notices | Recs (not configured), MAL (connected), Review (suggestion), Player (progress not saved) | Info, success, warning, error; the suggestion box is a distinct "proposal" style, clearly not a result. |
| Error state | every page | Message + Try again; used for load failures. |
| Empty state | every page | Sentence + what fills it; optional single action. |
| Skeletons | Home, Show, Schedule, Recs | Poster and row skeletons. |
| Toast / status line | mutations | A page-level status line that survives the element it refers to disappearing (Review already does this). |
| Player chrome | Player | See §6.5. |
| Auth shell | Login, Invite | Centred card on the dark ground with the brand. |

## 6. Screens

For every screen: purpose, contents, states, phone notes. Data below is what
the API already provides; nothing new is requested.

### 6.1 Login and Invite
- Login: email, password, sign in; error line for bad credentials and for the
  rate limit ("try again in N minutes").
- Invite (`/invite/<token>`): "Set up your account", the bound address (read
  only), password + confirm (≥ 10 chars), Create account; expired/used token
  state; the invite expiry shown.
- Phone: single column, keyboard-safe.

### 6.2 Home
- Sections: **Continue watching** (episodes with saved progress: poster,
  show, episode number, progress bar, Play), **Behind on** (shows with
  ready-but-unwatched episodes, count), **New this week** (episodes that aired
  this week for followed shows, state badge). Empty state per section and for
  the whole page ("Add a show from Search or connect MyAnimeList").
- Phone: poster rows scroll horizontally; the first section is the hero.

### 6.3 Schedule
- Weekday grid (Mon–Sun) for the season; today highlighted; each cell a
  compact poster + title + episode number + time in the viewer's timezone,
  "est." marker for estimated dates; followed shows marked; add-to-list on
  unfollowed ones. Previous/next season navigation; season label.
- Phone: one day per column is too narrow — stack days vertically with today
  first, or a horizontal day switcher.

### 6.4 Search
- Search box (debounced), results as poster cards with list-status control and
  the "via MAL" marker; states: idle (hint), searching, results, none found,
  catalogue unavailable (error with retry).
- Phone: two-column poster grid.

### 6.5 Show
- Header: large cover, titles (preferred + native), format · episodes ·
  status · season · studio, genre chips, catalogue-fallback notice when the
  data came via MAL. List status + score controls. Synopsis.
- Episodes table: number, title, air date (est.), aired, state badge (with
  progress for downloading/preparing), watched mark + "Mark watched", Play.
  Failure reason surfaced for `failed`/`unavailable`.
- Relations (sequels, prequels) as a small poster row if present.
- Phone: cover left/text right header collapsing to stacked; episodes as
  rows with the Play button as the main touch target.

### 6.6 Player
- Full-bleed video on a black ground; the app shell is hidden. Top bar:
  back, show title, episode; Previous/Next episode. Bottom controls:
  play/pause, time / duration, seek bar with buffered range, volume/mute,
  fullscreen, playback speed. "Length" and **Mark watched** outside the video.
  Keyboard hints (Space, ← → 5 s, F, M) shown on desktop only.
- States: loading, playing, paused (controls visible), buffering, error
  (media unavailable), "progress could not be saved" banner after repeated
  failures, end-of-episode with Next.
- Phone: landscape full-screen; controls large; auto-hide after 3 s.

### 6.7 MyAnimeList
- Not linked: explanation + Connect. Linked: "Connected as <name>", last
  import, Import now, Disconnect (with inline confirm). Write log: filter
  (all / pending / ok / failed / skipped), rows with show, field, old → new,
  cause (watch / manual / revert / conflict), status, time, Revert on the
  newest ok row per field. Empty log state.
- Phone: log rows stack (show on one line, change on the next).

### 6.8 Recommendations
- Explanation; mood textarea (pre-filled from the last run, 300-char counter);
  **Get picks** (pending: "Finding picks…" with spinner); "N of 10 left
  today"; admin-only models line ("gemini-3.5-flash ✓ · … resting until
  tomorrow"). Run header: "Picks for: <mood> · <time>", "Picked 3 of 40
  candidates · <model>". Picks as poster cards with a 2–4 sentence case and
  list-status control. **New in your franchises**: compact rows (thumb,
  title, "Sequel to X, which you completed", list-status control). Not
  configured notice; inline error for a failed run; empty state.
- Phone: picks stack; continuations stay compact.

### 6.9 Match review
- Tabs: Pending (count) / Ignored / Auto-linked. Pending card: filename
  (monospace, wraps), directory · size · age, parsed chips (title · S/E ·
  group · resolution), confidence %. **Suggestion** box (distinct, "from
  <model>, never applied automatically"): suggested show, episode, confidence
  chip, reason, "Use this"; "No suggestion: <error>"; "Ask for a suggestion"
  when there is none. Candidates list (thumb, title, summary, score % ·
  reasons, Choose). Search another title. Confirm form: chosen show, episode
  number, Confirm, Ignore (not anime). Ignored: Reopen. Auto-linked: read-only
  rows. Empty states per tab; page-level status line after a confirm.
- Phone: card sections stack; Choose/Confirm are full-width.

### 6.10 Admin
- Tabs: Users / Rules / Jobs / Storage / Acquisition; review-queue count and
  link in the header.
- Users: accounts table (email, role, status, created, actions: make
  admin/user, deactivate/reactivate with inline confirm; own account
  read-only), invites form (email optional, expiry hours, Create → one-time
  link with copy and a "shown once" warning), open invites table with delete.
- Rules: each rule as a labelled field with "Default: x · Reset to default",
  help line with the allowed range; groups as a tag input; Save rules with
  success line and per-field 422 errors; per-show overrides list (read-only).
- Jobs: summary strip (pending/running/done/failed/cancelled, worker alive +
  heartbeat age), status/type filters, table (id, type, status, attempts,
  timing, last error truncated with expand, Retry/Cancel), auto-refresh.
- Storage: disk bar (used / total / free), retained sources/renditions/total,
  episodes retained, "Run retention sweep now", next-sweep preview list with
  per-episode Delete files (inline confirm) and Re-fetch.
- Acquisition: paused state with Pause/Resume, active wants table (user,
  show, episode, state), qBittorrent panel (reachable/version or error;
  torrents table with state, progress, size, speeds, episode).
- Phone: tabs scroll horizontally; tables scroll inside their container.

### 6.11 Not found / route error
- Centred message, link home; route error shows the message and a reload.

## 7. Layout and navigation spec

- Desktop ≥ 1024 px: sidebar 220–260 px, content max-width ~1200 px, page
  gutter 24–32 px.
- Tablet 768–1023 px: sidebar collapses to icons with labels on hover, or the
  phone model applies — designer's call, say which.
- Phone < 768 px: top bar (brand, account), bottom tab bar (Home, Schedule,
  Search, More). "More" sheet: MAL, Recs, Review, Admin (admins), log out.
  Content gutter 16 px. Player hides both bars.
- Keyboard: visible focus ring on every interactive element; tab order follows
  reading order; tabs and menus operable with arrows.

## 8. Accessibility

WCAG AA contrast on the dark theme (text on surface ≥ 4.5:1, badges ≥ 3:1),
touch targets ≥ 44 px on phone, no colour-only state (badges carry text),
reduced-motion variant, screen-reader labels for icon buttons, the player
controls reachable by keyboard.

## 9. The reviewer's walkthrough (demo prep tie-in)

The course reviewer will open Home, Schedule, Search, a Show, the Player,
MAL, Recommendations, Review and Admin once each. Every page must make sense
without anime knowledge: the one-paragraph explanation at the top of each page
stays, page titles are plain English, and status badges use words. A later
"How Arc works" page (see roadmap "Demo prep") will reuse the same tokens and
components; design a simple pipeline diagram style for it now (list → want →
search → download → prepare → play → sync).

## 9b. What the before-screenshots show (for the designer)

Taken 2026-09-10 at 1512 px, dev data, in `notes/design/before/`:

- **Home**: the "Behind on" grid dominates; most cards have a blank cover
  because the catalogue rows came from the MyAnimeList fallback without a
  poster yet. The design needs a good placeholder, and the layout should not
  depend on every card having art.
- **Schedule**: seven equal columns are too narrow at 1512 px; titles wrap to
  three lines, every card carries a "via MAL" pill and a select, and the
  page is visually the busiest. Reduce per-card chrome; consider a denser
  list per day with the poster as a small thumb.
- **Show**: banner + cover + metadata works; the episode table is plain and
  the state column ("Not wanted") reads like an error. Badges need meaning by
  colour and a calmer default.
- **Search**: the card grid is the closest to the intended look; the "via
  MAL" pill and the status select compete with the title.
- **Player**: bare `<video>` chrome on black; the top bar and the keyboard
  hint line are the only Arc elements. Needs designed controls.
- **MAL, Review, Admin**: table-heavy pages with sensible structure; type
  scale and row density are inconsistent between them (three different
  table styles).
- **Sidebar**: text-only nav with a "Phase 2" label that should not ship;
  no icons, no active-state beyond a background tint, no phone version.

## 10. Deliverables

1. Token sheet (§4) as CSS variables plus a Tailwind `@theme` block.
2. Component sheet (§5) with every state, desktop and phone.
3. Screen designs (§6) at 1440 px and 390 px for every screen and tab,
   including empty, loading and error states for Home, Show, Player, Recs and
   Review.
4. App shell and navigation (§7) including the phone tab bar and "More" sheet.
5. Wordmark and logo mark, favicon at 32 px, and the accent decision with
   contrast checks.
6. A short implementation note per screen: which existing component changes,
   any copy edits. Behaviour must not change.

## 11. Acceptance (from roadmap M15)

The owner signs off on each page in the browser; the full suite stays green;
Home and Player are usable on a phone; screenshots before/after are kept in
`notes/design/`.
