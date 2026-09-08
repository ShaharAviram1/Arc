import { NavLink, Outlet } from 'react-router-dom'
import { useAcquisitionStatus } from '@/lib/acquisition'
import { useLogout, useMe } from '@/lib/auth'
import { useReviewSummary } from '@/lib/review'

interface NavItem {
  to: string
  label: string
  adminOnly?: boolean
}

const REVIEW_PATH = '/review'

const PHASE_1_NAV: NavItem[] = [
  { to: '/', label: 'Home' },
  { to: '/schedule', label: 'Schedule' },
  { to: '/search', label: 'Search' },
  { to: '/mal', label: 'MAL' },
]

const PHASE_2_NAV: NavItem[] = [
  { to: '/recs', label: 'Recs' },
  { to: REVIEW_PATH, label: 'Review' },
  { to: '/admin', label: 'Admin', adminOnly: true },
]

function navClass({ isActive }: { isActive: boolean }): string {
  const base =
    'flex items-center justify-between gap-2 rounded-md px-3 py-2 text-sm transition-colors'
  return isActive
    ? `${base} bg-[var(--arc-surface-raised)] text-[var(--arc-text)]`
    : `${base} text-[var(--arc-text-muted)] hover:bg-[var(--arc-surface-raised)] hover:text-[var(--arc-text)]`
}

/**
 * The pending-review count beside the Review entry. Only ever rendered for a
 * positive count, so the sidebar is silent when there is nothing to do; the
 * label spells out what the number means, since a bare "3" tells a screen
 * reader nothing.
 */
function ReviewCountPill({ count }: { count: number }) {
  const label = count === 1 ? '1 file needs review' : `${count} files need review`

  return (
    <span
      aria-label={label}
      className="inline-flex min-w-5 items-center justify-center rounded-full bg-[var(--arc-accent)] px-1.5 text-xs font-medium text-[var(--arc-accent-contrast)]"
    >
      {count}
    </span>
  )
}

/**
 * "Acquisition paused", under the nav, for an admin and only while it is true.
 *
 * A pause is invisible from everywhere else in the app — episodes just stop
 * moving — so the one thing this has to do is stop that being a mystery. No
 * controls yet: M14's admin panel is where pausing and resuming get buttons.
 * `role="status"` so a screen reader is told when it appears mid-session,
 * rather than only on a reload.
 */
function AcquisitionPausedNote() {
  // Nothing while it is loading, and nothing if the poll failed: an absent line
  // reads as "acquisition is running", which is both the common case and the
  // right thing to say when we do not know.
  const { data: acquisition } = useAcquisitionStatus()

  if (!acquisition?.paused) return null

  return (
    <p role="status" className="mt-4 px-3 text-xs text-[var(--arc-text-muted)]">
      Acquisition paused
    </p>
  )
}

function AccountPanel() {
  const { data: me } = useMe()
  const logout = useLogout()

  if (!me) return null

  return (
    <div className="mt-auto border-t border-[var(--arc-border)] pt-4">
      <p className="truncate px-3 text-xs text-[var(--arc-text-muted)]" title={me.email}>
        {me.email}
      </p>
      <button
        type="button"
        onClick={() => {
          logout.mutate()
        }}
        disabled={logout.isPending}
        className="mt-1 block w-full rounded-md px-3 py-2 text-left text-sm text-[var(--arc-text-muted)] transition-colors hover:bg-[var(--arc-surface-raised)] hover:text-[var(--arc-text)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:opacity-60"
      >
        {logout.isPending ? 'Logging out…' : 'Log out'}
      </button>
    </div>
  )
}

export function Layout() {
  const { data: me } = useMe()
  const isAdmin = me?.role === 'admin'
  const phase2Nav = PHASE_2_NAV.filter((item) => !item.adminOnly || isAdmin)

  // No count while it is loading, and none if the poll failed: an absent pill
  // reads as "nothing waiting", which is the safer thing to say when we do not
  // know (M13's review page is where a real answer lives).
  const { data: review } = useReviewSummary()
  const pendingReview = review?.pending ?? 0

  return (
    <div className="flex min-h-full">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-[var(--arc-border)] bg-[var(--arc-surface)] p-4 sm:flex">
        <div className="px-3 pb-4 text-lg font-semibold tracking-tight text-[var(--arc-accent)]">
          Arc
        </div>
        <nav className="flex flex-col gap-1">
          {PHASE_1_NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === '/'} className={navClass}>
              {item.label}
            </NavLink>
          ))}

          <div className="mt-6 mb-1 border-t border-[var(--arc-border)] pt-4">
            <span className="px-3 text-xs font-medium tracking-wide text-[var(--arc-text-muted)] uppercase">
              Phase 2
            </span>
          </div>
          {phase2Nav.map((item) => (
            <NavLink key={item.to} to={item.to} className={navClass}>
              {item.label}
              {item.to === REVIEW_PATH && pendingReview > 0 ? (
                <ReviewCountPill count={pendingReview} />
              ) : null}
            </NavLink>
          ))}
        </nav>

        <AcquisitionPausedNote />

        <AccountPanel />
      </aside>

      <main className="min-w-0 flex-1 p-6">
        <Outlet />
      </main>
    </div>
  )
}
