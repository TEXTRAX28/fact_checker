import json
import os
import re
from pathlib import Path

# DeepInfra API setup
_deepinfra_client = None
_tavily = None
_system_prompt = None

# Groq setup (commented out - using DeepInfra instead)
# _groq = None

# MODEL for Groq (archived)
# MODEL = "llama-3.3-70b-versatile"

# MODEL for DeepInfra
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

VERIFY_PROMPT = """You are a fact-checker. You will receive multiple claims, each with web search results.

Return a JSON ARRAY — one object per claim — with these fields:
  "speaker": "UNKNOWN"
  "claim": the original claim text
  "verdict": TRUE / FALSE / MISLEADING / UNVERIFIABLE
  "confidence": integer 60-100
  "explanation": 1-3 sentences
  "sources": array of URLs from the search results that support your verdict

Verdict definitions:
  TRUE = claim matches sources, FALSE = claim contradicts sources,
  MISLEADING = technically true but deceptive framing, UNVERIFIABLE = sources conflict

Skip any claim where confidence would be below 60 (omit it from the array).
Return ONLY the JSON array, no other text."""


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

# Groq client (archived - commented out)
# def _groq_():
#     global _groq
#     if _groq is None:
#         from groq import Groq
#         _groq = Groq(api_key=os.getenv("GROQ_API_KEY"))
#     return _groq

def _tavily_():
    global _tavily
    if _tavily is None:
        from tavily import TavilyClient
        _tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    return _tavily

def _chat(system: str, user: str, max_tokens: int = 1000) -> str:
    # Using DeepInfra (OpenAI-compatible)
    response = _deepinfra_().chat.completions.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content or ""

# Groq chat function (archived - commented out)
# def _chat_groq(system: str, user: str, max_tokens: int = 1000) -> str:
#     response = _groq_().chat.completions.create(
#         model=MODEL,
#         max_tokens=max_tokens,
#         messages=[
#             {"role": "system", "content": system},
#             {"role": "user", "content": user},
#         ],
#     )
#     return response.choices[0].message.content or ""

def _parse_json_array(text: str) -> list:
    text = re.sub(r"```(?:json)?\n?|```", "", text)
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []
    try:
        parsed = json.loads(match.group())
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        objects = []
        for m in re.finditer(r"\{[^{}]*\}", match.group()):
            try:
                obj = json.loads(m.group())
                if isinstance(obj, dict):
                    objects.append(obj)
            except json.JSONDecodeError:
                continue
        return objects

def _parse_json_object(text: str) -> dict:
    text = re.sub(r"```(?:json)?\n?|```", "", text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {}
    parsed = json.loads(match.group())
    return parsed if isinstance(parsed, dict) else {}

def _search(query: str) -> tuple[str, list[str]]:
    results = _tavily_().search(query, max_results=3).get("results", [])
    urls = [r["url"] for r in results]
    text = "\n\n".join(f"[{r['url']}]\n{r['content'][:600]}" for r in results)
    return text, urls

def fact_check(transcript: str) -> list[dict]:
    try:
        # step 1: extract claims + search queries (1 LLM call)
        raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=2500)
        claims = [c for c in _parse_json_array(raw) if isinstance(c, dict) and c.get("claim")]
        if not claims:
            return []

        # step 2: search all claims in parallel (no LLM)
        with __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor() as pool:
            search_results = list(pool.map(lambda c: _search(c["query"]), claims))

        # step 3: verify all claims in one LLM call
        context = ""
        for item, (search_text, _) in zip(claims, search_results):
            context += f"\n---\nSPEAKER: {item.get('speaker', 'UNKNOWN')}\nCLAIM: {item['claim']}\nSEARCH RESULTS:\n{search_text}\n"

        raw_verdicts = _chat(VERIFY_PROMPT, context, max_tokens=3500)
        verdicts = [v for v in _parse_json_array(raw_verdicts) if isinstance(v, dict) and v.get("verdict")]

        # inject sources if model didn't include them
        for verdict, (_, urls) in zip(verdicts, search_results):
            if not verdict.get("sources"):
                verdict["sources"] = urls[:3]

        return verdicts
    except Exception as e:
        print(f"[fact-check error] {e}")
        return []
