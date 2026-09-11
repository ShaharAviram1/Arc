import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import type HlsJs from 'hls.js'
import type { ErrorData } from 'hls.js'
import { buttonClass } from '@/components/ui/styles'

/**
 * A `<video>` fed by HLS (spec §4.5 FR-S1, §5.4).
 *
 * Two ways in, and the rule that picks between them is deliberately lopsided:
 * the playlist only goes straight into the element as a `src`, with no library
 * at all, when the browser has **no** `MediaSource` *and* answers
 * `canPlayType` for the HLS mime with something other than `''`. Every other
 * browser gets hls.js. `canPlayType` on its own is not a usable signal —
 * Chrome answers `'maybe'` for the playlist mime yet cannot play one natively,
 * which leaves the element sitting on a spinner forever — so the absence of
 * Media Source Extensions is what actually identifies the native-only browser
 * (iOS Safari). hls.js is imported lazily: it is by far the largest thing the
 * client ships, and only one route in the app ever needs it, so it must not
 * sit in the entry chunk.
 *
 * Either way the playlist and its segments are behind the session cookie, so
 * the requests hls.js makes have to carry credentials — hence `xhrSetup`. The
 * native path needs nothing: same origin, so the browser sends the cookie.
 *
 * All media state stays on the element the caller owns through `videoRef`;
 * this component only decides how bytes get into it, and reports the events
 * back out.
 */

/** Asked of the element; only meaningful together with the MSE check above. */
const HLS_MIME = 'application/vnd.apple.mpegurl'

const NO_HLS_MESSAGE = 'This browser cannot play HLS.'
const FATAL_MESSAGE = 'Playback stopped. The connection to the server may have dropped.'

type Loading = 'loading' | 'native' | 'hls' | 'unsupported'

export interface HlsVideoProps {
  /** The playlist URL the server handed us; never built from a route param. */
  src: string
  /** The caller owns the element: it seeks, mutes and reads position from it. */
  videoRef: RefObject<HTMLVideoElement | null>
  /**
   * The element's accessible name. A `<video>` has no text of its own, so
   * without one a screen reader announces an unlabelled media player and the
   * viewer has no way to tell which episode they landed on.
   */
  label: string
  className?: string
  /** Fires on `loadedmetadata` with the media's own duration (may be NaN). */
  onReady?: (duration: number) => void
  onTimeUpdate?: (position: number, duration: number) => void
  onPlay?: (position: number, duration: number) => void
  onPause?: (position: number, duration: number) => void
  onSeeked?: (position: number, duration: number) => void
  onEnded?: (position: number, duration: number) => void
  /**
   * Mute and rate changes, for a page that draws its own controls: the element
   * is the source of truth for both, and either can be moved by a keyboard
   * shortcut as easily as by a button, so anything drawing that state has to
   * hear about it from here rather than from whoever pressed something.
   */
  onVolumeChange?: (muted: boolean) => void
  onRateChange?: (rate: number) => void
  /**
   * A click on the picture itself — not on the chrome the page floats over it.
   * It is on the element rather than on the page's container so that a press
   * on a button, a notice or the retry strip is never mistaken for a gesture
   * aimed at the video. Counting clicks (single vs double) is the caller's
   * business; this only says that one happened.
   */
  onVideoClick?: () => void
}

export function HlsVideo({
  src,
  videoRef,
  label,
  className = '',
  onReady,
  onTimeUpdate,
  onPlay,
  onPause,
  onSeeked,
  onEnded,
  onVolumeChange,
  onRateChange,
  onVideoClick,
}: HlsVideoProps) {
  const [mode, setMode] = useState<Loading>('loading')
  const [fatal, setFatal] = useState<string | null>(null)
  /** Bumped by Retry; recreating the instance is the only reliable recovery. */
  const [attempt, setAttempt] = useState(0)
  const hlsRef = useRef<HlsJs | null>(null)

  useEffect(() => {
    const video = videoRef.current
    if (video === null || src === '') return

    setFatal(null)

    // Prefer hls.js whenever Media Source Extensions exist: Chrome answers
    // "maybe" to the HLS mime check yet cannot play a playlist natively, so
    // canPlayType alone is not a usable signal. Only a browser with no MSE
    // (iOS Safari) gets the native path.
    const hasMse = typeof window.MediaSource === 'function'
    if (!hasMse && video.canPlayType(HLS_MIME) !== '') {
      video.src = src
      setMode('native')
      return () => {
        video.removeAttribute('src')
      }
    }

    setMode('loading')
    let cancelled = false

    void import('hls.js')
      .then(({ default: Hls }) => {
        if (cancelled) return
        if (!Hls.isSupported()) {
          setMode('unsupported')
          return
        }

        const hls = new Hls({
          xhrSetup: (xhr) => {
            xhr.withCredentials = true
          },
        })
        hlsRef.current = hls
        hls.on(Hls.Events.ERROR, (_event: unknown, data: ErrorData) => {
          // Non-fatal errors are hls.js's own retries and are not worth
          // interrupting playback over; a fatal one has given up.
          if (data.fatal) setFatal(FATAL_MESSAGE)
        })
        hls.loadSource(src)
        hls.attachMedia(video)
        setMode('hls')
      })
      .catch(() => {
        if (!cancelled) setMode('unsupported')
      })

    return () => {
      cancelled = true
      hlsRef.current?.destroy()
      hlsRef.current = null
    }
  }, [src, attempt, videoRef])

  const positionOf = useCallback((): [number, number] => {
    const video = videoRef.current
    return [video?.currentTime ?? 0, video?.duration ?? Number.NaN]
  }, [videoRef])

  const emit = useCallback(
    (handler: ((position: number, duration: number) => void) | undefined) => () => {
      if (handler === undefined) return
      const [position, duration] = positionOf()
      handler(position, duration)
    },
    [positionOf],
  )

  return (
    // Fills the positioned parent the page gives it, rather than taking a
    // height from the element: the video *is* the page (M15), and a `<video>`
    // left to itself is 300×150.
    <div className={`absolute inset-0 bg-black ${className}`}>
      {/*
        No `controls`: the page draws its own (M15). The element keeps every
        media event it ever emitted, so the reporter and the shortcuts are
        untouched — only the chrome the browser would have painted is gone.
      */}
      <video
        ref={videoRef}
        playsInline
        aria-label={label}
        className="h-full w-full bg-black object-contain"
        onClick={onVideoClick}
        onLoadedMetadata={() => {
          onReady?.(videoRef.current?.duration ?? Number.NaN)
        }}
        onTimeUpdate={emit(onTimeUpdate)}
        onPlay={emit(onPlay)}
        onPause={emit(onPause)}
        onSeeked={emit(onSeeked)}
        onEnded={emit(onEnded)}
        onVolumeChange={() => {
          onVolumeChange?.(videoRef.current?.muted ?? false)
        }}
        onRateChange={() => {
          onRateChange?.(videoRef.current?.playbackRate ?? 1)
        }}
      />

      {mode === 'unsupported' ? (
        <p
          role="alert"
          className="absolute inset-0 flex items-center justify-center p-6 text-center text-[14px] text-[var(--arc-text-muted)]"
        >
          {NO_HLS_MESSAGE}
        </p>
      ) : null}

      {/*
        Above the control bar rather than flush to the bottom edge, which the
        bar now occupies. Glass, like everything else floating over the video.
        108px clears the shortened bar (12px inset, under 84px tall) with a
        little air; it does not have to track the bar exactly, only stay off it.
      */}
      {fatal === null ? null : (
        <div className="absolute inset-x-0 bottom-[108px] mx-auto flex w-fit max-w-[80%] flex-wrap items-center justify-center gap-4 rounded-card border-[0.5px] border-[rgba(255,255,255,0.16)] bg-[rgba(18,23,34,0.72)] px-5 py-3.5 text-[14px] shadow-bar backdrop-blur-bar">
          <span role="alert" className="text-[var(--arc-error)]">
            {fatal}
          </span>
          <button
            type="button"
            onClick={() => {
              setAttempt((value) => value + 1)
            }}
            className={buttonClass('chip', 'text-[14px] text-[var(--arc-text)]')}
          >
            Retry
          </button>
        </div>
      )}
    </div>
  )
}
