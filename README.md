# Real-Time Fact Checker

## Simple Overview

A real-time fact-checking tool that listens to speech (microphone, live streams, articles, or text) and instantly verifies claims against web sources. Returns verdicts (TRUE/FALSE/MISLEADING/UNVERIFIABLE) with confidence scores and source links.

**4 input modes:**
- **Microphone** -> Live speech fact-checking
- **Live stream URL** -> YouTube/news broadcast verification
- **Article URL** -> Web article claim extraction & verification
- **Paste text** -> Copy-paste content fact-checking

---

## In-Depth Project Overview

The Fact Checker is an intelligent verification system designed to combat misinformation in real-time. It operates on a three-stage pipeline: **capture -> extract -> verify**.

The system accepts information from different sources, including live microphone input, YouTube or news livestreams, website URLs, and manually pasted text. If the input contains audio, it uses Groq's Whisper API to convert speech into text. Text is then analyzed by Groq's Llama 3.3 70B model, which extracts the 10 most specific and fact-based claims, while ignoring opinions, predictions, or unclear statements.

After the claims are extracted, the system searches the internet using Tavily's web search API to find reliable evidence. Multiple searches are performed at the same time to improve speed. The Llama 3.3 70B model then compares each claim with the search results and classifies it as TRUE, FALSE, MISLEADING, or UNVERIFIABLE. Each result also includes a confidence score (60–100%), a short explanation, and links to the supporting sources.

The system is designed to work in real time. For live streams, it processes 30-second audio segments, skips repeated content to avoid checking the same claim multiple times, and detects when different people are speaking. For website URLs, it uses a three-step fallback method: first trying Jina Reader to extract the article, then the Wayback Machine if the page cannot be accessed, and finally Tavily Search to gather evidence from other trusted sources. This allows the system to verify content even if an article is behind a paywall or blocks automated access. It can also verify multiple claims from a single input while avoiding duplicate fact-checking.

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
- **Groq** = LLM inference (Llama 70B) + Whisper transcription
  - Free tier: ~5,000 tokens/day
- **Tavily** = Web search API
  - Free tier: ~100 searches/month
  - Returns top 3 results per query with 600-char snippets
- **Jina Reader** = Article text extraction
  - No auth required, free tier available
- **Wayback Machine** = Internet Archive snapshots
  - Free, no authentication needed

**Local Optional Dependencies (Mode 1 only)(Upcoming):**
- faster-whisper = Local speech-to-text (GPU-accelerated fallback)
- pyannote.audio = Speaker diarization (requires HuggingFace token)
- sounddevice = Microphone input
---

## How It Works

### Step 1: Capture
- **Microphone:** Record audio in 30-second chunks, skip silence
- **Live Stream:** Download best audio quality, process in 30-second segments
- **URL:** Fetch article via Jina -> Wayback -> Tavily fallback chain
- **Text:** Accept manually pasted content

### Step 2: Extract Claims
- Convert audio to text (Whisper API)
- Send transcript to Llama 70B model
- Model extracts specific and verifable claims
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
```bash
python main.py
```

Choose a mode (1-4), provide input, and get instant verdicts with sources.

