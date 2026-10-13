/**
 * The subtitle track at play time (spec FR-S1, FR-S10; architecture §5.4a, M20).
 *
 * A remuxed rendition carries its subtitles as an HLS subtitle rendition
 * (WebVTT, `DEFAULT=YES,AUTOSELECT=YES`), so the track is on by default and the
 * player only ever has to do one thing with it: switch it off, or back on.
 * There are two ways the track reaches the page, and each has its own switch:
 *
 * - **hls.js** (every browser with Media Source Extensions) renders the track
 *   as a native `TextTrack` (`renderTextTracksNatively`) but owns its mode:
 *   the switch is `hls.subtitleTrack` (`-1` disables it) and
 *   `hls.subtitleDisplay`. Setting the `TextTrack`'s mode behind hls.js's back
 *   would be undone on its next track update.
 * - **Native HLS** (iOS Safari) exposes the subtitle rendition as an entry in
 *   `video.textTracks`, which appears once the metadata has loaded: the switch
 *   is that track's `mode`, `showing` or `disabled`.
 *
 * The viewer's choice is remembered on the device, per account, in
 * `localStorage` — a per-browser convenience with nothing to tell the server.
 * Every access is wrapped: a browser that blocks site data throws from the
 * accessor itself, and the answer then is simply "on", the default.
 */

/** The part of an hls.js instance the switch touches. */
export interface HlsSubtitleControl {
  subtitleTrack: number
  subtitleDisplay: boolean
  readonly subtitleTracks: readonly { default: boolean }[]
}

/**
 * hls.js's half of the switch. Off disables the track (`-1`), so its segments
 * stop loading too; on picks the playlist's default track (the first, failing
 * that) when none is selected, and makes sure it is displayed.
 */
export function applyHlsSubtitles(hls: HlsSubtitleControl, on: boolean): void {
  if (!on) {
    if (hls.subtitleTrack !== -1) hls.subtitleTrack = -1
    hls.subtitleDisplay = false
    return
  }
  hls.subtitleDisplay = true
  if (hls.subtitleTrack === -1 && hls.subtitleTracks.length > 0) {
    const preferred = hls.subtitleTracks.findIndex((track) => track.default)
    hls.subtitleTrack = preferred >= 0 ? preferred : 0
  }
}

function isSubtitleTrack(track: TextTrack): boolean {
  return track.kind === 'subtitles' || track.kind === 'captions'
}

/**
 * The native half: the first subtitle track `showing` when on, every subtitle
 * track `disabled` when off. A list the element does not have (jsdom, an old
 * browser) is a no-op. Metadata and chapter tracks are left alone.
 */
export function showTextTracks(tracks: TextTrackList | null | undefined, on: boolean): void {
  if (tracks === null || tracks === undefined) return
  let shown = false
  for (const track of Array.from(tracks)) {
    if (!isSubtitleTrack(track)) continue
    const mode: TextTrackMode = on && !shown ? 'showing' : 'disabled'
    if (mode === 'showing') shown = true
    if (track.mode !== mode) track.mode = mode
  }
}

const SUBTITLES_OFF_KEY_PREFIX = 'arc:subtitles-off:'

function subtitlesKey(owner: number): string {
  return `${SUBTITLES_OFF_KEY_PREFIX}${String(owner)}`
}

/** Whether this account wants subtitles on this device. On unless it said otherwise. */
export function subtitlesRemembered(owner: number | null): boolean {
  if (owner === null) return true
  try {
    return window.localStorage.getItem(subtitlesKey(owner)) !== '1'
  } catch {
    return true
  }
}

/** Remembers the choice; with nobody signed in yet it lasts for this page only. */
export function rememberSubtitles(owner: number | null, on: boolean): void {
  if (owner === null) return
  try {
    if (on) window.localStorage.removeItem(subtitlesKey(owner))
    else window.localStorage.setItem(subtitlesKey(owner), '1')
  } catch {
    // Storage blocked: the choice holds until the page is left, which is the
    // harmless half of this failure.
  }
}
