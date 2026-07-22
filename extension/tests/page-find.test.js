import assert from "node:assert/strict";
import test from "node:test";

import { claimSearchCandidates, samePageUrl } from "../page-find.js";

test("claimSearchCandidates searches the complete normalized claim first", () => {
  const candidates = claimSearchCandidates("  Canada   imposed a 25% tariff on selected imports.  ");
  assert.equal(candidates[0], "Canada imposed a 25% tariff on selected imports.");
  assert.ok(candidates.includes("Canada imposed a 25% tariff on selected imports"));
});

test("claimSearchCandidates adds long distinctive phrases while preserving punctuation", () => {
  const candidates = claimSearchCandidates(
    "Trump announced tariffs on wine, hockey sticks, cement, and several industrial products from Canada.",
  );
  assert.ok(candidates.some((candidate) => candidate.includes("wine, hockey sticks, cement")));
  assert.ok(candidates.every((candidate) => candidate.split(/\s+/).length >= 6));
});

test("claimSearchCandidates does not fall back to short generic fragments", () => {
  assert.deepEqual(claimSearchCandidates("Tariffs increased yesterday."), ["Tariffs increased yesterday."]);
});

test("samePageUrl ignores fragments but not different queries or paths", () => {
  assert.equal(samePageUrl("https://example.com/news?a=1#story", "https://example.com/news?a=1"), true);
  assert.equal(samePageUrl("https://example.com/news?a=2", "https://example.com/news?a=1"), false);
  assert.equal(samePageUrl("not a URL", "https://example.com/news"), false);
});
