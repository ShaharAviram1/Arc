import type { ReactNode } from 'react'
import { FIELD_ERROR_CLASS } from '@/components/ui'

interface AuthShellProps {
  title: string
  subtitle?: ReactNode
  children: ReactNode
  footer?: ReactNode
}

/**
 * Centred card shared by the two chrome-less auth pages (Login, Invite).
 *
 * The logo rather than the word "Arc": these are the only two screens with no
 * toolbar, so the mark is the only thing saying whose sign-in this is. It is
 * the same 48px lockup the toolbar carries — the README's minimum, below which
 * the lightning forks sinter into a single line.
 */
export function AuthShell({ title, subtitle, children, footer }: AuthShellProps) {
  return (
    <div className="flex min-h-full items-center justify-center px-6 py-12">
      <div className="w-full max-w-[400px]">
        <div className="flex justify-center pb-8">
          <img src="/arc-logo.png" alt="Arc" className="h-12 w-auto" />
        </div>

        <div className="rounded-hero border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface)] p-7">
          <h1 className="text-[24px] leading-tight font-semibold tracking-[-0.02em] text-[var(--arc-text)]">
            {title}
          </h1>
          {subtitle ? (
            <p className="mt-2 text-[14px] leading-[1.55] text-[var(--arc-text-muted)]">
              {subtitle}
            </p>
          ) : null}
          <div className="mt-6">{children}</div>
        </div>

        {footer ? (
          <p className="mt-5 text-center text-[13px] text-[var(--arc-text-muted)]">{footer}</p>
        ) : null}
      </div>
    </div>
  )
}

/** Inline field error / form error, announced to assistive tech. */
export function FormError({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className={`mt-4 ${FIELD_ERROR_CLASS}`}>
      {children}
    </p>
  )
}
