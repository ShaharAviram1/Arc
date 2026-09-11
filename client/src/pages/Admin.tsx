/**
 * Admin (spec §4.10 FR-D1–FR-D4, §4.9 FR-T4, §5, roadmap M14).
 *
 * Five tabs over one role-gated page. The gate is the router's
 * (`RequireAdmin`), not this component's: every endpoint below is admin-only on
 * the server too, so the tab bar is a convenience and never the control.
 *
 * The tab lives in `?tab=`, which is the difference between a page you can send
 * someone and one you can only describe. It is written with `replace`, so
 * flipping through five tabs does not put five entries between the admin and
 * wherever they came from. An unknown or missing value falls back to Users
 * rather than erroring — a mistyped query string should show the page, not a
 * blank.
 *
 * FR-D4 — the match-review queue — is not a tab. That queue already has a page
 * of its own with candidates, LLM suggestions and the confirm flow (M13), and a
 * second, thinner copy of it here would be a worse one. What belongs here is
 * the *pointer*: how many files are waiting, and a way in.
 */

import { Link, useSearchParams } from 'react-router-dom'
import { AcquisitionTab } from '@/components/admin/AcquisitionTab'
import { JobsTab } from '@/components/admin/JobsTab'
import { RulesTab } from '@/components/admin/RulesTab'
import { StorageTab } from '@/components/admin/StorageTab'
import { UsersTab } from '@/components/admin/UsersTab'
import { Chip, cx, FOCUS_RING } from '@/components/ui'
import { ADMIN_TABS, ADMIN_TAB_LABELS, adminTabFrom, type AdminTab } from '@/lib/admin'
import { useReviewSummary } from '@/lib/review'

const EXPLANATION =
  'Everything here applies to the whole server, not to your own account: the accounts that may ' +
  'sign in, the rules acquisition and retention run on, and what the worker is doing with them.'

/** FR-D4: the count, and the way to the queue that already exists (M13). */
function ReviewLink() {
  const { data } = useReviewSummary()
  const pending = data?.pending ?? 0

  return (
    <p className="mt-4 text-[14px] text-[var(--arc-text-muted)]">
      {pending === 0
        ? 'Nothing is waiting in the match-review queue.'
        : `${pending === 1 ? '1 file is' : `${String(pending)} files are`} waiting in the match-review queue.`}{' '}
      <Link
        to="/review"
        className={cx('text-[var(--arc-focus)] underline underline-offset-2', FOCUS_RING)}
      >
        Open the review queue
      </Link>
    </p>
  )
}

export function Admin() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = adminTabFrom(searchParams.get('tab'))

  function select(next: AdminTab) {
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        params.set('tab', next)
        return params
      },
      { replace: true },
    )
  }

  return (
    <section className="mx-auto max-w-6xl">
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        Admin
      </h1>
      <p className="mt-3 max-w-[66ch] text-[16px] leading-[1.6] text-[var(--arc-text-muted)]">
        {EXPLANATION}
      </p>
      <ReviewLink />

      <div className="mt-7 flex flex-wrap gap-2.5">
        {ADMIN_TABS.map((value) => (
          <Chip
            key={value}
            active={value === tab}
            onClick={() => {
              select(value)
            }}
          >
            {ADMIN_TAB_LABELS[value]}
          </Chip>
        ))}
      </div>

      {/* One tab at a time, so the tabs that poll stop polling when they are
          left and a half-edited form does not survive a detour through another
          tab and come back looking authoritative. */}
      <div className="mt-9">
        {tab === 'users' ? (
          <UsersTab />
        ) : tab === 'rules' ? (
          <RulesTab />
        ) : tab === 'jobs' ? (
          <JobsTab />
        ) : tab === 'storage' ? (
          <StorageTab />
        ) : (
          <AcquisitionTab />
        )}
      </div>
    </section>
  )
}
