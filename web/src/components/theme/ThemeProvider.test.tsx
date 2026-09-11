import type { ReactNode } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "./ThemeProvider";

const themeState = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    setThemeSource: vi.fn(),
    theme: "system" as string | undefined,
    listeners,
    setTheme(next: string) {
      this.theme = next;
      for (const l of listeners) l();
    },
  };
});

vi.mock("@/lib/nativeBridge", () => ({
  setThemeSource: themeState.setThemeSource,
}));

// The mock useTheme must be REACTIVE (subscription-backed), like the real
// next-themes context. A plain read of themeState.theme relied on the parent
// rerender cascading into NativeThemeSync — the React Compiler memoizes the
// child element, so that cascade legitimately no longer happens.
vi.mock("next-themes", async () => {
  const { useSyncExternalStore } = await import("react");
  return {
    ThemeProvider: ({ children }: { children: ReactNode }) => children,
    useTheme: () => ({
      theme: useSyncExternalStore(
        (onChange) => {
          themeState.listeners.add(onChange);
          return () => themeState.listeners.delete(onChange);
        },
        () => themeState.theme,
      ),
    }),
  };
});

beforeEach(() => {
  themeState.setThemeSource.mockClear();
  themeState.theme = "system";
});

afterEach(cleanup);

describe("ThemeProvider native theme sync", () => {
  it("tells the native shell to follow the system theme by default", () => {
    render(<ThemeProvider>content</ThemeProvider>);
    expect(themeState.setThemeSource).toHaveBeenCalledWith("system");
  });

  it("updates the native shell when the user selects an explicit theme", () => {
    render(<ThemeProvider>content</ThemeProvider>);
    themeState.setThemeSource.mockClear();

    act(() => themeState.setTheme("light"));

    expect(themeState.setThemeSource).toHaveBeenCalledWith("light");
  });
});
