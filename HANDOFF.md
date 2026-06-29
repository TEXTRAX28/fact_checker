# Fact-Checker Handoff (Jun 29, 2026)

## Project Status: ✅ WORKING

A real-time fact-checker that verifies claims from 4 input sources using Groq LLM + Tavily web search.

---

## What Works ✅

| Mode | Status | Notes |
|---|---|---|
| **Mode 3: Article URL** | ✅ Full | 3-tier fallback (Jina → Wayback → Tavily), paywalled warnings |
| **Mode 4: Paste text** | ✅ Full | Clean article extraction, batch fact-checking |
| **Mode 2: Live stream** | ✅ Working | Duplicate detection, speaker numbering (SPEAKER_A/B/C) |
| **Mode 1: Microphone** | ⏳ Coming Soon | Disabled (too complex, requires 3+ libraries). Shows "Coming Soon" message. |

---

## Fixes Applied Today

1. **Search snippet size:** 300 → 600 chars (fixes fresh news verdict misses)
2. **URL fallback ladder:** Jina → Wayback → Tavily with content validation
3. **EXTRACT_PROMPT:** Removed "Bcs" typo that broke claim extraction
4. **Mode 2 infinite loop:** Added exit condition (3 consecutive capture failures)
5. **Speaker numbering:** Each chunk gets SPEAKER_A, SPEAKER_B, etc.
6. **Duplicate transcripts:** Skip >85% similar chunks (prevents duplicate fact-checking)
7. **Max tokens:** Increased VERIFY from 2000 → 3500 (supports 10+ verdicts)
8. **CLAUDE.md:** Updated to reflect Groq+Tavily implementation (not Anthropic)
9. **`.gitignore`:** Cleaned from 202 → 45 lines (removed Django/Flask/Celery bloat)
10. **CLAUDE.md & STATUS.md:** Removed from GitHub, kept locally in `.gitignore`

---

## How to Run

```bash
cd "C:\Users\natan\VSC Code\fact-checker"
python main.py
```

Then choose:
- **1** = Microphone (requires setup)
- **2** = YouTube/live stream URL
- **3** = Article URL
- **4** = Paste text

---

## Environment Setup

Create `.env` file:
```
GROQ_API_KEY=gsk_...
TAVILY_API_KEY=tvly_...
HUGGINGFACE_TOKEN=hf_...    # Only for Mode 1
```

Install dependencies:
```bash
pip install -r requirements.txt
```

---

## Tech Stack

| Component | Service | Token Type |
|---|---|---|
| **LLM** | Groq (llama-3.3-70b) | `GROQ_API_KEY` |
| **Web Search** | Tavily | `TAVILY_API_KEY` |
| **Speaker ID** | HuggingFace (pyannote) | `HUGGINGFACE_TOKEN` |
| **Transcription** | Groq Whisper | `GROQ_API_KEY` |

**Pricing:** Groq & Tavily have free tiers with token/search limits.

---

## Known Limitations

1. **Mode 1 (mic):** Untested. May have ffmpeg/dependency issues.
2. **Speaker diarization:** Simple per-chunk numbering, not voice-based. Same person across chunks = different speakers.
3. **Filler words:** Not stripped ("hmm", "uh" pass through). Acceptable quality trade-off.
4. **Indonesian sources:** Well-documented in `system_prompt.md` (Tier 1/2/3 hierarchy), ready to use.
5. **Stream repetition:** Video looping detected via >85% similarity. Won't fact-check repeats.

---

## File Structure

```
fact-checker/
├── main.py           ← Entry point (4 modes)
├── fact_checker.py   ← Groq LLM + Tavily search pipeline
├── display.py        ← Terminal output formatting
├── transcriber.py    ← Groq Whisper transcription
├── system_prompt.md  ← Archived reference (not actively used)
├── requirements.txt  ← Dependencies
├── .env              ← API keys (GITIGNORED)
├── CLAUDE.md         ← Local only (GITIGNORED)
├── STATUS.md         ← Local only (GITIGNORED)
└── .gitignore        ← Clean, minimal rules
```

---

## Code Highlights

**Fact-checking pipeline (3 steps):**
1. **Extract:** Groq reads transcript → extracts top 10 claims (1500 tokens)
2. **Search:** Tavily searches for each claim in parallel
3. **Verify:** Groq evaluates claims against search results (3500 tokens)

**URL fetching fallback:**
- Tier 1: Jina reader (primary)
- Tier 2: Wayback Machine (archival)
- Tier 3: Tavily search (coverage-based)

**Mode 2 deduplication:**
- Tracks last transcript
- Skips chunks >85% similar (prevents duplicate speakers/claims)

---

## Next Steps / TODOs

- [ ] Test Mode 1 (microphone) with dependencies installed
- [ ] Optimize speaker diarization (currently per-chunk, not voice-based)
- [ ] Test Indonesian speech heavily (system_prompt.md is ready)
- [ ] Monitor Groq/Tavily token usage for cost
- [ ] Add result caching (optional, CLAUDE.md currently says "do not cache")

---

## Quick Debug Tips

**Mode 2 gets stuck:**
- Video might have looping/repeated audio
- Deduplication should skip it (>85% similarity check)
- Press Ctrl+C to stop

**Mode 3 returns "No checkable claims":**
- Article might be text-light or poorly structured
- Check that Jina fetched enough content (>500 chars)
- Warnings show fallback chain (Jina → Wayback → Tavily)

**LLM not responding:**
- Check `GROQ_API_KEY` is valid
- Check free tier token limit not exceeded (console.groq.com)
- Verify internet connection for API calls

---

## Recent Commits

```
3a0852f Stop tracking CLAUDE.md and STATUS.md
d5f44e0 Increase VERIFY max_tokens 2000→3500 for Mode 1/2
c8bbb35 Skip duplicate transcripts in Mode 2: ignore >85% similar chunks
c61393b Fix Mode 2: add exit condition when stream ends
14e6c56 Add speaker numbering for Mode 2: SPEAKER_A, SPEAKER_B, etc.
f5e9f67 Clean up .gitignore: remove unneeded Django/Flask/Celery/etc.
3cd3854 Update CLAUDE.md: reflect actual Groq+Tavily implementation
1d385f2 Update STATUS.md with completed fixes
df390ec Improve URL fetching robustness and search accuracy
```

---

## Contact / Notes

- Project is on GitHub: https://github.com/TEXTRAX28/fact_checker
- CLAUDE.md and STATUS.md are local-only (not in repo, add to .gitignore)
- Ready for live testing with Prabowo speech or other Indonesian content
