/**
 * The viewer's open demands, each a link (FR-A14, M21; owner decision 2,
 * 2026-11-03).
 *
 * What the `demand_cap` refusal lists, on the show page and in the player:
 * the show's title links to its page (where the row's ✕ withdraws the
 * demand), and "Episode n" to the player, where finishing it closes it.
 */

import { Link } from 'react-router-dom'
import { cx, FOCUS_RING } from '@/components/ui'
import type { OpenDemand } from '@/lib/anime'

const LINK = cx('underline underline-offset-4 hover:text-[var(--arc-text)]', FOCUS_RING)

export function OpenDemandLinks({ open, className }: { open: OpenDemand[]; className?: string }) {
  if (open.length === 0) return null
  return (
    <ul aria-label="Your open requests" className={cx('flex flex-col gap-1', className)}>
      {open.map((row) => (
        <li key={row.episode_id}>
          <Link to={`/anime/${String(row.anime_id)}`} className={LINK}>
            {row.title}
          </Link>
          {' · '}
          <Link to={`/watch/${String(row.episode_id)}`} className={LINK}>
            {`Episode ${String(row.number)}`}
          </Link>
        </li>
      ))}
    </ul>
  )
}
