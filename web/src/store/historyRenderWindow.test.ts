// The history render window is anchored at its TOP (hidden leading bubbles),
// not counted from the end. These cases are why: both of them regress the
// moment the window is expressed as "render the last N".

import { describe, expect, it } from "vitest";
import {
  HISTORY_RENDER_GROWTH_STEP,
  INITIAL_HISTORY_RENDER_COUNT,
  initialHistoryRenderCount,
  INITIAL_HISTORY_RENDER_ROUNDS,
  MAX_INITIAL_HISTORY_RENDER_COUNT,
  resolveHiddenBubbleCount,
} from "./conversationState";

/** The flat floor, which is what every case below other than the rounds tests uses. */
function hiddenAtFloor(stored: number | null, total: number): number {
  return resolveHiddenBubbleCount(stored, total, INITIAL_HISTORY_RENDER_COUNT);
}

/** What the reader currently sees, given a window and a transcript length. */
function renderedRange(stored: number | null, total: number): { top: number; shown: number } {
  const hidden = hiddenAtFloor(stored, total);
  return { top: hidden, shown: total - hidden };
}

describe("history render window", () => {
  it("derives the initial window from the transcript length", () => {
    expect(hiddenAtFloor(null, 100)).toBe(100 - INITIAL_HISTORY_RENDER_COUNT);
    // A transcript shorter than the window hides nothing.
    expect(hiddenAtFloor(null, 5)).toBe(0);
  });

  it("keeps the reader's position when a turn streams in while they read history", () => {
    // Reader scrolled up: 50 bubbles hidden, reading the bubble at index 50.
    const before = renderedRange(50, 100);
    expect(before.top).toBe(50);

    // The agent appends a message. The transcript grows at the END.
    const after = renderedRange(50, 101);

    // The top of the rendered range must NOT move: a count-from-the-end window
    // would slide it to 51 and unmount the bubble under the reader's eyes.
    expect(after.top).toBe(50);
    expect(after.shown).toBe(before.shown + 1);
  });

  it("reveals a prepended history page instead of hiding it", () => {
    // Reader is at the top of the loaded window, everything rendered.
    expect(hiddenAtFloor(0, 100)).toBe(0);

    // loadMoreHistory prepends 40 older bubbles.
    const hidden = hiddenAtFloor(0, 140);

    // Those 40 are now on screen. A count-from-the-end window would hide
    // exactly the 40 it just fetched, so the page would appear to do nothing.
    expect(hidden).toBe(0);
  });

  it("grows toward the top of the transcript and stops there", () => {
    const grow = (stored: number | null, total: number) =>
      Math.max(0, hiddenAtFloor(stored, total) - HISTORY_RENDER_GROWTH_STEP);

    let hidden = hiddenAtFloor(null, 100); // 80
    hidden = grow(hidden, 100);
    expect(hidden).toBe(80 - HISTORY_RENDER_GROWTH_STEP);
    for (let i = 0; i < 10; i++) hidden = grow(hidden, 100);
    expect(hidden).toBe(0);
  });

  it("never resolves to a window that renders nothing", () => {
    // The window counts BUBBLES; the store holds `blocks`, several of which
    // fold into one bubble. A hidden count accidentally computed in block
    // units overshoots the bubble array — and unlike a too-large trailing
    // count (which harmlessly shows everything), a too-large hidden count
    // blanks the transcript. Resolving must never hide every bubble that
    // exists.
    for (const total of [1, 5, 20, 100]) {
      for (const stored of [total, total + 1, total * 4, 5000]) {
        const hidden = hiddenAtFloor(stored, total);
        expect(hidden).toBeLessThanOrEqual(total);
        if (total > 0) expect(total - hidden).toBeGreaterThan(0);
      }
    }
  });

  it("clamps a stored count that outlives the transcript it described", () => {
    // A rebind replaces history; a stale count must never slice past the end,
    // and must always leave something on screen.
    expect(hiddenAtFloor(500, 10)).toBe(9);
    expect(hiddenAtFloor(-5, 10)).toBe(0);
    expect(hiddenAtFloor(3, 0)).toBe(0);
  });
});

describe("initialHistoryRenderCount — the first paint is sized in exchanges", () => {
  /** Bubble flags from a compact script: "u" = the reader, "a" = anything else. */
  const flags = (script: string): boolean[] => [...script].map((c) => c === "u");

  it("reaches back past the last two user messages even when they are far apart", () => {
    // One turn that yielded mid-task, so the answer spans many assistant
    // bubbles: a trailing count of 20 would show tool work and no question.
    const script = "ua" + "a".repeat(18) + "u" + "a".repeat(18);
    const count = initialHistoryRenderCount(flags(script));
    const shown = script.slice(script.length - count);
    expect([...shown].filter((c) => c === "u")).toHaveLength(INITIAL_HISTORY_RENDER_ROUNDS);
  });

  it("keeps the flat floor when exchanges are short", () => {
    // Alternating u/a: two rounds is 4 bubbles, but the floor still mounts 20.
    expect(initialHistoryRenderCount(flags("ua".repeat(30)))).toBe(INITIAL_HISTORY_RENDER_COUNT);
  });

  it("never exceeds the ceiling, so one enormous turn cannot unwind the window", () => {
    const script = "u" + "a".repeat(400) + "u" + "a".repeat(400);
    expect(initialHistoryRenderCount(flags(script))).toBe(MAX_INITIAL_HISTORY_RENDER_COUNT);
  });

  it("shows what exists when the transcript has fewer than two user messages", () => {
    expect(initialHistoryRenderCount(flags("uaa"))).toBe(INITIAL_HISTORY_RENDER_COUNT);
    expect(initialHistoryRenderCount([])).toBe(INITIAL_HISTORY_RENDER_COUNT);
  });

  it("feeds the window so the guaranteed context is actually mounted", () => {
    const script = "a".repeat(40) + "u" + "a".repeat(12) + "u" + "a".repeat(12);
    const visible = initialHistoryRenderCount(flags(script));
    const hidden = resolveHiddenBubbleCount(null, script.length, visible);
    expect([...script.slice(hidden)].filter((c) => c === "u")).toHaveLength(2);
  });

  it("lets the ceiling win when two rounds would not fit in it", () => {
    // A turn that split into 30+ bubbles: the budget matters more than the
    // guarantee here, and the reader still has jump-to-top / older history.
    const script = "u" + "a".repeat(30) + "u" + "a".repeat(30);
    const visible = initialHistoryRenderCount(flags(script));
    expect(visible).toBe(MAX_INITIAL_HISTORY_RENDER_COUNT);
    const hidden = resolveHiddenBubbleCount(null, script.length, visible);
    expect([...script.slice(hidden)].filter((c) => c === "u")).toHaveLength(1);
  });
});
