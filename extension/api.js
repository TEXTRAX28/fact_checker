import { API_BASE_URL, CLIENT_LIMITS } from "./config.js";

const API_ORIGIN = new URL(API_BASE_URL).origin;
const PROVIDER_KEY_MIN_LENGTH = 8;
const PROVIDER_KEY_MAX_LENGTH = 512;

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

export function normalizeProviderCredentials(value = {}) {
  const credentials = {
    geminiKey: String(value.geminiKey || "").trim(),
  };
  if (!validProviderKey(credentials.geminiKey)) {
    throw new ApiError("Enter a valid Gemini API key.", { code: "missing_gemini_key" });
  }
  return credentials;
}

function validProviderKey(key) {
  return key.length >= PROVIDER_KEY_MIN_LENGTH
    && key.length <= PROVIDER_KEY_MAX_LENGTH
    && !/\s/.test(key);
}

function providerHeaders(credentials) {
  const normalized = normalizeProviderCredentials(credentials);
  return {
    "X-Gemini-Key": normalized.geminiKey,
  };
}

function accessHeaders(jobToken) {
  if (!jobToken) {
    throw new ApiError("This check no longer has an access token. Start a new check.", {
      code: "missing_job_token",
    });
  }
  return { Authorization: `Bearer ${jobToken}` };
}

function clientHeaders(clientId) {
  return clientId ? { "X-Client-Id": clientId } : {};
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
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
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
    throw new ApiError("Cannot reach the fact-checking backend.", {
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

export function createCheck(payload, credentials, clientId) {
  return requestJson("/v1/checks", {
    method: "POST",
    body: payload,
    headers: {
      ...providerHeaders(credentials),
      ...clientHeaders(clientId),
    },
    expectedStatus: 202,
    timeoutMs: 15_000,
  });
}

export function getCheckSnapshot(checkId, snapshotPath, jobToken, clientId) {
  return requestJson(snapshotPath || `/v1/checks/${encodeURIComponent(checkId)}`, {
    headers: {
      ...accessHeaders(jobToken),
      ...clientHeaders(clientId),
    },
    timeoutMs: 10_000,
  });
}

export function cancelCheck(checkId, jobToken, clientId) {
  return requestJson(`/v1/checks/${encodeURIComponent(checkId)}`, {
    method: "DELETE",
    headers: {
      ...accessHeaders(jobToken),
      ...clientHeaders(clientId),
    },
    timeoutMs: 10_000,
  });
}

export function retryCheckClaim(
  checkId, claimIndex, jobToken, credentials, clientId,
) {
  return requestJson(
    `/v1/checks/${encodeURIComponent(checkId)}/claims/${encodeURIComponent(claimIndex)}/retry`,
    {
      method: "POST",
      headers: {
        ...accessHeaders(jobToken),
        ...providerHeaders(credentials),
        ...clientHeaders(clientId),
      },
      expectedStatus: 202,
      timeoutMs: 15_000,
    },
  );
}

function parseEventBlock(block) {
  let type = "message";
  let lastEventId = "";
  const data = [];
  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator >= 0 ? line.slice(0, separator) : line;
    const value = separator >= 0 ? line.slice(separator + 1).replace(/^ /, "") : "";
    if (field === "event") type = value;
    if (field === "id") lastEventId = value;
    if (field === "data") data.push(value);
  }
  return { type, lastEventId, data: data.join("\n") };
}

export async function streamCheckEvents({
  eventsPath,
  jobToken,
  clientId,
  lastEventId = 0,
  signal,
  onEvent,
}) {
  const response = await fetch(apiUrl(eventsPath), {
    method: "GET",
    headers: {
      Accept: "text/event-stream",
      ...accessHeaders(jobToken),
      ...clientHeaders(clientId),
      ...(lastEventId ? { "Last-Event-ID": String(lastEventId) } : {}),
    },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      const data = JSON.parse(body);
      detail = data?.detail || "";
    } catch {}
    throw new ApiError(detail || `Event stream failed (${response.status}).`, {
      status: response.status,
      code: "stream_failed",
    });
  }
  if (!response.body) {
    throw new ApiError("The backend returned an empty event stream.", {
      code: "stream_unavailable",
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        if (!block.trim()) continue;
        onEvent(parseEventBlock(block));
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
}
