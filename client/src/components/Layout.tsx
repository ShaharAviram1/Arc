import { NavLink, Outlet } from 'react-router-dom'

interface NavItem {
  to: string
  label: string
}

const PHASE_1_NAV: NavItem[] = [
  { to: '/', label: 'Home' },
  { to: '/schedule', label: 'Schedule' },
  { to: '/search', label: 'Search' },
  { to: '/mal', label: 'MAL' },
]

const PHASE_2_NAV: NavItem[] = [
  { to: '/recs', label: 'Recs' },
  { to: '/review', label: 'Review' },
  { to: '/admin', label: 'Admin' },
]

function navClass({ isActive }: { isActive: boolean }): string {
  const base = 'block rounded-md px-3 py-2 text-sm transition-colors'
  return isActive
    ? `${base} bg-[var(--arc-surface-raised)] text-[var(--arc-text)]`
    : `${base} text-[var(--arc-text-muted)] hover:bg-[var(--arc-surface-raised)] hover:text-[var(--arc-text)]`
}

export function Layout() {
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
          {PHASE_2_NAV.map((item) => (
            <NavLink key={item.to} to={item.to} className={navClass}>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <main className="min-w-0 flex-1 p-6">
        <Outlet />
      </main>
    </div>
  )
}
