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

### 1. Verdicts on very fresh news can be wrong
- **Symptom:** Claims from articles published within the last 24–48 hours sometimes get FALSE/UNVERIFIABLE even when the claim is correct.
- **Root cause:** Search snippets are truncated to 300 chars. For compound articles (e.g. voting rights vs equity ownership), the confirming sentence often appears after the cutoff.
- **Fix (not yet applied):** Increase snippet from 300 to 600 chars in `fact_checker.py` `_search()`:
  ```python
  # line ~96 in fact_checker.py
  text = "\n\n".join(f"[{r['url']}]\n{r['content'][:300]}" for r in results)
  #                                                    ^^^^ change to 600
  ```

### 2. Mode 1 — Microphone (untested)
- **Status:** ❓ Not tested this session.
- **Likely issue:** `faster-whisper` and/or `pyannote.audio` may not be installed, or `HUGGINGFACE_TOKEN` is missing from `.env`.
- **What to do:** Run mode 1, check if it errors, install missing deps from `requirements.txt`.

### 3. Mode 2 — Live stream URL (untested)
- **Status:** ❓ Not tested this session.
- **Requirement:** `yt-dlp` and `ffmpeg` must be installed and on PATH.
- **What to do:** Run mode 2 with a YouTube live URL and check for errors.

### 4. Mode 3 — URL requires Jina prefix internally, no fallback messaging
- **Status:** ⚠️ Partially working — URL mode works but has two gaps.
- **Gap 1 (UX):** Mode 3 already prepends `https://r.jina.ai/` automatically inside `_fetch_article()`, so the user doesn't need to type it. But if the site is paywalled, bot-blocked, or returns thin content, the pipeline silently returns "No checkable claims found" with no explanation.
- **Gap 2 (fallback):** There is no fallback ladder — if Jina fails to read the URL, there's no retry via Wayback or search-the-claim path. It just fails silently.
- **What to build:**
  1. Add a content-length check after fetching — if result is < 500 chars, print a warning:
     ```
     ⚠ Could not fully read that page (paywalled, bot-blocked, or JS-rendered).
       Verdict will be based on other coverage of this claim, not the original article.
     ```
  2. Add the fallback ladder in `_fetch_article()`:
     - Tier 1: Jina reader (`r.jina.ai/{url}`)
     - Tier 2: Wayback Machine snapshot (`archive.org/wayback/available?url={url}`)
     - Tier 3: Tavily search on the URL itself (searches for coverage of the same story)
     - Dead end: print the warning above and return what little was found (or nothing)
  3. Known unreadable site types to warn about: hard paywalls (WSJ, FT, Bloomberg), login walls (X/Twitter, Instagram, Facebook), geo-blocked pages, dead links (404).

---

## Files Changed This Session

| File | What Changed |
|---|---|
| `main.py` | `run_text()` now calls `_clean_article()` — fixes mode 4 returning 0 claims |
| `main.py` | Heavy imports (`sounddevice`, `numpy`, `transcriber`) deferred into mic/stream functions |
| `fact_checker.py` | `EXTRACT_PROMPT` changed from "every claim" to "top 10 most specific" |
| `fact_checker.py` | Extract `max_tokens` raised from 800 → 1500 |
| `fact_checker.py` | `groq` and `tavily` imports deferred into `_groq_()` and `_tavily_()` lazy init functions |

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
