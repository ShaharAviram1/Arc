/**
 * Disk and retention (spec §4.9 FR-T4, §4.10 FR-D3, roadmap M14).
 *
 * FR-T4 is one sentence — "admin can see disk usage and manually delete or
 * re-fetch" — and this tab is all three of it. The preview is the important
 * half: it is the *sweep's own rule* with the deletion left out
 * (`arc/api/retention.py` calls `candidates`), so what is listed here is
 * exactly what would go, with the sentence the sweep would log beside it. A
 * second implementation of the policy for the sake of a preview would be worse
 * than no preview.
 *
 * Both per-episode buttons queue a job and say so. Delete asks first — it is
 * the one control in Arc that removes a file a person may be in the middle of
 * watching — while re-fetch does not, because the worst it can do is start a
 * download that was going to happen anyway.
 */

import { Link } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import {
  ConfirmButton,
  InlineError,
  Notice,
  panelClass,
  Pill,
  SectionHeading,
  TableScroll,
  tdClass,
  thClass,
} from '@/components/admin/ui'
import { primaryButtonClass, subtleButtonClass } from '@/components/admin/styles'
import {
  adminErrorMessage,
  formatBytes,
  formatPercent,
  useDeleteEpisodeFiles,
  useRefetchEpisode,
  useRetentionDisk,
  useRetentionPreview,
  useRunSweep,
  usedFraction,
  type RetentionDisk,
  type RetentionItem,
} from '@/lib/admin'
import { episodeStateClass, episodeStateLabel } from '@/lib/anime'

const EXPLANATION =
  'What Arc is holding, and what the next retention sweep would remove. Deleting an episode’s ' +
  'files resets it to “not wanted”; if anybody still wants it, the next window computation ' +
  're-acquires it (FR-T3).'

const DRY_RUN_NOTE =
  'RETENTION_DRY_RUN is on: the sweep will report this list and delete none of it.'

/** How full the data disk is, in a bar and in words. */
function DiskPanel({ disk }: { disk: RetentionDisk }) {
  const fraction = usedFraction(disk.data_dir)

  return (
    <div className={`mt-4 ${panelClass}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-[16px] font-medium text-[var(--arc-text)]">Data directory</h3>
        <p className="text-[14px] tabular-nums text-[var(--arc-text-muted)]">
          {formatBytes(disk.data_dir.used)} used of {formatBytes(disk.data_dir.total)} ·{' '}
          {formatBytes(disk.data_dir.free)} free
        </p>
      </div>
      <div
        role="progressbar"
        aria-label="Data directory usage"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(fraction * 100)}
        aria-valuetext={`${formatPercent(fraction)} used`}
        className="mt-3 h-1 w-full overflow-hidden rounded-full bg-[var(--arc-progress-track)]"
      >
        {/* White, like every other progress bar in Arc; the arc gradient marks
            season progress in My List and nothing else. Only a disk about to
            fill up earns a colour of its own. */}
        <div
          className={`h-full rounded-full ${
            fraction > 0.9 ? 'bg-[var(--arc-error)]' : 'bg-[var(--arc-progress-fill)]'
          }`}
          style={{ width: `${String(Math.round(fraction * 100))}%` }}
        />
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ['Sources', formatBytes(disk.retained.sources)],
          ['Renditions', formatBytes(disk.retained.renditions)],
          ['Retained total', formatBytes(disk.retained.total)],
          ['Episodes retained', String(disk.episodes_retained)],
        ].map(([label, value]) => (
          <div
            key={label}
            className="rounded-row border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] px-3.5 py-3"
          >
            <dt className="text-[12px] font-semibold tracking-[0.08em] text-[var(--arc-text-muted)] uppercase">
              {label}
            </dt>
            <dd className="mt-1 text-[16px] tabular-nums text-[var(--arc-text)]">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

/** One episode the sweep would take, with the two ways to pre-empt it. */
function PreviewRow({ item }: { item: RetentionItem }) {
  const remove = useDeleteEpisodeFiles()
  const refetch = useRefetchEpisode()

  const error = remove.isError
    ? adminErrorMessage(remove.error)
    : refetch.isError
      ? adminErrorMessage(refetch.error)
      : null
  const queued = remove.isSuccess ? 'Deletion queued.' : refetch.isSuccess ? 'Search queued.' : null

  return (
    <tr className="border-t border-[var(--arc-border)]">
      <td className={tdClass}>
        <Link to={`/anime/${String(item.anime_id)}`} className="font-medium hover:underline">
          {item.anime_title}
        </Link>
        <span className="text-[var(--arc-text-muted)]"> · episode {item.number}</span>
      </td>
      <td className={tdClass}>
        <span
          className={`inline-flex items-center rounded-full border-[0.5px] px-2.5 py-0.5 text-[12px] whitespace-nowrap ${episodeStateClass(item.state)}`}
        >
          {episodeStateLabel(item.state)}
        </span>
      </td>
      <td className={`${tdClass} text-[var(--arc-text-muted)]`}>{item.reason}</td>
      <td className={`${tdClass} whitespace-nowrap`}>{formatBytes(item.bytes)}</td>
      <td className={tdClass}>
        <div className="flex flex-wrap items-center gap-2">
          <ConfirmButton
            label="Delete files"
            question={`Delete ${item.anime_title} episode ${String(item.number)}?`}
            confirmLabel="Yes, delete"
            pending={remove.isPending}
            onConfirm={() => {
              remove.mutate(item.episode_id)
            }}
          />
          <button
            type="button"
            className={subtleButtonClass}
            disabled={refetch.isPending}
            onClick={() => {
              refetch.mutate(item.episode_id)
            }}
          >
            Re-fetch
          </button>
        </div>
        {queued === null ? null : <Notice className="mt-1">{queued}</Notice>}
        {error === null ? null : <InlineError className="mt-1" message={error} />}
      </td>
    </tr>
  )
}

export function StorageTab() {
  const disk = useRetentionDisk()
  const preview = useRetentionPreview()
  const sweep = useRunSweep()

  return (
    <div>
      <SectionHeading>Storage</SectionHeading>
      <p className="mt-2 max-w-[66ch] text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
        {EXPLANATION}
      </p>

      {disk.isPending ? (
        <p role="status" className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
          Loading disk usage…
        </p>
      ) : disk.isError ? (
        <ErrorState
          className="mt-4"
          message={adminErrorMessage(disk.error)}
          pending={disk.isFetching}
          onRetry={() => {
            void disk.refetch()
          }}
        />
      ) : (
        <DiskPanel disk={disk.data} />
      )}

      <section className="mt-10">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <SectionHeading>Next sweep</SectionHeading>
          <button
            type="button"
            className={primaryButtonClass}
            disabled={sweep.isPending}
            onClick={() => {
              sweep.mutate()
            }}
          >
            {sweep.isPending ? 'Queueing…' : 'Run retention sweep now'}
          </button>
        </div>

        {sweep.isSuccess ? (
          <Notice className="mt-2">
            Sweep queued as job {sweep.data.id}. It runs on the worker; the list below refreshes
            when it has.
          </Notice>
        ) : null}
        {sweep.isError ? (
          <InlineError className="mt-2" message={adminErrorMessage(sweep.error)} />
        ) : null}

        {preview.isPending ? (
          <p role="status" className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
            Loading the preview…
          </p>
        ) : preview.isError ? (
          <ErrorState
            className="mt-4"
            message={adminErrorMessage(preview.error)}
            pending={preview.isFetching}
            onRetry={() => {
              void preview.refetch()
            }}
          />
        ) : (
          <div className="mt-4">
            {preview.data.dry_run ? (
              <p className="mb-3">
                <Pill tone="warn">{DRY_RUN_NOTE}</Pill>
              </p>
            ) : null}

            {preview.data.episodes.length === 0 ? (
              <p className="text-[14px] text-[var(--arc-text-muted)]">
                Nothing is due for deletion. Files are removed, and only removed, once the grace
                period has run out.
              </p>
            ) : (
              <>
                <p className="mb-4 text-[14px] text-[var(--arc-text-muted)]">
                  {preview.data.episodes.length === 1
                    ? '1 episode'
                    : `${String(preview.data.episodes.length)} episodes`}{' '}
                  would be deleted, freeing {formatBytes(preview.data.bytes)}.
                </p>
                <TableScroll>
                  <table className="min-w-full border-collapse">
                    <caption className="sr-only">Retention preview</caption>
                    <thead>
                      <tr>
                        <th className={thClass}>Episode</th>
                        <th className={thClass}>State</th>
                        <th className={thClass}>Why</th>
                        <th className={thClass}>Size</th>
                        <th className={thClass}>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.data.episodes.map((item) => (
                        <PreviewRow key={item.episode_id} item={item} />
                      ))}
                    </tbody>
                  </table>
                </TableScroll>
              </>
            )}
          </div>
        )}
      </section>
    </div>
  )
}
