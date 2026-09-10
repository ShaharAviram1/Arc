/**
 * The one way a page says "that request failed".
 *
 * Every page had grown its own `<p role="alert">`, which meant the same
 * sentence in the same colour six times over and — more to the point — a dead
 * end: a failed load could only be recovered by reloading the whole page. The
 * button here is the same offer `RequireAuth` makes when it cannot reach the
 * server, so "Try again" means the same thing everywhere in Arc.
 *
 * `role="alert"` sits on the wrapper rather than the sentence so that assistive
 * tech announces the message and the way out together; the message keeps its
 * own element so a page can still be matched on the text alone.
 *
 * `onRetry` is optional because not every failure has a second attempt worth
 * making — a 404 is not going to change — and `pending` exists so a query that
 * is already refetching does not collect a queue of clicks.
 */

const RETRY_LABEL = 'Try again'

export interface ErrorStateProps {
  message: string
  /** Usually a query's `refetch`. Omitted when asking again cannot help. */
  onRetry?: () => void
  /** True while the retry is in flight; the button goes quiet. */
  pending?: boolean
  /** Spacing, which belongs to the page around it, not to this component. */
  className?: string
}

export function ErrorState({ message, onRetry, pending = false, className }: ErrorStateProps) {
  return (
    <div role="alert" className={className}>
      <p className="text-sm text-[var(--arc-error)]">{message}</p>
      {onRetry === undefined ? null : (
        <button
          type="button"
          disabled={pending}
          onClick={onRetry}
          className="mt-2 rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-1.5 text-sm text-[var(--arc-text)] transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60"
        >
          {RETRY_LABEL}
        </button>
      )}
    </div>
  )
}
