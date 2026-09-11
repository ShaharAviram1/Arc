# Handoff: Arc — Apple TV–grammar UI overhaul (M15)

## Overview

A dark, content-led redesign of the Arc anime-server client. It replaces the current
utilitarian tables-and-selects UI with a media-app interface: framed key art, horizontal
shelves, roomy grouped rows, and a designed video player. The visual grammar is borrowed
deliberately from Apple's TV app (framed artwork, white primary action, few items with a lot
of air) and adapted to anime's realities (tall 2:3 key visuals, the weekly broadcast
appointment, cours, franchise sprawl, Japanese titles).

Scope of this pass: **Watch Now (home), Browse, My List, Show, Player**, plus a **Brand** page
documenting the design rules. Screens not covered here are listed under
[Screens not designed in this pass](#screens-not-designed-in-this-pass).

## About the design files

The files in `source/` are **design references created in HTML**. They are prototypes that
show intended look and behaviour. They are **not production code to copy**.

The task is to **recreate these designs inside the existing Arc client** — React 19 +
TypeScript + Tailwind v4 + react-router, which already exists at `client/` in the Arc repo —
using its established patterns, data hooks, and token names. Do not port the prototype's
markup or its inline styles.

Two things to know about the prototype format before you open the source:

- The `.dc.html` files are a component format with `{{ }}` template holes and a
  `class Component` logic block. Treat them as a **spec to read**, not a codebase.
- All styling in them is **inline by necessity of that format**. In the real client, styling
  belongs in Tailwind utility classes driven by the `@theme` tokens in
  `client/src/index.css`. The hex values in this README are the source of truth.

`Arc Apple TV — open in browser.html` is a single self-contained file — open it directly in a
browser, no server or build needed, and click through all six screens.

## Fidelity

**High fidelity.** Colours, type sizes, weights, tracking, control heights, radii, shadows,
gutters, section rhythm, and transition timings are all final and specified below. Recreate
the UI to match. The only placeholders are the artwork itself: every poster, still and key
visual in the prototype is a diagonal-stripe placeholder box labelled with its intended
aspect ratio. Wire those to real catalogue images.

Two known gaps, called out so you don't treat them as bugs:

- **No icon set.** The prototype uses text labels and typographic glyphs (`‹ › ⌄ →`) where an
  icon set would normally be used. If the codebase adopts an icon library, swap these for real
  icons at the same sizes.
- **Copy is written but not reviewed.** Episode synopses and show blurbs are plausible
  placeholder prose. Product copy on the Brand page and the UI labels are final.

## Behaviour constraint (important)

This is a **visual overhaul only**. Every existing query, mutation, route, keyboard shortcut,
progress-reporting behaviour and error path in the current client must survive unchanged.
Where the design moves a control (e.g. list-status controls off the tile and into a row), it
changes only the control's placement and appearance, never what it calls.

---

## Design tokens

Add these to the `@theme` block in `client/src/index.css`. Keep the existing `--arc-*` naming
convention. The existing dark palette is being replaced by the values below; keep the token
names so a light variant remains possible later.

### Colour

| Token | Value | Use | Contrast |
|---|---|---|---|
| `--arc-bg` | `#080b11` | Window. Blue-black, derived from the logo's cold end | — |
| `--arc-surface` | `rgba(255,255,255,0.055)` | Grouped cards, appointment cards, rule cards | — |
| `--arc-surface-raised` | `rgba(255,255,255,0.09)` | Secondary buttons | — |
| `--arc-surface-hover` | `rgba(255,255,255,0.055)` | Row hover fill | — |
| `--arc-surface-input` | `rgba(255,255,255,0.06)` | Search field | — |
| `--arc-nav-active` | `rgba(255,255,255,0.1)` | Active nav pill, segmented track | — |
| `--arc-border` | `rgba(255,255,255,0.1)` | Hairline on cards and artwork, at `0.5px` | — |
| `--arc-border-strong` | `rgba(255,255,255,0.18)` | Secondary button border | — |
| `--arc-hairline` | `rgba(255,255,255,0.12)` | Toolbar bottom rule, at `0.5px` | — |
| `--arc-text` | `#f2f4f8` | Primary label | 15.9:1 on bg |
| `--arc-text-muted` | `rgba(235,239,245,0.8)` | All secondary text | 9.8:1 on bg |
| `--arc-text-faint` | `rgba(235,239,245,0.45)` | Placeholder labels inside artwork **only** — never informational | — |
| `--arc-ember` | `#ff9d4d` | Tonight's broadcast, "Airs tonight", Brand eyebrows | 9.4:1 on bg |
| `--arc-spark` | `#ff7a29` | The live dot, arc gradient warm end | — |
| `--arc-arc-cold` | `#2e7ddb` | Arc gradient cold end | — |
| `--arc-action` | `#ffffff` | Primary action fill | — |
| `--arc-action-ink` | `#000000` | Text on primary action | 21:1 |

### The arc gradient — one use only

```css
--arc-progress: linear-gradient(90deg, #2e7ddb 0%, #9fd0ff 38%, #ffb679 72%, #ff7a29 100%);
```

This is the logo read as a bar: cold at the start, hot at the end, ending in a spark. It is
used for **exactly one thing — progress through a season, in My List.** Nothing else in the
product may use it.

**All playback progress is plain white (`#fff`)** on a `rgba(255,255,255,0.2)` track — hero
bar, Up Next tiles, episode stills, and the player scrubber. This distinction is load-bearing;
if the gradient marks everything it marks nothing.

### Spacing, radius, elevation, motion

| Property | Value |
|---|---|
| Page gutter | `40px` |
| Content max width | `1180px` |
| Section rhythm (gap between shelves) | `72px` |
| Shelf item gap | `24px` (appointment cards `16px`, browse grid `26px`) |
| Card padding | `18px` (rule cards `22px`) |
| Control heights | `44px` (chips, nav, segmented, player controls) · `48px` (primary/secondary pills, play button) |
| Minimum hit target | `44px`. No exceptions anywhere in this design. |
| Radius | hero/brand card `20px` · player control bar `20px` · card `16px` · row `14px` · artwork `12px` · episode still `9px` · small thumb `5–6px` · segmented button `8px` in a `10px` track · pill/dot `999px` |
| Border width | `0.5px` everywhere (hairlines). Never `1px`. |
| Shadow, hero | `0 30px 70px rgba(0,0,0,0.66)` |
| Shadow, tile | `0 14px 34px rgba(0,0,0,0.5)` (browse grid `0 14px 30px rgba(0,0,0,0.46)`) |
| Shadow, player bar | `0 20px 48px rgba(0,0,0,0.55)` |
| Blur, toolbar & player bar | `saturate(180%) blur(24px)` / `blur(28px)` |
| Blur, glass buttons | `blur(20px)` |
| Screen enter | `rise 360ms cubic-bezier(0.2,0.8,0.2,1)` — `opacity 0→1`, `translateY(8px)→0` |
| Tile hover | `transform: translateY(-5px)`, `240ms cubic-bezier(0.2,0.8,0.2,1)` |
| Primary button hover | `transform: scale(1.02)`, `220ms cubic-bezier(0.2,0.8,0.2,1)` |
| Row hover | `background 200ms ease` |
| Nav / chip transition | `200ms ease` |
| Reduced motion | `@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }` |

### Typography

System font only — no webfont. This keeps Dynamic Type / OS text-size settings working, so
**no text container may have a fixed height.**

```css
font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', system-ui, sans-serif;
```

Japanese titles use a separate stack, because CJK glyphs need air that Latin does not:

```css
font-family: 'Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'Yu Gothic', 'Noto Sans JP', system-ui;
letter-spacing: 0.02em;
```

Filenames, IDs and clocks use `ui-monospace, 'SF Mono', monospace`.

| Style | Size / line-height | Weight | Tracking | Use |
|---|---|---|---|---|
| Hero title | `46px / 1.04` | 600 | `-0.03em` | Home hero, Show hero (`44px` on Show) |
| Hero title, JP | `17px` | 400 | `+0.02em` | Original Japanese title under the hero title |
| Page title | `40px / 1.08` | 600 | `-0.028em` | Browse, My List, Brand |
| Brand section | `28px` | 600 | `-0.022em` | Brand page sub-sections |
| Section | `24px` | 600 | `-0.02em` | Shelf headings |
| Card title | `20px` | 600 | `-0.015em` | Brand rule cards |
| Broadcast time | `28px` | 600 | `-0.02em`, `tabular-nums` | Appointment card time |
| Hero body | `16px / 1.5` | 400 | — | Hero episode line |
| Body | `16px / 1.6` | 400 | — | Show blurb, rule card body |
| Row title | `16px` | 500 | — | Episode and My List row titles |
| Tile title | `16px` | 500 | — | Up Next tile titles |
| Tile title, small | `15px` | 500 | — | 2:3 tile titles (`172px` and smaller) |
| Nav / secondary pill | `15–16px` | 400 (600 active) | — | Toolbar nav, buttons |
| Sub-lede | `14px` | 400 | — | Shelf descriptions, tile meta, state column |
| Footnote | `13px` | 400 | — | Episode meta, tile meta, credits role |
| Eyebrow, hero | `12px` | 600 | `+0.1em`, uppercase | "CONTINUE WATCHING" |
| Eyebrow, day | `12px` | 600 | `+0.08em`, uppercase | Appointment card day |
| Eyebrow, brand | `11px` | 600 | `+0.07em`, uppercase | Brand rule card tag |

Weights top out at **600**. Do not use 700 — it flattens the hierarchy, since every heading
then shouts at the same volume.

---

## Artwork shapes

Anime's native artwork is the tall key visual, so unlike Apple's TV app this design is not a
16:9 world. Two ratios, each with a fixed meaning:

| Shape | Ratio | Radius | Used for | Widths |
|---|---|---|---|---|
| Key visual | `2/3` | `12px` | Show artwork in shelves and grids | `172px` shelves · `160px` franchise · `168px+` browse grid |
| Small thumb | `2/3` | `5–6px` | Rows and appointment cards | `38px` appointment · `46px` My List row |
| Episode still | `16/9` | `12px` shelf, `9px` row | Up Next tiles, episode rows | `280px` tile · `152px` row |
| Hero key visual | `21/9` | `20px` | Home and Show hero, framed and inset | full content width, max `1180px` |

**Artwork is framed, never bled.** The hero is a rounded card inset `40px` from the window on
a soft shadow. Bleeding art to the viewport edge under a gradient wash is the streaming-service
move this design deliberately rejects.

**Titles go below the tile,** never burned over the image. Tiles carry no scrim and no badge —
only the progress line along the bottom edge. (Exception: the franchise rail's watch-order
number sits top-left on a `rgba(6,9,14,0.66)` + `blur(14px)` glass chip, `22px` tall,
`0.5px rgba(255,255,255,0.18)` border, `11px` tabular.)

Placeholder boxes in the prototype are
`repeating-linear-gradient(45deg, #161d2b 0 9px, #101725 9px 18px)` — replace with real images.

---

## The logo

`assets/arc-logo.png` — 1942×809, transparent PNG. The mark is an electric arc striking the
"A" and forking into a branching discharge with sparks, travelling from cold blue on the left
to hot orange on the right and terminating in a glowing spark.

- **Toolbar size: `48px` tall**, in a `68px` toolbar.
- **Minimum size: `48px`.** Below that the lightning forks sinter into a single line and it
  becomes a visibly tamer mark. If you need a smaller lockup (favicon, compact header), ask
  the designer for a simplified variant rather than scaling this asset down.
- The artwork already contains the discharge — **do not overlay drawn lightning on it.**
- The chrome draws no brand line of its own; the toolbar rule is a neutral hairline. The mark
  and the My List progress gradient are the only places the arc appears.
- A raster PNG at this size will soften on high-DPI displays. Request an SVG or a 3× PNG
  before shipping.

---

## Screens

### 1. Toolbar (persistent chrome)

Present on every screen **except** the player, where it is hidden entirely.

- Sticky, `z-index 30`, height `68px`, horizontal padding `40px`, gap `22px`.
- Background `rgba(8,11,17,0.8)` with `backdrop-filter: saturate(180%) blur(24px)`
  (plus `-webkit-` prefix).
- Bottom rule: `0.5px` solid `rgba(255,255,255,0.12)`.
- **Left:** logo `<img>`, `height: 48px`, `width: auto`, `flex-shrink: 0`.
- **Centre:** nav, `margin: 0 auto`, gap `2px`. Items: Watch Now · Browse · My List · Brand.
  Each is `44px` tall, padding `0 16px`, radius `10px`.
  - Active: background `rgba(255,255,255,0.1)`, colour `#f2f4f8`, weight 600.
  - Inactive: transparent, colour `rgba(235,239,245,0.78)`.
- **Right:** search input then avatar, gap `12px`.
  - Search: `240 × 44`, radius `999px`, padding `0 18px`, `0.5px` border
    `rgba(255,255,255,0.14)`, background `rgba(255,255,255,0.06)`, `14px`,
    placeholder "Search". Typing navigates to Browse and filters it.
  - Avatar: `36px` circle, `0.5px` border `rgba(255,255,255,0.18)`, background
    `rgba(255,255,255,0.05)`, `13px` initial, `aria-label="Account"`.

Nav labels intentionally use media-app language rather than the current route names. Map them:
Watch Now → `/`, Browse → `/search`, My List → a new list view, Brand → documentation only
(do not ship this screen).

### 2. Watch Now (home)

Padding `44px 40px 96px`.

**Hero** (`max-width: 1180px`)
- Framed card: radius `20px`, `aspect-ratio: 21/9`, padding `40px`, content bottom-aligned,
  shadow `0 30px 70px rgba(0,0,0,0.66)`.
- Scrim over the art:
  `linear-gradient(0deg, rgba(6,9,14,0.9) 0%, rgba(6,9,14,0.34) 44%, rgba(6,9,14,0) 78%)`.
- Content block, `max-width: 620px`:
  1. Eyebrow "Continue watching" — `12px`, 600, `+0.1em`, uppercase, `rgba(235,239,245,0.8)`.
  2. Title — `46px / 1.04`, 600, `-0.03em`, `#fff`, `text-wrap: pretty`.
  3. Japanese title — `17px`, Hiragino stack, `+0.02em`, `rgba(235,239,245,0.78)`.
  4. Episode line — `16px / 1.5`, `rgba(235,239,245,0.85)`:
     "Episode 12 · The Land Where Souls Rest · 9 minutes left".
- **Action row below the card**, `margin-top: 22px`, gap `14px`, wrapping:
  - Primary: "Resume episode 12" — `48px` tall, padding `0 26px`, radius `999px`,
    background `#fff`, colour `#000`, `16px`, 600, with a CSS triangle play glyph
    (`border-left: 9px solid #000`, `6px` transparent top/bottom), gap `10px`.
    Hover `scale(1.02)`.
  - Secondary: "Episodes" — same height, padding `0 24px`, `0.5px` border
    `rgba(255,255,255,0.18)`, background `rgba(255,255,255,0.09)`, `blur(20px)`, `16px`.
  - Inline progress: flexible `min-width: 240px; max-width: 360px`, `4px` track
    `rgba(255,255,255,0.16)`, **white** fill at `59%`, then `14px` tabular
    "14:02 / 23:40".

**Shelf: Up Next**
- Heading `24px / 600 / -0.02em`; sub-lede `14px` `rgba(235,239,245,0.8)`:
  "Where you stopped, and the first episode you haven't seen."
- Horizontal scroller, gap `24px`, `scroll-snap-type: x mandatory`, items
  `scroll-snap-align: start`. Hide the scrollbar (`::-webkit-scrollbar { height: 0 }`).
- Tile: `280px` wide, `16/9`, radius `12px`, `0.5px` border `rgba(255,255,255,0.1)`,
  shadow `0 14px 34px rgba(0,0,0,0.5)`. Hover `translateY(-5px)`.
- Progress, only when partially watched: `3px` strip flush to the bottom edge inside the
  tile, track `rgba(255,255,255,0.2)`, fill `#fff`.
- Below the tile: title `16px / 500`, then a meta row — episode slug left
  ("S1 E12"), time left right-aligned ("9 min left"), both `14px`
  `rgba(235,239,245,0.8)`.
- Four items. Ready-but-unstarted episodes show no progress strip and "Ready" in the
  right meta slot.

**Shelf: This week in Japan** — the anime-specific element
- Sub-lede: "Seasonal anime keeps appointments. Times are Europe/Berlin."
- Appointment card: `210px` wide, padding `18px`, radius `16px`, gap `16px` between cards.
  - Default: background `rgba(255,255,255,0.055)`, border `0.5px rgba(255,255,255,0.1)`.
  - Tonight: background `rgba(255,122,41,0.07)`, border `0.5px rgba(255,122,41,0.3)`,
    day label `#ff9d4d`, and a `6px` `#ff7a29` dot before the label. **This is the only
    orange dot on the page.**
  - Contents: day eyebrow (`12px`, 600, `+0.08em`, uppercase) → broadcast time
    (`28px`, 600, `-0.02em`, tabular) → `38px` 2:3 thumb + show title (`14px`) and episode
    (`13px`) → acquisition state (`13px`; `#f2f4f8` when ready, muted otherwise), e.g.
    "Arc will search at 20:00", "Ready · downloaded early", "Waiting for a release".

**Shelf: Catch up**
- Sub-lede: "Falling a cour behind is normal. Arc keeps them ready." Deliberately not a
  guilt mechanic — counts are prose, muted, `13px`, and there is **no badge on the artwork**.
- `172px` 2:3 key visuals, gap `24px`, hover `translateY(-5px)`; title `15px / 500`,
  meta `13px` ("Season 4 · 17 episodes behind").

**Shelf: Because you finished Frieren**
- Same tile spec as Catch up. Sub-lede names the studio connection:
  "Madhouse, and four others with the same unhurried register."
- Meta line credits the studio like an auteur: "Madhouse · 12 episodes".

### 3. Browse

- Page title `40px`, lede `16px / 1.55` `max-width: 66ch`. Lede is state-dependent:
  empty query → "Everything in the catalogue Arc has cached, newest first.";
  with a query → `12 results for "<query>"`.
- Genre chips: `44px` tall, padding `0 18px`, radius `999px`, `0.5px` border.
  - Active: background `rgba(255,255,255,0.14)`, border `rgba(255,255,255,0.24)`, weight 600.
  - Inactive: background `rgba(255,255,255,0.05)`, border `rgba(255,255,255,0.12)`,
    colour `rgba(235,239,245,0.82)`.
  - Options: All · Action · Slice of life · Fantasy · Isekai · Drama · Sports.
- Grid: `repeat(auto-fill, minmax(172px, 1fr))`, gap `26px`, `max-width: 1180px`.
  2:3 key visual, title `15px / 500`, meta `13px`.

### 4. My List

- Page title `40px`; lede: "Anything set to Watching is what Arc fetches episodes for. The
  list stays in step with MyAnimeList."
- Status filter chips (same chip spec): Watching · Plan to watch · On hold · Completed ·
  Dropped.
- Rows, `max-width: 1080px`, `gap: 2px`, each a full-width `<button>`: padding `12px 14px`,
  radius `14px`, hover background `rgba(255,255,255,0.055)`, `200ms ease`.
  Columns left to right:
  1. `46px` 2:3 thumb, radius `6px`.
  2. Title `16px / 500` + meta `13px` ("Episode 12 of 24 · Madhouse"), flexible.
  3. **Season progress**, fixed `160px`: `4px` track `rgba(255,255,255,0.16)` with the
     **arc gradient** fill, then the percentage `13px` tabular in a `42px` right-aligned box.
     This is the one place the gradient appears.
  4. Airing state, fixed `124px`, right-aligned, `14px`; `#f2f4f8` for "Airing",
     muted for "Finished".

### 5. Show

- Hero: identical framing to Home (`21/9`, radius `20px`, same shadow), scrim
  `linear-gradient(0deg, rgba(6,9,14,0.9) 0%, rgba(6,9,14,0.32) 46%, rgba(6,9,14,0) 80%)`.
  Content `max-width: 660px`: title `44px / 1.06`, 600, `-0.03em`, then the Japanese title
  `17px` Hiragino.
- Action row, `margin-top: 22px`, gap `12px`: primary "Play episode 9" (white pill, `48px`),
  then two glass pills "Watching ⌄" and "Rated 9 ⌄" (`48px`, padding `0 22px`).
  These two replace the current `<select>` controls but must call the same mutations.
- Blurb `16px / 1.6`, `max-width: 74ch`. Then a credits/meta line `14px` muted:
  "Studio Bind · Summer 2026 · 14 episodes · 8 watched · subtitled, Japanese audio".
- **Cour selector** — anime-specific. Segmented control beside the "Episodes" heading:
  track radius `10px`, background `rgba(255,255,255,0.1)`, padding `2px`; buttons `44px`
  tall, padding `0 18px`, radius `8px`; active background `rgba(255,255,255,0.18)`,
  weight 600. Options: "Season 3 · Cour 1" · "Season 3 · Cour 2" · "Specials".
- **Episode rows**, `gap: 2px`, each a `<button>`: padding `14px`, radius `14px`, hover
  `rgba(255,255,255,0.055)`, gap `20px`.
  1. `152px` 16:9 still, radius `9px`, with the white progress strip (`3px`) when part-watched.
  2. Flexible text column: `16. Title` at `16px / 500`; synopsis `14px / 1.5`
     `rgba(235,239,245,0.85)` with `text-wrap: pretty`; meta `13px` muted carrying date,
     duration, resolution and **release group** ("Aug 30 · 24 min · 1080p · SubsPlease") —
     the fansub reality is part of the information, not noise.
  3. State column, fixed `132px`, right-aligned, `14px`. Values and colours:
     "Watched" muted · "Ready to play" `#f2f4f8` · "Downloading 18%" muted ·
     "Searching" muted · "Airs tonight" `#ff9d4d` · "Not yet aired" muted.
     Note "Not wanted"-style resting states are muted, never error-coloured.
- **The franchise, in order** — anime-specific. Sub-lede: "Seasons, specials, the recap film
  and one side story — watch order, not release order." `160px` 2:3 cards, gap `20px`, each
  with the glass order chip top-left (`1`–`5`, and `—` for the side story) and
  kind line beneath ("TV · 23 episodes · 2021", "Special · 4 episodes", "Film · 98 minutes").
- **Made by** — studio treated as an auteur credit. `max-width: 720px`, rows with
  `0.5px` top border `rgba(255,255,255,0.1)`, padding `14px 0`: role label `190px`, `15px`
  muted; name `16px` `#f2f4f8`. Rows: Studio · Director · Series composition ·
  Character design · Music · Original work.

### 6. Player

Full-bleed, `position: absolute; inset: 0`, background `#000`. **Toolbar hidden.**

- Video area fills the screen.
- **Top bar**: padding `24px 28px`, background
  `linear-gradient(180deg, rgba(0,0,0,0.66), rgba(0,0,0,0))`.
  Back button `44px` circle, `0.5px` border `rgba(255,255,255,0.2)`, background
  `rgba(18,23,34,0.6)`, `blur(18px)`, glyph `‹`, `aria-label="Back to show"`.
  Beside it: show + season `16px / 600 / #fff`, then
  `Episode 9 · The Sword Saint · subs en, audio ja` at `14px rgba(235,239,245,0.82)`.
- **Control bar**: `position: absolute; left/right: 28px; bottom: 28px`, padding `16px 18px`,
  radius `20px`, background `rgba(18,23,34,0.64)`,
  `backdrop-filter: saturate(180%) blur(28px)`, `0.5px` border `rgba(255,255,255,0.16)`,
  shadow `0 20px 48px rgba(0,0,0,0.55)`.
  - Scrubber row, gap `16px`: elapsed `13px` tabular (`46px` box) → track `6px` radius `999px`
    `rgba(235,239,245,0.25)` containing **buffered** `rgba(235,239,245,0.42)` at `78%` and
    **played `#fff`** at `59%`, with a `16px` white knob
    (`box-shadow: 0 2px 8px rgba(0,0,0,0.6)`, `translateX(-8px)`) → remaining `-09:38`
    `13px` tabular right-aligned (`46px` box).
  - Control row, gap `10px`: play/pause `48px` white circle with `#000` glyph
    (`aria-label="Play or pause"`), then `44px` glass chips — `− 10s`, `+ 10s`, `Volume`,
    `Subtitles`, `1.0×` — then right-aligned `Mark watched` (glass) and `Next episode`
    (white pill, `14px`, 600).
- Keep every existing player behaviour: keyboard shortcuts, `ProgressReporter` wiring, the
  resume strip, and the "progress isn't being saved" warning strip. The design replaces the
  native `<video>` chrome and the header/footer layout, nothing else.

### 7. Brand (documentation — do not ship)

An in-design reference page: the logo at `max-width: 560px` on a
`radial-gradient(120% 140% at 50% 0%, #0f1725 0%, #080b11 70%)` panel, then eight rule cards,
the palette with contrast ratios, and the type scale. Useful to read during implementation;
it is not a product screen.

---

## Interactions & behaviour

| Trigger | Result |
|---|---|
| Toolbar nav item | Switch screen. Active pill updates. |
| Typing in toolbar search | Navigate to Browse and filter; lede switches to the result-count string. |
| Hero "Resume" / Up Next tile / episode row | Open the player for that episode. |
| Hero "Episodes" / Catch-up tile / Browse tile / My List row | Open the Show screen. |
| Player back `‹` | Return to Show. |
| Player play/pause | Toggle glyph between `❙❙` and `▶`. |
| Cour segmented control | Swap the episode list for that cour. |
| Genre chip / status chip | Filter the grid or list; active chip restyles. |
| Any tile hover | `translateY(-5px)` over `240ms`. |
| Any row hover | Background to `rgba(255,255,255,0.055)` over `200ms`. |
| Screen change | `rise` enter animation, `360ms`. |

**Loading, empty and error states are not designed in this pass.** The earlier M15 prototype
(`source/Arc M15 (other screens).dc.html`, "State" switch in its top bar) has a designed
skeleton, empty and error treatment for every screen; reuse that logic with this pass's
tokens. Skeletons there are `arcpulse 1.6s ease-in-out infinite` opacity pulses on
placeholder blocks.

**Responsive behaviour is not designed in this pass.** The prototype is a desktop layout
(shelves scroll horizontally, grids use `auto-fill minmax`, so it degrades gracefully, but
phone and tablet were not designed here). The M15 prototype has a full phone layout at 390px
— bottom tab bar with a "More" sheet, 44px minimum controls — if you need a starting point.

## State

All state in the prototype is local view state. No new data requirements.

| State | Type | Default | Drives |
|---|---|---|---|
| `screen` | `'home' \| 'browse' \| 'list' \| 'show' \| 'brand' \| 'player'` | `'home'` | Routing; also whether the toolbar renders |
| `query` | `string` | `''` | Search field; Browse lede and results |
| `genre` | `string` | `'All'` | Browse chips |
| `filter` | `string` | `'Watching'` | My List chips |
| `cour` | `string` | `'s3c2'` | Show episode list |
| `playing` | `boolean` | `true` | Player play/pause glyph |

In the real client, `screen` is react-router and the rest are component state or URL params —
`Browse`/`Search` and `Mal`/`Schedule` already sync their filters to the query string, and
that behaviour should be preserved.

## Assets

| Asset | Source | Notes |
|---|---|---|
| `assets/arc-logo.png` | Supplied by the client | 1942×809 transparent PNG. Request SVG or 3× before shipping. |
| Key visuals, posters, episode stills | **Not supplied** | Every one is a striped placeholder in the prototype. Wire to the catalogue's existing cover/banner URLs (`CoverThumb` already handles a null URL). |
| Icons | **None** | Text labels and typographic glyphs throughout. |

## Screen map — prototype to existing code

| Design screen | Existing files to change |
|---|---|
| Toolbar | `client/src/components/Layout.tsx` (currently a 240px left sidebar — this becomes a top toolbar) |
| Watch Now | `client/src/pages/Home.tsx`; hooks in `client/src/lib/anime.ts` |
| This week in Japan shelf | Data from `client/src/pages/Schedule.tsx`'s season/airing queries |
| Browse | `client/src/pages/Search.tsx`, `client/src/components/AnimeCard.tsx` |
| My List | New view; list state from `client/src/lib/anime.ts`, `ListStatusControl.tsx` |
| Show | `client/src/pages/Show.tsx` (its episodes `<table>` becomes the episode rows) |
| Player | `client/src/pages/Player.tsx` |
| Tokens | `client/src/index.css` (`@theme` block, `--arc-*`) |
| Shared | `CoverThumb.tsx` (add the 2:3 / 16:9 / 21:9 shapes), `SourceBadge.tsx`, `ErrorState.tsx` |

## Screens not designed in this pass

These exist in the current client and have **no design here**. Do not leave them looking like
the old UI next to the new one — either apply this pass's tokens and row language to them, or
schedule a second design pass:

Schedule · Recommendations · Match review · MyAnimeList · Admin (users, rules, jobs, storage,
acquisition) · Login · Invite.

`source/Arc M15 (other screens).dc.html` is an earlier, complete pass over **all ten** screens
in a quieter dark palette, with implementation notes per screen and loading/empty/error states.
It is the best available reference for the seven screens above. Its palette differs from this
one — take its structure, not its colours.

## Files in this bundle

| File | What it is |
|---|---|
| `Arc Apple TV — open in browser.html` | **Start here.** Self-contained, offline, no build. Click through all six screens. |
| `assets/arc-logo.png` | The logo asset to add to the repo. |
| `source/Arc Apple TV v2.dc.html` | The design source for this pass. Read it for exact values. |
| `source/support.js` | Runtime the `.dc.html` files need to open locally. Not for production. |
| `source/Arc M15 (other screens).dc.html` | Earlier full pass over all ten screens; reference for the undesigned ones. Needs `Arc Screens.dc.html` beside it. |
| `source/Arc Screens.dc.html` | Child component of the M15 file above. |
