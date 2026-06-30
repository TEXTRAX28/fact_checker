import json
import os
import re

# DeepInfra API (OpenAI-compatible)
_deepinfra_client = None
_tavily = None

MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"

EXTRACT_PROMPT = """Extract the 10 most specific and verifiable factual claims from the text.
Return a JSON array. Each item must have:
  "claim": the exact claim as stated
  "query": a short search query to verify it
  "speaker": the name or label of who made the claim (e.g. "Senator Davis", "SPEAKER_A") — use "UNKNOWN" only if truly unidentifiable

Only include: statistics, numbers, dates, named events, quotes, scientific/medical/legal/historical facts.
Skip: opinions, predictions, vague statements, rhetorical questions.
Return [] if nothing is checkable.
Return ONLY the JSON array, no other text."""

VERIFY_PROMPT = """You are a fact-checker. Output ONLY a JSON array. No markdown. No analysis. No prose. Just the JSON array.

Each object in the array must have:
  "speaker": the speaker label
  "claim": the original claim text
  "verdict": TRUE / FALSE / MISLEADING / UNVERIFIABLE
  "confidence": integer 60-100
  "explanation": 1-3 sentences
  "sources": array of URLs from the search results

Verdict definitions:
  TRUE = claim matches sources, FALSE = claim contradicts sources,
  MISLEADING = technically true but deceptive framing, UNVERIFIABLE = sources conflict

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
    text = re.sub(r':\s*(TRUE|FALSE|MISLEADING|UNVERIFIABLE)\b', r': "\1"', text)  # quote bare enums
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

def _search(query: str) -> tuple[str, list[str]]:
    results = _tavily_().search(query, max_results=3).get("results", [])
    urls = [r["url"] for r in results]
    text = "\n\n".join(f"[{r['url']}]\n{r['content'][:600]}" for r in results)
    return text, urls

def fact_check(transcript: str) -> list[dict]:
    try:
        if not transcript or len(transcript.strip()) < 10:
            return []

        try:
            raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=2500)
            claims = [c for c in _parse_json_array(raw) if isinstance(c, dict) and c.get("claim")]
            if not claims:
                return []
        except Exception as e:
            print(f"[ERROR] Extraction: {type(e).__name__}: {e}")
            return []

        try:
            with __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor() as pool:
                search_results = list(pool.map(lambda c: _search(c["query"]), claims))
        except Exception as e:
            print(f"[ERROR] Search: {type(e).__name__}: {e}")
            return []

        try:
            context = ""
            for item, (search_text, _) in zip(claims, search_results):
                context += f"\n---\nSPEAKER: {item.get('speaker', 'UNKNOWN')}\nCLAIM: {item['claim']}\nSEARCH RESULTS:\n{search_text}\n"

            raw_verdicts = _chat(VERIFY_PROMPT, context, max_tokens=3500)
            verdicts = [v for v in _parse_json_array(raw_verdicts) if isinstance(v, dict) and v.get("verdict")]
            if not verdicts:
                return []

            for verdict, (_, urls) in zip(verdicts, search_results):
                if not verdict.get("sources"):
                    verdict["sources"] = urls[:3]

            return verdicts
        except Exception as e:
            print(f"[ERROR] Verification: {type(e).__name__}: {e}")
            return []

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
    assert len(_parse_json_array(fenced)) == 1, "fenced"
    assert len(_parse_json_array(trailing)) == 1, "trailing comma"
    assert len(_parse_json_array(truncated)) == 2, "truncated keeps complete objects"
    assert _parse_json_array("not json at all") == [], "garbage"
    print("OK: all _parse_json_array cases pass")

