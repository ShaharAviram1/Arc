# Arc client

React 19 + TypeScript 6 + Vite + Tailwind + TanStack Query SPA.

```
pnpm install        # once
pnpm dev            # dev server on http://localhost:5173
pnpm test           # vitest (watch); pnpm test -- --run for a single pass
pnpm lint           # eslint, zero warnings allowed
pnpm format         # prettier --write .
pnpm build          # tsc -b && vite build  -> dist/
```

`pnpm dev` proxies `/api` and `/media` to the FastAPI app on `http://localhost:8000`
(`changeOrigin: false`, so the session cookie survives). Start the API with `make dev`
from the repo root; without it the Home page shows "API: unreachable".
