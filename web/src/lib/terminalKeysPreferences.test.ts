import { afterEach, describe, expect, it, vi } from "vitest";
import {
  normalizeTerminalKeysMode,
  readTerminalKeysMode,
  resolveTerminalKeysVisible,
  subscribeTerminalKeys,
  TERMINAL_KEYS_DEFAULT,
  writeTerminalKeysMode,
} from "./terminalKeysPreferences";

const STORAGE_KEY = "omnigent:terminal-keys";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("terminalKeysPreferences — read/write", () => {
  it("returns auto when nothing is stored", () => {
    expect(TERMINAL_KEYS_DEFAULT).toBe("auto");
    expect(readTerminalKeysMode()).toBe("auto");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("stores raw on and off strings", () => {
    writeTerminalKeysMode("on");
    expect(readTerminalKeysMode()).toBe("on");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("on");

    writeTerminalKeysMode("off");
    expect(readTerminalKeysMode()).toBe("off");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("off");
  });

  it("removes the key when written auto", () => {
    writeTerminalKeysMode("off");
    writeTerminalKeysMode("auto");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readTerminalKeysMode()).toBe("auto");
  });

  it("reads a corrupt stored value as auto", () => {
    localStorage.setItem(STORAGE_KEY, "sometimes");
    expect(readTerminalKeysMode()).toBe("auto");
  });

  it("never throws when storage is unreadable or unwritable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(readTerminalKeysMode()).toBe("auto");
    expect(() => writeTerminalKeysMode("on")).not.toThrow();
  });
});

describe("normalizeTerminalKeysMode", () => {
  it("passes through valid modes", () => {
    expect(normalizeTerminalKeysMode("auto")).toBe("auto");
    expect(normalizeTerminalKeysMode("on")).toBe("on");
    expect(normalizeTerminalKeysMode("off")).toBe("off");
  });

  it("maps unknown, null, and undefined to auto", () => {
    expect(normalizeTerminalKeysMode("true")).toBe("auto");
    expect(normalizeTerminalKeysMode(null)).toBe("auto");
    expect(normalizeTerminalKeysMode(undefined)).toBe("auto");
  });
});

describe("resolveTerminalKeysVisible", () => {
  it("follows the pointer when mode is auto", () => {
    expect(resolveTerminalKeysVisible("auto", true)).toBe(true);
    expect(resolveTerminalKeysVisible("auto", false)).toBe(false);
  });

  it("pins the row on regardless of the pointer", () => {
    expect(resolveTerminalKeysVisible("on", true)).toBe(true);
    expect(resolveTerminalKeysVisible("on", false)).toBe(true);
  });

  it("pins the row off regardless of the pointer", () => {
    expect(resolveTerminalKeysVisible("off", true)).toBe(false);
    expect(resolveTerminalKeysVisible("off", false)).toBe(false);
  });
});

describe("terminalKeysPreferences — pub/sub", () => {
  it("notifies subscribers with the written mode", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeTerminalKeys(listener);
    writeTerminalKeysMode("off");
    expect(listener).toHaveBeenCalledWith("off");
    unsubscribe();
  });

  it("notifies the intended mode even when the write fails", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    const listener = vi.fn();
    const unsubscribe = subscribeTerminalKeys(listener);
    writeTerminalKeysMode("on");
    expect(listener).toHaveBeenCalledWith("on");
    unsubscribe();
  });

  it("stops notifying after unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeTerminalKeys(listener);
    unsubscribe();
    writeTerminalKeysMode("on");
    expect(listener).not.toHaveBeenCalled();
  });
});
