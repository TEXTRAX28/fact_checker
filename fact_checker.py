import json
import os
import re
from pathlib import Path

# DeepInfra API setup
_deepinfra_client = None
_tavily = None
_system_prompt = None
_gemma_model = None
_gemma_tokenizer = None

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

def _chat(system: str, user: str, max_tokens: int = 1000, model_type: str = None) -> str:
    # Use specified model_type or fall back to ACTIVE_MODEL setting
    if model_type is None:
        model_type = os.getenv("ACTIVE_MODEL", "deepinfra").lower()

    if model_type == "gemma":
        return _chat_gemma(system, user, max_tokens)
    else:
        return _chat_deepinfra(system, user, max_tokens)

def _chat_deepinfra(system: str, user: str, max_tokens: int = 1000) -> str:
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

def _gemma_model_():
    global _gemma_model, _gemma_tokenizer
    if _gemma_model is None:
        from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
        import torch

        model_id = "google/gemma-4-E4B"

        if not torch.cuda.is_available():
            raise RuntimeError("[ERROR] CUDA not available. Gemma requires GPU.")

        _gemma_tokenizer = AutoTokenizer.from_pretrained(model_id)

        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16
        )

        _gemma_model = AutoModelForCausalLM.from_pretrained(
            model_id,
            quantization_config=bnb_config,
            device_map="auto"
        )
    return _gemma_model, _gemma_tokenizer

def _chat_gemma(system: str, user: str, max_tokens: int = 1000) -> str:
    try:
        import torch
        model, tokenizer = _gemma_model_()

        messages = [
            {"role": "user", "content": f"{system}\n\n{user}"}
        ]
        text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        inputs = tokenizer.encode(text, return_tensors="pt").to(device)

        outputs = model.generate(inputs, max_new_tokens=max_tokens, temperature=0.7)
        response = tokenizer.decode(outputs[0])

        if "[/INST]" in response:
            response = response.split("[/INST]")[-1].strip()
        return response
    except Exception as e:
        print(f"[ERROR] Gemma: {type(e).__name__}: {e}")
        raise

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

def compare_fact_check(transcript: str) -> dict:
    try:
        print("Comparing DeepInfra vs Gemma...\n")
        deepinfra_results = _fact_check_with_model(transcript, "deepinfra")
        gemma_results = _fact_check_with_model(transcript, "gemma")

        return {
            "deepinfra": deepinfra_results,
            "gemma": gemma_results,
            "transcript": transcript
        }
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return {}

def _fact_check_with_model(transcript: str, model_type: str) -> list[dict]:
    try:
        if not transcript or len(transcript.strip()) < 10:
            return []

        try:
            raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=2500, model_type=model_type)
            claims = [c for c in _parse_json_array(raw) if isinstance(c, dict) and c.get("claim")]
            if not claims:
                return []
        except Exception as e:
            print(f"[ERROR] {model_type}: {type(e).__name__}: {e}")
            return []

        try:
            with __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor() as pool:
                search_results = list(pool.map(lambda c: _search(c["query"]), claims))
        except Exception as e:
            print(f"[ERROR] {model_type}: {type(e).__name__}: {e}")
            return []

        try:
            context = ""
            for item, (search_text, _) in zip(claims, search_results):
                context += f"\n---\nSPEAKER: {item.get('speaker', 'UNKNOWN')}\nCLAIM: {item['claim']}\nSEARCH RESULTS:\n{search_text}\n"

            raw_verdicts = _chat(VERIFY_PROMPT, context, max_tokens=3500, model_type=model_type)
            verdicts = [v for v in _parse_json_array(raw_verdicts) if isinstance(v, dict) and v.get("verdict")]

            if not verdicts:
                return []

            for verdict, (_, urls) in zip(verdicts, search_results):
                if not verdict.get("sources"):
                    verdict["sources"] = urls[:3]
                verdict["model"] = model_type.upper()

            return verdicts
        except Exception as e:
            print(f"[ERROR] {model_type}: {type(e).__name__}: {e}")
            return []

    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return []

def fact_check(transcript: str) -> list[dict]:
    # Handle compare mode
    if os.getenv("ACTIVE_MODEL", "").lower() == "compare":
        result = compare_fact_check(transcript)
        if result:
            from display import show_comparison
            show_comparison(result)
        return []

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
