import { Link, useLocation } from 'react-router-dom'

export function NotFound() {
  const { pathname } = useLocation()

  return (
    <section className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">
        Page not found
      </h1>
      <p className="mt-2 text-sm leading-relaxed text-[var(--arc-text-muted)]">
        Nothing is routed at <code className="text-[var(--arc-text)]">{pathname}</code>.
      </p>
      <Link className="mt-6 inline-block text-sm text-[var(--arc-accent)] hover:underline" to="/">
        Back to home
      </Link>
    </section>
  )
}
