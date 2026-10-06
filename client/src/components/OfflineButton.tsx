import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cx, FOCUS_RING } from '@/components/ui'
import type { EpisodeOut } from '@/lib/anime'
import { formatSize } from '@/lib/review'
import { PHASE_LABEL, type TripPhase } from '@/lib/trips'
import { COPY_LABEL, downloads, SCREEN_NOTE, type DownloadRecord } from '@/offline/downloads'
import { canDownloadInApp } from '@/offline/opfs'
import { useDownload } from '@/offline/useDownloads'

/**
 * "Keep offline" as one icon button (spec FR-S9, owner 2026-10-05), the same
 * control on a Show page row and in the player's top bar.
 *
 * | state | glyph | a tap |
 * |---|---|---|
 * | no record | download arrow | starts the download |
 * | `preparing` | arrow inside a dotted ring, filling with the server's percent when known | stops waiting (removes the wish) |
 * | `queued` | arrow inside a dotted ring | pauses it |
 * | `downloading` | pause bars inside a ring that fills | pauses it |
 * | `paused` | arrow inside the ring, dimmed, where it stopped | resumes it |
 * | `failed` | arrow with a small warning mark | tries again |
 * | `downloaded` | a filled disc with a check | opens the small menu |
 *
 * The menu is in the page, never `confirm()`: which copy is on the device and
 * its size ("Smaller copy for this device · 210 MB" or "Full-size copy ·
 * 700 MB", M19), "Remove from this device", and the Downloads page. A
 * full-size copy already on the device stays as it is: there is no "replace
 * with the smaller copy" (one record per episode cannot hold both while the
 * new one downloads). While the player is playing *this
 * very copy* the remove is disabled with a sentence saying why — the bytes the
 * `<video>` is reading would go out from under it.
 *
 * **A trip's episode** (M19 T6, FR-A12) that is not ready — trip-only, made
 * only as a small copy for the viewer's device — is driven by the trip's
 * phase until the device has a record of it: `searching` / `downloading` /
 * `waiting_space` → a dotted ring that only says the server is on it (the
 * server's percent drawn while it downloads); `preparing` → the preparing
 * view, the server's percent, nothing to tap (the trip is cancelled as a
 * whole); `available` → the download arrow, whose tap keeps it now rather
 * than at the auto-keep hook's next pass. Once there is a record, the record
 * decides, as for any episode.
 *
 * Renders nothing for an episode that is not ready and not the viewer's trip
 * episode, has no file route, or on a browser with no OPFS or no workers. The
 * demo account is the caller's to rule out (it does not render this at all).
 */

export type OfflineButtonVariant = 'row' | 'player'

const SHOW_EPISODE_NOTE = 'Your progress stays, and Arc still has the episode.'
const PLAYING_THIS_COPY = 'Playing from this copy. Remove it after you leave the player.'
const START_FAILED = 'Could not start the download. Tap to try again.'
const TRIP_PREPARING_NOTE =
  'Arc is making a small copy of this episode for your trip. It downloads to this device by itself once it is ready, while Arc is open.'
const PREPARING_NOTE =
  'Arc is making a smaller copy of this episode for your device. It downloads by itself when it is ready. Tap to stop waiting.'

/**
 * A whole percentage; 0 until the size is known. While the server is making
 * the copy it is the server's own figure.
 */
function offlinePercent(
  record: Pick<DownloadRecord, 'bytes' | 'total' | 'state' | 'serverProgress'>,
): number {
  if (record.state === 'preparing') {
    const progress = record.serverProgress ?? 0
    return Math.max(0, Math.min(100, Math.floor(progress * 100)))
  }
  return record.total > 0 ? Math.min(100, Math.floor((record.bytes / record.total) * 100)) : 0
}

/** Whether the server has said how far it has got with a `preparing` copy. */
function serverPercentKnown(record: DownloadRecord | undefined): boolean {
  return (
    record?.state === 'preparing' &&
    record.serverProgress !== null &&
    record.serverProgress !== undefined
  )
}

type OfflineView =
  /** A trip episode the server is still fetching (no record on the device yet). */
  | 'server'
  | 'none'
  | 'start-failed'
  | 'preparing'
  | 'queued'
  | 'downloading'
  | 'paused'
  | 'failed'
  | 'downloaded'

/** What the viewer's trip says about this episode, when it holds it (M19). */
export interface TripRowInfo {
  id: number
  phase: TripPhase
  progress: number | null
  /** The copy's URL from the server while it can be downloaded; null until then. */
  url: string | null
}

/**
 * The view, or null for nothing to show: a non-ready episode outside the
 * viewer's trip, and a trip episode the server is done with that this device
 * does not hold (the trip panel offers "Ask again" for those).
 */
function offlineView(
  record: DownloadRecord | undefined,
  startFailed: boolean,
  ready: boolean,
  trip: TripRowInfo | null,
): OfflineView | null {
  if (record !== undefined) {
    return ready || trip !== null || record.tripId !== undefined ? record.state : null
  }
  if (ready) return startFailed ? 'start-failed' : 'none'
  switch (trip?.phase) {
    case 'available':
      // No URL from the server yet: nothing to download, the server is on it.
      if (trip.url === null) return 'server'
      return startFailed ? 'start-failed' : 'none'
    case 'preparing':
      return 'preparing'
    case 'searching':
    case 'downloading':
    case 'waiting_space':
      return 'server'
    default:
      return null
  }
}

/** The control's accessible name in each state. */
function offlineLabel(
  view: OfflineView,
  number: number,
  percent: number,
  percentKnown = true,
): string {
  const episode = `episode ${String(number)}`
  switch (view) {
    case 'server':
      return `Episode ${String(number)} is on its way to the server for your trip`
    case 'none':
      return `Keep ${episode} offline`
    case 'start-failed':
      return `Could not start keeping ${episode} offline — try again`
    case 'preparing':
      return percentKnown
        ? `Episode ${String(number)} is being prepared on the server, ${String(percent)}% — cancel`
        : `Episode ${String(number)} is being prepared on the server — cancel`
    case 'queued':
      return `Episode ${String(number)} is queued to download — pause`
    case 'downloading':
      return `Downloading ${episode}, ${String(percent)}% — pause`
    case 'paused':
      return `Download of ${episode} paused at ${String(percent)}% — resume`
    case 'failed':
      return `Download of ${episode} stopped — try again`
    case 'downloaded':
      return `Episode ${String(number)} is on this device`
  }
}

/** What the live region says: one line per *state*, never per percent. */
function announcement(view: OfflineView, number: number): string {
  const episode = `Episode ${String(number)}`
  switch (view) {
    case 'server':
      return `${episode} is on its way to the server.`
    case 'preparing':
      return `${episode} is being prepared on the server.`
    case 'queued':
      return `${episode} is queued to download.`
    case 'downloading':
      return `Downloading ${episode.toLowerCase()}.`
    case 'paused':
      return `Download of ${episode.toLowerCase()} paused.`
    case 'failed':
    case 'start-failed':
      return `Download of ${episode.toLowerCase()} stopped.`
    case 'downloaded':
      return `${episode} is on this device.`
    case 'none':
      return ''
  }
}

/** The short word beside the icon in the player, on a wide screen. */
function shortLabel(view: OfflineView, percent: number): string {
  switch (view) {
    case 'server':
      return 'On the server'
    case 'none':
      return 'Download'
    case 'preparing':
      return 'Preparing'
    case 'queued':
      return 'Queued'
    case 'downloading':
      return `${String(percent)}%`
    case 'paused':
      return 'Paused'
    case 'failed':
    case 'start-failed':
      return 'Retry'
    case 'downloaded':
      return 'On this device'
  }
}

/* --- Glyphs: inline SVG, `currentColor`, aria-hidden ------------------- */

const GLYPH = 'h-[18px] w-[18px]'

function DownloadGlyph() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={cx(GLYPH, 'fill-none stroke-current')}>
      <path
        d="M12 4.5v10.5M7.5 10.5 12 15l4.5-4.5M5.5 19h13"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function PauseGlyph() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-[13px] w-[13px] fill-current">
      <rect x="6" y="4.5" width="4" height="15" rx="1.2" />
      <rect x="14" y="4.5" width="4" height="15" rx="1.2" />
    </svg>
  )
}

/** A filled disc with the check cut out of it: done, and it says so. */
function OnDeviceGlyph() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5">
      <circle cx="12" cy="12" r="10" className="fill-current" />
      <path
        d="M7.5 12.4 10.6 15.5 16.5 9"
        className="fill-none stroke-[var(--arc-bg)]"
        strokeWidth="2.1"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function WarningMark() {
  return (
    <span
      aria-hidden="true"
      className="absolute top-[5px] right-[5px] flex h-[13px] w-[13px] items-center justify-center rounded-full bg-[var(--arc-error)] text-[9px] leading-none font-bold text-[var(--arc-bg)]"
    >
      !
    </span>
  )
}

/**
 * The ring around the glyph. Determinate while downloading; dotted and empty
 * while queued; held where it stopped (dimmed) while paused. While the server
 * prepares the copy it is dotted, with the server's percent drawn over the
 * dots, dimmed, once the server has given one (indeterminate until then).
 */
function Ring({
  percent,
  kind,
  label,
  known = true,
}: {
  percent: number
  kind: OfflineView
  label: string
  known?: boolean
}) {
  const radius = 20
  const circumference = 2 * Math.PI * radius
  const dotted = kind === 'queued' || kind === 'preparing' || kind === 'server'
  const filled = kind === 'preparing' ? known : !dotted
  return (
    <svg
      viewBox="0 0 44 44"
      role="progressbar"
      aria-label={label}
      aria-valuenow={known ? percent : undefined}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuetext={known ? `${String(percent)}%` : undefined}
      data-percent={known ? percent : undefined}
      className="pointer-events-none absolute inset-0 h-full w-full -rotate-90"
    >
      <circle
        cx="22"
        cy="22"
        r={radius}
        className="fill-none stroke-[rgba(255,255,255,0.16)]"
        strokeWidth="2.25"
        strokeDasharray={dotted ? '1.5 4.5' : undefined}
        strokeLinecap="round"
      />
      {filled ? (
        <circle
          cx="22"
          cy="22"
          r={radius}
          className={cx(
            'fill-none transition-[stroke-dashoffset] duration-500 ease-out motion-reduce:transition-none',
            kind === 'paused' || kind === 'preparing' || kind === 'server'
              ? 'stroke-[var(--arc-text-muted)]'
              : 'stroke-[var(--arc-text)]',
          )}
          strokeWidth="2.25"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - percent / 100)}
        />
      ) : null}
    </svg>
  )
}

/* --- Clothes ------------------------------------------------------------ */

/** The Show row: the app's chip, as a 44px circle, so it sits level with Watched. */
const ROW_BUTTON =
  'h-11 w-11 border-[var(--arc-border)] bg-[rgba(255,255,255,0.05)] text-[var(--arc-text-muted)] hover:bg-[rgba(255,255,255,0.1)] hover:text-[var(--arc-text)]'

/**
 * The player: the back button's dark glass and its sizes (44 on a phone and
 * under a finger, 36 under a mouse at `md`), so the two ends of the top bar
 * match. On a wide screen the button grows a short word beside the glyph.
 */
const PLAYER_BUTTON = cx(
  'h-11 min-w-11 md:h-9 md:min-w-9 pointer-coarse:md:h-11 pointer-coarse:md:min-w-11',
  'border-[rgba(255,255,255,0.2)] bg-[rgba(18,23,34,0.6)] text-white backdrop-blur-glass hover:bg-[rgba(18,23,34,0.78)]',
)

const PLAYER_ICON_BOX = 'h-11 w-11 md:h-9 md:w-9 pointer-coarse:md:h-11 pointer-coarse:md:w-11'

export interface OfflineButtonProps {
  episode: Pick<EpisodeOut, 'id' | 'number' | 'state' | 'download_url'>
  variant?: OfflineButtonVariant
  /** The player is playing this episode *from the device*: removing it is off. */
  playingFromDevice?: boolean
  /** Told when the menu opens and closes, so the player can hold its chrome. */
  onMenuOpenChange?: (open: boolean) => void
  /** The viewer's trip holds this episode (M19): its phase drives the button until there is a record. */
  trip?: TripRowInfo | null
}

export function OfflineButton({
  episode,
  variant = 'row',
  playingFromDevice = false,
  onMenuOpenChange,
  trip = null,
}: OfflineButtonProps) {
  const record = useDownload(episode.id)
  const [startFailed, setStartFailed] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const wrapRef = useRef<HTMLSpanElement | null>(null)
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const menuId = useId()

  const href = episode.download_url ?? null
  const ready = episode.state === 'ready' && href !== null
  const view = offlineView(record, startFailed, ready, trip)
  const open = menuOpen && view === 'downloaded'

  // Tell the player, and only on a real change (it passes a state setter).
  useEffect(() => {
    onMenuOpenChange?.(open)
  }, [open, onMenuOpenChange])

  // A tap anywhere else, or Escape, puts the menu away.
  useEffect(() => {
    if (!open) return undefined
    function onPointerDown(event: PointerEvent) {
      if (!(event.target instanceof Node)) return
      if (wrapRef.current?.contains(event.target) === true) return
      setMenuOpen(false)
    }
    window.addEventListener('pointerdown', onPointerDown)
    return () => {
      window.removeEventListener('pointerdown', onPointerDown)
    }
  }, [open])

  if (view === null || !canDownloadInApp()) return null

  // With no record yet, a trip episode's ring is the server's own progress.
  const tripPercent =
    trip?.progress === null || trip?.progress === undefined
      ? null
      : Math.max(0, Math.min(100, Math.floor(trip.progress * 100)))
  const percent = record === undefined ? (tripPercent ?? 0) : offlinePercent(record)
  const known =
    record === undefined
      ? view !== 'preparing' && view !== 'server'
        ? true
        : tripPercent !== null
      : view !== 'preparing' || serverPercentKnown(record)
  /** Nothing the viewer can do here: the server is working for the trip. */
  const inert = record === undefined && (view === 'server' || view === 'preparing')
  const label =
    inert && view === 'preparing'
      ? `Episode ${String(episode.number)} is being prepared on the server for your trip${known ? `, ${String(percent)}%` : ''}`
      : offlineLabel(view, episode.number, percent, known)
  const player = variant === 'player'

  const title =
    view === 'server' && trip !== null
      ? `On the server: ${PHASE_LABEL[trip.phase]}. It downloads to this device by itself once it is ready, while Arc is open.`
      : inert
        ? TRIP_PREPARING_NOTE
        : view === 'none'
          ? 'Download into Arc to watch without a connection'
          : view === 'start-failed'
            ? START_FAILED
            : view === 'downloading'
              ? SCREEN_NOTE
              : view === 'preparing'
                ? PREPARING_NOTE
                : view === 'queued'
                  ? 'Waiting for the download ahead of it'
                  : view === 'downloaded'
                    ? 'On this device'
                    : (record?.message ?? undefined)

  function activate(): void {
    const manager = downloads()
    if (inert) return
    switch (view) {
      case 'none':
      case 'start-failed':
        setStartFailed(false)
        if (!ready && trip !== null && trip.url !== null) {
          // A trip-only episode: its copy, straight away (FR-A12).
          manager.forgetDecline(trip.id, episode.id)
          manager
            .keepTripCopy({ episodeId: episode.id, tripId: trip.id, url: trip.url })
            .catch(() => {
              setStartFailed(true)
            })
          return
        }
        if (href === null) return
        manager.start({ episodeId: episode.id, url: href }).catch(() => {
          setStartFailed(true)
        })
        return
      case 'preparing':
        // Stop waiting: the wish goes; the server keeps or expires its copy.
        void manager.remove(episode.id)
        return
      case 'queued':
      case 'downloading':
        manager.pause(episode.id)
        return
      case 'failed':
        // A trip copy that vanished or could not be made: the trip's copy, if
        // it is there now; anything else is tried again as usual.
        if (
          record?.tripId !== undefined &&
          trip?.phase === 'available' &&
          trip.url !== null &&
          (record.reason === 'gone' || record.reason === 'unprepared')
        ) {
          manager.adoptTrip(episode.id, trip.id, trip.url)
          return
        }
        manager.resume(episode.id)
        return
      case 'paused':
        manager.resume(episode.id)
        return
      case 'downloaded':
        setMenuOpen((was) => !was)
        return
      case 'server':
        return
    }
  }

  let glyph: ReactNode
  switch (view) {
    case 'downloading':
      glyph = <PauseGlyph />
      break
    case 'downloaded':
      glyph = <OnDeviceGlyph />
      break
    default:
      glyph = <DownloadGlyph />
  }
  const ringed =
    view === 'preparing' ||
    view === 'queued' ||
    view === 'downloading' ||
    view === 'paused' ||
    view === 'server'
  const warned = view === 'failed' || view === 'start-failed'

  const icon = (
    <span
      className={cx(
        'relative inline-flex shrink-0 items-center justify-center',
        player ? PLAYER_ICON_BOX : 'h-11 w-11',
        view === 'paused' && 'text-[var(--arc-text-muted)]',
        (view === 'queued' || view === 'preparing' || view === 'server') && 'opacity-80',
      )}
    >
      {glyph}
      {warned ? <WarningMark /> : null}
    </span>
  )

  return (
    // In a Show row the wrapper is *not* the menu's containing block: the
    // row's action group is, so the menu lines up with the row's right edge
    // and never runs off the left of a phone screen.
    <span ref={wrapRef} className={cx('inline-flex shrink-0', player && 'relative')}>
      <span className="relative inline-flex">
        <button
          ref={buttonRef}
          type="button"
          aria-label={label}
          title={title}
          aria-expanded={view === 'downloaded' ? open : undefined}
          aria-controls={view === 'downloaded' ? menuId : undefined}
          aria-disabled={inert ? true : undefined}
          data-state={view}
          onClick={activate}
          onKeyDown={(event) => {
            if (event.key === 'Escape' && open) {
              event.preventDefault()
              setMenuOpen(false)
            }
          }}
          className={cx(
            'relative inline-flex items-center justify-start rounded-full border-[0.5px] transition-colors duration-200',
            player ? PLAYER_BUTTON : ROW_BUTTON,
            view === 'downloaded' &&
              (player
                ? 'border-[rgba(255,255,255,0.32)]'
                : 'border-[var(--arc-border-strong)] bg-[var(--arc-surface-raised)] text-[var(--arc-text)]'),
            warned && 'text-[var(--arc-text)]',
            inert && 'cursor-default hover:bg-[rgba(255,255,255,0.05)]',
            // The ring is the edge while there is one; a hairline under it doubles it.
            // On a wide player screen the button is a pill with a word in it, so the
            // pill keeps its edge and the ring shrinks to sit round the glyph.
            ringed &&
              (player
                ? 'border-transparent lg:border-[rgba(255,255,255,0.2)]'
                : 'border-transparent'),
            player && 'lg:pr-3.5',
            FOCUS_RING,
          )}
        >
          {icon}
          {player ? (
            <span
              aria-hidden="true"
              className="hidden text-[13px] font-medium whitespace-nowrap tabular-nums lg:inline"
            >
              {shortLabel(view, percent)}
            </span>
          ) : null}
        </button>
        {ringed ? (
          <span
            className={cx(
              'pointer-events-none absolute top-0 left-0',
              player ? cx(PLAYER_ICON_BOX, 'lg:scale-[0.78]') : 'h-11 w-11',
            )}
          >
            <Ring
              percent={percent}
              kind={view}
              known={known}
              label={
                view === 'preparing'
                  ? `Preparing episode ${String(episode.number)} on the server`
                  : view === 'server'
                    ? `Episode ${String(episode.number)} on the server`
                    : `Download of episode ${String(episode.number)}`
              }
            />
          </span>
        ) : null}
      </span>
      <span aria-live="polite" className="sr-only">
        {announcement(view, episode.number)}
      </span>

      {open && record !== undefined ? (
        <div
          id={menuId}
          role="group"
          aria-label={`Episode ${String(episode.number)} on this device`}
          data-offline-menu=""
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault()
              setMenuOpen(false)
              buttonRef.current?.focus()
            }
          }}
          className={cx(
            'absolute top-full right-0 z-30 mt-2 w-[16.5rem] rounded-card border-[0.5px] p-1.5 text-left shadow-bar backdrop-blur-bar',
            'border-[var(--arc-border-strong)] bg-[rgba(18,23,34,0.98)]',
          )}
        >
          <p className="flex items-center gap-2 px-3 pt-2 pb-1.5 text-[13px] text-[var(--arc-text)]">
            <span className="text-[var(--arc-ok)]">
              <OnDeviceGlyph />
            </span>
            <span>
              {COPY_LABEL[record.variant]}
              <span className="text-[var(--arc-text-muted)] tabular-nums">
                {` · ${formatSize(record.bytes)}`}
              </span>
            </span>
          </p>
          <button
            type="button"
            disabled={playingFromDevice}
            aria-describedby={playingFromDevice ? `${menuId}-why` : undefined}
            onClick={() => {
              setMenuOpen(false)
              void downloads().remove(episode.id)
            }}
            className={cx(
              'flex min-h-11 w-full items-center rounded-[10px] px-3 text-[14px] text-[var(--arc-error)]',
              'hover:bg-[rgba(255,255,255,0.07)] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent',
              FOCUS_RING,
            )}
          >
            Remove from this device
          </button>
          <p
            id={`${menuId}-why`}
            className="px-3 pb-1.5 text-[12px] leading-snug text-[var(--arc-text-muted)]"
          >
            {playingFromDevice ? PLAYING_THIS_COPY : SHOW_EPISODE_NOTE}
          </p>
          <div className="mx-3 my-1 h-[0.5px] bg-[var(--arc-border)]" />
          <Link
            to="/downloads"
            className={cx(
              'flex min-h-11 w-full items-center rounded-[10px] px-3 text-[14px] text-[var(--arc-text)]',
              'hover:bg-[rgba(255,255,255,0.07)]',
              FOCUS_RING,
            )}
          >
            Go to Downloads
          </Link>
        </div>
      ) : null}
    </span>
  )
}

/** The reason a download stopped, as a short line, for wherever there is room. */
export function OfflineReason({ episodeId, className }: { episodeId: number; className?: string }) {
  const record = useDownload(episodeId)
  if (record === undefined || record.message === null) return null
  if (record.state !== 'paused' && record.state !== 'failed') return null
  return (
    <span
      role={record.state === 'failed' ? 'alert' : undefined}
      title={record.message}
      className={cx(
        record.state === 'failed' ? 'text-[var(--arc-error)]' : 'text-[var(--arc-text-muted)]',
        className,
      )}
    >
      {record.message}
    </span>
  )
}
