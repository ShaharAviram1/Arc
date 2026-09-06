import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { AnimeCard } from '@/components/AnimeCard'
import { catalogErrorMessage, isSearchable, useAnimeSearch } from '@/lib/anime'

/** Long enough that a typed word is one request, short enough to feel live. */
const DEBOUNCE_MS = 300

const inputClass =
  'w-full rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-2 text-sm text-[var(--arc-text)] placeholder:text-[var(--arc-text-muted)] focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-[var(--arc-accent)]'

const pagerClass =
  'rounded-md border border-[var(--arc-border)] bg-[var(--arc-surface)] px-3 py-1.5 text-sm text-[var(--arc-text)] transition-opacity hover:opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--arc-accent)] disabled:cursor-not-allowed disabled:opacity-40'

function Note({ children }: { children: string }) {
  return <p className="mt-8 text-sm text-[var(--arc-text-muted)]">{children}</p>
}

/**
 * Search / add (spec §4.1 FR-C1, §5). The raw input lives in the URL so a
 * refresh or a back button lands on the same search; the request itself is
 * debounced, so a typed word costs one call, not one per keystroke.
 */
export function Search() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [input, setInput] = useState(() => searchParams.get('q') ?? '')
  // Seeded from the URL so an arriving deep link searches immediately.
  const [query, setQuery] = useState(input)
  const [page, setPage] = useState(1)

  useEffect(() => {
    if (input === query) return
    const timer = setTimeout(() => {
      setQuery(input)
      setPage(1)
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [input, query])

  const { data, error, isError, isFetching, isPlaceholderData } = useAnimeSearch(query, page)

  function handleChange(value: string) {
    setInput(value)
    const next = new URLSearchParams(searchParams)
    if (value === '') next.delete('q')
    else next.set('q', value)
    setSearchParams(next, { replace: true })
  }

  return (
    <section className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold tracking-tight text-[var(--arc-text)]">Search</h1>

      <div className="mt-4 max-w-md">
        <label className="sr-only" htmlFor="search-q">
          Search anime
        </label>
        <input
          id="search-q"
          type="search"
          autoFocus
          autoComplete="off"
          // The server rejects anything longer with a 422.
          maxLength={100}
          placeholder="Title…"
          value={input}
          onChange={(event) => {
            handleChange(event.target.value)
          }}
          className={inputClass}
        />
      </div>

      {!isSearchable(query) ? (
        <Note>Search the catalogue by title</Note>
      ) : isError ? (
        <Note>{catalogErrorMessage(error, 'Search failed. Try again.')}</Note>
      ) : data === undefined ? (
        <Note>Searching…</Note>
      ) : (
        <>
          {data.results.length === 0 ? (
            <Note>{page === 1 ? `No results for “${query.trim()}”` : 'No more results'}</Note>
          ) : (
            <div
              className={`mt-6 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5 ${
                isPlaceholderData || isFetching ? 'opacity-60' : ''
              }`}
            >
              {data.results.map((anime) => (
                <AnimeCard key={anime.id} anime={anime} />
              ))}
            </div>
          )}

          {data.has_next || page > 1 ? (
            <div className="mt-6 flex items-center gap-3">
              <button
                type="button"
                className={pagerClass}
                disabled={page === 1 || isFetching}
                onClick={() => {
                  setPage((current) => Math.max(1, current - 1))
                }}
              >
                Previous
              </button>
              <span className="text-sm text-[var(--arc-text-muted)]">Page {page}</span>
              <button
                type="button"
                className={pagerClass}
                disabled={!data.has_next || isFetching}
                onClick={() => {
                  setPage((current) => current + 1)
                }}
              >
                More
              </button>
            </div>
          ) : null}
        </>
      )}
    </section>
  )
}
