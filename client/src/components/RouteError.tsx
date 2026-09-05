import { Link, useRouteError } from 'react-router-dom'

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  return 'Unknown error'
}

/** Last-resort boundary for a route that threw during render or loading. */
export function RouteError() {
  const error = useRouteError()

  return (
    <section className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">
        Something went wrong
      </h1>
      <p className="mt-2 text-sm leading-relaxed text-[var(--arc-text-muted)]">
        {messageOf(error)}
      </p>
      <Link className="mt-6 inline-block text-sm text-[var(--arc-accent)] hover:underline" to="/">
        Back to home
      </Link>
    </section>
  )
}
