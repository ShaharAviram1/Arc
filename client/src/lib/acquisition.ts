/**
 * Acquisition status (spec §4.2, roadmap M6).
 *
 * `acquisition_paused` is an admin kill switch in `settings`: while it is set,
 * `compute_wants` does nothing and every `search_release` requeues itself
 * without touching Nyaa. Downloads already in flight still finish.
 *
 * A pause is silent from the app — episodes simply stop moving out of
 * `wanted` — which is precisely the state that needs saying out loud. So this
 * exists to put one muted line in the sidebar. It is admin-only: the endpoint
 * requires the role, and a user who cannot unpause it has nothing to do with
 * the information.
 *
 * Polled rather than invalidated, on a minute: the flag moves when an admin
 * presses something in another tab (M14 gives it controls of its own), and a
 * minute is far sooner than anyone notices. `retry: false` because a status
 * line is not worth a retry storm — a failed poll shows nothing until the next
 * one, and "nothing" reads as "not paused", which is the right thing to say
 * when we do not know.
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { apiFetch } from '@/lib/api'
import { useMe } from '@/lib/auth'

/** `GET /api/acquisition/status` — the server's `AcquisitionStatusOut`. */
export interface AcquisitionStatus {
  /** Whether the kill switch is set. */
  paused: boolean
  /** Live wants across every user (FR-A2 merges them; this counts rows). */
  active_wants: number
  /** Episodes a search job is looking for a release for. */
  searching: number
  /** Episodes qBittorrent is downloading. Unaffected by the pause. */
  downloading: number
}

export const acquisitionStatusQueryKey = ['acquisition', 'status'] as const

/** How long a fetched status is treated as fresh. */
const ACQUISITION_STALE_TIME_MS = 30_000

/** How often it is refetched while the app is open. */
const ACQUISITION_REFETCH_INTERVAL_MS = 60_000

/**
 * The acquisition status, for admins only.
 *
 * Stays disabled until `me` has answered and says `admin`: firing it otherwise
 * would only ever earn a 401 or a 403, and the query client reads a 401 as the
 * session dying under us.
 */
export function useAcquisitionStatus(): UseQueryResult<AcquisitionStatus, Error> {
  const { data: me } = useMe()

  return useQuery<AcquisitionStatus, Error>({
    queryKey: acquisitionStatusQueryKey,
    queryFn: () => apiFetch<AcquisitionStatus>('/api/acquisition/status'),
    staleTime: ACQUISITION_STALE_TIME_MS,
    refetchInterval: ACQUISITION_REFETCH_INTERVAL_MS,
    retry: false,
    enabled: me?.role === 'admin',
  })
}
