// Who owns a conversation, from the session list's `owner` grant.

import type { Conversation } from "@/hooks/useConversations";

/**
 * Whether `viewerId` owns `conversation`.
 *
 * Derived purely from the session's `owner` (the creator's user id), because
 * list rows carry no effective-level info — the server lists them without
 * resolving the caller's grant per session. A `null`/absent owner (permissions
 * disabled — the server emits `owner` only when a permission store is wired)
 * reads as owned, matching the prior permissive-on-null stance. In single-user
 * mode the owner grant and the viewer id are both the reserved `"local"` id,
 * so they match via the equality branch. A `null` `viewerId` (identity not yet
 * resolved) reads as "not the owner" for a shared row.
 */
export function isOwnedByViewer(conversation: Conversation, viewerId: string | null): boolean {
  const owner = conversation.owner ?? null;
  if (owner === null) return true;
  return owner === viewerId;
}
