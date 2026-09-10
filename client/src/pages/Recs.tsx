import { useState } from 'react'
import { Link } from 'react-router-dom'
import { CoverThumb } from '@/components/CoverThumb'
import { ErrorState } from '@/components/ErrorState'
import { ListStatusControl } from '@/components/ListStatusControl'
import { SourceBadge } from '@/components/SourceBadge'
import { summaryLine } from '@/lib/anime'
import { useMe } from '@/lib/auth'
import {
  chainLabel,
  continuationsOf,
  formatRelativeTime,
  MAX_PROMPT_LENGTH,
  NOT_CONFIGURED_MESSAGE,
  promptLabel,
  recsErrorMessage,
  runSummary,
  useRecs,
  useRunRecs,
  type RecContinuation,
  type RecPick,
  type RecRun,
} from '@/lib/recs'

/** What this page does with a list and an API key, said before anyone runs it. */
const EXPLANATION =
  'Arc sends a language model a summary of your list — what you rated highly, what you finished recently, ' +
  'what you dropped — along with a pool of shows you have not seen, and asks for three to five ' +
  'picks with a short case for each. Nothing on your list is recommended back to you except ' +
  'things you had already planned. Every run is kept, so this page is instant when you come back.'

const EMPTY_STATE =
  'No picks yet. Describe a mood if you have one, or ask for nothing in particular — the model gets ' +
  'the same list either way.'

const PLACEHOLDER = 'something short and funny · like Mushishi'

const CONTINUATIONS_INTRO =
  'Sequels, movies and spin-offs of shows on your list that you have not added yet.'

/** A run takes ten to thirty seconds, so the button has to say it is working. */
const RUNNING_LABEL = 'Finding picks…'

const primaryButtonClass =
  'inline-flex items-center gap-2 rounded-md bg-[var(--arc-accent)] px-3 py-1.5 text-sm font-medium text-[var(--arc-accent-contrast)] transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-60'

const textareaClass =
  'w-full resize-y rounded-md border border-[var(--arc-border)] bg-[var(--arc-bg)] px-3 py-2 text-sm text-[var(--arc-text)] placeholder:text-[var(--arc-text-muted)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)] disabled:opacity-60'

/** The same ring `RequireAuth` spins, tinted for the accent button it sits on. */
function Spinner() {
  return (
    <span
      aria-hidden
      className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-[var(--arc-accent-contrast)]/40 border-t-[var(--arc-accent-contrast)]"
    />
  )
}

function remainingLabel(remaining: number, limit: number): string {
  return `${String(remaining)} of ${String(limit)} left today`
}

/**
 * One pick: the show, and the argued case for it (FR-R3). The case is the
 * point of the card, so it gets body type rather than the muted caption the
 * secondary line uses, and the status select is right there — adding a pick to
 * "plan to watch" is meant to be one click (FR-R4).
 */
function PickCard({ pick }: { pick: RecPick }) {
  const { anime } = pick
  const secondary = summaryLine(anime)
  const href = `/anime/${String(anime.id)}`

  return (
    <article className="flex gap-4 rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] p-4">
      <Link to={href} className="block w-20 shrink-0 sm:w-24">
        <CoverThumb url={anime.cover_url} className="aspect-[2/3] w-full rounded-md" />
      </Link>

      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <Link
          to={href}
          className="leading-snug font-medium text-[var(--arc-text)] hover:text-[var(--arc-accent)]"
        >
          {anime.title.preferred}
        </Link>
        {secondary === '' ? null : (
          <p className="text-xs text-[var(--arc-text-muted)]">{secondary}</p>
        )}
        <SourceBadge source={anime.source} />
        <p className="text-sm leading-relaxed text-[var(--arc-text)]">{pick.case}</p>
        <ListStatusControl
          animeId={anime.id}
          status={anime.list_status}
          label={`List status for ${anime.title.preferred}`}
          className="mt-auto max-w-[14rem] pt-1"
        />
      </div>
    </article>
  )
}

/**
 * One continuation: a show following on from something already on the list.
 * Compact by design — a row, not a card — because the argument for it is one
 * line and it must not compete with the picks above.
 */
function ContinuationRow({ entry }: { entry: RecContinuation }) {
  const { anime } = entry
  const href = `/anime/${String(anime.id)}`

  return (
    <li className="flex items-center gap-3 rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] p-3">
      <Link to={href} className="block w-12 shrink-0">
        <CoverThumb url={anime.cover_url} className="aspect-[2/3] w-full rounded" />
      </Link>

      <div className="min-w-0 flex-1">
        <Link
          to={href}
          className="text-sm leading-snug font-medium text-[var(--arc-text)] hover:text-[var(--arc-accent)]"
        >
          {anime.title.preferred}
        </Link>
        <p className="mt-0.5 text-xs text-[var(--arc-text-muted)]">{entry.because}</p>
      </div>

      <ListStatusControl
        animeId={anime.id}
        status={anime.list_status}
        label={`List status for ${anime.title.preferred}`}
        className="w-36 shrink-0"
      />
    </li>
  )
}

/**
 * Shows that follow on from the viewer's own list (a sequel to something they
 * finished, a film of something they are watching). Rendered only when the
 * run has some: an empty heading over an empty list is worse than silence.
 */
function ContinuationsPanel({ entries }: { entries: RecContinuation[] }) {
  if (entries.length === 0) return null

  return (
    <section className="mt-8">
      <h2 className="text-lg font-semibold tracking-tight text-[var(--arc-text)]">
        New in your franchises
      </h2>
      <p className="mt-1 max-w-3xl text-sm text-[var(--arc-text-muted)]">{CONTINUATIONS_INTRO}</p>
      <ul className="mt-4 flex flex-col gap-2">
        {entries.map((entry) => (
          <ContinuationRow key={entry.anime.id} entry={entry} />
        ))}
      </ul>
    </section>
  )
}

/**
 * The stored newest run (FR-R5). It has no button of its own: "Get picks"
 * above is the only way to start a run, and since the box is pre-filled with
 * this run's own mood, pressing it on an untouched box *is* the spec's
 * refresh — one control, and no question about which mood it would use.
 */
function RunPanel({ run }: { run: RecRun }) {
  return (
    <section className="mt-8">
      <div className="min-w-0">
        <h2 className="text-lg font-semibold tracking-tight text-[var(--arc-text)]">
          Picks for: {promptLabel(run.prompt)}{' '}
          <span className="text-sm font-normal text-[var(--arc-text-muted)]">
            · {formatRelativeTime(run.created_at)}
          </span>
        </h2>
        <p className="mt-1 text-xs text-[var(--arc-text-muted)]">{runSummary(run)}</p>
      </div>

      {run.picks.length === 0 ? (
        <p className="mt-3 text-sm text-[var(--arc-text-muted)]">
          That run came back with nothing. Try again, or describe a different mood.
        </p>
      ) : (
        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          {run.picks.map((pick) => (
            <PickCard key={pick.anime.id} pick={pick} />
          ))}
        </div>
      )}

      {/* Below the picks, and independent of them: a run that argued for
          nothing can still have found a sequel worth naming. */}
      <ContinuationsPanel entries={continuationsOf(run)} />
    </section>
  )
}

/**
 * Recommendations (spec §4.8 FR-R1/FR-R4/FR-R5, §5, roadmap M12).
 *
 * The whole page is one stored run plus the form that replaces it. The GET is
 * cheap and cached, so arriving here is instant; the POST is the ten-to-thirty
 * second one, which is why its failures are shown next to the button that
 * caused them rather than replacing the picks a person can still read.
 */
export function Recs() {
  const { data, isPending, isError, isFetching, error, refetch } = useRecs()
  const { data: me } = useMe()
  const runRecs = useRunRecs()

  const [prompt, setPrompt] = useState('')
  // Which run the box was last filled from. Syncing on the run's *id* rather
  // than on every render is what lets the box be both pre-filled and typed
  // in: a refetch, or a status change patching the cache, re-renders this
  // component with the same run and must not eat what is half-typed. A new
  // run — the one this page just made — is a new subject, so the box follows
  // it. Adjusting state during render is React's own answer to "reset state
  // when a prop changes"; it re-renders before committing, so nothing flashes.
  const [filledFrom, setFilledFrom] = useState<number | null>(null)
  const runId = data?.run?.id ?? null
  if (runId !== filledFrom) {
    setFilledFrom(runId)
    setPrompt(data?.run?.prompt ?? '')
  }

  // An all-whitespace mood is not a mood: the server is told there was none.
  function start(mood: string) {
    const trimmed = mood.trim()
    runRecs.mutate(trimmed === '' ? null : trimmed)
  }

  if (isPending) {
    return (
      <section className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">
          Recommendations
        </h1>
        <p role="status" className="mt-4 text-sm text-[var(--arc-text-muted)]">
          Loading…
        </p>
      </section>
    )
  }

  if (isError) {
    return (
      <section className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">
          Recommendations
        </h1>
        <ErrorState
          className="mt-4"
          message={recsErrorMessage(error)}
          pending={isFetching}
          onRetry={() => {
            void refetch()
          }}
        />
      </section>
    )
  }

  const exhausted = data.remaining_today <= 0
  const run = data.run
  // Operational detail, and only for the person who can act on it. The server
  // sends the chain to admins alone; gating on the role as well means a stray
  // field could never leak it to a viewer.
  const models = me?.role === 'admin' ? chainLabel(data.chain ?? []) : ''

  return (
    <section className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">
        Recommendations
      </h1>
      <p className="mt-2 max-w-3xl text-sm leading-relaxed text-[var(--arc-text-muted)]">
        {EXPLANATION}
      </p>

      {!data.configured ? (
        <p className="mt-4 max-w-3xl rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-2 text-sm text-[var(--arc-text-muted)]">
          {NOT_CONFIGURED_MESSAGE}
        </p>
      ) : (
        <form
          className="mt-6 max-w-2xl"
          onSubmit={(event) => {
            event.preventDefault()
            start(prompt)
          }}
        >
          <label htmlFor="recs-prompt" className="block text-sm font-medium text-[var(--arc-text)]">
            Mood (optional)
          </label>
          <textarea
            id="recs-prompt"
            rows={2}
            maxLength={MAX_PROMPT_LENGTH}
            placeholder={PLACEHOLDER}
            value={prompt}
            disabled={runRecs.isPending}
            onChange={(event) => {
              setPrompt(event.target.value)
            }}
            className={`mt-1 ${textareaClass}`}
          />
          <p className="mt-1 text-right text-xs text-[var(--arc-text-muted)]">
            {prompt.length}/{MAX_PROMPT_LENGTH}
          </p>

          <div className="mt-2 flex flex-wrap items-center gap-3">
            <button
              type="submit"
              className={primaryButtonClass}
              disabled={runRecs.isPending || exhausted}
            >
              {runRecs.isPending ? <Spinner /> : null}
              {runRecs.isPending ? RUNNING_LABEL : 'Get picks'}
            </button>
            <span className="text-sm text-[var(--arc-text-muted)]">
              {remainingLabel(data.remaining_today, data.limit_per_day)}
            </span>
          </div>

          {models === '' ? null : (
            <p className="mt-2 text-xs text-[var(--arc-text-muted)]">Models: {models}</p>
          )}

          {runRecs.isError ? (
            <p role="alert" className="mt-2 text-sm text-[var(--arc-error)]">
              {recsErrorMessage(runRecs.error)}
            </p>
          ) : null}
        </form>
      )}

      {run === null ? (
        <p className="mt-8 max-w-3xl text-sm text-[var(--arc-text-muted)]">{EMPTY_STATE}</p>
      ) : (
        <RunPanel run={run} />
      )}
    </section>
  )
}
