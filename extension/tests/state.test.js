import assert from "node:assert/strict";
import test from "node:test";

import {
  compactSession,
  isTerminalState,
  mergeEvent,
  normalizeSnapshot,
  statusCopy,
} from "../state.js";

test("normalizeSnapshot renders a completed empty check distinctly", () => {
  const snapshot = normalizeSnapshot({
    check_id: "check-empty",
    state: "complete",
    stage: "complete",
    claim_count: 0,
    results: [],
    sequence: 8,
  });
  assert.equal(snapshot.state, "complete_no_claims");
  assert.equal(statusCopy(snapshot)[0], "No checkable claims");
  assert.equal(isTerminalState(snapshot.state), true);
});

test("normalizeSnapshot accepts partial verdict and source aliases", () => {
  const snapshot = normalizeSnapshot({
    check_id: "check-1",
    state: "partial",
    claim_count: 2,
    partial_verdicts: [{
      claim_index: 1,
      claim_text: "The event happened in 2025.",
      verdict: "TRUE",
      confidence: 91,
      reasoning: "Two primary sources agree.",
      sources: [{ href: "https://example.com/source", name: "Primary source" }],
    }],
    errors: [{ message: "One provider timed out." }],
  });
  assert.equal(snapshot.results[0].claim, "The event happened in 2025.");
  assert.equal(snapshot.results[0].sources[0].title, "Primary source");
  assert.deepEqual(snapshot.errors, ["One provider timed out."]);
  assert.deepEqual(statusCopy(snapshot), ["Partially complete", "1 of 2 claims were checked."]);
});

test("mergeEvent fills direct verdict events and ignores stale sequences", () => {
  const current = normalizeSnapshot({
    check_id: "check-2",
    state: "searching",
    stage: "searching",
    claim_count: 3,
    sequence: 4,
  });
  const merged = mergeEvent(current, {
    sequence: 5,
    claim_count: 3,
    claim_index: 0,
    claim: "The measured value is 42.",
    verdict: "FALSE",
    confidence: 87,
    explanation: "The official dataset reports 41.",
    sources: ["https://example.com/data"],
  }, "verdict");

  assert.equal(merged.state, "verifying");
  assert.equal(merged.claimCount, 3);
  assert.equal(merged.results.length, 1);
  assert.equal(merged.results[0].verdict, "FALSE");

  const stale = mergeEvent(merged, { sequence: 5, state: "failed" }, "failed");
  assert.deepEqual(stale, merged);
});

test("compactSession preserves recovery fields without source input text", () => {
  const snapshot = normalizeSnapshot({
    check_id: "check-3",
    state: "verifying",
    stage: "verifying",
    claim_count: 4,
    completed_result_count: 1,
    sequence: 9,
  });
  const compact = compactSession(snapshot, {
    checkId: "check-3",
    snapshotPath: "/v1/checks/check-3",
    eventsPath: "/v1/checks/check-3/events",
    mode: "text",
    context: { title: "Pasted text" },
  });
  assert.equal(compact.sequence, 9);
  assert.equal(compact.claimCount, 4);
  assert.equal("text" in compact, false);
});
