// Persisted preference for the key row under the terminal (Esc, Tab, arrows,
// Enter, ^C). A phone's soft keyboard has none of those keys, so `auto` shows
// the row on coarse-pointer (touch) devices while `on`/`off` pin it. A change
// reaches mounted terminals live through the pub/sub below.

const STORAGE_KEY = "omnigent:terminal-keys";

export const terminalKeysModes = ["auto", "on", "off"] as const;
export type TerminalKeysMode = (typeof terminalKeysModes)[number];
export const TERMINAL_KEYS_DEFAULT: TerminalKeysMode = "auto";

/** Return whether a string is one of the selectable terminal key row modes. */
export function isTerminalKeysMode(value: string | null | undefined): value is TerminalKeysMode {
  return value === "auto" || value === "on" || value === "off";
}

/** Normalize a stored value; anything unknown falls back to `auto`. */
export function normalizeTerminalKeysMode(value: string | null | undefined): TerminalKeysMode {
  return isTerminalKeysMode(value) ? value : TERMINAL_KEYS_DEFAULT;
}

/**
 * Read the persisted key row mode. Returns `auto` when nothing is stored, on a
 * server render, or when storage is unreadable; never throws.
 */
export function readTerminalKeysMode(): TerminalKeysMode {
  if (typeof window === "undefined") return TERMINAL_KEYS_DEFAULT;
  try {
    return normalizeTerminalKeysMode(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return TERMINAL_KEYS_DEFAULT;
  }
}

/**
 * Persist the key row mode, then notify subscribers. `auto` clears the key.
 * Storage errors are swallowed, and subscribers still get the new value.
 */
export function writeTerminalKeysMode(mode: TerminalKeysMode): void {
  const normalized = normalizeTerminalKeysMode(mode);
  if (typeof window !== "undefined") {
    try {
      if (normalized === TERMINAL_KEYS_DEFAULT) {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        window.localStorage.setItem(STORAGE_KEY, normalized);
      }
    } catch {
      // localStorage quota or access errors shouldn't break the app.
    }
  }
  emit(normalized);
}

/** Whether the key row shows, given the mode and whether the pointer is coarse. */
export function resolveTerminalKeysVisible(
  mode: TerminalKeysMode,
  isCoarsePointer: boolean,
): boolean {
  switch (mode) {
    case "auto":
      return isCoarsePointer;
    case "on":
      return true;
    case "off":
      return false;
    default: {
      const exhaustive: never = mode;
      return exhaustive;
    }
  }
}

type TerminalKeysListener = (mode: TerminalKeysMode) => void;

const listeners = new Set<TerminalKeysListener>();

/** Subscribe to key row mode changes. Returns an unsubscribe function. */
export function subscribeTerminalKeys(listener: TerminalKeysListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function emit(mode: TerminalKeysMode): void {
  for (const listener of listeners) listener(mode);
}
