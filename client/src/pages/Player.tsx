import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import { isStatus } from '@/lib/auth'
import {
  formatClock,
  resumeLabel,
  shouldResume,
  useMarkWatched,
  usePlayInfo,
  useReportProgress,
  type EpisodeRef,
  type PlayInfo,
} from '@/lib/playback'
import { HlsVideo } from '@/player/HlsVideo'
import { ProgressReporter } from '@/player/ProgressReporter'

/**
 * The player (spec §4.5 FR-S1–FR-S6, roadmap M8).
 *
 * Full-bleed and outside the app's sidebar: the route is a sibling of the
 * layout under the same auth gate, so this page is the whole window.
 *
 * Everything the page needs comes from one call — the playlist URL, the
 * duration, where to resume, and the episodes either side — so there is no
 * second request to race with the video starting. The media element itself is
 * the source of truth for position; the server's `duration` only stands in
 * while the media has not reported its own.
 */

/** ± this many seconds on an arrow key (FR-S6). */
const SEEK_STEP = 5

/** Fields that own their keystrokes; a shortcut must not fire inside one. */
const EDITABLE = new Set(['INPUT', 'TEXTAREA', 'SELECT'])

const NOT_READY_TITLE = 'Episode not ready'
/** A failed mark-watched used to say nothing at all, which reads as "done". */
const MARK_WATCHED_FAILED = 'Could not mark that watched. Try again.'
const NOT_READY_BODY =
  'Arc has no playable file for this episode yet. It may still be downloading or being prepared.'

const LOAD_FAILED_TITLE = 'Could not load this episode'
const LOAD_FAILED_BODY = 'Could not load this episode. Try again shortly.'

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

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return EDITABLE.has(target.tagName) || target.isContentEditable
}

/** The media's own duration when it has one, else what the transcode recorded. */
function resolveDuration(mediaDuration: number, fallback: number): number {
  return Number.isFinite(mediaDuration) && mediaDuration > 0 ? mediaDuration : fallback
}

function NotReady({ message }: { message: string }) {
  return (
    <div className="mx-auto flex min-h-screen max-w-lg flex-col items-center justify-center p-6 text-center">
      <h1 className="text-xl font-semibold text-[var(--arc-text)]">{NOT_READY_TITLE}</h1>
      <p className="mt-2 text-sm text-[var(--arc-text-muted)]">{message}</p>
      <Link to="/" className="mt-6 text-sm text-[var(--arc-accent)] hover:underline">
        Back to home
      </Link>
    </div>
  )
}

/**
 * A neighbouring episode. One that is not ready is still shown, disabled, with
 * its state as the reason — hiding it would read as "there is no next episode",
 * which is a different and wrong thing to say (FR-S5).
 */
function NeighbourLink({ episode, label }: { episode: EpisodeRef | null; label: string }) {
  if (episode === null) return null

  const text = `${label} · Episode ${String(episode.number)}`
  if (!episode.ready) {
    return (
      <button
        type="button"
        disabled
        title={`Episode ${String(episode.number)} isn’t ready yet`}
        className="cursor-not-allowed rounded-md border border-[var(--arc-border)] px-2.5 py-1 text-xs text-[var(--arc-text-muted)] opacity-60"
      >
        {text}
      </button>
    )
  }

  return (
    <Link
      to={`/watch/${String(episode.id)}`}
      className="rounded-md border border-[var(--arc-border)] px-2.5 py-1 text-xs text-[var(--arc-text)] hover:border-[var(--arc-accent)] hover:text-[var(--arc-accent)]"
    >
      {text}
    </Link>
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
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/80 p-6 text-center">
      {next === null ? (
        <p className="text-lg text-[var(--arc-text)]">This was the last episode</p>
      ) : next.ready ? (
        <>
          <p className="text-lg text-[var(--arc-text)]">{`Next: Episode ${String(next.number)}`}</p>
          <Link
            to={`/watch/${String(next.id)}`}
            className="rounded-md bg-[var(--arc-accent)] px-4 py-2 text-sm font-medium text-[var(--arc-accent-contrast)] hover:opacity-90"
          >
            Play
          </Link>
        </>
      ) : (
        <p className="text-lg text-[var(--arc-text)]">
          {`Episode ${String(next.number)} isn’t ready yet`}
        </p>
      )}

      <div className="flex items-center gap-4 text-sm">
        <Link
          to={`/anime/${String(info.anime.id)}`}
          className="text-[var(--arc-accent)] hover:underline"
        >
          Back to show
        </Link>
        <button
          type="button"
          onClick={onDismiss}
          className="text-[var(--arc-text-muted)] hover:text-[var(--arc-text)]"
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
  const reporterRef = useRef<ProgressReporter | null>(null)
  /** A resume is a one-off: re-seeking on every metadata event would trap the viewer. */
  const resumedRef = useRef(false)

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

  const serverDuration = data?.duration ?? 0
  const { mutate: reportProgress } = useReportProgress()
  const markWatched = useMarkWatched()

  const report = useCallback(
    (position: number, duration: number) => {
      reportProgress(
        { episode_id: id, position_s: position, duration_s: duration },
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
          return
        case 'ArrowRight':
          event.preventDefault()
          video.currentTime = video.currentTime + SEEK_STEP
          return
        case 'f':
        case 'F':
          event.preventDefault()
          toggleFullscreen()
          return
        case 'm':
        case 'M':
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
  }, [toggleFullscreen])

  if (isPending) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p role="status" className="text-sm text-[var(--arc-text-muted)]">
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
      <div className="mx-auto flex min-h-screen max-w-lg flex-col items-center justify-center p-6 text-center">
        <h1 className="text-xl font-semibold text-[var(--arc-text)]">{LOAD_FAILED_TITLE}</h1>
        <ErrorState
          className="mt-2"
          message={LOAD_FAILED_BODY}
          pending={isFetching}
          onRetry={() => {
            void refetch()
          }}
        />
        <Link to="/" className="mt-6 text-sm text-[var(--arc-accent)] hover:underline">
          Back to home
        </Link>
      </div>
    )
  }

  const info: PlayInfo = data
  const { anime, episode, previous, next } = info
  const episodeLabel = `Episode ${String(episode.number)}`
  const progressWarning = progressFailures >= PROGRESS_FAILURE_LIMIT && !progressWarningDismissed

  function onReady(mediaDuration: number) {
    const video = videoRef.current
    if (video === null) return

    // A second `loadedmetadata` means the stream was rebuilt — a Retry after a
    // fatal error. The fresh media starts at zero, which would throw away the
    // half hour the viewer had got through, so it goes back to the last
    // position the reporter saw. No toast: nothing was resumed from a stored
    // position, the page merely refused to lose its place.
    if (resumedRef.current) {
      const last = reporterRef.current?.lastPosition ?? null
      if (last !== null && last > 0) video.currentTime = last
      return
    }
    resumedRef.current = true

    const position = info.resume_position
    const duration = resolveDuration(mediaDuration, serverDuration)
    if (!shouldResume(position, duration) || position === null) return

    video.currentTime = position
    setResumedAt(position)
  }

  return (
    <div className="flex min-h-screen flex-col bg-[var(--arc-bg)]">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-[var(--arc-border)] px-4 py-3">
        <Link
          to={`/anime/${String(anime.id)}`}
          aria-label="Back to show"
          className="rounded-md px-2 py-1 text-lg text-[var(--arc-text-muted)] hover:text-[var(--arc-text)]"
        >
          ←
        </Link>
        <h1 className="min-w-0 truncate text-base font-semibold text-[var(--arc-text)]">
          {anime.title.preferred}
        </h1>
        <p className="text-sm text-[var(--arc-text-muted)] tabular-nums">{episodeLabel}</p>
        <nav aria-label="Episodes" className="ml-auto flex items-center gap-2">
          <NeighbourLink episode={previous} label="Previous" />
          <NeighbourLink episode={next} label="Next" />
        </nav>
      </header>

      {/*
        Deliberately a strip under the header and not a dialog: nothing about
        the episode has stopped working, so playback carries on and the viewer
        decides whether to care. `status` rather than `alert` for the same
        reason — it is worth reading, not worth interrupting.
      */}
      {progressWarning ? (
        <div
          role="status"
          className="mx-4 mt-3 flex items-center gap-3 self-start rounded-md border border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10 px-3 py-1.5 text-xs text-[var(--arc-warn)]"
        >
          <span>{PROGRESS_UNSAVED}</span>
          <button
            type="button"
            onClick={() => {
              setProgressWarningDismissed(true)
            }}
            className="text-[var(--arc-warn)] underline-offset-2 hover:underline"
          >
            Dismiss
          </button>
        </div>
      ) : null}

      {resumedAt === null ? null : (
        <div
          role="status"
          className="mx-4 mt-3 flex items-center gap-3 self-start rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-1.5 text-xs text-[var(--arc-text-muted)]"
        >
          <span>{resumeLabel(resumedAt)}</span>
          <button
            type="button"
            onClick={() => {
              setResumedAt(null)
            }}
            className="text-[var(--arc-text-muted)] hover:text-[var(--arc-text)]"
          >
            Dismiss
          </button>
        </div>
      )}

      <div ref={containerRef} className="relative mt-3 bg-black">
        <HlsVideo
          src={info.playlist_url}
          videoRef={videoRef}
          label={`${anime.title.preferred} — ${episodeLabel}`}
          onReady={onReady}
          onTimeUpdate={(position, duration) => {
            reporterRef.current?.update(position, resolveDuration(duration, serverDuration))
          }}
          onPlay={(position, duration) => {
            reporterRef.current?.play(position, resolveDuration(duration, serverDuration))
          }}
          onPause={(position, duration) => {
            reporterRef.current?.pause(position, resolveDuration(duration, serverDuration))
          }}
          onSeeked={(position, duration) => {
            reporterRef.current?.seek(position, resolveDuration(duration, serverDuration))
          }}
          onEnded={(position, duration) => {
            reporterRef.current?.end(position, resolveDuration(duration, serverDuration))
            setFinished(true)
          }}
        />
        {finished ? (
          <EndOverlay
            info={info}
            onDismiss={() => {
              setFinished(false)
            }}
          />
        ) : null}
      </div>

      <footer className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 text-xs text-[var(--arc-text-muted)]">
        <span className="tabular-nums">{`Length ${formatClock(serverDuration)}`}</span>
        {episode.watched || markWatched.isSuccess ? (
          <span className="text-[var(--arc-ok)]">Watched ✓</span>
        ) : (
          <button
            type="button"
            disabled={markWatched.isPending}
            onClick={() => {
              markWatched.mutate({ episodeId: id, animeId: anime.id })
            }}
            className="text-[var(--arc-accent)] hover:underline disabled:opacity-60"
          >
            Mark watched
          </button>
        )}
        {markWatched.isError ? (
          <span role="alert" className="text-[var(--arc-error)]">
            {MARK_WATCHED_FAILED}
          </span>
        ) : null}
        <span className="ml-auto">Space play/pause · ← → 5s · F fullscreen · M mute</span>
      </footer>
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
