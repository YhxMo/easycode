import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Unit/component tests run in jsdom with mocked fetch/SSE — they never start
// the real backend or touch the user's ~/.easycode/ data (easycode-audit rule 3).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // macOS AppleDouble files (`. _*.tsx`) must never be picked up as tests.
    // Explicitly exclude them so a stray Finder/TimeMachine copy cannot
    // re-pollute the run (easycode-audit item-21 / TE-4+TE-5).
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      "**/cypress/**",
      "**/test-results/**",
      "**/playwright-report/**",
      "**/.git/**",
      "**/._*",
    ],
    css: false,
  },
});
