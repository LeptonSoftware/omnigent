// In-memory cache of the last hydrated history window per session, so
// switching back to a recently viewed session renders instantly from
// memory while `bindStream` catches up in the background with a delta
// fetch (only items newer than the cached tail). Module-level (not
// react-query) because the store consumes it synchronously inside
// `switchTo`'s state reset — before any fetch is issued.
//
// Deliberately NOT persisted to localStorage/IndexedDB: a single long
// agentic session can exceed storage quotas, and a stale-on-disk window
// would still need the same delta fetch to be trustworthy.

import type { ConversationItem } from "./conversationItems";

export interface CachedHistoryWindow {
  /** Items oldest-to-newest, same shape `fetchInitialHistoryWindow` returns. */
  items: ConversationItem[];
  /** True when older items exist before the first cached item. */
  hasMore: boolean;
}

/**
 * Bound on cached sessions. Iteration order of `Map` is insertion order,
 * so re-inserting on read makes the first key the least recently used.
 */
const MAX_CACHED_SESSIONS = 20;

const cache = new Map<string, CachedHistoryWindow>();

/** The cached window for a session, or null. Refreshes its LRU position. */
export function getCachedHistoryWindow(sessionId: string): CachedHistoryWindow | null {
  const entry = cache.get(sessionId);
  if (!entry) return null;
  cache.delete(sessionId);
  cache.set(sessionId, entry);
  return entry;
}

/** Store (or replace) a session's window, evicting the least recently used. */
export function setCachedHistoryWindow(sessionId: string, window: CachedHistoryWindow): void {
  cache.delete(sessionId);
  cache.set(sessionId, window);
  while (cache.size > MAX_CACHED_SESSIONS) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

/** Drop one session's cached window (e.g. the session was deleted). */
export function evictCachedHistoryWindow(sessionId: string): void {
  cache.delete(sessionId);
}

/** Drop everything — used on logout and between tests. */
export function clearHistoryWindowCache(): void {
  cache.clear();
}
