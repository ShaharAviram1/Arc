import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { routes } from '@/app/router'

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  return render(<RouterProvider router={router} />)
}

describe('router', () => {
  it('renders the not-found page for an unknown path, inside the layout', async () => {
    renderAt('/definitely-not-a-route')

    expect(await screen.findByText('Page not found')).toBeInTheDocument()
    expect(screen.getByText('/definitely-not-a-route')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to home' })).toBeInTheDocument()
    expect(screen.getByRole('navigation')).toBeInTheDocument()
  })

  it('renders /login without the sidebar', async () => {
    renderAt('/login')

    expect(await screen.findByText('Login')).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })
})
