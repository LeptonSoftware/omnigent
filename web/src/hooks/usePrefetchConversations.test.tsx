// Invariants:
// - One warm-up pass per page load, after a settle delay.
// - Budget derives from the transport (maxLiveConversations) minus reserved
//   slots for the active conversation and one user-driven open.
// - Oldest target binds first so registry LRU order ends recency-aligned.
// - Active and archived conversations are never prefetched.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { resetPrefetchForTest, usePrefetchRecentConversations } from "./usePrefetchConversations";

const prefetchConversation = vi.hoisted(() => vi.fn((_id: string) => Promise.resolve()));
vi.mock("@/store/chatStore", () => ({ prefetchConversation }));

const maxLiveConversations = vi.hoisted(() => vi.fn(() => 30));
vi.mock("@/store/conversationRegistry", () => ({ maxLiveConversations }));

function conv(id: string, updatedAt: number, archived = false): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: updatedAt,
    labels: {},
    permission_level: null,
    archived,
  };
}

async function flushAsync(): Promise<void> {
  // Drain the sequential prefetch loop's awaits, one microtask at a time.
  /* oxlint-disable-next-line no-await-in-loop */
  for (let i = 0; i < 20; i++) await Promise.resolve();
}

describe("usePrefetchRecentConversations", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resetPrefetchForTest();
    prefetchConversation.mockClear();
    maxLiveConversations.mockReturnValue(30);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("warms the most recent threads once, oldest-target-first, skipping active and archived", async () => {
    const conversations = [
      conv("recent", 100),
      conv("active", 99),
      conv("archived", 98, true),
      conv("older", 50),
      conv("oldest", 10),
    ];
    const { rerender } = renderHook(({ list }) => usePrefetchRecentConversations(list, "active"), {
      initialProps: { list: conversations },
    });

    // Nothing before the settle delay.
    expect(prefetchConversation).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2500);
    await flushAsync();

    // Oldest target first so the registry's LRU order ends recency-aligned.
    expect(prefetchConversation.mock.calls.map((c) => c[0])).toEqual(["oldest", "older", "recent"]);

    // One pass per page load: a list refresh must not schedule another.
    rerender({ list: [...conversations] });
    await vi.advanceTimersByTimeAsync(10_000);
    await flushAsync();
    expect(prefetchConversation).toHaveBeenCalledTimes(3);
  });

  it("respects the transport budget, reserving slots for user-driven opens", async () => {
    // Serial transport (HTTP/1.1): 3 live streams → budget of 1.
    maxLiveConversations.mockReturnValue(3);
    const conversations = [conv("a", 3), conv("b", 2), conv("c", 1)];
    renderHook(() => usePrefetchRecentConversations(conversations, null));

    await vi.advanceTimersByTimeAsync(2500);
    await flushAsync();

    expect(prefetchConversation.mock.calls.map((c) => c[0])).toEqual(["a"]);
  });

  it("keeps going when one warm-up fails", async () => {
    prefetchConversation.mockRejectedValueOnce(new Error("bind failed"));
    const conversations = [conv("a", 2), conv("b", 1)];
    renderHook(() => usePrefetchRecentConversations(conversations, null));

    await vi.advanceTimersByTimeAsync(2500);
    await flushAsync();

    expect(prefetchConversation.mock.calls.map((c) => c[0])).toEqual(["b", "a"]);
  });
});
