import { Link, useLocation } from 'react-router-dom'
import { buttonClass } from '@/components/ui'

/**
 * A dead route, said calmly and once.
 *
 * Centred rather than top-left, and with exactly one way out: a 404 is not a
 * page with content to lay out, it is a sentence and a door.
 */
export function NotFound() {
  const { pathname } = useLocation()

  return (
    <section className="mx-auto flex min-h-[52vh] max-w-[46ch] flex-col items-center justify-center text-center">
      <h1 className="text-[40px] leading-[1.08] font-semibold tracking-[-0.028em] text-[var(--arc-text)]">
        Page not found
      </h1>
      <p className="mt-3 text-[16px] leading-[1.6] text-[var(--arc-text-muted)]">
        Nothing is routed at{' '}
        <code className="font-mono text-[14px] text-[var(--arc-text)]">{pathname}</code>.
      </p>
      <Link className={buttonClass('secondary', 'mt-7')} to="/">
        Back to home
      </Link>
    </section>
  )
}
