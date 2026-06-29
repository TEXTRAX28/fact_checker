# Real-Time Fact Checker

## Simple Overview

A real-time fact-checking tool that listens to speech (microphone, live streams, articles, or text) and instantly verifies claims against web sources. Returns verdicts (TRUE/FALSE/MISLEADING/UNVERIFIABLE) with confidence scores and source links.

**4 input modes:**
- 🎤 **Microphone** — Live speech fact-checking
- 📺 **Live stream URL** — YouTube/news broadcast verification
- 🔗 **Article URL** — Web article claim extraction & verification
- 📝 **Paste text** — Copy-paste content fact-checking

---

## In-Depth Project Overview

The Fact Checker is an intelligent verification system designed to combat misinformation in real-time. It operates on a three-stage pipeline: **capture → extract → verify**.

At its core, the system accepts factual claims from multiple sources (live speech via microphone, streaming video URLs from YouTube or news networks, web articles via URL, or manually pasted text). The input is processed through Groq's Whisper API (for audio transcription) or direct text intake. Claims are then extracted using Groq's llama-3.3-70b language model, which identifies the 10 most verifiable and specific factual statements from the input while filtering out opinions, predictions, and vague rhetoric.

Once claims are extracted, the system immediately searches for supporting or contradicting evidence using Tavily's web search API, conducting parallel searches for speed. Finally, the same 70B language model evaluates each claim against the search results and assigns a verdict (TRUE for claims matching sources, FALSE for contradictions, MISLEADING for technically true but deceptively framed claims, and UNVERIFIABLE for conflicting sources). Each verdict includes a confidence score (60-100%), a concise explanation (1-3 sentences), and direct source citations.

The system is built for **real-time performance**: Mode 2 (live streams) continuously captures 30-second audio chunks, detects and skips duplicate content to avoid redundant fact-checking, and intelligently identifies speaker transitions. Mode 3 (URLs) includes a robust 3-tier fallback chain—Jina reader for direct content extraction, Wayback Machine for archived versions, and Tavily search for coverage-based verification—ensuring articles behind paywalls or bot-blocked pages can still be fact-checked via alternative sources. The system scales to handle multiple claims per input and provides deduplication to prevent re-checking identical statements.

**Use cases include:**
- Journalists and media outlets verifying breaking news in real-time
- Debate moderators fact-checking live statements during broadcasts
- Content creators identifying misinformation in viral videos or articles
- Researchers and fact-checking organizations automating claim verification
- Indonesian news verification with specialized source credibility hierarchy

The fact-checker prioritizes accuracy over speed: search snippets are 600 characters (not truncated), verification uses 3500 tokens to support 10+ concurrent verdicts, and Indonesian news sources are weighted by editorial credibility.

---

## Tech Stack

| Component | Technology | Purpose |
|---|---|---|
| **Language** | Python 3.x | Core application |
| **LLM Model** | Llama 3.3 70B (via Groq) | Claim extraction & verification |
| **Web Search** | Tavily Search API | Find evidence for claims |
| **Audio Transcription** | Groq Whisper API | Convert speech to text |
| **Speaker Identification** | pyannote.audio | Identify speakers (Mode 1 only) |
| **Web Scraping** | Jina Reader API | Extract article text |
| **Archive Access** | Wayback Machine API | Access archived page versions |
| **Audio Capture** | sounddevice + numpy | Microphone input streaming |
| **Parallel Processing** | ThreadPoolExecutor | Concurrent search operations |
| **Output** | JSON + Terminal UI | Color-coded results display |

**Cloud Services (API-based):**
- **Groq** — LLM inference (Llama 70B) + Whisper transcription
  - Free tier: ~5,000 tokens/day
  - Pricing: \.15/1M input tokens, \.60/1M output tokens
- **Tavily** — Web search API
  - Free tier: ~100 searches/month
  - Returns top 3 results per query with 600-char snippets
- **Jina Reader** — Article text extraction
  - No auth required, free tier available
- **Wayback Machine** — Internet Archive snapshots
  - Free, no authentication needed

**Local Optional Dependencies (Mode 1 only):**
- aster-whisper — Local speech-to-text (GPU-accelerated fallback)
- pyannote.audio — Speaker diarization (requires HuggingFace token)
- sounddevice — Microphone input
- 
umpy — Audio processing

---

## How It Works

### Step 1: Capture
- **Microphone:** Record audio in 30-second chunks, skip silence
- **Live Stream:** Download best audio quality, process in 30-second segments
- **URL:** Fetch article via Jina → Wayback → Tavily fallback chain
- **Text:** Accept manually pasted content

### Step 2: Extract Claims
- Convert audio to text (Whisper API)
- Send transcript to Llama 70B model
- Model extracts top 10 most specific, verifiable claims
- Filter out opinions, predictions, rhetorical questions

### Step 3: Verify
- For each claim, search web via Tavily (600-char snippets, top 3 results)
- Send claim + search results to Llama 70B
- Model assigns verdict: TRUE / FALSE / MISLEADING / UNVERIFIABLE
- Return confidence score (60-100%), explanation, and source URLs

### Smart Features
- **Deduplication:** Skip >85% similar transcripts (prevents re-checking)
- **Speaker tracking:** Number speakers (SPEAKER_A, SPEAKER_B, etc.)
- **Fallback chain:** If paywall blocks Jina, try archive then search
- **Parallel search:** Search 10 claims simultaneously for speed
- **Indonesian support:** Tier 1/2/3 source weighting for local accuracy

---

## Quick Start

`ash
python main.py
`

Choose a mode (1-4), provide input, and get instant verdicts with sources.

See [HANDOFF.md](HANDOFF.md) for detailed setup and troubleshooting.
