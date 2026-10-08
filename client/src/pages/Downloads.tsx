import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Artwork, buttonClass, Chip, cx, EmptyState, FOCUS_RING, RowGroup } from '@/components/ui'
import { useMe } from '@/lib/auth'
import { formatSize } from '@/lib/review'
import { useCancelTrip, useCurrentTrip } from '@/lib/trips'
import { coverUrl } from '@/offline/cache'
import {
  COPY_NOTE,
  downloads,
  SCREEN_NOTE,
  watchedNote,
  type DownloadRecord,
} from '@/offline/downloads'
import { canDownloadInApp, estimate, persisted } from '@/offline/opfs'
import { useDownloads, useRemoveWatched } from '@/offline/useDownloads'

/**
 * The episodes kept on this device (spec FR-S9, §5 page table).
 *
 * Everything on this page comes from the device — the download records, the
 * posters, the browser's storage figures — so it renders in full with no
 * network, which is the point: it is the page an offline launch sends the
 * viewer to.
 *
 * **Deleting confirms in the page, never with `confirm()`.** A native dialog
 * in an installed web app on iOS looks like the browser rather than Arc, and
 * puts the destructive button where the thumb rests. The confirmation says
 * what is *not* lost: the progress, and the server's copy.
 *
 * The storage card is the browser's own numbers and whether it has promised to
 * keep them (*persistent*): without that, iOS may evict a download under
 * storage pressure, and "my episode vanished" is a failure with no visible
 * cause. Arc asks for persistence on the first download.
 *
 * **A trip's episodes are one group** (M19, FR-A12): "Trip · <show> · N
 * episodes · <total>", how many are on the device and how many are still on
 * their way, the rows as ever, and — while the trip is the account's active
 * one and the server can be reached — Cancel trip, confirmed in the page. A
 * trip copy the server has not yet heard arrived says so quietly on its row.
 *
 * **Watched copies leave by themselves** (owner, 2026-10-08): the switch
 * "Remove episodes once watched" (on by default, per device) and a Keep
 * toggle per row that exempts one episode. A row whose completion the server
 * has accepted says "Watched · removing soon" until the next pass takes it;
 * a kept one says "Kept".
 */

const LEDE = 'Episodes kept inside Arc on this device. They play with no connection.'

const EMPTY_MESSAGE =
  'Nothing is downloaded to this device yet. Tap the download button beside a ready episode on a show page, or in the player.'

const UNSUPPORTED =
  'This browser can’t keep episodes inside Arc. They still stream whenever Arc is reachable.'

/** The notes at the bottom: each is something that has surprised somebody. */
const NOTES = [
  SCREEN_NOTE,
  'Removing Arc from your Home Screen deletes its downloads (iOS). Delete episodes here instead.',
  'A downloaded episode is your copy: it keeps playing here even after the server clears its own.',
  'Progress made offline is saved on this device and syncs to Arc and MyAnimeList when you reconnect.',
]

function percentOf(record: DownloadRecord): number {
  if (record.state === 'preparing') {
    return Math.max(0, Math.min(100, Math.floor((record.serverProgress ?? 0) * 100)))
  }
  return record.total > 0 ? Math.min(100, Math.floor((record.bytes / record.total) * 100)) : 0
}

const STATE_LABEL: Record<DownloadRecord['state'], string> = {
  preparing: 'Preparing on the server',
  queued: 'Queued',
  downloading: 'Downloading',
  paused: 'Paused',
  downloaded: 'On this device',
  failed: 'Stopped',
}

const REMOVE_WATCHED_HELP =
  'Once Arc has your watch of an episode, its copy leaves this device. Episodes you keep stay.'

function RemoveWatchedSwitch() {
  const on = useRemoveWatched()
  return (
    <label className="flex items-start gap-3 rounded-card border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] p-4">
      <input
        type="checkbox"
        role="switch"
        checked={on}
        aria-describedby="remove-watched-help"
        className="mt-0.5 h-5 w-5 shrink-0 accent-[var(--arc-focus)]"
        onChange={(event) => {
          downloads().setRemoveWatched(event.target.checked)
        }}
      />
      <span className="min-w-0">
        <span className="block text-[15px] font-medium text-[var(--arc-text)]">
          Remove episodes once watched
        </span>
        <span id="remove-watched-help" className="block text-[13px] text-[var(--arc-text-muted)]">
          {REMOVE_WATCHED_HELP}
        </span>
      </span>
    </label>
  )
}

/** A remembered poster, as a blob URL; `null` until read, or when there is none. */
function useCover(userId: number, animeId: number): string | null {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    void coverUrl(userId, animeId).then((found) => {
      if (!cancelled) setUrl(found)
    })
    return () => {
      cancelled = true
    }
  }, [userId, animeId])
  return url
}

function DownloadRow({
  record,
  confirming,
  onConfirm,
  onCancel,
  grouped = false,
}: {
  record: DownloadRecord
  /** In a trip's group, whose heading already names the show: the episode leads. */
  grouped?: boolean
  confirming: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const manager = downloads()
  const removeWatched = useRemoveWatched()
  const note = watchedNote(record, removeWatched)
  const cover = useCover(record.userId, record.animeId)
  const { anime, episode } = record.snapshot
  const percent = percentOf(record)
  const done = record.state === 'downloaded'
  const preparing = record.state === 'preparing'
  const serverKnown = preparing && (record.serverProgress ?? null) !== null
  const running =
    record.state === 'downloading' || record.state === 'queued' || record.state === 'preparing'
  const episodeLabel = `Episode ${String(episode.number)}${episode.title === null ? '' : ` · ${episode.title}`}`
  const name = `${anime.title.preferred} episode ${String(episode.number)}`
  const headline = grouped ? episodeLabel : anime.title.preferred

  return (
    <div className="flex flex-col gap-3 rounded-row p-3.5">
      {/*
        On a phone the buttons take a line of their own under the text, lined
        up with it (as the show page's rows do); from `sm` it is one row.
      */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5 sm:flex-nowrap">
        <Artwork url={cover} shape="thumb" className="w-[46px] shrink-0" />
        <div className="min-w-0 flex-1 basis-[calc(100%-62px)] sm:basis-auto">
          <p className="truncate text-[16px] font-medium text-[var(--arc-text)]">
            {done ? (
              <Link to={`/watch/${String(record.episodeId)}`} className={FOCUS_RING}>
                {headline}
              </Link>
            ) : (
              headline
            )}
          </p>
          {grouped ? null : (
            <p className="truncate text-[13px] text-[var(--arc-text-muted)]">{episodeLabel}</p>
          )}
          <p className="text-[13px] tabular-nums text-[var(--arc-text-muted)]">
            {STATE_LABEL[record.state]}
            {preparing
              ? serverKnown
                ? ` · ${String(percent)}%`
                : null
              : ` · ${
                  done
                    ? formatSize(record.bytes)
                    : `${formatSize(record.bytes)} of ${record.total > 0 ? formatSize(record.total) : '…'} · ${String(percent)}%`
                }`}
            {preparing ? null : ` · ${COPY_NOTE[record.variant]}`}
            {done && record.confirm === 'pending' ? ' · telling Arc it arrived' : null}
          </p>
          {note === null ? null : (
            <p className="text-[13px] text-[var(--arc-text-muted)]">{note}</p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2 pl-[62px] sm:pl-0">
          {done ? (
            <Link
              to={`/watch/${String(record.episodeId)}`}
              aria-label={`Play ${name}`}
              className={buttonClass('chip', 'px-4 text-[13px]')}
            >
              Play
            </Link>
          ) : running ? (
            <button
              type="button"
              aria-label={`Pause ${name}`}
              className={buttonClass('chip', 'px-4 text-[13px]')}
              onClick={() => {
                manager.pause(record.episodeId)
              }}
            >
              Pause
            </button>
          ) : (
            <button
              type="button"
              aria-label={`${record.state === 'failed' ? 'Try again' : 'Resume'} ${name}`}
              className={buttonClass('chip', 'px-4 text-[13px]')}
              onClick={() => {
                manager.resume(record.episodeId)
              }}
            >
              {record.state === 'failed' ? 'Try again' : 'Resume'}
            </button>
          )}
          {removeWatched ? (
            <Chip
              active={record.keep === true}
              aria-label={`Keep ${name} after watching`}
              className="px-4 text-[13px]"
              onClick={() => {
                manager.setKeep(record.episodeId, record.keep !== true)
              }}
            >
              Keep
            </Chip>
          ) : null}
          <button
            type="button"
            aria-label={`Delete ${name}`}
            aria-expanded={confirming}
            className={buttonClass('chip', 'px-4 text-[13px]')}
            onClick={confirming ? onCancel : onConfirm}
          >
            Delete
          </button>
        </div>
      </div>

      {done ? null : (
        <span
          role="progressbar"
          aria-label={preparing ? `Preparing ${name} on the server` : `Downloading ${name}`}
          aria-valuenow={preparing && !serverKnown ? undefined : percent}
          aria-valuemin={0}
          aria-valuemax={100}
          className="block h-1 overflow-hidden rounded-full bg-[rgba(255,255,255,0.16)]"
        >
          <span
            className="block h-full bg-[var(--arc-text-muted)]"
            style={{ width: `${String(percent)}%` }}
          />
        </span>
      )}
      {record.message === null ? null : (
        <p
          className={cx(
            'text-[13px]',
            record.state === 'failed' ? 'text-[var(--arc-error)]' : 'text-[var(--arc-text-muted)]',
          )}
        >
          {record.message}
        </p>
      )}

      {confirming ? (
        <div
          role="group"
          aria-label={`Delete ${name} from this device?`}
          className="rounded-card border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] p-4"
        >
          <p className="text-[14px] text-[var(--arc-text)]">
            {record.bytes > 0
              ? `Delete from this device? It frees ${formatSize(record.bytes)}.`
              : 'Delete from this device? Nothing of it has downloaded yet.'}
          </p>
          <p className="mt-1 text-[13px] text-[var(--arc-text-muted)]">
            {record.tripId === undefined
              ? 'Your progress is kept, and Arc still has the episode if you want it again.'
              : 'Your progress is kept. While the trip lasts, Ask again on the show page fetches it once more.'}
          </p>
          <div className="mt-3 flex gap-2.5">
            <button
              type="button"
              className={buttonClass('danger')}
              onClick={() => {
                void manager.remove(record.episodeId)
                onCancel()
              }}
            >
              Delete download
            </button>
            <button type="button" className={buttonClass('chip')} onClick={onCancel}>
              Keep it
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}

function StorageCard({ used }: { used: number }) {
  const [space, setSpace] = useState<{ usage: number; quota: number } | null>(null)
  const [durable, setDurable] = useState<boolean | null>(null)

  useEffect(() => {
    let cancelled = false
    void estimate().then((found) => {
      if (!cancelled) setSpace(found)
    })
    void persisted().then((found) => {
      if (!cancelled) setDurable(found)
    })
    return () => {
      cancelled = true
    }
  }, [used])

  return (
    <div className="rounded-card border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] p-5">
      <h2 className="text-[16px] font-medium text-[var(--arc-text)]">Storage</h2>
      <p className="mt-2 text-[14px] tabular-nums text-[var(--arc-text-muted)]">
        Downloads use {formatSize(used)}.
        {space === null
          ? null
          : ` Arc is using ${formatSize(space.usage)} of the ${formatSize(space.quota)} this browser allows it.`}
      </p>
      <p className="mt-2 text-[14px] text-[var(--arc-text-muted)]">
        {durable === true
          ? 'Storage is persistent: the browser has promised not to clear these downloads when space runs low.'
          : durable === false
            ? 'Storage is not persistent yet: the browser may clear downloads when space runs low. Adding Arc to the Home Screen usually settles it.'
            : 'This browser does not say whether it will keep these downloads.'}
      </p>
    </div>
  )
}

/** The bytes a record will be once whole: its total when known, else what is here. */
function sizeOf(record: DownloadRecord): number {
  return Math.max(record.total, record.bytes)
}

/** "3 on this device · 2 downloading · 1 waiting": the group's counts, nothing that is zero. */
function tripCounts(records: readonly DownloadRecord[]): string {
  const count = (states: DownloadRecord['state'][]) =>
    records.filter((record) => states.includes(record.state)).length
  const parts: [number, string][] = [
    [count(['downloaded']), 'on this device'],
    [count(['downloading', 'queued']), 'downloading'],
    [count(['preparing']), 'waiting for the server'],
    [count(['paused']), 'paused'],
    [count(['failed']), 'stopped'],
  ]
  return parts
    .filter(([n]) => n > 0)
    .map(([n, words]) => `${String(n)} ${words}`)
    .join(' · ')
}

function TripGroup({
  tripId,
  records,
  active,
  renderRow,
}: {
  tripId: number
  records: DownloadRecord[]
  /** This is the account's active trip, and the server answered: it can be cancelled. */
  active: boolean
  renderRow: (record: DownloadRecord) => ReactNode
}) {
  const cancel = useCancelTrip()
  const [confirming, setConfirming] = useState(false)
  const first = records[0]
  if (first === undefined) return null
  const show = first.snapshot.anime.title.preferred
  const total = records.reduce((sum, record) => sum + sizeOf(record), 0)
  const n = records.length

  return (
    <section
      aria-label={`Trip · ${show}`}
      className="rounded-card border-[0.5px] border-[var(--arc-border)] sm:p-1.5"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 px-3.5 pt-2.5 pb-1">
        <div className="min-w-0">
          <h2 className="text-[16px] font-medium text-[var(--arc-text)]">
            {`Trip · ${show} · ${String(n)} episode${n === 1 ? '' : 's'} · ${formatSize(total)}`}
          </h2>
          <p className="text-[13px] text-[var(--arc-text-muted)] tabular-nums">
            {tripCounts(records)}
          </p>
        </div>
        {active ? (
          <button
            type="button"
            aria-expanded={confirming}
            className={buttonClass('chip', 'px-4 text-[13px]')}
            onClick={() => {
              cancel.reset()
              setConfirming(!confirming)
            }}
          >
            Cancel trip
          </button>
        ) : null}
      </div>
      {confirming && active ? (
        <div
          role="group"
          aria-label="Cancel this trip?"
          className="mx-3.5 my-2 rounded-card border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] p-4"
        >
          <p className="text-[14px] text-[var(--arc-text)]">Cancel this trip?</p>
          <p className="mt-1 text-[13px] text-[var(--arc-text-muted)]">
            Episodes already on this device stay. Arc stops fetching the rest.
          </p>
          <div className="mt-3 flex gap-2.5">
            <button
              type="button"
              disabled={cancel.isPending}
              className={buttonClass('danger')}
              onClick={() => {
                cancel.mutate(
                  { tripId, animeId: first.animeId },
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
            <p role="alert" className="mt-2 text-[13px] text-[var(--arc-error)]">
              Could not cancel the trip. Try again when Arc is reachable.
            </p>
          ) : null}
        </div>
      ) : null}
      <RowGroup>{records.map(renderRow)}</RowGroup>
    </section>
  )
}

export function Downloads() {
  const records = useDownloads()
  const { data: me } = useMe()
  const { data: current } = useCurrentTrip(me !== undefined && me !== null && !me.is_demo)
  const [confirming, setConfirming] = useState<number | null>(null)
  const items = Object.values(records).sort((a, b) => {
    const show = a.snapshot.anime.title.preferred.localeCompare(b.snapshot.anime.title.preferred)
    return show !== 0 ? show : a.snapshot.episode.number - b.snapshot.episode.number
  })
  const used = items.reduce((sum, item) => sum + item.bytes, 0)
  const loose = items.filter((record) => record.tripId === undefined)
  const trips = new Map<number, DownloadRecord[]>()
  for (const record of items) {
    if (record.tripId === undefined) continue
    trips.set(record.tripId, [...(trips.get(record.tripId) ?? []), record])
  }
  // The newest trip first: a larger id is a later trip.
  const groups = [...trips.entries()].sort(([a], [b]) => b - a)

  const renderRow = (record: DownloadRecord) => (
    <DownloadRow
      grouped={record.tripId !== undefined}
      key={record.episodeId}
      record={record}
      confirming={confirming === record.episodeId}
      onConfirm={() => {
        setConfirming(record.episodeId)
      }}
      onCancel={() => {
        setConfirming(null)
      }}
    />
  )

  return (
    <section>
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        Downloads
      </h1>
      <p className="mt-2.5 max-w-[66ch] text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
        {LEDE}
      </p>

      <div className="mt-7 flex max-w-[1080px] flex-col gap-6">
        {items.length === 0 ? null : <RemoveWatchedSwitch />}
        {items.length === 0 ? (
          <EmptyState
            message={canDownloadInApp() ? EMPTY_MESSAGE : UNSUPPORTED}
            action={
              <Link to="/" className={buttonClass('secondary')}>
                Go to Watch Now
              </Link>
            }
          />
        ) : (
          <>
            {groups.map(([tripId, group]) => (
              <TripGroup
                key={tripId}
                tripId={tripId}
                records={group}
                active={current !== undefined && current !== null && current.id === tripId}
                renderRow={renderRow}
              />
            ))}
            {loose.length === 0 ? null : <RowGroup>{loose.map(renderRow)}</RowGroup>}
          </>
        )}

        <StorageCard used={used} />

        <ul className="flex list-disc flex-col gap-1.5 pl-5 text-[13px] text-[var(--arc-text-muted)]">
          {NOTES.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      </div>
    </section>
  )
}
