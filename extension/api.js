import { API_BASE_URL, CLIENT_LIMITS } from "./config.js";

const API_ORIGIN = new URL(API_BASE_URL).origin;

export class ApiError extends Error {
  constructor(message, { status = 0, code = "request_failed", details = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function apiUrl(path = "/") {
  const url = new URL(path, `${API_BASE_URL.replace(/\/$/, "")}/`);
  if (url.origin !== API_ORIGIN) {
    throw new ApiError("The backend returned an unexpected URL.", {
      code: "invalid_backend_url",
    });
  }
  return url.toString();
}

export function normalizeHttpUrl(value) {
  let url;
  try {
    url = new URL(String(value).trim());
  } catch {
    throw new ApiError("Enter a valid http or https URL.", { code: "invalid_url" });
  }

  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new ApiError("Enter a valid http or https URL.", { code: "invalid_url" });
  }
  url.hash = "";
  return url.toString();
}

export function buildCheckPayload({ mode, page, url, text, forceRefresh = false }) {
  // The backend's CheckRequest field is named `type` and has no force_refresh
  // field (extra="forbid" rejects unknown fields, so sending either breaks
  // every request with a 422).
  const payload = { type: mode };

  if (mode === "page") {
    const pageText = String(page?.text || "").trim();
    if (!pageText) {
      throw new ApiError("This page could not be read. Try its URL or paste the text.", {
        code: "page_unavailable",
      });
    }
    if (pageText.length > CLIENT_LIMITS.textCharacters) {
      throw new ApiError(`This page is longer than ${CLIENT_LIMITS.textCharacters.toLocaleString()} characters. Try its URL or paste a shorter section.`, {
        code: "text_too_long",
      });
    }
    payload.url = normalizeHttpUrl(page.url);
    payload.title = String(page.title || "Untitled page").trim();
    payload.text = pageText;
    return payload;
  }

  if (mode === "url") {
    payload.url = normalizeHttpUrl(url);
    return payload;
  }

  if (mode === "text") {
    const cleanText = String(text || "").trim();
    if (!cleanText) {
      throw new ApiError("Paste text containing the claims you want to check.", {
        code: "empty_text",
      });
    }
    if (cleanText.length < CLIENT_LIMITS.minimumTextCharacters) {
      throw new ApiError(`Paste at least ${CLIENT_LIMITS.minimumTextCharacters} characters to check.`, {
        code: "text_too_short",
      });
    }
    if (cleanText.length > CLIENT_LIMITS.textCharacters) {
      throw new ApiError(`Keep pasted text under ${CLIENT_LIMITS.textCharacters.toLocaleString()} characters.`, {
        code: "text_too_long",
      });
    }
    payload.text = cleanText;
    return payload;
  }

  throw new ApiError("Select a valid checking mode.", { code: "invalid_mode" });
}

async function requestJson(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 12_000);

  try {
    const response = await fetch(apiUrl(path), {
      method: options.method || "GET",
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
      signal: controller.signal,
    });

    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json")
      ? await response.json()
      : await response.text();

    if (!response.ok || (options.expectedStatus && response.status !== options.expectedStatus)) {
      const message = typeof data === "object" && data
        ? data.detail || data.message || data.error
        : data;
      throw new ApiError(message || `Backend request failed (${response.status}).`, {
        status: response.status,
        code: "http_error",
        details: data,
      });
    }

    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error?.name === "AbortError") {
      throw new ApiError("The backend did not respond in time.", { code: "timeout" });
    }
    throw new ApiError("Cannot reach the local fact-checking backend.", {
      code: "backend_offline",
      details: error?.message || null,
    });
  } finally {
    clearTimeout(timeout);
  }
}

export function getHealth() {
  return requestJson("/health", { timeoutMs: 4_000 });
}

export function createCheck(payload) {
  return requestJson("/v1/checks", {
    method: "POST",
    body: payload,
    expectedStatus: 202,
    timeoutMs: 15_000,
  });
}

export function getCheckSnapshot(checkId, snapshotPath) {
  return requestJson(snapshotPath || `/v1/checks/${encodeURIComponent(checkId)}`, {
    timeoutMs: 10_000,
  });
}

export function cancelCheck(checkId) {
  return requestJson(`/v1/checks/${encodeURIComponent(checkId)}`, {
    method: "DELETE",
    timeoutMs: 10_000,
  });
}

export function retryCheckClaim(checkId, claimIndex) {
  return requestJson(
    `/v1/checks/${encodeURIComponent(checkId)}/claims/${encodeURIComponent(claimIndex)}/retry`,
    { method: "POST", expectedStatus: 202, timeoutMs: 15_000 },
  );
}
