import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { cx, FOCUS_RING } from '@/components/ui/styles'
import { useAcquisitionStatus } from '@/lib/acquisition'
import { useLogout, useMe } from '@/lib/auth'
import { useIsPhone } from '@/lib/media'
import { useReviewSummary } from '@/lib/review'

/**
 * The app shell (M15).
 *
 * A top toolbar rather than the 240px sidebar it replaces: the pages below it
 * are poster-forward, and a fixed column down the left both steals width from
 * artwork and gives every screen a permanent list of places it is not. So the
 * chrome is one 68px bar — the mark, three destinations, search, and an
 * account button — and everything that used to be a nav entry but is not a
 * place you go daily (MyAnimeList, Review, Admin) lives behind the avatar.
 *
 * Schedule joined the toolbar on 2026-09-11 (owner): a seasonal grid is a
 * place people go on their own schedule rather than something they arrive at
 * from Home, and being reachable only from a shelf action made it feel like a
 * detail of Watch Now. Recommendations is still deliberately absent — it is
 * reached from Home's "Picked for you" — and its route still works.
 *
 * On phones the same set becomes a bottom tab bar with a "More" sheet, which
 * is a different set of elements rather than a restyled toolbar — hence the
 * `useIsPhone` branch instead of `hidden md:flex`. The tab bar holds four
 * tabs and no more, so Schedule lives at the top of the sheet. Nothing hides
 * on a phone: every account item in the avatar menu is in the sheet too.
 */

const BROWSE_PATH = '/search'
const REVIEW_PATH = '/review'
const LIST_PATH = '/list'
const SCHEDULE_PATH = '/schedule'

/** Long enough that a typed word is one navigation, short enough to feel live. */
const SEARCH_DEBOUNCE_MS = 300

interface NavItem {
  to: string
  label: string
  /** Only `/` needs it: every other path is matched by prefix happily. */
  end?: boolean
}

/** The toolbar's centre. Three places, in media-app language (README §1). */
const MAIN_NAV: NavItem[] = [
  { to: '/', label: 'Watch Now', end: true },
  { to: BROWSE_PATH, label: 'Browse' },
  { to: SCHEDULE_PATH, label: 'Schedule' },
]

/* --- Account menu ------------------------------------------------------ */

interface AccountEntry {
  to: string
  label: string
  /** Rendered on the right of the row: a review count, a hint. */
  badge?: ReactNode
}

interface AccountMenu {
  email: string
  /** The letter on the avatar. */
  initial: string
  entries: AccountEntry[]
  /** Admin-only, and only while it is true (M14 has the controls). */
  acquisitionPaused: boolean
  logout: () => void
  loggingOut: boolean
}

/**
 * The pending-review count beside the Review entry. Only ever rendered for a
 * positive count, so the menu is silent when there is nothing to do; the label
 * spells out what the number means, since a bare "3" tells a screen reader
 * nothing.
 */
function ReviewCountPill({ count }: { count: number }) {
  const label = count === 1 ? '1 file needs review' : `${String(count)} files need review`

  return (
    <span
      aria-label={label}
      className="inline-flex min-w-5 items-center justify-center rounded-full bg-[var(--arc-ember)] px-1.5 text-[12px] font-semibold text-[var(--arc-accent-contrast)] tabular-nums"
    >
      {count}
    </span>
  )
}

/**
 * Everything behind the avatar, gathered once in the shell rather than in the
 * menu and the sheet separately: the queries have to run whether or not the
 * menu is open (the review count is the reason the menu is worth opening), and
 * two copies of them would be two components disagreeing about the same fact.
 */
function useAccountMenu(): AccountMenu | null {
  const { data: me } = useMe()
  const logout = useLogout()

  // No count while it is loading, and none if the poll failed: an absent pill
  // reads as "nothing waiting", which is the safer thing to say when we do not
  // know (M13's review page is where a real answer lives).
  const { data: review } = useReviewSummary()
  // Likewise: an absent line reads as "acquisition is running".
  const { data: acquisition } = useAcquisitionStatus()

  if (!me) return null

  const pending = review?.pending ?? 0
  const entries: AccountEntry[] = [
    { to: LIST_PATH, label: 'My List' },
    { to: '/mal', label: 'MyAnimeList' },
    {
      to: REVIEW_PATH,
      label: 'Review',
      badge: pending > 0 ? <ReviewCountPill count={pending} /> : undefined,
    },
  ]
  if (me.role === 'admin') entries.push({ to: '/admin', label: 'Admin' })

  return {
    email: me.email,
    initial: me.email.slice(0, 1).toUpperCase(),
    entries,
    acquisitionPaused: acquisition?.paused === true,
    logout: () => {
      logout.mutate()
    },
    loggingOut: logout.isPending,
  }
}

const MENU_ITEM =
  'flex min-h-11 w-full items-center justify-between gap-3 rounded-nav px-3 text-left text-[15px] text-[var(--arc-text)] transition-colors duration-200 hover:bg-[var(--arc-surface-hover)]'

/** A hairline, not a rule: it groups, it does not divide. */
function MenuSeparator() {
  return <div aria-hidden className="my-1.5 h-[0.5px] bg-[var(--arc-hairline)]" />
}

/**
 * "Acquisition paused", above Log out, for an admin and only while it is true.
 *
 * A pause is invisible from everywhere else in the app — episodes just stop
 * moving — so the one thing this has to do is stop that being a mystery.
 * `role="status"` so a screen reader is told when it appears mid-session
 * rather than only on a reload. It used to sit under the sidebar nav; the
 * sidebar is gone, and this is where an admin now looks.
 */
function AcquisitionPausedNote() {
  return (
    <p role="status" className="px-3 py-1 text-[13px] text-[var(--arc-text-muted)]">
      Acquisition paused
    </p>
  )
}

/** The links, the email and Log out — shared by the avatar menu and the sheet. */
function AccountItems({
  account,
  onNavigate,
  itemClassName,
}: {
  account: AccountMenu
  onNavigate: () => void
  itemClassName?: string
}) {
  const itemClass = cx(MENU_ITEM, FOCUS_RING, itemClassName)

  return (
    <>
      {account.entries.map((entry) => (
        <Link
          key={entry.to}
          to={entry.to}
          data-menu-item
          onClick={onNavigate}
          className={itemClass}
        >
          <span>{entry.label}</span>
          {entry.badge}
        </Link>
      ))}

      <MenuSeparator />

      <p
        className="truncate px-3 py-1 text-[13px] text-[var(--arc-text-muted)]"
        title={account.email}
      >
        {account.email}
      </p>
      {account.acquisitionPaused ? <AcquisitionPausedNote /> : null}

      <button
        type="button"
        data-menu-item
        onClick={account.logout}
        disabled={account.loggingOut}
        className={cx(itemClass, 'disabled:opacity-60')}
      >
        {account.loggingOut ? 'Logging out…' : 'Log out'}
      </button>
    </>
  )
}

/** Focusable rows inside an open menu, in document order. */
function menuItemsIn(root: HTMLElement | null): HTMLElement[] {
  if (root === null) return []
  return Array.from(root.querySelectorAll<HTMLElement>('[data-menu-item]'))
}

/**
 * Moves focus within an open menu. Wraps, because a menu of six items that
 * stops dead at the last one makes a person guess how far up they are.
 */
function focusMenuItem(root: HTMLElement | null, index: number): void {
  const items = menuItemsIn(root)
  if (items.length === 0) return
  const wrapped = ((index % items.length) + items.length) % items.length
  items[wrapped]?.focus()
}

/**
 * Arrow keys, Home/End and Escape inside an open menu. Returns true when it
 * handled the key, so the caller can decide what Escape means for it.
 */
function moveFocus(root: HTMLElement | null, key: string): boolean {
  const items = menuItemsIn(root)
  const current = items.findIndex((item) => item === document.activeElement)

  switch (key) {
    case 'ArrowDown':
      focusMenuItem(root, current + 1)
      return true
    case 'ArrowUp':
      focusMenuItem(root, current <= 0 ? items.length - 1 : current - 1)
      return true
    case 'Home':
      focusMenuItem(root, 0)
      return true
    case 'End':
      focusMenuItem(root, items.length - 1)
      return true
    default:
      return false
  }
}

/**
 * The 36px avatar and what is behind it.
 *
 * A disclosure rather than an ARIA menu: every item but one is a link, and
 * `role="menuitem"` would take the link role away from them — a person who
 * navigates by links would lose MyAnimeList, Review and Admin entirely. So it
 * keeps `aria-expanded`/`aria-controls`, and adds the keyboard handling people
 * expect of a menu on top: arrows move, Home/End jump, Escape closes and hands
 * focus back to the avatar.
 */
function AvatarMenu({ account }: { account: AccountMenu }) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  function close(returnFocus: boolean) {
    setOpen(false)
    if (returnFocus) buttonRef.current?.focus()
  }

  // The first item, once the panel exists. Opening a menu and leaving focus on
  // the button means the first arrow press goes nowhere.
  useEffect(() => {
    if (!open) return
    focusMenuItem(panelRef.current, 0)
  }, [open])

  // A click anywhere else closes it, including on a page behind the panel.
  useEffect(() => {
    if (!open) return
    function onPointerDown(event: MouseEvent) {
      const target = event.target
      if (target instanceof Node && rootRef.current?.contains(target) === true) return
      setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-label="Account"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => {
          setOpen((current) => !current)
        }}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
          event.preventDefault()
          setOpen(true)
        }}
        className={cx(
          'flex h-9 w-9 shrink-0 items-center justify-center rounded-full border-[0.5px] border-[var(--arc-border-strong)] bg-[rgba(255,255,255,0.05)] text-[13px] text-[var(--arc-text)] transition-colors duration-200 hover:bg-[rgba(255,255,255,0.1)]',
          FOCUS_RING,
        )}
      >
        {account.initial}
      </button>

      {open ? (
        <div
          id={panelId}
          ref={panelRef}
          aria-label="Account"
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault()
              close(true)
              return
            }
            if (moveFocus(panelRef.current, event.key)) event.preventDefault()
          }}
          className="absolute top-[calc(100%+10px)] right-0 z-40 w-64 rounded-card border-[0.5px] border-[var(--arc-border)] bg-[rgba(14,18,26,0.94)] p-1.5 shadow-bar backdrop-blur-toolbar backdrop-saturate-[180%]"
        >
          <AccountItems
            account={account}
            onNavigate={() => {
              close(false)
            }}
          />
        </div>
      ) : null}
    </div>
  )
}

/* --- Toolbar ----------------------------------------------------------- */

/**
 * The toolbar's search field.
 *
 * Browse has no search box of its own in this design — this is it — so typing
 * here navigates to `/search?q=…` and the page filters. The query stays in the
 * URL, which is what already made a search shareable and survivable across a
 * refresh, and the field follows the URL rather than owning it: arriving on
 * Browse from a link fills the box, leaving Browse empties it.
 */
function ToolbarSearch({ className, autoFocus }: { className?: string; autoFocus?: boolean }) {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const onBrowse = location.pathname === BROWSE_PATH
  const urlQuery = onBrowse ? (searchParams.get('q') ?? '') : ''
  const [value, setValue] = useState(urlQuery)

  // Adjusted during render rather than in an effect: the field mirrors the
  // URL, and a mirror that updates one paint late shows the previous search
  // for a frame. React re-renders this component before touching the DOM.
  const [seenQuery, setSeenQuery] = useState(urlQuery)
  if (urlQuery !== seenQuery) {
    setSeenQuery(urlQuery)
    setValue(urlQuery)
  }

  const go = useCallback(
    (next: string, replace: boolean) => {
      void navigate(next === '' ? BROWSE_PATH : `${BROWSE_PATH}?q=${encodeURIComponent(next)}`, {
        replace,
      })
    },
    [navigate],
  )

  useEffect(() => {
    if (value === urlQuery) return
    const timer = setTimeout(() => {
      // Replace only while already on Browse: refining a search should not
      // fill the back button with every prefix of the word.
      go(value, onBrowse)
    }, SEARCH_DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [value, urlQuery, onBrowse, go])

  return (
    <form
      role="search"
      className={cx('min-w-0', className)}
      onSubmit={(event) => {
        event.preventDefault()
        go(value, onBrowse)
      }}
    >
      <input
        type="search"
        aria-label="Search"
        placeholder="Search"
        autoComplete="off"
        autoFocus={autoFocus}
        // The catalogue endpoint rejects anything longer with a 422.
        maxLength={100}
        value={value}
        onChange={(event) => {
          setValue(event.target.value)
        }}
        className={cx(
          'h-11 w-full rounded-full border-[0.5px] border-[rgba(255,255,255,0.14)] bg-[var(--arc-surface-input)] px-[18px] text-[14px] text-[var(--arc-text)] placeholder:text-[var(--arc-text-muted)]',
          'focus-visible:outline-2 focus-visible:-outline-offset-1 focus-visible:outline-[var(--arc-focus)]',
        )}
      />
    </form>
  )
}

function navPillClass({ isActive }: { isActive: boolean }): string {
  return cx(
    'flex h-11 items-center rounded-nav px-4 text-[15px] transition-colors duration-200',
    isActive
      ? 'bg-[var(--arc-nav-active)] font-semibold text-[var(--arc-text)]'
      : 'text-[rgba(235,239,245,0.78)] hover:text-[var(--arc-text)]',
    FOCUS_RING,
  )
}

function Toolbar({ account, isPhone }: { account: AccountMenu | null; isPhone: boolean }) {
  // Phone: the field would leave no room for the mark, so it starts as a
  // 44px control and expands over the bar when it is asked for.
  const [searchOpen, setSearchOpen] = useState(false)

  return (
    <header className="sticky top-0 z-30 border-b-[0.5px] border-[var(--arc-hairline)] bg-[rgba(8,11,17,0.8)] backdrop-blur-toolbar backdrop-saturate-[180%]">
      <div className="flex h-[68px] items-center gap-[22px] px-4 md:px-10">
        {isPhone && searchOpen ? (
          <>
            <ToolbarSearch className="flex-1" autoFocus />
            <button
              type="button"
              onClick={() => {
                setSearchOpen(false)
              }}
              className={cx(
                'h-11 shrink-0 px-1 text-[15px] text-[var(--arc-text-muted)]',
                FOCUS_RING,
              )}
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            {/*
             * The mark is a way home, on both chromes (owner, 2026-09-11):
             * every media app in the world makes its logo the way back to the
             * front page, and Arc's was decoration. The link carries the
             * accessible name and the image is decorative, so a screen reader
             * hears "Arc — Watch Now" once rather than twice.
             */}
            <Link
              to="/"
              aria-label="Arc — Watch Now"
              className={cx('shrink-0 rounded-nav', FOCUS_RING)}
            >
              <img src="/arc-logo.png" alt="" className="h-12 w-auto" />
            </Link>

            {isPhone ? null : (
              <nav aria-label="Main" className="mx-auto flex items-center gap-0.5">
                {MAIN_NAV.map((item) => (
                  <NavLink key={item.to} to={item.to} end={item.end} className={navPillClass}>
                    {item.label}
                  </NavLink>
                ))}
              </nav>
            )}

            <div className={cx('flex items-center gap-3', isPhone && 'ml-auto')}>
              {isPhone ? (
                <button
                  type="button"
                  aria-label="Search"
                  onClick={() => {
                    setSearchOpen(true)
                  }}
                  className={cx(
                    'flex h-11 w-11 items-center justify-center rounded-full border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface-input)] text-[var(--arc-text-muted)]',
                    FOCUS_RING,
                  )}
                >
                  <span aria-hidden className="text-[16px]">
                    ⌕
                  </span>
                </button>
              ) : (
                <ToolbarSearch className="w-[240px]" />
              )}
              {account === null ? null : <AvatarMenu account={account} />}
            </div>
          </>
        )}
      </div>
    </header>
  )
}

/* --- Phone chrome ------------------------------------------------------ */

const TAB_NAV: NavItem[] = [
  { to: '/', label: 'Watch Now', end: true },
  { to: BROWSE_PATH, label: 'Browse' },
  { to: LIST_PATH, label: 'My List' },
]

function tabClass(active: boolean): string {
  return cx(
    'flex min-h-11 flex-1 flex-col items-center justify-center gap-1 rounded-nav px-1 py-1.5 text-[11px]',
    active ? 'text-[var(--arc-text)]' : 'text-[var(--arc-text-muted)]',
    FOCUS_RING,
  )
}

/** The dot under the active tab. There is no icon set, so this is the marker. */
function TabDot({ active }: { active: boolean }) {
  return (
    <span
      aria-hidden
      className={cx(
        'h-[5px] w-[5px] rounded-full',
        active ? 'bg-[var(--arc-text)]' : 'bg-transparent',
      )}
    />
  )
}

function TabBar({ moreOpen, onMore }: { moreOpen: boolean; onMore: () => void }) {
  return (
    <nav
      aria-label="Sections"
      className="fixed inset-x-0 bottom-0 z-30 border-t-[0.5px] border-[var(--arc-hairline)] bg-[rgba(8,11,17,0.92)] pb-[env(safe-area-inset-bottom)] backdrop-blur-toolbar backdrop-saturate-[180%]"
    >
      <div className="flex items-stretch gap-0.5 px-2 py-2">
        {TAB_NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => tabClass(isActive && !moreOpen)}
          >
            {({ isActive }) => (
              <>
                <TabDot active={isActive && !moreOpen} />
                <span>{item.label}</span>
              </>
            )}
          </NavLink>
        ))}
        <button
          type="button"
          aria-expanded={moreOpen}
          onClick={onMore}
          className={tabClass(moreOpen)}
        >
          <TabDot active={moreOpen} />
          <span>More</span>
        </button>
      </div>
    </nav>
  )
}

/**
 * The "More" sheet: the avatar menu, as a sheet, with Schedule above it.
 *
 * Same account items in the same order, because "one more tap" is the phone
 * concession this design makes and "somewhere else entirely" is not. Schedule
 * is a destination rather than an account item, so it sits first and above the
 * hairline: the tab bar is four tabs and a fifth would crowd them, and this is
 * the phone's version of the toolbar entry (owner, 2026-09-11).
 */
function MoreSheet({ account, onClose }: { account: AccountMenu; onClose: () => void }) {
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    focusMenuItem(panelRef.current, 0)
  }, [])

  return (
    <div
      className="fixed inset-0 z-40 flex items-end bg-[var(--arc-scrim-flat)]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        aria-label="More"
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.preventDefault()
            onClose()
            return
          }
          if (moveFocus(panelRef.current, event.key)) event.preventDefault()
        }}
        className="w-full rounded-t-hero border-t-[0.5px] border-[var(--arc-border-strong)] bg-[rgba(14,18,26,0.98)] px-3 pt-2.5 pb-[calc(22px+env(safe-area-inset-bottom))]"
      >
        <div
          aria-hidden
          className="mx-auto mb-3 h-1 w-9 rounded-full bg-[rgba(255,255,255,0.22)]"
        />
        <Link
          to={SCHEDULE_PATH}
          data-menu-item
          onClick={onClose}
          className={cx(MENU_ITEM, FOCUS_RING, 'min-h-12')}
        >
          <span>Schedule</span>
        </Link>
        <MenuSeparator />
        <AccountItems account={account} onNavigate={onClose} itemClassName="min-h-12" />
        <button
          type="button"
          onClick={onClose}
          className={cx(
            'mt-3 h-12 w-full rounded-nav border-[0.5px] border-[var(--arc-border)] bg-[var(--arc-surface-raised)] text-[15px] text-[var(--arc-text-muted)]',
            FOCUS_RING,
          )}
        >
          Close
        </button>
      </div>
    </div>
  )
}

/* --- Shell ------------------------------------------------------------- */

export function Layout() {
  const isPhone = useIsPhone()
  const account = useAccountMenu()
  const location = useLocation()
  const [moreOpen, setMoreOpen] = useState(false)

  // A sheet that survives the page it was opened from is a trapdoor, and one
  // that survives a resize to desktop widths is a sheet with no way to close
  // it. Adjusted during render rather than in an effect so the sheet is never
  // painted over the page it no longer belongs to.
  const chrome = `${location.pathname}|${String(isPhone)}`
  const [seenChrome, setSeenChrome] = useState(chrome)
  if (chrome !== seenChrome) {
    setSeenChrome(chrome)
    setMoreOpen(false)
  }

  return (
    <div className="flex min-h-full flex-col">
      <Toolbar account={account} isPhone={isPhone} />

      <main
        className={cx(
          'flex-1 px-4 pt-6 md:px-10 md:pt-11',
          isPhone ? 'pb-[calc(88px+env(safe-area-inset-bottom))]' : 'pb-24',
        )}
      >
        {/*
         * Keyed on the path so every route change replays `rise` — the 360ms
         * settle that makes a navigation read as a new screen rather than a
         * repaint. The reduced-motion rule in index.css turns it off.
         */}
        <div key={location.pathname} className="mx-auto w-full max-w-[1180px] animate-rise">
          <Outlet />
        </div>
      </main>

      {isPhone ? (
        <TabBar
          moreOpen={moreOpen}
          onMore={() => {
            setMoreOpen(true)
          }}
        />
      ) : null}

      {isPhone && moreOpen && account !== null ? (
        <MoreSheet
          account={account}
          onClose={() => {
            setMoreOpen(false)
          }}
        />
      ) : null}
    </div>
  )
}
