import { describe, expect, it } from 'vitest'
import type { AnimeDetail, EpisodeOut } from '@/lib/anime'
import { ApiError } from '@/lib/api'
import {
  askAgainErrorMessage,
  tripCandidates,
  tripCodecsPlayable,
  tripCountMax,
  tripErrorMessage,
  tripEstimateLabel,
  tripRoomFor,
  tripRangeLabel,
  tripRowStatus,
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
    expect(tripEstimateLabel(1)).toBe('100 MB')
    expect(tripEstimateLabel(9)).toBe('900 MB')
    expect(tripEstimateLabel(10)).toBe('1 GB')
    expect(tripEstimateLabel(12)).toBe('1.2 GB')
    expect(tripEstimateLabel(50)).toBe('5 GB')
  })
})

describe('the room the browser reports (owner, 2026-10-07)', () => {
  it('counts whole episodes at 110 MB each, and says nothing without figures', () => {
    expect(tripRoomFor({ usage: 4_700_000_000, quota: 32_400_000_000 })).toBe(251)
    expect(tripRoomFor({ usage: 0, quota: 219_999_999 })).toBe(1)
    expect(tripRoomFor({ usage: 6, quota: 5 })).toBe(0)
    expect(tripRoomFor({ usage: 0, quota: 0 })).toBeNull()
    expect(tripRoomFor(null)).toBeNull()
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
    ['trip_active', 'You already have a trip being prepared. Cancel it, or wait for it to finish.'],
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
