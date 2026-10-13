import { describe, expect, it } from 'vitest'
import {
  clampToFront,
  fewSeeders,
  JUMP_LEAD_S,
  liveStatusLabel,
  measureDuration,
  planSeek,
  readFront,
  settlePending,
} from '@/player/liveSeek'

/** A `TimeRanges` with one range, 0 → `end`. */
function ranges(...ends: number[]): TimeRanges {
  return {
    length: ends.length,
    start: () => 0,
    end: (index: number) => {
      const end = ends[index]
      if (end === undefined) throw new Error('IndexSizeError')
      return end
    },
  }
}

describe('planSeek (FR-P8)', () => {
  it.each([
    // [target, front, now, pending]
    [750, 300, 299, 750],
    [100, 300, 100, null],
    // Exactly at the edge is immediate; a hair past it waits.
    [299, 300, 299, null],
    [299.5, 300, 299, 299.5],
    // Nothing downloaded yet: wait at zero.
    [10, 0, 0, 10],
    [0, 0, 0, null],
    // Below zero is zero; a non-number is zero.
    [-5, 300, 0, null],
    [Number.NaN, 300, 0, null],
  ])('planSeek(%s, %s) → now %s, pending %s', (target, front, now, pending) => {
    expect(planSeek(target, front)).toEqual({ now, pending })
  })

  it('is the example in the plan', () => {
    expect(planSeek(750, 300)).toEqual({ pending: 750, now: 299 })
  })
})

describe('settlePending (FR-P8)', () => {
  it('waits until the front is six seconds past the target, then jumps', () => {
    expect(JUMP_LEAD_S).toBe(6)
    const at = (front: number) => settlePending(750, { front, position: 299, ended: false })
    expect(at(300)).toEqual({ action: 'wait' })
    expect(at(755.9)).toEqual({ action: 'wait' })
    expect(at(756)).toEqual({ action: 'jump', at: 750 })
    expect(at(900)).toEqual({ action: 'jump', at: 750 })
  })

  it('drops the target once playback has reached it by itself', () => {
    expect(settlePending(305, { front: 306, position: 305.2, ended: false })).toEqual({
      action: 'drop',
    })
  })

  it('once the download has ended, jumps within six seconds of the target, held to the front', () => {
    expect(settlePending(1430, { front: 1428, position: 299, ended: true })).toEqual({
      action: 'jump',
      at: 1427,
    })
    expect(settlePending(750, { front: 1420, position: 299, ended: true })).toEqual({
      action: 'jump',
      at: 750,
    })
  })

  it('still waits after the end while the element has not caught up', () => {
    // Safari's front before it reloads the finished playlist, and no range yet.
    expect(settlePending(750, { front: 300, position: 299, ended: true })).toEqual({
      action: 'wait',
    })
    expect(settlePending(750, { front: 0, position: 0, ended: true })).toEqual({
      action: 'wait',
    })
  })

  it('a seek below the front is immediate, so nothing is ever pending for it', () => {
    expect(planSeek(120, 300).pending).toBeNull()
  })
})

describe('clampToFront (FR-S6)', () => {
  it('holds an arrow or End seek to the playable edge', () => {
    expect(clampToFront(302, 300, 297)).toBe(299)
    expect(clampToFront(1440, 300, 120)).toBe(299)
    expect(clampToFront(105, 300, 100)).toBe(105)
  })

  it('never pulls a playhead already in the last second back', () => {
    expect(clampToFront(304.6, 300, 299.6)).toBe(299.6)
  })
})

describe('readFront', () => {
  it('is the end of the last seekable range, or the fallback', () => {
    expect(readFront(ranges(312), 300)).toBe(312)
    expect(readFront(ranges(10, 330), 300)).toBe(330)
    expect(readFront(ranges(), 300)).toBe(300)
    expect(readFront(undefined, 300)).toBe(300)
    expect(readFront(ranges(Number.POSITIVE_INFINITY), 300)).toBe(300)
  })

  it('falls back when the element throws', () => {
    const broken = {
      length: 1,
      start: () => 0,
      end: () => {
        throw new Error('InvalidStateError')
      },
    }
    expect(readFront(broken, 42)).toBe(42)
  })
})

describe('liveStatusLabel and fewSeeders', () => {
  it('says the download, the seeders and the jump', () => {
    expect(liveStatusLabel({ download_progress: 0.43, seeders: 12 }, 750)).toBe(
      'Downloading — 43 % · 12 seeders · will jump to 12:30',
    )
    expect(liveStatusLabel({ download_progress: 0.29, seeders: 1 }, null)).toBe(
      'Downloading — 29 % · 1 seeder',
    )
    expect(liveStatusLabel({ download_progress: 0.999, seeders: null }, null)).toBe(
      'Downloading — 99 %',
    )
  })

  it('calls fewer than three seeders few', () => {
    expect(fewSeeders(2)).toBe(true)
    expect(fewSeeders(0)).toBe(true)
    expect(fewSeeders(3)).toBe(false)
    expect(fewSeeders(null)).toBe(false)
  })
})

describe('the playhead counts as reachable (Safari plays past its range)', () => {
  it('a seek back from past the range is immediate', () => {
    expect(planSeek(20, 16, 30)).toEqual({ now: 20, pending: null })
    // No range at all yet.
    expect(planSeek(10, 0, 30)).toEqual({ now: 10, pending: null })
  })

  it('a seek ahead waits from where the playhead is, never pulling it back', () => {
    expect(planSeek(40, 16, 30)).toEqual({ now: 30, pending: 40 })
  })

  it('a key seek ahead is held to the larger front, or stays put', () => {
    expect(clampToFront(35, 40, 30)).toBe(35)
    expect(clampToFront(35, 16, 30)).toBe(30)
  })
})

describe('settlePending past the real end', () => {
  it('resolves to the end once the element has the whole file', () => {
    expect(
      settlePending(1435, { front: 1420, position: 299, ended: true, finalFront: 1420 }),
    ).toEqual({ action: 'jump', at: 1419 })
  })

  it('waits while the element is short of the final front', () => {
    expect(
      settlePending(1435, { front: 1300, position: 299, ended: true, finalFront: 1420 }),
    ).toEqual({ action: 'wait' })
    // Not ended: the final front is not known.
    expect(settlePending(1435, { front: 1420, position: 299, ended: false })).toEqual({
      action: 'wait',
    })
  })
})

describe('measureDuration (FR-S4)', () => {
  it('is the server’s while live, whatever the element says', () => {
    expect(measureDuration(300, 1440, { live: true, seenExpected: 1440 })).toBe(1440)
  })

  it('after adoption, refuses a stale short duration until it is 90 % of the expected', () => {
    const seen = { live: false, seenExpected: 1440 }
    expect(measureDuration(1200, 1440, seen)).toBe(1440)
    expect(measureDuration(Number.NaN, 0, seen)).toBe(1440)
    expect(measureDuration(1296, 1440, seen)).toBe(1296)
    expect(measureDuration(1450, 1440, seen)).toBe(1450)
  })

  it('is the media’s own on a page that never saw live', () => {
    const never = { live: false, seenExpected: null }
    expect(measureDuration(1436.8, 1400, never)).toBe(1436.8)
    expect(measureDuration(Number.POSITIVE_INFINITY, 1400, never)).toBe(1400)
  })
})
