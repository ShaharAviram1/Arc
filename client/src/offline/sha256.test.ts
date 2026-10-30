import { describe, expect, it } from 'vitest'
import { Sha256, sha256Hex } from '@/offline/sha256'

/** The FIPS 180-4 / NIST CAVS vectors, and `crypto.subtle` as the oracle for the rest. */

const text = (value: string) => new TextEncoder().encode(value)

async function subtleHex(data: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', data))
  return [...digest].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

describe('sha256', () => {
  it('matches the known vectors', () => {
    expect(sha256Hex(new Uint8Array(0))).toBe(
      'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    )
    expect(sha256Hex(text('abc'))).toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    )
    expect(sha256Hex(text('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq'))).toBe(
      '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
    )
  })

  it('hashes a million a’s fed in odd pieces', () => {
    const hash = new Sha256()
    const piece = text('a'.repeat(997))
    let left = 1_000_000
    while (left > 0) {
      const take = Math.min(left, piece.byteLength)
      hash.update(piece.subarray(0, take))
      left -= take
    }
    expect(hash.length).toBe(1_000_000)
    expect(hash.hex()).toBe('cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0')
  })

  it('agrees with crypto.subtle across every padding boundary and split', async () => {
    for (const size of [1, 55, 56, 63, 64, 65, 119, 120, 128, 1000, 4097]) {
      const data = Uint8Array.from({ length: size }, (_, i) => (i * 31 + size) % 256)
      const expected = await subtleHex(data)
      expect(sha256Hex(data)).toBe(expected)
      for (const cut of [1, 7, 64, size - 1]) {
        if (cut <= 0 || cut >= size) continue
        const split = new Sha256().update(data.subarray(0, cut)).update(data.subarray(cut)).hex()
        expect(split).toBe(expected)
      }
    }
  })

  it('refuses more input after the digest', () => {
    const hash = new Sha256().update(text('abc'))
    expect(hash.hex()).toBe(hash.hex())
    expect(() => hash.update(text('d'))).toThrow()
  })
})
