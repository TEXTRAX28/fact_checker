import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

# DeepInfra API (OpenAI-compatible)
_deepinfra_client = None
_tavily = None

MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"

EXTRACT_PROMPT = """Extract the 10 most specific and verifiable factual claims from the text.
Return a JSON array. Each item must have:
  "claim": the exact claim as stated
  "query": a short search query that targets the underlying FACT, not just the names in the claim.
           e.g. for "X is president of Indonesia" use "current president of Indonesia" so the real
           answer is findable and the claim can be disproved if false.
  "speaker": the name or label of who made the claim (e.g. "Senator Davis", "SPEAKER_A") — use "UNKNOWN" only if truly unidentifiable

Only include: statistics, numbers, dates, named events, quotes, scientific/medical/legal/historical facts.
Skip: opinions, predictions, vague statements, rhetorical questions.
Return [] if nothing is checkable.
Return ONLY the JSON array, no other text."""

VERIFY_PROMPT = """You are a fact-checker. Output ONLY a JSON array. No markdown. No analysis. No prose. Just the JSON array.

Each object in the array must have:
  "speaker": the speaker label
  "claim": the original claim text
  "verdict": one of TRUE / MOSTLY TRUE / PARTLY TRUE / MISLEADING / UNVERIFIABLE / FALSE
  "confidence": integer 60-100
  "explanation": 1-3 sentences
  "sources": array of URLs from the search results

Verdict definitions (pick the most precise one — don't collapse everything to TRUE/FALSE):
  TRUE = fully supported by the sources
  MOSTLY TRUE = core claim is right but a detail is off or a minor nuance is missing
  PARTLY TRUE = part is supported and part is wrong or unsupported
  MISLEADING = technically true but framed deceptively, or missing context that changes its meaning
  UNVERIFIABLE = sources conflict, or none directly address the claim
  FALSE = directly contradicted by the sources

If the claim is about a CURRENT or ONGOING state — vote counts, who currently holds an office,
live negotiations, present-day support for a bill — and the sources do not contain recent, direct
evidence for it, return UNVERIFIABLE. Do NOT infer a verdict from general or historical information.

Omit any claim where confidence would be below 60.

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

def _chat(system: str, user: str, max_tokens: int = 1000) -> str:
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
    # quote bare enums; multi-word ones listed first so they win over the TRUE/FALSE substrings
    text = re.sub(r':\s*(MOSTLY TRUE|PARTLY TRUE|MISLEADING|UNVERIFIABLE|TRUE|FALSE)\b', r': "\1"', text)
    text = re.sub(r",\s*([}\]])", r"\1", text)  # drop trailing commas

    # Clean full-array parse first.
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Fallback: pull complete objects straight from the text. Runs even when the
    # array is truncated mid-output (no closing ]) — keeps every complete object.
    objects = []
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group())
            if isinstance(obj, dict):
                objects.append(obj)
        except json.JSONDecodeError:
            continue
    return objects

# User-generated / social domains that system_prompt.md says never to use as a
# primary source. ponytail: substring blocklist; swap for a real source-tier map
# if this gets gamed or you need per-domain trust weights.
_LOW_QUALITY = (
    "facebook.com", "youtube.com", "youtu.be", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "reddit.com", "quora.com",
    "pinterest.com", "threads.net", "medium.com",
)

def _filter_sources(results: list[dict]) -> list[dict]:
    # Drop social/UGC results, keep the top 3. If that leaves nothing, keep the
    # originals — some evidence (with a low-confidence verdict) beats none.
    filtered = [r for r in results if not any(d in r["url"] for d in _LOW_QUALITY)]
    return (filtered or results)[:3]

def _search(query: str) -> tuple[str, list[str]]:
    # Pull extra results so filtering out social junk still leaves ~3 real sources.
    results = _filter_sources(_tavily_().search(query, max_results=6).get("results", []))
    urls = [r["url"] for r in results]
    text = "\n\n".join(f"[{r['url']}]\n{r['content'][:600]}" for r in results)
    return text, urls

def _verify_one(claim: dict, search_text: str, urls: list[str]) -> dict | None:
    # One verify call per claim: keeps each verdict paired with its own search
    # results (no positional zip drift) and lets callers reveal results as they land.
    context = (f"SPEAKER: {claim.get('speaker', 'UNKNOWN')}\n"
               f"CLAIM: {claim['claim']}\n"
               f"SEARCH RESULTS:\n{search_text}\n")
    parsed = [v for v in _parse_json_array(_chat(VERIFY_PROMPT, context, max_tokens=600))
              if isinstance(v, dict) and v.get("verdict")]
    if not parsed:
        return None
    verdict = parsed[0]
    if not verdict.get("sources"):
        verdict["sources"] = urls[:3]
    return verdict

def fact_check(transcript: str, on_result=None, verbose=False) -> list[dict]:
    # on_result(verdict) is called for each verdict as it's revealed, in claim
    # order, so callers can print results one by one instead of waiting for all.
    # verbose=True prints a stage-specific reason when nothing comes back, so the
    # caller can tell "too short" from "no claims" from "couldn't verify any".
    def note(msg: str):
        if verbose:
            print(msg)
    try:
        if not transcript or len(transcript.strip()) < 10:
            note("Input too short to fact-check — give it at least a full sentence.")
            return []

        try:
            raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=2500)
            claims = [c for c in _parse_json_array(raw) if isinstance(c, dict) and c.get("claim")]
            if not claims:
                note("No checkable factual claims found — looks like opinion, prediction, or too vague.")
                return []
        except Exception as e:
            print(f"[ERROR] Extraction: {type(e).__name__}: {e}")
            return []

        try:
            with ThreadPoolExecutor() as pool:
                search_results = list(pool.map(lambda c: _search(c["query"]), claims))
        except Exception as e:
            print(f"[ERROR] Search: {type(e).__name__}: {e}")
            return []

        # Verify each claim concurrently, but reveal in claim order: iterating the
        # futures list front-to-back blocks on #1 first while #2.. finish in the
        # background, so results appear in order as soon as each is ready.
        results: list[dict] = []
        with ThreadPoolExecutor() as pool:
            futures = [
                pool.submit(_verify_one, claim, search_text, urls)
                for claim, (search_text, urls) in zip(claims, search_results)
            ]
            for f in futures:
                try:
                    verdict = f.result()
                except Exception as e:
                    print(f"[ERROR] Verification: {type(e).__name__}: {e}")
                    continue
                if verdict:
                    results.append(verdict)
                    if on_result:
                        on_result(verdict)
        if not results:
            note(f"Extracted {len(claims)} claim(s), but none could be verified with enough "
                 f"confidence — no source clearly confirmed or denied them (confidence < 60).")
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

    assert len(_parse_json_array(clean)) == 2, "clean"
    assert len(_parse_json_array(bare_enum)) == 1, "bare enum"
    assert _parse_json_array(bare_enum)[0]["verdict"] == "TRUE", "enum value"
    bare_multi = '[{"claim":"a","verdict":MOSTLY TRUE,"confidence":80}]'      # multi-word unquoted enum
    assert _parse_json_array(bare_multi)[0]["verdict"] == "MOSTLY TRUE", "multi-word enum"
    assert len(_parse_json_array(fenced)) == 1, "fenced"
    assert len(_parse_json_array(trailing)) == 1, "trailing comma"
    assert len(_parse_json_array(truncated)) == 2, "truncated keeps complete objects"
    assert _parse_json_array("not json at all") == [], "garbage"

    # Source filter: drops social/UGC, keeps real sources, never returns empty.
    mixed = [{"url": "https://reuters.com/a"}, {"url": "https://facebook.com/b"},
             {"url": "https://bbc.com/c"}, {"url": "https://youtube.com/d"}]
    assert [r["url"] for r in _filter_sources(mixed)] == \
        ["https://reuters.com/a", "https://bbc.com/c"], "drops social, keeps real"
    all_bad = [{"url": "https://youtube.com/x"}, {"url": "https://x.com/y"}]
    assert _filter_sources(all_bad) == all_bad[:3], "keeps originals when all low-quality"

    print("OK: all self-checks pass")

