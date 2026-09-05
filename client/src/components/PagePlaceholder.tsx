import type { ReactNode } from 'react'

interface PagePlaceholderProps {
  title: string
  description: string
  children?: ReactNode
}

/** M0 stand-in: every page renders its name and its one-line brief from spec.md §5. */
export function PagePlaceholder({ title, description, children }: PagePlaceholderProps) {
  return (
    <section className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">{title}</h1>
      <p className="mt-2 text-sm leading-relaxed text-[var(--arc-text-muted)]">{description}</p>
      {children ? <div className="mt-6">{children}</div> : null}
    </section>
  )
}
