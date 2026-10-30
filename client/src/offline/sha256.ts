/**
 * SHA-256, incrementally (FIPS 180-4), for "delivered means verified" (M20.4
 * E, owner 2026-10-30).
 *
 * The download worker feeds every chunk it writes through {@link Sha256} as
 * the bytes arrive, so a finished copy carries its digest without reading the
 * file again; the confirmation and the holdings report send it and the server
 * compares it with the copy it made. `crypto.subtle.digest` cannot do this: it
 * takes the whole input at once, which for a 400 MB episode on an iPad means
 * the whole file in memory.
 *
 * Pure TypeScript, no dependency. It runs inside the worker (never on the
 * main thread for an episode's bytes); the test checks it against the FIPS
 * vectors and against `crypto.subtle` over split inputs.
 */

const K = Uint32Array.from([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
])

const INITIAL = [
  0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
]

export class Sha256 {
  private readonly h = Uint32Array.from(INITIAL)
  private readonly w = new Uint32Array(64)
  /** A partial block carried between updates. */
  private readonly block = new Uint8Array(64)
  private blockLength = 0
  /** Bytes hashed so far. A number is exact up to 2^53, far past any episode. */
  private total = 0
  private finished = false

  /** How many bytes have gone in. */
  get length(): number {
    return this.total
  }

  update(data: Uint8Array): this {
    if (this.finished) throw new Error('sha256: update after digest')
    this.total += data.byteLength
    let offset = 0
    if (this.blockLength > 0) {
      const take = Math.min(64 - this.blockLength, data.byteLength)
      this.block.set(data.subarray(0, take), this.blockLength)
      this.blockLength += take
      offset = take
      if (this.blockLength < 64) return this
      this.compress(this.block, 0)
      this.blockLength = 0
    }
    while (data.byteLength - offset >= 64) {
      this.compress(data, offset)
      offset += 64
    }
    if (offset < data.byteLength) {
      this.block.set(data.subarray(offset), 0)
      this.blockLength = data.byteLength - offset
    }
    return this
  }

  /** The digest, as 64 lowercase hex characters. Ends the hash. */
  hex(): string {
    let out = ''
    for (const byte of this.digest()) out += byte.toString(16).padStart(2, '0')
    return out
  }

  /** The 32-byte digest. Ends the hash. */
  digest(): Uint8Array {
    if (!this.finished) {
      const bits = this.total * 8
      const padding = new Uint8Array(this.blockLength < 56 ? 64 : 128)
      padding.set(this.block.subarray(0, this.blockLength))
      padding[this.blockLength] = 0x80
      const view = new DataView(padding.buffer)
      view.setUint32(padding.byteLength - 8, Math.floor(bits / 0x1_0000_0000))
      view.setUint32(padding.byteLength - 4, bits >>> 0)
      for (let offset = 0; offset < padding.byteLength; offset += 64) {
        this.compress(padding, offset)
      }
      this.finished = true
    }
    const out = new Uint8Array(32)
    const view = new DataView(out.buffer)
    for (let i = 0; i < 8; i += 1) view.setUint32(i * 4, this.h[i] ?? 0)
    return out
  }

  private compress(data: Uint8Array, offset: number): void {
    const w = this.w
    const h = this.h
    for (let i = 0; i < 16; i += 1) {
      const j = offset + i * 4
      w[i] = (data[j]! << 24) | (data[j + 1]! << 16) | (data[j + 2]! << 8) | data[j + 3]!
    }
    for (let i = 16; i < 64; i += 1) {
      const x = w[i - 15]!
      const y = w[i - 2]!
      const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3)
      const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10)
      w[i] = (w[i - 16]! + s0 + w[i - 7]! + s1) | 0
    }
    let a = h[0]!
    let b = h[1]!
    let c = h[2]!
    let d = h[3]!
    let e = h[4]!
    let f = h[5]!
    let g = h[6]!
    let k = h[7]!
    for (let i = 0; i < 64; i += 1) {
      const s1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7))
      const choice = (e & f) ^ (~e & g)
      const t1 = (k + s1 + choice + K[i]! + w[i]!) | 0
      const s0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10))
      const majority = (a & b) ^ (a & c) ^ (b & c)
      const t2 = (s0 + majority) | 0
      k = g
      g = f
      f = e
      e = (d + t1) | 0
      d = c
      c = b
      b = a
      a = (t1 + t2) | 0
    }
    h[0] = h[0]! + a
    h[1] = h[1]! + b
    h[2] = h[2]! + c
    h[3] = h[3]! + d
    h[4] = h[4]! + e
    h[5] = h[5]! + f
    h[6] = h[6]! + g
    h[7] = h[7]! + k
  }
}

/** The hex SHA-256 of `data`, in one go (tests, small inputs). */
export function sha256Hex(data: Uint8Array): string {
  return new Sha256().update(data).hex()
}
