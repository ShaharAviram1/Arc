import { Link, Navigate, Outlet } from 'react-router-dom'
import { AuthPending } from '@/components/RequireAuth'
import { useMe } from '@/lib/auth'

/**
 * Admin-only gate. A signed-in non-admin gets a 403-style page rather than a
 * redirect: bouncing them somewhere else would look like the link was broken,
 * when the truthful answer is "you are logged in, just not allowed here".
 */
export function RequireAdmin() {
  const { data: me, isPending } = useMe()

  if (isPending) return <AuthPending />
  if (!me) return <Navigate to="/login" replace />

  if (me.role !== 'admin') {
    return (
      <section className="mx-auto max-w-3xl">
        <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">
          Admins only
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-[var(--arc-text-muted)]">
          This page is limited to administrators. Ask an admin if you need access.
        </p>
        <Link className="mt-6 inline-block text-sm text-[var(--arc-accent)] hover:underline" to="/">
          Back to home
        </Link>
      </section>
    )
  }

  return <Outlet />
}
