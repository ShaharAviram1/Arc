import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'
import { ApiError } from '@/lib/api'
import { authMeQueryKey } from '@/lib/auth'

/**
 * A 401 from anything other than `/api/auth/me` means the session died under
 * us (expired, revoked, server restarted). `useMe` handles its own 401 by
 * resolving to `null`, so it is excluded here to avoid clobbering an in-flight
 * answer with a stale one.
 */
function isSessionLoss(error: unknown, queryKey?: readonly unknown[]): boolean {
  if (!(error instanceof ApiError) || error.status !== 401) return false
  if (queryKey && queryKey[0] === authMeQueryKey[0] && queryKey[1] === authMeQueryKey[1]) {
    return false
  }
  return true
}

export function createQueryClient(): QueryClient {
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({
      onError: (error, query) => {
        // `client` is captured, not read, at construction time.
        if (isSessionLoss(error, query.queryKey)) client.setQueryData(authMeQueryKey, null)
      },
    }),
    mutationCache: new MutationCache({
      onError: (error) => {
        if (isSessionLoss(error)) client.setQueryData(authMeQueryKey, null)
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: 1,
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: 0,
      },
    },
  })

  return client
}

export const queryClient = createQueryClient()
