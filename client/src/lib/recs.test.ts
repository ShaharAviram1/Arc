import { describe, expect, it } from 'vitest'
import { ApiError } from '@/lib/api'
import {
  formatRelativeTime,
  formatWait,
  NOT_CONFIGURED_MESSAGE,
  promptLabel,
  chainLabel,
  continuationsOf,
  modelStatusLabel,
  recsErrorMessage,
  runSummary,
  type RecPick,
  type RecRun,
} from '@/lib/recs'
import { FRIEREN } from '@/test/animeFixtures'

/** An `ApiError` shaped like the ones `apiFetch` throws. */
function apiError(status: number, body: unknown, retryAfter: number | null = null): ApiError {
  return new ApiError(status, body, `POST /api/recs/runs → ${String(status)}`, retryAfter)
}

/** A pick with a distinct id; only the count matters to the summary line. */
function pick(id: number): RecPick {
  return { anime: { ...FRIEREN, id }, case: 'because.' }
}

describe('formatWait', () => {
  it('rounds anything under a couple of minutes up to a minute', () => {
    expect(formatWait(0)).toBe('a minute')
    expect(formatWait(45)).toBe('a minute')
    expect(formatWait(90)).toBe('a minute')
  })

  it('counts in minutes below the hour', () => {
    expect(formatWait(600)).toBe('10 minutes')
    expect(formatWait(3000)).toBe('50 minutes')
  })

  it('counts in hours above it', () => {
    expect(formatWait(3600)).toBe('an hour')
    expect(formatWait(7 * 3600)).toBe('7 hours')
  })
})

describe('recsErrorMessage', () => {
  it('says how long to wait when the daily limit is hit', () => {
    const message = recsErrorMessage(
      apiError(429, { detail: 'daily limit reached', retry_after_seconds: 5 * 3600 }),
    )
    expect(message).toBe('Daily limit reached; try again in about 5 hours.')
  })

  it('falls back to the Retry-After header when the body carries no seconds', () => {
    expect(recsErrorMessage(apiError(429, { detail: 'slow down' }, 1800))).toBe(
      'Daily limit reached; try again in about 30 minutes.',
    )
  })

  it('tells an unconfigured server apart from a refusal, both 503', () => {
    expect(recsErrorMessage(apiError(503, { detail: 'Recommendations are not configured' }))).toBe(
      NOT_CONFIGURED_MESSAGE,
    )
    expect(recsErrorMessage(apiError(503, { detail: 'The model declined this request' }))).toMatch(
      /The model declined this request/,
    )
  })

  it('blames the upstream for a 502 rather than the person', () => {
    expect(recsErrorMessage(apiError(502, { detail: 'upstream timeout' }))).toMatch(
      /could not be reached/,
    )
  })

  it('passes the 409 through: it says what to do about it', () => {
    expect(
      recsErrorMessage(
        apiError(409, { detail: 'Nothing to recommend from: add or rate a few shows first' }),
      ),
    ).toBe('Nothing to recommend from: add or rate a few shows first')
  })

  it('names the length limit for a 422', () => {
    expect(recsErrorMessage(apiError(422, { detail: 'too long' }))).toMatch(/300 characters/)
  })

  it('treats anything that is not an ApiError as a transport failure', () => {
    expect(recsErrorMessage(new Error('offline'))).toBe('Could not reach the server. Try again.')
  })
})

describe('formatRelativeTime', () => {
  const NOW = Date.parse('2026-09-10T12:00:00Z')

  it('counts back in the coarsest unit that fits', () => {
    expect(formatRelativeTime('2026-09-10T10:00:00Z', NOW)).toBe('2 hours ago')
    expect(formatRelativeTime('2026-09-10T11:30:00Z', NOW)).toBe('30 minutes ago')
    expect(formatRelativeTime('2026-08-10T12:00:00Z', NOW)).toMatch(/month/)
  })

  it('says "now" for a run that has only just landed', () => {
    expect(formatRelativeTime('2026-09-10T12:00:00Z', NOW)).toBe('now')
  })

  it('does not blank the line on an unparseable timestamp', () => {
    expect(formatRelativeTime('not a date', NOW)).toBe('just now')
  })
})

describe('runSummary', () => {
  const RUN: RecRun = {
    id: 1,
    prompt: null,
    created_at: '2026-09-10T08:00:00Z',
    model: 'claude-opus-5',
    candidate_count: 40,
    picks: [],
  }

  it('counts the picks against the pool they came from, and names the model', () => {
    expect(runSummary({ ...RUN, picks: [pick(1), pick(2), pick(3), pick(4)] })).toBe(
      'Picked 4 of 40 candidates · claude-opus-5',
    )
  })

  it('leaves the model out rather than naming a blank one', () => {
    expect(runSummary({ ...RUN, model: null, picks: [pick(1)] })).toBe('Picked 1 of 40 candidates')
    expect(runSummary({ ...RUN, model: '', picks: [pick(1)] })).toBe('Picked 1 of 40 candidates')
  })
})

describe('modelStatusLabel', () => {
  it('ticks a model that is good for today', () => {
    expect(
      modelStatusLabel({ provider: 'gemini', model: 'gemini-3.5-flash', available: true }),
    ).toBe('gemini-3.5-flash ✓')
  })

  it('reads a spent quota as resting, never as a failure', () => {
    const label = modelStatusLabel({
      provider: 'gemini',
      model: 'gemini-2.5-flash',
      available: false,
    })
    expect(label).toBe('gemini-2.5-flash (resting until tomorrow)')
    expect(label).not.toMatch(/error|fail|down|unavailable/i)
  })

  it('names the provider only when the model id does not already', () => {
    expect(
      modelStatusLabel({ provider: 'openrouter', model: 'openai/gpt-5-mini', available: true }),
    ).toBe('openai/gpt-5-mini via openrouter ✓')
    // "gemini-3.5-flash via gemini" would say nothing twice.
    expect(
      modelStatusLabel({ provider: 'Gemini', model: 'gemini-3.5-flash', available: true }),
    ).toBe('gemini-3.5-flash ✓')
  })
})

describe('chainLabel', () => {
  it('joins the chain in the order it is tried', () => {
    expect(
      chainLabel([
        { provider: 'gemini', model: 'gemini-3.5-flash', available: true },
        { provider: 'gemini', model: 'gemini-2.5-flash', available: false },
      ]),
    ).toBe('gemini-3.5-flash ✓ · gemini-2.5-flash (resting until tomorrow)')
  })

  it('gives nothing to render for an empty chain', () => {
    expect(chainLabel([])).toBe('')
  })
})

describe('continuationsOf', () => {
  const RUN: RecRun = {
    id: 1,
    prompt: null,
    created_at: '2026-09-10T08:00:00Z',
    model: 'a-model',
    candidate_count: 40,
    picks: [],
  }

  it('reads a run stored before continuations existed as having none', () => {
    expect(continuationsOf(RUN)).toEqual([])
  })

  it('passes the entries through when there are some', () => {
    const entries = [{ anime: FRIEREN, because: 'follows something you watched.' }]
    expect(continuationsOf({ ...RUN, continuations: entries })).toBe(entries)
  })
})

describe('promptLabel', () => {
  it('gives a run with no mood words instead of a blank', () => {
    expect(promptLabel(null)).toBe('no particular mood')
    expect(promptLabel('   ')).toBe('no particular mood')
  })

  it('shows the mood that was asked for', () => {
    expect(promptLabel('like Mushishi')).toBe('like Mushishi')
  })
})
