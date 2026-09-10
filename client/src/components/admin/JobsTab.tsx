/**
 * The job queue (spec §4.10 FR-D3, §7 "admin page exposes queue depths",
 * roadmap M14).
 *
 * Every background action in Arc is a job (`arc/services/jobs`), so this table
 * is the answer to most operational questions: why an episode is not moving,
 * whether the worker is alive, what failed and with what message. It polls
 * every fifteen seconds while the tab is open — a job moves on the worker's
 * clock, not the admin's — and stops when the tab is left, because the queries
 * unmount with it.
 *
 * `last_error` is truncated with an expander rather than wrapped: an ffmpeg
 * stderr tail is a screen of text, and a table where one row is a page is a
 * table nobody scans. Retry and Cancel are rendered only where the server would
 * accept them (`canRetry`, `canCancel`); the 409 is still handled, because
 * between the render and the click the worker may have claimed the row.
 */

import { useState } from 'react'
import { ErrorState } from '@/components/ErrorState'
import {
  InlineError,
  Pill,
  SectionHeading,
  inputClass,
  subtleButtonClass,
  TableScroll,
  tdClass,
  thClass,
  type Tone,
} from '@/components/admin/ui'
import {
  JOBS_PAGE_SIZE,
  JOB_STATUSES,
  adminErrorMessage,
  attemptsLabel,
  canCancel,
  canRetry,
  formatRelativeTime,
  useCancelJob,
  useJobs,
  useJobsSummary,
  useRetryJob,
  type JobFilter,
  type JobRow,
  type JobStatus,
  type JobsSummary,
} from '@/lib/admin'

const EXPLANATION =
  'Everything Arc does in the background is a job: searching Nyaa, polling qBittorrent, matching a ' +
  'file, transcoding, sweeping. This refreshes itself every 15 seconds while you are on it.'

/** How much of a stack trace fits in a table cell before it stops helping. */
const ERROR_PREVIEW = 120

const STATUS_TONES: Record<JobStatus, Tone> = {
  pending: 'muted',
  running: 'busy',
  done: 'ok',
  failed: 'bad',
  cancelled: 'warn',
}

/** Counts by status, plus whether anything is running them at all. */
function SummaryStrip({ summary }: { summary: JobsSummary }) {
  const backlog = Object.entries(summary.by_type_pending).filter(([, count]) => count > 0)

  return (
    <div className="mt-4 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {JOB_STATUSES.map((status) => (
          <Pill key={status} tone={STATUS_TONES[status]}>
            {status}: {summary.by_status[status]}
          </Pill>
        ))}
        <Pill tone={summary.worker.alive ? 'ok' : 'bad'}>
          {summary.worker.alive ? 'worker alive' : 'worker not responding'}
        </Pill>
        <span className="text-xs text-[var(--arc-text-muted)]">
          {summary.worker.heartbeat_at === null
            ? 'no heartbeat yet'
            : `last heartbeat ${formatRelativeTime(summary.worker.heartbeat_at)}`}
        </span>
      </div>
      {backlog.length === 0 ? null : (
        <p className="text-xs text-[var(--arc-text-muted)]">
          Pending by type:{' '}
          {backlog.map(([type, count]) => `${type} (${String(count)})`).join(' · ')}
        </p>
      )}
    </div>
  )
}

/** The three timestamps that matter, in the order a job passes through them. */
function Timings({ job }: { job: JobRow }) {
  const rows: [string, string | null][] = [
    ['runs', job.started_at === null && job.finished_at === null ? job.run_after : null],
    ['started', job.started_at],
    ['finished', job.finished_at],
  ]

  return (
    <div className="flex flex-col gap-0.5 text-xs whitespace-nowrap text-[var(--arc-text-muted)]">
      {rows.map(([label, at]) =>
        at === null ? null : (
          <span key={label}>
            {label} {formatRelativeTime(at)}
          </span>
        ),
      )}
    </div>
  )
}

/** The failure, short by default. Nothing at all when the job did not fail. */
function LastError({ job }: { job: JobRow }) {
  const [open, setOpen] = useState(false)
  const text = job.last_error

  if (text === null || text === '') {
    return <span className="text-xs text-[var(--arc-text-muted)]">—</span>
  }

  const long = text.length > ERROR_PREVIEW

  return (
    <div className="min-w-0">
      <p className="max-w-md text-xs break-words text-[var(--arc-error)]">
        {open || !long ? text : `${text.slice(0, ERROR_PREVIEW)}…`}
      </p>
      {long ? (
        <button
          type="button"
          className="mt-1 text-xs text-[var(--arc-accent)] hover:underline"
          onClick={() => {
            setOpen((current) => !current)
          }}
        >
          {open ? 'Show less' : 'Show more'}
        </button>
      ) : null}
    </div>
  )
}

function JobsTable({ jobs }: { jobs: JobRow[] }) {
  const retry = useRetryJob()
  const cancel = useCancelJob()

  function errorFor(job: JobRow): string | null {
    if (retry.isError && retry.variables === job.id) return adminErrorMessage(retry.error)
    if (cancel.isError && cancel.variables === job.id) return adminErrorMessage(cancel.error)
    return null
  }

  return (
    <TableScroll>
      <table className="min-w-full border-collapse">
        <caption className="sr-only">Jobs</caption>
        <thead>
          <tr>
            <th className={thClass}>ID</th>
            <th className={thClass}>Type</th>
            <th className={thClass}>Status</th>
            <th className={thClass}>Attempts</th>
            <th className={thClass}>Timing</th>
            <th className={thClass}>Last error</th>
            <th className={thClass}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => {
            const error = errorFor(job)
            const busy =
              (retry.isPending && retry.variables === job.id) ||
              (cancel.isPending && cancel.variables === job.id)

            return (
              <tr key={job.id} className="border-t border-[var(--arc-border)]">
                <td className={`${tdClass} text-[var(--arc-text-muted)]`}>{job.id}</td>
                <td className={`${tdClass} whitespace-nowrap`}>{job.type}</td>
                <td className={tdClass}>
                  <Pill tone={STATUS_TONES[job.status]}>{job.status}</Pill>
                </td>
                <td className={`${tdClass} whitespace-nowrap`}>{attemptsLabel(job)}</td>
                <td className={tdClass}>
                  <Timings job={job} />
                </td>
                <td className={tdClass}>
                  <LastError job={job} />
                </td>
                <td className={tdClass}>
                  <div className="flex flex-wrap items-center gap-2">
                    {canRetry(job) ? (
                      <button
                        type="button"
                        className={subtleButtonClass}
                        disabled={busy}
                        onClick={() => {
                          retry.mutate(job.id)
                        }}
                      >
                        Retry
                      </button>
                    ) : null}
                    {canCancel(job) ? (
                      <button
                        type="button"
                        className={subtleButtonClass}
                        disabled={busy}
                        onClick={() => {
                          cancel.mutate(job.id)
                        }}
                      >
                        Cancel
                      </button>
                    ) : null}
                    {canRetry(job) || canCancel(job) ? null : (
                      <span className="text-xs text-[var(--arc-text-muted)]">—</span>
                    )}
                  </div>
                  {error === null ? null : <InlineError className="mt-1" message={error} />}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </TableScroll>
  )
}

export function JobsTab() {
  const [filter, setFilter] = useState<JobFilter>({ status: 'all', type: 'all', offset: 0 })
  const summary = useJobsSummary()
  const jobs = useJobs(filter)

  // Types worth offering: whatever has work queued, plus whatever is on screen.
  // Derived rather than hard-coded, because the registry lives in the worker's
  // service packages and a list here would go stale the day one is added.
  const types = [
    ...new Set([
      ...Object.keys(summary.data?.by_type_pending ?? {}),
      ...(jobs.data ?? []).map((job) => job.type),
      ...(filter.type === 'all' ? [] : [filter.type]),
    ]),
  ].sort((a, b) => a.localeCompare(b))

  const rows = jobs.data ?? []
  const hasNext = rows.length === JOBS_PAGE_SIZE

  return (
    <div>
      <SectionHeading>Jobs</SectionHeading>
      <p className="mt-1 max-w-3xl text-sm text-[var(--arc-text-muted)]">{EXPLANATION}</p>

      {summary.isError ? (
        <InlineError className="mt-4" message={adminErrorMessage(summary.error)} />
      ) : summary.data === undefined ? null : (
        <SummaryStrip summary={summary.data} />
      )}

      <div className="mt-4 flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="jobs-status" className="text-sm font-medium text-[var(--arc-text)]">
            Status
          </label>
          <select
            id="jobs-status"
            value={filter.status}
            className={`w-40 ${inputClass}`}
            onChange={(event) => {
              const value = event.target.value as JobStatus | 'all'
              setFilter((current) => ({ ...current, status: value, offset: 0 }))
            }}
          >
            <option value="all">All statuses</option>
            {JOB_STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="jobs-type" className="text-sm font-medium text-[var(--arc-text)]">
            Type
          </label>
          <select
            id="jobs-type"
            value={filter.type}
            className={`w-56 ${inputClass}`}
            onChange={(event) => {
              const value = event.target.value
              setFilter((current) => ({ ...current, type: value, offset: 0 }))
            }}
          >
            <option value="all">All types</option>
            {types.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </div>
      </div>

      {jobs.isPending ? (
        <p role="status" className="mt-4 text-sm text-[var(--arc-text-muted)]">
          Loading jobs…
        </p>
      ) : jobs.isError ? (
        <ErrorState
          className="mt-4"
          message={adminErrorMessage(jobs.error)}
          pending={jobs.isFetching}
          onRetry={() => {
            void jobs.refetch()
          }}
        />
      ) : rows.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--arc-text-muted)]">
          No jobs match that filter{filter.offset > 0 ? ' on this page' : ''}.
        </p>
      ) : (
        <div className="mt-4">
          <JobsTable jobs={rows} />
        </div>
      )}

      {filter.offset > 0 || hasNext ? (
        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            className={subtleButtonClass}
            disabled={filter.offset === 0}
            onClick={() => {
              setFilter((current) => ({
                ...current,
                offset: Math.max(0, current.offset - JOBS_PAGE_SIZE),
              }))
            }}
          >
            Newer
          </button>
          <button
            type="button"
            className={subtleButtonClass}
            disabled={!hasNext}
            onClick={() => {
              setFilter((current) => ({ ...current, offset: current.offset + JOBS_PAGE_SIZE }))
            }}
          >
            Older
          </button>
          <span className="text-xs text-[var(--arc-text-muted)]">
            Showing {filter.offset + 1}–{filter.offset + rows.length}
          </span>
        </div>
      ) : null}
    </div>
  )
}
