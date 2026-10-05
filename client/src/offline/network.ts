/**
 * Whether Arc can currently reach its server (M18, in-app offline).
 *
 * `navigator.onLine` alone is not enough: an iPad on hotel wifi that has
 * stopped forwarding is "online" and answers nothing. So the state is
 * *observed* — a request that failed for want of a network sets it
 * (`reportOffline`, from `apiFetch`), the next request that gets any response
 * at all clears it — with the browser's `online`/`offline` events as a hint
 * rather than the truth.
 *
 * Generic on purpose: the progress outbox flushes when this flips back to
 * online, and the later download and offline-page work reads the same flag.
 * A store outside React, read with {@link useOffline} via
 * `useSyncExternalStore`, because the shell, the player and the pages all ask
 * the same question.
 */

import { useSyncExternalStore } from 'react'

let offline = typeof navigator === 'undefined' ? false : !navigator.onLine
const listeners = new Set<() => void>()

function emit(next: boolean): void {
  if (offline === next) return
  offline = next
  for (const listener of listeners) listener()
}

/** A request failed with no response at all. */
export function reportOffline(): void {
  emit(true)
}

/** A request got a response: whatever the browser thinks, we are connected. */
export function reportOnline(): void {
  emit(false)
}

/** The current answer. Named apart from `isOffline(error)` in `@/lib/api`. */
export function offlineNow(): boolean {
  return offline
}

/** Called on every change of {@link offlineNow}. Returns an unsubscribe. */
export function subscribeNetwork(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** Wire the browser's own events. Returns a function that unwires them. */
export function watchNetwork(): () => void {
  const goOffline = () => {
    emit(true)
  }
  const goOnline = () => {
    emit(false)
  }
  window.addEventListener('offline', goOffline)
  window.addEventListener('online', goOnline)
  return () => {
    window.removeEventListener('offline', goOffline)
    window.removeEventListener('online', goOnline)
  }
}

export function useOffline(): boolean {
  return useSyncExternalStore(subscribeNetwork, offlineNow, () => false)
}

/** Reset. Tests only. */
export function resetNetwork(): void {
  offline = false
  listeners.clear()
}
