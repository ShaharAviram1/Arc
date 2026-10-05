import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Artwork, buttonClass, cx, EmptyState, FOCUS_RING, RowGroup } from '@/components/ui'
import { formatSize } from '@/lib/review'
import { coverUrl } from '@/offline/cache'
import { downloads, SCREEN_NOTE, type DownloadRecord } from '@/offline/downloads'
import { canDownloadInApp, estimate, persisted } from '@/offline/opfs'
import { useDownloads } from '@/offline/useDownloads'

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
 */

const LEDE = 'Episodes kept inside Arc on this device. They play with no connection.'

const EMPTY_MESSAGE =
  'Nothing is downloaded to this device yet. On a show page, choose “Keep offline” beside a ready episode.'

const UNSUPPORTED =
  'This browser can’t keep episodes inside Arc. “Save file” on a show page still saves the MP4.'

/** The notes at the bottom: each is something that has surprised somebody. */
const NOTES = [
  SCREEN_NOTE,
  'Removing Arc from your Home Screen deletes its downloads (iOS). Delete episodes here instead.',
  'A downloaded episode is your copy: it keeps playing here even after the server clears its own.',
  'Progress made offline is saved on this device and syncs to Arc and MyAnimeList when you reconnect.',
]

function percentOf(record: DownloadRecord): number {
  return record.total > 0 ? Math.min(100, Math.floor((record.bytes / record.total) * 100)) : 0
}

const STATE_LABEL: Record<DownloadRecord['state'], string> = {
  queued: 'Queued',
  downloading: 'Downloading',
  paused: 'Paused',
  downloaded: 'On this device',
  failed: 'Stopped',
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
}: {
  record: DownloadRecord
  confirming: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const manager = downloads()
  const cover = useCover(record.userId, record.animeId)
  const { anime, episode } = record.snapshot
  const percent = percentOf(record)
  const done = record.state === 'downloaded'
  const running = record.state === 'downloading' || record.state === 'queued'
  const episodeLabel = `Episode ${String(episode.number)}${episode.title === null ? '' : ` · ${episode.title}`}`
  const name = `${anime.title.preferred} episode ${String(episode.number)}`

  return (
    <div className="flex flex-col gap-3 rounded-row p-3.5">
      <div className="flex items-center gap-4">
        <Artwork url={cover} shape="thumb" className="w-[46px] shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[16px] font-medium text-[var(--arc-text)]">
            {done ? (
              <Link to={`/watch/${String(record.episodeId)}`} className={FOCUS_RING}>
                {anime.title.preferred}
              </Link>
            ) : (
              anime.title.preferred
            )}
          </p>
          <p className="truncate text-[13px] text-[var(--arc-text-muted)]">{episodeLabel}</p>
          <p className="text-[13px] tabular-nums text-[var(--arc-text-muted)]">
            {STATE_LABEL[record.state]}
            {' · '}
            {done
              ? formatSize(record.bytes)
              : `${formatSize(record.bytes)} of ${record.total > 0 ? formatSize(record.total) : '…'} · ${String(percent)}%`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
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
          aria-label={`Downloading ${name}`}
          aria-valuenow={percent}
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
            Delete from this device? It frees {formatSize(record.bytes)}.
          </p>
          <p className="mt-1 text-[13px] text-[var(--arc-text-muted)]">
            Your progress is kept, and Arc still has the episode if you want it again.
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

export function Downloads() {
  const records = useDownloads()
  const [confirming, setConfirming] = useState<number | null>(null)
  const items = Object.values(records).sort((a, b) => {
    const show = a.snapshot.anime.title.preferred.localeCompare(b.snapshot.anime.title.preferred)
    return show !== 0 ? show : a.snapshot.episode.number - b.snapshot.episode.number
  })
  const used = items.reduce((sum, item) => sum + item.bytes, 0)

  return (
    <section>
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        Downloads
      </h1>
      <p className="mt-2.5 max-w-[66ch] text-[16px] leading-[1.55] text-[var(--arc-text-muted)]">
        {LEDE}
      </p>

      <div className="mt-7 flex max-w-[1080px] flex-col gap-6">
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
          <RowGroup>
            {items.map((record) => (
              <DownloadRow
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
            ))}
          </RowGroup>
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
