import { act, fireEvent, render, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { HlsVideo, LIVE_SYNC_DURATION_S } from '@/player/HlsVideo'
import { Events, instances, resetHls } from '@/test/hlsMock'

vi.mock('hls.js', async () => await import('@/test/hlsMock'))

const SRC = '/media/9001/index.m3u8'

let canPlayTypeSpy: MockInstance<HTMLMediaElement['canPlayType']> | null = null

/** iOS Safari: no Media Source Extensions, and a yes to the HLS mime. */
function stubNativeHls(): void {
  canPlayTypeSpy = vi
    .spyOn(window.HTMLMediaElement.prototype, 'canPlayType')
    .mockReturnValue('probably')
  vi.stubGlobal('MediaSource', undefined)
}

/** A stand-in for a `TextTrack`: jsdom has none. */
function fakeTrack(kind: TextTrackKind, mode: TextTrackMode = 'disabled'): TextTrack {
  return { kind, mode } as TextTrack
}

/**
 * Installs `textTracks` on the element the way Safari would expose the master
 * playlist's subtitle rendition: an iterable list that can grow.
 */
function equipTextTracks(video: HTMLVideoElement, tracks: TextTrack[]): EventTarget {
  const list = Object.assign(new EventTarget(), {
    get length() {
      return tracks.length
    },
    [Symbol.iterator]: () => tracks[Symbol.iterator](),
  })
  Object.defineProperty(video, 'textTracks', { configurable: true, get: () => list })
  return list
}

function renderVideo(props: { startPosition?: number; subtitles?: boolean } = {}) {
  const videoRef = createRef<HTMLVideoElement>()
  const view = render(<HlsVideo src={SRC} videoRef={videoRef} label="Episode 1" {...props} />)
  return {
    ...view,
    rerenderWith: (next: { startPosition?: number; subtitles?: boolean }) => {
      view.rerender(<HlsVideo src={SRC} videoRef={videoRef} label="Episode 1" {...next} />)
    },
  }
}

async function hlsInstance() {
  await waitFor(() => {
    expect(instances).toHaveLength(1)
  })
  const hls = instances[0]
  if (hls === undefined) throw new Error('hls.js was not constructed')
  return hls
}

beforeEach(() => {
  resetHls()
})

afterEach(() => {
  canPlayTypeSpy?.mockRestore()
  canPlayTypeSpy = null
  vi.unstubAllGlobals()
})

describe('HlsVideo — hls.js configuration (M20 T3)', () => {
  it('starts loading at the resume position, with the text track rendered natively', async () => {
    renderVideo({ startPosition: 754 })

    const hls = await hlsInstance()
    expect(hls.config.startPosition).toBe(754)
    expect(hls.config.renderTextTracksNatively).toBe(true)
    // Not a config option in hls.js 1.7: a property of the instance, set on.
    expect(hls.subtitleDisplay).toBe(true)
    // The credentials hook is kept.
    const xhr = { withCredentials: false } as XMLHttpRequest
    hls.config.xhrSetup?.(xhr, SRC)
    expect(xhr.withCredentials).toBe(true)
  })

  it('starts at 0, never at the live edge, when there is nothing to resume', async () => {
    renderVideo()

    const hls = await hlsInstance()
    expect(hls.config.startPosition).toBe(0)
  })
})

describe('HlsVideo — the subtitle switch on hls.js (FR-S10)', () => {
  it('leaves hls.js its default track when subtitles are on', async () => {
    renderVideo({ subtitles: true })
    const hls = await hlsInstance()
    hls.subtitleTracks = [{ default: true }]

    act(() => {
      hls.emit(Events.SUBTITLE_TRACKS_UPDATED, {})
    })

    expect(hls.subtitleTrack).toBe(0)
    expect(hls.subtitleDisplay).toBe(true)
  })

  it('disables the track hls.js selects when the viewer turned subtitles off', async () => {
    renderVideo({ subtitles: false })
    const hls = await hlsInstance()
    hls.subtitleTracks = [{ default: true }]

    // hls.js auto-selects the DEFAULT=YES track; the switch undoes it.
    act(() => {
      hls.subtitleTrack = 0
      hls.emit(Events.SUBTITLE_TRACK_SWITCH, { id: 0 })
    })

    expect(hls.subtitleTrack).toBe(-1)
    expect(hls.subtitleDisplay).toBe(false)
  })

  it('follows the prop both ways without recreating the stream', async () => {
    const { rerenderWith } = renderVideo({ subtitles: true, startPosition: 10 })
    const hls = await hlsInstance()
    hls.subtitleTracks = [{ default: false }, { default: true }]
    act(() => {
      hls.emit(Events.SUBTITLE_TRACKS_UPDATED, {})
    })
    expect(hls.subtitleTrack).toBe(1)

    rerenderWith({ subtitles: false, startPosition: 99 })
    expect(hls.subtitleTrack).toBe(-1)

    rerenderWith({ subtitles: true, startPosition: 99 })
    expect(hls.subtitleTrack).toBe(1)
    expect(hls.subtitleDisplay).toBe(true)
    expect(instances).toHaveLength(1)
  })
})

describe('HlsVideo — the subtitle switch on native HLS (FR-S10)', () => {
  it('sets the text track mode after loadedmetadata', () => {
    stubNativeHls()
    const { container } = renderVideo({ subtitles: true })
    const video = container.querySelector('video')
    if (video === null) throw new Error('no video')
    expect(video).toHaveAttribute('src', SRC)

    const subs = fakeTrack('subtitles')
    const chapters = fakeTrack('chapters')
    equipTextTracks(video, [subs, chapters])
    fireEvent.loadedMetadata(video)

    expect(subs.mode).toBe('showing')
    expect(chapters.mode).toBe('disabled')
    expect(instances).toHaveLength(0)
  })

  it('keeps the track disabled when the viewer turned subtitles off', () => {
    stubNativeHls()
    const { container } = renderVideo({ subtitles: false })
    const video = container.querySelector('video')
    if (video === null) throw new Error('no video')
    const subs = fakeTrack('subtitles', 'showing')
    equipTextTracks(video, [subs])

    fireEvent.loadedMetadata(video)

    expect(subs.mode).toBe('disabled')
  })

  it('toggles the native track when the prop changes', () => {
    stubNativeHls()
    const { container, rerenderWith } = renderVideo({ subtitles: true })
    const video = container.querySelector('video')
    if (video === null) throw new Error('no video')
    const subs = fakeTrack('subtitles')
    equipTextTracks(video, [subs])
    fireEvent.loadedMetadata(video)
    expect(subs.mode).toBe('showing')

    rerenderWith({ subtitles: false })
    expect(subs.mode).toBe('disabled')

    rerenderWith({ subtitles: true })
    expect(subs.mode).toBe('showing')
  })

  it('applies the choice to a track Safari adds after the metadata', () => {
    stubNativeHls()
    const tracks: TextTrack[] = []
    const list = Object.assign(new EventTarget(), {
      get length() {
        return tracks.length
      },
      [Symbol.iterator]: () => tracks[Symbol.iterator](),
    })
    const descriptor = Object.getOwnPropertyDescriptor(
      window.HTMLMediaElement.prototype,
      'textTracks',
    )
    Object.defineProperty(window.HTMLMediaElement.prototype, 'textTracks', {
      configurable: true,
      get: () => list,
    })
    try {
      const { container } = renderVideo({ subtitles: false })
      const video = container.querySelector('video')
      if (video === null) throw new Error('no video')
      fireEvent.loadedMetadata(video)

      const late = fakeTrack('subtitles', 'showing')
      tracks.push(late)
      list.dispatchEvent(new Event('addtrack'))

      expect(late.mode).toBe('disabled')
    } finally {
      if (descriptor === undefined) {
        Reflect.deleteProperty(window.HTMLMediaElement.prototype, 'textTracks')
      } else {
        Object.defineProperty(window.HTMLMediaElement.prototype, 'textTracks', descriptor)
      }
    }
  })

  it('leaves the tracks alone when the stream has no subtitle switch', () => {
    stubNativeHls()
    const { container } = renderVideo()
    const video = container.querySelector('video')
    if (video === null) throw new Error('no video')
    const subs = fakeTrack('subtitles', 'showing')
    equipTextTracks(video, [subs])

    fireEvent.loadedMetadata(video)

    expect(subs.mode).toBe('showing')
  })
})

/**
 * A growing (EVENT) playlist (M20 T7, architecture §5.4a): hls.js must not
 * chase the download front, and Safari is put at the start position by hand,
 * whatever its `duration` says.
 */
describe('HlsVideo — a live stream (M20 T7)', () => {
  /** Safari's element after it read an EVENT playlist: at the edge, no end. */
  function atTheEdge(video: HTMLVideoElement, edge: number): void {
    let currentTime = edge
    Object.defineProperties(video, {
      currentTime: {
        configurable: true,
        get: () => currentTime,
        set: (value: number) => {
          currentTime = value
        },
      },
      duration: { configurable: true, get: () => Number.POSITIVE_INFINITY },
    })
  }

  it('configures hls.js never to chase the edge', async () => {
    const videoRef = createRef<HTMLVideoElement>()
    render(<HlsVideo src={SRC} videoRef={videoRef} label="Episode 1" startPosition={0} live />)

    const hls = await hlsInstance()
    expect(hls.config.startPosition).toBe(0)
    expect(hls.config.liveSyncDuration).toBe(LIVE_SYNC_DURATION_S)
    expect(hls.config.liveSyncDuration).toBeGreaterThanOrEqual(24 * 3600)
    expect('liveMaxLatencyDuration' in hls.config).toBe(true)
    expect(hls.config.liveMaxLatencyDuration).toBeUndefined()
    expect(hls.config.maxLiveSyncPlaybackRate).toBe(1)
  })

  it('leaves hls.js its defaults for a whole episode', async () => {
    renderVideo({ startPosition: 12 })

    const hls = await hlsInstance()
    expect(hls.config.liveSyncDuration).toBeUndefined()
    expect('maxLiveSyncPlaybackRate' in hls.config).toBe(false)
  })

  it('on Safari, seeks to the start after loadedmetadata, ignoring an Infinity duration', () => {
    stubNativeHls()
    const onReady = vi.fn()
    const videoRef = createRef<HTMLVideoElement>()
    const { container } = render(
      <HlsVideo
        src={SRC}
        videoRef={videoRef}
        label="Episode 1"
        startPosition={0}
        live
        onReady={onReady}
      />,
    )
    const video = container.querySelector('video')
    if (video === null) throw new Error('no video')
    atTheEdge(video, 300)

    fireEvent.loadedMetadata(video)

    expect(video.currentTime).toBe(0)
    expect(instances).toHaveLength(0)
    // The duration is handed on as it is; the page decides not to trust it.
    expect(onReady).toHaveBeenCalledWith(Number.POSITIVE_INFINITY)

    // Safari reloading the playlist must not send the viewer back again.
    video.currentTime = 120
    fireEvent.loadedMetadata(video)
    expect(video.currentTime).toBe(120)
  })

  it('on Safari, seeks to a resume start even though the duration is Infinity', () => {
    stubNativeHls()
    const videoRef = createRef<HTMLVideoElement>()
    const { container } = render(
      <HlsVideo src={SRC} videoRef={videoRef} label="Episode 1" startPosition={299} live />,
    )
    const video = container.querySelector('video')
    if (video === null) throw new Error('no video')
    atTheEdge(video, 0)

    fireEvent.loadedMetadata(video)

    expect(video.currentTime).toBe(299)
  })
})
