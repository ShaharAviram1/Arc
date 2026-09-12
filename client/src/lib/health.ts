import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { apiFetch } from '@/lib/api'

export interface Health {
  status: string
  version: string
  env: string
  /**
   * Whether this deployment has a `TMDB_API_KEY` (M15.5). TMDB's terms require
   * an attribution line wherever their data is shown, and with no key none of
   * it is — so the line follows the flag rather than being always on.
   */
  tmdb_enabled: boolean
}

export const healthQueryKey = ['health'] as const

export function useHealth(): UseQueryResult<Health, Error> {
  return useQuery<Health, Error>({
    queryKey: healthQueryKey,
    queryFn: () => apiFetch<Health>('/api/health'),
  })
}
