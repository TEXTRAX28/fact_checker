// Must cover every value in jobs.py's TERMINAL_STATUSES (after STATE_ALIASES
// normalization) - a status this set doesn't recognize as terminal means polling/
// SSE never stops, the Cancel button stays pointed at an already-finished job
// (a no-op there - jobs.py's cancel() checks job.terminal() first), and the status
// text never leaves the generic "Check in progress" fallback, even though the
// backend is genuinely done. Confirmed live: this is what a real provider timeout
// looked like before this fix - "timeout" is a real terminal status jobs.py sends,
// and it wasn't in this set.
export const TERMINAL_STATES = new Set([
  "complete",
  "complete_no_claims",
  "partial",
  "failed",
  "cancelled",
  "invalid_input",
  "no_evidence",
  "rate_limited",
  "timeout",
  "unreadable",
]);

export const STAGES = Object.freeze([
  { id: "queued", label: "Queued" },
  { id: "reading_page", label: "Reading" },
  { id: "extracting_claims", label: "Claims" },
  { id: "searching", label: "Sources" },
  { id: "verifying", label: "Verdicts" },
]);

// Every id a job can be actively working through, before it's terminal. Shared by
// mergeEvent (SSE) and normalizeSnapshot (polling) so both treat "in flight" the
// same way - see normalizeSnapshot's own comment for why that parity matters.
const IN_FLIGHT_STAGES = new Set(STAGES.map((stage) => stage.id));

const STATE_ALIASES = Object.freeze({
  completed: "complete",
  done: "complete",
  error: "failed",
  canceled: "cancelled",
  claims_extracted: "searching",
  verdict: "verifying",
  warning: "verifying",
  // jobs.py's real "no_claims" terminal status means the same thing this file
  // already had a synthesized "complete_no_claims" state for - reuse its copy
  // instead of duplicating it.
  no_claims: "complete_no_claims",
});

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null);
}

function asNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function normalizeUsage(value = {}, previous = {}) {
  const gemini = value?.gemini || value?.deepinfra || {};
  const tavily = value?.tavily || {};
  const previousGemini = previous?.gemini || previous?.deepinfra || {};
  const previousTavily = previous?.tavily || {};
  return {
    gemini: {
      requests: Math.max(0, asNumber(firstDefined(
        gemini.requests, previousGemini.requests, 0,
      ))),
      successfulRequests: Math.max(0, asNumber(firstDefined(
        gemini.successful_requests,
        gemini.successfulRequests,
        previousGemini.successfulRequests,
        0,
      ))),
      inputTokens: Math.max(0, asNumber(firstDefined(
        gemini.input_tokens, gemini.inputTokens, previousGemini.inputTokens, 0,
      ))),
      outputTokens: Math.max(0, asNumber(firstDefined(
        gemini.output_tokens, gemini.outputTokens, previousGemini.outputTokens, 0,
      ))),
      totalTokens: Math.max(0, asNumber(firstDefined(
        gemini.total_tokens, gemini.totalTokens, previousGemini.totalTokens, 0,
      ))),
      complete: Boolean(firstDefined(gemini.complete, previousGemini.complete, true)),
    },
    tavily: {
      searchAttempts: Math.max(0, asNumber(firstDefined(
        tavily.search_attempts, tavily.searchAttempts, previousTavily.searchAttempts, 0,
      ))),
      successfulSearches: Math.max(0, asNumber(firstDefined(
        tavily.successful_searches,
        tavily.successfulSearches,
        previousTavily.successfulSearches,
        0,
      ))),
      estimatedCredits: Math.max(0, asNumber(firstDefined(
        tavily.estimated_credits,
        tavily.estimatedCredits,
        previousTavily.estimatedCredits,
        0,
      ))),
      complete: Boolean(firstDefined(tavily.complete, previousTavily.complete, true)),
    },
    complete: Boolean(firstDefined(value?.complete, previous?.complete, true)),
  };
}

function normalizeState(value, fallback = "idle") {
  const state = String(value || fallback).toLowerCase().replaceAll("-", "_");
  return STATE_ALIASES[state] || state;
}

function normalizeSource(source, index) {
  if (typeof source === "string") {
    return { url: source, title: `Source ${index + 1}` };
  }
  if (!source || typeof source !== "object") return null;
  const url = firstDefined(source.url, source.href, source.link);
  if (!url) return null;
  return {
    url: String(url),
    title: String(firstDefined(source.title, source.name, source.domain, `Source ${index + 1}`)),
  };
}

function normalizeClaimError(error) {
  if (!error || typeof error !== "object") return null;
  const claimIndex = Number(error.claim_index);
  if (!Number.isInteger(claimIndex) || claimIndex < 0) return null;
  return {
    claimIndex,
    stage: String(error.stage || "pipeline"),
    code: String(error.code || "provider_error"),
    message: String(error.message || error.detail || "A provider request failed."),
  };
}

function normalizeClaimManifest(value) {
  if (!Array.isArray(value)) return [];
  return value.map((claim, fallbackIndex) => ({
    index: Math.max(0, asNumber(claim?.claim_index, fallbackIndex)),
    claim: String(firstDefined(claim?.claim, claim?.claim_text, "Claim unavailable")),
    speaker: String(firstDefined(claim?.speaker, "UNKNOWN")),
  }));
}

function normalizeClaimProgress(value, previous = {}) {
  const normalized = { ...previous };
  if (!value || typeof value !== "object" || Array.isArray(value)) return normalized;
  for (const [rawIndex, rawStage] of Object.entries(value)) {
    const index = Number(rawIndex);
    const stage = normalizeState(rawStage);
    if (Number.isInteger(index) && index >= 0
        && ["waiting", "searching", "verifying", "complete", "failed"].includes(stage)) {
      normalized[index] = stage;
    }
  }
  return normalized;
}

export function normalizeResult(result, fallbackIndex = 0) {
  const claimObject = result?.claim && typeof result.claim === "object" ? result.claim : null;
  const rawIndex = firstDefined(result?.claim_index, result?.index, result?.position, claimObject?.index);
  const sources = Array.isArray(result?.sources)
    ? result.sources.map(normalizeSource).filter(Boolean)
    : [];
  const rawVerdict = String(firstDefined(result?.verdict, result?.label, "UNVERIFIABLE")).toUpperCase();
  const verdict = ["TRUE", "FALSE", "UNVERIFIABLE"].includes(rawVerdict)
    ? rawVerdict
    : "UNVERIFIABLE";

  return {
    index: Math.max(0, asNumber(rawIndex, fallbackIndex)),
    claim: String(firstDefined(result?.claim_text, claimObject?.text, result?.claim, "Claim unavailable")),
    verdict,
    confidence: Math.min(100, Math.max(0, asNumber(result?.confidence, 0))),
    explanation: String(firstDefined(result?.explanation, result?.reasoning, "No explanation was provided.")),
    sources,
  };
}

export function normalizeSnapshot(raw = {}, previous = {}) {
  const progress = raw.progress && typeof raw.progress === "object" ? raw.progress : {};
  const counts = raw.counts && typeof raw.counts === "object" ? raw.counts : {};
  const rawResults = firstDefined(raw.results, raw.partial_verdicts, raw.verdicts, previous.results, []);
  const results = Array.isArray(rawResults)
    ? rawResults.map((result, index) => normalizeResult(result, index))
    : [];
  const claimCount = Math.max(0, asNumber(firstDefined(
    raw.claim_count,
    raw.claimCount,
    progress.claim_count,
    counts.claims,
    previous.claimCount,
    results.length,
  )));
  let state = normalizeState(firstDefined(raw.state, raw.status, previous.state), "idle");
  const stage = normalizeState(firstDefined(raw.stage, progress.stage, raw.event, previous.stage, state), state);

  // The backend's job status is only ever the coarse "running" for the entire time a
  // check is active - jobs.py never sets a fine-grained top-level status like
  // "verifying". mergeEvent (SSE) already refines this per-event; snapshot polling
  // (this function) has no equivalent unless it's done here too. Without this, the
  // status text gets stuck on the generic "Check in progress" fallback any time the
  // panel is relying on polling instead of live SSE (e.g. while the backend indicator
  // shows "Recovering") - confirmed live, not theoretical.
  if (state === "running" && IN_FLIGHT_STAGES.has(stage)) {
    state = stage;
  }

  if (state === "complete" && claimCount === 0 && results.length === 0) {
    state = "complete_no_claims";
  }

  const fallbackErrors = [
    ...(previous.errors || []),
    ...(previous.claimErrors || []).map((error) => ({
      claim_index: error.claimIndex,
      stage: error.stage,
      code: error.code,
      message: error.message,
    })),
  ];
  const primaryErrors = firstDefined(raw.errors, raw.warnings);
  const rawErrors = primaryErrors === undefined
    ? fallbackErrors
    : [
        ...(Array.isArray(primaryErrors) ? primaryErrors : [primaryErrors]),
        ...(raw.claimErrors || []).map((error) => ({
          claim_index: error.claimIndex,
          stage: error.stage,
          code: error.code,
          message: error.message,
        })),
      ];
  const errorValues = (Array.isArray(rawErrors) ? rawErrors : [rawErrors]).filter(Boolean);
  const claimErrors = errorValues.map(normalizeClaimError).filter(Boolean);
  const errors = errorValues
    .filter((error) => normalizeClaimError(error) === null)
    .map((error) => typeof error === "string"
      ? error
      : String(error.message || error.detail || error.code || "Unknown error"));
  const claimManifest = normalizeClaimManifest(firstDefined(
    raw.claim_manifest,
    raw.claimManifest,
    previous.claimManifest,
    [],
  ));
  const claimProgress = normalizeClaimProgress(
    firstDefined(raw.claim_progress, raw.claimProgress, {}),
    previous.claimProgress || {},
  );
  const rawRetryingIndex = raw.retrying_claim_index !== undefined
    ? raw.retrying_claim_index
    : firstDefined(raw.retryingClaimIndex, previous.retryingClaimIndex, null);
  const retryingClaimIndex = rawRetryingIndex !== null
    && rawRetryingIndex !== ""
    && Number.isInteger(Number(rawRetryingIndex))
    ? Number(rawRetryingIndex)
    : null;

  return {
    checkId: String(firstDefined(raw.check_id, raw.checkId, previous.checkId, "")),
    state,
    stage,
    sequence: Math.max(0, asNumber(firstDefined(raw.sequence, raw.event_sequence, previous.sequence), 0)),
    claimCount,
    completedCount: Math.max(results.length, asNumber(firstDefined(
      raw.completed_result_count,
      raw.result_count,
      raw.completedCount,
      progress.completed,
      counts.completed,
      previous.completedCount,
      results.length,
    ))),
    results,
    errors,
    claimErrors,
    claimManifest,
    claimProgress,
    retryingClaimIndex,
    retryAttempts: { ...(previous.retryAttempts || {}), ...(raw.retry_attempts || {}) },
    claimRetryLimit: Math.max(1, asNumber(firstDefined(
      raw.claim_retry_limit, raw.claimRetryLimit, previous.claimRetryLimit, 2,
    ), 2)),
    usage: normalizeUsage(
      firstDefined(raw.usage, raw.outcome?.usage, previous.usage, {}),
      previous.usage || {},
    ),
    cached: Boolean(firstDefined(raw.cached, raw.cache_hit, previous.cached, false)),
    startedAt: firstDefined(raw.started_at, raw.startedAt, previous.startedAt, null),
    completedAt: firstDefined(raw.completed_at, raw.completedAt, previous.completedAt, null),
  };
}

export function snapshotBelongsToJob(raw, checkId) {
  if (!raw || typeof raw !== "object" || !checkId) return false;
  const responseId = firstDefined(raw.id, raw.check_id, raw.checkId);
  return responseId !== undefined && responseId !== null
    && String(responseId) === String(checkId);
}

export function snapshotIsStale(raw, current = {}) {
  if (!raw || typeof raw !== "object") return false;
  const rawSequence = firstDefined(raw.sequence, raw.event_sequence);
  if (rawSequence === undefined || rawSequence === null) return false;
  return asNumber(rawSequence, 0) < asNumber(current.sequence, 0);
}

// The backend's SSE stream (jobs.py::JobManager._append_event) only ever names an
// event one of these five ways - "status" ({status}), "claims" (the sanitized
// extracted claim list), "progress" (whatever
// on_progress emitted, e.g. {stage, state, claim_count?}), "result" (the verdict
// dict itself, not nested under a "result" key), "terminal" ({status, outcome}).
// Each needs different unpacking; there is no single "the event name is the stage"
// shortcut, unlike an earlier draft of this vocabulary this code was written against.
export function mergeEvent(snapshot, data = {}, eventType = "") {
  const current = normalizeSnapshot(snapshot || {});
  const sequence = asNumber(firstDefined(data.sequence, data.event_sequence), current.sequence);
  if (sequence && sequence <= current.sequence) return current;
  const next = { ...current, sequence: Math.max(current.sequence, sequence) };

  if (eventType === "status") {
    const status = normalizeState(data.status, current.state);
    if (IN_FLIGHT_STAGES.has(status) || status === "cancelling") next.state = status;
    return normalizeSnapshot(next, current);
  }

  if (eventType === "claims") {
    const claims = Array.isArray(data.claims) ? data.claims : [];
    next.claimManifest = normalizeClaimManifest(claims);
    next.claimCount = Math.max(current.claimCount, asNumber(data.claim_count, claims.length));
    next.claimProgress = Object.fromEntries(
      next.claimManifest.map((claim) => [claim.index, "waiting"]),
    );
    return normalizeSnapshot(next, current);
  }

  if (eventType === "progress") {
    const stage = normalizeState(data.stage, current.stage);
    next.stage = stage;
    if (IN_FLIGHT_STAGES.has(stage)) next.state = stage;
    next.claimCount = Math.max(current.claimCount, asNumber(data.claim_count, current.claimCount));
    const claimIndex = Number(data.claim_index);
    if (Number.isInteger(claimIndex) && claimIndex >= 0
        && ["searching", "verifying"].includes(stage)) {
      next.claimProgress = { ...current.claimProgress, [claimIndex]: stage };
    }
    if (data.message && stage === "warning") next.errors = [...current.errors, String(data.message)];
    return normalizeSnapshot(next, current);
  }

  if (eventType === "result") {
    // data IS the verdict dict - not nested under data.result/data.verdict_data.
    const normalized = normalizeResult(data, next.results.length);
    const results = [...next.results];
    const existingIndex = results.findIndex((item) => item.index === normalized.index);
    if (existingIndex >= 0) results[existingIndex] = normalized;
    else results.push(normalized);
    next.results = results;
    next.completedCount = results.length;
    next.claimProgress = { ...current.claimProgress, [normalized.index]: "complete" };
    next.state = "verifying";
    return normalizeSnapshot(next, current);
  }

  if (eventType === "usage") {
    next.usage = normalizeUsage(data, current.usage);
    return normalizeSnapshot(next, current);
  }

  if (eventType === "terminal") {
    const outcome = data.outcome && typeof data.outcome === "object" ? data.outcome : {};
    return normalizeSnapshot({
      sequence: next.sequence,
      status: data.status,
      results: outcome.results,
      errors: outcome.errors,
      claim_count: outcome.claim_count,
      usage: outcome.usage,
      retrying_claim_index: null,
    }, current);
  }

  // Unrecognized event name - leave state alone rather than guess.
  return next;
}

export function isTerminalState(state) {
  return TERMINAL_STATES.has(normalizeState(state));
}

export function stageIndex(stage) {
  const normalized = normalizeState(stage);
  if (["complete", "complete_no_claims", "partial"].includes(normalized)) return STAGES.length;
  return STAGES.findIndex((item) => item.id === normalized);
}

export function statusCopy(snapshot) {
  const state = normalizeState(snapshot?.state);
  const count = snapshot?.claimCount || 0;
  const complete = snapshot?.completedCount || snapshot?.results?.length || 0;
  const copy = {
    idle: ["Ready to check", "Choose a source and start a check."],
    collecting_page: ["Reading current page", "Extracting the article text from this tab."],
    queued: ["Check queued", "Waiting for the local checker."],
    reading_page: ["Reading source", "Preparing the source text."],
    extracting_claims: ["Finding claims", "Identifying factual statements to verify."],
    searching: ["Finding sources", count ? `Searching evidence for ${count} claims.` : "Searching for reliable evidence."],
    verifying: ["Checking evidence", count ? `Verified ${complete} of ${count} claims.` : "Verdicts will appear as they finish."],
    complete: ["Check complete", `${complete} ${complete === 1 ? "claim" : "claims"} checked.`],
    complete_no_claims: ["No checkable claims", "The text appears to be opinion, prediction, or too vague to verify."],
    partial: ["Partially complete", `${complete} of ${count || complete} claims were checked.`],
    failed: ["Check failed", "The backend could not finish this check."],
    cancelling: ["Cancelling…", "Waiting for the current step to stop before finishing."],
    cancelled: ["Check cancelled", "No additional claims will be processed."],
    no_evidence: ["No evidence found", "Claims were found, but no sufficiently reliable evidence was available."],
    rate_limited: ["Rate limited", "A provider's rate limit was reached. Try again shortly."],
    timeout: ["Check timed out", "A provider took too long to respond."],
    unreadable: ["Page unreadable", "The page could not be read. Try pasting the article text instead."],
    invalid_input: ["Invalid input", "That input could not be checked."],
  };
  return copy[state] || ["Check in progress", "Waiting for the latest status."];
}

export function compactSession(snapshot, job) {
  if (!snapshot?.checkId && !job?.checkId) return null;
  return {
    checkId: snapshot?.checkId || job.checkId,
    snapshotPath: job?.snapshotPath || null,
    eventsPath: job?.eventsPath || null,
    jobToken: job?.jobToken || null,
    mode: job?.mode || "page",
    context: job?.context || null,
    state: snapshot?.state || "queued",
    stage: snapshot?.stage || "queued",
    sequence: snapshot?.sequence || 0,
    claimCount: snapshot?.claimCount || 0,
    completedCount: snapshot?.completedCount || 0,
    results: snapshot?.results || [],
    errors: snapshot?.errors || [],
    claimErrors: snapshot?.claimErrors || [],
    claimManifest: snapshot?.claimManifest || [],
    claimProgress: snapshot?.claimProgress || {},
    retryingClaimIndex: snapshot?.retryingClaimIndex ?? null,
    retryAttempts: snapshot?.retryAttempts || {},
    claimRetryLimit: snapshot?.claimRetryLimit || 2,
    usage: snapshot?.usage || normalizeUsage(),
    cached: Boolean(snapshot?.cached),
  };
}

export function claimProgressCopy(stage) {
  const copy = {
    waiting: ["DETECTED", "Not verified yet. Waiting for source search."],
    searching: ["FINDING SOURCES", "Searching for relevant, reliable evidence."],
    verifying: ["CHECKING EVIDENCE", "Comparing this claim against accepted evidence."],
  };
  return copy[normalizeState(stage)] || copy.waiting;
}

// Pure data-shaping for the Export buttons, kept here (not in sidepanel.js) so it's
// testable without a DOM/chrome.* shim, matching how the rest of this file works.
export function buildExportData(snapshot, source = {}) {
  const results = [...(snapshot?.results || [])].sort((left, right) => left.index - right.index);
  const resultByIndex = new Map(results.map((result) => [result.index, result]));
  const manifestByIndex = new Map(
    (snapshot?.claimManifest || []).map((claim) => [claim.index, claim]),
  );
  const errorByIndex = new Map(
    (snapshot?.claimErrors || []).map((error) => [error.claimIndex, error]),
  );
  const claimCount = Math.max(
    snapshot?.claimCount || 0,
    results.length,
    ...[...manifestByIndex.keys(), ...resultByIndex.keys()].map((index) => index + 1),
  );
  const claims = Array.from({ length: claimCount }, (_, index) => {
    const result = resultByIndex.get(index);
    if (result) {
      return {
        index,
        status: "completed",
        verdict: result.verdict,
        confidence: result.confidence,
        claim: result.claim,
        explanation: result.explanation,
        sources: result.sources || [],
      };
    }

    const manifest = manifestByIndex.get(index);
    const error = errorByIndex.get(index);
    return {
      index,
      status: error ? "failed" : "incomplete",
      claim: manifest?.claim || "Claim unavailable",
      speaker: manifest?.speaker || "UNKNOWN",
      error: error ? {
        stage: error.stage,
        code: error.code,
        message: error.message,
      } : null,
      sources: [],
    };
  });
  return {
    exportedAt: new Date().toISOString(),
    source: {
      mode: source.mode || null,
      title: source.title || null,
      url: source.url || null,
    },
    summary: {
      status: snapshot?.state || "idle",
      claimCount,
      completedCount: results.length,
      failedCount: claims.filter((claim) => claim.status === "failed").length,
      incompleteCount: claims.filter((claim) => claim.status === "incomplete").length,
      usage: normalizeUsage(snapshot?.usage),
    },
    errors: [
      ...(snapshot?.errors || []).map((message) => ({ message })),
      ...(snapshot?.claimErrors || []).map((error) => ({
        claimIndex: error.claimIndex,
        stage: error.stage,
        code: error.code,
        message: error.message,
      })),
    ],
    claims,
  };
}

export function toMarkdownReport(data) {
  const lines = ["# Fact Check Report", ""];
  if (data.source.title) lines.push(`**Source:** ${data.source.title}`);
  if (data.source.url) lines.push(`**URL:** ${data.source.url}`);
  lines.push(`**Exported:** ${data.exportedAt}`);
  lines.push(`**Claims checked:** ${data.summary.completedCount} of ${data.summary.claimCount}`);
  lines.push(`**Gemini tokens:** ${data.summary.usage.gemini.totalTokens}`);
  lines.push(`**Tavily estimated credits:** ${data.summary.usage.tavily.estimatedCredits}`);
  lines.push("");

  data.claims.forEach((claim, position) => {
    const label = Number.isFinite(claim.index) ? claim.index + 1 : position + 1;
    if (claim.status !== "completed") {
      const state = claim.status === "failed" ? "FAILED" : "NOT COMPLETED";
      lines.push(`## ${label}. ${state}`, "", claim.claim || "Claim unavailable", "");
      if (claim.error?.message) lines.push(`Error: ${claim.error.message}`, "");
      return;
    }
    const confidence = claim.confidence ? ` (${Math.round(claim.confidence)}% confidence)` : "";
    lines.push(`## ${label}. ${claim.verdict}${confidence}`, "", claim.claim || "", "");
    if (claim.explanation) lines.push(claim.explanation, "");
    if (claim.sources?.length) {
      lines.push("Sources:");
      for (const source of claim.sources) {
        lines.push(`- [${source.title || source.url}](${source.url})`);
      }
      lines.push("");
    }
  });

  return lines.join("\n");
}
