import { beforeEach, describe, expect, it } from "vitest";
import type { ConversationItem } from "./conversationItems";
import {
  clearHistoryWindowCache,
  evictCachedHistoryWindow,
  getCachedHistoryWindow,
  setCachedHistoryWindow,
} from "./historyWindowCache";

function win(id: string): { items: ConversationItem[]; hasMore: boolean } {
  return {
    items: [
      {
        id: `item_${id}`,
        response_id: `resp_${id}`,
        type: "message",
        role: "user",
        status: "completed",
        content: [{ type: "input_text", text: id }],
      } as ConversationItem,
    ],
    hasMore: false,
  };
}

beforeEach(() => {
  clearHistoryWindowCache();
});

describe("historyWindowCache", () => {
  it("stores and returns a window per session", () => {
    setCachedHistoryWindow("s1", win("a"));
    expect(getCachedHistoryWindow("s1")?.items[0]?.id).toBe("item_a");
    expect(getCachedHistoryWindow("s2")).toBeNull();
  });

  it("replaces on re-set and drops on evict/clear", () => {
    setCachedHistoryWindow("s1", win("a"));
    setCachedHistoryWindow("s1", win("b"));
    expect(getCachedHistoryWindow("s1")?.items[0]?.id).toBe("item_b");
    evictCachedHistoryWindow("s1");
    expect(getCachedHistoryWindow("s1")).toBeNull();
    setCachedHistoryWindow("s1", win("a"));
    clearHistoryWindowCache();
    expect(getCachedHistoryWindow("s1")).toBeNull();
  });

  it("evicts the least recently used session past the cap", () => {
    for (let i = 0; i < 20; i++) setCachedHistoryWindow(`s${i}`, win(`w${i}`));
    // Touch s0 so s1 becomes the oldest.
    getCachedHistoryWindow("s0");
    setCachedHistoryWindow("s20", win("w20"));
    expect(getCachedHistoryWindow("s0")).not.toBeNull();
    expect(getCachedHistoryWindow("s1")).toBeNull();
    expect(getCachedHistoryWindow("s20")).not.toBeNull();
  });
});
