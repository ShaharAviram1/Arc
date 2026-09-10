import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import {
  formatMalDate,
  formatMalValue,
  MAL_CAUSE_LABELS,
  MAL_FIELD_LABELS,
  MAL_WRITE_STATUS_CLASSES,
  MAL_WRITE_STATUS_LABELS,
  malErrorMessage,
  malLinkErrorMessage,
  useMalImport,
  useMalLog,
  useMalPush,
  useMalStatus,
  useRevertMalWrite,
  useStartMalLink,
  useUnlinkMal,
  type MalStatus,
  type MalWrite,
} from '@/lib/mal'

/** What Arc does with a MAL account, said once, before anyone authorises it. */
const LINK_EXPLANATION =
  'Linking reads your MyAnimeList list once as the baseline — status, progress and score — and ' +
  're-reads it every few hours so changes you make on MAL show up here. Arc writes back only ' +
  'changes you make in Arc: finishing an episode, changing a status, setting a score. Every ' +
  'write is listed below with the value it replaced, and you can put any of them back.'

const NOT_CONFIGURED =
  'This server has no MyAnimeList client id set, so there is nothing to connect to. An ' +
  'administrator needs to add one before this page can do anything.'

const NEEDS_RELINK =
  'MyAnimeList has stopped accepting Arc’s token. Nothing is being read or written until you ' +
  'authorise Arc again.'

const LINKED_MESSAGE = 'MyAnimeList connected.'

const EMPTY_LOG = 'Nothing written to MyAnimeList yet.'

const buttonClass =
  'rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface-raised)] px-3 py-1.5 text-sm text-[var(--arc-text)] transition-colors hover:border-[var(--arc-accent)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'

const primaryButtonClass =
  'rounded-md bg-[var(--arc-accent)] px-3 py-1.5 text-sm font-medium text-[var(--arc-accent-contrast)] transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'

const linkButtonClass =
  'text-xs text-[var(--arc-accent)] underline-offset-2 hover:underline disabled:opacity-60'

/** The three sets of rows the log offers; `all` sends no `status` filter. */
const LOG_FILTERS = ['all', 'failed', 'pending'] as const
type LogFilter = (typeof LOG_FILTERS)[number]

const LOG_FILTER_LABELS: Record<LogFilter, string> = {
  all: 'All writes',
  failed: 'Failed only',
  pending: 'Pending only',
}

function isLogFilter(value: string): value is LogFilter {
  return (LOG_FILTERS as readonly string[]).includes(value)
}

/** What the redirect back from MAL is telling the person, once. */
interface Notice {
  kind: 'ok' | 'error'
  text: string
}

function noticeFrom(params: URLSearchParams): Notice | null {
  const errorCode = params.get('error')
  if (errorCode !== null) return { kind: 'error', text: malLinkErrorMessage(errorCode) }
  return params.get('linked') === null ? null : { kind: 'ok', text: LINKED_MESSAGE }
}

/**
 * The outcome of the round trip to MAL, read out of `?linked=1` / `?error=…`
 * and then wiped from the URL — a reload or a shared link must not replay a
 * message about something that happened minutes ago.
 *
 * The message is captured at mount rather than derived on every render,
 * because the effect below immediately takes the parameters it was read from
 * away; the effect's only job is the URL, which is the external state here.
 */
function useLinkOutcome(): Notice | null {
  const [searchParams, setSearchParams] = useSearchParams()
  const [notice] = useState<Notice | null>(() => noticeFrom(searchParams))

  useEffect(() => {
    if (!searchParams.has('linked') && !searchParams.has('error')) return

    const next = new URLSearchParams(searchParams)
    next.delete('linked')
    next.delete('error')
    // `replace` so the back button does not walk into the consumed callback.
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])

  return notice
}

function NoticeBanner({ notice }: { notice: Notice }) {
  const isError = notice.kind === 'error'
  const tone = isError
    ? 'border-[var(--arc-error)]/40 bg-[var(--arc-error)]/10 text-[var(--arc-error)]'
    : 'border-[var(--arc-ok)]/40 bg-[var(--arc-ok)]/10 text-[var(--arc-ok)]'

  return (
    <p
      role={isError ? 'alert' : 'status'}
      className={`mt-4 rounded-md border px-3 py-2 text-sm ${tone}`}
    >
      {notice.text}
    </p>
  )
}

/** The one control that starts the OAuth round trip; reused by "Reconnect". */
function ConnectButton({ label, className }: { label: string; className: string }) {
  const startLink = useStartMalLink()

  return (
    <>
      <button
        type="button"
        className={className}
        disabled={startLink.isPending}
        onClick={() => {
          startLink.mutate()
        }}
      >
        {startLink.isPending ? 'Opening MyAnimeList…' : label}
      </button>
      {startLink.isError ? (
        <p role="alert" className="mt-2 text-sm text-[var(--arc-error)]">
          {malErrorMessage(startLink.error)}
        </p>
      ) : null}
    </>
  )
}

function NotLinked() {
  return (
    <div className="mt-4 rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] p-4">
      <p className="max-w-2xl text-sm leading-relaxed text-[var(--arc-text-muted)]">
        {LINK_EXPLANATION}
      </p>
      <div className="mt-4">
        <ConnectButton label="Connect MyAnimeList" className={primaryButtonClass} />
      </div>
    </div>
  )
}

/**
 * Disconnecting throws away tokens and stops every future write, so it asks
 * first — inline rather than through `window.confirm`, which blocks the tab
 * and cannot be styled or read by the tests.
 */
function DisconnectControl() {
  const [confirming, setConfirming] = useState(false)
  const unlink = useUnlinkMal()

  if (!confirming) {
    return (
      <button
        type="button"
        className={buttonClass}
        onClick={() => {
          setConfirming(true)
        }}
      >
        Disconnect
      </button>
    )
  }

  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="text-sm text-[var(--arc-text)]">Really disconnect?</span>
      <button
        type="button"
        className={buttonClass}
        disabled={unlink.isPending}
        onClick={() => {
          unlink.mutate()
        }}
      >
        Yes
      </button>
      <button
        type="button"
        className={buttonClass}
        disabled={unlink.isPending}
        onClick={() => {
          setConfirming(false)
        }}
      >
        No
      </button>
      {unlink.isError ? (
        <span role="alert" className="text-sm text-[var(--arc-error)]">
          {malErrorMessage(unlink.error)}
        </span>
      ) : null}
    </span>
  )
}

/**
 * How many *fields* are queued, not how many shows: one show whose status,
 * score and progress all moved is three pending writes, and saying "writes"
 * is what stops the number reading as a show count.
 */
function pendingLabel(count: number): string {
  return count === 1 ? 'Push 1 pending write' : `Push ${String(count)} pending writes`
}

/**
 * The linked account and the three things that can be done to it. Import and
 * push both answer 202 and hand the work to a job, so the buttons report
 * "asked for" rather than "done" and the numbers correct themselves on the
 * next status refetch.
 */
function LinkedPanel({ status }: { status: MalStatus }) {
  const runImport = useMalImport()
  const push = useMalPush()

  return (
    <div className="mt-4 rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] p-4">
      {status.needs_relink ? (
        <div
          role="alert"
          className="mb-4 rounded-md border border-[var(--arc-warn)]/40 bg-[var(--arc-warn)]/10 px-3 py-2"
        >
          <p className="text-sm text-[var(--arc-warn)]">{NEEDS_RELINK}</p>
          <div className="mt-2">
            <ConnectButton label="Reconnect" className={buttonClass} />
          </div>
        </div>
      ) : null}

      <p className="text-sm text-[var(--arc-text)]">
        Connected as{' '}
        <span className="font-medium">{status.mal_username ?? 'your MyAnimeList account'}</span>
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <span className="text-sm text-[var(--arc-text-muted)]">
          Last import: {formatMalDate(status.last_import_at)}
        </span>
        <button
          type="button"
          className={buttonClass}
          disabled={runImport.isPending}
          onClick={() => {
            runImport.mutate()
          }}
        >
          {runImport.isPending ? 'Starting…' : 'Import now'}
        </button>
        {status.pending_writes > 0 ? (
          <button
            type="button"
            className={buttonClass}
            disabled={push.isPending}
            onClick={() => {
              push.mutate()
            }}
          >
            {pendingLabel(status.pending_writes)}
          </button>
        ) : null}
        <DisconnectControl />
      </div>

      {status.failed_writes > 0 ? (
        <p className="mt-3 text-sm text-[var(--arc-error)]">
          {status.failed_writes === 1
            ? '1 write failed and was not applied.'
            : `${String(status.failed_writes)} writes failed and were not applied.`}
        </p>
      ) : null}

      {runImport.isError ? (
        <p role="alert" className="mt-3 text-sm text-[var(--arc-error)]">
          {malErrorMessage(runImport.error)}
        </p>
      ) : null}
      {push.isError ? (
        <p role="alert" className="mt-3 text-sm text-[var(--arc-error)]">
          {malErrorMessage(push.error)}
        </p>
      ) : null}
    </div>
  )
}

/**
 * Put a write's previous value back (FR-M5). Only offered while the server
 * still says the row is revertible; the button goes quiet while the request is
 * in flight, since a second click would queue a second revert.
 */
function RevertButton({ write }: { write: MalWrite }) {
  const revert = useRevertMalWrite()

  return (
    <>
      <button
        type="button"
        className={linkButtonClass}
        disabled={revert.isPending}
        onClick={() => {
          revert.mutate(write.id)
        }}
      >
        Revert
      </button>
      {revert.isError ? (
        <span role="alert" className="mt-1 block text-xs text-[var(--arc-error)]">
          {malErrorMessage(revert.error)}
        </span>
      ) : null}
    </>
  )
}

function WriteRow({ write }: { write: MalWrite }) {
  const title = write.anime?.title.preferred ?? 'Unknown show'
  const note = write.error === null || write.error === '' ? null : write.error
  // A skipped row's sentence says why nothing was sent ("automatic progress
  // never lowers MAL") — an explanation, not a complaint, so it is muted.
  // A skipped row's note is an explanation and a pending row's note is the
  // last attempt's error while it is still being retried; only a final
  // failure reads as an error.
  const noteClass =
    write.status === 'failed' ? 'text-[var(--arc-error)]' : 'text-[var(--arc-text-muted)]'

  return (
    <tr className="border-t border-[var(--arc-border)]">
      <td className="px-3 py-2 whitespace-nowrap text-[var(--arc-text-muted)]">
        {formatMalDate(write.created_at)}
      </td>
      <td className="px-3 py-2 text-[var(--arc-text)]">
        {write.anime === null ? (
          <span className="text-[var(--arc-text-muted)]">{title}</span>
        ) : (
          <Link
            to={`/anime/${String(write.anime.id)}`}
            className="text-[var(--arc-accent)] hover:underline"
          >
            {title}
          </Link>
        )}
      </td>
      <td className="px-3 py-2 text-[var(--arc-text-muted)]">{MAL_FIELD_LABELS[write.field]}</td>
      <td className="px-3 py-2 whitespace-nowrap text-[var(--arc-text)]">
        {formatMalValue(write.field, write.old_value)} →{' '}
        {formatMalValue(write.field, write.new_value)}
      </td>
      <td className="px-3 py-2 text-[var(--arc-text-muted)]">{MAL_CAUSE_LABELS[write.cause]}</td>
      <td className="px-3 py-2">
        <span
          title={note ?? undefined}
          className={`inline-block rounded-full border px-2 py-0.5 text-xs ${MAL_WRITE_STATUS_CLASSES[write.status]}`}
        >
          {MAL_WRITE_STATUS_LABELS[write.status]}
        </span>
        {note === null ? null : <span className={`mt-1 block text-xs ${noteClass}`}>{note}</span>}
      </td>
      <td className="px-3 py-2 text-right">
        {write.revertible ? <RevertButton write={write} /> : null}
      </td>
    </tr>
  )
}

function WriteLog() {
  const [filter, setFilter] = useState<LogFilter>('all')
  const { data, isPending, isError, error } = useMalLog({
    status: filter === 'all' ? undefined : filter,
  })

  return (
    <section className="mt-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight text-[var(--arc-text)]">Write log</h2>
        <select
          aria-label="Filter writes"
          value={filter}
          onChange={(event) => {
            const next = event.target.value
            if (isLogFilter(next)) setFilter(next)
          }}
          className="rounded-md border border-[var(--arc-border)] bg-[var(--arc-bg)] px-2 py-1.5 text-sm text-[var(--arc-text)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)]"
        >
          {LOG_FILTERS.map((value) => (
            <option key={value} value={value}>
              {LOG_FILTER_LABELS[value]}
            </option>
          ))}
        </select>
      </div>

      {isPending ? (
        <p role="status" className="mt-3 text-sm text-[var(--arc-text-muted)]">
          Loading…
        </p>
      ) : isError ? (
        <p role="alert" className="mt-3 text-sm text-[var(--arc-error)]">
          {malErrorMessage(error)}
        </p>
      ) : data.length === 0 ? (
        <p className="mt-3 text-sm text-[var(--arc-text-muted)]">{EMPTY_LOG}</p>
      ) : (
        <div className="mt-3 overflow-x-auto rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)]">
          <table className="w-full min-w-[48rem] text-left text-sm">
            <thead className="text-xs tracking-wide text-[var(--arc-text-muted)] uppercase">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">
                  When
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Show
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Field
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Change
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Cause
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Status
                </th>
                <th scope="col" className="px-3 py-2 text-right font-medium">
                  Revert
                </th>
              </tr>
            </thead>
            <tbody>
              {data.map((write) => (
                <WriteRow key={write.id} write={write} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/**
 * MAL link / sync log (spec §4.7, §5, roadmap M9). One page for the whole
 * integration: whether it is connected, what it has written, and how to undo
 * any of it.
 */
export function Mal() {
  const notice = useLinkOutcome()
  const { data: status, isPending, isError, isFetching, error, refetch } = useMalStatus()

  return (
    <section className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">MyAnimeList</h1>

      {notice === null ? null : <NoticeBanner notice={notice} />}

      {isPending ? (
        <p role="status" className="mt-4 text-sm text-[var(--arc-text-muted)]">
          Loading…
        </p>
      ) : isError ? (
        <ErrorState
          className="mt-4"
          message={malErrorMessage(error)}
          pending={isFetching}
          onRetry={() => {
            void refetch()
          }}
        />
      ) : !status.configured ? (
        <p className="mt-4 max-w-2xl rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-2 text-sm text-[var(--arc-text-muted)]">
          {NOT_CONFIGURED}
        </p>
      ) : (
        <>
          {status.linked ? <LinkedPanel status={status} /> : <NotLinked />}
          <WriteLog />
        </>
      )}
    </section>
  )
}
