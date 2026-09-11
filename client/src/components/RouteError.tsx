import { Link, useRouteError } from 'react-router-dom'
import { buttonClass } from '@/components/ui'

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  return 'Unknown error'
}

/**
 * Last-resort boundary for a route that threw during render or loading.
 *
 * Same shape as `NotFound`: centred, one sentence, one door. The thrown
 * message is shown because it is the only thing anyone can act on, but it is
 * secondary type — the page is not an error console.
 */
export function RouteError() {
  const error = useRouteError()

  return (
    <section className="mx-auto flex min-h-[52vh] max-w-[52ch] flex-col items-center justify-center text-center">
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        Something went wrong
      </h1>
      <p className="mt-3 text-[16px] leading-[1.6] text-[var(--arc-text-muted)]">
        {messageOf(error)}
      </p>
      <Link className={buttonClass('secondary', 'mt-7')} to="/">
        Back to home
      </Link>
    </section>
  )
}
