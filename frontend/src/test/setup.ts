import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

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
// keep the jsdom DOM isolated (no leaks across tests).
afterEach(() => {
  cleanup();
});
