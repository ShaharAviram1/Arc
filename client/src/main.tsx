import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import { router } from '@/app/router'
import { watchPendingLogout } from '@/lib/auth'
import { queryClient } from '@/lib/queryClient'
import '@/index.css'

// A sign-out made with no network is sent at launch and on every reconnect
// (FR-S9); until then the app treats itself as signed out.
watchPendingLogout()

const container = document.getElementById('root')
if (!container) {
  throw new Error('Root element #root not found')
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
