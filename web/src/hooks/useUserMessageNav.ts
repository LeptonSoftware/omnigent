// Cursor for stepping through past user messages.
//
// Anchor tracked by itemId (not index) so loadMoreHistory's prepend
// doesn't corrupt position. Stale anchor (e.g. tempId promoted to
// real itemId mid-nav) degrades to outside-end — next goPrev lands
// on the latest message.
//
// Call ONCE per parent and share the returned object; two callers
// would each hold their own anchor and diverge.

import { useCallback, useMemo, useState } from "react";
import { releaseBottomLock } from "@/lib/bottomLock";
import { useChatStore } from "@/store/chatStore";

export interface UserMessageNav {
  goPrev: () => void;
  goNext: () => void;
  canPrev: boolean;
  canNext: boolean;
}

// How long the scroll must stay quiet before we treat the smooth-scroll as
// finished and fire the flash.
const SCROLL_SETTLE_MS = 120;
// Absolute cap so a flash always happens even if scroll events never settle.
const SCROLL_SETTLE_MAX_MS = 1200;

let cancelPendingFlash: (() => void) | null = null;
// Bumped by every jump. The expand-and-retry path resumes two frames later, so
// it has to check it is still the newest jump before scrolling anywhere.
let navSeq = 0;

// Nearest scrollable ancestor — the element scrollIntoView actually moves and
// whose `scroll` events tell us when motion stops. Falls back to window.
function getScrollParent(node: Element): Element | null {
  let el: HTMLElement | null = node.parentElement;
  while (el) {
    const { overflowY } = getComputedStyle(el);
    if (overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay") {
      return el;
    }
    el = el.parentElement;
  }
  return null;
}

/**
 * Smooth-scroll a user message into view (centered) and flash it once the
 * scroll settles. Shared by the Cmd+Alt nav hook and the turn rail so both
 * land on the message the same way. Anchors on the `data-user-message-id`
 * DOM attribute stamped by UserBubble.
 *
 * @param itemId - The user bubble's itemId (the DOM anchor to scroll to).
 * @param flash - Optional highlight callback fired when the scroll settles.
 */
export function scrollToUserMessage(itemId: string, flash?: (id: string) => void): void {
  // Supersede the previous jump here, synchronously: deferring it to
  // scrollToElement lets a jump that takes the two-frame retry path below
  // cancel the flash of a LATER jump that landed immediately.
  cancelPendingFlash?.();
  const seq = ++navSeq;
  // The reader is deliberately leaving the bottom. Without this the switch pin
  // re-pins every frame for ~3s and cancels the smooth scroll below.
  releaseBottomLock();
  const conversationId = useChatStore.getState().conversationId;
  const find = () =>
    document.querySelector(
      // CSS.escape is defensive — itemIds are alphanumeric today.
      `[data-user-message-id="${CSS.escape(itemId)}"]`,
    );
  const el = find();
  if (el) {
    scrollToElement(el, itemId, flash);
    return;
  }
  // The message may be loaded but above the history render window (only the
  // trailing window of bubbles mounts). Mount everything and retry once the
  // expansion has committed.
  useChatStore.getState().expandHistoryRenderWindow();
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      // A newer jump, or a switch, owns the transcript now.
      if (seq !== navSeq) return;
      if (useChatStore.getState().conversationId !== conversationId) return;
      const retried = find();
      if (!retried) {
        // Fail loud: id exists in the list but DOM anchor is missing.
        console.warn(`scrollToUserMessage: no element for itemId=${itemId}`);
        return;
      }
      scrollToElement(retried, itemId, flash);
    });
  });
}

function scrollToElement(el: Element, itemId: string, flash?: (id: string) => void): void {
  el.scrollIntoView({ block: "center", behavior: "smooth" });

  // Nothing to defer when there's no flash to fire — the smooth-scroll runs
  // to completion on its own.
  if (!flash) return;

  // Defer the flash until the smooth-scroll settles. On a long jump the
  // highlight would otherwise burn out before the message is on screen.
  const scroller: EventTarget = getScrollParent(el) ?? window;
  let settleTimer = 0;
  let maxTimer = 0;
  let done = false;

  function cleanup(): void {
    window.clearTimeout(settleTimer);
    window.clearTimeout(maxTimer);
    scroller.removeEventListener("scroll", onScroll);
    if (cancelPendingFlash === cleanup) cancelPendingFlash = null;
  }

  function finish(): void {
    if (done) return;
    done = true;
    cleanup();
    flash?.(itemId);
  }

  function onScroll(): void {
    window.clearTimeout(settleTimer);
    settleTimer = window.setTimeout(finish, SCROLL_SETTLE_MS);
  }

  cancelPendingFlash = cleanup;
  scroller.addEventListener("scroll", onScroll, { passive: true });
  // First timer doubles as the "already in view, nothing scrolled" fast path;
  // each scroll event reschedules it while the smooth-scroll is in motion.
  settleTimer = window.setTimeout(finish, SCROLL_SETTLE_MS);
  maxTimer = window.setTimeout(finish, SCROLL_SETTLE_MAX_MS);
}

export function useUserMessageNav(userMessageIds: readonly string[]): UserMessageNav {
  const flashUserMessage = useChatStore((s) => s.flashUserMessage);
  const [anchorId, setAnchorId] = useState<string | null>(null);

  const currentIndex = anchorId === null ? -1 : userMessageIds.indexOf(anchorId);
  // outside = never navigated, or anchor was removed from the list.
  const outside = anchorId === null || currentIndex === -1;

  const canPrev = userMessageIds.length > 0 && (outside || currentIndex > 0);
  const canNext = !outside && currentIndex < userMessageIds.length - 1;

  const goPrev = useCallback(() => {
    if (userMessageIds.length === 0) return;
    if (!outside && currentIndex === 0) return;
    const target = outside
      ? userMessageIds[userMessageIds.length - 1]
      : userMessageIds[currentIndex - 1];
    setAnchorId(target);
    scrollToUserMessage(target, flashUserMessage);
  }, [userMessageIds, currentIndex, outside, flashUserMessage]);

  const goNext = useCallback(() => {
    if (outside) return;
    if (currentIndex >= userMessageIds.length - 1) return;
    const target = userMessageIds[currentIndex + 1];
    setAnchorId(target);
    scrollToUserMessage(target, flashUserMessage);
  }, [userMessageIds, currentIndex, outside, flashUserMessage]);

  // Stable identity so consumers can put the return value in an
  // effect dep array without re-registering on every render.
  return useMemo(() => ({ goPrev, goNext, canPrev, canNext }), [goPrev, goNext, canPrev, canNext]);
}
