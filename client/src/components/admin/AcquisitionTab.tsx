/**
 * Acquisition (spec §4.2, §4.10 FR-D3, roadmap M14).
 *
 * The pause switch lives here rather than with the other rules because it is
 * not a rule to be edited and saved: it is a brake, pressed while watching the
 * three counts beside it, and it takes effect the moment the server answers.
 * It shares `acquisitionStatusQueryKey` with the sidebar's "Acquisition paused"
 * note, so the two can never disagree.
 *
 * What a pause does is spelled out on the page, because the surprising part is
 * what it does *not* do: downloads already running finish and are ingested
 * (`poll_qbit` ignores the flag). Without that sentence the first thing an
 * admin does after pausing is wonder why a torrent is still moving.
 *
 * The wants table is the diagnostic the endpoint exists for — "why is Arc
 * downloading that?", and its harder twin, "why is it *not* downloading this?"
 * — with each want's episode state beside it. The qBittorrent panel is the
 * other half of the same question: unreachable is rendered as an answer, not
 * as a failed page, because "qBittorrent is down" is what an admin came to
 * learn.
 */

import { Link } from 'react-router-dom'
import { ErrorState } from '@/components/ErrorState'
import {
  InlineError,
  panelClass,
  Pill,
  SectionHeading,
  TableScroll,
  tdClass,
  thClass,
} from '@/components/admin/ui'
import { primaryButtonClass } from '@/components/admin/styles'
import {
  adminErrorMessage,
  formatBytes,
  formatPercent,
  useAcquisitionStatus,
  useQbit,
  useSetPaused,
  useWants,
  type AcquisitionStatus,
  type QbitStatus,
} from '@/lib/admin'
import { episodeStateClass, episodeStateLabel } from '@/lib/anime'

const PAUSE_EXPLANATION =
  'While acquisition is paused Arc computes no new wants and sends no new query to Nyaa. Downloads ' +
  'already running still finish and are still ingested, and searches already queued requeue ' +
  'themselves until it is resumed.'

/**
 * The storage hold (spec §4.9 FR-T6), in one sentence with the two numbers in
 * it. It names the remedy because the remedy is *nothing*: unlike the pause
 * beside it, this is not a switch anybody flipped and not one anybody has to
 * flip back — which is the first thing an admin seeing "held" needs told.
 */
function holdExplanation(free: number, floor: number): string {
  return (
    `Acquisition is holding: ${formatBytes(free)} free, floor ${formatBytes(floor)}. ` +
    'It resumes on its own when retention frees room.'
  )
}

const DORMANT_EXPLANATION =
  'Imported shows nobody has touched in Arc yet fetch nothing until someone does (FR-A9); a show ' +
  'that is currently airing is the exception and keeps fetching.'

const WAITING_EXPLANATION =
  'Each user fetches at most K shows at once (FR-A10): what is airing first, then what they ' +
  'touched most recently. The rest wait — visibly, on their own show page — and start as the ' +
  'others finish. Nothing is ever cancelled to make room, so lowering K only stops new starts.'

/**
 * "2 waiting · cap 5". The cap travels with the figure because "2" on its own
 * raises the question it should answer: out of what? Both halves are labelled,
 * because "2 of 5" invites the reading "2 of the 5 that are fetching" — which
 * is the opposite of what it counts. A server with no cap says so in words,
 * since "cap 0" would read as "nothing may fetch".
 */
function waitingStat(status: AcquisitionStatus): string {
  const waiting = String(status.waiting_shows ?? 0)
  const cap = status.slot_cap_k ?? 0
  return cap === 0 ? `${waiting} waiting · no cap` : `${waiting} waiting · cap ${String(cap)}`
}

const WANTS_EXPLANATION =
  'Every want that has not been dropped, across all users. One episode wanted by three people is ' +
  'three rows; acquisition merges them into one download.'

function PausePanel() {
  const status = useAcquisitionStatus()
  const setPaused = useSetPaused()

  if (status.isPending) {
    return (
      <p role="status" className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
        Loading acquisition status…
      </p>
    )
  }

  if (status.isError) {
    return (
      <ErrorState
        className="mt-4"
        message={adminErrorMessage(status.error)}
        pending={status.isFetching}
        onRetry={() => {
          void status.refetch()
        }}
      />
    )
  }

  const paused = status.data.paused
  // A hold is reported only while nothing louder is true: paused already
  // explains why nothing is moving, and two brakes in one line reads as a
  // fault rather than as two facts.
  const held = status.data.storage_held === true
  const pill = paused ? 'paused' : held ? 'held' : 'running'

  return (
    <div className={`mt-4 ${panelClass}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Pill tone={paused || held ? 'warn' : 'ok'}>{pill}</Pill>
          <span className="text-[16px] text-[var(--arc-text)]">
            {paused
              ? 'Acquisition is paused.'
              : held
                ? 'Acquisition is held by free space.'
                : 'Acquisition is running.'}
          </span>
        </div>
        <button
          type="button"
          className={primaryButtonClass}
          disabled={setPaused.isPending}
          aria-pressed={paused}
          onClick={() => {
            setPaused.mutate(!paused)
          }}
        >
          {setPaused.isPending ? 'Working…' : paused ? 'Resume acquisition' : 'Pause acquisition'}
        </button>
      </div>

      {held ? (
        <p className="mt-3 max-w-[66ch] text-[14px] leading-[1.55] text-[var(--arc-warn)]">
          {holdExplanation(status.data.free_bytes ?? 0, status.data.min_free_bytes ?? 0)}
        </p>
      ) : null}

      <p className="mt-3 max-w-[66ch] text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
        {PAUSE_EXPLANATION}
      </p>

      {setPaused.isError ? (
        <InlineError className="mt-2" message={adminErrorMessage(setPaused.error)} />
      ) : null}

      <dl className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {[
          ['Active wants', String(status.data.active_wants)],
          ['Searching', String(status.data.searching)],
          ['Downloading', String(status.data.downloading)],
          ['Dormant imports', String(status.data.dormant_entries ?? 0)],
          // FR-A10. The cap is beside the count because "3 waiting" says
          // nothing on its own — the question it raises is "out of what?".
          ['Waiting for a slot', waitingStat(status.data)],
          ['Retained', formatBytes(status.data.retained_bytes ?? 0)],
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

      <p className="mt-3 max-w-[66ch] text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
        {DORMANT_EXPLANATION}
      </p>

      <p className="mt-2 max-w-[66ch] text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
        {WAITING_EXPLANATION}
      </p>
    </div>
  )
}

function WantsPanel() {
  const wants = useWants()

  return (
    <section className="mt-10">
      <SectionHeading>Active wants</SectionHeading>
      <p className="mt-2 max-w-[66ch] text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
        {WANTS_EXPLANATION}
      </p>

      {wants.isPending ? (
        <p role="status" className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
          Loading wants…
        </p>
      ) : wants.isError ? (
        <ErrorState
          className="mt-4"
          message={adminErrorMessage(wants.error)}
          pending={wants.isFetching}
          onRetry={() => {
            void wants.refetch()
          }}
        />
      ) : wants.data.length === 0 ? (
        <p className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
          Nothing is wanted right now. Wants appear as episodes air for shows people are watching.
        </p>
      ) : (
        <div className="mt-4">
          <TableScroll>
            <table className="min-w-full border-collapse">
              <caption className="sr-only">Active wants</caption>
              <thead>
                <tr>
                  <th className={thClass}>User</th>
                  <th className={thClass}>Show</th>
                  <th className={thClass}>Episode</th>
                  <th className={thClass}>State</th>
                </tr>
              </thead>
              <tbody>
                {wants.data.map((want) => (
                  <tr
                    key={`${String(want.user_id)}-${String(want.episode_id)}`}
                    className="border-t border-[var(--arc-border)]"
                  >
                    <td className={`${tdClass} text-[var(--arc-text-muted)]`}>
                      {want.user_email ?? `user ${String(want.user_id)}`}
                    </td>
                    <td className={tdClass}>
                      <Link to={`/anime/${String(want.anime_id)}`} className="hover:underline">
                        {want.anime_title}
                      </Link>
                    </td>
                    <td className={`${tdClass} whitespace-nowrap`}>{want.episode_number}</td>
                    <td className={tdClass}>
                      <span
                        className={`inline-flex items-center rounded-full border-[0.5px] px-2.5 py-0.5 text-[12px] whitespace-nowrap ${episodeStateClass(want.state)}`}
                      >
                        {episodeStateLabel(want.state)}
                      </span>
                      {want.unavailable_reason === null ? null : (
                        <p className="mt-1 text-[13px] text-[var(--arc-text-muted)]">
                          {want.unavailable_reason}
                        </p>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </div>
      )}
    </section>
  )
}

/**
 * Torrents as qBittorrent reports them, with Arc's episode attached.
 *
 * A batch says so and belongs to no single episode (FR-A11): its files are
 * several episodes' and the rest of the pack sits at priority 0. The size
 * column is the **selected** files either way — Arc's own `wanted_bytes` where
 * it has it, the client's `size` (which already means the same thing) where it
 * does not — because a pack's total is not a figure this panel may show.
 */
function TorrentsTable({ status }: { status: QbitStatus }) {
  if (status.torrents.length === 0) {
    return (
      <p className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
        qBittorrent is holding no torrents.
      </p>
    )
  }

  return (
    <div className="mt-4">
      <TableScroll>
        <table className="min-w-full border-collapse">
          <caption className="sr-only">Torrents</caption>
          <thead>
            <tr>
              <th className={thClass}>Name</th>
              <th className={thClass}>State</th>
              <th className={thClass}>Progress</th>
              <th className={thClass}>Size</th>
              <th className={thClass}>Down</th>
              <th className={thClass}>Up</th>
              <th className={thClass}>Episode</th>
            </tr>
          </thead>
          <tbody>
            {status.torrents.map((torrent) => (
              <tr key={torrent.hash} className="border-t border-[var(--arc-border)]">
                <td className={`${tdClass} max-w-md break-words`}>
                  {torrent.name}
                  {torrent.kind === 'batch' ? (
                    <span className="ml-2 text-[13px] text-[var(--arc-text-muted)]">
                      batch · selected files only
                    </span>
                  ) : null}
                </td>
                <td className={tdClass}>
                  <Pill tone={torrent.progress >= 1 ? 'ok' : 'busy'}>{torrent.state}</Pill>
                </td>
                <td className={`${tdClass} whitespace-nowrap`}>
                  {formatPercent(torrent.progress)}
                </td>
                <td className={`${tdClass} whitespace-nowrap`}>
                  {formatBytes(torrent.wanted_bytes ?? torrent.size)}
                </td>
                <td className={`${tdClass} whitespace-nowrap`}>{formatBytes(torrent.dlspeed)}/s</td>
                <td className={`${tdClass} whitespace-nowrap`}>{formatBytes(torrent.upspeed)}/s</td>
                <td className={tdClass}>
                  {torrent.episode_id === null ? (
                    <span className="text-[13px] text-[var(--arc-text-muted)]">
                      {torrent.kind === 'batch' ? 'several' : 'not linked'}
                    </span>
                  ) : (
                    <span className="whitespace-nowrap">#{torrent.episode_id}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
    </div>
  )
}

function QbitPanel() {
  const qbit = useQbit()

  return (
    <section className="mt-10">
      <SectionHeading>qBittorrent</SectionHeading>

      {qbit.isPending ? (
        <p role="status" className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
          Loading qBittorrent status…
        </p>
      ) : qbit.isError ? (
        <ErrorState
          className="mt-4"
          message={adminErrorMessage(qbit.error)}
          pending={qbit.isFetching}
          onRetry={() => {
            void qbit.refetch()
          }}
        />
      ) : (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Pill tone={qbit.data.reachable ? 'ok' : 'bad'}>
              {qbit.data.reachable ? 'reachable' : 'unreachable'}
            </Pill>
            {qbit.data.version === null ? null : (
              <span className="text-[14px] text-[var(--arc-text-muted)]">
                version {qbit.data.version}
              </span>
            )}
          </div>
          {qbit.data.error === null || qbit.data.error === '' ? null : (
            <InlineError className="mt-2" message={qbit.data.error} />
          )}
          {qbit.data.reachable ? <TorrentsTable status={qbit.data} /> : null}
        </>
      )}
    </section>
  )
}

export function AcquisitionTab() {
  return (
    <div>
      <SectionHeading>Acquisition</SectionHeading>
      <PausePanel />
      <WantsPanel />
      <QbitPanel />
    </div>
  )
}
