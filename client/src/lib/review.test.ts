import { describe, expect, it } from 'vitest'
import { ApiError } from '@/lib/api'
import { CATALOGUE_UNAVAILABLE_MESSAGE } from '@/lib/anime'
import {
  ALREADY_LINKED_MESSAGE,
  directoryLabel,
  episodeLabel,
  formatConfidence,
  formatScore,
  formatSize,
  GONE_MESSAGE,
  NOT_IGNORED_MESSAGE,
  parseEpisodeInput,
  parsedChips,
  REVIEW_CONFLICT_DETAILS,
  reviewErrorMessage,
  reviewQueueQueryKey,
  reviewSummaryQueryKey,
  startingEpisode,
  SUGGESTIONS_OFF_MESSAGE,
  suggestionOf,
  suggestionsEnabled,
} from '@/lib/review'
import { REVIEW_SUGGESTION, reviewItem, reviewParsed } from '@/test/animeFixtures'

/** An `ApiError` shaped like the ones `apiFetch` throws. */
function apiError(status: number, detail: string): ApiError {
  return new ApiError(status, { detail }, `POST /api/review/1/confirm → ${String(status)}`)
}

describe('formatSize', () => {
  it('names the unit a person would use', () => {
    expect(formatSize(512)).toBe('512 B')
    expect(formatSize(1024)).toBe('1 KB')
    expect(formatSize(1024 * 1024 * 350)).toBe('350 MB')
    expect(formatSize(1_503_238_553)).toBe('1.4 GB')
  })

  it('says so rather than guessing when the scan recorded no size', () => {
    expect(formatSize(null)).toBe('size unknown')
    expect(formatSize(Number.NaN)).toBe('size unknown')
  })
})

describe('formatConfidence', () => {
  it('reads as a percentage', () => {
    expect(formatConfidence(0.62)).toBe('62% sure')
    expect(formatConfidence(0)).toBe('0% sure')
  })

  it('has nothing to report once a person has decided', () => {
    // Confirming clears the score: it described the matcher, not the file.
    expect(formatConfidence(null)).toBe('no score')
  })
})

describe('formatScore', () => {
  it('is a bare percentage, and blank when there is none', () => {
    expect(formatScore(0.415)).toBe('42%')
    expect(formatScore(null)).toBe('')
  })
})

describe('episodeLabel', () => {
  it('pads the episode and names the season only when there is one', () => {
    expect(episodeLabel(reviewParsed({ episode: 5, season: null }))).toBe('E05')
    expect(episodeLabel(reviewParsed({ episode: 12, season: 2 }))).toBe('S2E12')
  })

  it('is empty when the filename claimed no episode', () => {
    expect(episodeLabel(reviewParsed({ episode: null }))).toBe('')
  })
})

describe('parsedChips', () => {
  it('lists what the filename claimed, in reading order', () => {
    expect(parsedChips(reviewParsed())).toEqual(['Sousou no Frieren', 'E05', 'SubsPlease', '1080p'])
  })

  it('leaves out what the parser could not find, rather than showing a blank', () => {
    expect(parsedChips(reviewParsed({ group: null, resolution: null, episode: null }))).toEqual([
      'Sousou no Frieren',
    ])
  })

  it('names an unusual kind but never the ordinary one', () => {
    expect(parsedChips(reviewParsed({ kind: 'movie' }))).toContain('movie')
    expect(parsedChips(reviewParsed({ kind: 'episode' }))).not.toContain('episode')
  })
})

describe('directoryLabel', () => {
  it('gives the library root a name', () => {
    expect(directoryLabel('')).toBe('library root')
    expect(directoryLabel('downloads/Frieren')).toBe('downloads/Frieren')
  })
})

describe('parseEpisodeInput', () => {
  it('accepts a whole episode number from one', () => {
    expect(parseEpisodeInput('5')).toBe(5)
    expect(parseEpisodeInput(' 12 ')).toBe(12)
  })

  it('rejects everything that is not one', () => {
    expect(parseEpisodeInput('')).toBeNull()
    expect(parseEpisodeInput('0')).toBeNull()
    expect(parseEpisodeInput('-3')).toBeNull()
    expect(parseEpisodeInput('1.5')).toBeNull()
    expect(parseEpisodeInput('five')).toBeNull()
  })
})

describe('startingEpisode', () => {
  it('prefers the candidate’s own number over the filename’s', () => {
    expect(startingEpisode(9, reviewParsed({ episode: 5 }))).toBe(9)
  })

  it('falls back to the filename, then to nothing', () => {
    expect(startingEpisode(null, reviewParsed({ episode: 5 }))).toBe(5)
    expect(startingEpisode(null, reviewParsed({ episode: null }))).toBeNull()
  })
})

describe('suggestionOf', () => {
  it('reads a server that has no suggestions at all as having none', () => {
    expect(suggestionOf(reviewItem())).toBeNull()
    expect(suggestionOf(reviewItem({ suggestion: null }))).toBeNull()
    expect(suggestionOf(reviewItem({ suggestion: REVIEW_SUGGESTION }))).toBe(REVIEW_SUGGESTION)
  })
})

describe('suggestionsEnabled', () => {
  it('defaults to off when the server does not say', () => {
    expect(suggestionsEnabled({ items: [], pending: 0 })).toBe(false)
    expect(suggestionsEnabled({ items: [], pending: 0, suggestions_enabled: true })).toBe(true)
  })
})

describe('reviewErrorMessage', () => {
  it('tells the two 409s apart', () => {
    expect(reviewErrorMessage(apiError(409, REVIEW_CONFLICT_DETAILS.alreadyLinked))).toBe(
      ALREADY_LINKED_MESSAGE,
    )
    expect(reviewErrorMessage(apiError(409, REVIEW_CONFLICT_DETAILS.notIgnored))).toBe(
      NOT_IGNORED_MESSAGE,
    )
  })

  it('says a file has gone rather than showing the server’s own words', () => {
    expect(reviewErrorMessage(apiError(404, 'review item not found'))).toBe(GONE_MESSAGE)
  })

  it('blames the catalogue for a failed search', () => {
    expect(reviewErrorMessage(apiError(502, 'catalogue unavailable'))).toBe(
      CATALOGUE_UNAVAILABLE_MESSAGE,
    )
  })

  it('tells a switched-off suggestion apart from a busy server, both 503', () => {
    expect(reviewErrorMessage(apiError(503, 'Suggestions are not enabled'))).toBe(
      SUGGESTIONS_OFF_MESSAGE,
    )
    expect(reviewErrorMessage(apiError(503, 'busy'))).toBe('Try again in a moment.')
  })

  it('falls back to something a person can act on', () => {
    expect(reviewErrorMessage(new Error('offline'))).toMatch(/Could not reach the server/)
    expect(reviewErrorMessage(apiError(500, 'boom'))).toMatch(/Something went wrong/)
  })
})

describe('query keys', () => {
  it('keeps every listing under the queue prefix, apart from the badge', () => {
    expect(reviewQueueQueryKey('pending')).toEqual(['review', 'queue', 'pending'])
    expect(reviewQueueQueryKey('auto')).toEqual(['review', 'queue', 'auto'])
    expect(reviewSummaryQueryKey).toEqual(['review', 'summary'])
  })
})
