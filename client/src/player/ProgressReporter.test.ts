import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PROGRESS_PATH } from '@/lib/playback'
import {
  MIN_REPORT_GAP_MS,
  ProgressReporter,
  REPORT_INTERVAL_MS,
  SEEK_DEBOUNCE_MS,
} from '@/player/ProgressReporter'

const EPISODE_ID = 9001
const DURATION = 1436

type Beacon = (url: string, data?: BodyInit | null) => boolean

/** A `sendBeacon` that always accepts, typed so its recorded calls are too. */
function stubBeacon() {
  const beacon = vi.fn<Beacon>(() => true)
  vi.stubGlobal('navigator', { sendBeacon: beacon })
  return beacon
}

function reporter() {
  const report = vi.fn<(position: number, duration: number) => void>()
  const instance = new ProgressReporter({ episodeId: EPISODE_ID, report })
  return { instance, report }
}

/** The Blob a recorded `sendBeacon` call carried. */
function beaconBlob(beacon: ReturnType<typeof stubBeacon>, index = 0): Blob {
  const body = beacon.mock.calls[index]?.[1]
  if (!(body instanceof Blob)) throw new Error('beacon did not send a Blob')
  return body
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('ProgressReporter', () => {
  it('reports every ten seconds while playing (FR-S3)', () => {
    const { instance, report } = reporter()

    instance.play(30, DURATION)
    expect(report).toHaveBeenCalledExactlyOnceWith(30, DURATION)

    instance.update(40, DURATION)
    vi.advanceTimersByTime(REPORT_INTERVAL_MS)
    expect(report).toHaveBeenLastCalledWith(40, DURATION)

    instance.update(50, DURATION)
    vi.advanceTimersByTime(REPORT_INTERVAL_MS)
    expect(report).toHaveBeenLastCalledWith(50, DURATION)
    expect(report).toHaveBeenCalledTimes(3)

    instance.destroy()
  })

  it('reports immediately on pause and stops the timer', () => {
    const { instance, report } = reporter()

    instance.play(30, DURATION)
    report.mockClear()

    instance.pause(31, DURATION)
    expect(report).toHaveBeenCalledExactlyOnceWith(31, DURATION)

    vi.advanceTimersByTime(REPORT_INTERVAL_MS * 3)
    expect(report).toHaveBeenCalledTimes(1)

    instance.destroy()
  })

  it('waits for a seek to settle before reporting it', () => {
    const { instance, report } = reporter()

    instance.seek(100, DURATION)
    instance.seek(200, DURATION)
    vi.advanceTimersByTime(SEEK_DEBOUNCE_MS - 1)
    expect(report).not.toHaveBeenCalled()

    instance.seek(300, DURATION)
    vi.advanceTimersByTime(SEEK_DEBOUNCE_MS)
    expect(report).toHaveBeenCalledExactlyOnceWith(300, DURATION)

    instance.destroy()
  })

  it('never sends two ordinary reports inside two seconds', () => {
    const { instance, report } = reporter()

    // A pause is deliberate, so it goes out whatever the clock says.
    instance.pause(30, DURATION)
    expect(report).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(MIN_REPORT_GAP_MS - 1)
    instance.play(30, DURATION)
    expect(report).toHaveBeenCalledTimes(1)

    // The interval that the same `play` started is far enough away to send.
    vi.advanceTimersByTime(REPORT_INTERVAL_MS)
    expect(report).toHaveBeenCalledTimes(2)

    instance.destroy()
  })

  it('beacons the last position on unload, as JSON', async () => {
    const beacon = stubBeacon()
    const { instance, report } = reporter()

    instance.play(30, DURATION)
    instance.update(42.5, DURATION)
    window.dispatchEvent(new Event('pagehide'))

    expect(beacon).toHaveBeenCalledTimes(1)
    expect(beacon.mock.calls[0]?.[0]).toBe(PROGRESS_PATH)
    const blob = beaconBlob(beacon)
    expect(blob.type).toBe('application/json')
    expect(JSON.parse(await blob.text())).toEqual({
      episode_id: EPISODE_ID,
      position_s: 42.5,
      duration_s: DURATION,
    })
    // The beacon replaces the ordinary report; it does not duplicate it.
    expect(report).toHaveBeenCalledTimes(1)

    instance.destroy()
  })

  it('falls back to a keepalive fetch when there is no sendBeacon', () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response(null, { status: 204 })),
    )
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('fetch', fetchMock)
    const { instance } = reporter()

    instance.update(12, DURATION)
    window.dispatchEvent(new Event('pagehide'))

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.keepalive).toBe(true)
    expect(init?.method).toBe('POST')

    instance.destroy()
  })

  it('sends one final report when the media ends, then goes quiet (FR-S4)', () => {
    const beacon = stubBeacon()
    const { instance, report } = reporter()

    instance.play(1400, DURATION)
    report.mockClear()

    instance.end(DURATION, DURATION)
    expect(report).toHaveBeenCalledExactlyOnceWith(DURATION, DURATION)

    // Nothing the element does afterwards — a stray timeupdate, the seek back
    // to zero some browsers do — reopens the episode.
    instance.update(0, DURATION)
    instance.seek(0, DURATION)
    instance.pause(0, DURATION)
    vi.advanceTimersByTime(REPORT_INTERVAL_MS * 3)
    window.dispatchEvent(new Event('pagehide'))

    expect(report).toHaveBeenCalledTimes(1)
    expect(beacon).not.toHaveBeenCalled()

    instance.destroy()
  })

  it('beacons an unsaved position when the player unmounts', () => {
    const beacon = stubBeacon()
    const { instance } = reporter()

    instance.play(30, DURATION)
    instance.update(35, DURATION)
    instance.destroy()

    expect(beacon).toHaveBeenCalledTimes(1)
  })

  it('does not beacon on unmount when the server already has the position', () => {
    const beacon = stubBeacon()
    const { instance } = reporter()

    instance.pause(30, DURATION)
    instance.destroy()

    expect(beacon).not.toHaveBeenCalled()
  })

  it('does not beacon on unload when the server already has the position', () => {
    const beacon = stubBeacon()
    const { instance } = reporter()

    instance.play(30, DURATION)
    window.dispatchEvent(new Event('pagehide'))

    expect(beacon).not.toHaveBeenCalled()

    instance.destroy()
  })

  /**
   * `POST /api/progress` requires `duration_s > 0` and answers 422 otherwise,
   * so a position taken before the media knows its own length waits rather
   * than being thrown away.
   */
  it('holds back a sample with no duration until one arrives', () => {
    const beacon = stubBeacon()
    const { instance, report } = reporter()

    instance.play(30, Number.NaN)
    expect(report).not.toHaveBeenCalled()

    // Nor does the unload path send the unusable sample.
    window.dispatchEvent(new Event('pagehide'))
    expect(beacon).not.toHaveBeenCalled()

    // The first duration the element reports makes the position sendable, and
    // the timer `play` started carries it out.
    instance.update(40, DURATION)
    vi.advanceTimersByTime(REPORT_INTERVAL_MS)
    expect(report).toHaveBeenCalledExactlyOnceWith(40, DURATION)

    instance.destroy()
  })

  it('reports the position on pause once a duration is known', () => {
    const { instance, report } = reporter()

    instance.play(30, 0)
    expect(report).not.toHaveBeenCalled()

    instance.pause(31, DURATION)
    expect(report).toHaveBeenCalledExactlyOnceWith(31, DURATION)

    instance.destroy()
  })

  it('exposes the last position it was told about', () => {
    const { instance } = reporter()

    expect(instance.lastPosition).toBeNull()

    instance.play(30, DURATION)
    expect(instance.lastPosition).toBe(30)

    instance.update(88.5, DURATION)
    expect(instance.lastPosition).toBe(88.5)

    instance.destroy()
  })
})
