import '@testing-library/jest-dom/vitest'
import { beforeEach } from 'vitest'
import { clearAspectCache } from '@/components/ui/aspect'
import { setDownloads } from '@/offline/downloads'
import { openStore } from '@/offline/store'

/**
 * Measured image shapes live in a module-level store for the life of the tab
 * (see `components/ui/aspect.ts`), which is what stops Watch Now's carousel
 * flashing the blurred wash on every rotation — and what would otherwise leak
 * between tests: the suites measure one and the same fixture url as a 16:9
 * backdrop in one test and a 4.75:1 strip in the next.
 */
beforeEach(() => {
  clearAspectCache()
})

/**
 * The offline caches (FR-S9) are module-level memory stores under jsdom (no
 * IndexedDB): a remembered user or page from one test would otherwise answer
 * for a network failure in the next. The outbox's own store is left to the
 * outbox suites, which manage it themselves.
 */
beforeEach(async () => {
  setDownloads(null)
  await Promise.all(
    (['downloads', 'player', 'payloads', 'covers', 'session'] as const).map((name) =>
      openStore(name).clear(),
    ),
  )
})
