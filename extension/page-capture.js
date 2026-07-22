import { samePageUrl } from "./page-find.js";

export const PAGE_CAPTURE_ORIGINS = Object.freeze(["http://*/*", "https://*/*"]);

export async function requestFreshPageCapture(chromeApi) {
  try {
    const granted = await chromeApi.permissions.request({ origins: PAGE_CAPTURE_ORIGINS });
    if (!granted) {
      return {
        ok: false,
        message: "Allow page access to refresh newly opened tabs.",
      };
    }
    const response = await chromeApi.runtime.sendMessage({ type: "REFRESH_PAGE_CAPTURE" });
    return response?.ok === false ? response : { ok: true, ...response };
  } catch {
    return {
      ok: false,
      message: "The current page could not be refreshed.",
    };
  }
}

export function hasNewPageSinceCheck(job, page, mode, terminal) {
  if (!terminal || mode !== "page" || job?.mode !== "page" || page?.status !== "ready") {
    return false;
  }
  const checkedUrl = job.context?.url;
  return Boolean(checkedUrl && page.url && !samePageUrl(checkedUrl, page.url));
}
