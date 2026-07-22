import json
import copy
import difflib
import io
import os
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

SEARCH_WORKERS = 4
VERIFY_WORKERS = 3
MAX_CLAIMS = 15
MIN_INPUT_NON_WHITESPACE = 10
DEEPINFRA_TIMEOUT_SECONDS = 30.0
TAVILY_TIMEOUT_SECONDS = 15.0
PROVIDER_MAX_RETRIES = 2
MAX_EVIDENCE_PER_SOURCE_CHARS = 4_000
MAX_EVIDENCE_TOTAL_CHARS = 10_000
VERIFY_BACKLOG_MULTIPLIER = 2
RAW_RESPONSE_LOG_MAX_CHARS = 8_000

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


class ProviderProtocolError(Exception):
    """Raised when provider output does not satisfy the expected JSON protocol."""


class FactCheckResult(list):
    """List-compatible pipeline result with enough metadata for service adapters."""

    def __init__(self, values=(), *, status: str = "completed", claim_count: int = 0,
                 errors: list[dict] | None = None):
        super().__init__(values)
        self.status = status
        self.claim_count = claim_count
        self.errors = errors or []


def _safe_callback(callback, value, label: str) -> None:
    if callback is None:
        return
    try:
        callback(copy.deepcopy(value))
    except Exception as exc:
        print(f"[ERROR] {label} callback: {type(exc).__name__}: {exc}")


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) or getattr(exc, "status_code", None)


def _is_rate_limited(exc: Exception) -> bool:
    return (_status_code(exc) == 429
            or type(exc).__name__ in {
                "RateLimitError", "UsageLimitExceededError", "TavilyKeylessLimitError"
            })


def _is_retryable(exc: Exception) -> bool:
    status_code = _status_code(exc)
    return (_is_timeout(exc) or _is_rate_limited(exc)
            or status_code in {408, 409}
            or (status_code is not None and status_code >= 500))


def _public_provider_error(stage: str, exc: Exception,
                           claim_index: int | None = None) -> dict:
    if _is_rate_limited(exc):
        code, message = "provider_rate_limited", "A provider rate limit was reached."
    elif _is_timeout(exc):
        code, message = "provider_timeout", "A provider request timed out."
    elif isinstance(exc, ProviderProtocolError):
        code, message = "provider_protocol_error", "A provider returned malformed output."
    else:
        code, message = "provider_error", "A provider request failed."
    error = {"stage": stage, "code": code, "message": message}
    if claim_index is not None:
        error["claim_index"] = claim_index
    return error

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

Only include: statistics, numbers, dates, named events, quotes, scientific/medical/legal/historical
facts, and factual-sounding assertions about a named person or entity (identity, role, personal
attributes) even if sensitive or likely hard to verify.
Skip: subjective value judgments (e.g. "X is a bad person"), predictions, vague statements,
rhetorical questions. Do NOT skip a factual-sounding claim just because it's sensitive, personal,
or likely unverifiable (e.g. a claim about someone's identity, orientation, or private life) -
extract it; the verifier will return UNVERIFIABLE if no evidence exists either way.
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
  "source_analysis": array with exactly one entry per numbered source shown in SEARCH
                      RESULTS below. Reference sources ONLY by the [N] index shown there -
                      never invent an index and never use a URL. Each entry:
                        "source_index": the [N] number of the source
                        "stance": one of SUPPORTS / CONTRADICTS / PARTIAL / IRRELEVANT / INSUFFICIENT
                        "directness": DIRECT if the source addresses the exact entity, number,
                                      and timeframe in the claim; INDIRECT if only generally related
                        "reason": one short sentence
                        "evidence_excerpt": a short exact quote (under 25 words) from that
                                            source supporting the stance, or null if the
                                            stance is IRRELEVANT or INSUFFICIENT

SOURCE_ANALYSIS DEFINITIONS:
  SUPPORTS = this source directly and explicitly confirms the claim.
  CONTRADICTS = this source directly and explicitly denies or contradicts the claim.
  PARTIAL = this source confirms only part of a compound or qualified claim, not all of it.
  IRRELEVANT = this source discusses the same general topic but does not address the
               claim's specific entity, number, or timeframe - topical relevance alone is
               never SUPPORTS.
  INSUFFICIENT = this source might be relevant but lacks enough detail to decide either way.

"supported" and "contradicted" above must be consistent with source_analysis: set
supported=true only if at least one entry is SUPPORTS, and contradicted=true only if at
least one entry is CONTRADICTS. These two fields will also be recomputed downstream
directly from source_analysis, so an inconsistency here will be corrected automatically -
but make them agree in the first place.

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
def _print_raw_response(*, run_id: str, claim_label: str, provider: str, operation: str,
                        attempt: int, elapsed: float, body, **extra_fields) -> None:
    # Terminal-only debug output, gated behind --verbose in main.py's CLI. The API
    # path never sets verbose=True (not wired into api.py/jobs.py), so this never
    # reaches the extension - it's strictly a `python main.py -v` / `python
    # fact_checker.py` terminal aid, not something the frontend can trigger or see.
    lines = [
        "=" * 80,
        "RAW PROVIDER RESPONSE",
        f"timestamp: {datetime.now(timezone.utc).astimezone().isoformat(timespec='milliseconds')}",
        f"run_id: {run_id}",
        f"claim: {claim_label}",
        f"provider: {provider}",
        f"operation: {operation}",
        f"attempt: {attempt}",
        f"elapsed_seconds: {elapsed:.2f}",
    ]
    for key, value in extra_fields.items():
        lines.append(f"{key}: {value}")
    lines.append("=" * 80)

    body_text = body if isinstance(body, str) else json.dumps(body, indent=2, default=str)
    if len(body_text) > RAW_RESPONSE_LOG_MAX_CHARS:
        omitted = len(body_text) - RAW_RESPONSE_LOG_MAX_CHARS
        body_text = body_text[:RAW_RESPONSE_LOG_MAX_CHARS] + f"\n...truncated ({omitted} more characters)..."
    lines.append(body_text)
    lines.append("=" * 80)
    print("\n".join(lines))


def _deepinfra_():
    global _deepinfra_client
    if _deepinfra_client is None:
        from openai import OpenAI
        _deepinfra_client = OpenAI(
            api_key=os.getenv("DEEPINFRA_API_KEY"),
            base_url=DEEPINFRA_BASE_URL,
            timeout=DEEPINFRA_TIMEOUT_SECONDS,
            max_retries=PROVIDER_MAX_RETRIES,
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
            timeout=DEEPINFRA_TIMEOUT_SECONDS,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        print(f"[ERROR] DeepInfra: {type(e).__name__}: {e}")
        raise

def _parse_provider_array(text: str) -> tuple[list, bool]:
    text = re.sub(r"```(?:json)?\n?|```", "", text)
    # quote bare enums (e.g. "verdict": TRUE -> "verdict": "TRUE"). Anchored to the "verdict" key specifically, not just any ": TRUE"/": FALSE" otherwise a colon inside an explanation string gets corrupted too.
    text = re.sub(r'("verdict"\s*:\s*)(UNVERIFIABLE|TRUE|FALSE)\b', r'\1"\2"', text)
    text = re.sub(r",\s*([}\]])", r"\1", text)  # Remove trailing commas
    # Strip backslash-escapes JSON doesn't recognize (e.g. "\_", a markdown-style
    # underscore escape the model sometimes emits in legal citations like "607 U.S. \_\_\_"),
    # a real, live-reproduced cause of raw_decode failing on an otherwise-valid response.
    # Consumes escape pairs atomically so a valid "\\" isn't split into "\" + a stray second
    # backslash that then looks invalid on its own.
    text = re.sub(r'\\(.)', lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu' else m.group(1), text)

    # Full array parse first, via raw_decode from the first '[' rather than a regex
    # spanning greedily to the LAST ']' in the text - a stray bracket in trailing
    # prose after a genuinely complete array must not corrupt an otherwise-good match.
    decoder = json.JSONDecoder()
    start = text.find("[")
    if start != -1:
        try:
            parsed, _ = decoder.raw_decode(text, start)
            if isinstance(parsed, list):
                return parsed, True
        except json.JSONDecodeError:
            pass

    # Fallback: scan for complete top-level objects using the JSON parser itself, not
    # a `{[^{}]*}` regex - that regex explicitly excludes nested braces, so a claim
    # or sources value that isn't a plain string (a real, confirmed cause of
    # ProviderProtocolError, not a guess) silently matched nothing. Runs even when
    # the array is truncated mid-output (no closing ]); keeps every complete object
    # found before the cutoff, same as before.
    objects = []
    pos = 0
    while pos < len(text):
        brace = text.find("{", pos)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
            if isinstance(obj, dict):
                objects.append(obj)
            pos = end
        except json.JSONDecodeError:
            pos = brace + 1
    return objects, bool(objects)


def _parse_json_array(text: str) -> list:
    return _parse_provider_array(text)[0]

_LOW_QUALITY = (
    "facebook.com", "youtube.com", "youtu.be", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "reddit.com", "quora.com",
    "pinterest.com", "threads.net", "medium.com", "linkedin.com",
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

def _domain(url: str) -> str:
    from urllib.parse import urlparse
    netloc = urlparse(url).netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc

def _domain_matches(netloc: str, domain: str) -> bool:
    # Matches the real host, not a substring anywhere in the URL - a query param
    # like "?ref=reuters.com" on a spam domain must NOT count as reuters.com.
    if domain.startswith("."):
        return netloc.endswith(domain)
    return netloc == domain or netloc.endswith("." + domain)

def _filter_sources(results: list[dict]) -> list[dict]:
    # Social/UGC domains are already excluded upstream via Tavily's exclude_domains. Checks for the high and medium quality
    def rank(r):
        netloc = _domain(r["url"])
        for domain in _HIGH_QUALITY:
            if _domain_matches(netloc, domain):
                return 0
        for domain in _MEDIUM_QUALITY:
            if _domain_matches(netloc, domain):
                return 1
        return 2

    results.sort(key=rank)
    return results[:3]

def _score(r: dict) -> float:
    return r.get("score", 0)

def _search(query: str, claim_label: str = "", verbose: bool = False, run_id: str = "") -> tuple[str, list[dict]]:
    # Low-quality/UGC domains excluded at the Tavily, not filtered after the fact check.
    start = time.perf_counter()
    for attempt in range(PROVIDER_MAX_RETRIES + 1):
        try:
            response = _tavily_().search(
                query,
                max_results=10,
                exclude_domains=list(_LOW_QUALITY),
                search_depth="advanced",
                include_raw_content="markdown",
                timeout=TAVILY_TIMEOUT_SECONDS,
            )
            break
        except Exception as exc:
            print(f"[ERROR] Tavily attempt {attempt + 1}: {type(exc).__name__}: {exc}")
            if attempt >= PROVIDER_MAX_RETRIES or not _is_retryable(exc):
                raise
            time.sleep(0.1 * (attempt + 1))
    raw_results = response.get("results", [])

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
        _print_raw_response(
            run_id=run_id, claim_label=claim_label, provider="tavily",
            operation="evidence_search", attempt=attempt + 1,
            elapsed=time.perf_counter() - start, body=response,
            query=query, results_returned=len(raw_results),
            results_accepted=len(accepted), results_rejected=len(rejected),
        )

    # sources[i] and the "[i] ..." block in the evidence text refer to the same
    # source by construction - this index (not the URL) is what the model is
    # asked to cite in source_analysis, so a malformed/truncated URL in the
    # model's output can never misattribute an analysis to the wrong source.
    sources = []
    text_parts = []
    evidence_size = 0
    for r in accepted:
        url = r["url"]
        raw_content = r.get("raw_content")
        is_full_content = isinstance(raw_content, str) and bool(raw_content.strip())
        body = raw_content if is_full_content else r.get("content", "")
        if not isinstance(body, str) or not body.strip():
            continue
        index = len(sources)
        header = f"[{index}] {url}\n"
        separator_size = 2 if text_parts else 0
        remaining = (MAX_EVIDENCE_TOTAL_CHARS - evidence_size
                     - separator_size - len(header))
        if remaining <= 0:
            break
        excerpt = body[:min(MAX_EVIDENCE_PER_SOURCE_CHARS, remaining)]
        sources.append({
            "url": url,
            "title": r.get("title") or None,
            "domain": _domain(url),
            "score": _score(r),
            "content": excerpt,
            "is_full_content": is_full_content,
        })
        text_parts.append(header + excerpt)
        evidence_size += separator_size + len(header) + len(excerpt)

    text = "\n\n".join(text_parts)
    return text, sources

_VALID_STANCES = {"SUPPORTS", "CONTRADICTS", "PARTIAL", "IRRELEVANT", "INSUFFICIENT"}
_VALID_DIRECTNESS = {"DIRECT", "INDIRECT"}

# Confidence ceilings below are deliberately simple, named constants (not a weighted
# formula) - each one is a cap, not a deduction, and callers combine them with min().
_CONFIDENCE_CAP_NO_DIRECT_SOURCE = 75
_CONFIDENCE_CAP_SINGLE_SUPPORT = 85
_CONFIDENCE_CAP_UNRESOLVED_CONTRADICTION = 70
_CONFIDENCE_CAP_SNIPPETS_ONLY = 85

_DUPLICATE_CONTENT_THRESHOLD = 0.85


def _validate_source_analysis(entries, source_count: int) -> list[dict]:
    # Drops malformed entries rather than failing the whole claim or spending a retry -
    # a partially-malformed source_analysis is still useful signal, and retrying costs
    # a real DeepInfra call for something usually recoverable.
    valid = []
    if not isinstance(entries, list):
        return valid
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("source_index")
        if not isinstance(index, int) or not (0 <= index < source_count):
            continue
        stance = str(entry.get("stance", "")).upper()
        if stance not in _VALID_STANCES:
            continue
        directness = str(entry.get("directness", "")).upper()
        if directness not in _VALID_DIRECTNESS:
            directness = "INDIRECT"  # safe default rather than dropping an otherwise-valid entry
        excerpt = entry.get("evidence_excerpt")
        valid.append({
            "source_index": index,
            "stance": stance,
            "directness": directness,
            "reason": str(entry.get("reason", ""))[:300],
            "evidence_excerpt": excerpt if isinstance(excerpt, str) and excerpt.strip() else None,
        })
    return valid


def _aggregate_stance(source_analysis: list[dict]) -> tuple[bool, bool]:
    # This is what makes source_analysis authoritative rather than decorative: supported/
    # contradicted are derived from the validated per-source stances, not trusted from the
    # model's own top-level fields - the same "recompute, don't trust" pattern already used
    # one level up for the final TRUE/FALSE/UNVERIFIABLE verdict.
    supported = any(entry["stance"] == "SUPPORTS" for entry in source_analysis)
    contradicted = any(entry["stance"] == "CONTRADICTS" for entry in source_analysis)
    return supported, contradicted


def _verify_evidence_excerpt(excerpt: str | None, source_content: str) -> bool:
    # Soft hallucination signal, not proof: a false result means the excerpt doesn't
    # appear verbatim (after whitespace normalization) in what was actually retrieved -
    # models often paraphrase even when asked to quote, so this is a warning input,
    # never grounds to discard a stance outright.
    if not excerpt or not source_content:
        return False
    normalize = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    return normalize(excerpt) in normalize(source_content)


def _group_duplicate_sources(sources: list[dict]) -> list[int]:
    # Returns, per source, the index of the first source in its near-duplicate group
    # (itself, if it's first) - groups syndicated/wire-service copies so they don't
    # each count as independent confirmation, without hiding either URL from the
    # user-facing source list. Pure stdlib (difflib), no new dependency.
    group_of = list(range(len(sources)))
    for i in range(len(sources)):
        for j in range(i):
            if group_of[j] != j:
                continue  # only compare against each group's representative
            similarity = difflib.SequenceMatcher(
                None, sources[i].get("content", ""), sources[j].get("content", "")
            ).ratio()
            if similarity >= _DUPLICATE_CONTENT_THRESHOLD:
                group_of[i] = j
                break
    return group_of


def _cap_confidence(confidence: int, *, independent_supports: int, has_contradiction: bool,
                     any_direct: bool, all_snippets: bool) -> int:
    # Ceilings, not deductions: each condition caps the model's own number rather than
    # discarding the result. A single weak signal shouldn't fail a claim outright. Silent
    # by design - no user-facing explanation string is produced; regular users found the
    # warning text more confusing than useful, so only the calibrated number is kept.
    cap = 100
    if not any_direct:
        cap = min(cap, _CONFIDENCE_CAP_NO_DIRECT_SOURCE)
    if independent_supports <= 1:
        cap = min(cap, _CONFIDENCE_CAP_SINGLE_SUPPORT)
    if has_contradiction:
        cap = min(cap, _CONFIDENCE_CAP_UNRESOLVED_CONTRADICTION)
    if all_snippets:
        cap = min(cap, _CONFIDENCE_CAP_SNIPPETS_ONLY)
    return min(int(confidence), cap)


def _verify_one(claim: dict, search_text: str, sources: list[dict], verbose: bool = False,
                 claim_label: str = "", run_id: str = "") -> dict | None:
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
    raw_reply = _chat(VERIFY_PROMPT, context, max_tokens=1000)
    if verbose:
        # One print() call, not several: this claim's verify runs in its own worker thread
        # alongside every other claim's, so bundling done-time + raw text into a single write
        # keeps concurrent claims' raw output from interleaving into a garbled mess on screen.
        _print_raw_response(
            run_id=run_id, claim_label=claim_label or claim["claim"][:60], provider="deepinfra",
            operation="claim_verification", attempt=1,
            elapsed=time.perf_counter() - start, body=raw_reply,
        )

    raw_items, protocol_valid = _parse_provider_array(raw_reply)
    if not protocol_valid:
        # Printed unconditionally (not gated behind verbose) because this is
        # genuinely rare and is exactly the evidence needed to diagnose why
        # parsing failed - without it there's no way to tell what the model
        # actually returned after the fact.
        print(f"[ERROR] Verification protocol: could not parse a JSON array or object "
              f"from the response for {claim['claim'][:60]!r}. Raw response:\n{raw_reply}")
        raise ProviderProtocolError("verification response was not a JSON array")

    parsed = []
    for v in raw_items:
        if isinstance(v, dict) and "supported" in v and "contradicted" in v:
            parsed.append(v)

    if not parsed:
        if raw_items:
            print(f"[ERROR] Verification protocol: parsed {len(raw_items)} object(s) but none "
                  f"had 'supported'/'contradicted' for {claim['claim'][:60]!r}. Raw response:\n{raw_reply}")
            raise ProviderProtocolError("verification response omitted required fields")
        return None
    verdict = parsed[0]

    # source_analysis is what makes supported/contradicted authoritative rather than
    # decorative: aggregated per-source stances become these two fields, not whatever
    # the model returned at the top level directly - otherwise the model could report
    # supported=true while its own source_analysis says every source CONTRADICTS or is
    # IRRELEVANT, and nothing would catch it.
    source_analysis = _validate_source_analysis(verdict.get("source_analysis"), len(sources))
    if source_analysis:
        supported, contradicted = _aggregate_stance(source_analysis)
    else:
        # Graceful degradation: no usable per-source breakdown (missing, or every entry
        # was malformed) - fall back to the model's own top-level fields rather than
        # failing or retrying the whole claim over a partially-malformed response.
        supported = bool(verdict.get("supported"))
        contradicted = bool(verdict.get("contradicted"))

    for entry in source_analysis:
        source = sources[entry["source_index"]]
        entry["evidence_excerpt_valid"] = _verify_evidence_excerpt(
            entry["evidence_excerpt"], source.get("content", "")
        )

    duplicate_group = _group_duplicate_sources(sources)
    independent_supports = len({
        duplicate_group[entry["source_index"]]
        for entry in source_analysis if entry["stance"] == "SUPPORTS"
    })
    any_direct = any(
        entry["directness"] == "DIRECT" and entry["stance"] in ("SUPPORTS", "CONTRADICTS")
        for entry in source_analysis
    )
    all_snippets = bool(sources) and all(not s.get("is_full_content") for s in sources)

    try:
        raw_confidence = int(verdict.get("confidence", 60))
    except (TypeError, ValueError):
        raw_confidence = 60
    confidence = _cap_confidence(
        raw_confidence,
        independent_supports=independent_supports,
        has_contradiction=contradicted,
        any_direct=any_direct,
        all_snippets=all_snippets,
    )

    verdict["supported"] = supported
    verdict["contradicted"] = contradicted
    verdict["source_analysis"] = source_analysis
    # extension/state.js::normalizeSource already falls back title -> name -> domain ->
    # "Source N" for object-shaped sources - including domain here means a source with
    # no Tavily-supplied title shows its domain instead of a generic placeholder.
    verdict["sources"] = [
        {"url": s["url"], "title": s.get("title"), "domain": s.get("domain")} for s in sources
    ]
    verdict["confidence"] = confidence
    # No user-facing "warning" text - removed deliberately, see _cap_confidence's
    # comment. Pop defensively in case the model ever spontaneously includes one
    # (VERIFY_PROMPT never asks for it, so this should be a no-op in practice).
    verdict.pop("warning", None)

    # Deterministic verdict: derived from supported/contradicted (themselves now derived
    # from source_analysis, not trusted from the model directly) rather than trusting the
    # model's own "verdict" field - closes the "no evidence found -> FALSE" failure mode
    # at the code level instead of just asking the model not to do it.
    model_verdict = str(verdict.get("verdict", "")).upper()
    if supported and not contradicted:
        verdict["verdict"] = "TRUE"
    elif contradicted and not supported:
        verdict["verdict"] = "FALSE"
    else:
        verdict["verdict"] = "UNVERIFIABLE"

    # The model's explanation was written to justify model_verdict, not necessarily the recomputed one. If we overrode it, say so, otherwise the label and explanation can
    # read as contradicting each other (e.g. "[TRUE] Why: no direct evidence found").
    if model_verdict and model_verdict != verdict["verdict"]:
        verdict["explanation"] = (verdict.get("explanation", "").rstrip() +
            f" (Verdict corrected to {verdict['verdict']} from the evidence fields.)")

    return verdict

def fact_check(transcript: str, on_result=None, verbose: bool = False, *, on_progress=None,
               cancel_event=None, search_workers: int = SEARCH_WORKERS,
               verify_workers: int = VERIFY_WORKERS) -> FactCheckResult:
    """Extract, search, and verify claims while retaining the legacy list interface."""
    errors: list[dict] = []
    # Only used to label --verbose's raw-response terminal blocks so multiple runs in
    # the same terminal scrollback can be told apart - not a real job/check identity.
    run_id = secrets.token_hex(4)

    def progress(stage: str, **details) -> None:
        _safe_callback(on_progress, {"stage": stage, **details}, "Progress")

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def finish(status: str, values=(), claim_count: int = 0) -> FactCheckResult:
        result = FactCheckResult(values, status=status, claim_count=claim_count, errors=errors)
        progress("complete", status=status, claim_count=claim_count,
                 completed_count=len(result))
        return result

    if cancelled():
        return finish("cancelled")
    if (not isinstance(transcript, str)
            or sum(not char.isspace() for char in transcript) < MIN_INPUT_NON_WHITESPACE):
        print("Input too short to fact-check, give more sentences to fact-check")
        return finish("invalid_input")

    progress("extracting_claims", state="started")
    try:
        start = time.perf_counter()
        if verbose:
            raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=4000)
            _print_raw_response(
                run_id=run_id, claim_label="-", provider="deepinfra",
                operation="claim_extraction", attempt=1,
                elapsed=time.perf_counter() - start, body=raw,
            )
        else:
            with _loading("Extracting claims (Llama 3.3 70B via DeepInfra)"):
                raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=4000)
    except Exception as exc:
        print(f"[ERROR] Extraction: {type(exc).__name__}: {exc}")
        errors.append(_public_provider_error("extraction", exc))
        if _is_rate_limited(exc):
            return finish("rate_limited")
        return finish("timeout" if _is_timeout(exc) else "failed")

    if cancelled():
        return finish("cancelled")

    raw_claims, protocol_valid = _parse_provider_array(raw)
    if not protocol_valid:
        exc = ProviderProtocolError("extraction response was not a JSON array")
        errors.append(_public_provider_error("extraction", exc))
        return finish("failed")

    claims = [
        claim for claim in raw_claims
        if isinstance(claim, dict) and claim.get("claim") and claim.get("query")
    ][:MAX_CLAIMS]
    if raw_claims and not claims:
        exc = ProviderProtocolError("extraction response omitted required fields")
        errors.append(_public_provider_error("extraction", exc))
        return finish("failed")
    progress("extracting_claims", state="completed", claim_count=len(claims))
    if not claims:
        print("No checkable factual claims found, looks like opinion, prediction, or too vague.")
        return finish("no_claims")

    print(f"Found {len(claims)} claim(s).")
    print(f"Verifying {len(claims)} claim(s) (Llama 3.3 70B via DeepInfra)...\n")

    search_limit = max(1, min(int(search_workers), len(claims)))
    verify_limit = max(1, min(int(verify_workers), len(claims)))
    verify_backlog_limit = min(
        len(claims), max(1, VERIFY_BACKLOG_MULTIPLIER * verify_limit)
    )
    search_pool = ThreadPoolExecutor(max_workers=search_limit,
                                     thread_name_prefix="fact-search")
    verify_pool = ThreadPoolExecutor(max_workers=verify_limit,
                                     thread_name_prefix="fact-verify")
    search_futures = {}
    verify_futures = {}
    pending_verifications = []
    next_search = 0
    results: list[dict] = []
    no_evidence_count = 0
    failed_count = 0
    timeout_count = 0
    rate_limited_count = 0
    dropped_count = 0
    was_cancelled = False

    def record_error(stage: str, claim_index: int, exc: Exception) -> None:
        nonlocal failed_count, timeout_count, rate_limited_count
        print(f"[ERROR] {stage.title()}: {type(exc).__name__}: {exc}")
        errors.append(_public_provider_error(stage, exc, claim_index))
        if _is_rate_limited(exc):
            rate_limited_count += 1
        elif _is_timeout(exc):
            timeout_count += 1
        else:
            failed_count += 1

    def submit_searches() -> None:
        nonlocal next_search
        while (not cancelled() and next_search < len(claims)
               and len(search_futures) < search_limit
               and (len(search_futures) + len(verify_futures)
                    + len(pending_verifications)) < verify_backlog_limit):
            claim_index = next_search
            claim = claims[claim_index]
            progress("searching", state="started", claim_index=claim_index,
                     claim_count=len(claims))
            future = search_pool.submit(
                _search, claim["query"], f"{claim_index + 1}/{len(claims)}", verbose, run_id
            )
            search_futures[future] = claim_index
            next_search += 1

    def submit_verifications() -> None:
        while (not cancelled() and pending_verifications
               and len(verify_futures) < verify_backlog_limit):
            claim_index, search_text, sources = pending_verifications.pop(0)
            progress("verifying", state="started", claim_index=claim_index,
                     claim_count=len(claims))
            if cancelled():
                return
            future = verify_pool.submit(
                _verify_one, claims[claim_index], search_text, sources, verbose,
                f"{claim_index + 1}/{len(claims)}", run_id,
            )
            verify_futures[future] = claim_index

    submit_searches()
    try:
        while search_futures or verify_futures or pending_verifications:
            if cancelled():
                was_cancelled = True
                break

            submit_verifications()
            submit_searches()

            done, _ = wait(
                set(search_futures) | set(verify_futures),
                timeout=0.05,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue

            for future in done:
                if cancelled():
                    was_cancelled = True
                    break
                if future in search_futures:
                    claim_index = search_futures.pop(future)
                    progress("searching", state="completed", claim_index=claim_index,
                             claim_count=len(claims))
                    if cancelled():
                        was_cancelled = True
                        break
                    try:
                        search_text, sources = future.result()
                    except Exception as exc:
                        if cancelled():
                            was_cancelled = True
                            break
                        record_error("search", claim_index, exc)
                    else:
                        if cancelled():
                            was_cancelled = True
                            break
                        if not search_text:
                            no_evidence_count += 1
                        else:
                            pending_verifications.append((claim_index, search_text, sources))
                            submit_verifications()
                    submit_searches()
                    continue

                claim_index = verify_futures.pop(future)
                progress("verifying", state="completed", claim_index=claim_index,
                         claim_count=len(claims))
                if cancelled():
                    was_cancelled = True
                    break
                try:
                    verdict = future.result()
                except NoEvidenceError:
                    if cancelled():
                        was_cancelled = True
                        break
                    no_evidence_count += 1
                    submit_searches()
                    continue
                except Exception as exc:
                    if cancelled():
                        was_cancelled = True
                        break
                    record_error("verification", claim_index, exc)
                    submit_searches()
                    continue

                if cancelled():
                    was_cancelled = True
                    break
                if verdict is None:
                    dropped_count += 1
                    submit_searches()
                    continue

                # Provider output cannot change which extracted claim this future belongs to.
                verdict["claim"] = claims[claim_index]["claim"]
                verdict["speaker"] = claims[claim_index].get("speaker", "UNKNOWN")
                verdict["claim_index"] = claim_index
                if cancelled():
                    was_cancelled = True
                    break
                results.append(verdict)
                if not cancelled():
                    _safe_callback(on_result, verdict, "Result")
                submit_searches()
    finally:
        if was_cancelled or cancelled():
            was_cancelled = True
            for future in list(search_futures) + list(verify_futures):
                future.cancel()
            pending_verifications.clear()
            search_pool.shutdown(wait=True, cancel_futures=True)
            verify_pool.shutdown(wait=True, cancel_futures=True)
        else:
            search_pool.shutdown(wait=True, cancel_futures=True)
            verify_pool.shutdown(wait=True, cancel_futures=True)

    if was_cancelled:
        return finish("cancelled", results, len(claims))
    if results and len(results) == len(claims):
        return finish("completed", results, len(claims))
    if results:
        return finish("partial", results, len(claims))
    if rate_limited_count:
        return finish("rate_limited", claim_count=len(claims))
    if timeout_count:
        return finish("timeout", claim_count=len(claims))
    if failed_count:
        return finish("failed", claim_count=len(claims))

    if no_evidence_count + dropped_count == len(claims):
        if dropped_count:
            print(f"{dropped_count} claim(s) dropped (confidence < 60).")
        print(f"Extracted {len(claims)} claim(s), but none could be verified with enough "
              "evidence, no source clearly confirmed or denied them.")
        return finish("no_evidence", claim_count=len(claims))
    return finish("failed", claim_count=len(claims))

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

    # A `{[^{}]*}` regex (the old fallback) cannot match an object containing a
    # nested object/array anywhere in a field - real bug: a live 9/10-claims-checked
    # run raised ProviderProtocolError on the tenth because of exactly this. The
    # fallback now uses json.JSONDecoder.raw_decode so nesting doesn't break it.
    nested = ('[{"claim":"a","verdict":TRUE,"sources":["u1"],'
              '"meta":{"note":"see [1]","weight":2}}]')
    assert len(_parse_json_array(nested)) == 1, "nested object/array in a field must not break parsing"
    assert _parse_json_array(nested)[0]["meta"]["weight"] == 2, "nested value preserved correctly"

    # Same nested-object case, but forcing the object-scan fallback (truncated,
    # no closing ]) rather than the full-array path - the fallback is the one
    # that used the naive non-nesting regex before this fix.
    nested_truncated = ('[{"claim":"a","verdict":TRUE,"sources":["u1"],'
                         '"meta":{"note":"see [1]","weight":2}},'
                         '{"claim":"b","verdict":')
    assert len(_parse_json_array(nested_truncated)) == 1, "fallback scan must handle nesting too"
    assert _parse_json_array(nested_truncated)[0]["meta"]["weight"] == 2, "fallback preserves nested value"

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

    # rank() must match the real host, not any substring in the URL - a spam domain
    # stuffing a trusted name into a query param must not be ranked as high-quality
    # (real bug: "domain in r['url']" matched "reuters.com" inside a ?ref= param).
    spoofed = [{"url": "https://spam.com/?ref=reuters.com"}, {"url": "https://reuters.com/real"}]
    ranked_spoofed = _filter_sources(spoofed)
    assert ranked_spoofed[0]["url"] == "https://reuters.com/real", "real host must outrank a spoofed query param"

    # Empty search evidence must raise NoEvidenceError before ever calling the LLM (real bug found live: with search failing entirely, the model answered from its own training knowledge and fabricated citations instead of admitting no evidence was found).
    raised_no_evidence = False
    try:
        _verify_one({"claim": "Earth is square", "speaker": "X"}, "", [])
    except NoEvidenceError:
        raised_no_evidence = True
    assert raised_no_evidence, "empty search evidence must raise NoEvidenceError"

    # _validate_source_analysis: malformed entries are dropped, not fatal.
    valid_entries = _validate_source_analysis([
        {"source_index": 0, "stance": "supports", "directness": "direct", "reason": "x", "evidence_excerpt": "q"},
        {"source_index": 5, "stance": "SUPPORTS"},          # out-of-range index, dropped
        {"source_index": 1, "stance": "MAYBE"},              # invalid stance, dropped
        "not even a dict",                                    # dropped
        {"source_index": 1, "stance": "CONTRADICTS"},         # missing directness, defaults to INDIRECT
    ], source_count=2)
    assert len(valid_entries) == 2, "malformed source_analysis entries must be dropped, not fatal"
    assert valid_entries[0]["stance"] == "SUPPORTS", "stance is uppercased"
    assert valid_entries[1]["directness"] == "INDIRECT", "missing directness defaults safely"
    assert _validate_source_analysis(None, source_count=3) == [], "non-list input never raises"
    assert _validate_source_analysis("not a list", source_count=3) == [], "non-list input never raises"

    # _aggregate_stance is what makes source_analysis authoritative: it must not just
    # echo whatever the model's own top-level supported/contradicted said.
    assert _aggregate_stance([{"stance": "SUPPORTS"}, {"stance": "IRRELEVANT"}]) == (True, False)
    assert _aggregate_stance([{"stance": "CONTRADICTS"}, {"stance": "INSUFFICIENT"}]) == (False, True)
    assert _aggregate_stance([{"stance": "SUPPORTS"}, {"stance": "CONTRADICTS"}]) == (True, True)
    assert _aggregate_stance([{"stance": "IRRELEVANT"}]) == (False, False), "topical relevance alone is not support"
    assert _aggregate_stance([]) == (False, False)

    # _verify_evidence_excerpt: a soft signal, whitespace-normalized substring check.
    assert _verify_evidence_excerpt("a 50% tariff on steel", "Reports say a  50%\ntariff on steel imports.")
    assert not _verify_evidence_excerpt("a 90% tariff on steel", "Reports say a 50% tariff on steel imports.")
    assert not _verify_evidence_excerpt(None, "some content")
    assert not _verify_evidence_excerpt("quote", "")

    # _group_duplicate_sources: near-identical content groups together (syndicated
    # copies), clearly different content does not.
    syndicated = [
        {"content": "The president announced a 50% tariff on steel imports today."},
        {"content": "The president announced a 50% tariff on steel imports today, officials said."},
        {"content": "Meanwhile, the central bank left interest rates unchanged this week."},
    ]
    groups = _group_duplicate_sources(syndicated)
    assert groups[0] == groups[1], "near-identical wire copies must group together"
    assert groups[2] != groups[0], "unrelated content must not be grouped"

    # _cap_confidence: caps the model's own number silently - no explanatory string,
    # by design (see the function's own comment).
    capped = _cap_confidence(98, independent_supports=2, has_contradiction=False,
                              any_direct=True, all_snippets=False)
    assert capped == 98, "strong direct evidence is not capped"
    capped = _cap_confidence(98, independent_supports=0, has_contradiction=False,
                              any_direct=False, all_snippets=False)
    assert capped <= _CONFIDENCE_CAP_NO_DIRECT_SOURCE, "no direct source must cap confidence"
    capped = _cap_confidence(98, independent_supports=1, has_contradiction=True,
                              any_direct=True, all_snippets=False)
    assert capped <= _CONFIDENCE_CAP_UNRESOLVED_CONTRADICTION, "unresolved contradiction caps hardest"

    # _print_raw_response: terminal-only debug output, gated by the caller's own
    # `if verbose:` checks (this function itself has no gate) - confirms the required
    # fields render and that a genuinely huge body actually gets truncated, not just
    # decorated with a marker on top of the full text.
    captured = io.StringIO()
    with redirect_stdout(captured):
        _print_raw_response(run_id="test1234", claim_label="1/1", provider="test",
                             operation="test_op", attempt=1, elapsed=0.5, body="x" * 20_000,
                             extra_field="present")
    output = captured.getvalue()
    assert "run_id: test1234" in output and "extra_field: present" in output
    assert "...truncated (" in output, "a body over the cap must be truncated with a marker"
    assert len(output) < 20_000 + 1_000, "truncation must actually shrink a huge body, not just append a marker"

    captured = io.StringIO()
    with redirect_stdout(captured):
        _print_raw_response(run_id="t", claim_label="-", provider="test", operation="test_op",
                             attempt=1, elapsed=0.1, body={"a": 1})
    assert '"a": 1' in captured.getvalue(), "a non-string body must be pretty-printed as JSON"

    print("OK: all self-checks pass")
    

