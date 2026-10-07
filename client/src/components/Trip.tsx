import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  buttonClass,
  cx,
  FIELD_ERROR_CLASS,
  FOCUS_RING,
  inputClass,
  LABEL_CLASS,
} from '@/components/ui'
import { ChoosePackButton } from '@/components/ReleaseSheet'
import type { AnimeDetail } from '@/lib/anime'
import { formatSize } from '@/lib/review'
import {
  askAgainErrorMessage,
  onDeviceCount,
  tripCandidates,
  tripCopyUrl,
  tripCountMax,
  tripErrorMessage,
  tripEstimateLabel,
  tripRangeLabel,
  tripRefusal,
  tripRoomFor,
  tripRowStatus,
  useAskAgain,
  useCancelTrip,
  useCreateTrip,
  useCurrentTrip,
  useStorageRoom,
  type StorageRoom,
  type Trip,
  type TripEpisode,
  type TripRowStatus,
} from '@/lib/trips'
import { downloads, MESSAGES } from '@/offline/downloads'
import { deviceName } from '@/offline/opfs'
import { useDownloads } from '@/offline/useDownloads'

/**
 * "Prepare for a trip" and the trip it makes (spec FR-A12, FR-S9, M19 T6).
 *
 * `TripControl` sits with the show's other actions under the hero: a chip that
 * opens an inline card — how many episodes (1 up to the cap or to what has
 * aired after the viewer's progress, whichever is fewer), which they are, a
 * labelled size estimate beside what the browser reports free, and the one
 * action. When the browser reports room for fewer episodes than the cap, the
 * card says so — as the browser's estimate, since on iPadOS it can report
 * room the iPad does not have (owner, 2026-10-07) — and the stepper stops
 * there; with no room at all the action is off. A refusal is said in a sentence;
 * "another trip is active" links to that show.
 *
 * `TripPanel` replaces it while the viewer's trip on this show is active: per
 * episode where it stands (the device's own record first, else the server's
 * phase), "Ask again" for an expired episode or one no longer on this device,
 * and Cancel trip behind an in-page confirmation — never `confirm()`.
 *
 * Neither is rendered for the demo account, nor where the browser cannot keep
 * files or play the copies; the page decides that and says it in one line.
 */

const TRIP_LABEL = 'Prepare for a trip'
const TRIP_LEDE =
  'Arc makes small copies of the next aired episodes for this device to keep, so you can watch with no connection. Keep Arc open while they download.'
const OPEN_NOTE =
  'Downloads run while Arc is open and on screen. Episodes reach this device as the server finishes each one.'
const CANCEL_NOTE =
  'Episodes already on this device stay. Arc stops fetching the rest and deletes the copies it made for this trip.'
const DEFAULT_COUNT = 10

/** The stepper: − value +, the value typed or stepped, always within 1..max. */
function Stepper({
  value,
  max,
  onChange,
}: {
  value: number
  max: number
  onChange: (value: number) => void
}) {
  const clamp = (next: number) => Math.max(1, Math.min(max, Math.round(next)))
  const step = cx(
    buttonClass('chip', 'h-11 w-11 justify-center px-0 text-[18px]'),
    'disabled:opacity-40',
  )
  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        aria-label="Fewer episodes"
        disabled={value <= 1}
        className={step}
        onClick={() => {
          onChange(clamp(value - 1))
        }}
      >
        −
      </button>
      <input
        id="trip-count"
        type="number"
        inputMode="numeric"
        min={1}
        max={max}
        value={value}
        className={inputClass(
          'h-11 w-[4.5rem] [appearance:textfield] text-center tabular-nums [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none',
        )}
        onChange={(event) => {
          const typed = Number(event.target.value)
          if (Number.isFinite(typed) && event.target.value !== '') onChange(clamp(typed))
        }}
      />
      <button
        type="button"
        aria-label="More episodes"
        disabled={value >= max}
        className={step}
        onClick={() => {
          onChange(clamp(value + 1))
        }}
      >
        +
      </button>
    </div>
  )
}

/** The chip under the hero, and the card it opens. */
export function TripControl({ anime }: { anime: AnimeDetail }) {
  const [open, setOpen] = useState(false)
  const candidates = tripCandidates(anime)
  const max = tripCountMax(anime)
  const [count, setCount] = useState(() => Math.max(1, Math.min(max, DEFAULT_COUNT)))
  const create = useCreateTrip(anime.id)
  // The shell's auto-keep hook keeps this answer fresh; it says whether
  // another show already holds the one trip a viewer may have.
  const { data: current } = useCurrentTrip(true)
  const { data: room } = useStorageRoom(open)

  if (max === 0) return null
  const roomFor = tripRoomFor(room ?? null)
  const cap = roomFor === null ? max : Math.max(1, Math.min(max, roomFor))
  const chosen = Math.max(1, Math.min(cap, count))
  const noRoom = roomFor === 0
  const elsewhere =
    current !== undefined && current !== null && current.anime_id !== anime.id ? current : null
  const refusal = tripRefusal(create.error)

  return (
    <>
      <button
        type="button"
        aria-expanded={open}
        aria-controls="trip-card"
        className={buttonClass('secondary')}
        onClick={() => {
          if (!open) create.reset()
          setOpen(!open)
        }}
      >
        <SuitcaseGlyph />
        {TRIP_LABEL}
      </button>

      {open ? (
        <form
          id="trip-card"
          aria-label={TRIP_LABEL}
          className="rounded-card order-last flex w-full flex-col gap-3.5 border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] p-4 sm:max-w-[460px] sm:p-5"
          onSubmit={(event) => {
            event.preventDefault()
            create.mutate(chosen, {
              onSuccess: () => {
                setOpen(false)
              },
            })
          }}
        >
          <div>
            <h3 className="text-[16px] font-semibold text-[var(--arc-text)]">{TRIP_LABEL}</h3>
            <p className="mt-1 text-[13px] leading-[1.5] text-[var(--arc-text-muted)]">
              {TRIP_LEDE}
            </p>
          </div>

          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="trip-count" className={LABEL_CLASS}>
                Episodes
              </label>
              <Stepper value={chosen} max={cap} onChange={setCount} />
            </div>
            <p className="text-right text-[14px] leading-[1.45] text-[var(--arc-text)] tabular-nums">
              <span data-testid="trip-range">{tripRangeLabel(candidates, chosen)}</span>
              <br />
              <span className="text-[13px] text-[var(--arc-text-muted)]">
                {`About ${tripEstimateLabel(chosen)} (estimate)`}
                {room === undefined || room === null || roomFor === null
                  ? null
                  : ` · the browser reports ${formatSize(Math.max(0, room.quota - room.usage))} free`}
              </span>
            </p>
          </div>

          {room !== undefined && room !== null && roomFor !== null && roomFor < max ? (
            <p data-testid="trip-room" className="text-[13px] leading-[1.5] text-[var(--arc-text)]">
              {roomSentence(roomFor, room)}
            </p>
          ) : null}

          {elsewhere === null ? null : (
            <p className="text-[13px] leading-[1.5] text-[var(--arc-text-muted)]">
              {'You have a trip for '}
              <Link
                to={`/anime/${String(elsewhere.anime_id)}`}
                className={cx('text-[var(--arc-text)] underline underline-offset-2', FOCUS_RING)}
              >
                {elsewhere.anime_title}
              </Link>
              {' already. One trip at a time — cancel it there to start this one.'}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2.5">
            <button
              type="submit"
              disabled={create.isPending || elsewhere !== null || noRoom}
              className={buttonClass('primary', 'px-5 text-[14px]')}
            >
              {create.isPending
                ? 'Starting…'
                : `Prepare ${String(chosen)} episode${chosen === 1 ? '' : 's'}`}
            </button>
            <button
              type="button"
              className={buttonClass('chip', 'px-4 text-[13px]')}
              onClick={() => {
                setOpen(false)
              }}
            >
              Not now
            </button>
          </div>

          {create.isError ? (
            <p role="alert" className={FIELD_ERROR_CLASS}>
              {tripErrorMessage(create.error)}
              {refusal === 'trip_active' &&
              elsewhere === null &&
              current !== undefined &&
              current !== null ? (
                <>
                  {' '}
                  <Link
                    to={`/anime/${String(current.anime_id)}`}
                    className={cx('underline underline-offset-2', FOCUS_RING)}
                  >
                    {`Go to ${current.anime_title}`}
                  </Link>
                </>
              ) : null}
            </p>
          ) : null}
        </form>
      ) : null}
    </>
  )
}

/** What the browser reports, said as its estimate (owner, 2026-10-07). */
function roomSentence(roomFor: number, room: StorageRoom): string {
  const device = deviceName()
  const figures = `${formatSize(Math.max(0, room.quota - room.usage))} free of ${formatSize(room.quota)}`
  if (roomFor === 0) {
    return `The browser reports no room for another episode on this ${device} (${figures}). Remove something from Downloads first.`
  }
  return `The browser reports room for about ${String(roomFor)} episode${roomFor === 1 ? '' : 's'} on this ${device} (${figures}). It is an estimate: the ${device}'s own storage can fill up first.`
}

function SuitcaseGlyph() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="h-[17px] w-[17px] fill-none stroke-current"
    >
      <rect x="3.5" y="7.5" width="17" height="12" rx="2.5" strokeWidth="1.7" />
      <path d="M9 7.5V5.8A1.3 1.3 0 0 1 10.3 4.5h3.4A1.3 1.3 0 0 1 15 5.8v1.7" strokeWidth="1.7" />
    </svg>
  )
}

const TONE: Record<TripRowStatus['tone'], string> = {
  muted: 'text-[var(--arc-text-muted)]',
  bright: 'text-[var(--arc-text)]',
  ok: 'text-[var(--arc-ok)]',
  error: 'text-[var(--arc-error)]',
}

function dateOf(iso: string): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  return at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

function TripRow({
  trip,
  episode,
  status,
}: {
  trip: Trip
  episode: TripEpisode
  status: TripRowStatus
}) {
  const again = useAskAgain()
  return (
    <li className="flex min-h-11 flex-wrap items-center gap-x-4 gap-y-1 border-b-[0.5px] border-[var(--arc-border)] py-1.5">
      <span className="w-[5.5rem] shrink-0 text-[14px] font-medium text-[var(--arc-text)] tabular-nums">
        {`Episode ${String(episode.number)}`}
      </span>
      <span className="flex min-w-0 flex-1 items-center gap-3">
        <span className={cx('truncate text-[13px] tabular-nums', TONE[status.tone])}>
          {status.label}
        </span>
        {status.percent === null ? null : (
          <span
            role="progressbar"
            aria-label={`Episode ${String(episode.number)}`}
            aria-valuenow={status.percent}
            aria-valuemin={0}
            aria-valuemax={100}
            className="hidden h-[2px] w-16 shrink-0 overflow-hidden rounded-full bg-[rgba(255,255,255,0.16)] sm:block"
          >
            <span
              className="block h-full bg-[var(--arc-text-muted)]"
              style={{ width: `${String(status.percent)}%` }}
            />
          </span>
        )}
      </span>
      {status.askAgain ? (
        <button
          type="button"
          disabled={again.isPending}
          aria-label={`Ask again for episode ${String(episode.number)}`}
          className={buttonClass('chip', 'h-9 px-3.5 text-[13px]')}
          onClick={() => {
            const manager = downloads()
            manager.forgetDecline(trip.id, episode.episode_id)
            if (episode.phase === 'delivered' || episode.phase === 'expired') {
              again.mutate({
                tripId: trip.id,
                animeId: trip.anime_id,
                episodeId: episode.episode_id,
              })
            } else {
              const url = tripCopyUrl(episode)
              if (url !== null) {
                manager
                  .keepTripCopy({ episodeId: episode.episode_id, tripId: trip.id, url })
                  .catch(() => undefined)
              }
            }
          }}
        >
          {again.isPending ? 'Asking…' : 'Ask again'}
        </button>
      ) : null}
      {again.isError ? (
        <span role="alert" className="w-full text-[13px] text-[var(--arc-error)]">
          {askAgainErrorMessage(again.error)}
        </span>
      ) : null}
    </li>
  )
}

/** The viewer's active trip on this show. */
export function TripPanel({ trip }: { trip: Trip }) {
  const records = useDownloads()
  const manager = downloads()
  const cancel = useCancelTrip()
  const [confirming, setConfirming] = useState(false)
  const held = onDeviceCount(trip, records)
  // A trip download paused because the device is full holds the whole queue:
  // said at the top of the panel, with the way to make room.
  const outOfSpace = trip.episodes.some((episode) => {
    const record = records[episode.episode_id]
    return record?.state === 'paused' && record.reason === 'quota'
  })
  const range =
    trip.first_number === trip.last_number
      ? `Episode ${String(trip.first_number)}`
      : `Episodes ${String(trip.first_number)}–${String(trip.last_number)}`

  return (
    <section
      aria-labelledby="trip-heading"
      className="rounded-card mt-10 border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2
            id="trip-heading"
            className="flex items-center gap-2 text-[18px] font-semibold tracking-[-0.01em] text-[var(--arc-text)]"
          >
            <SuitcaseGlyph />
            {`Trip · ${range}`}
          </h2>
          <p className="mt-1 text-[13px] text-[var(--arc-text-muted)] tabular-nums">
            {`${String(held)} of ${String(trip.count)} on this device`}
            {dateOf(trip.deadline_at) === ''
              ? null
              : ` · the server keeps each copy until this device has it, at most until ${dateOf(trip.deadline_at)}`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* FR-A13: point the trip's pending episodes at a pack of your choosing. */}
          <ChoosePackButton tripId={trip.id} animeId={trip.anime_id} />
          <button
            type="button"
            aria-expanded={confirming}
            className={buttonClass('chip', 'h-9 px-4 text-[13px]')}
            onClick={() => {
              cancel.reset()
              setConfirming(!confirming)
            }}
          >
            Cancel trip
          </button>
        </div>
      </div>

      {outOfSpace ? (
        <p
          role="alert"
          className="mt-3 rounded-card border-[0.5px] border-[var(--arc-border-strong)] bg-[var(--arc-bg)] p-3 text-[14px] leading-[1.5] text-[var(--arc-error)]"
        >
          {MESSAGES.quota}{' '}
          <Link
            to="/downloads"
            className={cx('text-[var(--arc-text)] underline underline-offset-2', FOCUS_RING)}
          >
            Go to Downloads
          </Link>
        </p>
      ) : null}

      {confirming ? (
        <div
          role="group"
          aria-label="Cancel this trip?"
          className="mt-3 rounded-card border-[0.5px] border-[var(--arc-border-strong)] bg-[var(--arc-bg)] p-4"
        >
          <p className="text-[14px] text-[var(--arc-text)]">Cancel this trip?</p>
          <p className="mt-1 text-[13px] leading-[1.5] text-[var(--arc-text-muted)]">
            {CANCEL_NOTE}
          </p>
          <div className="mt-3 flex flex-wrap gap-2.5">
            <button
              type="button"
              disabled={cancel.isPending}
              className={buttonClass('danger')}
              onClick={() => {
                cancel.mutate(
                  { tripId: trip.id, animeId: trip.anime_id },
                  {
                    onSuccess: () => {
                      setConfirming(false)
                    },
                  },
                )
              }}
            >
              {cancel.isPending ? 'Cancelling…' : 'Cancel trip'}
            </button>
            <button
              type="button"
              className={buttonClass('chip')}
              onClick={() => {
                setConfirming(false)
              }}
            >
              Keep the trip
            </button>
          </div>
          {cancel.isError ? (
            <p role="alert" className={cx(FIELD_ERROR_CLASS, 'mt-2')}>
              Could not cancel the trip. Try again.
            </p>
          ) : null}
        </div>
      ) : null}

      <ul className="mt-3 grid gap-x-10 md:grid-cols-2">
        {trip.episodes.map((episode) => (
          <TripRow
            key={episode.episode_id}
            trip={trip}
            episode={episode}
            status={tripRowStatus(
              episode,
              records[episode.episode_id],
              manager.isDeclined(trip.id, episode.episode_id),
              formatSize,
            )}
          />
        ))}
      </ul>

      <p className="mt-3 text-[13px] leading-[1.5] text-[var(--arc-text-muted)]">{OPEN_NOTE}</p>
    </section>
  )
}
