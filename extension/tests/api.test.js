import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiError,
  apiUrl,
  buildCheckPayload,
  normalizeHttpUrl,
  retryCheckClaim,
} from "../api.js";

test("apiUrl builds same-origin API URLs", () => {
  assert.equal(apiUrl("/health"), "http://127.0.0.1:8000/health");
  assert.equal(
    apiUrl("/v1/checks/check-1/events"),
    "http://127.0.0.1:8000/v1/checks/check-1/events",
  );
  assert.throws(
    () => apiUrl("https://example.com/v1/checks/check-1"),
    (error) => error instanceof ApiError && error.code === "invalid_backend_url",
  );
});

test("normalizeHttpUrl accepts web URLs and removes fragments", () => {
  assert.equal(normalizeHttpUrl(" https://example.com/story#comments "), "https://example.com/story");
  assert.throws(() => normalizeHttpUrl("javascript:alert(1)"), /http or https/);
  assert.throws(() => normalizeHttpUrl("not a url"), /valid http or https/);
});

test("buildCheckPayload constructs the exact page contract", () => {
  // The backend's CheckRequest field is `type` (not `mode`), and it has no
  // force_refresh field - extra="forbid" rejects unknown fields, so the wire
  // payload must be exactly {type, url, title, text}.
  assert.deepEqual(buildCheckPayload({
    mode: "page",
    page: {
      url: "https://example.com/article#section",
      title: " Article title ",
      text: " A factual article body with enough content. ",
    },
  }), {
    type: "page",
    url: "https://example.com/article",
    title: "Article title",
    text: "A factual article body with enough content.",
  });
});

test("buildCheckPayload validates URL and text modes", () => {
  assert.deepEqual(buildCheckPayload({ mode: "url", url: "https://example.com/a", forceRefresh: true }), {
    type: "url",
    url: "https://example.com/a",
  });
  assert.deepEqual(buildCheckPayload({ mode: "text", text: "  This is a factual claim to verify.  " }), {
    type: "text",
    text: "This is a factual claim to verify.",
  });
  assert.throws(() => buildCheckPayload({ mode: "text", text: "Too short" }), /at least 20/);
});

test("retryCheckClaim starts only the selected claim retry", async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ id: "check-1", status: "running" }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    const response = await retryCheckClaim("check-1", 3);
    assert.equal(response.status, "running");
    assert.equal(captured.url, "http://127.0.0.1:8000/v1/checks/check-1/claims/3/retry");
    assert.equal(captured.options.method, "POST");
    assert.equal(captured.options.body, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
