// localStorage-backed recent workspace directories, keyed per host
// (paths are host-specific). Feeds the combobox "Recent" group.

import { useCallback, useState } from "react";

const STORAGE_KEY = "omnigent:recent-workspaces";
const MAX_PER_HOST = 8;

// Map of host_id -> most-recent-first list of absolute paths.
type RecentMap = Record<string, string[]>;

function readAll(): RecentMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object") return {};
    // Keep only string[] values; drop anything malformed so a
    // corrupted entry can't crash the picker.
    const out: RecentMap = {};
    for (const [host, list] of Object.entries(parsed as Record<string, unknown>)) {
      if (Array.isArray(list)) {
        out[host] = list.filter((x): x is string => typeof x === "string");
      }
    }
    return out;
  } catch {
    return {};
  }
}

function writeAll(map: RecentMap): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Quota exceeded or storage disabled — non-fatal; recents just
    // stop persisting until the next successful write.
  }
}

export interface RecentWorkspaces {
  /** Most-recent-first absolute paths used on this host. */
  recent: string[];
  /**
   * Record ``path`` as the newest recent for the current host.
   * De-duplicates (moves an existing entry to the front) and caps
   * the list. No-op when ``hostId`` is null or the path is blank.
   */
  addRecent: (path: string) => void;
}

/**
 * Track recently-used workspace directories for one host.
 *
 * @param hostId Host whose recents to read/write, e.g.
 *   ``"host_a1b2..."``. ``null`` yields an empty list and a no-op
 *   ``addRecent`` (nothing is host-scoped yet).
 * @returns The host's recent paths plus an ``addRecent`` recorder.
 */
const NO_RECENTS: string[] = [];

export function useRecentWorkspaces(hostId: string | null): RecentWorkspaces {
  // State owns the whole per-host map; localStorage is only its durable
  // mirror. The previous shape read storage inside a useMemo keyed on a
  // bumped counter — an impure memo the React Compiler correctly freezes.
  // Deriving from state keyed on hostId keeps ``recent`` consistent with the
  // current host on the same render (an effect-based hydration lagged one
  // render behind hostId, briefly leaking the previous host's paths).
  const [all, setAll] = useState<Record<string, string[]>>(readAll);
  const recent = hostId === null ? NO_RECENTS : (all[hostId] ?? NO_RECENTS);

  const addRecent = useCallback(
    (path: string) => {
      if (hostId === null) return;
      const trimmed = path.trim();
      if (!trimmed) return;
      setAll((prev) => {
        const existing = prev[hostId] ?? [];
        const next = [trimmed, ...existing.filter((p) => p !== trimmed)].slice(0, MAX_PER_HOST);
        const nextAll = { ...prev, [hostId]: next };
        writeAll(nextAll);
        return nextAll;
      });
    },
    [hostId],
  );

  return { recent, addRecent };
}
