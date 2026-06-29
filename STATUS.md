# Fact-Checker — Project Status

## What's Working

| Feature | Status | Notes |
|---|---|---|
| **Mode 3 — Article URL** | ✅ Working | Fetches via Jina reader, strips headers + images, extracts claims |
| **Mode 4 — Paste text** | ✅ Working | Accepts raw Jina-format paste (Title / URL Source / Markdown Content blocks) |
| **Claim extraction** | ✅ Working | Extracts top 10 claims via Groq (llama-3.3-70b-versatile), 1500 token budget |
| **Claim verification** | ✅ Working | Searches via Tavily, verifies with Groq, returns TRUE/FALSE/MISLEADING/UNVERIFIABLE |
| **JSON parsing** | ✅ Working | Two-tier parser: full array first, falls back to per-object extraction if truncated |
| **Display** | ✅ Working | Verdict, confidence bar, explanation, sources |
| **Startup speed** | ✅ Fixed | 0.1s (was ~1.7s) — heavy imports now deferred to when mic/stream modes are actually used |

---

## What's Not Working / Known Issues

### 1. Verdicts on very fresh news can be wrong ✅ FIXED
- **Was:** Search snippets truncated to 300 chars → missed confirming sentences
- **Now:** Increased to 600 chars in `fact_checker.py` line 96
- **Impact:** Verdicts on fresh news should be more accurate now

### 2. Mode 1 — Microphone (untested)
- **Status:** ❓ Not tested this session.
- **Likely issue:** `faster-whisper` and/or `pyannote.audio` may not be installed, or `HUGGINGFACE_TOKEN` is missing from `.env`.
- **What to do:** Run mode 1, check if it errors, install missing deps from `requirements.txt`.

### 3. Mode 2 — Live stream URL (untested)
- **Status:** ❓ Not tested this session.
- **Requirement:** `yt-dlp` and `ffmpeg` must be installed and on PATH.
- **What to do:** Run mode 2 with a YouTube live URL and check for errors.

### 4. Mode 3 — URL fetching with fallback ladder ✅ IMPLEMENTED
- **Status:** ✅ Fully working with 3-tier fallback + user warnings
- **Tier 1:** Jina reader (`https://r.jina.ai/{url}`)
- **Tier 2:** Wayback Machine snapshot for archival content
- **Tier 3:** Tavily search for coverage of the story
- **UX:** Shows warning message when page is paywalled/blocked, indicates which tier succeeded
- **Content check:** Rejects results < 500 chars to avoid fact-checking thin stubs
- **Known limitation:** Bot-blocked or heavy JS-rendered sites may still fail all tiers

---

## Files Changed This Session (Jun 29, 2026)

| File | What Changed |
|---|---|
| `fact_checker.py` | Search snippet increased 300 → 600 chars (line 96) — fixes fresh news verdict misses |
| `main.py` | `_fetch_article()` now returns `(content, warning)` tuple with 3-tier fallback |
| `main.py` | Tier 1: Jina reader; Tier 2: Wayback Machine; Tier 3: Tavily search |
| `main.py` | Content validation: rejects results < 500 chars, shows paywalled/blocked warnings |
| `main.py` | `run_article()` updated to display fallback warnings |

---

## How to Run

```
cd "C:\Users\natan\VSC Code\fact-checker"
python main.py
```

- **Mode 3:** paste an article URL — fetches and fact-checks automatically
- **Mode 4:** paste Jina-formatted article text (Title / URL Source / Markdown Content), then press Enter twice to submit

## Environment Variables Required (`.env`)

```
GROQ_API_KEY=...
TAVILY_API_KEY=...
HUGGINGFACE_TOKEN=...   ← only needed for mode 1 (mic)
```
