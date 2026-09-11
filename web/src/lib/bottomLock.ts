// The open transcript's bottom-lock, published so plain helpers can reach it.
//
// A helper like `scrollToUserMessage` (turn-rail tick, Cmd+Alt nav) is not a
// component and has no StickToBottom context, but it still has to declare that
// the reader is deliberately leaving the bottom — otherwise the switch pin,
// which re-pins every frame for ~3s, cancels the scroll it just started.

/** The mutable halves of StickToBottom's lock state that callers may clear. */
export interface BottomLockState {
  isAtBottom: boolean;
  escapedFromLock: boolean;
}

let current: BottomLockState | null = null;

/** Publish the live lock for the transcript on screen (`null` on unmount). */
export function publishBottomLock(lock: BottomLockState | null): void {
  current = lock;
}

/**
 * Declare a deliberate move away from the bottom.
 *
 * `escapedFromLock` is what every bottom-holding effect treats as "the reader
 * wants to be elsewhere", so setting it before a programmatic scroll is how a
 * nav survives the post-switch pin.
 */
export function releaseBottomLock(): void {
  if (current === null) return;
  current.isAtBottom = false;
  current.escapedFromLock = true;
}
