// The history render window is anchored at its TOP (hidden leading bubbles),
// not counted from the end. These cases are why: both of them regress the
// moment the window is expressed as "render the last N".

import { describe, expect, it } from "vitest";
import {
  HISTORY_RENDER_GROWTH_STEP,
  INITIAL_HISTORY_RENDER_COUNT,
  resolveHiddenBubbleCount,
} from "./conversationState";

/** What the reader currently sees, given a window and a transcript length. */
function renderedRange(stored: number | null, total: number): { top: number; shown: number } {
  const hidden = resolveHiddenBubbleCount(stored, total);
  return { top: hidden, shown: total - hidden };
}

describe("history render window", () => {
  it("derives the initial window from the transcript length", () => {
    expect(resolveHiddenBubbleCount(null, 100)).toBe(100 - INITIAL_HISTORY_RENDER_COUNT);
    // A transcript shorter than the window hides nothing.
    expect(resolveHiddenBubbleCount(null, 5)).toBe(0);
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
    expect(resolveHiddenBubbleCount(0, 100)).toBe(0);

    // loadMoreHistory prepends 40 older bubbles.
    const hidden = resolveHiddenBubbleCount(0, 140);

    // Those 40 are now on screen. A count-from-the-end window would hide
    // exactly the 40 it just fetched, so the page would appear to do nothing.
    expect(hidden).toBe(0);
  });

  it("grows toward the top of the transcript and stops there", () => {
    const grow = (stored: number | null, total: number) =>
      Math.max(0, resolveHiddenBubbleCount(stored, total) - HISTORY_RENDER_GROWTH_STEP);

    let hidden = resolveHiddenBubbleCount(null, 100); // 80
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
        const hidden = resolveHiddenBubbleCount(stored, total);
        expect(hidden).toBeLessThanOrEqual(total);
        if (total > 0) expect(total - hidden).toBeGreaterThan(0);
      }
    }
  });

  it("clamps a stored count that outlives the transcript it described", () => {
    // A rebind replaces history; a stale count must never slice past the end,
    // and must always leave something on screen.
    expect(resolveHiddenBubbleCount(500, 10)).toBe(9);
    expect(resolveHiddenBubbleCount(-5, 10)).toBe(0);
    expect(resolveHiddenBubbleCount(3, 0)).toBe(0);
  });
});
