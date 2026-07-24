import {
  ApiError,
  apiUrl,
  buildCheckPayload,
  cancelCheck,
  createCheck,
  getCheckSnapshot,
  getHealth,
  normalizeProviderCredentials,
  retryCheckClaim,
  streamCheckEvents,
} from "./api.js";
import { CLIENT_LIMITS } from "./config.js";
import { createIcon, hydrateIcons } from "./icons.js";
import { hasNewPageSinceCheck, requestFreshPageCapture } from "./page-capture.js";
import {
  STAGES,
  buildExportData,
  claimProgressCopy,
  compactSession,
  isTerminalState,
  mergeEvent,
  normalizeSnapshot,
  snapshotBelongsToJob,
  snapshotIsStale,
  stageIndex,
  statusCopy,
  toMarkdownReport,
} from "./state.js";

const STORAGE = Object.freeze({
  activeJob: "fc:activeJob",
  activeTabId: "fc:activeTabId",
  drafts: "fc:drafts",
  credentials: "fc:providerCredentials",
  installId: "fc:installId",
});
const elements = {
  backend: document.querySelector("#backend-status"),
  backendLabel: document.querySelector(".backend-label"),
  credentialsAction: document.querySelector("#credentials-action"),
  credentialsDialog: document.querySelector("#credentials-dialog"),
  credentialsForm: document.querySelector("#credentials-form"),
  credentialsClose: document.querySelector("#credentials-close"),
  credentialsClear: document.querySelector("#credentials-clear"),
  credentialsError: document.querySelector("#credentials-error"),
  geminiKey: document.querySelector("#gemini-key"),
  tavilyKey: document.querySelector("#tavily-key"),
  modeControl: document.querySelector("#mode-control"),
  inputPanel: document.querySelector("#input-panel"),
  inputError: document.querySelector("#input-error"),
  primary: document.querySelector("#primary-action"),
  primaryIcon: document.querySelector("#primary-action .button-icon"),
  primaryLabel: document.querySelector("#primary-action .button-label"),
  progressSection: document.querySelector("#progress-section"),
  progressCount: document.querySelector("#progress-count"),
  statusIcon: document.querySelector("#status-icon"),
  statusTitle: document.querySelector("#status-title"),
  statusDetail: document.querySelector("#status-detail"),
  stageList: document.querySelector("#stage-list"),
  jobErrors: document.querySelector("#job-errors"),
  usageSection: document.querySelector("#usage-section"),
  usageStatus: document.querySelector("#usage-status"),
  usageInputTokens: document.querySelector("#usage-input-tokens"),
  usageOutputTokens: document.querySelector("#usage-output-tokens"),
  usageTotalTokens: document.querySelector("#usage-total-tokens"),
  usageTavilyCredits: document.querySelector("#usage-tavily-credits"),
  resultsSection: document.querySelector("#results-section"),
  resultsCount: document.querySelector("#results-count"),
  resultsContext: document.querySelector("#results-context"),
  resultsList: document.querySelector("#results-list"),
  exportActions: document.querySelector("#export-actions"),
  liveStatus: document.querySelector("#live-status"),
  privacyLink: document.querySelector("#privacy-link"),
};

const app = {
  mode: "page",
  drafts: { url: "", text: "" },
  credentials: { geminiKey: "", tavilyKey: "" },
  installId: "",
  page: null,
  activeTabId: null,
  backend: "checking",
  snapshot: normalizeSnapshot({ state: "idle" }),
  job: null,
  eventController: null,
  reconnectTimer: null,
  pollTimer: null,
  snapshotTimer: null,
  refreshInFlight: null,
  cancelPending: false,
  inputMessage: "",
  lastAnnouncement: "",
};

hydrateIcons();
bindEvents();
await initialize();

async function initialize() {
  elements.privacyLink.href = apiUrl("/privacy?v=20260724-1");
  await chrome.storage.session.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  const saved = await chrome.storage.session.get([
    STORAGE.activeJob,
    STORAGE.activeTabId,
    STORAGE.drafts,
    STORAGE.credentials,
  ]);
  const local = await chrome.storage.local.get(STORAGE.installId);

  app.activeTabId = saved[STORAGE.activeTabId] ?? null;
  app.drafts = { ...app.drafts, ...(saved[STORAGE.drafts] || {}) };
  app.credentials = { ...app.credentials, ...(saved[STORAGE.credentials] || {}) };
  app.installId = String(local[STORAGE.installId] || "");
  if (!app.installId) {
    app.installId = crypto.randomUUID();
    await chrome.storage.local.set({ [STORAGE.installId]: app.installId });
  }

  const savedJob = saved[STORAGE.activeJob];
  if (savedJob?.checkId) {
    app.mode = savedJob.mode || app.drafts.mode || "page";
    app.job = {
      checkId: savedJob.checkId,
      snapshotPath: savedJob.snapshotPath,
      eventsPath: savedJob.eventsPath,
      jobToken: savedJob.jobToken,
      mode: savedJob.mode,
      context: savedJob.context,
    };
    app.snapshot = normalizeSnapshot({}, savedJob);
  } else if (app.drafts.mode) {
    app.mode = app.drafts.mode;
  }

  await loadPageData();
  renderInput();
  render();
  await checkBackend();

  if (app.job) {
    await refreshSnapshot();
    connectTransport();
  }
}

function bindEvents() {
  elements.modeControl.addEventListener("click", (event) => {
    const button = event.target.closest("[data-mode]");
    if (!button || isRunning()) return;
    setMode(button.dataset.mode);
  });
  elements.modeControl.addEventListener("keydown", (event) => {
    if (isRunning() || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const buttons = [...elements.modeControl.querySelectorAll("[data-mode]")];
    const current = buttons.indexOf(event.target.closest("[data-mode]"));
    if (current < 0) return;
    let next = current;
    if (event.key === "ArrowLeft") next = (current - 1 + buttons.length) % buttons.length;
    if (event.key === "ArrowRight") next = (current + 1) % buttons.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = buttons.length - 1;
    event.preventDefault();
    buttons[next].focus();
    setMode(buttons[next].dataset.mode);
  });

  elements.primary.addEventListener("click", () => {
    if (isRunning()) void requestCancellation();
    else void startCheck(isTerminalState(app.snapshot.state));
  });

  elements.backend.addEventListener("click", () => void checkBackend());
  elements.credentialsAction.addEventListener("click", openCredentialsDialog);
  elements.credentialsClose.addEventListener("click", () => elements.credentialsDialog.close());
  elements.credentialsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void saveCredentials();
  });
  elements.credentialsClear.addEventListener("click", () => void clearCredentials());
  elements.credentialsDialog.addEventListener("cancel", () => {
    elements.credentialsError.hidden = true;
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (message?.tabId === app.activeTabId && message.type?.startsWith("PAGE_CAPTURE")) {
      void loadPageData().then(() => {
        if (app.mode === "page") renderInput();
        render();
      });
    }
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "session") return;
    if (changes[STORAGE.activeTabId]) {
      app.activeTabId = changes[STORAGE.activeTabId].newValue ?? null;
      void loadPageData().then(() => {
        if (app.mode === "page") renderInput();
        render();
      });
      return;
    }

    const pageKey = app.activeTabId === null ? null : `fc:page:${app.activeTabId}`;
    if (pageKey && changes[pageKey]) {
      app.page = changes[pageKey].newValue || null;
      if (app.mode === "page") renderInput();
      render();
    }
  });

  window.addEventListener("pagehide", stopTransport);
  setInterval(() => void checkBackend({ quiet: true }), 30_000);
}

function hasProviderCredentials() {
  try {
    normalizeProviderCredentials(app.credentials);
    return true;
  } catch {
    return false;
  }
}

function openCredentialsDialog() {
  elements.geminiKey.value = app.credentials.geminiKey || "";
  elements.tavilyKey.value = app.credentials.tavilyKey || "";
  elements.credentialsError.hidden = true;
  elements.credentialsDialog.showModal();
  elements.geminiKey.focus();
}

async function saveCredentials() {
  try {
    app.credentials = normalizeProviderCredentials({
      geminiKey: elements.geminiKey.value,
      tavilyKey: elements.tavilyKey.value,
    });
    await chrome.storage.session.set({ [STORAGE.credentials]: app.credentials });
    elements.credentialsError.hidden = true;
    elements.credentialsDialog.close();
    app.inputMessage = "";
    render();
  } catch (error) {
    elements.credentialsError.textContent = error?.message || "Enter valid provider keys.";
    elements.credentialsError.hidden = false;
  }
}

async function clearCredentials() {
  app.credentials = { geminiKey: "", tavilyKey: "" };
  elements.geminiKey.value = "";
  elements.tavilyKey.value = "";
  await chrome.storage.session.remove(STORAGE.credentials);
  elements.credentialsDialog.close();
  render();
}

async function requestPageRefresh() {
  app.inputMessage = "";
  renderInputMessage();
  const response = await requestFreshPageCapture(chrome);
  if (!response.ok) {
    app.inputMessage = response.message;
    renderInputMessage();
  }
}

async function loadPageData() {
  if (app.activeTabId === null) {
    app.page = null;
    return;
  }
  const key = `fc:page:${app.activeTabId}`;
  const stored = await chrome.storage.session.get(key);
  app.page = stored[key] || null;
}

async function resetActiveJob() {
  // Shared by every case where the active job can no longer be trusted: the
  // server has no record of it (404 - almost always because api.py restarted
  // and its in-memory JobManager lost every job it knew about). Page changes do
  // not call this: the previous result stays visible until a new check starts.
  stopTransport();
  app.job = null;
  app.snapshot = normalizeSnapshot({ state: "idle" });
  await chrome.storage.session.remove(STORAGE.activeJob);
}

function isJobNotFound(error) {
  return error instanceof ApiError && error.status === 404;
}

function setMode(mode) {
  if (!["page", "url", "text"].includes(mode)) return;
  app.mode = mode;
  app.inputMessage = "";
  app.drafts.mode = mode;
  void saveDrafts();
  renderInput();
  render();
}

function renderInput() {
  elements.inputPanel.replaceChildren();
  for (const button of elements.modeControl.querySelectorAll("[data-mode]")) {
    const selected = button.dataset.mode === app.mode;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
  elements.inputPanel.setAttribute("aria-labelledby", `mode-${app.mode}`);

  if (app.mode === "page") renderPageInput();
  if (app.mode === "url") renderUrlInput();
  if (app.mode === "text") renderTextInput();
}

function renderPageInput() {
  const status = app.page?.status || "missing";
  const preview = node("div", "page-preview");
  preview.dataset.status = status;
  const icon = node("div", "page-preview-icon");
  icon.append(createIcon(status === "failed" || status === "missing" ? "triangle-alert" : "file-text", 18));
  const content = node("div");

  if (status === "ready") {
    content.append(
      node("h2", "", app.page.title || "Untitled page"),
      node("p", "", pageMeta(app.page)),
    );
    const actions = node("div", "page-actions");
    actions.append(actionButton("Refresh", "rotate-cw", requestPageRefresh));
    preview.append(icon, content, actions);
    elements.inputPanel.append(preview);
    return;
  } else if (status === "collecting") {
    content.append(
      node("h2", "", "Reading current page"),
      node("p", "", "Extracting readable text from this tab."),
    );
  } else {
    content.append(
      node("h2", "", "Page unavailable"),
      node("p", "", app.page?.error || "Open the extension from a normal web page to check it."),
    );
    const actions = node("div", "page-actions");
    if (/^https?:\/\//i.test(app.page?.url || "")) {
      actions.append(actionButton("Try URL fetch", "link", () => {
        app.drafts.url = app.page.url;
        setMode("url");
      }));
    }
    actions.append(actionButton("Paste text", "align-left", () => setMode("text")));
    preview.append(icon, content, actions);
    elements.inputPanel.append(preview);
    return;
  }

  preview.append(icon, content);
  elements.inputPanel.append(preview);
}

function renderUrlInput() {
  const label = node("label", "field-label", "Article URL");
  label.htmlFor = "url-input";
  const input = node("input", "text-input");
  input.id = "url-input";
  input.type = "url";
  input.inputMode = "url";
  input.autocomplete = "url";
  input.placeholder = "https://example.com/article";
  input.value = app.drafts.url || "";
  input.setAttribute("aria-describedby", "url-hint");
  input.addEventListener("input", () => {
    app.drafts.url = input.value;
    app.inputMessage = "";
    void saveDrafts();
    renderAction();
    renderInputMessage();
  });
  const hint = node("p", "field-meta", "Only http and https URLs");
  hint.id = "url-hint";
  elements.inputPanel.append(label, input, hint);
}

function renderTextInput() {
  const label = node("label", "field-label", "Text to check");
  label.htmlFor = "text-input";
  const area = node("textarea", "text-area");
  area.id = "text-input";
  area.placeholder = "Paste an article, statement, or paragraph";
  area.maxLength = CLIENT_LIMITS.textCharacters;
  area.value = app.drafts.text || "";
  area.setAttribute("aria-describedby", "text-count");
  const meta = node("div", "field-meta");
  const minimum = node("span", "", `Minimum ${CLIENT_LIMITS.minimumTextCharacters} characters`);
  const count = node("span", "", `${area.value.length.toLocaleString()} / ${CLIENT_LIMITS.textCharacters.toLocaleString()}`);
  count.id = "text-count";
  meta.append(minimum, count);
  area.addEventListener("input", () => {
    app.drafts.text = area.value;
    count.textContent = `${area.value.length.toLocaleString()} / ${CLIENT_LIMITS.textCharacters.toLocaleString()}`;
    app.inputMessage = "";
    void saveDrafts();
    renderAction();
    renderInputMessage();
  });
  elements.inputPanel.append(label, area, meta);
}

function render() {
  renderInputMessage();
  renderAction();
  renderBackend();
  renderProgress();
  renderUsage();
  renderResults();
}

function renderInputMessage() {
  elements.inputError.hidden = !app.inputMessage;
  elements.inputError.textContent = app.inputMessage;
}

function renderAction() {
  const running = isRunning();
  const terminal = Boolean(app.job) && isTerminalState(app.snapshot.state);
  const newPage = hasNewPageSinceCheck(app.job, app.page, app.mode, terminal);
  // app.cancelPending only covers the DELETE request itself (a fraction of a
  // second); the job can stay in "cancelling" much longer than that if a
  // provider call was already in flight when cancel was requested (best-effort
  // cancellation - see chrome-extension.md). Both must disable/relabel the button,
  // or a slow cancellation looks identical to "Cancel check" doing nothing.
  const cancelling = app.cancelPending || app.snapshot.state === "cancelling";
  let label = terminal ? (newPage ? "Check new page" : "Recheck") : "Check claims";
  let iconName = terminal ? "rotate-cw" : "search";
  let action = terminal ? "recheck" : "check";

  if (running) {
    label = cancelling ? "Cancelling" : "Cancel check";
    iconName = "square";
    action = "cancel";
  }

  elements.primary.dataset.action = action;
  elements.primary.title = !running && !hasProviderCredentials()
    ? "Add your Gemini and Tavily API keys first."
    : "";
  elements.primaryLabel.textContent = label;
  elements.primaryIcon.replaceChildren(createIcon(iconName, 18));
  elements.primary.disabled = cancelling || (
    !running && (
      app.backend !== "online"
      || !hasUsableInput()
      || !hasProviderCredentials()
    )
  );

  for (const button of elements.modeControl.querySelectorAll("[data-mode]")) {
    button.disabled = running;
  }
}

function renderBackend() {
  const labels = { checking: "Connecting", online: "Online", offline: "Offline", recovering: "Recovering" };
  elements.backend.dataset.status = app.backend;
  elements.backendLabel.textContent = labels[app.backend] || "Unknown";
  elements.backend.setAttribute("aria-label", `Backend ${labels[app.backend] || "unknown"}. Activate to retry.`);
  const credentialsReady = hasProviderCredentials();
  elements.credentialsAction.dataset.status = credentialsReady ? "ready" : "missing";
  elements.credentialsAction.setAttribute(
    "aria-label",
    credentialsReady ? "Provider API keys configured" : "Provider API keys required",
  );
}

function renderUsage() {
  const usage = app.snapshot.usage;
  const gemini = usage?.gemini || {};
  const tavily = usage?.tavily || {};
  const hasUsage = Number(gemini.requests || 0) > 0
    || Number(tavily.searchAttempts || 0) > 0;
  elements.usageSection.hidden = !app.job || !hasUsage;
  if (elements.usageSection.hidden) return;
  elements.usageInputTokens.textContent = Number(gemini.inputTokens || 0).toLocaleString();
  elements.usageOutputTokens.textContent = Number(gemini.outputTokens || 0).toLocaleString();
  elements.usageTotalTokens.textContent = Number(gemini.totalTokens || 0).toLocaleString();
  elements.usageTavilyCredits.textContent = Number(tavily.estimatedCredits || 0).toLocaleString();
  elements.usageStatus.textContent = usage.complete ? "Reported" : "Partial";
}

function renderProgress() {
  const hasJob = Boolean(app.job);
  elements.progressSection.hidden = !hasJob;
  if (!hasJob) return;

  const [title, detail] = statusCopy(app.snapshot);
  elements.statusTitle.textContent = title;
  elements.statusDetail.textContent = detail;
  elements.progressCount.textContent = app.snapshot.claimCount
    ? `${app.snapshot.completedCount} / ${app.snapshot.claimCount}`
    : "";

  const visual = statusVisual(app.snapshot.state);
  elements.statusIcon.dataset.tone = visual.tone;
  elements.statusIcon.dataset.icon = visual.icon;
  elements.statusIcon.replaceChildren(createIcon(visual.icon, 18));

  const currentIndex = stageIndex(app.snapshot.stage);
  const terminalSuccess = ["complete", "complete_no_claims", "partial"].includes(app.snapshot.state);
  elements.stageList.replaceChildren(...STAGES.map((stage, index) => {
    const item = node("li", "stage-item", stage.label);
    item.dataset.status = terminalSuccess || index < currentIndex
      ? "complete"
      : index === currentIndex ? "active" : "pending";
    return item;
  }));

  elements.jobErrors.hidden = app.snapshot.errors.length === 0;
  elements.jobErrors.replaceChildren(...app.snapshot.errors.map((message) => node("p", "", message)));

  const announcement = `${title}. ${detail}`;
  if (announcement !== app.lastAnnouncement) {
    elements.liveStatus.textContent = announcement;
    app.lastAnnouncement = announcement;
  }
}

function renderResults() {
  const results = app.snapshot.results || [];
  const slotCount = Math.max(app.snapshot.claimCount || 0, results.length);
  const terminal = isTerminalState(app.snapshot.state);
  const showEmpty = terminal && slotCount === 0;
  elements.resultsSection.hidden = !slotCount && !showEmpty;
  if (elements.resultsSection.hidden) return;

  elements.resultsCount.textContent = slotCount
    ? `${results.length} of ${slotCount}`
    : "";
  const checkedTitle = app.job?.mode === "page" ? String(app.job.context?.title || "").trim() : "";
  elements.resultsContext.hidden = !checkedTitle;
  elements.resultsContext.textContent = checkedTitle ? `Results for: ${checkedTitle}` : "";

  elements.exportActions.hidden = results.length === 0;
  if (results.length > 0) {
    elements.exportActions.replaceChildren(
      actionButton("Export JSON", "download", () => exportResults("json")),
      actionButton("Export Markdown", "download", () => exportResults("markdown")),
    );
  }
  elements.resultsList.replaceChildren();

  if (showEmpty) {
    elements.resultsList.append(renderEmptyResult());
    return;
  }

  // claim_index is zero-based throughout fact_checker.py and jobs.py. Inferring
  // one-based indices from a partial result set misplaces claim 1 when claim 0
  // is the one that failed.
  const resultBySlot = new Map(results.map((result, fallbackIndex) => [
    Number.isInteger(result.index) ? result.index : fallbackIndex,
    result,
  ]));
  const claimBySlot = new Map(
    (app.snapshot.claimManifest || []).map((claim) => [claim.index, claim]),
  );
  const errorBySlot = new Map(
    (app.snapshot.claimErrors || []).map((error) => [error.claimIndex, error]),
  );

  for (let index = 0; index < slotCount; index += 1) {
    let card;
    if (resultBySlot.has(index)) {
      card = renderResult(resultBySlot.get(index));
    } else if (claimBySlot.has(index) && (terminal || errorBySlot.has(index))) {
      card = renderFailedClaim(index, claimBySlot.get(index), errorBySlot.get(index));
    } else if (claimBySlot.has(index)) {
      card = renderPendingClaim(
        index,
        claimBySlot.get(index),
        app.snapshot.claimProgress?.[index] || "waiting",
      );
    } else {
      card = renderPlaceholder(index);
    }
    elements.resultsList.append(card);
  }
}

function renderResult(result) {
  const card = node("article", "result-card");
  card.dataset.verdict = result.verdict;
  const header = node("div", "result-header");
  const verdict = node("span", "verdict-label");
  verdict.dataset.verdict = result.verdict;
  verdict.append(
    createIcon(result.verdict === "TRUE" ? "check" : result.verdict === "FALSE" ? "x" : "circle-help", 15),
    document.createTextNode(result.verdict === "UNVERIFIABLE" ? "UNVERIFIABLE" : result.verdict),
  );
  const confidence = node(
    "span",
    "confidence",
    result.confidence ? `${Math.round(result.confidence)}% confidence` : "Confidence unavailable",
  );
  header.append(verdict, confidence);
  card.append(
    header,
    node("h3", "claim-text", result.claim),
    node("p", "explanation", result.explanation),
  );

  if (findablePageContext()) {
    const status = node("span", "find-page-status");
    const button = actionButton("Find on page", "search", () => {
      void findClaimOnPage(result.claim, button, status);
    });
    button.classList.add("find-page-action");
    const actions = node("div", "result-actions");
    actions.append(button, status);
    card.append(actions);
  }

  const sources = result.sources.filter((source) => safeSourceUrl(source.url));
  if (sources.length) {
    const list = node("ul", "source-list");
    for (const source of sources) {
      const item = node("li");
      const link = node("a", "source-link");
      link.href = safeSourceUrl(source.url);
      link.target = "_blank";
      link.rel = "noreferrer";
      link.title = source.url;
      link.append(node("span", "", source.title), createIcon("external-link", 14));
      item.append(link);
      list.append(item);
    }
    card.append(list);
  }

  return card;
}

function renderPlaceholder(index) {
  const placeholder = node("div", "claim-placeholder");
  placeholder.setAttribute("aria-label", `Claim ${index + 1} is still being checked`);
  placeholder.append(
    node("div", "skeleton-line"),
    node("div", "skeleton-line"),
    node("div", "skeleton-line"),
  );
  return placeholder;
}

function renderPendingClaim(index, claim, stage) {
  const [labelText, detail] = claimProgressCopy(stage);
  const card = node("article", "result-card pending-claim");
  card.dataset.verdict = "PENDING";
  card.setAttribute("aria-label", `Claim ${index + 1}: ${labelText.toLowerCase()}`);
  const header = node("div", "result-header");
  const label = node("span", "verdict-label");
  label.dataset.verdict = "PENDING";
  label.append(
    createIcon(stage === "waiting" ? "file-text" : "search", 14),
    document.createTextNode(labelText),
  );
  header.append(label, node("span", "confidence", `Claim ${index + 1}`));
  card.append(
    header,
    node("h3", "claim-text", claim.claim),
    node("p", "explanation", detail),
  );
  return card;
}

function renderFailedClaim(index, claim, error) {
  const card = node("article", "result-card failed-claim");
  card.dataset.verdict = "FAILED";
  const header = node("div", "result-header");
  const label = node("span", "verdict-label", "CHECK FAILED");
  label.dataset.verdict = "FAILED";
  const attemptCount = Number(app.snapshot.retryAttempts?.[index] || 0);
  const retryLimit = app.snapshot.claimRetryLimit || 2;
  header.append(label, node("span", "confidence", `Claim ${index + 1}`));
  card.append(
    header,
    node("h3", "claim-text", claim.claim),
    node("p", "explanation", error?.message || "No verdict was produced for this claim."),
  );

  if (error && isRetryableClaimError(error.code) && attemptCount < retryLimit) {
    const status = node("span", "retry-status");
    const retrying = app.snapshot.retryingClaimIndex === index;
    const button = actionButton(
      retrying ? "Retrying" : "Retry claim",
      "rotate-cw",
      () => void retryFailedClaim(index, status),
    );
    button.classList.add("retry-claim-action");
    button.disabled = isRunning();
    const actions = node("div", "result-actions");
    actions.append(button, status);
    card.append(actions);
  } else if (attemptCount >= retryLimit) {
    card.append(node("p", "retry-status", "Retry limit reached."));
  }
  return card;
}

function renderEmptyResult() {
  const empty = node("div", "empty-result");
  const [title, detail] = statusCopy(app.snapshot);
  const content = node("div");
  content.append(createIcon(statusVisual(app.snapshot.state).icon, 22), node("p", "", `${title}. ${detail}`));
  empty.append(content);
  return empty;
}

async function startCheck(forceRefresh) {
  app.inputMessage = "";
  renderInputMessage();

  try {
    const credentials = normalizeProviderCredentials(app.credentials);
    const payload = buildCheckPayload({
      mode: app.mode,
      page: app.page,
      url: app.drafts.url,
      text: app.drafts.text,
      forceRefresh,
    });
    const response = await createCheck(payload, credentials, app.installId);
    // The backend's job snapshot field is `id`, not `check_id` (jobs.py's
    // JobManager._snapshot_locked), and it doesn't return snapshot_url/events_url
    // convenience fields - the client constructs those paths itself from `id`.
    if (!response?.id || !response?.job_token) {
      throw new ApiError("The backend did not return secure check access.");
    }

    stopTransport();
    app.job = {
      checkId: response.id,
      snapshotPath: `/v1/checks/${encodeURIComponent(response.id)}`,
      eventsPath: `/v1/checks/${encodeURIComponent(response.id)}/events`,
      jobToken: response.job_token,
      mode: app.mode,
      context: submissionContext(),
    };
    app.snapshot = normalizeSnapshot({
      check_id: response.id,
      state: response.status || "queued",
      stage: "queued",
      sequence: 0,
    });
    app.cancelPending = false;
    await persistActiveJob();
    render();
    connectTransport();
    await refreshSnapshot();
  } catch (error) {
    showRequestError(error);
  }
}

async function requestCancellation() {
  if (!app.job || app.cancelPending) return;
  app.cancelPending = true;
  renderAction();
  try {
    await cancelCheck(app.job.checkId, app.job.jobToken, app.installId);
    await refreshSnapshot();
  } catch (error) {
    if (isJobNotFound(error)) {
      await resetActiveJob();
      render();
    } else {
      showRequestError(error);
    }
  } finally {
    app.cancelPending = false;
    renderAction();
  }
}

async function retryFailedClaim(claimIndex, status) {
  if (!app.job || isRunning()) return;
  status.textContent = "Starting retry...";
  try {
    const credentials = normalizeProviderCredentials(app.credentials);
    const raw = await retryCheckClaim(
      app.job.checkId,
      claimIndex,
      app.job.jobToken,
      credentials,
      app.installId,
    );
    if (!snapshotBelongsToJob(raw, app.job.checkId)) {
      throw new ApiError("The backend returned an unexpected retry response.");
    }
    if (!snapshotIsStale(raw, app.snapshot)) {
      app.snapshot = normalizeSnapshot(raw, app.snapshot);
    }
    await persistActiveJob();
    render();
    connectTransport();
  } catch (error) {
    status.textContent = error?.message || "The claim could not be retried.";
    elements.liveStatus.textContent = status.textContent;
  }
}

function connectTransport() {
  if (!app.job || isTerminalState(app.snapshot.state)) return;
  stopTransport();
  schedulePolling(10_000);

  const job = { ...app.job };
  const controller = new AbortController();
  app.eventController = controller;
  setBackend("online");
  void streamCheckEvents({
    eventsPath: job.eventsPath,
    jobToken: job.jobToken,
    clientId: app.installId,
    lastEventId: app.snapshot.sequence,
    signal: controller.signal,
    onEvent: handleServerEvent,
  }).then(() => {
    if (app.eventController !== controller || isTerminalState(app.snapshot.state)) return;
    app.eventController = null;
    app.reconnectTimer = setTimeout(connectTransport, 1_500);
  }).catch((error) => {
    if (controller.signal.aborted || app.eventController !== controller) return;
    app.eventController = null;
    if (isJobNotFound(error)) {
      void resetActiveJob().then(render);
    } else {
      setBackend("recovering");
      schedulePolling(2_500);
      app.reconnectTimer = setTimeout(connectTransport, 2_500);
    }
  });
}

function handleServerEvent(event) {
  let data = {};
  try {
    data = event.data ? JSON.parse(event.data) : {};
  } catch {
    data = { message: event.data };
  }
  if (!data.sequence && event.lastEventId) data.sequence = Number(event.lastEventId);
  app.snapshot = mergeEvent(app.snapshot, data, event.type);
  void persistActiveJob();
  render();

  clearTimeout(app.snapshotTimer);
  app.snapshotTimer = setTimeout(() => void refreshSnapshot(), 250);
  if (isTerminalState(app.snapshot.state)) stopTransport();
}

async function refreshSnapshot() {
  if (!app.job || app.refreshInFlight === app.job.checkId) return;
  const requestedJob = {
    checkId: app.job.checkId,
    snapshotPath: app.job.snapshotPath,
    jobToken: app.job.jobToken,
  };
  app.refreshInFlight = requestedJob.checkId;
  try {
    const raw = await getCheckSnapshot(
      requestedJob.checkId,
      requestedJob.snapshotPath,
      requestedJob.jobToken,
      app.installId,
    );
    if (app.job?.checkId !== requestedJob.checkId
        || !snapshotBelongsToJob(raw, requestedJob.checkId)
        || snapshotIsStale(raw, app.snapshot)) return;
    app.snapshot = normalizeSnapshot(raw, app.snapshot);
    setBackend("online");
    await persistActiveJob();
    render();
    if (isTerminalState(app.snapshot.state)) stopTransport();
  } catch (error) {
    if (app.job?.checkId !== requestedJob.checkId) return;
    if (isJobNotFound(error)) {
      // The backend has no record of this job - almost always api.py having
      // restarted since this job was created. Polling it forever would never
      // recover on its own; resetting is the only correct move.
      await resetActiveJob();
      render();
    } else if (error instanceof ApiError && ["backend_offline", "timeout"].includes(error.code)) {
      setBackend("offline");
      schedulePolling(2_500);
    }
  } finally {
    if (app.refreshInFlight === requestedJob.checkId) app.refreshInFlight = null;
  }
}

async function checkBackend({ quiet = false } = {}) {
  if (!quiet) setBackend("checking");
  try {
    const health = await getHealth();
    setBackend(health?.ok === false ? "offline" : "online");
    if (app.job && !isTerminalState(app.snapshot.state)) void refreshSnapshot();
  } catch {
    setBackend("offline");
  }
}

function schedulePolling(interval) {
  clearInterval(app.pollTimer);
  if (!app.job || isTerminalState(app.snapshot.state)) return;
  app.pollTimer = setInterval(() => void refreshSnapshot(), interval);
}

function stopTransport() {
  app.eventController?.abort();
  app.eventController = null;
  clearTimeout(app.reconnectTimer);
  app.reconnectTimer = null;
  clearInterval(app.pollTimer);
  app.pollTimer = null;
  clearTimeout(app.snapshotTimer);
  app.snapshotTimer = null;
}

function setBackend(status) {
  app.backend = status;
  renderBackend();
  renderAction();
}

function isRunning() {
  return Boolean(app.job) && !isTerminalState(app.snapshot.state);
}

function hasUsableInput() {
  if (app.mode === "page") return app.page?.status === "ready" && Boolean(app.page.text);
  if (app.mode === "url") return Boolean(app.drafts.url.trim());
  return app.drafts.text.trim().length >= CLIENT_LIMITS.minimumTextCharacters;
}

function submissionContext() {
  if (app.mode === "page") {
    return { title: app.page?.title, url: app.page?.url, tabId: app.page?.tabId ?? app.activeTabId };
  }
  if (app.mode === "url") return { url: app.drafts.url.trim() };
  return { title: "Pasted text" };
}

function exportResults(format) {
  // app.job.context is what was actually checked (captured at submission time);
  // preferred over submissionContext(), which reflects the current input and may
  // have changed since the check ran.
  const source = app.job?.context || submissionContext();
  const data = buildExportData(app.snapshot, { mode: app.job?.mode || app.mode, ...source });
  const stamp = data.exportedAt.replace(/[:.]/g, "-");
  if (format === "json") {
    downloadFile(`fact-check-${stamp}.json`, JSON.stringify(data, null, 2), "application/json");
  } else {
    downloadFile(`fact-check-${stamp}.md`, toMarkdownReport(data), "text/markdown");
  }
}

function downloadFile(filename, content, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function persistActiveJob() {
  const compact = compactSession(app.snapshot, app.job);
  if (compact) await chrome.storage.session.set({ [STORAGE.activeJob]: compact });
}

function saveDrafts() {
  return chrome.storage.session.set({ [STORAGE.drafts]: app.drafts });
}

function showRequestError(error) {
  app.inputMessage = error?.message || "The request could not be completed.";
  if (error instanceof ApiError && ["backend_offline", "timeout"].includes(error.code)) {
    setBackend("offline");
  }
  renderInputMessage();
}

function pageMeta(page) {
  const parts = [];
  try {
    parts.push(new URL(page.url).hostname.replace(/^www\./, ""));
  } catch {
    if (page.siteName) parts.push(page.siteName);
  }
  if (page.characterCount) parts.push(`${page.characterCount.toLocaleString()} characters`);
  return parts.join(" · ") || "Readable page text captured";
}

function statusVisual(state) {
  if (state === "complete") return { icon: "shield-check", tone: "success" };
  if (["complete_no_claims", "partial", "no_evidence"].includes(state)) {
    return { icon: "triangle-alert", tone: "warning" };
  }
  if (["failed", "cancelled", "timeout", "rate_limited", "unreadable", "invalid_input"].includes(state)) {
    return { icon: state === "cancelled" ? "square" : "x", tone: "danger" };
  }
  return { icon: "search", tone: "active" };
}

function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.toString() : "";
  } catch {
    return "";
  }
}

function isRetryableClaimError(code) {
  return [
    "provider_error",
    "provider_protocol_error",
    "provider_rate_limited",
    "provider_timeout",
  ].includes(code);
}

function actionButton(label, iconName, onClick) {
  const button = node("button", "secondary-action");
  button.type = "button";
  button.append(createIcon(iconName, 16), document.createTextNode(label));
  button.addEventListener("click", onClick);
  return button;
}

function findablePageContext() {
  if (!isTerminalState(app.snapshot.state)
      || app.job?.mode !== "page"
      || !app.job.context?.url) return null;
  const tabId = app.job.context.tabId ?? app.activeTabId;
  return Number.isInteger(tabId) ? { tabId, url: app.job.context.url } : null;
}

async function findClaimOnPage(claim, button, status) {
  const context = findablePageContext();
  if (!context) return;

  button.disabled = true;
  button.replaceChildren(createIcon("search", 16), document.createTextNode("Finding"));
  status.textContent = "";

  try {
    const response = await chrome.runtime.sendMessage({
      type: "FIND_CLAIM_ON_PAGE",
      tabId: context.tabId,
      url: context.url,
      claim,
    });
    const found = response?.found === true;
    status.textContent = found ? "Found on page." : response?.message || "Text not found on this page.";
    elements.liveStatus.textContent = status.textContent;
    button.replaceChildren(
      createIcon("search", 16),
      document.createTextNode(found ? "Find again" : "Try again"),
    );
  } catch {
    status.textContent = "The page could not be searched. Reopen it with the toolbar icon.";
    elements.liveStatus.textContent = status.textContent;
    button.replaceChildren(createIcon("search", 16), document.createTextNode("Try again"));
  } finally {
    button.disabled = false;
  }
}

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}
