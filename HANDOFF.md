# Fact-Checker Handoff (Jun 29, 2026)

## Project Status: WORKING

A real-time fact-checker that verifies claims from 4 input sources using Groq LLM + Tavily web search.

---

## What Works

| Mode | Status | Notes |
|---|---|---|
| Mode 3: Article URL | FULL | 3-tier fallback (Jina → Wayback → Tavily), paywalled warnings |
| Mode 4: Paste text | FULL | Clean article extraction, batch fact-checking |
| Mode 2: Live stream | WORKING | Duplicate detection, speaker numbering (SPEAKER_A/B/C) |
| Mode 1: Microphone | COMING SOON | Disabled (too complex, requires 3+ libraries). Shows Coming Soon message. |

---

## Fixes Applied Today

1. Search snippet size: 300 to 600 chars (fixes fresh news verdict misses)
2. URL fallback ladder: Jina → Wayback → Tavily with content validation
3. EXTRACT_PROMPT: Removed Bcs typo that broke claim extraction
4. Mode 2 infinite loop: Added exit condition (3 consecutive capture failures)
5. Speaker numbering: Each chunk gets SPEAKER_A, SPEAKER_B, etc.
6. Duplicate transcripts: Skip >85% similar chunks (prevents duplicate fact-checking)
7. Max tokens: Increased VERIFY from 2000 to 3500 (supports 10+ verdicts)
8. CLAUDE.md: Updated to reflect Groq+Tavily implementation (not Anthropic)
9. .gitignore: Cleaned from 202 to 45 lines (removed Django/Flask/Celery bloat)
10. CLAUDE.md & STATUS.md: Removed from GitHub, kept locally in .gitignore
11. Mode 1: Disabled with Coming Soon message

---

## How to Run

\\\ash
cd "C:\Users\natan\VSC Code\fact-checker"
python main.py
\\\

Then choose:
- 1 = Microphone (Coming Soon)
- 2 = YouTube/live stream URL
- 3 = Article URL
- 4 = Paste text

---

## Environment Setup

Create .env file:
\\\
GROQ_API_KEY=gsk_...
TAVILY_API_KEY=tvly_...
HUGGINGFACE_TOKEN=hf_...    (Only for Mode 1)
\\\

Install dependencies:
\\\ash
pip install -r requirements.txt
\\\

---

## Tech Stack

| Component | Technology | Purpose |
|---|---|---|
| Language | Python 3.x | Core application |
| LLM Model | Llama 3.3 70B (via Groq) | Claim extraction & verification |
| Web Search | Tavily Search API | Find evidence for claims |
| Transcription | Groq Whisper API | Convert speech to text |
| Web Scraping | Jina Reader API | Extract article text |
| Archive Access | Wayback Machine API | Access archived page versions |
| Async Processing | ThreadPoolExecutor | Parallel search & fact-checking |

---

## File Structure

\\\
fact-checker/
├── main.py           (Entry point, 4 modes)
├── fact_checker.py   (Groq LLM + Tavily search)
├── display.py        (Terminal output)
├── transcriber.py    (Groq Whisper API)
├── system_prompt.md  (Archived reference)
├── README.md         (Overview & tech stack)
├── HANDOFF.md        (This file)
├── STATUS.md         (Project status)
├── requirements.txt  (Dependencies)
├── .env              (API keys - GITIGNORED)
└── .gitignore        (Clean rules)
\\\

---

## Known Limitations

1. Speaker diarization: Per-chunk numbering, not voice-based
   - Same person across 2 chunks appears as different speakers
   - True identification would require pyannote.audio

2. Indonesian news: Fully supported but untested
   - System has Tier 1/2/3 source weighting
   - Ready to use on Indonesian content

3. Mode 1 (Microphone): Disabled
   - Requires: faster-whisper, pyannote.audio, HUGGINGFACE_TOKEN
   - Complex setup, untested implementation
   - Planned for future release

---

## Next Steps

- Test Mode 1 if needed (install dependencies, check errors)
- Test Indonesian speech/articles heavily
- Monitor Groq/Tavily token usage for cost
- Optional: Add result caching (currently disabled per CLAUDE.md)

---

## Recent Commits

fc337b3 Disable Mode 1 (Microphone) with Coming Soon message
6e7702e Add comprehensive Tech Stack section to README
53baf5f Add handoff document for future sessions
3a0852f Stop tracking CLAUDE.md and STATUS.md
d5f44e0 Increase VERIFY max_tokens 2000-3500
c8bbb35 Skip duplicate transcripts in Mode 2
c61393b Fix Mode 2: add exit condition when stream ends
14e6c56 Add speaker numbering for Mode 2
f5e9f67 Clean up .gitignore
3cd3854 Update CLAUDE.md: reflect Groq+Tavily
1d385f2 Update STATUS.md with completed fixes
df390ec Improve URL fetching robustness

---

## Quick Debug Tips

Mode 2 gets stuck:
- Video might have looping audio
- Deduplication should skip it (>85% similarity check)
- Press Ctrl+C to stop

Mode 3 returns "No checkable claims":
- Article might be text-light or poorly structured
- Check Jina fetched enough content (>500 chars)
- Warnings show fallback chain (Jina → Wayback → Tavily)

LLM not responding:
- Check GROQ_API_KEY is valid
- Check free tier limit not exceeded (console.groq.com)
- Verify internet connection

---

Project is ready for live testing with Modes 2, 3, 4.
