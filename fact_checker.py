import json
import copy
import difflib
import logging
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from providers import ProviderContext

logger = logging.getLogger(__name__)

SEARCH_WORKERS = 4
VERIFY_WORKERS = 3
MAX_CLAIMS = 15
MIN_INPUT_NON_WHITESPACE = 10
DEEPINFRA_TIMEOUT_SECONDS = 40.0
TAVILY_TIMEOUT_SECONDS = 15.0
WHOLE_JOB_DEADLINE_SECONDS = 300.0
CLAIM_RETRY_DEADLINE_SECONDS = 60.0
PROVIDER_MAX_RETRIES = 2
MAX_EVIDENCE_PER_SOURCE_CHARS = 4_000
MAX_EVIDENCE_TOTAL_CHARS = 10_000
VERIFY_BACKLOG_MULTIPLIER = 2
DEEPINFRA_REASONING_EFFORT = "none"

_LANGUAGE_MARKERS = {
    "English": frozenset({
        "the", "and", "are", "is", "was", "were", "this", "that", "these",
        "those", "to", "from", "for", "with", "without", "into", "because",
        "when", "which", "can", "cannot", "could", "would", "should", "will",
        "has", "have", "had", "you", "your", "their", "our", "not", "claim",
        "evidence", "source", "supports", "contradicts",
    }),
    "Indonesian": frozenset({
        "yang", "dan", "adalah", "merupakan", "ini", "itu", "tersebut", "dengan",
        "tanpa", "karena", "ketika", "dari", "untuk", "pada", "dalam", "dapat",
        "bisa", "tidak", "bukan", "akan", "telah", "sudah", "memiliki", "klaim",
        "bukti", "sumber", "menyatakan", "namun", "tetapi", "secara", "bahwa",
        "oleh", "hanya", "mendukung", "membantah",
    }),
}

class NoEvidenceError(Exception):
    # Raised when a claim has zero search evidence to verify against (search failed or returned nothing usable). 
    pass


class ProviderProtocolError(Exception):
    """Raised when provider output does not satisfy the expected JSON protocol."""


class WholeJobDeadlineExceeded(TimeoutError):
    """Raised when the complete fact-check has exhausted its wall-clock budget."""


class FactCheckResult(list):
    """List-compatible pipeline result with enough metadata for service adapters."""

    def __init__(self, values=(), *, status: str = "completed", claim_count: int = 0,
                 errors: list[dict] | None = None,
                 usage: dict | None = None):
        super().__init__(values)
        self.status = status
        self.claim_count = claim_count
        self.errors = errors or []
        self.usage = usage or {}


def _safe_callback(callback, value, label: str) -> None:
    if callback is None:
        return
    try:
        callback(copy.deepcopy(value))
    except Exception as exc:
        logger.warning("%s callback failed (%s)", label, type(exc).__name__)


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


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WholeJobDeadlineExceeded("The fact-check exceeded its time limit.")
    return remaining


def _bounded_timeout(limit: float, deadline: float | None) -> float:
    remaining = _remaining_seconds(deadline)
    return limit if remaining is None else min(limit, remaining)


def _sleep_before_retry(attempt: int, deadline: float | None) -> None:
    delay = 0.1 * (attempt + 1)
    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining <= delay:
        raise WholeJobDeadlineExceeded("The fact-check exceeded its time limit.")
    time.sleep(delay)


def _public_provider_error(stage: str, exc: Exception,
                           claim_index: int | None = None) -> dict:
    if isinstance(exc, WholeJobDeadlineExceeded):
        code, message = "job_deadline_exceeded", "The fact-check exceeded its time limit."
    elif _is_rate_limited(exc):
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

MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"

EXTRACT_PROMPT = """Extract up to 15 most specific and verifiable factual claims from the text.
Return a JSON array. Each item must have:
  "claim": a faithful, self-contained version of the factual claim. Preserve names, numbers,
           units, dates, and the source's wording wherever possible so the claim can still be
           located in the original text. Remove rhetorical intensifiers or subjective framing
           (such as "dangerous metabolic tidal wave", "violent", or "brutal") that cannot be
           independently verified. Never add a fact or inference that the source did not state.
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
If one sentence mixes checkable facts with rhetoric, extract only the factual core. If it combines
independent factual assertions that could receive different verdicts, split them into separate
claims. Keep tightly related quantities together only when they describe one calculation or whole.
Also skip routine procedural narration the source already states as plain, undisputed fact (a plea
entered, a filing date, a standard step in a legal/administrative process), UNLESS it contains a
specific number, quote, or attribution that could plausibly be misreported. Routine narration has
near-zero misinformation risk and isn't worth a search+verify call.

If two or more figures are stated as complementary parts of one whole (a percentage split, a
budget breakdown, a ratio that sums to a total), extract them as ONE claim describing the full
breakdown, not one claim per figure. e.g. for "60% from nilai manfaat, 40% from Bipih" extract
a single claim "the scheme is 60% nilai manfaat and 40% Bipih", not two separate claims. Splitting
them risks one being verified TRUE and the other FALSE even though they're the same fact.
For a claim about a named authority's limit, guideline, or recommendation, make the query target
that authority's official guidance and include the quantity being compared. For a calculated claim,
query for the underlying per-unit values and the named benchmark, not the dramatic conclusion.
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
                 language the search results/sources are in. Write each source_analysis "reason"
                 in that language too.
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

CALCULATIONS AND GUIDELINE COMPARISONS:
Perform basic arithmetic and standard unit conversions when the accepted evidence supplies the
inputs. A source that states a per-unit amount can directly support a claimed total for multiple
identical units; do not mark the total UNVERIFIABLE merely because the source did not print the
multiplication result. Show the calculation briefly in the explanation. For example, four packages
with 25 g each directly support a 100 g total. Likewise, recalculate a percentage comparison before
choosing a stance: 87 compared with a limit of 30 is 290%, which reasonably supports
"nearly 300%."

When a claim names an organization's recommendation, distinguish its main recommendation from a
stricter conditional or aspirational target. Judge the claim against the benchmark it actually
names; do not silently replace that benchmark with a different recommendation. Before returning,
check that every calculation in the explanation agrees with the selected source stances,
supported/contradicted fields, and verdict.

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


EXTRACT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "extracted_claims",
        "strict": True,
        "schema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "query": {"type": "string"},
                    "speaker": {"type": "string"},
                },
                "required": ["claim", "query", "speaker"],
                "additionalProperties": False,
            },
        },
    },
}

VERIFY_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "claim_verification",
        "strict": True,
        "schema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "claim": {"type": "string"},
                    "supported": {"type": "boolean"},
                    "contradicted": {"type": "boolean"},
                    "verdict": {
                        "type": "string",
                        "enum": ["TRUE", "FALSE", "UNVERIFIABLE"],
                    },
                    "confidence": {"type": "integer", "minimum": 60, "maximum": 100},
                    "explanation": {"type": "string"},
                    "source_analysis": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_index": {"type": "integer", "minimum": 0},
                                "stance": {
                                    "type": "string",
                                    "enum": [
                                        "SUPPORTS", "CONTRADICTS", "PARTIAL",
                                        "IRRELEVANT", "INSUFFICIENT",
                                    ],
                                },
                                "directness": {
                                    "type": "string",
                                    "enum": ["DIRECT", "INDIRECT"],
                                },
                                "reason": {"type": "string"},
                                "evidence_excerpt": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                },
                            },
                            "required": [
                                "source_index", "stance", "directness", "reason",
                                "evidence_excerpt",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "speaker", "claim", "supported", "contradicted", "verdict",
                    "confidence", "explanation", "source_analysis",
                ],
                "additionalProperties": False,
            },
        },
    },
}


# DeepInfra client (OpenAI-compatible)
def _read_chat_stream(stream, deadline: float | None) -> tuple[str, object | None]:
    parts: list[str] = []
    usage = None
    for event in stream:
        if deadline is not None:
            _remaining_seconds(deadline)
        event_usage = getattr(event, "usage", None)
        if event_usage is not None:
            usage = event_usage
        choices = getattr(event, "choices", None)
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts), usage


def _chat(system: str, user: str, max_tokens: int, *, deadline: float | None = None,
          response_format: dict | None = None,
          provider_context: ProviderContext | None = None,
          stage: str = "unknown", claim_index: int | None = None) -> str:
    providers = provider_context or ProviderContext.from_environment()
    for attempt in range(PROVIDER_MAX_RETRIES + 1):
        try:
            slot_timeout = (
                _bounded_timeout(DEEPINFRA_TIMEOUT_SECONDS, deadline)
                if providers.concurrency_limited
                else None
            )
            with providers.deepinfra_slot(slot_timeout):
                request = {
                    "model": MODEL,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "extra_body": {"reasoning_effort": DEEPINFRA_REASONING_EFFORT},
                    "stream": True,
                    "timeout": _bounded_timeout(DEEPINFRA_TIMEOUT_SECONDS, deadline),
                }
                if response_format is not None:
                    request["response_format"] = response_format
                stream = providers.deepinfra_client(
                    base_url=DEEPINFRA_BASE_URL,
                    timeout=DEEPINFRA_TIMEOUT_SECONDS,
                ).chat.completions.create(**request)
                with stream:
                    content, usage = _read_chat_stream(stream, deadline)
            providers.usage.record_deepinfra(
                model=MODEL,
                stage=stage,
                claim_index=claim_index,
                attempt=attempt + 1,
                usage=usage,
                succeeded=True,
            )
            return content
        except Exception as exc:
            providers.usage.record_deepinfra(
                model=MODEL,
                stage=stage,
                claim_index=claim_index,
                attempt=attempt + 1,
                succeeded=False,
            )
            logger.warning(
                "DeepInfra attempt %d failed (%s)",
                attempt + 1,
                type(exc).__name__,
            )
            if (isinstance(exc, WholeJobDeadlineExceeded)
                    or attempt >= PROVIDER_MAX_RETRIES
                    or not _is_retryable(exc)):
                raise
            _sleep_before_retry(attempt, deadline)
    raise AssertionError("DeepInfra retry loop exhausted without returning or raising.")

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

def _search(
    query: str,
    deadline: float | None = None,
    provider_context: ProviderContext | None = None,
    claim_index: int | None = None,
) -> tuple[str, list[dict]]:
    # Low-quality/UGC domains excluded at the Tavily, not filtered after the fact check.
    providers = provider_context or ProviderContext.from_environment()
    for attempt in range(PROVIDER_MAX_RETRIES + 1):
        try:
            slot_timeout = (
                _bounded_timeout(TAVILY_TIMEOUT_SECONDS, deadline)
                if providers.concurrency_limited
                else None
            )
            with providers.tavily_slot(slot_timeout):
                response = providers.tavily_client().search(
                    query,
                    max_results=10,
                    exclude_domains=list(_LOW_QUALITY),
                    search_depth="advanced",
                    include_raw_content="markdown",
                    timeout=_bounded_timeout(TAVILY_TIMEOUT_SECONDS, deadline),
                )
            providers.usage.record_tavily(
                stage="search",
                claim_index=claim_index,
                attempt=attempt + 1,
                succeeded=True,
                search_depth="advanced",
            )
            break
        except Exception as exc:
            providers.usage.record_tavily(
                stage="search",
                claim_index=claim_index,
                attempt=attempt + 1,
                succeeded=False,
                search_depth="advanced",
            )
            logger.warning(
                "Tavily attempt %d failed (%s)",
                attempt + 1,
                type(exc).__name__,
            )
            if (isinstance(exc, WholeJobDeadlineExceeded)
                    or attempt >= PROVIDER_MAX_RETRIES
                    or not _is_retryable(exc)):
                raise
            _sleep_before_retry(attempt, deadline)
    raw_results = response.get("results", [])

    passed = []
    for r in raw_results:
        if _score(r) >= _MIN_SCORE:
            passed.append(r)

    accepted = _filter_sources(passed)

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


def _one_based_source_references(explanation, source_count: int) -> str:
    """Convert the model's zero-based evidence citations for display."""
    text = explanation if isinstance(explanation, str) else ""

    def replace(match: re.Match) -> str:
        index = int(match.group(1))
        return f"[{index + 1}]" if index < source_count else match.group(0)

    return re.sub(r"\[(\d+)\]", replace, text)


def _detect_supported_language(text, *, minimum_score: int = 3) -> str | None:
    """Identify clear English/Indonesian prose without adding a runtime dependency."""
    if not isinstance(text, str):
        return None
    words = re.findall(r"[^\W\d_]+", text.lower(), flags=re.UNICODE)
    scores = {
        language: sum(word in markers for word in words)
        for language, markers in _LANGUAGE_MARKERS.items()
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (language, score), (_, runner_up) = ranked
    if score < minimum_score or score - runner_up < 2:
        return None
    return language


def _source_reference_list(indices: list[int], language: str) -> str:
    references = [f"[{index}]" for index in indices]
    if len(references) < 2:
        return references[0] if references else ""
    conjunction = " dan " if language == "Indonesian" else " and "
    return ", ".join(references[:-1]) + conjunction + references[-1]


def _language_safe_explanation(verdict: str, source_analysis: list[dict],
                               language: str) -> str:
    """Produce a truthful fallback when model prose uses the wrong language."""
    if verdict == "TRUE":
        indices = [
            entry["source_index"] for entry in source_analysis
            if entry["stance"] == "SUPPORTS"
        ]
        references = _source_reference_list(indices, language)
        if language == "Indonesian":
            location = f" dalam sumber {references}" if references else ""
            return f"Bukti yang diterima{location} secara langsung mendukung klaim ini."
        location = f" in source {references}" if references else ""
        return f"The accepted evidence{location} directly supports this claim."

    if verdict == "FALSE":
        indices = [
            entry["source_index"] for entry in source_analysis
            if entry["stance"] == "CONTRADICTS"
        ]
        references = _source_reference_list(indices, language)
        if language == "Indonesian":
            location = f" dalam sumber {references}" if references else ""
            return f"Bukti yang diterima{location} secara langsung membantah klaim ini."
        location = f" in source {references}" if references else ""
        return f"The accepted evidence{location} directly contradicts this claim."

    partial_indices = [
        entry["source_index"] for entry in source_analysis
        if entry["stance"] == "PARTIAL"
    ]
    references = _source_reference_list(partial_indices, language)
    if language == "Indonesian":
        if references:
            return (f"Sumber {references} hanya membahas sebagian klaim, sehingga keseluruhan "
                    "klaim tidak dapat diverifikasi.")
        return ("Sumber yang tersedia tidak secara langsung membuktikan atau membantah "
                "keseluruhan klaim ini.")
    if references:
        return (f"Source {references} addresses only part of the claim, so the full claim "
                "cannot be verified.")
    return ("The available sources do not directly establish or contradict the full claim, "
            "so it remains unverifiable.")


def _no_evidence_verdict(claim: dict, claim_index: int,
                         document_language: str | None = None) -> dict:
    """Represent an evidence miss explicitly instead of leaving a blank claim slot."""
    language = _detect_supported_language(claim.get("claim")) or document_language or "English"
    if language == "Indonesian":
        explanation = ("Tidak ditemukan bukti yang cukup relevan, sehingga klaim ini tidak "
                       "dapat diverifikasi.")
    else:
        explanation = ("No sufficiently relevant evidence was retrieved, so this claim cannot "
                       "be verified.")
    return {
        "claim": claim["claim"],
        "speaker": claim.get("speaker", "UNKNOWN"),
        "claim_index": claim_index,
        "supported": False,
        "contradicted": False,
        "verdict": "UNVERIFIABLE",
        "confidence": 60,
        "explanation": explanation,
        "source_analysis": [],
        "sources": [],
    }


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


def _verify_one(claim: dict, search_text: str, sources: list[dict],
                 deadline: float | None = None,
                 document_language: str | None = None,
                 provider_context: ProviderContext | None = None,
                 claim_index: int | None = None) -> dict | None:
    # One verify call per claim: keeps each verdict paired with its own search results (no positional zip drift) and lets callers reveal results as they land.
    if not search_text:
        # No search evidence at all (search failed or returned zero usable sources). Do not
        # send an empty evidence block to the model - verified live that it will still answer
        # confidently from its own training knowledge and fabricate source URLs that were
        # never actually retrieved. Raise instead of guessing; the caller's existing per-claim
        # error handling drops this claim from the results rather than showing a fake verdict.
        raise NoEvidenceError(f"no search evidence for claim: {claim['claim'][:60]!r}")

    required_language = (
        _detect_supported_language(claim["claim"]) or document_language
    )
    language_instruction = (
        f"REQUIRED OUTPUT LANGUAGE: {required_language}. Write all generated prose in "
        f"{required_language}, even if the sources use another language.\n"
        if required_language else
        "REQUIRED OUTPUT LANGUAGE: exactly match the language of the CLAIM, not the sources.\n"
    )
    context = (f"SPEAKER: {claim.get('speaker', 'UNKNOWN')}\n"
               f"CLAIM: {claim['claim']}\n"
               f"SEARCH RESULTS:\n{search_text}\n\n"
               f"{language_instruction}")
    raw_reply = _chat(
        VERIFY_PROMPT,
        context,
        max_tokens=1000,
        deadline=deadline,
        response_format=VERIFY_RESPONSE_FORMAT,
        provider_context=provider_context,
        stage="verification",
        claim_index=claim_index,
    )
    raw_items, protocol_valid = _parse_provider_array(raw_reply)
    if not protocol_valid:
        logger.error(
            "Verification protocol returned no JSON array or object."
        )
        raise ProviderProtocolError("verification response was not a JSON array")

    parsed = []
    for v in raw_items:
        if isinstance(v, dict) and "supported" in v and "contradicted" in v:
            parsed.append(v)

    if not parsed:
        if raw_items:
            logger.error(
                "Verification protocol parsed %d object(s), but none had "
                "supported/contradicted.",
                len(raw_items),
            )
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

    explanation = verdict.get("explanation")
    explanation_language = _detect_supported_language(explanation, minimum_score=2)
    if (required_language and
            (not isinstance(explanation, str) or not explanation.strip()
             or (explanation_language and explanation_language != required_language))):
        verdict["explanation"] = _language_safe_explanation(
            verdict["verdict"], source_analysis, required_language
        )

    verdict["explanation"] = _one_based_source_references(
        verdict.get("explanation"), len(sources)
    )

    return verdict

def fact_check(transcript: str, on_result=None, *, on_progress=None,
               on_claims=None, on_evidence=None, cancel_event=None,
               search_workers: int = SEARCH_WORKERS,
               verify_workers: int = VERIFY_WORKERS,
               deadline: float | None = None,
               provider_context: ProviderContext | None = None) -> FactCheckResult:
    """Extract, search, and verify claims while retaining the legacy list interface."""
    errors: list[dict] = []
    providers = provider_context or ProviderContext.from_environment()
    if deadline is None:
        deadline = time.monotonic() + WHOLE_JOB_DEADLINE_SECONDS
    deadline_reported = False

    def progress(stage: str, **details) -> None:
        _safe_callback(on_progress, {"stage": stage, **details}, "Progress")

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def deadline_expired() -> bool:
        return time.monotonic() >= deadline

    def report_deadline() -> None:
        nonlocal deadline_reported
        if deadline_reported:
            return
        deadline_reported = True
        errors.append(_public_provider_error("pipeline", WholeJobDeadlineExceeded()))

    def finish(status: str, values=(), claim_count: int = 0) -> FactCheckResult:
        result = FactCheckResult(
            values,
            status=status,
            claim_count=claim_count,
            errors=errors,
            usage=providers.usage.snapshot(),
        )
        progress("complete", status=status, claim_count=claim_count,
                 completed_count=len(result))
        return result

    if cancelled():
        return finish("cancelled")
    if deadline_expired():
        report_deadline()
        return finish("timeout")
    if (not isinstance(transcript, str)
            or sum(not char.isspace() for char in transcript) < MIN_INPUT_NON_WHITESPACE):
        return finish("invalid_input")

    document_language = _detect_supported_language(transcript)

    progress("extracting_claims", state="started")
    try:
        raw = _chat(
            EXTRACT_PROMPT,
            transcript,
            max_tokens=4000,
            deadline=deadline,
            response_format=EXTRACT_RESPONSE_FORMAT,
            provider_context=providers,
            stage="extraction",
        )
    except Exception as exc:
        logger.warning("Claim extraction failed (%s)", type(exc).__name__)
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
    _safe_callback(on_claims, [dict(claim) for claim in claims], "Claims")
    if not claims:
        return finish("no_claims")
    if deadline_expired():
        report_deadline()
        return finish("timeout", claim_count=len(claims))

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
    deadline_reached = False

    def record_error(stage: str, claim_index: int, exc: Exception) -> None:
        nonlocal failed_count, timeout_count, rate_limited_count
        logger.warning("%s failed (%s)", stage.title(), type(exc).__name__)
        errors.append(_public_provider_error(stage, exc, claim_index))
        if _is_rate_limited(exc):
            rate_limited_count += 1
        elif _is_timeout(exc):
            timeout_count += 1
        else:
            failed_count += 1

    def submit_searches() -> None:
        nonlocal next_search
        while (not cancelled() and not deadline_expired() and next_search < len(claims)
               and len(search_futures) < search_limit
               and (len(search_futures) + len(verify_futures)
                    + len(pending_verifications)) < verify_backlog_limit):
            claim_index = next_search
            claim = claims[claim_index]
            progress("searching", state="started", claim_index=claim_index,
                     claim_count=len(claims))
            future = search_pool.submit(
                _search, claim["query"], deadline, providers, claim_index,
            )
            search_futures[future] = claim_index
            next_search += 1

    def submit_verifications() -> None:
        while (not cancelled() and not deadline_expired() and pending_verifications
               and len(verify_futures) < verify_backlog_limit):
            claim_index, search_text, sources = pending_verifications.pop(0)
            progress("verifying", state="started", claim_index=claim_index,
                     claim_count=len(claims))
            if cancelled():
                return
            future = verify_pool.submit(
                _verify_one, claims[claim_index], search_text, sources,
                deadline, document_language, providers, claim_index,
            )
            verify_futures[future] = claim_index

    submit_searches()
    try:
        while (next_search < len(claims) or search_futures
               or verify_futures or pending_verifications):
            if cancelled():
                was_cancelled = True
                break

            submit_verifications()
            submit_searches()

            active_futures = set(search_futures) | set(verify_futures)
            remaining = max(0.0, deadline - time.monotonic())
            done, _ = wait(
                active_futures,
                timeout=min(0.05, remaining),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                if deadline_expired():
                    deadline_reached = True
                    report_deadline()
                    break
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
                    except WholeJobDeadlineExceeded:
                        deadline_reached = True
                        report_deadline()
                        continue
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
                            verdict = _no_evidence_verdict(
                                claims[claim_index], claim_index, document_language
                            )
                            results.append(verdict)
                            if not cancelled():
                                _safe_callback(on_result, verdict, "Result")
                        else:
                            _safe_callback(on_evidence, {
                                "claim_index": claim_index,
                                "search_text": search_text,
                                "sources": sources,
                            }, "Evidence")
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
                    verdict = _no_evidence_verdict(
                        claims[claim_index], claim_index, document_language
                    )
                    results.append(verdict)
                    if not cancelled():
                        _safe_callback(on_result, verdict, "Result")
                    submit_searches()
                    continue
                except WholeJobDeadlineExceeded:
                    deadline_reached = True
                    report_deadline()
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
            if deadline_reached:
                break
    finally:
        if was_cancelled or cancelled() or deadline_reached:
            was_cancelled = True
            for future in list(search_futures) + list(verify_futures):
                future.cancel()
            pending_verifications.clear()
            search_pool.shutdown(wait=True, cancel_futures=True)
            verify_pool.shutdown(wait=True, cancel_futures=True)
        else:
            search_pool.shutdown(wait=True, cancel_futures=True)
            verify_pool.shutdown(wait=True, cancel_futures=True)

    if deadline_reached:
        return finish("partial" if results else "timeout", results, len(claims))
    if was_cancelled:
        return finish("cancelled", results, len(claims))
    if len(results) == len(claims) and no_evidence_count == len(claims):
        return finish("no_evidence", results, len(claims))
    if len(results) == len(claims):
        return finish("completed", results, len(claims))
    if results:
        return finish("partial", results, len(claims))
    if rate_limited_count:
        return finish("rate_limited", claim_count=len(claims))
    if timeout_count:
        return finish("timeout", claim_count=len(claims))
    if failed_count:
        return finish("failed", claim_count=len(claims))

    return finish("no_evidence", claim_count=len(claims))


def retry_claim(claim: dict, claim_index: int, *, evidence: dict | None = None,
                on_progress=None, on_evidence=None, cancel_event=None,
                deadline: float | None = None,
                provider_context: ProviderContext | None = None) -> FactCheckResult:
    """Retry one previously extracted claim without running extraction again."""
    providers = provider_context or ProviderContext.from_environment()
    if deadline is None:
        deadline = time.monotonic() + CLAIM_RETRY_DEADLINE_SECONDS

    def finish(status: str, values=(), errors=()) -> FactCheckResult:
        result = FactCheckResult(
            values,
            status=status,
            claim_count=1,
            errors=list(errors),
            usage=providers.usage.snapshot(),
        )
        _safe_callback(on_progress, {
            "stage": "complete",
            "status": status,
            "claim_index": claim_index,
            "completed_count": len(result),
        }, "Progress")
        return result

    def failed(stage: str, exc: Exception) -> FactCheckResult:
        error = _public_provider_error(stage, exc, claim_index)
        if _is_rate_limited(exc):
            status = "rate_limited"
        elif _is_timeout(exc):
            status = "timeout"
        else:
            status = "failed"
        return finish(status, errors=[error])

    if (not isinstance(claim, dict) or not claim.get("claim")
            or not claim.get("query") or not isinstance(claim_index, int)
            or claim_index < 0):
        return failed("verification", ProviderProtocolError("invalid saved claim"))
    if cancel_event is not None and cancel_event.is_set():
        return finish("cancelled")

    search_text = evidence.get("search_text") if isinstance(evidence, dict) else None
    sources = evidence.get("sources") if isinstance(evidence, dict) else None
    if not isinstance(search_text, str) or not search_text.strip() or not isinstance(sources, list):
        _safe_callback(on_progress, {
            "stage": "searching", "state": "started", "claim_index": claim_index,
        }, "Progress")
        try:
            search_text, sources = _search(
                claim["query"],
                deadline=deadline,
                provider_context=providers,
                claim_index=claim_index,
            )
        except Exception as exc:
            return failed("search", exc)
        _safe_callback(on_progress, {
            "stage": "searching", "state": "completed", "claim_index": claim_index,
        }, "Progress")
        if not search_text:
            return finish("no_evidence", errors=[{
                "stage": "search",
                "code": "claim_no_evidence",
                "message": "No sufficient evidence was found for this claim.",
                "claim_index": claim_index,
            }])
        _safe_callback(on_evidence, {
            "claim_index": claim_index,
            "search_text": search_text,
            "sources": sources,
        }, "Evidence")

    if cancel_event is not None and cancel_event.is_set():
        return finish("cancelled")

    _safe_callback(on_progress, {
        "stage": "verifying", "state": "started", "claim_index": claim_index,
    }, "Progress")
    try:
        verdict = _verify_one(
            claim,
            search_text,
            sources,
            deadline=deadline,
            provider_context=providers,
            claim_index=claim_index,
        )
    except NoEvidenceError:
        return finish("no_evidence", errors=[{
            "stage": "verification",
            "code": "claim_no_evidence",
            "message": "No sufficient evidence was found for this claim.",
            "claim_index": claim_index,
        }])
    except Exception as exc:
        return failed("verification", exc)
    if cancel_event is not None and cancel_event.is_set():
        return finish("cancelled")
    if verdict is None:
        return failed(
            "verification", ProviderProtocolError("verification returned no verdict")
        )

    verdict["claim"] = claim["claim"]
    verdict["speaker"] = claim.get("speaker", "UNKNOWN")
    verdict["claim_index"] = claim_index
    _safe_callback(on_progress, {
        "stage": "verifying", "state": "completed", "claim_index": claim_index,
    }, "Progress")
    return finish("completed", [verdict])
