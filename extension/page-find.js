const WORD_PATTERN = /[\p{L}\p{N}]+/gu;
const MIN_FALLBACK_WORDS = 6;
const MAX_FALLBACK_WORDS = 12;
const MAX_CANDIDATES = 80;

export function claimSearchCandidates(value) {
  const claim = String(value || "").replace(/\s+/g, " ").trim();
  if (!claim) return [];

  const candidates = [claim];
  const words = [...claim.matchAll(WORD_PATTERN)];
  if (words.length < MIN_FALLBACK_WORDS) return candidates;

  const longest = Math.min(MAX_FALLBACK_WORDS, words.length);
  for (let length = longest; length >= MIN_FALLBACK_WORDS; length -= 1) {
    for (let start = 0; start + length <= words.length; start += 1) {
      const first = words[start];
      const last = words[start + length - 1];
      const phrase = claim.slice(first.index, last.index + last[0].length);
      if (!candidates.includes(phrase)) candidates.push(phrase);
      if (candidates.length >= MAX_CANDIDATES) return candidates;
    }
  }
  return candidates;
}

export function samePageUrl(actual, expected) {
  try {
    const actualUrl = new URL(actual);
    const expectedUrl = new URL(expected);
    actualUrl.hash = "";
    expectedUrl.hash = "";
    return actualUrl.href === expectedUrl.href;
  } catch {
    return false;
  }
}

export function selectClaimText(candidates) {
  if (!document.body || typeof window.find !== "function") return { found: false };

  const selection = window.getSelection();
  const resetSearch = () => {
    selection.removeAllRanges();
    const start = document.createRange();
    start.selectNodeContents(document.body);
    start.collapse(true);
    selection.addRange(start);
  };

  for (let index = 0; index < candidates.length; index += 1) {
    resetSearch();
    if (!window.find(candidates[index], false, false, true, false, false, false)) continue;

    const range = selection.rangeCount ? selection.getRangeAt(0) : null;
    const target = range?.startContainer?.nodeType === Node.ELEMENT_NODE
      ? range.startContainer
      : range?.startContainer?.parentElement;
    target?.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "center",
    });
    return { found: true, exact: index === 0, matchedText: selection.toString() };
  }

  selection.removeAllRanges();
  return { found: false };
}
