import assert from "node:assert/strict";
import test from "node:test";

import {
  buildExportData,
  claimProgressCopy,
  compactSession,
  isTerminalState,
  mergeEvent,
  normalizeSnapshot,
  snapshotBelongsToJob,
  snapshotIsStale,
  statusCopy,
  toMarkdownReport,
} from "../state.js";

test("snapshotBelongsToJob rejects a response from a replaced job", () => {
  assert.equal(snapshotBelongsToJob({ id: "check-old" }, "check-new"), false);
  assert.equal(snapshotBelongsToJob({ check_id: "check-new" }, "check-new"), true);
  assert.equal(snapshotBelongsToJob({}, "check-new"), false);
});

test("snapshotIsStale rejects an older response from the same job", () => {
  assert.equal(snapshotIsStale({ sequence: 8 }, { sequence: 9 }), true);
  assert.equal(snapshotIsStale({ sequence: 9 }, { sequence: 9 }), false);
  assert.equal(snapshotIsStale({ sequence: 10 }, { sequence: 9 }), false);
});

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
  assert.equal(snapshot.results[0].index, 1);
  assert.equal(snapshot.results[0].sources[0].title, "Primary source");
  assert.deepEqual(snapshot.errors, ["One provider timed out."]);
  assert.deepEqual(statusCopy(snapshot), ["Partially complete", "1 of 2 claims were checked."]);
});

// The four tests below use the real event envelope jobs.py::JobManager._append_event
// actually sends - {event: "status"|"progress"|"result"|"terminal", data: {...}} - not
// the stage-named-event vocabulary an earlier draft assumed. That mismatch meant every
// SSE event was silently dropped in the browser (a named SSE event only reaches a
// listener registered for that exact name); confirmed by reading jobs.py/api.py
// directly, not assumed.

test("mergeEvent handles a status event (e.g. cancelling)", () => {
  const current = normalizeSnapshot({ id: "check-2", status: "running", sequence: 4 });
  const merged = mergeEvent(current, { sequence: 5, status: "cancelling" }, "status");
  assert.equal(merged.state, "cancelling");
});

test("mergeEvent reveals extracted claims before any verdict finishes", () => {
  const current = normalizeSnapshot({ id: "check-2", status: "running", sequence: 2 });
  const merged = mergeEvent(current, {
    sequence: 3,
    claim_count: 2,
    claims: [
      { claim_index: 0, claim: "First detected claim", speaker: "A" },
      { claim_index: 1, claim: "Second detected claim", speaker: "B" },
    ],
  }, "claims");

  assert.equal(merged.claimCount, 2);
  assert.deepEqual(merged.claimManifest.map((claim) => claim.claim), [
    "First detected claim", "Second detected claim",
  ]);
  assert.deepEqual(merged.claimProgress, { 0: "waiting", 1: "waiting" });
});

test("mergeEvent handles a progress event, refining stage and claim count", () => {
  const current = normalizeSnapshot({ id: "check-2", status: "running", sequence: 4 });
  const merged = mergeEvent(current, {
    sequence: 5,
    stage: "verifying",
    state: "started",
    claim_index: 2,
    claim_count: 5,
  }, "progress");
  assert.equal(merged.state, "verifying");
  assert.equal(merged.claimCount, 5);
  assert.equal(merged.claimProgress[2], "verifying");
});

test("mergeEvent handles a result event - data IS the verdict, not nested", () => {
  const current = normalizeSnapshot({
    id: "check-2", status: "running", sequence: 4,
  });
  const merged = mergeEvent(current, {
    sequence: 5,
    claim_index: 0,
    claim: "The measured value is 42.",
    verdict: "FALSE",
    confidence: 87,
    explanation: "The official dataset reports 41.",
    sources: ["https://example.com/data"],
  }, "result");

  assert.equal(merged.state, "verifying");
  assert.equal(merged.results.length, 1);
  assert.equal(merged.results[0].verdict, "FALSE");
  assert.equal(merged.claimProgress[0], "complete");
});

test("mergeEvent replaces provider usage totals without double counting replay", () => {
  const current = normalizeSnapshot({ id: "check-usage", status: "running", sequence: 4 });
  const data = {
    sequence: 5,
    deepinfra: {
      requests: 2,
      successful_requests: 2,
      input_tokens: 120,
      output_tokens: 30,
      total_tokens: 150,
      complete: true,
    },
    tavily: {
      search_attempts: 3,
      successful_searches: 3,
      estimated_credits: 6,
      complete: true,
    },
    complete: true,
  };
  const merged = mergeEvent(current, data, "usage");
  const replayed = mergeEvent(merged, data, "usage");
  assert.equal(replayed.usage.deepinfra.totalTokens, 150);
  assert.equal(replayed.usage.tavily.estimatedCredits, 6);
  assert.equal(replayed.usage.complete, true);
});

test("claimProgressCopy clearly marks unfinished claims as unverified", () => {
  assert.deepEqual(claimProgressCopy("waiting"), [
    "DETECTED", "Not verified yet. Waiting for source search.",
  ]);
  assert.deepEqual(claimProgressCopy("searching"), [
    "FINDING SOURCES", "Searching for relevant, reliable evidence.",
  ]);
  assert.deepEqual(claimProgressCopy("verifying"), [
    "CHECKING EVIDENCE", "Comparing this claim against accepted evidence.",
  ]);
});

test("mergeEvent handles a terminal event, unpacking the nested outcome", () => {
  const current = normalizeSnapshot({ id: "check-2", status: "running", sequence: 4 });
  const merged = mergeEvent(current, {
    sequence: 6,
    status: "completed",
    outcome: {
      status: "completed",
      results: [{ claim_index: 0, claim: "x", verdict: "TRUE", confidence: 90 }],
      errors: [],
      claim_count: 1,
      completed_count: 1,
      message: "Fact-check completed.",
    },
  }, "terminal");

  assert.equal(merged.state, "complete");
  assert.equal(merged.results.length, 1);
  assert.equal(merged.results[0].verdict, "TRUE");
});

test("mergeEvent ignores an event whose sequence is not newer than current", () => {
  const current = normalizeSnapshot({ id: "check-2", status: "running", sequence: 5 });
  const stale = mergeEvent(current, { sequence: 5, status: "cancelling" }, "status");
  assert.deepEqual(stale, current);
});

test("normalizeSnapshot refines the backend's coarse \"running\" status using progress.stage (polling path)", () => {
  // This is the real shape jobs.py's snapshot endpoint sends while a job is active:
  // a coarse top-level "running" status, with the actual stage nested under
  // progress.stage. Earlier tests in this file passed state directly as
  // "verifying" etc., which never exercises this path and is why the bug this
  // test guards against went unnoticed until it was seen live.
  const snapshot = normalizeSnapshot({
    id: "check-4",
    status: "running",
    progress: { stage: "verifying", state: "started", claim_count: 6 },
    sequence: 12,
  });
  assert.equal(snapshot.state, "verifying");
  // claim_count: 6 is truthy, so statusCopy's count-aware branch applies, not its
  // zero-claims fallback text.
  assert.deepEqual(statusCopy(snapshot), ["Checking evidence", "Verified 0 of 6 claims."]);
});

test("normalizeSnapshot does not let a stale progress.stage override a genuinely terminal status", () => {
  const snapshot = normalizeSnapshot({
    id: "check-5",
    status: "completed",
    // A leftover progress object from the last stage before completion - must not
    // pull state back to "verifying" once the job has actually finished.
    progress: { stage: "verifying", state: "completed" },
    results: [{ claim: "x", verdict: "TRUE", confidence: 90 }],
    sequence: 20,
  });
  assert.equal(snapshot.state, "complete");
});

test("normalizeSnapshot leaves \"running\" alone when progress.stage isn't a known in-flight stage", () => {
  const snapshot = normalizeSnapshot({
    id: "check-6",
    status: "running",
    progress: {},
    sequence: 1,
  });
  assert.equal(snapshot.state, "running");
});

test("every real jobs.py terminal status is recognized as terminal, with non-generic copy", () => {
  // jobs.py::TERMINAL_STATUSES = {cancelled, completed, failed, invalid_input,
  // no_claims, no_evidence, partial, rate_limited, timeout, unreadable}. Missing
  // any of these here means that outcome leaves the UI stuck forever - this is
  // the exact bug a live DeepInfra timeout exposed (status: "timeout" was not
  // recognized as terminal before this fix).
  const backendTerminalStatuses = [
    "cancelled", "completed", "failed", "invalid_input", "no_claims",
    "no_evidence", "partial", "rate_limited", "timeout", "unreadable",
  ];
  for (const status of backendTerminalStatuses) {
    const snapshot = normalizeSnapshot({ id: "check-x", status, sequence: 1 });
    assert.equal(isTerminalState(snapshot.state), true, `${status} must be terminal`);
    assert.notDeepEqual(
      statusCopy(snapshot),
      ["Check in progress", "Waiting for the latest status."],
      `${status} must not fall back to the generic in-progress copy`,
    );
  }
});

test("a live 'invalid_input' terminal snapshot (captured from the real server) is handled correctly", () => {
  // Exact shape curl'd from a live api.py run: POST /v1/checks {type:"text",text:"hi"}.
  const snapshot = normalizeSnapshot({
    id: "k2Apxklr6iokSFf8p3irA2lKwQLbgrl6",
    type: "text",
    status: "invalid_input",
    sequence: 4,
    progress: { stage: "validating", state: "started" },
    results: [],
    outcome: {
      status: "invalid_input", results: [], claim_count: 0, errors: [],
      message: "Text must contain at least 10 non-whitespace characters.",
      normalized_url: null, metadata: { source: "text" }, completed_count: 0,
    },
  });
  assert.equal(snapshot.state, "invalid_input");
  assert.equal(isTerminalState(snapshot.state), true);
  assert.deepEqual(statusCopy(snapshot), ["Invalid input", "That input could not be checked."]);
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
    jobToken: "job-access-token",
    mode: "text",
    context: { title: "Pasted text" },
  });
  assert.equal(compact.sequence, 9);
  assert.equal(compact.claimCount, 4);
  assert.equal(compact.jobToken, "job-access-token");
  assert.equal("text" in compact, false);
});

test("normalizeSnapshot retains structured failed-claim retry state", () => {
  const snapshot = normalizeSnapshot({
    id: "check-retry",
    status: "partial",
    claim_count: 2,
    results: [{ claim_index: 0, claim: "done", verdict: "TRUE" }],
    claim_manifest: [
      { claim_index: 0, claim: "done", speaker: "A" },
      { claim_index: 1, claim: "failed claim", speaker: "A" },
    ],
    errors: [{
      claim_index: 1,
      stage: "verification",
      code: "provider_timeout",
      message: "A provider request timed out.",
    }],
    retrying_claim_index: 1,
    retry_attempts: { 1: 1 },
    claim_retry_limit: 2,
    claim_progress: { 0: "complete", 1: "verifying" },
  });

  assert.deepEqual(snapshot.errors, []);
  assert.deepEqual(snapshot.claimErrors, [{
    claimIndex: 1,
    stage: "verification",
    code: "provider_timeout",
    message: "A provider request timed out.",
  }]);
  assert.equal(snapshot.claimManifest[1].claim, "failed claim");
  assert.equal(snapshot.retryingClaimIndex, 1);
  assert.equal(snapshot.retryAttempts[1], 1);
  assert.deepEqual(snapshot.claimProgress, { 0: "complete", 1: "verifying" });

  const terminal = mergeEvent(snapshot, {
    sequence: 5,
    status: "completed",
    outcome: { results: snapshot.results, errors: [], claim_count: 2 },
  }, "terminal");
  assert.equal(terminal.retryingClaimIndex, null);
});

test("buildExportData shapes a completed snapshot for export", () => {
  // normalizeSnapshot reads raw.results directly - real snapshot responses have
  // this at the top level (jobs.py::_snapshot_locked), not only nested under
  // outcome (only mergeEvent's "terminal" branch unnests outcome.results itself).
  const snapshot = normalizeSnapshot({
    id: "check-6",
    status: "completed",
    results: [
      {
        claim_index: 0, claim: "The Eiffel Tower is in Paris.", verdict: "TRUE",
        confidence: 95, explanation: "Confirmed by official sources.",
        sources: ["https://en.wikipedia.org/wiki/Eiffel_Tower"],
      },
    ],
    claim_count: 1,
  });
  const data = buildExportData(snapshot, { mode: "url", url: "https://example.com/article" });

  assert.equal(data.source.mode, "url");
  assert.equal(data.source.url, "https://example.com/article");
  assert.equal(data.summary.completedCount, 1);
  assert.equal(data.claims.length, 1);
  assert.equal(data.claims[0].verdict, "TRUE");
  assert.equal(data.claims[0].sources[0].url, "https://en.wikipedia.org/wiki/Eiffel_Tower");
  assert.equal(typeof data.exportedAt, "string");
});

test("exports include usage totals but never capabilities or provider keys", () => {
  const snapshot = normalizeSnapshot({
    id: "check-secret-export",
    status: "completed",
    results: [],
    job_token: "private-job-token",
    deepinfraKey: "private-deepinfra-key",
    tavilyKey: "private-tavily-key",
    usage: {
      deepinfra: {
        input_tokens: 80,
        output_tokens: 20,
        total_tokens: 100,
        complete: true,
      },
      tavily: {
        search_attempts: 2,
        successful_searches: 2,
        estimated_credits: 4,
        complete: true,
      },
      complete: true,
    },
  });

  const serialized = JSON.stringify(
    buildExportData(snapshot, { mode: "text", title: "Pasted text" }),
  );
  assert.match(serialized, /"totalTokens":100/);
  assert.match(serialized, /"estimatedCredits":4/);
  assert.doesNotMatch(serialized, /private-job-token/);
  assert.doesNotMatch(serialized, /private-deepinfra-key/);
  assert.doesNotMatch(serialized, /private-tavily-key/);
});

test("buildExportData includes failed and unfinished claim slots", () => {
  const snapshot = normalizeSnapshot({
    id: "check-partial-export",
    status: "partial",
    claim_count: 3,
    results: [
      { claim_index: 0, claim: "Completed claim.", verdict: "TRUE", sources: [] },
    ],
    claim_manifest: [
      { claim_index: 0, claim: "Completed claim.", speaker: "A" },
      { claim_index: 1, claim: "Timed-out claim.", speaker: "A" },
      { claim_index: 2, claim: "Unfinished claim.", speaker: "A" },
    ],
    errors: [{
      claim_index: 1,
      stage: "verification",
      code: "provider_timeout",
      message: "A provider request timed out.",
    }],
  });

  const data = buildExportData(snapshot, { mode: "text", title: "Pasted text" });

  assert.equal(data.claims.length, 3);
  assert.equal(data.claims[0].status, "completed");
  assert.equal(data.claims[1].status, "failed");
  assert.equal(data.claims[1].claim, "Timed-out claim.");
  assert.equal(data.claims[1].error.code, "provider_timeout");
  assert.equal(data.claims[2].status, "incomplete");
  assert.equal(data.summary.failedCount, 1);
  assert.equal(data.summary.incompleteCount, 1);
  assert.equal(data.errors[0].claimIndex, 1);

  const markdown = toMarkdownReport(data);
  assert.match(markdown, /## 2\. FAILED/);
  assert.match(markdown, /Timed-out claim\./);
  assert.match(markdown, /A provider request timed out\./);
  assert.match(markdown, /## 3\. NOT COMPLETED/);
});

test("toMarkdownReport renders source, summary, and every claim", () => {
  const data = buildExportData(
    normalizeSnapshot({
      id: "check-7",
      status: "completed",
      results: [
        { claim_index: 0, claim: "Claim one.", verdict: "FALSE", confidence: 88,
          explanation: "Contradicted by the source.", sources: ["https://example.com/a"] },
      ],
      claim_count: 1,
    }),
    { mode: "text", title: "Pasted text" },
  );
  const markdown = toMarkdownReport(data);

  assert.match(markdown, /^# Fact Check Report/);
  assert.match(markdown, /\*\*Source:\*\* Pasted text/);
  assert.match(markdown, /## 1\. FALSE \(88% confidence\)/);
  assert.match(markdown, /Claim one\./);
  assert.match(markdown, /Contradicted by the source\./);
  // A bare string source normalizes to {url, title: "Source 1"} (state.js's
  // normalizeSource) - the link text is the fallback title, not the raw URL.
  assert.match(markdown, /- \[Source 1\]\(https:\/\/example\.com\/a\)/);
});
