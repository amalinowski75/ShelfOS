import { defineConfig } from "vitest/config";

// The browser scripts are plain (non-module) files that mutate the DOM on load,
// so each test builds an isolated jsdom window by hand (see tests/js/harness.js)
// rather than relying on a shared Vitest jsdom environment.
export default defineConfig({
  test: {
    include: ["tests/js/**/*.test.js"],
    environment: "node",
    setupFiles: ["tests/js/setup.js"],
  },
});
