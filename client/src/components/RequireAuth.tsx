import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useMe } from '@/lib/auth'

/** Minimal centred placeholder used while the session is being resolved. */
export function AuthPending() {
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div role="status" className="flex items-center gap-3 text-sm text-[var(--arc-text-muted)]">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-[var(--arc-border)] border-t-[var(--arc-accent)]" />
        Loading…
      </div>
    </div>
  )
}

/**
 * Gate for everything behind a session. `useMe` never throws on 401, so an
 * error here is a transport failure, which is worth reporting rather than
 * bouncing the user to a login page that cannot reach the server either.
 */
export function RequireAuth() {
  const location = useLocation()
  const { data: me, isPending, isError, refetch } = useMe()

  if (isPending) return <AuthPending />

  if (isError) {
    return (
      <div className="flex min-h-full items-center justify-center p-6">
        <div className="max-w-sm text-center">
          <h1 className="text-lg font-semibold text-[var(--arc-text)]">Can’t reach Arc</h1>
          <p className="mt-2 text-sm text-[var(--arc-text-muted)]">
            The server did not answer. It may be restarting.
          </p>
          <button
            type="button"
            onClick={() => {
              void refetch()
            }}
            className="mt-4 rounded-md bg-[var(--arc-accent)] px-3 py-2 text-sm font-medium text-[var(--arc-accent-contrast)] hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)]"
          >
            Try again
          </button>
        </div>
      </div>
    )
  }

  if (me === null) return <Navigate to="/login" state={{ from: location }} replace />

  return <Outlet />
}
