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
    # Using DeepInfra (OpenAI-compatible)
    try:
        print(f"[DEBUG] API call to DeepInfra (model={MODEL}, max_tokens={max_tokens})...")
        response = _deepinfra_().chat.completions.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        print("[DEBUG] API response received")
        return response.choices[0].message.content or ""
    except Exception as e:
        print(f"[CHAT ERROR] {type(e).__name__}: {e}")
        raise

def _gemma_model_():
    global _gemma_model, _gemma_tokenizer
    if _gemma_model is None:
        from transformers import AutoTokenizer, AutoModelForCausalLM
        import torch

        model_id = "google/gemma-4-12b-it"
        print("[DEBUG] Checking GPU...")
        cuda_available = torch.cuda.is_available()
        print(f"[DEBUG] CUDA available: {cuda_available}")

        if not cuda_available:
            print("[ERROR] CUDA not available. Gemma requires GPU. Install PyTorch CUDA:")
            print("pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118")
            raise RuntimeError("CUDA required for Gemma")

        print("[DEBUG] Loading tokenizer...")
        _gemma_tokenizer = AutoTokenizer.from_pretrained(model_id)

        print("[DEBUG] Loading model (5-10 min, first run only)...")
        print("[DEBUG] This may show warnings from HuggingFace, that's normal.\n")
        _gemma_model = AutoModelForCausalLM.from_pretrained(
            model_id,
            torch_dtype=torch.float16,
            device_map="auto"
        )
        print("[DEBUG] Gemma 4 12B ready!\n")
    return _gemma_model, _gemma_tokenizer

def _chat_gemma(system: str, user: str, max_tokens: int = 1000) -> str:
    try:
        print("[DEBUG] Running local Gemma 4 12B inference...")
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

        print("[DEBUG] Gemma response received")
        if "[/INST]" in response:
            response = response.split("[/INST]")[-1].strip()
        return response
    except Exception as e:
        print(f"[CHAT ERROR - Gemma] {type(e).__name__}: {e}")
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
        print("\n[COMPARE MODE] Running both models...\n")
        deepinfra_results = _fact_check_with_model(transcript, "deepinfra")
        print("\n" + "="*60 + "\n")
        gemma_results = _fact_check_with_model(transcript, "gemma")

        return {
            "deepinfra": deepinfra_results,
            "gemma": gemma_results,
            "transcript": transcript
        }
    except Exception as e:
        print(f"[ERROR] Comparison failed: {type(e).__name__}: {e}")
        return {}

def _fact_check_with_model(transcript: str, model_type: str) -> list[dict]:
    try:
        if not transcript or len(transcript.strip()) < 10:
            print("ERROR: Text too short to analyze (minimum 10 characters)")
            return []

        # step 1: extract claims
        try:
            print(f"[{model_type.upper()}] Extracting claims...")
            raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=2500, model_type=model_type)
            claims = [c for c in _parse_json_array(raw) if isinstance(c, dict) and c.get("claim")]
            print(f"[{model_type.upper()}] Found {len(claims)} claims")

            if not claims:
                print(f"[{model_type.upper()}] No checkable claims found")
                return []
        except Exception as e:
            print(f"[{model_type.upper()}] Extraction error: {type(e).__name__}: {e}")
            return []

        # step 2: search all claims
        try:
            print(f"[{model_type.upper()}] Searching evidence...")
            with __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor() as pool:
                search_results = list(pool.map(lambda c: _search(c["query"]), claims))
        except Exception as e:
            print(f"[{model_type.upper()}] Search error: {type(e).__name__}: {e}")
            return []

        # step 3: verify claims
        try:
            print(f"[{model_type.upper()}] Verifying claims...")
            context = ""
            for item, (search_text, _) in zip(claims, search_results):
                context += f"\n---\nSPEAKER: {item.get('speaker', 'UNKNOWN')}\nCLAIM: {item['claim']}\nSEARCH RESULTS:\n{search_text}\n"

            raw_verdicts = _chat(VERIFY_PROMPT, context, max_tokens=3500, model_type=model_type)
            verdicts = [v for v in _parse_json_array(raw_verdicts) if isinstance(v, dict) and v.get("verdict")]

            if not verdicts:
                print(f"[{model_type.upper()}] No verdicts returned")
                return []

            # inject sources
            for verdict, (_, urls) in zip(verdicts, search_results):
                if not verdict.get("sources"):
                    verdict["sources"] = urls[:3]
                verdict["model"] = model_type.upper()

            print(f"[{model_type.upper()}] Complete: {len(verdicts)} claims verified")
            return verdicts
        except Exception as e:
            print(f"[{model_type.upper()}] Verification error: {type(e).__name__}: {e}")
            return []

    except Exception as e:
        print(f"[{model_type.upper()}] Unexpected error: {type(e).__name__}: {e}")
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
            print("ERROR: Text too short to analyze (minimum 10 characters)")
            return []

        # step 1: extract claims
        try:
            print("[DEBUG] Starting EXTRACT...")
            raw = _chat(EXTRACT_PROMPT, transcript, max_tokens=2500)
            print(f"[DEBUG] EXTRACT response: {raw[:100]}...")
            claims = [c for c in _parse_json_array(raw) if isinstance(c, dict) and c.get("claim")]
            print(f"[DEBUG] Found {len(claims)} claims")

            if not claims:
                print("No checkable claims found in text (contains only opinions, predictions, or vague statements)")
                return []
        except Exception as e:
            print(f"ERROR during claim extraction: {type(e).__name__}: {e}")
            return []

        # step 2: search all claims in parallel
        try:
            print("[DEBUG] Searching for evidence...")
            with __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor() as pool:
                search_results = list(pool.map(lambda c: _search(c["query"]), claims))
        except Exception as e:
            print(f"ERROR during web search: {type(e).__name__}: {e}")
            return []

        # step 3: verify claims
        try:
            print("[DEBUG] Verifying claims...")
            context = ""
            for item, (search_text, _) in zip(claims, search_results):
                context += f"\n---\nSPEAKER: {item.get('speaker', 'UNKNOWN')}\nCLAIM: {item['claim']}\nSEARCH RESULTS:\n{search_text}\n"

            raw_verdicts = _chat(VERIFY_PROMPT, context, max_tokens=3500)
            verdicts = [v for v in _parse_json_array(raw_verdicts) if isinstance(v, dict) and v.get("verdict")]

            if not verdicts:
                print("WARNING: Verification returned no verdicts (confidence may be too low)")
                return []

            # inject sources if model didn't include them
            for verdict, (_, urls) in zip(verdicts, search_results):
                if not verdict.get("sources"):
                    verdict["sources"] = urls[:3]

            print(f"[DEBUG] Verification complete: {len(verdicts)} verified claims")
            return verdicts
        except Exception as e:
            print(f"ERROR during claim verification: {type(e).__name__}: {e}")
            return []

    except Exception as e:
        print(f"ERROR: Unexpected error in fact_check: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return []
