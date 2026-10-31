import { useEffect, useSyncExternalStore } from "react";
import { getCurrentAuthorId, resolveIdentity, subscribeIdentity } from "@/lib/identity";

/**
 * The viewer's author id (``getCurrentAuthorId``: ``null`` for the reserved
 * ``"local"`` id and before identity resolves), re-rendering when ``/v1/me``
 * settles. Render code must read identity through a hook: the React Compiler
 * caches an argument-less call like ``getCurrentAuthorId()`` on first render,
 * so a bare read made before ``/v1/me`` answers stays ``null`` forever.
 */
export function useViewerAuthorId(): string | null {
  useEffect(() => {
    void resolveIdentity();
  }, []);
  return useSyncExternalStore(subscribeIdentity, getCurrentAuthorId, getCurrentAuthorId);
}
