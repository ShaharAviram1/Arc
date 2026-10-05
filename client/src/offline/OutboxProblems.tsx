/**
 * The outbox's rows in Watch Now's failure banner (FR-S8 inside FR-W6).
 *
 * Four kinds of row, and none of them is silent about losing anything:
 *
 * - a record the server **rejected**: what it was, when, and the server's
 *   reason, with a dismiss that deletes the record — the only way a rejected
 *   record ever leaves the device;
 * - records made under **another account** on this device: a count and no
 *   button, because they are not this viewer's to delete; they go up when
 *   their owner signs in again;
 * - records still **failing to send** after a while (ten minutes and three
 *   attempts): how many, and the last error — never deleted automatically;
 * - the queue living **in memory only** (no IndexedDB, e.g. a private
 *   window) while something is waiting: a warning that a reload loses it.
 */

import { cx, FOCUS_RING } from '@/components/ui'
import { formatClock } from '@/lib/playback'
import type { OutboxRecord } from '@/offline/outbox'
import type { OutboxProblems } from '@/offline/useOutbox'

const ROW =
  'flex items-baseline gap-2.5 border-t-[0.5px] border-[var(--arc-border)] px-3.5 py-2.5 text-[14px] first:border-t-0'

export const OUTBOX_LABEL = 'Offline progress'

function what(record: OutboxRecord): string {
  if (record.kind === 'completion') return 'Watched to the end'
  if (record.kind === 'unmark') return 'Marked unwatched'
  return `Stopped at ${formatClock(record.position_s ?? 0)}`
}

function when(record: OutboxRecord): string {
  const at = new Date(record.at)
  return Number.isNaN(at.getTime())
    ? ''
    : ` on ${at.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`
}

export function OutboxProblemRows({ problems }: { problems: OutboxProblems }) {
  return (
    <>
      {problems.problems.map((record) => {
        const subject = `${what(record)}${when(record)}`
        return (
          <li key={record.id} className={ROW}>
            <span className="shrink-0 font-semibold text-[var(--arc-text)]">Not synced</span>
            <span className="min-w-0 flex-1 text-[var(--arc-text-muted)]">
              <span className="text-[var(--arc-text)]">{subject}</span>
              {' — '}
              {record.problem}
            </span>
            <button
              type="button"
              aria-label={`Dismiss ${OUTBOX_LABEL}: ${subject}`}
              onClick={() => {
                problems.dismiss(record)
              }}
              className={cx(
                'shrink-0 rounded-full px-2 py-1 text-[14px] leading-none text-[var(--arc-text-muted)] hover:text-[var(--arc-text)]',
                FOCUS_RING,
              )}
            >
              <span aria-hidden>✕</span>
            </button>
          </li>
        )
      })}
      {problems.stuck.count > 0 ? (
        <li className={ROW}>
          <span className="shrink-0 font-semibold text-[var(--arc-text)]">{OUTBOX_LABEL}</span>
          <span className="min-w-0 flex-1 text-[var(--arc-text-muted)]">
            <span className="text-[var(--arc-text)]">
              {problems.stuck.count === 1
                ? '1 offline record not yet synced'
                : `${String(problems.stuck.count)} offline records not yet synced`}
            </span>
            {problems.stuck.lastError !== null ? ` — ${problems.stuck.lastError}` : null}. Kept on
            this device; Arc keeps trying.
          </span>
        </li>
      ) : null}
      {problems.foreign > 0 ? (
        <li className={ROW}>
          <span className="shrink-0 font-semibold text-[var(--arc-text)]">{OUTBOX_LABEL}</span>
          <span className="min-w-0 flex-1 text-[var(--arc-text-muted)]">
            {problems.foreign === 1
              ? '1 record from another account on this device is waiting for it to sign in.'
              : `${String(problems.foreign)} records from another account on this device are waiting for it to sign in.`}
          </span>
        </li>
      ) : null}
      {!problems.persistent && problems.waiting > 0 ? (
        <li className={ROW}>
          <span className="shrink-0 font-semibold text-[var(--arc-text)]">{OUTBOX_LABEL}</span>
          <span className="min-w-0 flex-1 text-[var(--arc-text-muted)]">
            This browser is not storing data, so progress saved while offline is lost if this page
            reloads before the connection is back.
          </span>
        </li>
      ) : null}
    </>
  )
}
