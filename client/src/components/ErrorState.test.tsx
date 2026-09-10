import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ErrorState } from '@/components/ErrorState'

describe('ErrorState', () => {
  it('announces the message it was given', () => {
    render(<ErrorState message="Could not load your home page." />)

    expect(screen.getByRole('alert')).toHaveTextContent('Could not load your home page.')
  })

  it('offers no retry when there is nothing useful to retry', () => {
    render(<ErrorState message="That is gone." />)

    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })

  it('calls onRetry when the button is used', async () => {
    const onRetry = vi.fn()
    render(<ErrorState message="Search failed. Try again." onRetry={onRetry} />)

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('goes quiet while the retry it started is still in flight', async () => {
    const onRetry = vi.fn()
    render(<ErrorState message="Search failed. Try again." onRetry={onRetry} pending />)

    const button = screen.getByRole('button', { name: 'Try again' })
    expect(button).toBeDisabled()

    await userEvent.click(button)
    expect(onRetry).not.toHaveBeenCalled()
  })
})
