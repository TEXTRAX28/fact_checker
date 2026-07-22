import assert from "node:assert/strict";
import test from "node:test";

import {
  PAGE_CAPTURE_ORIGINS,
  hasNewPageSinceCheck,
  requestFreshPageCapture,
} from "../page-capture.js";

test("requestFreshPageCapture requests optional page access before refreshing", async () => {
  const calls = [];
  const chromeApi = {
    permissions: {
      async request(value) {
        calls.push(["permission", value]);
        return true;
      },
    },
    runtime: {
      async sendMessage(value) {
        calls.push(["message", value]);
        return { ok: true, tabId: 42 };
      },
    },
  };

  const result = await requestFreshPageCapture(chromeApi);

  assert.deepEqual(result, { ok: true, tabId: 42 });
  assert.deepEqual(calls, [
    ["permission", { origins: PAGE_CAPTURE_ORIGINS }],
    ["message", { type: "REFRESH_PAGE_CAPTURE" }],
  ]);
});

test("requestFreshPageCapture does not capture when page access is declined", async () => {
  let messageSent = false;
  const result = await requestFreshPageCapture({
    permissions: { request: async () => false },
    runtime: { sendMessage: async () => { messageSent = true; } },
  });

  assert.equal(result.ok, false);
  assert.match(result.message, /Allow page access/);
  assert.equal(messageSent, false);
});

test("hasNewPageSinceCheck distinguishes the next input from preserved results", () => {
  const job = {
    mode: "page",
    context: { url: "https://news.example/first#section" },
  };
  assert.equal(hasNewPageSinceCheck(
    job, { status: "ready", url: "https://news.example/first" }, "page", true,
  ), false);
  assert.equal(hasNewPageSinceCheck(
    job, { status: "ready", url: "https://news.example/second" }, "page", true,
  ), true);
  assert.equal(hasNewPageSinceCheck(
    job, { status: "collecting", url: "https://news.example/second" }, "page", true,
  ), false);
});
