// Runtime companion to the `ConversationState` type: its key set and its
// initial values.
//
// Both are derived from the type via exhaustive records, so adding a field to
// `ConversationState` fails the build here rather than silently skipping the
// split or the reset. The key set is what `createConversationStore`'s setter
// uses to route a patch — a key missing from it would write conversation state
// onto the app-global store instead, where it would look correct until a second
// conversation existed.

import type { ConversationState } from "./chatStore";

/**
 * How many trailing bubbles a conversation renders when it first paints. The
 * rest of the loaded history stays in the store and is revealed by
 * `growHistoryRenderWindow` as the reader scrolls up — rendering all ~100
 * markdown bubbles on every switch is what made switches slow, not fetching.
 */
export const INITIAL_HISTORY_RENDER_COUNT = 20;

/** How many more bubbles each `growHistoryRenderWindow` call reveals. */
export const HISTORY_RENDER_GROWTH_STEP = 30;

/**
 * Exchanges the first paint always includes, when the transcript has them.
 *
 * A flat bubble count is the wrong unit for "enough context to know where you
 * are": one agent turn is usually one bubble, but a turn that yields mid-task
 * (dispatching sub-agents) continues in further assistant bubbles, and routing
 * and compaction rows take slots too — so a trailing count alone can open a
 * conversation on nothing but the tail of one long turn, with the reader's own
 * last question above the fold. Counting back to the Nth-most-recent user
 * message guarantees the question AND the answer are on screen.
 */
export const INITIAL_HISTORY_RENDER_ROUNDS = 2;

/**
 * Ceiling on the first paint, even when the rounds guarantee asks for more.
 * One enormous turn would otherwise unwind the whole point of the window;
 * beyond this the reader scrolls, which reveals the rest.
 */
export const MAX_INITIAL_HISTORY_RENDER_COUNT = 60;

/**
 * How many trailing bubbles the first paint should mount.
 *
 * `INITIAL_HISTORY_RENDER_COUNT` is the floor, the last
 * `INITIAL_HISTORY_RENDER_ROUNDS` user messages (and everything after them) the
 * guarantee, and `MAX_INITIAL_HISTORY_RENDER_COUNT` the ceiling.
 *
 * @param isUserBubble - One flag per bubble, transcript order, marking the
 *   reader's own messages. Taken as flags rather than bubbles so the store
 *   layer never has to know the bubble union.
 */
export function initialHistoryRenderCount(isUserBubble: readonly boolean[]): number {
  let rounds = 0;
  let needed = 0;
  for (let i = isUserBubble.length - 1; i >= 0; i -= 1) {
    if (isUserBubble[i] === true) {
      rounds += 1;
      // Include this user message itself, and stop once we have enough.
      needed = isUserBubble.length - i;
      if (rounds >= INITIAL_HISTORY_RENDER_ROUNDS) break;
    }
  }
  return Math.min(Math.max(needed, INITIAL_HISTORY_RENDER_COUNT), MAX_INITIAL_HISTORY_RENDER_COUNT);
}

/**
 * Resolve how many leading bubbles stay unmounted, given what the conversation
 * has stored and how many bubbles exist right now.
 *
 * The window is anchored at its TOP (a count of hidden leading bubbles), not
 * at the end. Anchoring it to the end — "render the last N" — sounds
 * equivalent and is not: every event that changes the length then moves the
 * top. A turn streaming in while the reader is scrolled up would unmount the
 * bubble they are reading (length grows, so the Nth-from-last slides down the
 * list), and a prepended history page would be hidden the moment it arrived
 * (length grows at the front, so the same trailing N stays on screen).
 * Hidden-from-top makes both correct by construction: an append leaves the
 * reader's position untouched, and a prepend reveals exactly what it fetched.
 *
 * `null` means "not chosen yet" — the initial window is derived here rather
 * than at store-init, because a conversation's blocks land after its state
 * does. Reset to `null` whenever the history window itself resets (rebind,
 * reconnect re-hydrate), so the new window re-derives instead of inheriting a
 * count that described the old one.
 */
export function resolveHiddenBubbleCount(
  stored: number | null,
  total: number,
  initialVisible: number,
): number {
  if (stored === null) return Math.max(0, total - initialVisible);
  // Never hide every bubble. The window counts bubbles while the store counts
  // blocks — several of which fold into one bubble — so a count that leaks
  // across that boundary overshoots. Clamping to "at least one rendered"
  // makes the worst case a short transcript the reader can scroll, never a
  // blank one that looks like the conversation failed to load.
  return Math.min(Math.max(0, stored), Math.max(0, total - 1));
}

// Exhaustive by construction: `Record<keyof ConversationState, true>` rejects a
// missing key and a stale one.
const CONVERSATION_STATE_KEY_MAP: Record<keyof ConversationState, true> = {
  blocks: true,
  pendingUserMessages: true,
  activeResponse: true,
  interruptedResponseIds: true,
  status: true,
  sessionStatus: true,
  backgroundTaskCount: true,
  blockedOn: true,
  isNativeTerminalSession: true,
  nativeVendorOwnsModel: true,
  boundAgentId: true,
  boundAgentName: true,
  loadingConversation: true,
  conversationLoadError: true,
  sessionModelOverride: true,
  sessionReasoningEffort: true,
  costControlModeOverride: true,
  subagentRoutingOverride: true,
  codexPlanMode: true,
  claudePermissionMode: true,
  hasMoreHistory: true,
  loadingMoreHistory: true,
  historyHiddenCount: true,
  oldestItemId: true,
  llmModel: true,
  pendingModelChange: true,
  sessionHarness: true,
  subAgentName: true,
  contextWindow: true,
  tokensUsed: true,
  sessionCostUsd: true,
  sessionUsageByModel: true,
  gitBranch: true,
  todos: true,
  skills: true,
  codexModelOptions: true,
  terminalPending: true,
  viewers: true,
  sandboxStatus: true,
  mcpStartup: true,
  abortController: true,
  runnerLaunchedAt: true,
  failedSendDraft: true,
  sendLatchedAt: true,
  historyGeneration: true,
};

const CONVERSATION_STATE_KEYS = new Set<string>(Object.keys(CONVERSATION_STATE_KEY_MAP));

/** Whether `key` names conversation-scoped state (vs. app-global or an action). */
export function isConversationStateKey(key: string | symbol): key is keyof ConversationState {
  return typeof key === "string" && CONVERSATION_STATE_KEYS.has(key);
}

/**
 * A conversation's state before anything is bound — what a cold load starts
 * from, and what `switchTo` used to reset the single active slot to.
 */
export function createInitialConversationState(): ConversationState {
  return {
    blocks: [],
    pendingUserMessages: [],
    activeResponse: null,
    interruptedResponseIds: [],
    status: "idle",
    sessionStatus: "idle",
    backgroundTaskCount: 0,
    blockedOn: null,
    isNativeTerminalSession: false,
    nativeVendorOwnsModel: false,
    boundAgentId: null,
    boundAgentName: null,
    loadingConversation: false,
    conversationLoadError: null,
    sessionModelOverride: null,
    sessionReasoningEffort: null,
    costControlModeOverride: null,
    subagentRoutingOverride: null,
    codexPlanMode: false,
    claudePermissionMode: "",
    hasMoreHistory: false,
    loadingMoreHistory: false,
    historyHiddenCount: null,
    oldestItemId: null,
    llmModel: null,
    pendingModelChange: null,
    sessionHarness: null,
    subAgentName: null,
    contextWindow: null,
    tokensUsed: null,
    sessionCostUsd: null,
    sessionUsageByModel: null,
    gitBranch: null,
    todos: [],
    skills: [],
    codexModelOptions: [],
    terminalPending: false,
    viewers: [],
    sandboxStatus: null,
    mcpStartup: null,
    abortController: null,
    runnerLaunchedAt: null,
    failedSendDraft: null,
    sendLatchedAt: null,
    historyGeneration: 0,
  };
}
