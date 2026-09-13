import '@testing-library/jest-dom/vitest'
import { beforeEach } from 'vitest'
import { clearAspectCache } from '@/components/ui/aspect'

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
