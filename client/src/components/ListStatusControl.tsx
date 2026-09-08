import {
  isListStatus,
  LIST_STATUSES,
  LIST_STATUS_LABELS,
  listErrorMessage,
  useRemoveListEntry,
  useSetListEntry,
  type ListStatus,
} from '@/lib/anime'

interface ListStatusControlProps {
  animeId: number
  /** The viewer's current status, or null when the show is not on their list. */
  status: ListStatus | null
  /** Accessible name; distinguishes the many controls on a results grid. */
  label?: string
  className?: string
}

const OFF_LIST = ''

/**
 * The one control that puts a show on the viewer's list, moves it between
 * statuses, or takes it off (spec §4.6 FR-W2). Setting a status is a PUT;
 * "Not on list" is a DELETE, and only when there is an entry to delete.
 */
export function ListStatusControl({
  animeId,
  status,
  label = 'List status',
  className = '',
}: ListStatusControlProps) {
  const setEntry = useSetListEntry()
  const removeEntry = useRemoveListEntry()

  const isPending = setEntry.isPending || removeEntry.isPending
  const error = setEntry.error ?? removeEntry.error

  function handleChange(value: string) {
    if (value === OFF_LIST) {
      // Nothing to delete when the show was never on the list.
      if (status !== null) removeEntry.mutate(animeId)
      return
    }
    if (isListStatus(value) && value !== status) setEntry.mutate({ animeId, status: value })
  }

  return (
    <div className={className}>
      <select
        aria-label={label}
        value={status ?? OFF_LIST}
        disabled={isPending}
        onChange={(event) => {
          handleChange(event.target.value)
        }}
        className="w-full rounded-md border border-[var(--arc-border)] bg-[var(--arc-bg)] px-2 py-1.5 text-sm text-[var(--arc-text)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)] disabled:opacity-60"
      >
        <option value={OFF_LIST}>Not on list</option>
        {LIST_STATUSES.map((value) => (
          <option key={value} value={value}>
            {LIST_STATUS_LABELS[value]}
          </option>
        ))}
      </select>
      {error ? (
        <p role="alert" className="mt-1 text-xs text-[var(--arc-error)]">
          {listErrorMessage(error)}
        </p>
      ) : null}
    </div>
  )
}
