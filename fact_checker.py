import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

@contextmanager
def _loading(message: str):
    # Prints "message." / "message.." / "message..." on a loop, overwriting the same
    # line, so a slow API call doesn't look like a frozen terminal. Always stops the
    # spinner in `finally`, even if the wrapped call raises.
    stop = threading.Event()

    def spin():
        dots = 0
        while not stop.is_set():
            sys.stdout.write(f"\r{message}{'.' * (dots % 4):<3}")
            sys.stdout.flush()
            dots += 1
            time.sleep(0.4)
        sys.stdout.write("\r" + " " * (len(message) + 3) + "\r")
        sys.stdout.flush()

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()

class NoEvidenceError(Exception):
    # Raised when a claim has zero search evidence to verify against (search failed or returned nothing usable). 
    pass

# DeepInfra API (OpenAI-compatible)
_deepinfra_client = None
_tavily = None

MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"

EXTRACT_PROMPT = """Extract up to 15 most specific and verifiable factual claims from the text.
Return a JSON array. Each item must have:
  "claim": the exact claim as stated
  "query": a short search query that targets the underlying FACT, not just the names in the claim.
           e.g. for "X is president of Indonesia" use "current president of Indonesia" so the real
           answer is findable and the claim can be disproved if false.
           Include the specific named person, company, or organization tied to the claim so the
           query surfaces primary/official sources instead of generic aggregator or stats sites.
           e.g. for "ChatGPT reached one million users in five days" use
           "OpenAI Sam Altman ChatGPT one million users five days", not just "ChatGPT million users".
  "speaker": the name or label of who made the claim (e.g. "Senator Davis", "SPEAKER_A"), use "UNKNOWN" only if truly unidentifiable

Only include: statistics, numbers, dates, named events, quotes, scientific/medical/legal/historical facts.
Skip: opinions, predictions, vague statements, rhetorical questions.
Also skip routine procedural narration the source already states as plain, undisputed fact (a plea
entered, a filing date, a standard step in a legal/administrative process), UNLESS it contains a
specific number, quote, or attribution that could plausibly be misreported. Routine narration has
near-zero misinformation risk and isn't worth a search+verify call.

If two or more figures are stated as complementary parts of one whole (a percentage split, a
budget breakdown, a ratio that sums to a total), extract them as ONE claim describing the full
breakdown, not one claim per figure. e.g. for "60% from nilai manfaat, 40% from Bipih" extract
a single claim "the scheme is 60% nilai manfaat and 40% Bipih", not two separate claims. Splitting
them risks one being verified TRUE and the other FALSE even though they're the same fact.
Return [] if nothing is checkable.
Return ONLY the JSON array, no other text."""

VERIFY_PROMPT = """You are a fact-checker. Output ONLY a JSON array. No markdown. No analysis. No prose. Just the JSON array.

Each object in the array must have:
  "speaker": the speaker label
  "claim": the original claim text
  "supported": true ONLY if the evidence explicitly and directly confirms the claim. False otherwise,
               including when the evidence is simply silent or missing, silence is not support.
  "contradicted": true ONLY if the evidence explicitly and directly contradicts the claim (denies
                   the event, states a number that changes the claim's meaning, not just a rounding/
                   date/conversion difference, see NUMERIC TOLERANCE below before marking any numeric
                   mismatch as contradicted), OR the claim is comparative/superlative and fails the
                   historical-comparison test below. False if the evidence is merely silent, absent,
                   or doesn't mention the claim at all, absence is not contradiction.
  "verdict": one of TRUE / UNVERIFIABLE / FALSE, this field is informational only and will be
             recomputed from "supported"/"contradicted" downstream, but fill it in consistently:
             supported=true & contradicted=false -> TRUE
             contradicted=true & supported=false -> FALSE
             anything else (neither, or both) -> UNVERIFIABLE
  "confidence": integer 60-100
  "explanation": 1-3 sentences, written in the SAME language as the claim text (e.g. claim in
                 Indonesian -> explanation in Indonesian, claim in English -> explanation in
                 English). Never mix languages within one explanation, regardless of what
                 language the search results/sources are in.
  "sources": array of URLs from the search results

Confidence guidelines:
  95-100 = Two or more independent, high-quality sources (e.g. official government, academic, Reuters, AP, BBC) directly support or directly contradict the claim.
  80-94 = At least one reliable source clearly supports or contradicts the claim, but independent confirmation is limited.
  60-79 = Evidence is incomplete, indirect, outdated, or conflicting. The verdict is plausible but not strongly supported.

The most common mistake is treating "I found no evidence either way" as FALSE. It is not, that is
UNVERIFIABLE. Only mark FALSE when the evidence actively says something different from the claim,
never because the evidence is merely absent or thin.

NUMERIC TOLERANCE (apply this BEFORE setting contradicted=true on any numeric claim):
A numeric claim is NOT contradicted just because a source states a different specific number.
Sources routinely round, convert currency, measure from a different baseline, or report a different
date. Only set contradicted=true if the gap is large enough to change what a reader would take away
from the claim, roughly: off by more than ~10%, off by an order of magnitude, or it crosses a
meaningful threshold the claim depends on (e.g. "over 1 million" vs an actual 800,000 changes the
claim's meaning; "1.05 million" vs an actual 1 million does not). If multiple sources disagree
slightly among themselves (e.g. 324m vs 330m for the same structure), that spread itself signals
normal measurement/rounding variance, not grounds to contradict a claim landing near that range.
This applies to ANY numeric claim, not just money: heights, distances, dates, counts, percentages.
e.g. a claim of "$125m in losses" against a source saying "$120m" is supported, not contradicted,
and a claim of "approximately 335 meters" against sources saying 324m-330m is supported, not
contradicted, small measurement variance is not a fabrication.

EVIDENCE ABOUT A DIFFERENT INSTANCE IS NOT THE SAME AS NO EVIDENCE:
If the search results discuss a different specific instance of a similar recurring subject (a
different session, year, edition, or version than the one named in the claim), that is not the
same as finding zero evidence. Do not use it to support or contradict the claim (still
UNVERIFIABLE, leave both false), but say so explicitly in the explanation, e.g. "sources cover the
78th session, not the 80th session named in the claim" rather than a generic "no evidence found."

COMPARATIVE AND SUPERLATIVE CLAIMS (CRITICAL):
If the claim contains comparative or superlative phrases like "nearer than ever before", "best ever", "highest ever", "more than before", "closest ever", "farthest ever", "one of the largest", "unprecedented", it requires DIFFERENT evidence than general claims.

For these claims:
  - Do NOT accept general trend data (e.g., "improving" or "growing") as proof of "closer than ever"
  - Require explicit historical comparison: minimum/maximum values, time-series data, or direct statements comparing current vs past
  - If the sources show ONLY that the subject is improving but provide NO historical minimum/maximum or time-series comparison, leave both supported and contradicted false (UNVERIFIABLE) - the specific superlative hasn't been proven, but general trend data isn't evidence against it either
  - If the sources provide clear historical data showing this IS the closest/best/highest point, set supported=true
  - Only set contradicted=true if the sources show the OPPOSITE: historical data proving some past point was actually closer/better/higher than now

Example: "Indonesia is nearer than ever before to ending poverty" + sources showing only "progress in poverty reduction" = UNVERIFIABLE (progress is not the same as "closest ever," but it's not evidence against it either). Only set supported=true if you find historical poverty rates proving current levels are lowest ever; only set contradicted=true if you find historical rates proving a past level was actually lower.

If the claim is about a CURRENT or ONGOING state (who currently holds office, live negotiations, present-day support), and the sources do not contain recent, direct evidence, leave both supported and contradicted false (UNVERIFIABLE). Do NOT infer a verdict from general or historical information.

Remove any claim where confidence would be below 60.

YOUR ENTIRE RESPONSE MUST BE A VALID JSON ARRAY STARTING WITH [ AND ENDING WITH ]. NOTHING ELSE."""


# DeepInfra client (OpenAI-compatible)
def _deepinfra_():
    global _deepinfra_client
    if _deepinfra_client is None:
        from openai import OpenAI
        _deepinfra_client = OpenAI(
            api_key=os.getenv("DEEPINFRA_API_KEY"),
            base_url=DEEPINFRA_BASE_URL
        )
    return _deepinfra_client

def _tavily_():
    global _tavily
    if _tavily is None:
        from tavily import TavilyClient
        _tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    return _tavily

def _chat(system: str, user: str, max_tokens: int) -> str:
    try:
        response = _deepinfra_().chat.completions.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        print(f"[ERROR] DeepInfra: {type(e).__name__}: {e}")
        raise

def _parse_json_array(text: str) -> list:
    text = re.sub(r"```(?:json)?\n?|```", "", text)
    # quote bare enums (e.g. "verdict": TRUE -> "verdict": "TRUE"). Anchored to the "verdict" key specifically, not just any ": TRUE"/": FALSE" otherwise a colon inside an explanation string gets corrupted too.
    text = re.sub(r'("verdict"\s*:\s*)(UNVERIFIABLE|TRUE|FALSE)\b', r'\1"\2"', text)
    text = re.sub(r",\s*([}\]])", r"\1", text)  # Remove trailing commas

    # Clean full array parse first.
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Fallback: pull complete objects straight from the text. Runs even when the array is truncated mid-output (no closing ]), keeps every complete object.
    objects = []
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group())
            if isinstance(obj, dict):
                objects.append(obj)
        except json.JSONDecodeError:
            continue
    return objects

_LOW_QUALITY = (
    "facebook.com", "youtube.com", "youtu.be", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "reddit.com", "quora.com",
    "pinterest.com", "threads.net", "medium.com",
)

_HIGH_QUALITY = (
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", ".gov", ".go.id",
    "who.int", "worldbank.org", "un.org", "imf.org", "nature.com",
    "sciencedirect.com", "nytimes.com",
)

# Not blocked, ranked below explicit high-quality sources but above unrecognized domains
_MEDIUM_QUALITY = (
    "wikipedia.org",
)

# Tavily's own relevance score per result, 0-1. Below this, results tend to be off-topic or thin (song lyrics, wrong-year pages) rather than just low-quality domains.
_MIN_SCORE = 0.3

def _filter_sources(results: list[dict]) -> list[dict]:
    # Social/UGC domains are already excluded upstream via Tavily's exclude_domains. Checks for the high and medium quality
    def rank(r):
        for domain in _HIGH_QUALITY:
            if domain in r["url"]:
                return 0
        for domain in _MEDIUM_QUALITY:
            if domain in r["url"]:
                return 1
        return 2

    results.sort(key=rank)
    return results[:3]

def _domain(url: str) -> str:
    from urllib.parse import urlparse
    netloc = urlparse(url).netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc

def _score(r: dict) -> float:
    return r.get("score", 0)

def _search(query: str, claim_label: str = "", verbose: bool = False) -> tuple[str, list[str]]:
    # Low-quality/UGC domains excluded at the Tavily, not filtered after the fact check.
    start = time.perf_counter()
    raw_results = _tavily_().search(query, max_results=10, exclude_domains=list(_LOW_QUALITY), search_depth="advanced").get("results", [])

    passed = []
    for r in raw_results:
        if _score(r) >= _MIN_SCORE:
            passed.append(r)

    accepted = _filter_sources(passed)

    accepted_urls = []
    for r in accepted:
        accepted_urls.append(r["url"])

    rejected = []
    for r in raw_results:
        if r["url"] not in accepted_urls:
            rejected.append(r)
    rejected.sort(key=_score, reverse=True)

    if verbose:
        # One print() call per claim (not one per line): each claim's search runs in its own
        # worker thread, so a block per print() call keeps concurrent claims' output from
        # interleaving line-by-line on screen.
        lines = []
        lines.append(f"\nClaim {claim_label}  ({time.perf_counter() - start:.2f}s)")
        lines.append(f"Retrieved: {len(raw_results)}")
        lines.append("")
        lines.append("Accepted")
        for r in accepted:
            lines.append(f"{_score(r):.2f} {_domain(r['url'])}")
        lines.append("")
        lines.append("Rejected")
        for r in rejected:
            lines.append(f"{_score(r):.2f} {_domain(r['url'])}")
        print("\n".join(lines))

    urls = []
    text_parts = []
    for r in accepted:
        urls.append(r["url"])
        text_parts.append(f"[{r['url']}]\n{r['content'][:600]}")

    text = "\n\n".join(text_parts)
    return text, urls

def _verify_one(claim: dict, search_text: str, urls: list[str], verbose: bool = False) -> dict | None:
    # One verify call per claim: keeps each verdict paired with its own search results (no positional zip drift) and lets callers reveal results as they land.
    if not search_text:
        # No search evidence at all (search failed or returned zero usable sources). Do not
        # send an empty evidence block to the model - verified live that it will still answer
        # confidently from its own training knowledge and fabricate source URLs that were
        # never actually retrieved. Raise instead of guessing; the caller's existing per-claim
        # error handling drops this claim from the results rather than showing a fake verdict.
        raise NoEvidenceError(f"no search evidence for claim: {claim['claim'][:60]!r}")

    context = (f"SPEAKER: {claim.get('speaker', 'UNKNOWN')}\n"
               f"CLAIM: {claim['claim']}\n"
               f"SEARCH RESULTS:\n{search_text}\n")
    start = time.perf_counter()
    if verbose:
        print(f"[v] DeepInfra verify -> {claim['claim'][:60]!r}")
    raw_reply = _chat(VERIFY_PROMPT, context, max_tokens=600)
    if verbose:
        # One print() call, not several: this claim's verify runs in its own worker thread
        # alongside every other claim's, so bundling done-time + raw text into a single write
        # keeps concurrent claims' raw output from interleaving into a garbled mess on screen.
        lines = [
            f"[v] DeepInfra verify done in {time.perf_counter() - start:.2f}s -> {claim['claim'][:60]!r}",
            f"[v] --- RAW RESPONSE (verify: {claim['claim'][:60]!r}) ---",
            raw_reply,
            "[v] --- END RAW RESPONSE ---",
        ]
        print("\n".join(lines))

    parsed = []
    for v in _parse_json_array(raw_reply):
        if isinstance(v, dict) and v.get("verdict"):
            parsed.append(v)

    if not parsed:
        return None
    verdict = parsed[0]
    if not verdict.get("sources"):
        verdict["sources"] = urls[:3]

    # Deterministic verdict: derived from supported/contradicted rather than trusting the
    # model's own "verdict" field, closes the "no evidence found -> FALSE" failure mode
    # at the code level instead of just asking the model not to do it.
    supported = bool(verdict.get("supported"))
    contradicted = bool(verdict.get("contradicted"))
    if supported and not contradicted:
        verdict["verdict"] = "TRUE"
    elif contradicted and not supported:
        verdict["verdict"] = "FALSE"
    else:
        verdict["verdict"] = "UNVERIFIABLE"

    return verdict

def fact_check(transcript: str, on_result=None, verbose=False) -> list[dict]:
    # on_result(verdict) is called for each verdict as it's revealed, in claim order, so callers can print results one by one instead of waiting for all.
    # verbose=True prints a specific reason when nothing comes back, so the caller can tell "too short" from "no claims" from "couldn't verify any".
    def note(msg: str):
        if verbose:
            print(msg)
    try:
        if not transcript or len(transcript.strip()) < 10:
            note("Input too short to fact-check, give more sentences to fact-check")
            return []

        try:
            start = time.perf_counter()
            if verbose:
                print("[v] DeepInfra extract -> starting")
                raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=4000)
                lines = [
                    f"[v] DeepInfra extract done in {time.perf_counter() - start:.2f}s",
                    "[v] --- RAW RESPONSE (extract) ---",
                    raw,
                    "[v] --- END RAW RESPONSE ---",
                ]
                print("\n".join(lines))
            else:
                with _loading("Extracting claims (Llama 3.3 70B via DeepInfra)"):
                    raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=4000)

            claims = []
            for c in _parse_json_array(raw):
                if isinstance(c, dict) and c.get("claim"):
                    claims.append(c)

            if not claims:
                note("No checkable factual claims found, looks like opinion, prediction, or too vague.")
                return []
        except Exception as e:
            print(f"[ERROR] Extraction: {type(e).__name__}: {e}")
            return []

        try:
            print(f"Found {len(claims)} claim(s).")

            def _run_searches():
                with ThreadPoolExecutor() as pool:
                    search_futures = []
                    for i, claim in enumerate(claims, start=1):
                        claim_label = f"{i}/{len(claims)}"
                        search_futures.append(pool.submit(_search, claim["query"], claim_label, verbose))

                    results = []
                    for f in search_futures:
                        try:
                            results.append(f.result())
                        except Exception as e:
                            print(f"[ERROR] Search: {type(e).__name__}: {e}")
                            results.append(("", []))
                    return results

            if verbose:
                search_results = _run_searches()
            else:
                with _loading("Searching Tavily for evidence"):
                    search_results = _run_searches()
        except Exception as e:
            print(f"[ERROR] Search: {type(e).__name__}: {e}")
            return []

        # Verify each claim concurrently, but reveal in claim order: iterating the futures list front-to-back blocks on #1 first while #2.. finish in the
        # background, so results appear in order as soon as each is ready.
        print(f"Verifying {len(claims)} claim(s) (Llama 3.3 70B via DeepInfra)...\n")
        results: list[dict] = []
        with ThreadPoolExecutor() as pool:
            futures = []
            for claim, search_result in zip(claims, search_results):
                search_text, urls = search_result
                futures.append(pool.submit(_verify_one, claim, search_text, urls, verbose))

            error_count = 0
            for f in futures:
                try:
                    verdict = f.result()
                except Exception as e:
                    print(f"[ERROR] Verification: {type(e).__name__}: {e}")
                    error_count += 1
                    continue
                if verdict:
                    results.append(verdict)
                    if on_result:
                        on_result(verdict)
        if not results:
            note(f"Extracted {len(claims)} claim(s), but none could be verified with enough "
                 f"confidence, no source clearly confirmed or denied them (confidence < 60).")
        else:
            low_confidence_dropped = len(claims) - len(results) - error_count
            if low_confidence_dropped > 0:
                note(f"{low_confidence_dropped} claim(s) dropped (confidence < 60).")
        return results

    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return []

if __name__ == "__main__":
    # Self-check: _parse_json_array must survive the malformed JSON the LLM actually emits.
    clean = '[{"claim":"a","verdict":"TRUE"},{"claim":"b","verdict":"FALSE"}]'
    bare_enum = '[{"claim":"a","verdict":TRUE,"confidence":90}]'              # unquoted enum (option-3 failure)
    fenced = '```json\n[{"claim":"a","verdict":"TRUE"}]\n```'
    trailing = '[{"claim":"a","verdict":"TRUE"},]'                            # trailing comma
    truncated = ('[{"claim":"a","verdict":TRUE,"sources":["u1"]},'
                 '{"claim":"b","verdict":FALSE,"sources":["u2"]},'
                 '{"claim":"c","verdict":')                                   # cut off mid-output
    explanation_colon = ('[{"claim":"a","verdict":TRUE,"confidence":90,'
                          '"explanation":"Critics say the verdict is: FALSE, but evidence supports TRUE overall."}]')

    assert len(_parse_json_array(clean)) == 2, "clean"
    assert len(_parse_json_array(bare_enum)) == 1, "bare enum"
    assert _parse_json_array(bare_enum)[0]["verdict"] == "TRUE", "enum value"
    assert len(_parse_json_array(fenced)) == 1, "fenced"
    assert len(_parse_json_array(trailing)) == 1, "trailing comma"
    assert len(_parse_json_array(truncated)) == 2, "truncated keeps complete objects"
    assert _parse_json_array("not json at all") == [], "garbage"
    assert len(_parse_json_array(explanation_colon)) == 1, "colon inside explanation must not corrupt JSON"
    assert _parse_json_array(explanation_colon)[0]["verdict"] == "TRUE", "verdict still quoted correctly"

    # Source ranking: high-quality first, Wikipedia second (above unrecognized domains,
    # below explicit high-quality ones), everything else keeps its original (Tavily-given)
    # relative order last.
    mixed = [{"url": "https://en.wikipedia.org/a"}, {"url": "https://some-blog.com/b"},
             {"url": "https://reuters.com/c"}, {"url": "https://other-blog.com/d"}]
    ranked = _filter_sources(mixed)
    ranked_urls = []
    for r in ranked:
        ranked_urls.append(r["url"])
    assert ranked_urls == ["https://reuters.com/c", "https://en.wikipedia.org/a",
                           "https://some-blog.com/b"], "high-quality first, wikipedia above unranked domains, order preserved within tiers"
    assert len(ranked) <= 3, "caps at 3 sources"

    # Empty search evidence must raise NoEvidenceError before ever calling the LLM (real bug found live: with search failing entirely, the model answered from its own training knowledge and fabricated citations instead of admitting no evidence was found).
    raised_no_evidence = False
    try:
        _verify_one({"claim": "Earth is square", "speaker": "X"}, "", [])
    except NoEvidenceError:
        raised_no_evidence = True
    assert raised_no_evidence, "empty search evidence must raise NoEvidenceError"

    print("OK: all self-checks pass")
    

