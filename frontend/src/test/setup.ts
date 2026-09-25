import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Node 22+ ships a global `localStorage` that stays undefined without
// `--localstorage-file`, and vitest's jsdom environment does not overwrite a
// global that already exists — so the App's tab list would silently lose its
// storage. Install an in-memory equivalent for the test run.
if (typeof window !== "undefined" && !window.localStorage) {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, String(value)),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
      key: (index: number) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
  });
}

// The App auto-scrolls the message pane on every items change. jsdom does not
// implement element/window scrolling and would otherwise emit noisy "Not
// implemented" errors, so stub the scrollTo calls out (test infra only).
if (typeof window !== "undefined" && !window.scrollTo) {
  window.scrollTo = () => {};
}
if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = () => {};
}

// RTL does not auto-cleanup when `globals: false`, so unmount between tests to
// keep the jsdom DOM isolated (no leaks across tests). Persisted UI state is
// cleared with it, so one test's open tabs cannot seed the next.
afterEach(() => {
  cleanup();
  window.localStorage.clear();
});
