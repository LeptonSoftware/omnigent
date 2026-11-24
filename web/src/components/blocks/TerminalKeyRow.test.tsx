import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TERMINAL_ROW_KEYS,
  TerminalKeyRow,
  terminalRowKeyBytes,
  type TerminalRowKey,
} from "./TerminalKeyRow";

afterEach(() => {
  cleanup();
});

describe("TERMINAL_ROW_KEYS", () => {
  it("puts the menu keys first, where a phone shows them without scrolling", () => {
    expect(TERMINAL_ROW_KEYS.slice(0, 4).map((k) => k.key)).toEqual([
      "up",
      "down",
      "enter",
      "escape",
    ]);
  });
});

describe("terminalRowKeyBytes", () => {
  it.each<[TerminalRowKey, string]>([
    ["escape", "\x1b"],
    ["tab", "\t"],
    ["shift-tab", "\x1b[Z"],
    ["enter", "\r"],
    ["interrupt", "\x03"],
  ])("sends %s as the same bytes in either cursor mode", (key, bytes) => {
    expect(terminalRowKeyBytes(key, false)).toBe(bytes);
    expect(terminalRowKeyBytes(key, true)).toBe(bytes);
  });

  it.each<[TerminalRowKey, string, string]>([
    ["up", "\x1b[A", "\x1bOA"],
    ["down", "\x1b[B", "\x1bOB"],
    ["right", "\x1b[C", "\x1bOC"],
    ["left", "\x1b[D", "\x1bOD"],
  ])("sends %s as CSI normally and SS3 in application cursor mode", (key, normal, application) => {
    expect(terminalRowKeyBytes(key, false)).toBe(normal);
    expect(terminalRowKeyBytes(key, true)).toBe(application);
  });
});

describe("TerminalKeyRow", () => {
  it("renders every key, in order, as a labelled button", () => {
    render(<TerminalKeyRow disabled={false} onKey={() => {}} />);
    const row = screen.getByRole("group", { name: "Terminal keys" });
    const names = screen.getAllByRole("button").map((button) => button.getAttribute("aria-label"));
    expect(names).toEqual([
      "Up arrow",
      "Down arrow",
      "Enter",
      "Escape",
      "Left arrow",
      "Right arrow",
      "Tab",
      "Shift Tab",
      "Control C",
    ]);
    expect(TERMINAL_ROW_KEYS).toHaveLength(9);
    // One row that scrolls sideways at narrow widths, with ~40 px tap targets.
    expect(row).toHaveClass("flex", "overflow-x-auto", "shrink-0");
    for (const button of screen.getAllByRole("button")) {
      expect(button).toHaveClass("h-10", "min-w-10", "shrink-0");
    }
  });

  it("reports the tapped key on click", () => {
    const onKey = vi.fn();
    render(<TerminalKeyRow disabled={false} onKey={onKey} />);
    fireEvent.click(screen.getByRole("button", { name: "Down arrow" }));
    fireEvent.click(screen.getByRole("button", { name: "Enter" }));
    expect(onKey.mock.calls).toEqual([["down"], ["enter"]]);
  });

  it("prevents mousedown so a tap never moves focus, and leaves pointerdown alone", () => {
    render(<TerminalKeyRow disabled={false} onKey={() => {}} />);
    const button = screen.getByRole("button", { name: "Escape" });
    // fireEvent returns false when a handler called preventDefault.
    expect(fireEvent.mouseDown(button)).toBe(false);
    // A cancelled touch pointerdown makes WebKit (iOS) drop the tap's click.
    expect(fireEvent.pointerDown(button)).toBe(true);
    // Gaps between keys too, so a near miss doesn't blur the terminal.
    const row = screen.getByRole("group", { name: "Terminal keys" });
    expect(fireEvent.mouseDown(row)).toBe(false);
    expect(fireEvent.pointerDown(row)).toBe(true);
  });

  it("disables every key while the bridge is down", () => {
    const onKey = vi.fn();
    render(<TerminalKeyRow disabled onKey={onKey} />);
    for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Enter" }));
    expect(onKey).not.toHaveBeenCalled();
  });
});
