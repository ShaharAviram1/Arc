import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import type HlsJs from 'hls.js'
import type { ErrorData } from 'hls.js'

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
    <div className={`relative bg-black ${className}`}>
      <video
        ref={videoRef}
        controls
        playsInline
        aria-label={label}
        className="max-h-[80vh] w-full bg-black"
        onLoadedMetadata={() => {
          onReady?.(videoRef.current?.duration ?? Number.NaN)
        }}
        onTimeUpdate={emit(onTimeUpdate)}
        onPlay={emit(onPlay)}
        onPause={emit(onPause)}
        onSeeked={emit(onSeeked)}
        onEnded={emit(onEnded)}
      />

      {mode === 'unsupported' ? (
        <p
          role="alert"
          className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-[var(--arc-text-muted)]"
        >
          {NO_HLS_MESSAGE}
        </p>
      ) : null}

      {fatal === null ? null : (
        <div className="absolute inset-x-0 bottom-0 flex flex-wrap items-center justify-center gap-3 bg-[var(--arc-surface)]/95 p-3 text-sm">
          <span role="alert" className="text-[var(--arc-error)]">
            {fatal}
          </span>
          <button
            type="button"
            onClick={() => {
              setAttempt((value) => value + 1)
            }}
            className="rounded-md bg-[var(--arc-accent)] px-3 py-1 text-xs font-medium text-[var(--arc-accent-contrast)] hover:opacity-90"
          >
            Retry
          </button>
        </div>
      )}
    </div>
  )
}
