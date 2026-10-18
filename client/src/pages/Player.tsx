import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import { OfflineButton, OfflineReason } from '@/components/OfflineButton'
import { buttonClass, cx, FOCUS_RING } from '@/components/ui'
import { listErrorMessage, useSetListEntry } from '@/lib/anime'
import { isStatus, useMe } from '@/lib/auth'
import {
  alreadyDemanded,
  DEMAND_NOT_FOUND_RETRIES,
  demandEpisode,
  demandRefusal,
  formatClock,
  pendingLabel,
  resumeLabel,
  shouldResume,
  useMarkWatched,
  usePlayInfo,
  useReportProgress,
  useUnmarkWatched,
  wantsDemand,
  WATCHED_BY_PROGRESS_HINT,
  type DemandRefusal,
  type EpisodeRef,
  type NextSeasonRef,
  type PendingInfo,
  type PlayInfo,
  type WatchState,
} from '@/lib/playback'
import { rememberPosition } from '@/offline/cache'
import { downloads } from '@/offline/downloads'
import { useOffline } from '@/offline/network'
import { useDownload } from '@/offline/useDownloads'
import { HlsVideo } from '@/player/HlsVideo'
import { ProgressReporter } from '@/player/ProgressReporter'
import {
  clampToFront,
  fewSeeders,
  liveStatusLabel,
  measureDuration,
  planSeek,
  playableEdge,
  readFront,
  settlePending,
  type PendingVerdict,
} from '@/player/liveSeek'
import { chooseSource, useLocalCopy } from '@/player/source'
import { rememberSubtitles, subtitlesRemembered } from '@/player/subtitles'

/**
 * The player (spec §4.5 FR-S1–FR-S6, roadmap M8; chrome redesigned in M15).
 *
 * Full-bleed and outside the app's toolbar: the route is a sibling of the
 * layout under the same auth gate, so this page is the whole window.
 *
 * Everything the page needs comes from one call — the playlist URL, the
 * duration, where to resume, and the episodes either side — so there is no
 * second request to race with the video starting. The media element itself is
 * the source of truth for position; the server's `duration` only stands in
 * while the media has not reported its own.
 *
 * The controls are the page's own rather than the browser's (M15). Words that
 * a glyph says better — previous, next, watched, fullscreen, ±10s — are
 * glyphs, with the words moved into `aria-label` and `title` so nothing is
 * lost to a screen reader or a hover. The row reads in three groups, on a
 * three-column grid so the middle one is centred on the picture rather than on
 * whatever is left over: transport on the left (back 10, play/pause, forward
 * 10), what-to-watch in the middle (previous, next, watched), and fullscreen
 * on its own at the right.
 *
 * The end of an episode is **two moments**, not one (owner, 2026-09-17). The
 * completion mark (FR-S4, 90 %) is bookkeeping: Arc has recorded the episode,
 * and all the viewer needs is a receipt, so it gets a small "Marked as
 * watched" toast for four seconds and nothing else. The *end* is the decision
 * — with 1:30 left, and again once the media ends, a card offers the next
 * episode, staying put, and the show page. The overlay used to arrive at the
 * completion mark, which put a decision on screen ten minutes before there was
 * anything to decide.
 *
 * The bar's second pass is about what it *covers*. Subtitles are burned into
 * the picture just above the bottom edge, so the bar sits 12px off it, is
 * barely there (0.22, carried by the blur rather than the fill, with the
 * hairline left at full strength to draw the edge), is under 84px tall on a
 * desktop, and takes itself away after a second and a half of an idle pointer
 * — playing or paused alike. The line of keyboard hints that used to sit under
 * the controls is gone from the screen and lives in the play button's tooltip.
 *
 * The page also **starts playing on its own** (owner, 2026-09-17): opening an
 * episode is the decision, and a second press on a play button is a step
 * nothing was waiting for. The browser has the last word, so the attempt is
 * made once, falls back to muted once, and then stops and leaves the play
 * button — see :func:`beginPlayback`.
 *
 * Nothing below the chrome changed: the same reporter, the same shortcuts, the
 * same resume, the same warning strip — a button is simply a second way to
 * reach what the keyboard already did.
 */

/** ± this many seconds on an arrow key (FR-S6). */
const SEEK_STEP = 5

/**
 * How often a live player re-reads the download front from the element and
 * settles a pending seek (FR-P8, M20 T7). The numbers from the server arrive
 * every 5 s with the `/play` poll; the element's playlist moves on its own.
 */
const FRONT_CHECK_MS = 1000

/** Said under the download line when the release has few seeders (FR-P8). */
const FEW_SEEDERS_NOTE = 'Few seeders — the download may be slow.'

/** ± this many on the two round skip buttons. */
const SKIP_STEP = 10

/** How long the chrome waits, with nothing moving, before it gets out of the way. */
const CHROME_HIDE_MS = 1500

/**
 * How long a click on the video waits to find out whether it was half of a
 * double one. Long enough for a deliberate double-tap, short enough that a
 * single tap does not feel like it was ignored.
 */
const DOUBLE_CLICK_MS = 250

/**
 * How close a touch `pointerdown` and a `touchstart` have to be to count as
 * one press. They are dispatched back to back; two real taps are never this
 * close together.
 */
const SAME_PRESS_MS = 40

/** Pointer moves are continuous; this is how often one is allowed to re-render. */
const ACTIVITY_THROTTLE_MS = 400

/**
 * How long "Resumed from 12:34" stays up before it takes itself away.
 *
 * The notice is a receipt, not a decision: it tells a viewer why the episode
 * started where it did, and once that has been read it is a box sitting on the
 * picture. The owner asked for it on a timer rather than waiting on a click
 * (owner, 2026-09-12). Five seconds is long enough to catch the eye and read
 * six words; Dismiss still works for anyone who wants it gone sooner.
 */
const RESUME_NOTICE_MS = 5000

/**
 * How much has to be left before the end-of-episode card comes up (owner,
 * 2026-09-17). A minute and a half is the end of an episode as a viewer
 * experiences it — the outro is running and the decision about what happens
 * next is live — where the completion mark is merely where Arc's bookkeeping
 * happens, ten minutes earlier on a 24-minute episode.
 *
 * `total > END_OVERLAY_SECONDS` guards the card: on something shorter than the
 * window itself the rule would put it up at the first frame, which is the
 * opposite of an *end*-of-episode card. Such a file still gets it on `ended`.
 */
const END_OVERLAY_SECONDS = 90

/** How long the completion receipt stays up before it takes itself away. */
const MARKED_TOAST_MS = 4000

/** The receipt itself. Six words, no action: nothing is being asked. */
const MARKED_WATCHED = 'Marked as watched'

/**
 * The offer after a muted autoplay (owner, 2026-09-17).
 *
 * "Tap", not "click": the browsers that refuse audible autoplay refuse it
 * hardest on a phone, which is where most of these pills will be read, and a
 * tap works on a mouse too.
 */
const TAP_TO_UNMUTE = 'Tap to unmute'

/** The mark-watched control's two labels (FR-W3, owner 2026-09-17). */
const MARK_WATCHED = 'Mark watched'
const MARK_UNWATCHED = 'Mark unwatched'
/** …and the non-actionable third state's, which says a fact, not an action. */
const WATCHED = 'Watched'

/** Fields that own their keystrokes; a shortcut must not fire inside one. */
const EDITABLE = new Set(['INPUT', 'TEXTAREA', 'SELECT'])

const NOT_READY_TITLE = 'Episode not ready'
/** A failed mark-watched used to say nothing at all, which reads as "done". */
const MARK_WATCHED_FAILED = 'Could not mark that watched. Try again.'
const UNMARK_WATCHED_FAILED = 'Could not unmark that. Try again.'
const NOT_READY_BODY =
  'Arc has no playable file for this episode yet. It may still be downloading or being prepared.'

/** Play on demand refused (FR-A14): the server's sentence goes under this. */
const CANNOT_FETCH_TITLE = 'Can’t fetch this episode now'
/** A demand that failed with no sentence from the server (no network, a 5xx). */
const DEMAND_FAILED = 'Could not ask Arc for that episode. Try again shortly.'
/** The sequel's first episode, at a finished season's end (FR-S5 item 3). */
const NEXT_SEASON = 'Next season'
const LOAD_FAILED_TITLE = 'Could not load this episode'
const LOAD_FAILED_BODY = 'Could not load this episode. Try again shortly.'

/** Offline, and this episode is not on the device (FR-S9). */
const OFFLINE_TITLE = 'You’re offline'
const OFFLINE_BODY =
  'This episode isn’t downloaded to this device. Episodes you have downloaded play without a connection.'

/**
 * A trip episode that lives only on this account's devices (FR-A12, M19), and
 * this device does not have it yet.
 */
const OFFLINE_ONLY_TITLE = 'Not on this device yet'
const OFFLINE_ONLY_BODY =
  'This episode is only for your device — keep it offline first. Arc downloads a trip’s episodes by itself while it is open.'

/** A download whose file the browser would not open (evicted, or damaged). */
const FILE_UNREADABLE =
  'The downloaded copy of this episode could not be read. Delete it on the Downloads page and download it again.'

/**
 * How often, at most, the position is written to the device while playing
 * (FR-S9), streamed or from the device's copy. It is what an offline resume
 * seeks to, and online it wins over the server's when it is the newer one
 * (FR-S2, 2026-10-08); pause, seek and the end write it at once.
 */
const LOCAL_POSITION_EVERY_MS = 5000

/**
 * The shortcuts. They used to be a line of text under the controls; the owner's
 * second pass took that line off the screen, because the bar is sitting on the
 * subtitles and a permanent crib sheet is the least valuable row in it. They
 * are not lost — they hang off the play button's tooltip, which is where a
 * viewer already points when they are looking for the transport.
 */
const KEY_HINT = 'Space · ← → 5s · F · M'

/**
 * How many progress writes have to fail in a row before the viewer is told.
 *
 * One failure is normal: a report is fire-and-forget, it is never retried, and
 * the next one is ten seconds away with a better position in it, so saying
 * anything about a single miss would be noise about something already fixed.
 * Three in a row is half a minute of a viewer's place going nowhere, which is
 * worth interrupting for — but only just, hence a strip rather than a dialog.
 */
const PROGRESS_FAILURE_LIMIT = 3

const PROGRESS_UNSAVED = 'Progress isn’t being saved — check your connection.'

/* --- The clothes the player's own controls wear ------------------------ */

/**
 * A glass disc, brighter than the app's chips: it sits over moving video.
 *
 * 36px on a pointer, 40px on a phone. The phone size is the one number in the
 * bar that had to give: seven controls in one row inside a bar inset 12px from
 * a 360px screen do not all fit at 44, and the owner's brief is explicitly
 * that this row holds three groups on a phone as well as a desktop. 40 still
 * clears WCAG's target-size minimum by a wide margin, and the one control a
 * thumb reaches for in the dark — play/pause — keeps its 44.
 *
 * A tablet is the third case (M18): desktop widths, but a finger. On a coarse
 * pointer at `md` and up there is room for the full 44, so it gets it.
 */
const PLAYER_ICON = cx(
  'inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border-[0.5px]',
  'md:h-9 md:w-9 pointer-coarse:md:h-11 pointer-coarse:md:w-11',
  'border-[rgba(255,255,255,0.18)] bg-[rgba(255,255,255,0.12)] text-white',
  'transition-colors duration-200 hover:bg-[rgba(255,255,255,0.2)]',
  'disabled:cursor-not-allowed disabled:opacity-40',
  FOCUS_RING,
)

/**
 * The clothes both of the small corner messages wear — the completion receipt
 * and the unmute offer. One class list because they share a shelf above the
 * control bar and a pill that is nearly the same as its neighbour is worse
 * than one that is identical.
 */
const PILL = cx(
  'rounded-full border-[0.5px] border-[rgba(255,255,255,0.16)] px-4 py-2 text-[13px]',
  'bg-[rgba(18,23,34,0.72)] text-[var(--arc-text-muted)] shadow-bar backdrop-blur-bar',
)

/* --- Glyphs ------------------------------------------------------------
 *
 * Inline SVG rather than an icon package: the design ships no icon set, the
 * client has no dependency for one, and four paths inherit `currentColor`
 * without a second asset or a second network request. Every one of them is
 * `aria-hidden`; the button around it carries the name.
 */

const STROKE_ICON = 'h-[17px] w-[17px] fill-none stroke-current md:h-4 md:w-4'
const SOLID_ICON = 'h-[17px] w-[17px] fill-current md:h-4 md:w-4'

/** ▶ / ❙❙, on the one white circle in the bar. */
function PlayPauseGlyph({ playing }: { playing: boolean }) {
  return (
    <svg
      aria-hidden
      focusable="false"
      viewBox="0 0 24 24"
      className="h-[15px] w-[15px] fill-current"
    >
      {playing ? (
        <>
          <rect x="6.5" y="4.6" width="3.8" height="14.8" rx="1.3" />
          <rect x="13.7" y="4.6" width="3.8" height="14.8" rx="1.3" />
        </>
      ) : (
        <path d="M6.9 5.5v13a1 1 0 0 0 1.53.85l10.1-6.5a1 1 0 0 0 0-1.7L8.43 4.65A1 1 0 0 0 6.9 5.5Z" />
      )}
    </svg>
  )
}

/** The conventional four corner brackets: outward to enter, inward to leave. */
function FullscreenGlyph({ exit }: { exit: boolean }) {
  return (
    <svg
      aria-hidden
      focusable="false"
      viewBox="0 0 24 24"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={STROKE_ICON}
    >
      {exit ? (
        <>
          <path d="M9.5 4.5v5h-5" />
          <path d="M14.5 4.5v5h5" />
          <path d="M9.5 19.5v-5h-5" />
          <path d="M14.5 19.5v-5h5" />
        </>
      ) : (
        <>
          <path d="M4.5 9.5v-5h5" />
          <path d="M19.5 9.5v-5h-5" />
          <path d="M4.5 14.5v5h5" />
          <path d="M19.5 14.5v5h-5" />
        </>
      )}
    </svg>
  )
}

/**
 * "CC" in a rounded frame — the mark every player uses for the subtitle
 * switch. Drawn solid while subtitles show, as an outline while they are off,
 * so the state reads without the pressed styling.
 */
function SubtitlesGlyph({ on }: { on: boolean }) {
  return (
    <svg aria-hidden focusable="false" viewBox="0 0 24 24" className={SOLID_ICON}>
      <rect
        x="2.75"
        y="5.25"
        width="18.5"
        height="13.5"
        rx="2.6"
        className={on ? 'fill-current' : 'fill-none stroke-current'}
        strokeWidth={1.8}
      />
      <text
        x="12"
        y="15.1"
        textAnchor="middle"
        fontSize="7.6"
        fontWeight="700"
        fontFamily="-apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif"
        className={on ? 'fill-black' : 'fill-current'}
      >
        CC
      </text>
    </svg>
  )
}

/**
 * ⟲ 10 / ⟳ 10 — the round-trip arrow every player uses for a ten-second jump,
 * with the number reading through the middle of it so the button says how far
 * without a word of text beside it.
 *
 * One drawing, mirrored for the forward case, and only the ring and its head
 * are mirrored: the number has to stay the right way round, so it sits outside
 * the mirrored group.
 *
 * The rewind is the drawing (owner, 2026-09-17: the arc was on the wrong side).
 * Its ring **opens on the left** — three quarters of a circle running from
 * half past ten, the long way over the top and down the right, to half past
 * seven — and the head sits at the top of that opening pointing down into it,
 * which is counter-clockwise, which is backwards. The forward glyph is that
 * flipped about the vertical: the opening on the right and the head turning
 * clockwise.
 */
function SkipSecondsGlyph({ back }: { back: boolean }) {
  return (
    <svg
      aria-hidden
      focusable="false"
      viewBox="0 0 24 24"
      className="h-[19px] w-[19px] md:h-[18px] md:w-[18px]"
    >
      <g
        className="fill-none stroke-current"
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
        transform={back ? undefined : 'translate(24 0) scale(-1 1)'}
      >
        <path d="M6.7 7.3A7.5 7.5 0 1 1 6.7 17.9" />
        <path d="M10.3 7.3H6.7V3.7" />
      </g>
      <text
        x="12"
        y="16.3"
        textAnchor="middle"
        className="fill-current"
        style={{ fontSize: '9.6px', fontWeight: 700, letterSpacing: '-0.04em' }}
      >
        10
      </text>
    </svg>
  )
}

/** ⏮ — a bar and a triangle pointing at it. */
function SkipBackGlyph() {
  return (
    <svg aria-hidden focusable="false" viewBox="0 0 24 24" className={SOLID_ICON}>
      <rect x="4.8" y="5.2" width="2.4" height="13.6" rx="1.2" />
      <path d="M19.4 6.1v11.8a1 1 0 0 1-1.55.84l-8.9-5.9a1 1 0 0 1 0-1.68l8.9-5.9a1 1 0 0 1 1.55.84Z" />
    </svg>
  )
}

/** ⏭ — the same shape, the other way round. */
function SkipForwardGlyph() {
  return (
    <svg aria-hidden focusable="false" viewBox="0 0 24 24" className={SOLID_ICON}>
      <rect x="16.8" y="5.2" width="2.4" height="13.6" rx="1.2" />
      <path d="M4.6 6.1v11.8a1 1 0 0 0 1.55.84l8.9-5.9a1 1 0 0 0 0-1.68l-8.9-5.9a1 1 0 0 0-1.55.84Z" />
    </svg>
  )
}

/**
 * The mark-watched control's face: a glyph in a circle, outlined while the
 * episode is unwatched and filled once it is — the same pressed / not-pressed
 * treatment the show page's pill wears, so the two places Arc offers this
 * agree about which state is which.
 *
 * The glyph inside is the *action*, not the state (owner, 2026-09-17). Before:
 * a check, for "mark watched". After: a **cross**, for "mark unwatched" — a
 * second tick said only what the filled disc already says, and on a control
 * that is a button in both states the viewer is owed the verb rather than the
 * fact. The one place the tick survives is the non-actionable pill for an
 * episode watched by list progress (FR-W5): nothing is being offered there, so
 * a fact is all there is to draw.
 *
 * Either glyph is punched out in the bar's own colour on a filled disc rather
 * than drawn in white, which would disappear into it.
 */
function WatchedGlyph({ filled, cross = false }: { filled: boolean; cross?: boolean }) {
  const ink = filled ? 'fill-none stroke-[#121722]' : 'fill-none stroke-current'
  return (
    <svg
      aria-hidden
      focusable="false"
      viewBox="0 0 24 24"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-[18px] w-[18px] md:h-[17px] md:w-[17px]"
    >
      <circle
        cx="12"
        cy="12"
        r="8.6"
        className={filled ? 'fill-current stroke-none' : 'fill-none stroke-current'}
      />
      {cross ? (
        <>
          <path d="m9.2 9.2 5.6 5.6" className={ink} />
          <path d="m14.8 9.2-5.6 5.6" className={ink} />
        </>
      ) : (
        <path d="m8.1 12.3 2.7 2.7 5.1-5.6" className={ink} />
      )}
    </svg>
  )
}

/**
 * Press play and do not care what the browser thinks of it.
 *
 * `play()` answers with a promise that **rejects** when the autoplay policy
 * refuses — and a rejection nobody catches is an unhandled one in the console,
 * which is noise about a decision the browser is entitled to make. (Older
 * browsers, and jsdom, answer with nothing at all, hence the `Promise.resolve`
 * around it.) The one caller that has something to *do* about a refusal is
 * :func:`beginPlayback`, which awaits it itself.
 */
function playQuietly(video: HTMLVideoElement): void {
  void Promise.resolve(video.play()).catch(() => undefined)
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return EDITABLE.has(target.tagName) || target.isContentEditable
}

/**
 * How much of the episode the browser has pulled down, 0–1. Wrapped because a
 * media element that has loaded nothing — or a test environment that does not
 * implement buffering — answers with an empty range or throws, and neither is
 * worth a broken bar.
 */
function bufferedFraction(video: HTMLVideoElement | null, total: number): number {
  if (video === null || total <= 0) return 0
  try {
    const ranges: TimeRanges | undefined = video.buffered
    if (ranges === undefined || ranges.length === 0) return 0
    return Math.min(1, Math.max(0, ranges.end(ranges.length - 1) / total))
  } catch {
    return 0
  }
}

function NotReady({ message }: { message: string }) {
  return (
    <div className="fixed inset-0 flex flex-col items-center justify-center bg-[var(--arc-bg)] p-6 text-center">
      <h1 className="text-[28px] font-semibold tracking-[-0.022em] text-[var(--arc-text)]">
        {NOT_READY_TITLE}
      </h1>
      <p className="mt-3 max-w-[52ch] text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
        {message}
      </p>
      <Link to="/" className={buttonClass('secondary', 'mt-7')}>
        Back to home
      </Link>
    </div>
  )
}

/**
 * Play on demand, waiting (FR-A14, M20): the episode is wanted and on its
 * way. The page polls `/play` every few seconds (`usePlayInfo`) and becomes
 * the player the moment `playlist_url` arrives; until then it says where the
 * episode is — "Searching", "Downloading 43 %", "Preparing" — with the
 * release's seeders once there is one.
 */
function PendingView({ info, pending }: { info: PlayInfo; pending: PendingInfo }) {
  const label = pendingLabel(pending)
  return (
    <div className="fixed inset-0 mx-auto flex max-w-lg flex-col items-center justify-center p-6 text-center">
      <p className="text-[14px] text-[var(--arc-text-muted)]">{info.anime.title.preferred}</p>
      <h1 className="mt-1 text-[28px] font-semibold tracking-[-0.022em] text-[var(--arc-text)]">
        Episode {String(info.episode.number)}
      </h1>
      <div
        role="status"
        className="mt-5 flex items-center gap-3 text-[16px] text-[var(--arc-text)]"
      >
        <span
          aria-hidden="true"
          data-testid="pending-spinner"
          className="h-4 w-4 animate-spin rounded-full border-2 border-[var(--arc-border)] border-t-[var(--arc-text)]"
        />
        <span>{label}</span>
      </div>
      {pending.seeders === null ? null : (
        <p className="mt-2 text-[13px] text-[var(--arc-text-muted)]">
          {pending.seeders === 1 ? '1 seeder' : `${String(pending.seeders)} seeders`}
        </p>
      )}
      <p className="mt-3 max-w-[46ch] text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
        It plays here as soon as it is ready.
      </p>
      <Link to={`/anime/${String(info.anime.id)}`} className={buttonClass('secondary', 'mt-7')}>
        Back to the show
      </Link>
    </div>
  )
}

/**
 * Play on demand, refused (FR-A14, M20): the server's own sentence — not in
 * the window, acquisition paused, storage held — and nothing spinning,
 * because nothing was queued and nothing is on its way.
 */
function DemandRefused({ refusal, animeId }: { refusal: DemandRefusal; animeId: number | null }) {
  return (
    <div className="fixed inset-0 mx-auto flex max-w-lg flex-col items-center justify-center p-6 text-center">
      <h1 className="text-[28px] font-semibold tracking-[-0.022em] text-[var(--arc-text)]">
        {CANNOT_FETCH_TITLE}
      </h1>
      <p
        role="alert"
        className="mt-3 max-w-[52ch] text-[16px] leading-[1.55] text-[var(--arc-text-muted)]"
      >
        {refusal.message}
      </p>
      <Link
        to={animeId === null ? '/' : `/anime/${String(animeId)}`}
        className={buttonClass('secondary', 'mt-7')}
      >
        {animeId === null ? 'Back to home' : 'Back to the show'}
      </Link>
    </div>
  )
}

/**
 * Pressing "Next season" (FR-S5 item 3, owner 2026-10-18): one press, end to end.
 *
 * An active sequel whose episode 1 is ready is a plain link. Otherwise the
 * press is first a **touch** of the sequel's entry (FR-A9) through the same
 * list write the show page uses — an `absent` sequel is added as Watching (an
 * explicit status change, so it goes to MyAnimeList like any other), a
 * `dormant` one is re-sent unchanged, which is all "Fetch this show" does —
 * and then, for an episode that is not ready, the demand (FR-A14) and the
 * waiting player, exactly as Next does. The demand works out the window
 * itself, so the episode the write just brought into it is accepted on the
 * spot; nothing waits for the reconciler.
 *
 * Offered only when the episode is ready or the server says it can be fetched
 * once the entry is active (`episode.fetchable`). A failed write says the
 * list's sentence; a refused demand says the server's.
 *
 * One instance per player, in {@link PlayerView}: the end overlay and the
 * Next glyph share it, so a press from either disables both and a refusal is
 * known to both.
 */
interface NextSeasonPress {
  /** The sequel on offer, or null where the show has a next row or no sequel. */
  season: NextSeasonRef | null
  offered: boolean
  direct: boolean
  asking: boolean
  refused: string | null
  press: () => void
}

function useNextSeasonPress(season: NextSeasonRef | null): NextSeasonPress {
  const navigate = useNavigate()
  const setEntry = useSetListEntry()
  const [asking, setAsking] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  const episode = season?.episode ?? null
  const offered = episode !== null && (episode.ready || episode.fetchable === true)
  /** Nothing to do but go there: no touch, no fetch. */
  const direct = season !== null && season.list_state === 'active' && season.episode.ready

  const press = async (sequel: NextSeasonRef) => {
    const target = `/watch/${String(sequel.episode.id)}`
    try {
      if (sequel.list_state !== 'active') {
        await setEntry.mutateAsync(
          sequel.list_state === 'absent'
            ? { animeId: sequel.anime_id, status: 'watching' }
            : { animeId: sequel.anime_id },
        )
      }
    } catch (error: unknown) {
      setAsking(false)
      setRefused(listErrorMessage(error))
      return
    }
    if (sequel.episode.ready) {
      void navigate(target)
      return
    }
    try {
      await demandEpisode(sequel.episode.id)
    } catch (error: unknown) {
      setAsking(false)
      setRefused(demandRefusal(error)?.message ?? DEMAND_FAILED)
      return
    }
    const state: WatchState = { demanded: true }
    void navigate(target, { state })
  }

  return {
    season,
    offered,
    direct,
    asking,
    refused,
    press: () => {
      if (season === null || asking) return
      setAsking(true)
      setRefused(null)
      void press(season)
    },
  }
}

/** "Next season: Iruma-kun 3 · Episode 1" — how the overlay and the glyph name it. */
function nextSeasonName(season: NextSeasonRef): string {
  return `${season.title} · Episode ${String(season.episode.number)}`
}

/**
 * Why the sequel's episode is not on offer: "hasn't aired yet" for a sequel
 * that is only announced or whose episode 1 has not aired (`aired: false`),
 * "isn't ready yet" otherwise.
 */
function nextSeasonUnavailable(season: NextSeasonRef): string {
  const why = season.aired === false ? 'hasn’t aired yet' : 'isn’t ready yet'
  return `${nextSeasonName(season)} ${why}`
}

/**
 * The Next glyph when the season ended here and a sequel is offered (FR-S5
 * item 3): the same press as the overlay's "Next season" — the same instance
 * of it. Disabled, with the reason as its label and tooltip, when the
 * sequel's episode is neither ready nor fetchable, or after a refusal. The
 * refusal is announced here only when the end overlay is not up to say it
 * (`announce`), so a screen reader hears it once.
 */
function NextSeasonLink({
  season,
  sequel,
  announce,
  glyph,
}: {
  season: NextSeasonRef
  sequel: NextSeasonPress
  announce: boolean
  glyph: ReactNode
}) {
  const { offered, direct, asking, refused, press } = sequel
  const name = `${NEXT_SEASON}: ${nextSeasonName(season)}`

  if (offered && direct) {
    return (
      <Link
        to={`/watch/${String(season.episode.id)}`}
        aria-label={name}
        title={name}
        className={PLAYER_ICON}
      >
        {glyph}
      </Link>
    )
  }
  const usable = offered && refused === null
  const label = refused ?? (usable ? name : nextSeasonUnavailable(season))
  return (
    <>
      <button
        type="button"
        disabled={!usable || asking}
        onClick={press}
        aria-label={label}
        title={label}
        className={PLAYER_ICON}
      >
        {glyph}
      </button>
      {refused === null || !announce ? null : (
        <span role="alert" className="sr-only">
          {refused}
        </span>
      )}
    </>
  )
}

/**
 * A neighbouring episode, as a glyph.
 *
 * One that is not ready is still shown, disabled, with its state as the
 * reason — hiding it would read as "there is no next episode", which is a
 * different and wrong thing to say (FR-S5). One that genuinely does not exist
 * is now shown disabled too rather than dropped: a control row that changes
 * width between episodes is worse than a dimmed arrow that cannot be pressed.
 */
function NeighbourLink({
  episode,
  label,
  glyph,
}: {
  episode: EpisodeRef | null
  label: 'Previous episode' | 'Next episode'
  glyph: ReactNode
}) {
  if (episode === null) {
    return (
      <button
        type="button"
        disabled
        aria-label={label}
        title={`No ${label.toLowerCase()}`}
        className={PLAYER_ICON}
      >
        {glyph}
      </button>
    )
  }

  const name = `${label} ${String(episode.number)}`
  if (!episode.ready) {
    return (
      <button
        type="button"
        disabled
        aria-label={name}
        title={`Episode ${String(episode.number)} isn’t ready yet`}
        className={PLAYER_ICON}
      >
        {glyph}
      </button>
    )
  }

  return (
    <Link
      to={`/watch/${String(episode.id)}`}
      aria-label={name}
      title={name}
      className={PLAYER_ICON}
    >
      {glyph}
    </Link>
  )
}

/** A strip over the video: something happened that playback does not show. */
function Notice({
  children,
  tone = 'quiet',
  onDismiss,
}: {
  children: string
  tone?: 'quiet' | 'warn'
  onDismiss: () => void
}) {
  return (
    <div
      role="status"
      className={cx(
        'pointer-events-auto flex items-center gap-4 rounded-full border-[0.5px] px-5 py-2.5 text-[13px]',
        'bg-[rgba(18,23,34,0.72)] shadow-bar backdrop-blur-bar',
        tone === 'warn'
          ? 'border-[color-mix(in_srgb,var(--arc-warn)_40%,transparent)] text-[var(--arc-warn)]'
          : 'border-[rgba(255,255,255,0.16)] text-[var(--arc-text-muted)]',
      )}
    >
      <span>{children}</span>
      <button
        type="button"
        onClick={onDismiss}
        className={cx('underline-offset-4 hover:underline', FOCUS_RING)}
      >
        Dismiss
      </button>
    </div>
  )
}

/**
 * The receipt for crossing the completion mark (FR-S4).
 *
 * Quiet by construction: a pill of the same glass the notices wear, in the
 * corner the bar already owns, gone in four seconds. It carries **no button**
 * — nothing is being asked, and an OK on a statement of fact is one more thing
 * to dismiss — but the pill itself takes a click for anyone who wants it gone
 * sooner. It is a `role="status"` live region, so the announcement a sighted
 * viewer gets from the corner of their eye is the one a screen reader gets in
 * words, and neither interrupts the episode.
 */
function MarkedToast({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div
      role="status"
      aria-live="polite"
      title="Dismiss"
      onClick={onDismiss}
      className={cx(PILL, 'cursor-pointer')}
    >
      {MARKED_WATCHED}
    </div>
  )
}

/**
 * The offer to turn the sound on, after the browser refused to start with it
 * (owner, 2026-09-17).
 *
 * The same glass as the receipt beside it, and deliberately as quiet: a muted
 * start is a browser policy, not a fault, and a viewer who wanted silence can
 * leave it alone. It is a button because it does something — unmuting, while
 * the episode keeps playing — which also means the keyboard can reach it; `m`
 * does the same and takes the pill away with it.
 */
function UnmutePill({ onUnmute }: { onUnmute: () => void }) {
  return (
    <button type="button" onClick={onUnmute} title={TAP_TO_UNMUTE} className={cx(PILL, FOCUS_RING)}>
      {TAP_TO_UNMUTE}
    </button>
  )
}

/**
 * What happens next, with the episode nearly over (FR-S5, owner 2026-09-17).
 *
 * A card rather than the black wash it replaces: the video is still playing
 * underneath during the last ninety seconds, and blanking the picture to ask a
 * question about it is the wrong trade. The wash is a gradient off the bottom
 * edge, the card sits above the control bar on the side the toast does not
 * use, and the whole scrim is `pointer-events-none` so a click on the picture
 * still plays and pauses it.
 *
 * Three actions, in the order a viewer wants them. **Next episode** is the
 * primary and appears only when there is one and it is ready, or can be
 * fetched on demand (`next.fetchable`, M20: the button asks the server, then
 * opens the waiting player); when the next
 * episode exists but is still being prepared, or there is no next episode, the
 * line above says which, and no dead button is drawn under a sentence that has
 * already explained itself. At a finished season's last episode, where there
 * is no next row, the primary is **Next season** instead — the sequel's first
 * episode (`next_season`, FR-S5 item 3), on the same ready / fetchable rule,
 * pressed through {@link useNextSeasonPress}. **Keep watching** puts the card away for the rest
 * of this playback. **Back to the show** leaves.
 *
 * Nothing here is focused on mount: the playback shortcuts live on the window
 * and must keep working (FR-S6), and space landing on a focused button instead
 * of the video would be exactly the theft this card is meant to avoid.
 */
function EndOverlay({
  info,
  sequel,
  onKeepWatching,
}: {
  info: PlayInfo
  /** The player's one {@link useNextSeasonPress}, shared with the Next glyph. */
  sequel: NextSeasonPress
  onKeepWatching: () => void
}) {
  const next = info.next
  /** Only where the show itself has no next row (FR-S5 item 3). */
  const season = next === null ? sequel.season : null
  const navigate = useNavigate()
  /**
   * Next on an episode Arc can fetch on demand (FR-S5 item 2, FR-A14, M20):
   * pressing it asks the server first, then opens the player, which waits
   * with "Searching" / "Downloading n %". A refusal is said here, as the
   * server words it, with no spinner.
   */
  const [asking, setAsking] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  const fetchable = next !== null && !next.ready && next.fetchable === true
  const heading =
    next === null
      ? season === null
        ? 'This was the last episode'
        : sequel.offered
          ? `${NEXT_SEASON}: ${nextSeasonName(season)}`
          : nextSeasonUnavailable(season)
      : next.ready || fetchable
        ? `Next: Episode ${String(next.number)}`
        : `Episode ${String(next.number)} isn’t ready yet`

  const fetchNext = () => {
    if (next === null || asking) return
    setAsking(true)
    setRefused(null)
    void demandEpisode(next.id).then(
      () => {
        const state: WatchState = { demanded: true }
        void navigate(`/watch/${String(next.id)}`, { state })
      },
      (error: unknown) => {
        setAsking(false)
        setRefused(demandRefusal(error)?.message ?? DEMAND_FAILED)
      },
    )
  }

  return (
    <div
      className={cx(
        'pointer-events-none absolute inset-0 z-20 flex items-end justify-end p-3 md:p-6',
        'bg-gradient-to-t from-[rgba(0,0,0,0.72)] via-[rgba(0,0,0,0.22)] to-transparent',
      )}
    >
      <div
        role="group"
        aria-label="End of episode"
        className={cx(
          'pointer-events-auto mb-[calc(96px+env(safe-area-inset-bottom))] w-full max-w-[26rem] rounded-card border-[0.5px]',
          'border-[rgba(255,255,255,0.16)] bg-[rgba(18,23,34,0.72)] p-5 shadow-bar backdrop-blur-bar',
        )}
      >
        <p className="text-[17px] font-semibold text-[var(--arc-text)]">{heading}</p>
        {(refused ?? sequel.refused) === null ? null : (
          <p role="alert" className="mt-2 text-[14px] leading-[1.5] text-[var(--arc-warn)]">
            {refused ?? sequel.refused}
          </p>
        )}
        <div className="mt-4 flex flex-wrap items-center gap-2.5">
          {season !== null && sequel.offered && sequel.direct ? (
            <Link
              to={`/watch/${String(season.episode.id)}`}
              className={buttonClass('primary', 'h-11 px-5 text-[15px]')}
            >
              {NEXT_SEASON}
            </Link>
          ) : season !== null && sequel.offered && sequel.refused === null ? (
            <button
              type="button"
              onClick={sequel.press}
              disabled={sequel.asking}
              className={buttonClass('primary', 'h-11 px-5 text-[15px]')}
            >
              {sequel.asking ? 'Starting…' : NEXT_SEASON}
            </button>
          ) : next !== null && next.ready ? (
            <Link
              to={`/watch/${String(next.id)}`}
              className={buttonClass('primary', 'h-11 px-5 text-[15px]')}
            >
              Next episode
            </Link>
          ) : fetchable && refused === null ? (
            <button
              type="button"
              onClick={fetchNext}
              disabled={asking}
              className={buttonClass('primary', 'h-11 px-5 text-[15px]')}
            >
              {asking ? 'Starting…' : 'Next episode'}
            </button>
          ) : null}
          <button
            type="button"
            onClick={onKeepWatching}
            className={buttonClass('chip', 'text-[var(--arc-text)]')}
          >
            Keep watching
          </button>
          <Link
            to={`/anime/${String(info.anime.id)}`}
            className={buttonClass('chip', 'text-[var(--arc-text)]')}
          >
            Back to the show
          </Link>
        </div>
      </div>
    </div>
  )
}

/**
 * The page for one episode. Mounted under a `key` of that episode's id, so
 * moving to the next one starts from scratch — no resume flag, no end overlay,
 * no "Keep watching" still in force, and no reporter left over from the
 * episode just finished.
 */
function PlayerView({ id }: { id: number }) {
  /**
   * Play on demand (FR-A14, M20). Opened from a play button on a not-ready
   * episode (`state.demand`), the page asks the server to fetch it before it
   * loads `/play`, which would otherwise answer 404 for an episode nobody
   * wants yet. Opened on an episode that is already pending, it asks once too:
   * the viewer pressing play is the demand, and a refusal (paused, storage
   * held) is the only way to learn that nothing is coming. A refusal stops the
   * poll and is shown as the server's sentence, with no spinner.
   */
  const location = useLocation()
  const demandFirst = wantsDemand(location.state)
  const demandedRef = useRef(alreadyDemanded(location.state))
  const [demandSettled, setDemandSettled] = useState(!demandFirst)
  const [refusal, setRefusal] = useState<DemandRefusal | null>(null)
  /** A demand the server took (202): a 404 from `/play` right after it is the reconciler racing. */
  const [demandAccepted, setDemandAccepted] = useState(alreadyDemanded(location.state))
  const askToFetch = useCallback(() => {
    if (demandedRef.current) return
    demandedRef.current = true
    void demandEpisode(id).then(
      () => {
        setDemandAccepted(true)
        setDemandSettled(true)
      },
      (failure: unknown) => {
        // No sentence (no network, a 5xx): `/play` says what it can.
        setRefusal(demandRefusal(failure))
        setDemandSettled(true)
      },
    )
  }, [id])
  /**
   * This account's download of the episode, if any (FR-S9). Resolved before
   * the video mounts, so no tap ever waits on it: every `play()` below is
   * called synchronously from the event that asked for it.
   */
  const local = useLocalCopy(id)
  const localRecord = useDownload(id)
  // A copy on this device plays from the file: nothing to fetch first.
  const onDevice = local.checked && local.url !== null
  const { data, isPending, isError, isFetching, error, refetch } = usePlayInfo(id, {
    // Not after a refusal: nothing was queued, so `/play` has nothing to say.
    enabled: (demandSettled && refusal === null) || onDevice,
    pollWhilePending: refusal === null,
    retryNotFound: demandAccepted ? DEMAND_NOT_FOUND_RETRIES : 0,
    // A copy on this device plays from the file: nothing live to follow.
    pollWhileLive: !onDevice,
  })
  useEffect(() => {
    if (demandFirst && local.checked && local.url === null) askToFetch()
  }, [demandFirst, local.checked, local.url, askToFetch])
  // "Next season" at a finished season's end (FR-S5 item 3): one press for
  // the end overlay and the Next glyph, so neither can start a second one.
  const sequel = useNextSeasonPress(data?.next === null ? (data.next_season ?? null) : null)
  // A pending episode with no copy on this device: pressing play is the demand.
  const waitingOnServer =
    data?.pending != null && data.playlist_url === null && local.checked && local.url === null
  useEffect(() => {
    if (waitingOnServer) askToFetch()
  }, [waitingOnServer, askToFetch])
  // A watched copy is never removed while its episode is open here (owner,
  // 2026-10-08): it waits until the player is left.
  useEffect(() => downloads().enterPlayer(id), [id])
  const offline = useOffline()
  const { data: me } = useMe()
  const userId = me?.id ?? null
  const lastLocalSaveRef = useRef(0)

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const barRef = useRef<HTMLDivElement | null>(null)
  const reporterRef = useRef<ProgressReporter | null>(null)
  /** A resume is a one-off: re-seeking on every metadata event would trap the viewer. */
  const resumedRef = useRef(false)
  /**
   * And so is the autoplay attempt: two at most (audible, then muted), once
   * per page, never in a loop. A browser that refuses is not going to change
   * its mind because it was asked a third time, and a page that keeps calling
   * `play()` is a page that fights the viewer who has just pressed pause.
   */
  const autoplayedRef = useRef(false)
  /** The last pointer move that counted; the rest are dropped on the floor. */
  const lastMoveRef = useRef(0)
  const draggingRef = useRef(false)
  /**
   * `chromeShown` as a ref as well as state, because a click on the video has
   * to know whether the chrome was up *before* the pointerdown that preceded
   * it — and by then React has already been told to show it again.
   */
  const chromeShownRef = useRef(true)
  const hiddenAtPressRef = useRef(false)
  /** When the last press began, to pair a finger's pointerdown and touchstart. */
  const lastPressRef = useRef(0)
  /** Touch and mouse want different things from a tap on the video. */
  const pointerTypeRef = useRef<string>('mouse')
  /** Whether the last thing the viewer did was press a key rather than aim. */
  const keyboardRef = useRef(false)
  /** The pending single click, waiting to find out if it is half of a double. */
  const clickTimerRef = useRef<number | null>(null)
  /** `chromeHeld` (below) for the tap handler, which is declared before it. */
  const chromeHeldRef = useRef(false)

  const [resumedAt, setResumedAt] = useState<number | null>(null)
  /**
   * The three pieces the end of an episode is made of (owner, 2026-09-17).
   *
   * `markedToast` is the completion receipt and nothing more — it is raised by
   * the one report the server calls `newly_completed`, which is exactly once
   * per (user, episode), so a rewatch never raises it and the client needs no
   * rule of its own for that. `ended` is the media's own end. `endDismissed`
   * is "Keep watching", which holds for the rest of *this* playback: the page
   * is keyed on the episode id, so the next episode starts with it cleared,
   * and nothing in between — a tick, the media ending — brings the card back
   * to someone who has just said they did not want it.
   */
  const [markedToast, setMarkedToast] = useState(false)
  const [ended, setEnded] = useState(false)
  const [endDismissed, setEndDismissed] = useState(false)
  /**
   * Whether playback started muted because the browser would not allow sound
   * (owner, 2026-09-17). It is the only reason the unmute pill is ever up —
   * a viewer who muted the episode themselves is not offered their own
   * decision back — and it is cleared by anything that unmutes, the pill and
   * the `m` shortcut alike, because the media element is the truth about mute
   * and this flag is only an offer about it.
   */
  const [autoplayMuted, setAutoplayMuted] = useState(false)
  /**
   * Consecutive failed progress writes, and whether the viewer has waved the
   * warning away. Both live here rather than in `ProgressReporter`: the
   * reporter is framework-free and knows only about time and media events,
   * while "how many in a row have failed" is a fact about the mutation, which
   * is React's. Counted with an updater so `report` below never has to depend
   * on the count — a new `report` identity would rebuild the reporter and send
   * a spurious final beacon.
   */
  const [progressFailures, setProgressFailures] = useState(0)
  const [progressWarningDismissed, setProgressWarningDismissed] = useState(false)

  /**
   * What the controls draw. All of it is read back off the media element —
   * the element stays the source of truth, and a keyboard shortcut moves the
   * chips exactly as a click does.
   */
  const [playing, setPlaying] = useState(false)
  const [position, setPosition] = useState(0)
  const [mediaDuration, setMediaDuration] = useState(0)
  const [buffered, setBuffered] = useState(0)
  const [fullscreen, setFullscreen] = useState(false)
  /** A drag in progress; the chrome may not be taken away mid-scrub. */
  const [scrubbing, setScrubbing] = useState(false)
  const [chromeShown, setChromeShown] = useState(true)
  /** Bumped by any sign of life; the hide timer restarts on every change. */
  const [activity, setActivity] = useState(0)
  /** `null` until the viewer moves the mark themselves; then it wins. */
  const [watchedOverride, setWatchedOverride] = useState<boolean | null>(null)
  /** The offline control's menu is open: the chrome stays while it is. */
  const [offlineMenuOpen, setOfflineMenuOpen] = useState(false)

  /**
   * A stream still being cut from a download (M20): its media duration grows
   * with the download, so the server's is the only one used (FR-S4, C8) — the
   * episode's expected length, for as long as `live` is in the answer, ended
   * or not. Every duration below goes through `measure`.
   */
  const liveInfo = data?.live ?? null
  const live = liveInfo !== null
  /**
   * The expected duration of a `live` answer this page has seen. It outlives
   * `live` itself: adopted between two polls, hls.js can still hold the
   * partial playlist and its short duration, so the page keeps measuring by
   * the expected one until the element's is at least 90 % of it (FR-S4,
   * `measureDuration`). Set during render, the documented way to keep a
   * value from an earlier render.
   */
  const [seenExpected, setSeenExpected] = useState<number | null>(null)
  if (
    liveInfo !== null &&
    liveInfo.expected_duration > 0 &&
    seenExpected !== liveInfo.expected_duration
  ) {
    setSeenExpected(liveInfo.expected_duration)
  }
  /**
   * The live answer as the stream sees it: none for a copy on this device,
   * which plays a whole file and has no front, no pending seeks and no poll.
   */
  const liveStream = liveInfo !== null && local.url === null ? liveInfo : null
  /**
   * The download is still going (FR-P8, M20 T7): the front bar, pending
   * seeks, the clamps and the 5 s poll. Off once the playlist has its
   * ENDLIST, off when the episode was adopted (`live` gone), and off for a
   * device copy.
   */
  const liveActive = liveStream !== null && !liveStream.ended
  const serverDuration =
    liveInfo !== null && liveInfo.expected_duration > 0
      ? liveInfo.expected_duration
      : (data?.duration ?? 0)
  const liveFrontSeconds = liveStream?.front_seconds ?? 0
  /** Every duration the page uses, by FR-S4's rule (see `measureDuration`). */
  const measure = useCallback(
    (media: number, fallback: number) => measureDuration(media, fallback, { live, seenExpected }),
    [live, seenExpected],
  )
  /**
   * The front as the element last reported it — the end of its seekable
   * range — or `null` while it has none, when the bar draws the server's
   * `front_seconds` instead.
   *
   * Seeks are planned and settled against the **element's** front and the
   * playhead, never the server's number: what the server has written is not
   * what the player can reach. Safari holds back three target durations from
   * the end of a live playlist, and undoes a seek past its seekable range —
   * yet plays past that range, so where the playhead is (or was) is
   * reachable too. hls.js's range is the loaded playlist, which lags the
   * server by up to a reload. An element with no range reaches only what the
   * playhead has passed. The arrow keys and End (which do not wait) are held
   * to the larger of the element's front and the server's.
   */
  const [front, setFront] = useState<number | null>(null)
  /**
   * A seek past the front, waiting for the download (FR-P8): the target, and
   * whether it was the resume (which gets its "Resumed from" receipt when the
   * jump happens). The ref is what the timers read; the state is what draws.
   */
  const pendingRef = useRef<{ at: number; resume: boolean } | null>(null)
  const [pendingSeek, setPendingSeek] = useState<number | null>(null)
  const setPending = useCallback((next: { at: number; resume: boolean } | null) => {
    pendingRef.current = next
    setPendingSeek(next?.at ?? null)
  }, [])
  const [fewSeedersDismissed, setFewSeedersDismissed] = useState(false)
  /** The element's seekable end now (0 with none). */
  const frontNow = useCallback(() => readFront(videoRef.current?.seekable, 0), [])
  /** What a key seek is held to: the element's front or the server's, the larger. */
  const keyFront = useCallback(
    () => Math.max(readFront(videoRef.current?.seekable, 0), liveFrontSeconds),
    [liveFrontSeconds],
  )
  /**
   * The position the reporter and the device are told: while a resume waits
   * on the download (FR-S2), its target — the playhead parked at the front is
   * not where the viewer is, and reporting it would overwrite their place.
   */
  const reportedPosition = useCallback((position: number) => {
    const pending = pendingRef.current
    return pending !== null && pending.resume ? pending.at : position
  }, [])
  /**
   * The subtitle switch (FR-S10): on by default, the viewer's last choice on
   * this device once they have made one. Per account, like the position.
   */
  const subtitlesOwner = userId ?? downloads().ownerId
  const [subtitlesChoice, setSubtitlesChoice] = useState<boolean | null>(null)
  // Read from storage once per account, not on every render (a timeupdate
  // re-renders the page several times a second).
  const subtitlesStored = useMemo(() => subtitlesRemembered(subtitlesOwner), [subtitlesOwner])
  const subtitlesOn = subtitlesChoice ?? subtitlesStored
  const toggleSubtitles = useCallback(() => {
    const on = !subtitlesOn
    setSubtitlesChoice(on)
    rememberSubtitles(subtitlesOwner, on)
  }, [subtitlesOn, subtitlesOwner])
  const total = mediaDuration > 0 ? mediaDuration : serverDuration
  const remaining = total > 0 ? Math.max(0, total - position) : 0

  /**
   * Whether the end-of-episode card is up. Derived rather than stored, so the
   * only thing that has to be remembered is the viewer's "no" — it comes up on
   * its own with 1:30 left and again if the media ends, and a seek backwards
   * out of the window takes it away again by the same rule that brought it.
   */
  const endOverlayShown =
    !endDismissed && (ended || (total > END_OVERLAY_SECONDS && remaining <= END_OVERLAY_SECONDS))

  const { mutate: reportProgress } = useReportProgress()
  const markWatched = useMarkWatched()
  const unmarkWatched = useUnmarkWatched()

  /** "Keep watching", from the card's own button and from Escape. */
  const keepWatching = useCallback(() => {
    setEndDismissed(true)
  }, [])

  /**
   * Start the episode, without waiting to be asked (FR-S1, owner 2026-09-17).
   *
   * Opening an episode is the decision; a play button the viewer then has to
   * find is a second one nothing was waiting for. The browser disagrees in
   * two different ways and this handles each of them exactly once:
   *
   * 1. `play()` rejects because there has been **no user gesture on this
   *    page** — following a link from Home is a gesture on the *other* page —
   *    or because audible autoplay is not allowed here. Neither is an error
   *    and neither deserves a toast.
   * 2. So it asks again **muted**, which every autoplay policy permits, and
   *    puts up the quiet "Tap to unmute" pill: the episode is running, the
   *    viewer has one press to get the sound, and nothing was lost.
   * 3. If even that is refused it stops. The bar's play button is already on
   *    screen and is now the answer; the mute is undone so that the first
   *    press has sound, and no error is shown, because nothing went wrong.
   *
   * Two attempts, never a loop: a policy is not going to change its mind
   * because it was asked a third time.
   */
  const beginPlayback = useCallback(async (video: HTMLVideoElement) => {
    try {
      await video.play()
      return
    } catch {
      // Refused with sound. Fall through to the muted attempt.
    }

    video.muted = true
    try {
      await video.play()
      setAutoplayMuted(true)
    } catch {
      video.muted = false
    }
  }, [])

  /**
   * The last position on this device, per (user, episode), for every source —
   * what an offline resume uses, and what an online one prefers when it is
   * newer than the server's (FR-S2, FR-S9). Fire and forget. Until `/me` has
   * answered, the account whose downloads are loaded is the one signed in.
   */
  const keepPosition = useCallback(
    (nextPosition: number, duration: number) => {
      const owner = userId ?? downloads().ownerId
      if (owner === null) return
      lastLocalSaveRef.current = Date.now()
      void rememberPosition(owner, id, nextPosition, duration)
    },
    [id, userId],
  )

  /** The pill's press, and the one place the flag is cleared by hand. */
  const unmute = useCallback(() => {
    const video = videoRef.current
    if (video !== null) video.muted = false
    setAutoplayMuted(false)
  }, [])

  /** The one place `chromeShown` moves, so the ref never drifts from the state. */
  const setChrome = useCallback((next: boolean) => {
    chromeShownRef.current = next
    setChromeShown(next)
  }, [])

  const report = useCallback(
    (nextPosition: number, duration: number) => {
      reportProgress(
        { episode_id: id, position_s: nextPosition, duration_s: duration },
        {
          onSuccess: (result) => {
            // A write that landed says the connection is back, so the run of
            // failures ends and the warning is armed again for the next one.
            setProgressFailures(0)
            setProgressWarningDismissed(false)
            // The completion mark is a receipt, not a decision: a toast, and
            // no overlay (owner, 2026-09-17).
            if (result.newly_completed) setMarkedToast(true)
          },
          onError: () => {
            setProgressFailures((count) => count + 1)
          },
        },
      )
    },
    [id, reportProgress],
  )

  // One reporter per episode; tearing it down is what sends the final position
  // for the episode being left (FR-S3).
  useEffect(() => {
    const reporter = new ProgressReporter({ episodeId: id, report })
    reporterRef.current = reporter
    return () => {
      reporterRef.current = null
      reporter.destroy()
    }
  }, [id, report])

  const toggleFullscreen = useCallback(() => {
    const container = containerRef.current
    if (container === null) return
    if (document.fullscreenElement != null) {
      void document.exitFullscreen?.()
      return
    }
    void container.requestFullscreen?.()
  }, [])

  // The browser owns the fullscreen state — Escape and F11 move it without
  // asking — so the button's label is read off the document, never assumed.
  useEffect(() => {
    function sync() {
      setFullscreen(document.fullscreenElement != null)
    }

    sync()
    document.addEventListener('fullscreenchange', sync)
    return () => {
      document.removeEventListener('fullscreenchange', sync)
    }
  }, [])

  const togglePlay = useCallback(() => {
    const video = videoRef.current
    if (video === null) return
    if (video.paused) playQuietly(video)
    else video.pause()
  }, [])

  /**
   * A click on the video itself.
   *
   * With a mouse (or a pen), a single click plays or pauses and a double one
   * goes fullscreen, which are the two gestures every video on the web has —
   * but a double click is two clicks, so the single has to wait a quarter of a
   * second to find out whether it was the first half of one. Without that
   * wait, double-clicking to go fullscreen would also pause the episode,
   * which is exactly the moment a viewer least wants it to stop.
   *
   * On touch a single tap only toggles the controls and never plays or
   * pauses (owner, 2026-10-18, after the real-iPad check): a tap on a bare
   * picture means "show me the controls", and a tap with them up means "take
   * them away", not "stop". Play and pause are the centre button in the bar.
   * The press that started the tap has already shown the chrome (the
   * activity effect below), so what matters is the state *at press time*:
   * hidden then, the tap is done; shown then, it hides — after the same
   * double-tap wait, so the first half of a double-tap to fullscreen does not
   * blink the bar away, and only when nothing is holding the chrome open
   * (`chromeHeld`). A double-tap still toggles fullscreen.
   */
  const onVideoClick = useCallback(() => {
    if (clickTimerRef.current !== null) {
      window.clearTimeout(clickTimerRef.current)
      clickTimerRef.current = null
      toggleFullscreen()
      return
    }

    const touch = pointerTypeRef.current === 'touch'
    const hiddenAtPress = hiddenAtPressRef.current
    clickTimerRef.current = window.setTimeout(() => {
      clickTimerRef.current = null
      if (!touch) {
        togglePlay()
        return
      }
      if (hiddenAtPress || chromeHeldRef.current) return
      setChrome(false)
    }, DOUBLE_CLICK_MS)
  }, [toggleFullscreen, togglePlay, setChrome])

  useEffect(
    () => () => {
      if (clickTimerRef.current !== null) window.clearTimeout(clickTimerRef.current)
    },
    [],
  )

  /**
   * The one way the page moves the playhead; everything else calls this.
   *
   * While the download is going (FR-P8), a target past `front − 1 s` is not
   * asked of the element: the playhead goes to that edge now and the target
   * is kept as a pending seek, which the front check below carries out once
   * the front is six seconds past it. `fromResume` marks the resume (FR-S2).
   */
  const seekTo = useCallback(
    (seconds: number, fromResume = false) => {
      const video = videoRef.current
      if (video === null) return
      const limit = measure(video.duration, serverDuration)
      const clamped = Math.max(0, limit > 0 ? Math.min(seconds, limit) : seconds)
      if (liveActive) {
        const plan = planSeek(clamped, frontNow(), video.currentTime)
        if (plan.pending === null) {
          setPending(null)
          video.currentTime = plan.now
          setPosition(plan.now)
          return
        }
        setPending({ at: plan.pending, resume: fromResume })
        // A playhead already in the last second is left there, not pulled back.
        if (video.currentTime < plan.now) video.currentTime = plan.now
        setPosition(video.currentTime)
        return
      }
      setPending(null)
      video.currentTime = clamped
      setPosition(clamped)
    },
    [serverDuration, measure, liveActive, frontNow, setPending],
  )

  const skip = useCallback(
    (seconds: number) => {
      const video = videoRef.current
      if (video === null) return
      seekTo(video.currentTime + seconds)
    },
    [seekTo],
  )

  // Shortcuts belong to the page, not to the video: the element only has focus
  // if the viewer clicked it, and space should still pause after clicking a
  // button in the header (FR-S6).
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const video = videoRef.current
      if (video === null || event.defaultPrevented || isEditableTarget(event.target)) return
      if (event.metaKey || event.ctrlKey || event.altKey) return

      switch (event.key) {
        case ' ':
        case 'Spacebar':
          event.preventDefault()
          if (video.paused) playQuietly(video)
          else video.pause()
          return
        // A key seek replaces a pending one, and while the download is going
        // it is held to the front rather than made to wait (FR-S6).
        case 'ArrowLeft':
          event.preventDefault()
          setPending(null)
          video.currentTime = Math.max(0, video.currentTime - SEEK_STEP)
          setPosition(video.currentTime)
          return
        case 'ArrowRight': {
          event.preventDefault()
          setPending(null)
          const target = video.currentTime + SEEK_STEP
          video.currentTime = liveActive
            ? clampToFront(target, keyFront(), video.currentTime)
            : target
          setPosition(video.currentTime)
          return
        }
        case 'Home':
          event.preventDefault()
          setPending(null)
          video.currentTime = 0
          setPosition(0)
          return
        case 'End': {
          event.preventDefault()
          const limit = measure(video.duration, serverDuration)
          if (limit <= 0) return
          setPending(null)
          const target = liveActive ? clampToFront(limit, keyFront(), video.currentTime) : limit
          video.currentTime = target
          setPosition(target)
          return
        }
        case 'f':
        case 'F':
          event.preventDefault()
          toggleFullscreen()
          return
        case 'm':
        case 'M':
          // The mute chip is gone, but the shortcut is not: it is the one
          // thing a viewer reaches for without looking.
          event.preventDefault()
          video.muted = !video.muted
          return
        case 'Escape':
          // Escape is the card's Keep watching — but only while the card is
          // up, or a viewer leaving fullscreen at minute three would silently
          // spend a dismissal they never made. It is deliberately *not*
          // prevented: in fullscreen the browser reads the same key as "get
          // out", and both of those are wanted at once.
          if (endOverlayShown) keepWatching()
          return
        default:
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [
    endOverlayShown,
    keepWatching,
    serverDuration,
    measure,
    liveActive,
    keyFront,
    setPending,
    toggleFullscreen,
  ])

  /**
   * The download front, re-read from the element every second while the
   * download is going, and the pending seek settled against it (FR-P8, M20
   * T7): the jump once the front is six seconds past the target; once the
   * playlist has ended or the episode was adopted (`live` gone), as soon as
   * the element's range is within six seconds of the target, or has caught
   * up with the real end (a target past it resolves to the end); nothing at
   * all when playback got there by itself. Adoption is no shortcut: hls.js
   * may not have reloaded the playlist yet, so the element's range is still
   * what decides. Every state change is in a timer callback, never in the
   * effect's own body.
   */
  const adoptedDuration = liveStream === null ? (data?.duration ?? null) : null
  useEffect(() => {
    if (liveStream === null && pendingRef.current === null) return

    const tick = () => {
      const video = videoRef.current
      if (video === null) return
      const reach = readFront(video.seekable, 0)
      if (liveStream !== null) setFront(reach > 0 ? reach : null)
      const pending = pendingRef.current
      if (pending === null) return
      const ended = liveStream === null || liveStream.ended
      const verdict: PendingVerdict = settlePending(pending.at, {
        front: reach,
        position: video.currentTime,
        ended,
        finalFront: liveStream === null ? adoptedDuration : ended ? liveStream.front_seconds : null,
      })
      if (verdict.action === 'wait') return
      setPending(null)
      if (verdict.action === 'drop') return
      video.currentTime = verdict.at
      setPosition(verdict.at)
      if (pending.resume) setResumedAt(verdict.at)
    }

    const first = window.setTimeout(tick, 0)
    const every =
      (liveStream !== null && !liveStream.ended) || pendingRef.current !== null
        ? window.setInterval(tick, FRONT_CHECK_MS)
        : null
    return () => {
      window.clearTimeout(first)
      if (every !== null) window.clearInterval(every)
    }
  }, [liveStream, adoptedDuration, setPending])

  // Any sign of a person brings the chrome back. Moves are throttled, because
  // a pointer move fires dozens of times a second and none of them need a
  // render; a press or a key is rare enough to be taken at face value.
  useEffect(() => {
    function show() {
      setChrome(true)
      setActivity((count) => count + 1)
    }

    function onMove() {
      const now = Date.now()
      if (now - lastMoveRef.current < ACTIVITY_THROTTLE_MS) return
      lastMoveRef.current = now
      keyboardRef.current = false
      show()
    }

    // A finger fires both a touch `pointerdown` and a `touchstart` for the
    // same press, in an order browsers do not agree on. Whichever comes
    // second must not read the chrome state again: the first one has
    // already shown the chrome, so it would always see it up.
    function press(pointerType: string) {
      const now = Date.now()
      const sameTouch =
        pointerType === 'touch' &&
        pointerTypeRef.current === 'touch' &&
        now - lastPressRef.current < SAME_PRESS_MS
      lastPressRef.current = now
      pointerTypeRef.current = pointerType
      keyboardRef.current = false
      if (!sameTouch) hiddenAtPressRef.current = !chromeShownRef.current
      show()
    }

    function onPointerDown(event: PointerEvent) {
      press(event.pointerType === '' ? 'mouse' : event.pointerType)
    }

    function onTouchStart() {
      press('touch')
    }

    function onKeyDown() {
      keyboardRef.current = true
      show()
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerdown', onPointerDown)
    window.addEventListener('touchstart', onTouchStart)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('touchstart', onTouchStart)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [setChrome])

  /**
   * Whether something on screen is holding the chrome open against the timer.
   *
   * Each of the three is a case where taking the bar away would either break a
   * gesture in progress or hide the answer to a question the page has just
   * asked. A drag would have the track pulled out from under the finger
   * dragging it. The end-of-episode card and the unsaved-progress strip are
   * both the page saying something playback cannot show, and both offer a
   * control — Next episode, Dismiss — that has to stay reachable while they
   * are up.
   */
  const progressWarning = progressFailures >= PROGRESS_FAILURE_LIMIT && !progressWarningDismissed
  const chromeHeld = scrubbing || endOverlayShown || progressWarning || offlineMenuOpen
  useEffect(() => {
    chromeHeldRef.current = chromeHeld
  }, [chromeHeld])

  /**
   * The chrome hides itself after a second and a half of an idle pointer, and
   * takes the mouse cursor with it. Not two, and not three: the owner's note
   * is that the bar is in the way of the thing it is there to serve — the
   * picture, and the subtitles burned into the bottom of it.
   *
   * An idle pointer, not idle *playback*: the timer runs whether the video is
   * playing or paused (owner, 2026-09-12). A pause is not a request to keep
   * the bar — someone who pauses on a frame to read a sign in the background
   * wants the bar gone as much as anyone, and the picture is the same picture
   * either way. Anything that looks like a person — a move, a touch, a key,
   * the pause event itself — brings it straight back, so the cost of being
   * wrong is one wave of the mouse.
   *
   * What still holds it open is `chromeHeld` above, plus one case that cannot
   * be state: a keyboard viewer standing inside the bar. They cannot press a
   * button that is not there and have no pointer to wave, so focus inside the
   * bar keeps it — but only when the last input really was a key. Resting a
   * *pointer* over the bar is deliberately not enough: a pointer that has
   * stopped moving has stopped asking for anything.
   *
   * Only the *hiding* lives here. Bringing the bars back is the job of
   * whatever brought them back — a pointer move, a key, the pause event —
   * because a state change an effect makes on its own is a second render
   * nobody asked for.
   */
  useEffect(() => {
    if (chromeHeld) return

    const timer = window.setTimeout(() => {
      if (keyboardRef.current && barRef.current?.contains(document.activeElement) === true) return
      setChrome(false)
    }, CHROME_HIDE_MS)
    return () => {
      window.clearTimeout(timer)
    }
  }, [chromeHeld, playing, activity, setChrome])

  /**
   * The resume notice takes itself off after five seconds (FR-S2).
   *
   * Keyed on `resumedAt` rather than run once: a Dismiss sets it to `null`,
   * which cancels the pending timeout instead of leaving one armed to clear a
   * value that is already gone, and a fresh resume would start its own five
   * seconds. Nothing else in the page reads the timer, so it needs no ref.
   */
  useEffect(() => {
    if (resumedAt === null) return

    const timer = window.setTimeout(() => {
      setResumedAt(null)
    }, RESUME_NOTICE_MS)
    return () => {
      window.clearTimeout(timer)
    }
  }, [resumedAt])

  /**
   * And so does the completion receipt, after four (owner, 2026-09-17). Same
   * shape as the resume notice above: keyed on the flag, so a click that puts
   * it away early cancels the pending timeout rather than leaving one armed.
   */
  useEffect(() => {
    if (!markedToast) return

    const timer = window.setTimeout(() => {
      setMarkedToast(false)
    }, MARKED_TOAST_MS)
    return () => {
      window.clearTimeout(timer)
    }
  }, [markedToast])

  // Play on demand refused (FR-A14): said, not spun — unless there is
  // something to play anyway (a copy here, or a stream that is ready).
  if (refusal !== null && local.checked && local.url === null && !data?.playlist_url) {
    return <DemandRefused refusal={refusal} animeId={data?.anime.id ?? null} />
  }

  if (isPending || !local.checked) {
    return (
      <div className="fixed inset-0 flex items-center justify-center bg-black">
        <p role="status" className="text-[14px] text-[var(--arc-text-muted)]">
          Loading…
        </p>
      </div>
    )
  }

  // A failed live poll (FR-P8) leaves the video playing on what it has: only
  // an answer with nothing to play turns into one of the pages below.
  if (isError && !data?.playlist_url) {
    // 404 is the server saying "no rendition"; anything else is Arc failing,
    // and telling the viewer their episode is not ready would be a statement
    // about their library that this client has no basis for. Only the second
    // is worth a retry: a missing rendition is not made by asking again.
    if (isStatus(error, 404)) return <NotReady message={NOT_READY_BODY} />

    if (offline) {
      return (
        <div className="fixed inset-0 mx-auto flex max-w-lg flex-col items-center justify-center p-6 text-center">
          <h1 className="text-[28px] font-semibold tracking-[-0.022em] text-[var(--arc-text)]">
            {OFFLINE_TITLE}
          </h1>
          <p className="mt-3 text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
            {OFFLINE_BODY}
          </p>
          <Link to="/downloads" className={buttonClass('primary', 'mt-7')}>
            Go to Downloads
          </Link>
        </div>
      )
    }

    return (
      <div className="fixed inset-0 mx-auto flex max-w-lg flex-col items-center justify-center p-6 text-center">
        <h1 className="text-[28px] font-semibold tracking-[-0.022em] text-[var(--arc-text)]">
          {LOAD_FAILED_TITLE}
        </h1>
        <ErrorState
          className="mt-4 w-full"
          message={LOAD_FAILED_BODY}
          pending={isFetching}
          onRetry={() => {
            void refetch()
          }}
        />
        <Link to="/" className={buttonClass('secondary', 'mt-7')}>
          Back to home
        </Link>
      </div>
    )
  }

  if (data === undefined) return null
  const info: PlayInfo = data
  const source = chooseSource(info, local.url)
  if (source.kind === 'none') {
    // Wanted and on its way (FR-A14, M20): wait here, polling, until it streams.
    if (info.pending != null && info.playlist_url === null) {
      return <PendingView info={info} pending={info.pending} />
    }
    // A trip-only episode with no copy here (or one still on its way): say
    // where it comes from. A copy that is here but would not open is the
    // unreadable case below, whichever kind of episode it is.
    if (info.offline_only === true && localRecord?.reason !== 'unreadable') {
      return (
        <div className="fixed inset-0 mx-auto flex max-w-lg flex-col items-center justify-center p-6 text-center">
          <h1 className="text-[28px] font-semibold tracking-[-0.022em] text-[var(--arc-text)]">
            {OFFLINE_ONLY_TITLE}
          </h1>
          <p className="mt-3 text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
            {OFFLINE_ONLY_BODY}
          </p>
          <div className="mt-7 flex flex-wrap justify-center gap-2.5">
            <Link to="/downloads" className={buttonClass('primary')}>
              Go to Downloads
            </Link>
            <Link to={`/anime/${String(info.anime.id)}`} className={buttonClass('secondary')}>
              Back to the show
            </Link>
          </div>
        </div>
      )
    }
    return (
      <div className="fixed inset-0 mx-auto flex max-w-lg flex-col items-center justify-center p-6 text-center">
        <h1 className="text-[28px] font-semibold tracking-[-0.022em] text-[var(--arc-text)]">
          {LOAD_FAILED_TITLE}
        </h1>
        <p className="mt-3 text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
          {FILE_UNREADABLE}
        </p>
        <Link to="/downloads" className={buttonClass('secondary', 'mt-7')}>
          Go to Downloads
        </Link>
      </div>
    )
  }
  const { anime, episode, previous, next } = info
  const episodeLabel = `Episode ${String(episode.number)}`
  /** The demo account never downloads; `OfflineButton` rules out the rest. */
  const canKeepOffline = me?.is_demo === false

  const watched = watchedOverride ?? episode.watched
  /**
   * Whether this tick has an undo (FR-W5). `watched_source` folds that in: an
   * episode *under* the viewer's latest watched one is `progress`, and the
   * un-mark only ever moves the list by one from the top (FR-S4, revised
   * 2026-09-13), so there is nothing here to press. The toggle becomes the same
   * non-actionable control the show page's rows use, tooltip and all, with the
   * glyph still filled because the viewer has still watched it — and still a
   * **tick**, because the cross names an action and this state has none.
   *
   * `watchedOverride` settles the question on its own: it is only ever set by
   * pressing this control, which a `progress` episode never offers.
   */
  const watchedByList =
    watchedOverride === null && episode.watched && episode.watched_source === 'progress'
  const watchedPending = markWatched.isPending || unmarkWatched.isPending
  const watchedError = unmarkWatched.isError
    ? UNMARK_WATCHED_FAILED
    : markWatched.isError
      ? MARK_WATCHED_FAILED
      : null

  /** "The Sword Saint · subs en, audio ja" — what this file actually is. */
  const episodeTitle = episode.title ?? null
  const rendition = episode.rendition
  const languages =
    rendition === null
      ? ''
      : [
          rendition.subtitle_lang === null ? null : `subs ${rendition.subtitle_lang}`,
          rendition.audio_lang === null ? null : `audio ${rendition.audio_lang}`,
        ]
          .filter((part): part is string => part !== null)
          .join(', ')

  const played = total > 0 ? Math.min(1, Math.max(0, position / total)) : 0

  /**
   * The CC button exists only for a stream whose rendition has a text track
   * (FR-S10). A device copy has its subtitles burned into the picture (§5.3b),
   * so there is nothing to switch there.
   */
  const hasSubtitles = (info.subtitles ?? null) !== null && source.kind !== 'file'
  /**
   * Where hls.js starts loading: the resume, by the same rule as the seek —
   * held to the front while the download is going (a resume past it waits as
   * a pending seek, FR-S2).
   */
  const resumeStart =
    info.resume_position !== null && shouldResume(info.resume_position, serverDuration)
      ? info.resume_position
      : 0
  const startPosition =
    liveStream !== null && !liveStream.ended
      ? planSeek(resumeStart, liveStream.front_seconds).now
      : resumeStart

  /**
   * The front bar (FR-P8): how much of the episode is downloaded, 0–1. The
   * larger of the server's number and the element's range, so it never moves
   * backwards when Safari's range (which trails its playlist) first appears.
   */
  const shownFront = Math.max(front ?? 0, liveFrontSeconds)
  const frontFraction =
    liveActive && total > 0 ? Math.min(1, Math.max(0, shownFront / total)) : null
  /**
   * Waiting on the download: a pending seek, or a playhead that has caught up
   * with the front. Either way the viewer is told why nothing moves.
   */
  const waitingOnDownload =
    liveStream !== null &&
    liveActive &&
    (pendingSeek !== null || position >= playableEdge(shownFront) - 1)
  const showFewSeeders =
    liveStream !== null && liveActive && fewSeeders(liveStream.seeders) && !fewSeedersDismissed

  function onReady(duration: number) {
    const video = videoRef.current
    if (video === null) return

    setMediaDuration(measure(duration, 0))

    // A second `loadedmetadata` means the stream was rebuilt — a Retry after a
    // fatal error. The fresh media starts at zero, which would throw away the
    // half hour the viewer had got through, so it goes back to the last
    // position the reporter saw. No toast: nothing was resumed from a stored
    // position, the page merely refused to lose its place.
    if (resumedRef.current) {
      const last = reporterRef.current?.lastPosition ?? null
      if (last !== null && last > 0) {
        video.currentTime = last
        setPosition(last)
      }
      return
    }
    resumedRef.current = true

    const start = info.resume_position
    if (shouldResume(start, measure(duration, serverDuration)) && start !== null) {
      if (liveActive) {
        // A resume past the front waits (FR-S2); its receipt comes with the jump.
        seekTo(start, true)
        if (pendingRef.current === null) setResumedAt(start)
      } else {
        video.currentTime = start
        setPosition(start)
        setResumedAt(start)
      }
    }

    // And then play, from wherever that left the playhead: autoplay comes
    // *after* the resume seek so the episode never starts at zero and jumps.
    if (!autoplayedRef.current) {
      autoplayedRef.current = true
      void beginPlayback(video)
    }
  }

  /** Where along the track a pointer landed, 0–1. */
  function fractionOf(event: ReactPointerEvent<HTMLDivElement>): number | null {
    const rect = event.currentTarget.getBoundingClientRect()
    if (rect.width <= 0) return null
    return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  }

  function scrub(event: ReactPointerEvent<HTMLDivElement>): void {
    const fraction = fractionOf(event)
    if (fraction === null || total <= 0) return
    seekTo(fraction * total)
  }

  function toggleWatched(): void {
    const input = { episodeId: id, animeId: anime.id }
    if (watched) {
      unmarkWatched.mutate(input, {
        onSuccess: () => {
          setWatchedOverride(false)
        },
      })
      return
    }
    markWatched.mutate(input, {
      onSuccess: () => {
        setWatchedOverride(true)
      },
    })
  }

  return (
    <div ref={containerRef} className="fixed inset-0 overflow-hidden bg-black">
      <HlsVideo
        src={source.url}
        kind={source.kind === 'file' ? 'file' : 'hls'}
        // The file would not open: forget it and stream, when there is a
        // stream to fall back to (`chooseSource` says so otherwise).
        onSourceError={local.fail}
        videoRef={videoRef}
        label={`${anime.title.preferred} — ${episodeLabel}`}
        // The cursor goes wherever the chrome goes: a pointer arrow parked
        // over a full-bleed picture is the same kind of clutter as the bar.
        className={chromeShown ? '' : 'cursor-none'}
        onVideoClick={onVideoClick}
        startPosition={startPosition}
        live={liveActive}
        subtitles={hasSubtitles ? subtitlesOn : undefined}
        onReady={onReady}
        onTimeUpdate={(nextPosition, duration) => {
          reporterRef.current?.update(
            reportedPosition(nextPosition),
            measure(duration, serverDuration),
          )
          if (Date.now() - lastLocalSaveRef.current >= LOCAL_POSITION_EVERY_MS) {
            keepPosition(reportedPosition(nextPosition), measure(duration, serverDuration))
          }
          setPosition(nextPosition)
          setMediaDuration(measure(duration, 0))
          setBuffered(bufferedFraction(videoRef.current, measure(duration, serverDuration)))
          if (liveActive) {
            const reachable = readFront(videoRef.current?.seekable, 0)
            setFront(reachable > 0 ? reachable : null)
          }
        }}
        onPlay={(nextPosition, duration) => {
          reporterRef.current?.play(
            reportedPosition(nextPosition),
            measure(duration, serverDuration),
          )
          setPlaying(true)
        }}
        onPause={(nextPosition, duration) => {
          reporterRef.current?.pause(
            reportedPosition(nextPosition),
            measure(duration, serverDuration),
          )
          keepPosition(reportedPosition(nextPosition), measure(duration, serverDuration))
          setPlaying(false)
          // A pause is a sign of a person, so the bar comes back — but it no
          // longer *stays*: the idle timer runs while paused too, and takes it
          // away again if nothing else happens (owner, 2026-09-12).
          setChrome(true)
        }}
        onVolumeChange={(muted) => {
          // The pill is an offer to undo a *muted start*, so anything that
          // takes the sound back — the pill itself, or `m` — retires it. The
          // element is the truth about mute; this only follows it.
          if (!muted) setAutoplayMuted(false)
        }}
        onSeeked={(nextPosition, duration) => {
          reporterRef.current?.seek(
            reportedPosition(nextPosition),
            measure(duration, serverDuration),
          )
          keepPosition(reportedPosition(nextPosition), measure(duration, serverDuration))
          setPosition(nextPosition)
        }}
        onEnded={(nextPosition, duration) => {
          reporterRef.current?.end(
            reportedPosition(nextPosition),
            measure(duration, serverDuration),
          )
          keepPosition(reportedPosition(nextPosition), measure(duration, serverDuration))
          setPlaying(false)
          setChrome(true)
          setEnded(true)
        }}
      />

      {/*
        The two strips that are not chrome: they say something playback itself
        cannot, so they stay on screen while the controls fade away.
      */}
      <div className="pointer-events-none absolute inset-x-0 top-[96px] z-10 flex flex-col items-center gap-2.5 px-7">
        {progressWarning ? (
          <Notice
            tone="warn"
            onDismiss={() => {
              setProgressWarningDismissed(true)
            }}
          >
            {PROGRESS_UNSAVED}
          </Notice>
        ) : null}
        {resumedAt === null ? null : (
          <Notice
            onDismiss={() => {
              setResumedAt(null)
            }}
          >
            {resumeLabel(resumedAt)}
          </Notice>
        )}
        {waitingOnDownload ? (
          <p role="status" data-testid="live-status" className={PILL}>
            {liveStatusLabel(liveStream, pendingSeek)}
          </p>
        ) : null}
        {showFewSeeders ? (
          <Notice
            tone="warn"
            onDismiss={() => {
              setFewSeedersDismissed(true)
            }}
          >
            {FEW_SEEDERS_NOTE}
          </Notice>
        ) : null}
      </div>

      <div
        aria-hidden={!chromeShown}
        className={cx(
          'transition-opacity duration-200',
          chromeShown ? 'opacity-100' : 'pointer-events-none opacity-0',
        )}
      >
        {/*
          Same wash as before, a quarter opaque at its darkest; the rest is
          blur. Padded by the safe-area insets as well (M18): the installed
          app draws edge to edge (`viewport-fit=cover`), and on a notched
          phone held sideways the back button would sit under the notch.
        */}
        <div className="absolute inset-x-0 top-0 flex items-center gap-3 bg-gradient-to-b from-[rgba(0,0,0,0.25)] to-transparent pt-[calc(20px+env(safe-area-inset-top))] pr-[max(24px,env(safe-area-inset-right))] pb-5 pl-[max(24px,env(safe-area-inset-left))] md:gap-3.5">
          <Link
            to={`/anime/${String(anime.id)}`}
            aria-label="Back to show"
            title="Back to show"
            className={cx(
              'inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full md:h-9 md:w-9',
              'pointer-coarse:md:h-11 pointer-coarse:md:w-11',
              'border-[0.5px] border-[rgba(255,255,255,0.2)] bg-[rgba(18,23,34,0.6)] backdrop-blur-glass',
              'text-[17px] leading-none text-white transition-colors duration-200 hover:bg-[rgba(18,23,34,0.78)]',
              FOCUS_RING,
            )}
          >
            ‹
          </Link>
          {/*
            `flex-1` so the title takes what is left between the two buttons
            and truncates there, rather than pushing the offline control off
            the edge on a phone.
          */}
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-[15px] font-semibold text-white">
              {anime.title.preferred}
            </h1>
            <p className="mt-0.5 truncate text-[13px] text-[rgba(235,239,245,0.82)]">
              <span>{episodeLabel}</span>
              {episodeTitle === null ? null : ` · ${episodeTitle}`}
              {languages === '' ? null : ` · ${languages}`}
            </p>
            {canKeepOffline ? (
              <OfflineReason
                episodeId={episode.id}
                className="mt-0.5 block truncate text-[12px] [text-shadow:0_1px_6px_rgba(0,0,0,0.6)]"
              />
            ) : null}
          </div>
          {/*
            Keep offline (FR-S9), opposite the way back. Downloading the
            episode being streamed does not touch playback: the source was
            chosen when the page opened (`useLocalCopy` resolves once per
            episode) and the device copy is used the next time it is opened.
          */}
          {canKeepOffline ? (
            <OfflineButton
              episode={episode}
              variant="player"
              playingFromDevice={source.kind === 'file'}
              onMenuOpenChange={setOfflineMenuOpen}
            />
          ) : null}
        </div>

        <div
          ref={barRef}
          role="group"
          aria-label="Player controls"
          className={cx(
            // 12px off the bottom edge, not 28: the burned-in subtitles sit
            // just above that edge, and the bar was parked on top of them.
            // Plus the safe-area insets (M18), which are zero in a browser
            // tab and keep the bar off the home indicator once installed.
            'absolute right-[max(12px,env(safe-area-inset-right))] bottom-[calc(12px+env(safe-area-inset-bottom))] left-[max(12px,env(safe-area-inset-left))]',
            'md:right-[max(24px,env(safe-area-inset-right))] md:left-[max(24px,env(safe-area-inset-left))]',
            // The hairline keeps its 0.16: at this fill it is the only thing
            // telling the eye where the bar ends and the picture starts.
            'rounded-card border-[0.5px] border-[rgba(255,255,255,0.16)]',
            // 0.22, down from 0.64 and then 0.35. What makes the bar readable
            // is the blur behind it, not the fill — the fill only has to keep
            // the picture from reading as text through the glass.
            'bg-[rgba(18,23,34,0.22)] px-2 py-2 shadow-bar backdrop-blur-bar md:px-2.5',
          )}
        >
          <div className="flex items-center gap-2.5 md:gap-3">
            {/*
              `leading-[16px]`, not the inherited 1.5: the clocks are the only
              text in the bar, so their line box is the scrubber row's height,
              and 18px of it against a 4px track is two pixels of the picture
              covered for nothing. Digits have no descenders to clip.
            */}
            <span className="w-[42px] shrink-0 text-[12px] leading-[16px] tabular-nums text-[rgba(235,239,245,0.9)]">
              {formatClock(position)}
            </span>
            {/*
              A slider rather than an `<input type=range>`: the track carries
              three layers (buffered, played, knob) that a native range cannot
              draw. Arrow keys are already handled for the whole page, so this
              adds pointer seeking and nothing else.
            */}
            <div
              role="slider"
              tabIndex={0}
              aria-label="Seek"
              aria-valuemin={0}
              aria-valuemax={Math.round(total)}
              aria-valuenow={Math.round(position)}
              aria-valuetext={formatClock(position)}
              onPointerDown={(event) => {
                draggingRef.current = true
                setScrubbing(true)
                event.currentTarget.setPointerCapture?.(event.pointerId)
                scrub(event)
              }}
              onPointerMove={(event) => {
                if (draggingRef.current) scrub(event)
              }}
              onPointerUp={(event) => {
                draggingRef.current = false
                setScrubbing(false)
                event.currentTarget.releasePointerCapture?.(event.pointerId)
              }}
              onPointerCancel={() => {
                draggingRef.current = false
                setScrubbing(false)
              }}
              className={cx(
                'relative h-1.5 flex-1 cursor-pointer rounded-full bg-[rgba(235,239,245,0.25)] md:h-1',
                "before:absolute before:inset-x-0 before:-top-4 before:-bottom-4 before:content-['']",
                FOCUS_RING,
              )}
            >
              {/*
                The download front (FR-P8), under everything else: how far a
                seek can go now. Past it, a seek waits for the download.
              */}
              {frontFraction === null ? null : (
                <span
                  aria-hidden
                  data-testid="live-front"
                  className="absolute inset-y-0 left-0 rounded-full bg-[rgba(235,239,245,0.3)]"
                  style={{ width: `${String(frontFraction * 100)}%` }}
                />
              )}
              <span
                aria-hidden
                className="absolute inset-y-0 left-0 rounded-full bg-[rgba(235,239,245,0.42)]"
                style={{ width: `${String(buffered * 100)}%` }}
              />
              <span
                aria-hidden
                className="absolute inset-y-0 left-0 rounded-full bg-white"
                style={{ width: `${String(played * 100)}%` }}
              />
              <span
                aria-hidden
                className={cx(
                  'absolute -top-[5px] h-4 w-4 -translate-x-2 rounded-full bg-white',
                  'md:-top-1 md:h-3 md:w-3 md:-translate-x-1.5',
                  'shadow-[0_2px_8px_rgba(0,0,0,0.6)]',
                )}
                style={{ left: `${String(played * 100)}%` }}
              />
            </div>
            <span className="w-[42px] shrink-0 text-right text-[12px] leading-[16px] tabular-nums text-[rgba(235,239,245,0.82)]">
              {`-${formatClock(remaining)}`}
            </span>
          </div>

          {/*
            Three groups, on a grid rather than a flex row with an `ml-auto`.
            The middle group is meant to be centred on the *picture*, and a
            flex row can only centre it in the space the other two leave — with
            three buttons on the left and one on the right that is visibly off.
            Two `1fr` columns either side of an `auto` one put it dead centre
            whatever the side groups weigh.
          */}
          <div className="mt-2 grid grid-cols-[1fr_auto_1fr] items-center gap-1 md:gap-2">
            <div className="flex items-center gap-1 justify-self-start md:gap-2">
              <button
                type="button"
                aria-label={`Back ${String(SKIP_STEP)} seconds`}
                title={`Back ${String(SKIP_STEP)} seconds`}
                onClick={() => {
                  skip(-SKIP_STEP)
                }}
                className={PLAYER_ICON}
              >
                <SkipSecondsGlyph back />
              </button>

              <button
                type="button"
                aria-label="Play or pause"
                // The shortcut line used to live under the bar; it lives here
                // now, behind the one control everybody hovers.
                title={`${playing ? 'Pause' : 'Play'} · ${KEY_HINT}`}
                onClick={togglePlay}
                className={cx(
                  'inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-white',
                  'md:h-10 md:w-10 pointer-coarse:md:h-11 pointer-coarse:md:w-11',
                  'text-black transition-transform duration-200 ease-arc hover:scale-[1.04]',
                  FOCUS_RING,
                )}
              >
                <PlayPauseGlyph playing={playing} />
              </button>

              <button
                type="button"
                aria-label={`Forward ${String(SKIP_STEP)} seconds`}
                title={`Forward ${String(SKIP_STEP)} seconds`}
                onClick={() => {
                  skip(SKIP_STEP)
                }}
                className={PLAYER_ICON}
              >
                <SkipSecondsGlyph back={false} />
              </button>
            </div>

            <div className="flex items-center gap-1 justify-self-center md:gap-2">
              {/* Exactly the two episode links, and nothing else, are the nav. */}
              <nav aria-label="Episodes" className="flex items-center gap-1 md:gap-2">
                <NeighbourLink
                  episode={previous}
                  label="Previous episode"
                  glyph={<SkipBackGlyph />}
                />
                {sequel.season !== null ? (
                  <NextSeasonLink
                    season={sequel.season}
                    sequel={sequel}
                    announce={!endOverlayShown}
                    glyph={<SkipForwardGlyph />}
                  />
                ) : (
                  <NeighbourLink episode={next} label="Next episode" glyph={<SkipForwardGlyph />} />
                )}
              </nav>
              {watchedByList ? (
                <button
                  type="button"
                  aria-label={WATCHED}
                  aria-pressed
                  aria-disabled
                  title={WATCHED_BY_PROGRESS_HINT}
                  className={PLAYER_ICON}
                >
                  {/* A tick, not a cross: there is no action to name here. */}
                  <WatchedGlyph filled />
                </button>
              ) : (
                <button
                  type="button"
                  aria-label={watched ? MARK_UNWATCHED : MARK_WATCHED}
                  aria-pressed={watched}
                  title={watched ? MARK_UNWATCHED : MARK_WATCHED}
                  disabled={watchedPending}
                  onClick={toggleWatched}
                  className={PLAYER_ICON}
                >
                  <WatchedGlyph filled={watched} cross={watched} />
                </button>
              )}
            </div>

            <div className="flex items-center gap-1 justify-self-end md:gap-2">
              {hasSubtitles ? (
                <button
                  type="button"
                  aria-label="Subtitles"
                  aria-pressed={subtitlesOn}
                  title={subtitlesOn ? 'Turn subtitles off' : 'Turn subtitles on'}
                  onClick={toggleSubtitles}
                  className={PLAYER_ICON}
                >
                  <SubtitlesGlyph on={subtitlesOn} />
                </button>
              ) : null}
              <button
                type="button"
                aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
                aria-pressed={fullscreen}
                title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
                onClick={toggleFullscreen}
                className={PLAYER_ICON}
              >
                <FullscreenGlyph exit={fullscreen} />
              </button>
            </div>
          </div>

          {watchedError === null ? null : (
            <p role="alert" className="mt-2 text-[12px] text-[var(--arc-error)]">
              {watchedError}
            </p>
          )}
        </div>
      </div>

      {/*
        The shelf above the bar, where the page's two small messages live: the
        unmute offer, and the completion receipt. A column rather than one
        corner each, so that on the occasion both are up neither is sitting on
        the other — and 108px up, the same shelf the fatal-error strip uses:
        clear of the bar whether the bar is showing or has faded out from
        under it.

        The receipt stands down when the end card comes up: the card says more
        than the pill does, and on a narrow screen they want the same corner.
        The unmute pill does not, because muted playback is still wrong while
        the card is up.
      */}
      <div className="absolute bottom-[108px] left-3 z-10 flex flex-col items-start gap-2 md:left-6">
        {autoplayMuted ? <UnmutePill onUnmute={unmute} /> : null}
        {markedToast && !endOverlayShown ? (
          <MarkedToast
            onDismiss={() => {
              setMarkedToast(false)
            }}
          />
        ) : null}
      </div>

      {endOverlayShown ? (
        <EndOverlay info={info} sequel={sequel} onKeepWatching={keepWatching} />
      ) : null}
    </div>
  )
}

export function Player() {
  const { episodeId } = useParams()
  const id = Number(episodeId)

  if (!Number.isInteger(id) || id <= 0) {
    return <NotReady message="That is not an episode id." />
  }

  return <PlayerView key={id} id={id} />
}
