const WORD_PATTERN = /[\p{L}\p{N}]+/gu;
const NUMBER_PATTERN = /(?:[$\u20ac\u00a3]\s*)?\d+(?:[.,]\d+)*(?:\s*%)?/gu;
const MIN_LONG_FALLBACK_WORDS = 6;
const SHORT_FALLBACK_WORDS = 5;
const MAX_SHORT_CANDIDATES = 32;
const MAX_NUMBER_CANDIDATES = 8;
const MAX_FALLBACK_WORDS = 12;
const MAX_CANDIDATES = 80;

export function claimSearchCandidates(value) {
  const claim = String(value || "").replace(/\s+/g, " ").trim();
  if (!claim) return [];

  const candidates = [claim];
  const words = [...claim.matchAll(WORD_PATTERN)];
  if (words.length < SHORT_FALLBACK_WORDS) return candidates;

  // Long claims can produce hundreds of sliding windows. Reserve space for
  // shorter phrases from across the entire claim before filling the remaining
  // budget with longer, more precise matches. Otherwise the cap is exhausted
  // near the beginning and paraphrased text later in the claim is never tried.
  const shortCandidates = [];
  const shortWindowCount = words.length - SHORT_FALLBACK_WORDS + 1;
  const shortStarts = sampledStarts(shortWindowCount, MAX_SHORT_CANDIDATES);
  for (const start of shortStarts) {
    shortCandidates.push(phraseAt(claim, words, start, SHORT_FALLBACK_WORDS));
  }

  const numberCandidates = [...claim.matchAll(NUMBER_PATTERN)]
    .map((match) => match[0].trim())
    .filter((value, index, values) => value && values.indexOf(value) === index)
    .slice(0, MAX_NUMBER_CANDIDATES);

  const longBudget = Math.max(
    0,
    MAX_CANDIDATES - candidates.length - shortCandidates.length - numberCandidates.length,
  );
  const longCandidates = [];

  const longest = Math.min(MAX_FALLBACK_WORDS, words.length);
  for (let length = longest; length >= MIN_LONG_FALLBACK_WORDS; length -= 1) {
    for (let start = 0; start + length <= words.length; start += 1) {
      const phrase = phraseAt(claim, words, start, length);
      if (!longCandidates.includes(phrase)) longCandidates.push(phrase);
      if (longCandidates.length >= longBudget) break;
    }
    if (longCandidates.length >= longBudget) break;
  }

  for (const phrase of [...longCandidates, ...shortCandidates, ...numberCandidates]) {
    if (!candidates.includes(phrase)) candidates.push(phrase);
    if (candidates.length >= MAX_CANDIDATES) break;
  }
  return candidates;
}

function phraseAt(claim, words, start, length) {
  const first = words[start];
  const last = words[start + length - 1];
  return claim.slice(first.index, last.index + last[0].length);
}

function sampledStarts(count, limit) {
  if (count <= limit) return Array.from({ length: count }, (_, index) => index);
  const starts = Array.from(
    { length: limit },
    (_, index) => Math.round(index * (count - 1) / (limit - 1)),
  );
  return [...new Set(starts)];
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
