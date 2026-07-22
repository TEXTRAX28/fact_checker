import { claimSearchCandidates, samePageUrl, selectClaimText } from "./page-find.js";

const ACTIVE_TAB_KEY = "fc:activeTabId";

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => {});
});

chrome.action.onClicked.addListener((tab) => {
  if (!Number.isInteger(tab.id)) return;

  chrome.sidePanel.open({ tabId: tab.id }).catch(() => {});
  void capturePage(tab);
});

// Re-invoked from the side panel's Refresh button after the panel requests its
// optional page-access permission. activeTab alone is not enough here: Chrome
// grants it for toolbar invocation, not for a button inside an extension page.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "REFRESH_PAGE_CAPTURE") {
    void captureActivePage().then(sendResponse).catch(() => {
      sendResponse({ ok: false, message: "The active browser tab could not be found." });
    });
    return true;
  }

  if (message?.type === "FIND_CLAIM_ON_PAGE") {
    void findClaimOnPage(message).then(sendResponse);
    return true;
  }

  return undefined;
});

async function captureActivePage() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || !Number.isInteger(tab.id)) {
    return { ok: false, message: "The active browser tab could not be found." };
  }
  await capturePage(tab);
  return { ok: true, tabId: tab.id };
}

async function findClaimOnPage(message) {
  const tabId = message?.tabId;
  const candidates = claimSearchCandidates(message?.claim);
  if (!Number.isInteger(tabId) || !message?.url || candidates.length === 0) {
    return { found: false, message: "This claim cannot be searched on the page." };
  }

  try {
    const tab = await chrome.tabs.get(tabId);
    if (!samePageUrl(tab.url, message.url)) {
      return { found: false, message: "The checked page is no longer open in that tab." };
    }

    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [0] },
      func: selectClaimText,
      args: [candidates],
    });
    if (!result?.found) {
      return { found: false, message: "The claim wording was not found on this page." };
    }

    await chrome.tabs.update(tabId, { active: true }).catch(() => {});
    return result;
  } catch {
    return {
      found: false,
      message: "The page could not be searched. Reopen it with the Fact Check toolbar icon.",
    };
  }
}

async function capturePage(tab) {
  const tabId = tab.id;
  const pageKey = `fc:page:${tabId}`;
  const url = tab.url || "";

  await chrome.storage.session.set({
    [ACTIVE_TAB_KEY]: tabId,
    [pageKey]: {
      tabId,
      url,
      title: tab.title || "",
      status: "collecting",
      capturedAt: Date.now(),
    },
  });
  notifyPanel("PAGE_CAPTURE_STARTED", tabId);

  if (!url) {
    // tab.url comes back empty when the extension currently lacks activeTab
    // access for this tab - normal any time this runs from something other
    // than a direct toolbar-icon click (e.g. the panel's Refresh button),
    // since navigation revokes the grant. Distinct from a genuinely
    // unsupported page: the fix is re-clicking the toolbar icon, not "this
    // page can never be read."
    await storeCaptureFailure(pageKey, tabId, url, tab.title,
      "Couldn't re-read this page from here. Click the Fact Check icon in your toolbar to refresh it.");
    return;
  }
  if (!/^https?:\/\//i.test(url)) {
    await storeCaptureFailure(pageKey, tabId, url, tab.title, "Chrome does not allow reading this page.");
    return;
  }

  try {
    await chrome.scripting.executeScript({
      target: { tabId, frameIds: [0] },
      files: ["vendor/readability/Readability.js"],
    });

    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [0] },
      func: extractReadablePage,
    });

    if (!result?.text || result.text.trim().length < 20) {
      throw new Error("No readable article text was found on this page.");
    }

    const page = {
      ...result,
      tabId,
      url,
      title: result.title || tab.title || "Untitled page",
      status: "ready",
      capturedAt: Date.now(),
    };
    await chrome.storage.session.set({ [pageKey]: page });
    notifyPanel("PAGE_CAPTURED", tabId);
  } catch (error) {
    await storeCaptureFailure(
      pageKey,
      tabId,
      url,
      tab.title,
      error?.message || "This page could not be read.",
    );
  }
}

function extractReadablePage() {
  const documentClone = document.cloneNode(true);
  const article = typeof Readability === "function"
    ? new Readability(documentClone).parse()
    : null;
  const fallbackText = documentClone.body?.textContent || "";
  const text = (article?.textContent || fallbackText).replace(/\s+/g, " ").trim();
  const publishedTime = document.querySelector('meta[property="article:published_time"]')?.content
    || document.querySelector("time[datetime]")?.dateTime
    || null;

  return {
    title: article?.title || document.title || "Untitled page",
    byline: article?.byline || null,
    siteName: article?.siteName || location.hostname,
    publishedTime,
    excerpt: article?.excerpt || null,
    text,
    characterCount: text.length,
  };
}

async function storeCaptureFailure(pageKey, tabId, url, title, detail) {
  await chrome.storage.session.set({
    [pageKey]: {
      tabId,
      url,
      title: title || "Unavailable page",
      status: "failed",
      error: detail,
      capturedAt: Date.now(),
    },
  });
  notifyPanel("PAGE_CAPTURE_FAILED", tabId);
}

function notifyPanel(type, tabId) {
  chrome.runtime.sendMessage({ type, tabId }).catch(() => {});
}
