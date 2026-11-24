// On-screen keys for touch screens: a soft keyboard has no Esc, Tab or arrow
// keys, so a terminal menu (e.g. "❯ No / Yes", needing ↓ then Enter) can't be
// answered without them.

import { ArrowDownIcon, ArrowLeftIcon, ArrowRightIcon, ArrowUpIcon } from "lucide-react";
import type { ReactNode, SyntheticEvent } from "react";
import { Button } from "@/components/ui/button";

export type TerminalRowKey =
  "escape" | "tab" | "shift-tab" | "up" | "down" | "left" | "right" | "enter" | "interrupt";

interface TerminalRowKeySpec {
  key: TerminalRowKey;
  label: ReactNode;
  ariaLabel: string;
}

/**
 * Menu keys first: a terminal menu is answered with ↑/↓ then Enter (or Esc), and
 * a 390 px phone shows only the first six keys before the row scrolls.
 */
export const TERMINAL_ROW_KEYS: readonly TerminalRowKeySpec[] = [
  { key: "up", label: <ArrowUpIcon aria-hidden="true" />, ariaLabel: "Up arrow" },
  { key: "down", label: <ArrowDownIcon aria-hidden="true" />, ariaLabel: "Down arrow" },
  { key: "enter", label: "Enter", ariaLabel: "Enter" },
  { key: "escape", label: "Esc", ariaLabel: "Escape" },
  { key: "left", label: <ArrowLeftIcon aria-hidden="true" />, ariaLabel: "Left arrow" },
  { key: "right", label: <ArrowRightIcon aria-hidden="true" />, ariaLabel: "Right arrow" },
  { key: "tab", label: "Tab", ariaLabel: "Tab" },
  { key: "shift-tab", label: "⇧Tab", ariaLabel: "Shift Tab" },
  { key: "interrupt", label: "^C", ariaLabel: "Control C" },
];

/** CSI (`ESC [`) normally; SS3 (`ESC O`) once the program enables DECCKM. */
function cursorKey(final: "A" | "B" | "C" | "D", applicationCursorKeys: boolean): string {
  return applicationCursorKeys ? `\x1bO${final}` : `\x1b[${final}`;
}

/** The bytes a real keyboard would send for `key`. */
export function terminalRowKeyBytes(key: TerminalRowKey, applicationCursorKeys: boolean): string {
  switch (key) {
    case "escape":
      return "\x1b";
    case "tab":
      return "\t";
    case "shift-tab":
      return "\x1b[Z";
    case "up":
      return cursorKey("A", applicationCursorKeys);
    case "down":
      return cursorKey("B", applicationCursorKeys);
    case "right":
      return cursorKey("C", applicationCursorKeys);
    case "left":
      return cursorKey("D", applicationCursorKeys);
    case "enter":
      return "\r";
    case "interrupt":
      return "\x03";
    default: {
      const exhaustive: never = key;
      return exhaustive;
    }
  }
}

/**
 * Keep focus where it is, so the soft keyboard stays open or closed across a tap.
 * Only mousedown: a tap's focus change rides the synthetic mousedown. Cancelling
 * pointerdown as well made WebKit treat the touch as handled and drop the click,
 * so on iOS the keys sent nothing (VIN-24216 review).
 */
function keepFocus(event: SyntheticEvent): void {
  event.preventDefault();
}

export function TerminalKeyRow({
  disabled,
  onKey,
}: {
  /** True while the bridge isn't connected: the row stays visible but inert. */
  disabled: boolean;
  onKey: (key: TerminalRowKey) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Terminal keys"
      data-testid="terminal-key-row"
      onMouseDown={keepFocus}
      className="flex shrink-0 items-center gap-1 overflow-x-auto border-t border-border bg-background px-2 py-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      {TERMINAL_ROW_KEYS.map(({ key, label, ariaLabel }) => (
        <Button
          key={key}
          type="button"
          variant="outline"
          size="sm"
          aria-label={ariaLabel}
          disabled={disabled}
          onClick={() => onKey(key)}
          className="h-10 min-w-10 shrink-0 touch-manipulation px-2.5"
        >
          {label}
        </Button>
      ))}
    </div>
  );
}
