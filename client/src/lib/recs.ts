/**
 * Recommendations data layer (spec §4.8 FR-R1, FR-R4, FR-R5, roadmap M12).
 *
 * One query and one mutation. The query is the *stored* newest run (FR-R5) —
 * so arriving at the page costs nothing and shows last night's picks — plus
 * the two numbers the page needs to know whether a run can be asked for at
 * all: how many are left today, and whether the server has a key.
 *
 * The mutation is the expensive half: the server builds a candidate pool,
 * calls the recommendation model and stores the result, which takes ten to thirty seconds. Its
 * answer *is* the new newest run, so it is written straight into the query's
 * cache rather than triggering a refetch — the only thing the server would
 * tell us that the response does not is the decremented counter, and that is
 * arithmetic we can do here.
 *
 * A pick's `list_status` is kept current by `applyListStatus` in `anime.ts`,
 * which patches this cache along with every other view of a show. That is why
 * adding a pick to "plan to watch" (FR-R4) leaves the select where the person
 * put it instead of snapping back.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { ApiError, apiFetch } from '@/lib/api'
// Type-only, and erased under `verbatimModuleSyntax`: `anime.ts` imports
// values from here, so this must never become a runtime import.
import type { AnimeSummary } from '@/lib/anime'
import { errorDetail } from '@/lib/auth'

/** One recommendation: the show, and the model's argued case for it (FR-R3). */
export interface RecPick {
  anime: AnimeSummary
  /** Two to four sentences referencing the viewer's own history. */
  case: string
}

/**
 * A sequel, movie or spin-off of something already on the viewer's list that
 * they have not added yet. Not a recommendation — no case is argued for it,
 * because the argument is just "you watched the other one" — so it gets its
 * own quieter section rather than competing with the picks.
 */
export interface RecContinuation {
  anime: AnimeSummary
  /** One line naming the show on the list that this follows from. */
  because: string
}

/**
 * One model in the fallback chain, in the order the server tries them. Admin
 * only: it is operational detail, not something a viewer can act on.
 */
export interface RecModelStatus {
  provider: string
  model: string
  /**
   * False when this model is out of quota for the day. It is a resting model,
   * not a broken one — the chain simply moves to the next — so it must never
   * be shown as an error.
   */
  available: boolean
}

/** A stored run — one call to the model and what came back (FR-R5). */
export interface RecRun {
  id: number
  /** The mood the person typed, or null when they asked for nothing special. */
  prompt: string | null
  created_at: string
  /**
   * The model that answered, shown verbatim in the run's summary line so a
   * run can be told apart from one made under a different model. Null for a
   * run stored before the model was recorded, and then simply left out.
   */
  model: string | null
  /**
   * How many shows the pool held (spec §4.8 FR-R2). Shown as the "of N
   * candidates" half of the summary line: a handful of picks out of forty
   * reads as a choice, where the picks alone read as all there was.
   */
  candidate_count: number
  picks: RecPick[]
  /**
   * Shows following on from the viewer's own list. Optional on the wire: runs
   * stored before continuations existed simply omit the key, and `continuationsOf`
   * is how every reader should get at it so a missing key and an empty list
   * stay the same thing.
   */
  continuations?: RecContinuation[]
}

/** A run's continuations, with a run made before they existed reading as none. */
export function continuationsOf(run: RecRun): RecContinuation[] {
  return run.continuations ?? []
}

/** `GET /api/recs` — the newest run and what the viewer may still ask for. */
export interface RecsPage {
  /** Null until this viewer has ever run one. */
  run: RecRun | null
  remaining_today: number
  limit_per_day: number
  /** False when the server has no model API key, so a run cannot be offered. */
  configured: boolean
  /**
   * The fallback chain, in the order the server tries it. Sent **only** to
   * admins — absent rather than null for everyone else. The page still gates
   * on the viewer's role: an absent key and a non-admin viewer have to agree,
   * and the role is the half this client can reason about.
   */
  chain?: RecModelStatus[]
}

export const RECS_QUERY_KEY = 'recs'

export const recsQueryKey = [RECS_QUERY_KEY] as const

/** The server rejects anything longer with a 422; the textarea stops first. */
export const MAX_PROMPT_LENGTH = 300

/** The 503 detail that means "no key", as opposed to "the model said no". */
const NOT_CONFIGURED_DETAIL = 'Recommendations are not configured'

export const NOT_CONFIGURED_MESSAGE =
  'Recommendations need a model API key on the server. An administrator has to add one ' +
  'before this page can ask for anything.'

const REFUSAL_MESSAGE =
  'The model declined this request. Try describing the mood differently, or run without a prompt.'

const UPSTREAM_MESSAGE = 'The recommendation service could not be reached. Try again in a minute.'

const TOO_LONG_MESSAGE = `That mood is too long — ${String(MAX_PROMPT_LENGTH)} characters at most.`

const NOTHING_TO_WORK_FROM_MESSAGE = 'Nothing to recommend from: add or rate a few shows first.'

/**
 * How long to wait, in the words a person would use. Seconds are never worth
 * showing — the limit resets on a daily boundary — so anything under a couple
 * of minutes rounds up to "a minute" rather than counting down.
 */
export function formatWait(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 90) return 'a minute'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${String(minutes)} minutes`
  const hours = Math.round(minutes / 60)
  return hours <= 1 ? 'an hour' : `${String(hours)} hours`
}

/** The seconds a 429 says to wait: the body's field, else the header. */
function retryAfterSeconds(error: ApiError): number {
  const body: unknown = error.body
  if (typeof body === 'object' && body !== null && 'retry_after_seconds' in body) {
    const seconds: unknown = body.retry_after_seconds
    if (typeof seconds === 'number' && Number.isFinite(seconds)) return seconds
  }
  return error.retryAfter ?? 0
}

/**
 * What to say when a run could not be made. Every case here is something the
 * person can act on — wait, rephrase, watch a few things first — except the
 * upstream one, which is at least honest about whose fault it is.
 */
export function recsErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Could not reach the server. Try again.'

  switch (error.status) {
    case 401:
      return 'Your session expired. Sign in again.'
    case 409:
      return errorDetail(error) ?? NOTHING_TO_WORK_FROM_MESSAGE
    case 422:
      return TOO_LONG_MESSAGE
    case 429:
      return `Daily limit reached; try again in about ${formatWait(retryAfterSeconds(error))}.`
    case 502:
      return UPSTREAM_MESSAGE
    case 503:
      return errorDetail(error) === NOT_CONFIGURED_DETAIL ? NOT_CONFIGURED_MESSAGE : REFUSAL_MESSAGE
    default:
      return 'Something went wrong. Try again.'
  }
}

/**
 * Units and how many of each fit in the next one up. The last row is open
 * ended, so the walk below always terminates on it.
 */
const RELATIVE_UNITS: readonly (readonly [Intl.RelativeTimeFormatUnit, number])[] = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', 7],
  ['week', 4.34524],
  ['month', 12],
]

/**
 * "2 hours ago", "yesterday". A run's exact timestamp is never the question —
 * "is this from before I finished that show?" is — so the coarse form reads
 * better than a date. `now` is a parameter so the tests do not race the clock.
 */
export function formatRelativeTime(iso: string, now: number = Date.now()): string {
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return 'just now'

  const format = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  let delta = (at - now) / 1000

  for (const [unit, size] of RELATIVE_UNITS) {
    if (Math.abs(delta) < size) return format.format(Math.round(delta), unit)
    delta /= size
  }
  return format.format(Math.round(delta), 'year')
}

/**
 * "Picks for: something short and funny" / "Picks for: no particular mood".
 * A run with no prompt is the common case and deserves words rather than a
 * blank after the colon.
 */
export function promptLabel(prompt: string | null): string {
  return prompt === null || prompt.trim() === '' ? 'no particular mood' : prompt
}

/**
 * "Picked 4 of 40 candidates · claude-opus-5" — what the run actually did,
 * under the heading that says what it was for. The pool size is the honest
 * part: it says these few were chosen out of many rather than being all the
 * server could find. The model is whatever the server reports, named rather
 * than interpreted, and omitted when there is none to name.
 */
export function runSummary(run: RecRun): string {
  const parts = [`Picked ${String(run.picks.length)} of ${String(run.candidate_count)} candidates`]
  if (run.model !== null && run.model !== '') parts.push(run.model)
  return parts.join(' · ')
}

/**
 * One model in the chain as an admin reads it: "gemini-2.5-flash (resting
 * until tomorrow)". The provider is named only when it is not already the
 * prefix of the model id — "openai/gpt-5-mini via openrouter" says something,
 * "gemini-2.5-flash via gemini" does not.
 *
 * A model that is out of quota is *resting*, never failing: the chain moving
 * past it is the design working, and an admin glancing at this line should
 * read "today's allowance is spent", not "something is broken".
 */
export function modelStatusLabel(status: RecModelStatus): string {
  const provider = status.provider.trim()
  const named =
    provider !== '' && !status.model.toLowerCase().startsWith(provider.toLowerCase())
      ? `${status.model} via ${provider}`
      : status.model
  return status.available ? `${named} ✓` : `${named} (resting until tomorrow)`
}

/**
 * The whole chain on one line, in the order it is tried. An empty chain gives
 * an empty string, which the page renders as nothing at all rather than as a
 * label with nothing after it.
 */
export function chainLabel(chain: readonly RecModelStatus[]): string {
  return chain.map(modelStatusLabel).join(' · ')
}

/**
 * The stored run and the day's allowance. `retry: false` because none of the
 * failures here get better by being asked twice in a row, and the page offers
 * an explicit "Try again" instead.
 */
export function useRecs(): UseQueryResult<RecsPage, Error> {
  return useQuery<RecsPage, Error>({
    queryKey: recsQueryKey,
    queryFn: () => apiFetch<RecsPage>('/api/recs'),
    retry: false,
  })
}

/**
 * Ask for a new run (FR-R5's "refresh"). Takes the mood prompt, or null for
 * "surprise me"; the caller trims, since an all-whitespace prompt is not a
 * mood. The answer replaces the stored run and costs one of the day's runs,
 * both of which are applied to the cache directly — a refetch would only
 * re-read what we already hold.
 */
export function useRunRecs(): UseMutationResult<RecRun, Error, string | null> {
  const client = useQueryClient()

  return useMutation<RecRun, Error, string | null>({
    mutationFn: (prompt) =>
      apiFetch<RecRun>('/api/recs/runs', {
        method: 'POST',
        body: JSON.stringify({ prompt }),
      }),
    onSuccess: (run) => {
      client.setQueryData<RecsPage>(recsQueryKey, (current) =>
        current === undefined
          ? current
          : { ...current, run, remaining_today: Math.max(0, current.remaining_today - 1) },
      )
    },
    onError: (error) => {
      // A 429 means the server's count of today's runs and ours have diverged
      // — a run made in another tab, or a day that rolled over. Ours is the
      // stale one, and it is the number on screen, so re-ask rather than let
      // "3 left today" sit above a refusal to make one.
      if (error instanceof ApiError && error.status === 429) {
        void client.invalidateQueries({ queryKey: recsQueryKey })
      }
    },
  })
}
