/**
 * Choosing or changing the release for an episode or a trip by hand (spec
 * FR-A13, architecture §5.1a "Manual release choice").
 *
 * Not a "download anything" door: the sheet only opens for an episode the
 * viewer already wants, or for the viewer's own trip, and whatever is chosen
 * replaces what that episode (or trip) was downloading from. This module is
 * the wire shapes, the query and the mutation, and the few pure rules the
 * sheet renders with.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'
import { ApiError, apiFetch, isOffline } from '@/lib/api'
import { animeQueryKey } from '@/lib/anime'
import { TRIP_QUERY_KEY } from '@/lib/trips'

export type ReleaseKind = 'single' | 'batch'

/** One release the sheet lists (`ReleaseCandidateOut`). */
export interface ReleaseCandidate {
  /** What `POST …/release` takes. */
  id: string
  title: string
  group: string | null
  resolution: string | null
  /** As Nyaa writes it, e.g. "1.4 GiB". */
  size: string | null
  seeders: number
  leechers: number
  kind: ReleaseKind
  trusted: boolean
  /** The viewer's wanted episodes it would serve; null for a pack naming no range. */
  covers: number[] | null
  /** Whether Arc would take it on its own terms; `reason` says why not. */
  acceptable: boolean
  reason: string | null
  /** The release downloading now. Optional on the wire. */
  current?: boolean
}

/** The download running now (`CurrentReleaseOut`). */
export interface CurrentRelease {
  title: string | null
  kind: ReleaseKind
  state: string | null
  progress: number | null
  manual: boolean
}

/** `GET /api/episodes/{id}/releases`, `GET /api/trips/{id}/releases`. */
export interface Releases {
  scope: 'episode' | 'trip'
  scope_id: number
  number: number
  candidates: ReleaseCandidate[]
  current: CurrentRelease | null
  cached: boolean
  searched_seconds_ago: number
}

/** `POST …/release` → 202. */
export interface ReleaseChosen {
  title: string
  kind: ReleaseKind
  info_hash: string
  episodes: { episode_id: number; number: number; state: string }[]
}

/** What the sheet is about: one episode, or the viewer's trip on a show. */
export type ReleaseScope =
  | { kind: 'episode'; id: number; animeId: number; number: number }
  | { kind: 'trip'; id: number; animeId: number }

/** A choice: a listed candidate, or a pasted magnet / Nyaa link. */
export type ReleaseChoice = { candidate_id: string } | { link: string }

function basePath(scope: ReleaseScope): string {
  return scope.kind === 'episode'
    ? `/api/episodes/${String(scope.id)}`
    : `/api/trips/${String(scope.id)}`
}

export function releasesPath(scope: ReleaseScope): string {
  return `${basePath(scope)}/releases`
}

export function releasePath(scope: ReleaseScope): string {
  return `${basePath(scope)}/release`
}

export function releasesQueryKey(scope: ReleaseScope): readonly [string, string, number] {
  return ['releases', scope.kind, scope.id]
}

/**
 * The list for a scope, fetched when the sheet opens. The server searches
 * Nyaa at most once a minute per viewer and scope, so a minute of staleness
 * here asks nothing it would not answer from its own memory anyway.
 */
export function useReleases(
  scope: ReleaseScope,
  enabled: boolean,
): UseQueryResult<Releases, Error> {
  return useQuery<Releases, Error>({
    queryKey: releasesQueryKey(scope),
    queryFn: () => apiFetch<Releases>(releasesPath(scope)),
    enabled,
    staleTime: 60_000,
    retry: false,
  })
}

/**
 * Use the chosen release. On success the show (its rows), the trip and the
 * list itself are refetched, so the row says what it is downloading now.
 */
export function useChooseRelease(
  scope: ReleaseScope,
): UseMutationResult<ReleaseChosen, Error, ReleaseChoice> {
  const client = useQueryClient()
  return useMutation<ReleaseChosen, Error, ReleaseChoice>({
    mutationFn: (choice) =>
      apiFetch<ReleaseChosen>(releasePath(scope), {
        method: 'POST',
        body: JSON.stringify(choice),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: animeQueryKey(scope.animeId) })
      void client.invalidateQueries({ queryKey: TRIP_QUERY_KEY })
      void client.invalidateQueries({ queryKey: releasesQueryKey(scope) })
    },
  })
}

/** The server's refusal sentence, when it sent one (`detail: {code, message}`). */
function refusal(error: unknown): { code: string; message: string } | null {
  if (!(error instanceof ApiError)) return null
  const body = error.body
  if (typeof body !== 'object' || body === null || !('detail' in body)) return null
  const detail = body.detail
  if (typeof detail !== 'object' || detail === null) return null
  const { code, message } = detail as { code?: unknown; message?: unknown }
  if (typeof code !== 'string' || typeof message !== 'string') return null
  return { code, message }
}

/** The code of a refusal, for a caller that branches on one. */
export function releaseRefusalCode(error: unknown): string | null {
  return refusal(error)?.code ?? null
}

/** A refusal as a sentence a person can read. */
export function releaseErrorMessage(error: unknown, fallback: string): string {
  const found = refusal(error)
  if (found !== null) return found.message
  if (isOffline(error)) return 'Arc could not be reached. Try again when you are back online.'
  if (error instanceof ApiError && error.status === 422) {
    return 'Choose a release from the list, or paste one link.'
  }
  return fallback
}

/** `[1, 2, 3, 5]` → `"1–3, 5"`: consecutive runs collapse to a range. */
export function numberRuns(numbers: readonly number[]): string {
  const sorted = [...new Set(numbers)].sort((a, b) => a - b)
  const runs: string[] = []
  let start: number | undefined
  let previous: number | undefined
  for (const number of sorted) {
    if (start !== undefined && previous !== undefined && number === previous + 1) {
      previous = number
      continue
    }
    if (start !== undefined && previous !== undefined) {
      runs.push(start === previous ? String(start) : `${String(start)}–${String(previous)}`)
    }
    start = number
    previous = number
  }
  if (start !== undefined && previous !== undefined) {
    runs.push(start === previous ? String(start) : `${String(start)}–${String(previous)}`)
  }
  return runs.join(', ')
}

/** What a candidate would serve, in a few words: "covers 1–12", "episode 7". */
export function coversLabel(candidate: Pick<ReleaseCandidate, 'kind' | 'covers'>): string {
  if (candidate.covers === null) return 'contents read when chosen'
  if (candidate.covers.length === 0) return 'none of your episodes by its name'
  if (candidate.kind === 'single' && candidate.covers.length === 1) {
    return `episode ${String(candidate.covers[0])}`
  }
  return `covers ${numberRuns(candidate.covers)}`
}

/** One line of facts about a candidate: resolution, size, seeders, single or pack. */
export function candidateFacts(candidate: ReleaseCandidate): string {
  const parts = [
    candidate.kind === 'batch' ? 'Pack' : 'Single',
    candidate.resolution,
    candidate.size,
    `${String(candidate.seeders)} seeders`,
    coversLabel(candidate),
  ]
  return parts.filter((part): part is string => part !== null && part !== '').join(' · ')
}

/** The episode states a viewer may change the release of: nothing has landed yet. */
export const CHANGEABLE_STATES: ReadonlySet<string> = new Set([
  'not_wanted',
  'wanted',
  'searching',
  'downloading',
  'unavailable',
])
