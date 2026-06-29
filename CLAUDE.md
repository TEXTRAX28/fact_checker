# CLAUDE.md — Real-Time Fact Checker

## Project Overview

A real-time fact-checking application that listens via microphone, transcribes speech with speaker diarization, extracts factual claims, and verifies them using live web search. Results are displayed with verdicts, confidence scores, and source links.

---

## Architecture

```
Mic Input
   ↓
Audio Buffer (chunked every ~5s)
   ↓
Whisper (transcription + speaker diarization)
   ↓
Transcript Chunk → [SPEAKER_A] "claim here..."
   ↓
Claude API (system_prompt.md) + Web Search Tool
   ↓
JSON result array
   ↓
UI Display (verdict, confidence %, explanation, source)
```

---

## Tech Stack

| Layer | Tool | Reason |
|---|---|---|
| Transcription | Groq Whisper API | Fast, accurate, low latency |
| Speaker Diarization | `pyannote.audio` (mode 1 only) | Identifies who is speaking |
| LLM | Groq (llama-3.3-70b-versatile) | Claim extraction + verdict |
| Web Search | Tavily Search API | Grounded, reliable sources |
| UI | Terminal output | Fast to build, readable |
| Audio | `sounddevice` + `numpy` | Low-latency mic capture |

---

## Chunking Strategy

- Capture audio in **5-second rolling windows**
- Only send a chunk to Whisper when audio energy exceeds silence threshold (VAD)
- After transcription, immediately queue the chunk for fact-checking
- Fact-checking runs **async** — transcription never waits for it
- This means: transcript continues scrolling while fact-checks for earlier chunks come in slightly later (5–15s lag is acceptable)

---

## Speaker Diarization Notes

- `pyannote.audio` assigns `SPEAKER_00`, `SPEAKER_01`, etc. — rename to `SPEAKER_A`, `SPEAKER_B` in output
- Single mic setup will struggle with overlapping speech — this is a hardware limitation, not a code bug
- If diarization confidence is low for a segment, label it `UNKNOWN` rather than guess

---

## System Prompts

- `EXTRACT_PROMPT` (fact_checker.py) — instructs LLM to extract 10 most verifiable claims
- `VERIFY_PROMPT` (fact_checker.py) — instructs LLM to verify claims against search results
- `system_prompt.md` — archived reference documentation (not currently used in API calls)

---

## LLM Call Structure (Groq)

```python
from groq import Groq

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    max_tokens=1500,
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": transcript_chunk}  # e.g. "[SPEAKER_A] The rate dropped to 3.4%."
    ]
)
```

Extract text from `response.choices[0].message.content` and parse it as JSON.

---

## Output Handling

Parse the JSON array returned by Claude. Each item has:

```python
{
    "speaker": str,        # "SPEAKER_A", "SPEAKER_B", "UNKNOWN"
    "claim": str,          # exact claim that was checked
    "verdict": str,        # "TRUE", "FALSE", "MISLEADING", "UNVERIFIABLE"
    "confidence": int,     # 60–100
    "explanation": str,    # 1–3 sentence explanation
    "source": str          # URL — always real, never fabricated
}
```

If Claude returns `[]`, no claims were worth checking in that chunk. That's fine — display nothing.

---

## Display Rules

Color-code verdicts in the UI:
- `TRUE` → 🟢 green
- `FALSE` → 🔴 red
- `MISLEADING` → 🟡 yellow
- `UNVERIFIABLE` → ⚪ grey

Show confidence as a percentage bar next to each result.

Always show the source as a clickable link.

---

## Performance Constraints

- Transcription must complete within **2–3 seconds** per 5s chunk
- Claude API call (with web search) typically takes **5–12 seconds**
- These run **concurrently** — use `asyncio` or a thread pool
- Never block the mic capture loop waiting for fact-check results
- Queue fact-check jobs; process them FIFO

---

## What Not to Do

- **Do not cache verdicts** based on claim text similarity — claims can change slightly and context matters
- **Do not retry failed API calls more than once** — if it fails, skip that chunk
- **Do not display partial results** — only show a fact-check when the full JSON is ready
- **Do not send audio directly to Claude** — always transcribe first, then send text
- **Do not send chunks smaller than one complete sentence** — wait for a sentence boundary

---

## Known Limitations (document these in the UI)

1. Speaker diarization is best-effort on a single mic — overlapping speech degrades accuracy
2. Fact-checks lag 5–15 seconds behind live speech — this is intentional and unavoidable
3. Fast-moving or highly niche claims may be skipped if no reliable source is found in time
4. Non-English speech requires setting Whisper's language parameter explicitly

---

## Environment Variables

```
ANTHROPIC_API_KEY=your_key_here
HUGGINGFACE_TOKEN=your_token_here   # required for pyannote diarization model
```

---

## Project Root

All files should be created and run from this directory:

```
C:\Users\natan\VSC Code\fact-checker\
```

## File Structure

```
C:\Users\natan\VSC Code\fact-checker\
├── CLAUDE.md               ← claude file prompt
├── system_prompt.md        ← Claude system prompt
├── main.py                 ← entry point, mic capture loop
├── transcriber.py          ← Whisper + diarization
├── fact_checker.py         ← Claude API calls, claim parsing
├── display.py              ← UI rendering
├── requirements.txt        ← requirements that needed
└── .env
```

---

## Security Requirements

This project uses sensitive API keys. Treat any leak as a serious incident — Anthropic and HuggingFace keys can rack up charges or get abused if stolen.

### Step 0 — Before Anything Else

Before the first `git init` or `git add`, create `.gitignore` at the project root with at minimum:

```
.env
*.env
.env.*
__pycache__/
*.pyc
*.pyo
.DS_Store
Thumbs.db
```

**Never commit `.env` under any name or extension.**

### Code Rules

- Load keys exclusively via `os.getenv()` or `python-dotenv` — never as string literals, never in comments, never in print/debug statements
- Never log the API key or any part of it to console or to a log file
- If you need to debug an auth issue, log only `"API key loaded: YES/NO"` — not the key itself
- Never pass API keys as CLI arguments (they show up in shell history)

### Pre-Push Checklist (run every time before `git push`)

Claude should automatically check for these before any push or when asked to review the codebase:

1. Scan all `.py` files for hardcoded strings matching `sk-ant-` (Anthropic key prefix) or `hf_` (HuggingFace key prefix)
2. Confirm `.env` is listed in `.gitignore` and is NOT tracked by git — `git ls-files .env` should return nothing
3. Confirm no key strings appear in any commit message or filename
4. Run `git diff --cached` before committing and flag anything that looks like a secret

### If a Key is Accidentally Pushed to GitHub

1. Immediately revoke it — console.anthropic.com for the Anthropic key, huggingface.co/settings/tokens for HuggingFace
2. Generate a new key and update `.env` locally
3. Do NOT just delete the file in a new commit — the key is still in git history; use `git filter-repo` to purge it or contact GitHub support
4. Treat the old key as permanently compromised even if you acted within seconds

---

## Indonesian News — Source Strategy & Known Problem

This is a real problem worth spending time on. Indonesian news is significantly more fragmented than English-language news ecosystems. The same event can be reported with different numbers, different quotes, and different framing across sources — and some sources are outright unreliable or politically aligned. A naive web search will return inconsistent results and make the fact-checker less trustworthy for Indonesian content.

### The Core Problem

When fact-checking Indonesian claims:
- There is no single authoritative aggregator equivalent to AP or Reuters in Bahasa Indonesia
- Viral claims spread fast on Twitter/X and Instagram before any outlet verifies them
- Regional outlets often copy-paste from each other without independent verification, creating false consensus (10 articles saying the same wrong thing)
- Some major outlets have known political leanings that affect how they frame statistics and quotes

### Source Tier List for Indonesian Fact-Checking

Claude must weight sources differently depending on origin. Use this hierarchy:

**Tier 1 — Highest trust (use these to confirm or deny a claim)**
- Reuters Indonesia / Reuters (reuters.com)
- BBC Indonesia (bbc.com/indonesia)
- Associated Press (apnews.com)
- Official government data: BPS (bps.go.id) for statistics, Bank Indonesia (bi.go.id) for economic data, Kemenkes (kemkes.go.id) for health data
- WHO, World Bank, IMF for macroeconomic/health claims about Indonesia

**Tier 2 — Generally reliable, use with one corroborating source**
- Kompas (kompas.com) — most established Indonesian broadsheet
- Tempo (tempo.co) — known for investigative journalism
- Antara (antaranews.com) — state news agency, reliable for official statements
- Katadata (katadata.co.id) — strong on economics and data

**Tier 3 — Use only if Tier 1/2 unavailable, and flag it**
- Detik (detik.com) — high traffic, fast reporting, but sometimes rushes verification
- CNN Indonesia (cnnindonesia.com)
- Tribunnews (tribunnews.com) — very high volume, quality varies significantly

**Do not use as primary source**
- Viral social media posts, WhatsApp forwards
- Outlets with no clear editorial standards or about page
- Aggregator sites that don't cite original reporting

### Dedicated Indonesian Fact-Checking Sites

These exist specifically to debunk Indonesian misinformation — check these first for viral claims:
- **Cek Fakta Kompas** (cekfakta.kompas.com)
- **Turn Back Hoax** (turnbackhoax.id)
- **AFP Fact Check Indonesia** (factcheck.afp.com — filter by Indonesia)
- **Mafindo** (mafindo.or.id) — Indonesia's largest anti-hoax volunteer network

### Search Strategy for Indonesian Claims

When a claim comes in Bahasa Indonesia or is about Indonesia:

1. Search in **both Bahasa Indonesia and English** — some data only exists in English sources (World Bank reports, etc.)
2. Search the dedicated fact-check sites above first if the claim sounds viral or political
3. For statistics (GDP, inflation, unemployment, population): always go to BPS or Bank Indonesia directly — do not trust a number cited by an outlet without checking the primary source
4. If 3+ Tier 3 sources agree but no Tier 1/2 source confirms it, output `UNVERIFIABLE` not `TRUE`
5. If sources conflict between tiers, always defer to the higher tier

### Bahasa Indonesia Handling

- Whisper handles Bahasa Indonesia well — no special config needed
- Set `language="id"` in the Whisper call when Indonesian speech is detected
- Claude handles Bahasa Indonesia natively — claims can be sent in Indonesian and Claude will fact-check them without translation
- If a claim is a mix of Indonesian and English (code-switching, common in Indonesian media), send it as-is — do not pre-translate
