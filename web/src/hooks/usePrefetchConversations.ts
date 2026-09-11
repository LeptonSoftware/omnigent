// Background warm-up of the most recent conversations.
//
// A warm switch (registry entry live) paints in ~0.5s; a cold one pays the
// bind + hydration (~1-1.7s). This hook binds the top few recent sessions in
// the background shortly after the sidebar list loads, so the first click on
// any of them is already the warm path. Prefetched entries are ordinary
// background entries: evictable under stream-slot pressure, kept current by
// their SSE streams, and never marked seen (read-state writes are tied to
// viewing, not to binding).
//
// Only the viewer's OWN sessions are warmed. Binding a stream registers the
// viewer in the session's presence registry, which broadcasts the viewer list
// to every co-viewer — so warming a shared session would tell a collaborator
// someone is reading a thread they never opened, and make presence useless as
// a signal. Own sessions have no co-viewer to mislead.

import { useEffect, useRef } from "react";
import { prefetchConversation } from "@/store/chatStore";
import { maxLiveConversations } from "@/store/conversationRegistry";
import { isOwnedByViewer } from "@/lib/conversationOwnership";
import { getCurrentUserId, resolveIdentity } from "@/lib/identity";
import type { Conversation } from "@/hooks/useConversations";

/**
 * Hard cap on prefetched conversations, below the transport budget. The real
 * budget also reserves slots for the active conversation and one manual open,
 * so on HTTP/1.1 (3 live streams) this warms just one thread and on h2 (30)
 * it warms eight.
 */
const PREFETCH_MAX = 8;

/** Slots kept free for the active conversation and one user-driven open. */
const PREFETCH_RESERVED_SLOTS = 2;

/**
 * Wait after the list loads before warming, so the active conversation's own
 * bind and first paint never compete with prefetch traffic.
 */
const PREFETCH_DELAY_MS = 2500;

/** One prefetch pass per page load. */
let prefetchScheduled = false;

/** Test-only seam: allow a fresh page-load's single prefetch pass again. */
export function resetPrefetchForTest(): void {
  prefetchScheduled = false;
}

/**
 * Schedule one background warm-up pass over the most recent conversations.
 *
 * Mounted once in AppShell. Reads the freshest list/active id at fire time
 * (not at schedule time) via a ref, and binds oldest-target-first so the
 * registry's LRU order ends aligned with recency.
 */
export function usePrefetchRecentConversations(
  conversations: readonly Conversation[] | undefined,
  activeConversationId: string | null | undefined,
): void {
  const latestRef = useRef({ conversations, activeConversationId });
  latestRef.current = { conversations, activeConversationId };

  useEffect(() => {
    if (prefetchScheduled) return;
    if (conversations === undefined || conversations.length === 0) return;
    prefetchScheduled = true;
    // Deliberately not cleared on re-render/unmount: the pass runs once per
    // page load, each bind no-ops if its conversation went live meanwhile.
    setTimeout(() => {
      const latest = latestRef.current;
      const budget = Math.min(PREFETCH_MAX, maxLiveConversations() - PREFETCH_RESERVED_SLOTS);
      const available = latest.conversations;
      if (budget <= 0 || available === undefined) return;
      void (async () => {
        // Identity is normally resolved well before this fires; await it so a
        // slow resolve can't read every shared session as unowned. A rejected
        // resolve must not cancel the pass — fall back to whatever is known.
        try {
          await resolveIdentity();
        } catch {
          // Best-effort: `getCurrentUserId` may still hold a cached id.
        }
        const viewerId = getCurrentUserId();
        const targets = [...available]
          .filter(
            (c) =>
              c.archived !== true &&
              c.id !== latest.activeConversationId &&
              isOwnedByViewer(c, viewerId),
          )
          .sort((a, b) => b.updated_at - a.updated_at)
          .slice(0, budget)
          // Oldest first, so the most recent thread ends most-recently-used.
          .reverse();
        // Sequential on purpose: one bind's fetches at a time keeps the
        // warm-up invisible next to user-driven traffic.
        /* oxlint-disable no-await-in-loop */
        for (const target of targets) {
          try {
            await prefetchConversation(target.id);
          } catch {
            // Best-effort: a failed warm-up leaves that conversation cold;
            // clicking it takes the normal bind path.
          }
        }
      })();
    }, PREFETCH_DELAY_MS);
  }, [conversations]);
}
