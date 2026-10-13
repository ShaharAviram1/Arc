import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import type HlsJs from 'hls.js'
import type { ErrorData } from 'hls.js'
import { buttonClass } from '@/components/ui/styles'
import { applyHlsSubtitles, showTextTracks } from '@/player/subtitles'

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
 *
 * Two things it does decide (M20 T3, architecture §5.4a):
 *
 * - **Where hls.js starts loading** (`startPosition`): the caller's resume
 *   position, or 0 — never hls.js's default of "the live edge", which on a
 *   growing (EVENT) playlist would start the viewer at the download front.
 * - **Whether the subtitle track shows** (`subtitles`), through
 *   `setSubtitles(on)`: `hls.subtitleTrack` on the hls.js path, the
 *   `TextTrack`'s `mode` on the native one (see `player/subtitles.ts`). It is
 *   applied after `loadedmetadata`, whenever hls.js updates or switches its
 *   subtitle tracks, whenever Safari adds one, and whenever the prop changes.
 *
 * And, for a stream still being cut from a download (`live`, M20 T7,
 * architecture §5.4a), **that the edge is never chased**: hls.js is told its
 * target latency is a day (`liveSyncDuration`), with no maximum latency and
 * no catch-up playback rate, so it never seeks or speeds the viewer towards
 * the download front. Safari is given the same start by hand: on the native
 * path the element is put at `startPosition` once its metadata has loaded,
 * whatever it made of `EXT-X-START`, and its `duration` (which may be
 * `Infinity` on a growing playlist) is never consulted for it.
 */

/**
 * hls.js's target distance from the live edge on a growing stream: far more
 * than any episode, so the sync point is always the playlist's start and
 * nothing ever moves the viewer towards the front (M20 T7).
 */
export const LIVE_SYNC_DURATION_S = 86_400

/** Asked of the element; only meaningful together with the MSE check above. */
const HLS_MIME = 'application/vnd.apple.mpegurl'

const NO_HLS_MESSAGE = 'This browser cannot play HLS.'
const FATAL_MESSAGE = 'Playback stopped. The connection to the server may have dropped.'

type Loading = 'loading' | 'native' | 'hls' | 'unsupported'

export interface HlsVideoProps {
  /** The playlist URL the server handed us; never built from a route param. */
  src: string
  /**
   * `file` for an episode kept on this device (FR-S9): `src` is a blob URL of
   * one fragmented MP4, which goes onto the element as it is — no hls.js, no
   * playlist. `hls` (the default) is the stream.
   */
  kind?: 'hls' | 'file'
  /** The element refused a `file` source (the file is gone or unreadable). */
  onSourceError?: () => void
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
  /**
   * Where hls.js starts loading, in seconds (the resume position, or 0). Read
   * when the instance is created, never as a reason to recreate one.
   */
  startPosition?: number
  /**
   * Whether the stream's subtitle track should show (FR-S10). `undefined`
   * leaves the track alone (a stream with no text track, or a device copy).
   */
  subtitles?: boolean
  /**
   * A stream still being cut from a download (M20 T7): hls.js is configured
   * not to chase the edge. Read when the instance is created; a stream that
   * becomes whole keeps its instance (and its URLs) with no reload.
   */
  live?: boolean
}

export function HlsVideo({
  src,
  kind = 'hls',
  onSourceError,
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
  startPosition = 0,
  subtitles,
  live = false,
}: HlsVideoProps) {
  const [mode, setMode] = useState<Loading>('loading')
  const [fatal, setFatal] = useState<string | null>(null)
  /** Bumped by Retry; recreating the instance is the only reliable recovery. */
  const [attempt, setAttempt] = useState(0)
  const hlsRef = useRef<HlsJs | null>(null)
  /**
   * The latest props the effects below read without depending on them: a
   * new resume position or a toggle must not tear the stream down.
   */
  const startRef = useRef(startPosition)
  const subtitlesRef = useRef(subtitles)
  const liveRef = useRef(live)
  useEffect(() => {
    startRef.current = startPosition
    subtitlesRef.current = subtitles
    liveRef.current = live
  })
  /**
   * The native path's one start seek has been made for this source (M20 T7):
   * a second `loadedmetadata` (Safari reloading the playlist) must not throw
   * the viewer back to where they started.
   */
  const nativeStartedRef = useRef(false)

  /**
   * The CC switch (FR-S10). hls.js owns the track's mode on its path, so it
   * is asked through its own API; on the native path the element's
   * `textTracks` are the only handle there is.
   */
  const setSubtitles = useCallback(
    (on: boolean) => {
      const hls = hlsRef.current
      if (hls !== null) {
        applyHlsSubtitles(hls, on)
        return
      }
      showTextTracks(videoRef.current?.textTracks, on)
    },
    [videoRef],
  )

  /** The prop's current value applied, if it has one. */
  const applySubtitles = useCallback(() => {
    const on = subtitlesRef.current
    if (on !== undefined) setSubtitles(on)
  }, [setSubtitles])

  useEffect(() => {
    if (subtitles !== undefined) setSubtitles(subtitles)
  }, [subtitles, setSubtitles])

  useEffect(() => {
    const video = videoRef.current
    if (video === null || src === '') return

    setFatal(null)

    // A file on this device (FR-S9) plays natively everywhere. No `stalled`
    // listener on this path, ever: a blob never stalls for want of a network,
    // and treating its idle moments as a stall is what broke playback in the
    // project this design comes from.
    if (kind === 'file') {
      video.src = src
      return () => {
        video.removeAttribute('src')
      }
    }

    // Prefer hls.js whenever Media Source Extensions exist: Chrome answers
    // "maybe" to the HLS mime check yet cannot play a playlist natively, so
    // canPlayType alone is not a usable signal. Only a browser with no MSE
    // (iOS Safari) gets the native path.
    const hasMse = typeof window.MediaSource === 'function'
    if (!hasMse && video.canPlayType(HLS_MIME) !== '') {
      video.src = src
      nativeStartedRef.current = false
      setMode('native')
      // Safari may add the subtitle rendition's track after the metadata has
      // loaded; the viewer's choice is applied to it as it appears.
      // (Guarded: a test DOM's list may be no event target at all.)
      const tracks = video.textTracks as Partial<TextTrackList> | undefined
      const listens =
        tracks !== undefined &&
        typeof tracks.addEventListener === 'function' &&
        typeof tracks.removeEventListener === 'function'
      if (listens) tracks.addEventListener?.('addtrack', applySubtitles)
      return () => {
        if (listens) tracks.removeEventListener?.('addtrack', applySubtitles)
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
          // From the resume position (or the start), never the live edge an
          // EVENT playlist would otherwise put the viewer at (M20).
          startPosition: startRef.current,
          // A growing stream (M20 T7): never towards the download front. The
          // sync point is the playlist's start, there is no maximum latency
          // (hls.js's count default is Infinity), and no catch-up rate.
          ...(liveRef.current
            ? {
                liveSyncDuration: LIVE_SYNC_DURATION_S,
                liveMaxLatencyDuration: undefined,
                maxLiveSyncPlaybackRate: 1,
              }
            : {}),
          // The remux's WebVTT track, drawn by the browser as a TextTrack so
          // `::cue` styles it (FR-S10).
          renderTextTracksNatively: true,
        })
        // On by default. hls.js 1.7 has no `subtitleDisplay` config option —
        // it is a property of the instance (true by default) — so it is set
        // here, explicitly, rather than left to the library's default.
        hls.subtitleDisplay = true
        hlsRef.current = hls
        // hls.js picks the playlist's DEFAULT track itself; a viewer who
        // switched subtitles off has that undone as soon as it happens.
        hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, applySubtitles)
        hls.on(Hls.Events.SUBTITLE_TRACK_SWITCH, applySubtitles)
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
  }, [src, kind, attempt, videoRef, applySubtitles])

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
        onError={() => {
          if (kind === 'file') onSourceError?.()
        }}
        onLoadedMetadata={() => {
          // Safari on an EVENT playlist (M20 T7): from the start position,
          // never the edge, once per source. `duration` is not looked at — it
          // may be Infinity while the stream grows.
          const element = videoRef.current
          if (mode === 'native' && element !== null && !nativeStartedRef.current) {
            nativeStartedRef.current = true
            if (Math.abs(element.currentTime - startRef.current) > 0.5) {
              element.currentTime = startRef.current
            }
          }
          if (kind === 'hls') applySubtitles()
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
