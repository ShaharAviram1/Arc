import { fileURLToPath, URL } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Dev server proxies the API and media routes to the FastAPI app on :8000.
// changeOrigin stays false so the Host header (and therefore the session
// cookie's domain) is preserved end to end.
const API_ORIGIN = 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: API_ORIGIN,
        changeOrigin: false,
      },
      '/media': {
        target: API_ORIGIN,
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // Reported only when asked for (`vitest run --coverage`, which is what CI
    // runs); a plain `pnpm test` is unaffected. `include` is what decides the
    // denominator — a source file no test ever imports is still counted, at
    // 0 %, rather than silently flattering the total.
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      reportsDirectory: './coverage',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/main.tsx', 'src/test/**', 'src/**/*.test.{ts,tsx}', 'src/**/*.d.ts'],
    },
  },
})
