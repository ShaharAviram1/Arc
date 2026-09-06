/**
 * Catalogue source health for the admin (architecture.md §5b, roadmap M14).
 *
 * The AniList → MAL fallback is deliberately invisible to a viewer: search
 * keeps answering, show pages keep rendering, air dates just come back
 * estimated. `GET /api/catalog/status` is where an admin finds out which
 * source is actually answering and what the other one said when it failed.
 *
 * Admin-only on the server. There is no UI for it yet — M14's admin panel is
 * the consumer; this is the typed hook it will call.
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { apiFetch } from '@/lib/api'

/** One source's circuit-breaker state, as `SourceStatusOut` sends it. */
export interface SourceStatus {
  /** `"open"` while Arc is skipping this source, `"closed"` otherwise. */
  state: string
  /** Last time it answered; null on a process that has not needed it yet. */
  healthy_at: string | null
  /** Last time it failed; null likewise. */
  failed_at: string | null
  /** What it said when it failed — an HTTP status, `"unconfigured"`, … */
  reason: string | null
  /** False for MAL until `MAL_CLIENT_ID` is set; always true for AniList. */
  configured: boolean
}

/** `GET /api/catalog/status` — the server's `CatalogStatusOut`. */
export interface CatalogStatus {
  /** Keyed by source name (`"anilist"`, `"mal"`). */
  sources: Record<string, SourceStatus>
  /**
   * Which source the next read would go to: `"anilist"`, `"mal"`, or `"none"`
   * when both are open or unconfigured.
   */
  active: string
}

export const catalogStatusQueryKey = ['catalog', 'status'] as const

export function useCatalogStatus(): UseQueryResult<CatalogStatus, Error> {
  return useQuery<CatalogStatus, Error>({
    queryKey: catalogStatusQueryKey,
    queryFn: () => apiFetch<CatalogStatus>('/api/catalog/status'),
  })
}
