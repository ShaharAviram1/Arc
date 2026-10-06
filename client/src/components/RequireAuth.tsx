import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { Button } from '@/components/ui/Button'
import { useMe } from '@/lib/auth'
import { useDownloadsSession } from '@/offline/useDownloads'
import { useOutboxSession } from '@/offline/useOutbox'
import { useTripAutoKeep } from '@/offline/useTripAutoKeep'

/** Minimal centred placeholder used while the session is being resolved. */
export function AuthPending() {
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div role="status" className="flex items-center gap-3 text-sm text-[var(--arc-text-muted)]">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-[var(--arc-border)] border-t-[var(--arc-text)]" />
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
  // The progress outbox's owner and flush triggers (FR-S8), wired once for
  // every signed-in page — the player included, which renders outside Layout.
  useOutboxSession(me?.id ?? null)
  // And the downloads' (FR-S9): whose episodes are visible and playable, the
  // launch-time check of records against files, and the resume triggers.
  useDownloadsSession(me?.id ?? null)
  // A trip's copies download by themselves while Arc is open (FR-A12, M19);
  // not for the demo account, nor where the browser cannot keep files.
  useTripAutoKeep(me)

  if (isPending) return <AuthPending />

  if (isError) {
    return (
      <div className="flex min-h-full items-center justify-center p-6">
        <div className="max-w-sm text-center">
          <h1 className="text-[24px] font-semibold tracking-[-0.02em] text-[var(--arc-text)]">
            Can’t reach Arc
          </h1>
          <p className="mt-2 text-[14px] text-[var(--arc-text-muted)]">
            The server did not answer. It may be restarting.
          </p>
          <Button
            variant="primary"
            className="mt-5"
            onClick={() => {
              void refetch()
            }}
          >
            Try again
          </Button>
        </div>
      </div>
    )
  }

  if (me === null) return <Navigate to="/login" state={{ from: location }} replace />

  return <Outlet />
}
