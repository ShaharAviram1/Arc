import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { OutboxProblemRows } from '@/offline/OutboxProblems'
import {
  Outbox,
  setOutbox,
  STUCK_AFTER_MS,
  STUCK_ATTEMPTS,
  type SyncResponse,
} from '@/offline/outbox'
import { memoryStore } from '@/offline/store'
import { outboxRowCount, useOutboxProblems } from '@/offline/useOutbox'

function Rows() {
  const problems = useOutboxProblems()
  return <ul>{outboxRowCount(problems) > 0 ? <OutboxProblemRows problems={problems} /> : null}</ul>
}

function rejecting(): Outbox {
  return new Outbox({
    store: memoryStore(),
    persistent: () => Promise.resolve(true),
    locks: null,
    send: (body) =>
      Promise.resolve<SyncResponse>({
        results: body.items.map((item) => ({
          client_id: item.client_id,
          status: 'rejected',
          reason: 'episode no longer exists',
        })),
      }),
  })
}

afterEach(() => {
  setOutbox(null)
})

describe('offline progress in the failure banner (FR-S8, FR-W6)', () => {
  it('shows a rejected record with its reason and deletes it on dismiss', async () => {
    const box = rejecting()
    setOutbox(box)
    box.setOwner(1)
    await box.recordCompletion(1, 77)
    await box.flush()

    render(<Rows />)

    expect(await screen.findByText(/episode no longer exists/)).toBeInTheDocument()
    expect(screen.getByText(/Watched to the end/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /Dismiss Offline progress/ }))

    await act(async () => {
      await box.refresh()
    })
    expect(screen.queryByText(/episode no longer exists/)).not.toBeInTheDocument()
    expect(await box.all()).toEqual([])
  })

  it('counts another account’s records without offering to delete them', async () => {
    const box = rejecting()
    setOutbox(box)
    box.setOwner(1)
    await box.recordPosition(1, 77, 30, 1420)
    box.setOwner(2)
    await box.refresh()

    render(<Rows />)

    expect(await screen.findByText(/1 record from another account/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('warns that a memory-only queue will not survive a reload', async () => {
    const box = new Outbox({
      store: memoryStore(),
      persistent: () => Promise.resolve(false),
      locks: null,
    })
    setOutbox(box)
    box.setOwner(1)
    await box.recordPosition(1, 77, 30, 1420)

    render(<Rows />)

    expect(await screen.findByText(/lost if this page reloads/)).toBeInTheDocument()
  })
})

describe('records that keep failing (S2)', () => {
  afterEach(() => {
    setOutbox(null)
  })

  it('are counted with the last error, and nothing is deleted', async () => {
    let at = Date.UTC(2026, 9, 5, 12)
    const box = new Outbox({
      store: memoryStore(),
      persistent: () => Promise.resolve(true),
      locks: null,
      now: () => new Date(at),
      send: () => Promise.reject(new TypeError('Failed to fetch')),
    })
    setOutbox(box)
    box.setOwner(1)
    await box.recordCompletion(1, 77)
    for (let attempt = 0; attempt < STUCK_ATTEMPTS; attempt += 1) await box.flush()
    at += STUCK_AFTER_MS

    render(<Rows />)
    await act(async () => {
      await box.refresh()
    })

    expect(screen.getByText('1 offline record not yet synced')).toBeInTheDocument()
    expect(screen.getByText(/Failed to fetch/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(await box.all()).toHaveLength(1)
  })
})
