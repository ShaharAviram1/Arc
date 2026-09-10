/**
 * Match-review data layer (spec §4.3 FR-L4/FR-L5/FR-L6, roadmap M5 and M13).
 *
 * A file the matcher could not link with enough confidence goes to review
 * rather than being auto-linked to a guess, and nothing is played from it
 * until a person says which show it is. This module is everything the Review
 * page and the sidebar badge need to work that queue: the queue itself, the
 * three ways out of it (confirm, ignore, reopen), a catalogue search hung off
 * an item for the show the matcher never proposed, and the request for an LLM
 * suggestion — which is *shown*, never applied (FR-L5).
 *
 * Every mutation invalidates both the queue and the summary count, because
 * both are server arithmetic: a confirm removes a row and drops the badge, an
 * ignore does the same, a reopen puts one back. Nothing here patches a cache
 * optimistically — the queue is small, the round trip is cheap, and a badge
 * that briefly disagrees with the list is worse than one that lags by 200 ms.
 *
 * Confirming or ignoring also moves an *episode* (`matched`, or back to
 * `unavailable` when Arc's own download turns out to be the wrong file), so
 * the anime tree is invalidated with them; a show page left open must not go
 * on claiming the old state.
 */

import { useEffect, useState } from 'react'
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { ApiError, apiFetch } from '@/lib/api'
import {
  ANIME_QUERY_KEY,
  CATALOGUE_UNAVAILABLE_MESSAGE,
  isSearchable,
  type AnimeSearchResponse,
  type AnimeSummary,
} from '@/lib/anime'
import { errorDetail, useMe } from '@/lib/auth'

/* --- Wire shapes ------------------------------------------------------ */

/** The matcher's verdict on a file, as `arc.models.enums.ReviewState`. */
export type ReviewState = 'auto' | 'pending' | 'confirmed' | 'ignored'

/** The three states the queue can be listed in; `confirmed` has its own home. */
export const REVIEW_TABS = ['pending', 'ignored', 'auto'] as const
export type ReviewTab = (typeof REVIEW_TABS)[number]

export const REVIEW_TAB_LABELS: Record<ReviewTab, string> = {
  pending: 'Pending',
  ignored: 'Ignored',
  auto: 'Auto-linked',
}

/** The part of the filename parse the queue renders (server `ParsedOut`). */
export interface ReviewParsed {
  title: string | null
  episode: number | null
  season: number | null
  group: string | null
  resolution: string | null
  kind: string | null
}

/**
 * One scored candidate. `anime` is null — and `reason` set instead — when the
 * matcher had nothing to offer and the entry is a sentence rather than a show.
 */
export interface ReviewCandidate {
  anime: AnimeSummary | null
  episode_number: number | null
  score: number | null
  reasons: string[]
  /** True when the episode number came from the absolute-numbering rule. */
  absolute: boolean
  /** Set instead of `anime`: "no good candidates". */
  reason: string | null
}

/** How sure the model says it is. Never a number: it did not measure one. */
export type SuggestionConfidence = 'high' | 'medium' | 'low'

/**
 * What a language model proposed for a file it was asked about (FR-L5).
 *
 * Never applied by anything: the page renders it, and a person presses
 * Confirm — or does not. `error` is set instead of a proposal when the call
 * failed or the model declined, so a failed ask still has something to say.
 */
export interface ReviewSuggestion {
  anime_id: number | null
  anime: AnimeSummary | null
  episode_number: number | null
  /**
   * One line of argument, in the model's own words. Null exactly when `error`
   * is set: an ask that failed has an explanation, not a case.
   */
  reason: string | null
  /** Null on the same terms as `reason`: nothing was proposed to be sure of. */
  confidence: SuggestionConfidence | null
  /** The model that answered, named verbatim. Null for an older row. */
  model: string | null
  created_at: string
  /** Why there is no proposal, when there is none. */
  error: string | null
}

/** One row of the queue (server `ReviewItem`). Never carries a path. */
export interface ReviewItem {
  id: number
  /** The filename alone. */
  name: string
  /** Its directory relative to `DATA_DIR`; `""` for the library root. */
  directory: string
  size: number | null
  parsed: ReviewParsed
  /** The matcher's score, 0–1. Null once a person has decided. */
  confidence: number | null
  candidates: ReviewCandidate[]
  review_state: ReviewState
  /** The episode this file is linked to, once it is. Null while pending. */
  episode_id: number | null
  created_at: string
  /**
   * Optional on the wire: a server without the suggestion job simply omits
   * the key, and `suggestionOf` is how every reader should get at it so a
   * missing key and an explicit null stay the same thing.
   */
  suggestion?: ReviewSuggestion | null
}

/** `GET /api/review` — the queue, and how much of it is pending. */
export interface ReviewPage {
  items: ReviewItem[]
  /**
   * Always the *pending* count, whatever `?state=` asked for: the sidebar
   * badge renders this and must not change because somebody filtered.
   */
  pending: number
  /**
   * False when the server has no model key or `LLM_MATCH_SUGGESTIONS` is off.
   * Optional for the same reason `suggestion` is; read it through
   * `suggestionsEnabled`.
   */
  suggestions_enabled?: boolean
}

/** `GET /api/review/summary` — the one number the sidebar polls. */
export interface ReviewSummary {
  /** Media files waiting for this viewer to resolve them. */
  pending: number
}

/** `POST /api/review/{id}/confirm` (FR-L6). Both fields are set by hand. */
export interface ConfirmReview {
  id: number
  anime_id: number
  episode_number: number
}

/** `POST /api/review/{id}/suggest` — 202, because a job does the asking. */
export interface SuggestionRequested {
  job_id: number
  status: string
}

/** An item's suggestion, with a server that has none reading as "none". */
export function suggestionOf(item: ReviewItem): ReviewSuggestion | null {
  return item.suggestion ?? null
}

/** Whether this server will ask a model at all (FR-L5's toggle). */
export function suggestionsEnabled(page: ReviewPage): boolean {
  return page.suggestions_enabled ?? false
}

/* --- Query keys ------------------------------------------------------- */

export const REVIEW_QUERY_KEY = 'review'

export const reviewSummaryQueryKey = [REVIEW_QUERY_KEY, 'summary'] as const

/** Every listing of the queue, under one prefix so all of them invalidate. */
export const reviewQueuesQueryKey = [REVIEW_QUERY_KEY, 'queue'] as const

export function reviewQueueQueryKey(state: ReviewTab): readonly unknown[] {
  return [...reviewQueuesQueryKey, state]
}

export function reviewSearchQueryKey(id: number, q: string): readonly unknown[] {
  return [REVIEW_QUERY_KEY, 'search', id, q]
}

/* --- Timings ---------------------------------------------------------- */

/** How long a fetched count is treated as fresh. */
const REVIEW_STALE_TIME_MS = 60_000

/** How often the count is refetched while the app is open. */
const REVIEW_REFETCH_INTERVAL_MS = 120_000

/** Long enough that a typed word is one request, short enough to feel live. */
export const SEARCH_DEBOUNCE_MS = 300

/** How often the queue is re-asked while a suggestion is being generated. */
export const SUGGESTION_POLL_MS = 10_000

/**
 * How long to keep asking. A model call takes seconds; two minutes is the
 * point at which something has gone wrong on the server and polling is only
 * noise. The page still has a Try-again button after it.
 */
export const SUGGESTION_WAIT_MS = 120_000

/* --- Words ------------------------------------------------------------ */

/** The 409 detail that means "somebody linked this while you were looking". */
const ALREADY_LINKED_DETAIL = 'this file is already linked to an episode'

/** The 409 detail `reopen` answers when the file is not ignored any more. */
const NOT_IGNORED_DETAIL = 'only an ignored file can be reopened'

/** The 503 detail that means the server will not ask a model at all. */
const SUGGESTIONS_OFF_DETAIL = 'Suggestions are not enabled'

export const ALREADY_LINKED_MESSAGE =
  'That file is already linked to an episode. Refresh the queue to see where it went.'

export const GONE_MESSAGE = 'That file is no longer in the queue. Refresh to see the current list.'

export const NOT_IGNORED_MESSAGE =
  'That file is not ignored any more. Refresh the queue to see its current state.'

export const SUGGESTIONS_OFF_MESSAGE =
  'Suggestions are switched off on this server. An administrator can enable them.'

/**
 * What to say when a review action fails. Every case is something the person
 * can act on — refresh, wait, ask an admin — except the last, which is at
 * least honest about not knowing.
 */
export function reviewErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Could not reach the server. Try again.'

  const detail = errorDetail(error)
  switch (error.status) {
    case 401:
      return 'Your session expired. Sign in again.'
    case 404:
      return GONE_MESSAGE
    case 409:
      return detail === NOT_IGNORED_DETAIL ? NOT_IGNORED_MESSAGE : ALREADY_LINKED_MESSAGE
    case 502:
      return CATALOGUE_UNAVAILABLE_MESSAGE
    case 503:
      return detail === SUGGESTIONS_OFF_DETAIL ? SUGGESTIONS_OFF_MESSAGE : 'Try again in a moment.'
    default:
      return 'Something went wrong. Try again.'
  }
}

/** Kept so a caller can tell the two 409s apart without re-deriving them. */
export const REVIEW_CONFLICT_DETAILS = {
  alreadyLinked: ALREADY_LINKED_DETAIL,
  notIgnored: NOT_IGNORED_DETAIL,
} as const

/* --- Formatting ------------------------------------------------------- */

const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const

/**
 * "1.4 GB". The exact byte count is never the question — "is this the whole
 * episode or a 40 kB sample?" is — so one decimal below a hundred is plenty.
 */
export function formatSize(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return 'size unknown'

  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  const rounded = unit === 0 || value >= 100 ? Math.round(value) : Number(value.toFixed(1))
  return `${String(rounded)} ${SIZE_UNITS[unit] ?? 'B'}`
}

/**
 * The matcher's score as a percentage. A file whose score has been cleared —
 * which is what confirming does — has nothing to report rather than "0 %".
 */
export function formatConfidence(confidence: number | null): string {
  if (confidence === null || !Number.isFinite(confidence)) return 'no score'
  return `${String(Math.round(confidence * 100))}% sure`
}

/** A candidate's score as a bare percentage; blank when it has none. */
export function formatScore(score: number | null): string {
  if (score === null || !Number.isFinite(score)) return ''
  return `${String(Math.round(score * 100))}%`
}

/** "S2E05", "E05", "movie" — the episode as the parse saw it, or ''. */
export function episodeLabel(parsed: ReviewParsed): string {
  if (parsed.episode === null) return ''
  const episode = `E${String(parsed.episode).padStart(2, '0')}`
  return parsed.season === null ? episode : `S${String(parsed.season)}${episode}`
}

/**
 * The parse as a handful of chips: what the filename claimed, in the order a
 * person reads it. Anything the parser could not find is simply absent — an
 * empty chip says less than no chip.
 */
export function parsedChips(parsed: ReviewParsed): string[] {
  const chips: string[] = []
  if (parsed.title !== null && parsed.title !== '') chips.push(parsed.title)
  const episode = episodeLabel(parsed)
  if (episode !== '') chips.push(episode)
  if (parsed.group !== null && parsed.group !== '') chips.push(parsed.group)
  if (parsed.resolution !== null && parsed.resolution !== '') chips.push(parsed.resolution)
  if (parsed.kind !== null && parsed.kind !== '' && parsed.kind !== 'episode') {
    chips.push(parsed.kind)
  }
  return chips
}

/** Where the file sits, in words. The root of the library has a name here. */
export function directoryLabel(directory: string): string {
  return directory === '' ? 'library root' : directory
}

/**
 * The episode number to start the confirm form at: the candidate's own, else
 * whatever the filename claimed, else nothing for a person to fill in.
 */
export function startingEpisode(
  candidateEpisode: number | null,
  parsed: ReviewParsed,
): number | null {
  return candidateEpisode ?? parsed.episode
}

/** An episode number a person typed, or null when it is not one. */
export function parseEpisodeInput(raw: string): number | null {
  const trimmed = raw.trim()
  if (trimmed === '') return null
  const value = Number(trimmed)
  return Number.isInteger(value) && value >= 1 ? value : null
}

/* --- Queries ---------------------------------------------------------- */

/**
 * The pending-review count for the signed-in viewer.
 *
 * The count moves when the ingest job runs, not when the viewer clicks, so it
 * is polled on a slow interval instead of invalidated. `retry: false` because
 * a badge is not worth a retry storm. The endpoint needs a session, so the
 * query stays disabled until `me` has answered: firing it while logged out
 * would only ever earn a 401, which the query client reads as the session
 * dying under us.
 */
export function useReviewSummary(): UseQueryResult<ReviewSummary, Error> {
  const { data: me } = useMe()

  return useQuery<ReviewSummary, Error>({
    queryKey: reviewSummaryQueryKey,
    queryFn: () => apiFetch<ReviewSummary>('/api/review/summary'),
    staleTime: REVIEW_STALE_TIME_MS,
    refetchInterval: REVIEW_REFETCH_INTERVAL_MS,
    retry: false,
    enabled: me != null,
  })
}

/**
 * One state's worth of the queue.
 *
 * `pollUntil` is an epoch millisecond deadline, set while a suggestion has
 * been asked for and has not arrived: the answer lands in a job, so the only
 * way to see it is to re-ask. Reading the clock inside the interval callback
 * rather than closing over a boolean is what makes the deadline self-ending —
 * the first tick past it returns `false` and the polling stops with no timer
 * of its own.
 */
export function useReviewQueue(
  state: ReviewTab,
  pollUntil: number | null = null,
): UseQueryResult<ReviewPage, Error> {
  return useQuery<ReviewPage, Error>({
    queryKey: reviewQueueQueryKey(state),
    queryFn: () => apiFetch<ReviewPage>(`/api/review?state=${state}`),
    retry: false,
    // Switching tabs must not blank the list under the tabs that did it.
    placeholderData: keepPreviousData,
    refetchInterval: () =>
      pollUntil !== null && Date.now() < pollUntil ? SUGGESTION_POLL_MS : false,
  })
}

/**
 * `value`, but only once it has stopped changing for `delayMs`.
 *
 * Here rather than in the page because the debounce belongs to the request,
 * not to the input: every caller of `useReviewSearch` should cost one call per
 * typed word, and none of them should have to remember to arrange it.
 */
export function useDebounced<T>(value: T, delayMs: number = SEARCH_DEBOUNCE_MS): T {
  const [settled, setSettled] = useState(value)

  useEffect(() => {
    if (settled === value) return
    const timer = setTimeout(() => {
      setSettled(value)
    }, delayMs)
    return () => {
      clearTimeout(timer)
    }
  }, [value, settled, delayMs])

  return settled
}

/**
 * Catalogue search for the show the matcher never proposed (FR-L6).
 *
 * Hung off the item's id, as the server has it, so the client does not have to
 * hold two ideas at once. Each result is upserted on the way past, which is
 * what gives it the internal id `confirm` then takes. `retry: false`: every
 * attempt costs the server a catalogue call, and neither a rejected query nor
 * a failing upstream gets better for being asked twice.
 */
export function useReviewSearch(id: number, q: string): UseQueryResult<AnimeSearchResponse, Error> {
  const query = useDebounced(q.trim())

  return useQuery<AnimeSearchResponse, Error>({
    queryKey: reviewSearchQueryKey(id, query),
    queryFn: () => {
      const params = new URLSearchParams({ q: query })
      return apiFetch<AnimeSearchResponse>(`/api/review/${String(id)}/search?${params.toString()}`)
    },
    enabled: Number.isInteger(id) && id > 0 && isSearchable(query),
    placeholderData: keepPreviousData,
    retry: false,
  })
}

/* --- Mutations -------------------------------------------------------- */

/**
 * Every action here changes the queue, the badge, and — because confirming
 * and ignoring both move an episode — what a show page says about its own
 * state. All three are server-derived, so all three are re-asked rather than
 * patched.
 */
function useReviewMutation<TVariables>(
  request: (variables: TVariables) => Promise<ReviewItem>,
): UseMutationResult<ReviewItem, Error, TVariables> {
  const client = useQueryClient()

  return useMutation<ReviewItem, Error, TVariables>({
    mutationFn: request,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: reviewQueuesQueryKey })
      void client.invalidateQueries({ queryKey: reviewSummaryQueryKey })
      void client.invalidateQueries({ queryKey: [ANIME_QUERY_KEY] })
    },
  })
}

/** Link a file to an episode by hand (FR-L6). 409 when it is already linked. */
export function useConfirmReview(): UseMutationResult<ReviewItem, Error, ConfirmReview> {
  return useReviewMutation<ConfirmReview>(({ id, anime_id, episode_number }) =>
    apiFetch<ReviewItem>(`/api/review/${String(id)}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ anime_id, episode_number }),
    }),
  )
}

/** "Not anime / ignore" (FR-L6): out of the queue, still on disk. */
export function useIgnoreReview(): UseMutationResult<ReviewItem, Error, number> {
  return useReviewMutation<number>((id) =>
    apiFetch<ReviewItem>(`/api/review/${String(id)}/ignore`, { method: 'POST' }),
  )
}

/** Put an ignored file back in the queue. Only from `ignored`, hence the 409. */
export function useReopenReview(): UseMutationResult<ReviewItem, Error, number> {
  return useReviewMutation<number>((id) =>
    apiFetch<ReviewItem>(`/api/review/${String(id)}/reopen`, { method: 'POST' }),
  )
}

/**
 * Ask a model what this file probably is (FR-L5). The answer arrives in a
 * job, so the 202 says only that the asking has started; the queue is
 * invalidated with it for consistency with the other actions, and the page
 * polls until the suggestion appears.
 *
 * Nothing this returns is ever applied: the suggestion is rendered, and a
 * person presses Confirm.
 */
export function useRequestSuggestion(): UseMutationResult<SuggestionRequested, Error, number> {
  const client = useQueryClient()

  return useMutation<SuggestionRequested, Error, number>({
    mutationFn: (id) =>
      apiFetch<SuggestionRequested>(`/api/review/${String(id)}/suggest`, { method: 'POST' }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: reviewQueuesQueryKey })
      void client.invalidateQueries({ queryKey: reviewSummaryQueryKey })
    },
  })
}
