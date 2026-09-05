import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { apiFetch } from '@/lib/api'

export interface Health {
  status: string
  version: string
  env: string
}

export const healthQueryKey = ['health'] as const

export function useHealth(): UseQueryResult<Health, Error> {
  return useQuery<Health, Error>({
    queryKey: healthQueryKey,
    queryFn: () => apiFetch<Health>('/api/health'),
  })
}
