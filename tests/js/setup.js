import { afterEach } from "vitest";

// The scripts under test wire every write as a fire-and-forget `guard(async …)`
// callback, so a throw inside a handler becomes an *unhandled promise
// rejection* rather than a synchronous error jsdom would report to the
// virtualConsole. Collect those process-level rejections and fail the owning
// test, so a broken handler can never slip through green.
const rejections = [];
process.on("unhandledRejection", (reason) => rejections.push(reason));

afterEach(async () => {
  // Give any just-scheduled rejection a tick to surface before we check.
  await new Promise((resolve) => setTimeout(resolve, 0));
  if (rejections.length) {
    const collected = rejections.splice(0);
    throw new Error(
      "Unhandled rejection(s) in a script handler:\n" +
        collected.map((r) => (r && r.stack) || String(r)).join("\n"),
    );
  }
});
