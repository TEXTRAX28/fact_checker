export const TERMINAL_STATES = new Set([
  "complete",
  "complete_no_claims",
  "partial",
  "failed",
  "cancelled",
]);

export const STAGES = Object.freeze([
  { id: "queued", label: "Queued" },
  { id: "reading_page", label: "Reading" },
  { id: "extracting_claims", label: "Claims" },
  { id: "searching", label: "Sources" },
  { id: "verifying", label: "Verdicts" },
]);

const STATE_ALIASES = Object.freeze({
  completed: "complete",
  done: "complete",
  error: "failed",
  canceled: "cancelled",
  claims_extracted: "searching",
  verdict: "verifying",
  warning: "verifying",
});

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null);
}

function asNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
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
    warning: String(firstDefined(result?.warning, result?.source_warning, "")),
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

  if (state === "complete" && claimCount === 0 && results.length === 0) {
    state = "complete_no_claims";
  }

  const rawErrors = firstDefined(raw.errors, raw.warnings, previous.errors, []);
  const errors = (Array.isArray(rawErrors) ? rawErrors : [rawErrors])
    .filter(Boolean)
    .map((error) => typeof error === "string" ? error : String(error.message || error.detail || error.code || "Unknown error"));

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
    cached: Boolean(firstDefined(raw.cached, raw.cache_hit, previous.cached, false)),
    startedAt: firstDefined(raw.started_at, raw.startedAt, previous.startedAt, null),
    completedAt: firstDefined(raw.completed_at, raw.completedAt, previous.completedAt, null),
  };
}

export function mergeEvent(snapshot, event = {}, eventType = "") {
  const current = normalizeSnapshot(snapshot || {});
  const nextSequence = asNumber(firstDefined(event.sequence, event.event_sequence), current.sequence);
  if (nextSequence && nextSequence <= current.sequence) return current;

  const rawType = String(firstDefined(event.type, event.event, eventType, current.state));
  const type = normalizeState(rawType, current.state);
  const next = {
    ...current,
    sequence: Math.max(current.sequence, nextSequence),
    state: ["complete", "failed", "cancelled", "partial"].includes(type) ? type : current.state,
    stage: type,
    claimCount: Math.max(current.claimCount, asNumber(firstDefined(event.claim_count, event.total), current.claimCount)),
    errors: event.message && type === "warning" ? [...current.errors, String(event.message)] : current.errors,
  };

  if (["queued", "reading_page", "extracting_claims", "searching", "verifying"].includes(type)) {
    next.state = type;
  }

  const result = firstDefined(
    event.result,
    event.verdict_data,
    rawType === "verdict" && event.claim ? event : null,
    rawType === "verdict" ? event.verdict : null,
  );
  if (result && typeof result === "object") {
    const normalized = normalizeResult(result, next.results.length);
    const results = [...next.results];
    const existingIndex = results.findIndex((item) => item.index === normalized.index);
    if (existingIndex >= 0) results[existingIndex] = normalized;
    else results.push(normalized);
    next.results = results;
    next.completedCount = results.length;
    next.state = "verifying";
  }

  return normalizeSnapshot(next, current);
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
  };
  return copy[state] || ["Check in progress", "Waiting for the latest status."];
}

export function compactSession(snapshot, job) {
  if (!snapshot?.checkId && !job?.checkId) return null;
  return {
    checkId: snapshot?.checkId || job.checkId,
    snapshotPath: job?.snapshotPath || null,
    eventsPath: job?.eventsPath || null,
    mode: job?.mode || "page",
    context: job?.context || null,
    state: snapshot?.state || "queued",
    stage: snapshot?.stage || "queued",
    sequence: snapshot?.sequence || 0,
    claimCount: snapshot?.claimCount || 0,
    completedCount: snapshot?.completedCount || 0,
    results: snapshot?.results || [],
    errors: snapshot?.errors || [],
    cached: Boolean(snapshot?.cached),
  };
}
