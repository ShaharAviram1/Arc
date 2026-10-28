import { describe, expect, it } from 'vitest'
import type { AnimeDetail, EpisodeOut } from '@/lib/anime'
import { ApiError } from '@/lib/api'
import {
  askAgainErrorMessage,
  copyEstimateOf,
  otherTripsReserve,
  TRIP_COPY_ESTIMATE_FALLBACK,
  tripCandidates,
  tripCodecsPlayable,
  tripCountMax,
  tripErrorMessage,
  tripEstimateLabel,
  tripRoomFor,
  tripRangeLabel,
  tripRowStatus,
  withTrip,
  type Trip,
  type TripEpisode,
} from '@/lib/trips'
import type { DownloadRecord } from '@/offline/downloads'
import { FRIEREN_DETAIL, listEntry, PLAY_INFO } from '@/test/animeFixtures'
import { downloadedRecord } from '@/test/downloadFixtures'

/** The pure rules behind "Prepare for a trip" and its panel (M19 T6, FR-A12). */

const BASE = FRIEREN_DETAIL.episodes[0] as EpisodeOut

function episodes(count: number, aired: number, watched = 0): EpisodeOut[] {
  return Array.from({ length: count }, (_, index) => ({
    ...BASE,
    id: 100 + index,
    number: index + 1,
    aired: index < aired,
    watched: index < watched,
    state: 'not_wanted',
  }))
}

function show(list: EpisodeOut[], progress?: number): Pick<AnimeDetail, 'episodes' | 'list_entry'> {
  return {
    episodes: list,
    list_entry: progress === undefined ? null : listEntry({ progress }),
  }
}

const bytes = (n: number) => `${String(n)} B`

describe('the stepper’s bounds', () => {
  it('counts the aired episodes after the viewer’s progress', () => {
    // Episode 1 is watched (FRIEREN_DETAIL), 3 has not aired: 2, 4, 5, 6, 7.
    expect(tripCandidates(FRIEREN_DETAIL).map((episode) => episode.number)).toEqual([2, 4, 5, 6, 7])
    expect(tripCountMax(FRIEREN_DETAIL)).toBe(5)
  })

  it('starts after the list’s progress or the furthest watched episode, whichever is later', () => {
    expect(tripCountMax(show(episodes(12, 12), 9))).toBe(3)
    expect(tripCountMax(show(episodes(12, 12, 10), 4))).toBe(2)
  })

  it('stops at the server’s cap, else at 50 for a payload that does not say', () => {
    expect(tripCountMax(show(episodes(80, 80)))).toBe(50)
    expect(tripCountMax({ ...show(episodes(80, 80)), trip_limits: { max_episodes: 12 } })).toBe(12)
    expect(tripCountMax({ ...show(episodes(5, 5)), trip_limits: { max_episodes: 12 } })).toBe(5)
  })

  it('is 0 when nothing has aired after the viewer’s progress', () => {
    expect(tripCountMax(show(episodes(12, 6), 6))).toBe(0)
  })

  it('names the range the count takes', () => {
    const candidates = tripCandidates(show(episodes(20, 20), 3))
    expect(tripRangeLabel(candidates, 12)).toBe('Episodes 4–15')
    expect(tripRangeLabel(candidates, 1)).toBe('Episode 4')
    expect(tripRangeLabel(candidates, 99)).toBe('Episodes 4–20')
    expect(tripRangeLabel([], 3)).toBe('')
  })
})

describe('the size estimate', () => {
  it('rounds to decimal units, as a guess should', () => {
    expect(tripEstimateLabel(1, 100_000_000)).toBe('100 MB')
    expect(tripEstimateLabel(9, 100_000_000)).toBe('900 MB')
    expect(tripEstimateLabel(10, 100_000_000)).toBe('1 GB')
    expect(tripEstimateLabel(12, 100_000_000)).toBe('1.2 GB')
    expect(tripEstimateLabel(10, 254_000_000)).toBe('2.5 GB')
  })

  it("takes the server's estimate, else its fallback (owner, 2026-10-28)", () => {
    expect(copyEstimateOf({ copy_estimate_bytes: 254_000_000 })).toBe(254_000_000)
    expect(copyEstimateOf({ copy_estimate_bytes: 0 })).toBe(TRIP_COPY_ESTIMATE_FALLBACK)
    expect(copyEstimateOf({})).toBe(TRIP_COPY_ESTIMATE_FALLBACK)
    expect(copyEstimateOf(null)).toBe(TRIP_COPY_ESTIMATE_FALLBACK)
    expect(TRIP_COPY_ESTIMATE_FALLBACK).toBe(260_000_000)
  })
})

describe('the room the browser reports (owner, 2026-10-07)', () => {
  it('counts whole episodes at the estimate plus a tenth, and says nothing without figures', () => {
    expect(tripRoomFor({ usage: 4_700_000_000, quota: 32_400_000_000 }, 100_000_000)).toBe(251)
    expect(tripRoomFor({ usage: 0, quota: 219_999_999 }, 100_000_000)).toBe(1)
    expect(tripRoomFor({ usage: 0, quota: 2_794_000_000 }, 254_000_000)).toBe(10)
    expect(tripRoomFor({ usage: 6, quota: 5 }, 100_000_000)).toBe(0)
    expect(tripRoomFor({ usage: 0, quota: 0 }, 100_000_000)).toBeNull()
    expect(tripRoomFor(null, 100_000_000)).toBeNull()
  })

  it('sets aside what the other trips still have to bring (owner, 2026-10-28)', () => {
    const room = { usage: 0, quota: 2_794_000_000 }
    expect(tripRoomFor(room, 254_000_000, 4 * 254_000_000)).toBe(6)
    expect(tripRoomFor(room, 254_000_000, 20 * 254_000_000)).toBe(0)
  })
})

function tripOf(id: number, animeId: number, phases: TripEpisode['phase'][]): Trip {
  return {
    id,
    anime_id: animeId,
    anime_title: `Show ${String(animeId)}`,
    first_number: 1,
    last_number: phases.length,
    count: phases.length,
    state: 'active',
    created_at: '2026-10-28T08:00:00Z',
    deadline_at: '2026-11-11T08:00:00Z',
    episodes: phases.map((phase, index) => ({
      episode_id: id * 100 + index,
      number: index + 1,
      phase,
      progress: null,
      size: null,
      delivered: phase === 'delivered',
    })),
  }
}

describe('the other trips on the device (owner, 2026-10-28)', () => {
  it("counts each other trip's episodes still to come, never this show's", () => {
    const trips = [
      tripOf(1, 10, ['available', 'searching', 'delivered', 'expired', 'unavailable']),
      tripOf(2, 20, ['downloading', 'preparing', 'waiting_space']),
      tripOf(3, 30, ['searching', 'searching']),
    ]
    expect(otherTripsReserve(trips, 30, 100)).toBe(500)
    expect(otherTripsReserve(trips, 99, 100)).toBe(700)
    expect(otherTripsReserve([], 30, 100)).toBe(0)
    expect(otherTripsReserve(undefined, 30, 100)).toBe(0)
  })

  it('leaves out a copy this device already holds', () => {
    const trips = [tripOf(1, 10, ['available', 'available'])]
    const records = { 100: { ...downloadedRecord(1, PLAY_INFO), episodeId: 100 } }
    expect(otherTripsReserve(trips, 30, 100, records)).toBe(100)
  })

  it('keeps the cached list in step: added at the end, replaced in place, ended removed', () => {
    const one = tripOf(1, 10, ['searching'])
    const two = tripOf(2, 20, ['searching'])
    expect(withTrip(undefined, one)).toEqual([one])
    expect(withTrip([one], two)).toEqual([one, two])
    const moved = tripOf(1, 10, ['available'])
    expect(withTrip([one, two], moved)).toEqual([moved, two])
    expect(withTrip([one, two], { ...one, state: 'finished' })).toEqual([two])
  })
})

describe('the codec check', () => {
  it('checks the codecs a ready episode’s copy names, else the default', () => {
    const asked: string[] = []
    const probe = (type: string) => {
      asked.push(type)
      return type.includes('hvc1') ? '' : 'maybe'
    }
    expect(tripCodecsPlayable(show(episodes(2, 2)), probe)).toBe(true)
    const hevc = episodes(2, 2).map((episode) => ({
      ...episode,
      offline: {
        state: 'none' as const,
        progress: null,
        size: null,
        url: null,
        codecs: 'hvc1.1.6.L93.B0',
      },
    }))
    expect(tripCodecsPlayable(show(hevc), probe)).toBe(false)
    expect(asked).toEqual([
      'video/mp4; codecs="avc1.640028"',
      'video/mp4; codecs="hvc1.1.6.L93.B0"',
    ])
  })
})

describe('refusals, in sentences', () => {
  it.each([
    ['trip_active', 'This show already has a trip being prepared.'],
    [
      'storage_held',
      'Arc’s server is short of disk space right now, so it cannot start a trip. Try again later.',
    ],
    ['nothing_aired', 'Nothing has aired after where you are in this show.'],
    ['count_out_of_range', 'That is more episodes than this server allows for one trip.'],
    ['demo_account', 'The demo account cannot prepare trips.'],
  ])('%s', (detail, sentence) => {
    const status =
      detail === 'demo_account'
        ? 403
        : detail.startsWith('trip') || detail === 'storage_held'
          ? 409
          : 422
    expect(tripErrorMessage(new ApiError(status, { detail }))).toBe(sentence)
  })

  it('says something for anything else, and for no answer at all', () => {
    expect(tripErrorMessage(new ApiError(500, { detail: 'boom' }))).toBe(
      'Could not start the trip. Try again.',
    )
    expect(tripErrorMessage(new TypeError('Load failed'))).toBe(
      'Could not reach Arc. Try again when you are online.',
    )
    expect(askAgainErrorMessage(new ApiError(409, { detail: 'trip_not_active' }))).toBe(
      'This trip has ended.',
    )
  })
})

describe('a trip episode, as this device sees it', () => {
  function row(phase: TripEpisode['phase'], patch: Partial<TripEpisode> = {}): TripEpisode {
    return {
      episode_id: 9001,
      number: 1,
      phase,
      progress: null,
      size: null,
      delivered: false,
      ...patch,
    }
  }

  it.each<[TripEpisode['phase'], Partial<TripEpisode>, string, boolean]>([
    ['searching', {}, 'Looking for it', false],
    ['waiting_space', {}, 'Waiting for disk space', false],
    ['downloading', { progress: 0.42 }, 'Downloading to the server 42%', false],
    ['preparing', { progress: 0.3 }, 'Making the small copy 30%', false],
    ['preparing', {}, 'Making the small copy', false],
    ['available', { size: 98 }, 'Ready to download · 98 B', false],
    ['delivered', { delivered: true }, 'Not on this device', true],
    ['expired', {}, 'Expired on the server', true],
    ['unavailable', {}, 'Not available', false],
  ])('%s → %s', (phase, patch, label, askAgain) => {
    const status = tripRowStatus(row(phase, patch), undefined, false, bytes)
    expect(status.label).toBe(label)
    expect(status.askAgain).toBe(askAgain)
  })

  it('puts the device’s own record first', () => {
    const done = downloadedRecord(1, PLAY_INFO, 210)
    expect(tripRowStatus(row('delivered'), done, false, bytes)).toMatchObject({
      label: 'On this device · 210 B',
      askAgain: false,
    })
    const running: DownloadRecord = { ...done, state: 'downloading', bytes: 50, total: 200 }
    expect(tripRowStatus(row('available'), running, false, bytes)).toMatchObject({
      label: 'Downloading 25%',
      percent: 25,
    })
  })

  it('offers Ask again for an episode taken off this device by hand', () => {
    expect(tripRowStatus(row('available'), undefined, true, bytes)).toMatchObject({
      label: 'Removed from this device',
      askAgain: true,
    })
  })
})
