/**
 * The viewer's open demands, each a link with a ✕ (FR-A14, M21; owner
 * decision 2, 2026-11-03).
 *
 * What the `demand_cap` refusal lists, on the show page and in the player:
 * the show's title links to its page, "Episode n" to the player, where
 * finishing it closes it, and the ✕ withdraws it on the spot (`DELETE
 * /api/episodes/{id}/demand`) so the viewer can free a place without leaving.
 * A withdrawn row leaves the list at once; the shows and Home are refetched,
 * and `onWithdrawn` lets the player refetch its own episode too.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { cx, FOCUS_RING } from '@/components/ui'
import type { OpenDemand } from '@/lib/anime'
import { useWithdrawDemand, WITHDRAW_FAILED } from '@/lib/playback'

const LINK = cx('underline underline-offset-4 hover:text-[var(--arc-text)]', FOCUS_RING)

export function OpenDemandLinks({
  open,
  className,
  onWithdrawn,
}: {
  open: OpenDemand[]
  className?: string
  /** Called after a withdraw the server accepted, with its episode id. */
  onWithdrawn?: (episodeId: number) => void
}) {
  const withdraw = useWithdrawDemand()
  const [gone, setGone] = useState<ReadonlySet<number>>(new Set())
  const rows = open.filter((row) => !gone.has(row.episode_id))
  if (rows.length === 0 && !withdraw.isError) return null
  return (
    <div className={className}>
      {rows.length === 0 ? null : (
        <ul aria-label="Your open requests" className="flex flex-col gap-1">
          {rows.map((row) => (
            <li key={row.episode_id}>
              <Link to={`/anime/${String(row.anime_id)}`} className={LINK}>
                {row.title}
              </Link>
              {' · '}
              <Link to={`/watch/${String(row.episode_id)}`} className={LINK}>
                {`Episode ${String(row.number)}`}
              </Link>
              <button
                type="button"
                disabled={withdraw.isPending}
                aria-label={`Withdraw the request for ${row.title} episode ${String(row.number)}`}
                title="Withdraw the request"
                onClick={() => {
                  withdraw.mutate(
                    { episodeId: row.episode_id },
                    {
                      onSuccess: () => {
                        setGone((before) => new Set([...before, row.episode_id]))
                        onWithdrawn?.(row.episode_id)
                      },
                    },
                  )
                }}
                className={cx('ml-2 leading-none hover:text-[var(--arc-text)]', FOCUS_RING)}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
      {withdraw.isError ? (
        <p role="alert" className="mt-1 text-[var(--arc-error)]">
          {WITHDRAW_FAILED}
        </p>
      ) : null}
    </div>
  )
}
