/**
 * Review queue summary (spec §4.3 FR-L6, roadmap M5).
 *
 * A file the matcher could not link with enough confidence goes to review
 * rather than being auto-linked to a guess. The queue itself is phase-1
 * API-only — the page that works through it is M13 — so all the client needs
 * here is the count, to put a pill on the sidebar's Review entry and stop the
 * pile from being invisible.
 *
 * The count moves when the ingest job runs, not when the viewer clicks, so it
 * is polled on a slow interval instead of invalidated: two minutes is far
 * sooner than anyone notices a new file, and cheap enough to leave running for
 * the life of the session. `retry: false` because a badge is not worth a retry
 * storm; a failed poll simply shows nothing until the next one.
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { apiFetch } from '@/lib/api'
import { useMe } from '@/lib/auth'

/** `GET /api/review/summary` — the server's `ReviewSummaryOut`. */
export interface ReviewSummary {
  /** Media files waiting for this viewer to resolve them. */
  pending: number
}

export const reviewSummaryQueryKey = ['review', 'summary'] as const

/** How long a fetched count is treated as fresh. */
const REVIEW_STALE_TIME_MS = 60_000

/** How often the count is refetched while the app is open. */
const REVIEW_REFETCH_INTERVAL_MS = 120_000

/**
 * The pending-review count for the signed-in viewer.
 *
 * The endpoint needs a session, so the query stays disabled until `me` has
 * answered: firing it while logged out would only ever earn a 401, which the
 * query client reads as the session dying under us.
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
