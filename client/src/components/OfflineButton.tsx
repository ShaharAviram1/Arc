import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cx, FOCUS_RING } from '@/components/ui'
import type { EpisodeOut } from '@/lib/anime'
import { formatSize } from '@/lib/review'
import { downloads, SCREEN_NOTE, type DownloadRecord } from '@/offline/downloads'
import { canDownloadInApp } from '@/offline/opfs'
import { useDownload } from '@/offline/useDownloads'

/**
 * "Keep offline" as one icon button (spec FR-S9, owner 2026-10-05), the same
 * control on a Show page row and in the player's top bar.
 *
 * | state | glyph | a tap |
 * |---|---|---|
 * | no record | download arrow | starts the download |
 * | `queued` | arrow inside a dotted ring | pauses it |
 * | `downloading` | pause bars inside a ring that fills | pauses it |
 * | `paused` | arrow inside the ring, dimmed, where it stopped | resumes it |
 * | `failed` | arrow with a small warning mark | tries again |
 * | `downloaded` | a filled disc with a check | opens the small menu |
 *
 * The menu is in the page, never `confirm()`: the size on the device, "Remove
 * from this device", and the Downloads page. While the player is playing *this
 * very copy* the remove is disabled with a sentence saying why — the bytes the
 * `<video>` is reading would go out from under it.
 *
 * Renders nothing for an episode that is not ready, has no file route, or on a
 * browser with no OPFS or no workers. The demo account is the caller's to rule
 * out (it does not render this at all).
 */

export type OfflineButtonVariant = 'row' | 'player'

const SHOW_EPISODE_NOTE = 'Your progress stays, and Arc still has the episode.'
const PLAYING_THIS_COPY = 'Playing from this copy. Remove it after you leave the player.'
const START_FAILED = 'Could not start the download. Tap to try again.'

/** A whole percentage; 0 until the size is known. */
function offlinePercent(record: Pick<DownloadRecord, 'bytes' | 'total'>): number {
  return record.total > 0 ? Math.min(100, Math.floor((record.bytes / record.total) * 100)) : 0
}

type OfflineView =
  'none' | 'start-failed' | 'queued' | 'downloading' | 'paused' | 'failed' | 'downloaded'

function offlineView(record: DownloadRecord | undefined, startFailed: boolean): OfflineView {
  if (record === undefined) return startFailed ? 'start-failed' : 'none'
  return record.state
}

/** The control's accessible name in each state. */
function offlineLabel(view: OfflineView, number: number, percent: number): string {
  const episode = `episode ${String(number)}`
  switch (view) {
    case 'none':
      return `Keep ${episode} offline`
    case 'start-failed':
      return `Could not start keeping ${episode} offline — try again`
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
    case 'none':
      return 'Download'
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
 * while queued; held where it stopped (dimmed) while paused.
 */
function Ring({ percent, kind, label }: { percent: number; kind: OfflineView; label: string }) {
  const radius = 20
  const circumference = 2 * Math.PI * radius
  const dotted = kind === 'queued'
  return (
    <svg
      viewBox="0 0 44 44"
      role="progressbar"
      aria-label={label}
      aria-valuenow={percent}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuetext={`${String(percent)}%`}
      data-percent={percent}
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
      {dotted ? null : (
        <circle
          cx="22"
          cy="22"
          r={radius}
          className={cx(
            'fill-none transition-[stroke-dashoffset] duration-500 ease-out motion-reduce:transition-none',
            kind === 'paused' ? 'stroke-[var(--arc-text-muted)]' : 'stroke-[var(--arc-text)]',
          )}
          strokeWidth="2.25"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - percent / 100)}
        />
      )}
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
}

export function OfflineButton({
  episode,
  variant = 'row',
  playingFromDevice = false,
  onMenuOpenChange,
}: OfflineButtonProps) {
  const record = useDownload(episode.id)
  const [startFailed, setStartFailed] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const wrapRef = useRef<HTMLSpanElement | null>(null)
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const menuId = useId()

  const view = offlineView(record, startFailed)
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

  const href = episode.download_url ?? null
  if (episode.state !== 'ready' || href === null || !canDownloadInApp()) return null

  const percent = record === undefined ? 0 : offlinePercent(record)
  const label = offlineLabel(view, episode.number, percent)
  const player = variant === 'player'

  const title =
    view === 'none'
      ? 'Download into Arc to watch without a connection'
      : view === 'start-failed'
        ? START_FAILED
        : view === 'downloading'
          ? SCREEN_NOTE
          : view === 'queued'
            ? 'Waiting for the download ahead of it'
            : view === 'downloaded'
              ? 'On this device'
              : (record?.message ?? undefined)

  function activate(): void {
    const manager = downloads()
    switch (view) {
      case 'none':
      case 'start-failed':
        if (href === null) return
        setStartFailed(false)
        manager.start({ episodeId: episode.id, url: href }).catch(() => {
          setStartFailed(true)
        })
        return
      case 'queued':
      case 'downloading':
        manager.pause(episode.id)
        return
      case 'paused':
      case 'failed':
        manager.resume(episode.id)
        return
      case 'downloaded':
        setMenuOpen((was) => !was)
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
  const ringed = view === 'queued' || view === 'downloading' || view === 'paused'
  const warned = view === 'failed' || view === 'start-failed'

  const icon = (
    <span
      className={cx(
        'relative inline-flex shrink-0 items-center justify-center',
        player ? PLAYER_ICON_BOX : 'h-11 w-11',
        view === 'paused' && 'text-[var(--arc-text-muted)]',
        view === 'queued' && 'opacity-80',
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
              label={`Download of episode ${String(episode.number)}`}
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
              On this device
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
