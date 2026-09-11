import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import { Link, useParams } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import { buttonClass, cx, FOCUS_RING } from '@/components/ui'
import { isStatus } from '@/lib/auth'
import {
  formatClock,
  resumeLabel,
  shouldResume,
  useMarkWatched,
  usePlayInfo,
  useReportProgress,
  useUnmarkWatched,
  type EpisodeRef,
  type PlayInfo,
} from '@/lib/playback'
import { HlsVideo } from '@/player/HlsVideo'
import { ProgressReporter } from '@/player/ProgressReporter'

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
 * The bar's second pass is about what it *covers*. Subtitles are burned into
 * the picture just above the bottom edge, so the bar sits 12px off it, is
 * barely there (0.22, carried by the blur rather than the fill, with the
 * hairline left at full strength to draw the edge), is under 84px tall on a
 * desktop, and takes itself away after a second and a half of an idle pointer
 * — playing or paused alike. The line of keyboard hints that used to sit under
 * the controls is gone from the screen and lives in the play button's tooltip.
 *
 * Nothing below the chrome changed: the same reporter, the same shortcuts, the
 * same resume, the same warning strip — a button is simply a second way to
 * reach what the keyboard already did.
 */

/** ± this many seconds on an arrow key (FR-S6). */
const SEEK_STEP = 5

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

/** Pointer moves are continuous; this is how often one is allowed to re-render. */
const ACTIVITY_THROTTLE_MS = 400

/** Fields that own their keystrokes; a shortcut must not fire inside one. */
const EDITABLE = new Set(['INPUT', 'TEXTAREA', 'SELECT'])

const NOT_READY_TITLE = 'Episode not ready'
/** A failed mark-watched used to say nothing at all, which reads as "done". */
const MARK_WATCHED_FAILED = 'Could not mark that watched. Try again.'
const UNMARK_WATCHED_FAILED = 'Could not unmark that. Try again.'
const NOT_READY_BODY =
  'Arc has no playable file for this episode yet. It may still be downloading or being prepared.'

const LOAD_FAILED_TITLE = 'Could not load this episode'
const LOAD_FAILED_BODY = 'Could not load this episode. Try again shortly.'

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
 */
const PLAYER_ICON = cx(
  'inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border-[0.5px]',
  'md:h-9 md:w-9',
  'border-[rgba(255,255,255,0.18)] bg-[rgba(255,255,255,0.12)] text-white',
  'transition-colors duration-200 hover:bg-[rgba(255,255,255,0.2)]',
  'disabled:cursor-not-allowed disabled:opacity-40',
  FOCUS_RING,
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
 * ⟲ 10 / ⟳ 10 — the round-trip arrow every player uses for a ten-second jump,
 * with the number reading through the middle of it so the button says how far
 * without a word of text beside it.
 *
 * One drawing, mirrored for the forward case, and only the arc is mirrored:
 * the number has to stay the right way round. The arc is three quarters of a
 * circle with the gap at the top, and the open chevron sitting in that gap is
 * the head, pointing the way the playhead is about to move.
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
        <path d="M12 5.1A7.6 7.6 0 1 0 19.6 12.7" />
        <path d="M15.1 1.9 11.9 5.1l3.2 3.2" />
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
 * A check in a circle: outlined while the episode is unwatched, filled once it
 * is. The filled tick is punched out in the bar's own colour rather than drawn
 * in white, which would disappear into the disc behind it.
 */
function WatchedGlyph({ filled }: { filled: boolean }) {
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
      <path
        d="m8.1 12.3 2.7 2.7 5.1-5.6"
        className={filled ? 'fill-none stroke-[#121722]' : 'fill-none stroke-current'}
      />
    </svg>
  )
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return EDITABLE.has(target.tagName) || target.isContentEditable
}

/** The media's own duration when it has one, else what the transcode recorded. */
function resolveDuration(mediaDuration: number, fallback: number): number {
  return Number.isFinite(mediaDuration) && mediaDuration > 0 ? mediaDuration : fallback
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
 * What happens next, once the episode is finished (FR-S5). The next episode is
 * offered when it is playable and named when it is not, so the viewer knows
 * whether to wait or to leave.
 */
function EndOverlay({ info, onDismiss }: { info: PlayInfo; onDismiss: () => void }) {
  const next = info.next

  return (
    <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-5 bg-black/80 p-6 text-center">
      {next === null ? (
        <p className="text-[20px] font-semibold text-[var(--arc-text)]">
          This was the last episode
        </p>
      ) : next.ready ? (
        <>
          <p className="text-[20px] font-semibold text-[var(--arc-text)]">
            {`Next: Episode ${String(next.number)}`}
          </p>
          <Link to={`/watch/${String(next.id)}`} className={buttonClass('primary')}>
            Play
          </Link>
        </>
      ) : (
        <p className="text-[20px] font-semibold text-[var(--arc-text)]">
          {`Episode ${String(next.number)} isn’t ready yet`}
        </p>
      )}

      <div className="flex items-center gap-5 text-[14px]">
        <Link
          to={`/anime/${String(info.anime.id)}`}
          className={cx('text-[var(--arc-text)] underline-offset-4 hover:underline', FOCUS_RING)}
        >
          Back to show
        </Link>
        <button
          type="button"
          onClick={onDismiss}
          className={cx(
            'text-[var(--arc-text-muted)] hover:text-[var(--arc-text)]',
            'underline-offset-4 hover:underline',
            FOCUS_RING,
          )}
        >
          Dismiss
        </button>
      </div>
    </div>
  )
}

/**
 * The page for one episode. Mounted under a `key` of that episode's id, so
 * moving to the next one starts from scratch — no resume flag, no end overlay
 * and no reporter left over from the episode just finished.
 */
function PlayerView({ id }: { id: number }) {
  const { data, isPending, isError, isFetching, error, refetch } = usePlayInfo(id)

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const barRef = useRef<HTMLDivElement | null>(null)
  const reporterRef = useRef<ProgressReporter | null>(null)
  /** A resume is a one-off: re-seeking on every metadata event would trap the viewer. */
  const resumedRef = useRef(false)
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
  /** Touch and mouse want different things from a tap on the video. */
  const pointerTypeRef = useRef<string>('mouse')
  /** Whether the last thing the viewer did was press a key rather than aim. */
  const keyboardRef = useRef(false)
  /** The pending single click, waiting to find out if it is half of a double. */
  const clickTimerRef = useRef<number | null>(null)

  const [resumedAt, setResumedAt] = useState<number | null>(null)
  const [finished, setFinished] = useState(false)
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

  const serverDuration = data?.duration ?? 0
  const total = mediaDuration > 0 ? mediaDuration : serverDuration
  const { mutate: reportProgress } = useReportProgress()
  const markWatched = useMarkWatched()
  const unmarkWatched = useUnmarkWatched()

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
            if (result.newly_completed) setFinished(true)
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
    if (video.paused) void video.play()
    else video.pause()
  }, [])

  /**
   * A click on the video itself.
   *
   * A single one plays or pauses and a double one goes fullscreen, which are
   * the two gestures every video on the web has — but a double click is two
   * clicks, so the single has to wait a quarter of a second to find out
   * whether it was the first half of one. Without that wait, double-clicking
   * to go fullscreen would also pause the episode, which is exactly the moment
   * a viewer least wants it to stop.
   *
   * One exception, for touch only: a tap that brought the hidden controls back
   * has already done its job, and pausing as well would make the chrome
   * impossible to consult without interrupting playback.
   */
  const onVideoClick = useCallback(() => {
    if (clickTimerRef.current !== null) {
      window.clearTimeout(clickTimerRef.current)
      clickTimerRef.current = null
      toggleFullscreen()
      return
    }

    const revealedByTap = pointerTypeRef.current === 'touch' && hiddenAtPressRef.current
    clickTimerRef.current = window.setTimeout(() => {
      clickTimerRef.current = null
      if (revealedByTap) return
      togglePlay()
    }, DOUBLE_CLICK_MS)
  }, [toggleFullscreen, togglePlay])

  useEffect(
    () => () => {
      if (clickTimerRef.current !== null) window.clearTimeout(clickTimerRef.current)
    },
    [],
  )

  /** The one way the page moves the playhead; everything else calls this. */
  const seekTo = useCallback(
    (seconds: number) => {
      const video = videoRef.current
      if (video === null) return
      const limit = resolveDuration(video.duration, serverDuration)
      const clamped = Math.max(0, limit > 0 ? Math.min(seconds, limit) : seconds)
      video.currentTime = clamped
      setPosition(clamped)
    },
    [serverDuration],
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
          if (video.paused) void video.play()
          else video.pause()
          return
        case 'ArrowLeft':
          event.preventDefault()
          video.currentTime = Math.max(0, video.currentTime - SEEK_STEP)
          setPosition(video.currentTime)
          return
        case 'ArrowRight':
          event.preventDefault()
          video.currentTime = video.currentTime + SEEK_STEP
          setPosition(video.currentTime)
          return
        case 'Home':
          event.preventDefault()
          video.currentTime = 0
          setPosition(0)
          return
        case 'End': {
          event.preventDefault()
          const limit = resolveDuration(video.duration, serverDuration)
          if (limit <= 0) return
          video.currentTime = limit
          setPosition(limit)
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
        default:
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [serverDuration, toggleFullscreen])

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

    function onPointerDown(event: PointerEvent) {
      pointerTypeRef.current = event.pointerType === '' ? 'mouse' : event.pointerType
      keyboardRef.current = false
      hiddenAtPressRef.current = !chromeShownRef.current
      show()
    }

    function onTouchStart() {
      pointerTypeRef.current = 'touch'
      keyboardRef.current = false
      hiddenAtPressRef.current = !chromeShownRef.current
      show()
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
   * dragging it. The end overlay and the unsaved-progress strip are both the
   * page saying something playback cannot show, and both offer a control —
   * Play, Dismiss — that has to stay reachable while they are up.
   */
  const progressWarning = progressFailures >= PROGRESS_FAILURE_LIMIT && !progressWarningDismissed
  const chromeHeld = scrubbing || finished || progressWarning

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

  if (isPending) {
    return (
      <div className="fixed inset-0 flex items-center justify-center bg-black">
        <p role="status" className="text-[14px] text-[var(--arc-text-muted)]">
          Loading…
        </p>
      </div>
    )
  }

  if (isError) {
    // 404 is the server saying "no rendition"; anything else is Arc failing,
    // and telling the viewer their episode is not ready would be a statement
    // about their library that this client has no basis for. Only the second
    // is worth a retry: a missing rendition is not made by asking again.
    if (isStatus(error, 404)) return <NotReady message={NOT_READY_BODY} />

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

  const info: PlayInfo = data
  const { anime, episode, previous, next } = info
  const episodeLabel = `Episode ${String(episode.number)}`

  const watched = watchedOverride ?? episode.watched
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
  const remaining = total > 0 ? Math.max(0, total - position) : 0

  function onReady(duration: number) {
    const video = videoRef.current
    if (video === null) return

    setMediaDuration(resolveDuration(duration, 0))

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
    if (!shouldResume(start, resolveDuration(duration, serverDuration)) || start === null) return

    video.currentTime = start
    setPosition(start)
    setResumedAt(start)
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
        src={info.playlist_url}
        videoRef={videoRef}
        label={`${anime.title.preferred} — ${episodeLabel}`}
        // The cursor goes wherever the chrome goes: a pointer arrow parked
        // over a full-bleed picture is the same kind of clutter as the bar.
        className={chromeShown ? '' : 'cursor-none'}
        onVideoClick={onVideoClick}
        onReady={onReady}
        onTimeUpdate={(nextPosition, duration) => {
          reporterRef.current?.update(nextPosition, resolveDuration(duration, serverDuration))
          setPosition(nextPosition)
          setMediaDuration(resolveDuration(duration, 0))
          setBuffered(bufferedFraction(videoRef.current, resolveDuration(duration, serverDuration)))
        }}
        onPlay={(nextPosition, duration) => {
          reporterRef.current?.play(nextPosition, resolveDuration(duration, serverDuration))
          setPlaying(true)
        }}
        onPause={(nextPosition, duration) => {
          reporterRef.current?.pause(nextPosition, resolveDuration(duration, serverDuration))
          setPlaying(false)
          // A pause is a sign of a person, so the bar comes back — but it no
          // longer *stays*: the idle timer runs while paused too, and takes it
          // away again if nothing else happens (owner, 2026-09-12).
          setChrome(true)
        }}
        onSeeked={(nextPosition, duration) => {
          reporterRef.current?.seek(nextPosition, resolveDuration(duration, serverDuration))
          setPosition(nextPosition)
        }}
        onEnded={(nextPosition, duration) => {
          reporterRef.current?.end(nextPosition, resolveDuration(duration, serverDuration))
          setPlaying(false)
          setChrome(true)
          setFinished(true)
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
      </div>

      <div
        aria-hidden={!chromeShown}
        className={cx(
          'transition-opacity duration-200',
          chromeShown ? 'opacity-100' : 'pointer-events-none opacity-0',
        )}
      >
        {/* Same wash as before, a quarter opaque at its darkest; the rest is blur. */}
        <div className="absolute inset-x-0 top-0 flex items-center gap-3 bg-gradient-to-b from-[rgba(0,0,0,0.25)] to-transparent px-6 py-5">
          <Link
            to={`/anime/${String(anime.id)}`}
            aria-label="Back to show"
            className={cx(
              'inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full md:h-9 md:w-9',
              'border-[0.5px] border-[rgba(255,255,255,0.2)] bg-[rgba(18,23,34,0.6)] backdrop-blur-glass',
              'text-[17px] leading-none text-white',
              FOCUS_RING,
            )}
          >
            ‹
          </Link>
          <div className="min-w-0">
            <h1 className="truncate text-[15px] font-semibold text-white">
              {anime.title.preferred}
            </h1>
            <p className="mt-0.5 truncate text-[13px] text-[rgba(235,239,245,0.82)]">
              <span>{episodeLabel}</span>
              {episodeTitle === null ? null : ` · ${episodeTitle}`}
              {languages === '' ? null : ` · ${languages}`}
            </p>
          </div>
        </div>

        <div
          ref={barRef}
          role="group"
          aria-label="Player controls"
          className={cx(
            // 12px off the bottom edge, not 28: the burned-in subtitles sit
            // just above that edge, and the bar was parked on top of them.
            'absolute right-3 bottom-3 left-3 md:right-6 md:left-6',
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
                  'md:h-10 md:w-10',
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
                <NeighbourLink episode={next} label="Next episode" glyph={<SkipForwardGlyph />} />
              </nav>
              <button
                type="button"
                aria-label={watched ? 'Unmark watched' : 'Mark watched'}
                aria-pressed={watched}
                title={watched ? 'Unmark watched' : 'Mark watched'}
                disabled={watchedPending}
                onClick={toggleWatched}
                className={PLAYER_ICON}
              >
                <WatchedGlyph filled={watched} />
              </button>
            </div>

            <div className="flex items-center justify-self-end">
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

      {finished ? (
        <EndOverlay
          info={info}
          onDismiss={() => {
            setFinished(false)
          }}
        />
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
