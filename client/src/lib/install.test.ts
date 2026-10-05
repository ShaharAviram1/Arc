import { afterEach, describe, expect, it } from 'vitest'
import { installGuide, isChromeOnIos, isIos, isIosSafari, isStandalone } from '@/lib/install'
import { resetDevice, stubDevice, UA } from '@/test/device'

afterEach(() => {
  resetDevice()
})

describe('isStandalone', () => {
  it('is false in a browser tab', () => {
    stubDevice({ ua: UA.iphoneSafari, touchPoints: 5 })
    expect(isStandalone()).toBe(false)
  })

  it('is true from the Home Screen', () => {
    stubDevice({ ua: UA.ipadSafari, touchPoints: 5, standalone: true })
    expect(isStandalone()).toBe(true)
  })

  it('believes the display-mode query alone, as a browser without navigator.standalone says it', () => {
    stubDevice({ ua: UA.windowsChrome, standalone: true })
    Reflect.deleteProperty(navigator, 'standalone')
    expect(isStandalone()).toBe(true)
  })
})

describe('device detection', () => {
  it('sees an iPad through its Mac user agent by its touch points', () => {
    stubDevice({ ua: UA.ipadSafari, touchPoints: 5 })
    expect(isIos()).toBe(true)
    expect(isIosSafari()).toBe(true)
    expect(isChromeOnIos()).toBe(false)
  })

  it('does not mistake a Mac for an iPad', () => {
    stubDevice({ ua: UA.macSafari, touchPoints: 0 })
    expect(isIos()).toBe(false)
    expect(isIosSafari()).toBe(false)
  })

  it('tells Chrome on iOS from Safari', () => {
    stubDevice({ ua: UA.ipadChrome, touchPoints: 5 })
    expect(isIos()).toBe(true)
    expect(isChromeOnIos()).toBe(true)
    expect(isIosSafari()).toBe(false)
  })

  it('matches neither for Firefox on iOS', () => {
    stubDevice({ ua: UA.iphoneFirefox, touchPoints: 5 })
    expect(isIos()).toBe(true)
    expect(isIosSafari()).toBe(false)
    expect(isChromeOnIos()).toBe(false)
  })
})

describe('installGuide', () => {
  it.each([
    ['iPad Safari', { ua: UA.ipadSafari, touchPoints: 5 }, 'safari'],
    ['iPhone Safari', { ua: UA.iphoneSafari, touchPoints: 5 }, 'safari'],
    ['Chrome on iPad', { ua: UA.ipadChrome, touchPoints: 5 }, 'chrome-ios'],
    ['Firefox on iPhone', { ua: UA.iphoneFirefox, touchPoints: 5 }, null],
    ['a Mac', { ua: UA.macSafari, touchPoints: 0 }, null],
    ['Windows', { ua: UA.windowsChrome, touchPoints: 0 }, null],
    ['the installed app', { ua: UA.ipadSafari, touchPoints: 5, standalone: true }, null],
  ] as const)('%s → %s', (_name, device, expected) => {
    stubDevice(device)
    expect(installGuide()).toBe(expected)
  })
})
