import type { ReactNode } from 'react'

interface AuthShellProps {
  title: string
  subtitle?: ReactNode
  children: ReactNode
  footer?: ReactNode
}

/** Centred card shared by the two chrome-less auth pages (Login, Invite). */
export function AuthShell({ title, subtitle, children, footer }: AuthShellProps) {
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <div className="pb-6 text-center text-xl font-semibold tracking-tight text-[var(--arc-accent)]">
          Arc
        </div>
        <div className="rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] p-6">
          <h1 className="text-lg font-semibold tracking-tight text-[var(--arc-text)]">{title}</h1>
          {subtitle ? (
            <p className="mt-1 text-sm leading-relaxed text-[var(--arc-text-muted)]">{subtitle}</p>
          ) : null}
          <div className="mt-5">{children}</div>
        </div>
        {footer ? (
          <p className="mt-4 text-center text-xs text-[var(--arc-text-muted)]">{footer}</p>
        ) : null}
      </div>
    </div>
  )
}

/** Inline field error / form error, announced to assistive tech. */
export function FormError({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="mt-3 text-sm text-[var(--arc-error)]">
      {children}
    </p>
  )
}

export const fieldClass =
  'mt-1 w-full rounded-md border border-[var(--arc-border)] bg-[var(--arc-bg)] px-3 py-2 text-sm text-[var(--arc-text)] placeholder:text-[var(--arc-text-muted)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)] disabled:opacity-60 read-only:text-[var(--arc-text-muted)]'

export const labelClass = 'block text-sm font-medium text-[var(--arc-text)]'

export const submitClass =
  'mt-5 w-full rounded-md bg-[var(--arc-accent)] px-3 py-2 text-sm font-medium text-[var(--arc-accent-contrast)] transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'
