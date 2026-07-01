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

The system accepts information from different sources, including live microphone input, YouTube or news livestreams, website URLs, and manually pasted text. If the input contains audio, it uses local faster-whisper to convert speech into text. Text is then analyzed by the Llama 3.3 70B model (served via DeepInfra), which extracts the 10 most specific and fact-based claims, while ignoring opinions, predictions, or unclear statements.

After the claims are extracted, the system searches the internet using Tavily's web search API to find reliable evidence. Multiple searches are performed at the same time to improve speed. The Llama 3.3 70B model then compares each claim with the search results and classifies it as TRUE, FALSE, MISLEADING, or UNVERIFIABLE. Each result also includes a confidence score (60-100%), a short explanation, and links to the supporting sources.

The system is designed to work in real time. For live streams, it processes 30-second audio segments, skips repeated content to avoid checking the same claim multiple times, and labels different speakers. For website URLs, it uses a three-step fallback method: first trying Jina Reader to extract the article, then the Wayback Machine if the page cannot be accessed, and finally Tavily Search to gather evidence from other trusted sources. This allows the system to verify content even if an article is behind a paywall or blocks automated access. It can also verify multiple claims from a single input while avoiding duplicate fact-checking.

---

## Tech Stack

| Component | Technology | Purpose |
|---|---|---|
| **Language** | Python 3.x | Core application |
| **LLM Model** | Llama 3.3 70B (via DeepInfra) | Claim extraction & verification |
| **Web Search** | Tavily Search API | Find evidence for claims |
| **Audio Transcription** | Local faster-whisper | Convert speech to text |
| **Web Scraping** | Jina Reader API | Extract article text |
| **Archive Access** | Wayback Machine API | Access archived page versions |
| **Audio Capture** | sounddevice + numpy | Microphone input streaming |
| **Parallel Processing** | ThreadPoolExecutor | Concurrent search operations |
| **Output** | JSON + Terminal UI | Color-coded results display |

**Services:**
- **DeepInfra** = LLM inference (Llama 3.3 70B), OpenAI-compatible API
  - Roughly $0.30-0.50/month at light usage
- **Tavily** = Web search API
  - Free tier: ~100 searches/month
  - Returns top 3 results per query with 600-char snippets
- **Jina Reader** = Article text extraction
  - No auth required, free tier available
- **Wayback Machine** = Internet Archive snapshots
  - Free, no authentication needed

**Local dependencies (audio modes):**
- faster-whisper = local speech-to-text (GPU-accelerated)
- sounddevice = microphone input

---

## How It Works

### Step 1: Capture
- **Microphone:** Record audio in chunks, skip silence (Mode 1, coming soon)
- **Live Stream:** Download best audio quality, process in 30-second segments
- **URL:** Fetch article via Jina -> Wayback -> Tavily fallback chain
- **Text:** Accept manually pasted content

### Step 2: Extract Claims
- Convert audio to text (local faster-whisper) for audio modes
- Send transcript to the Llama 70B model
- Model extracts specific, verifiable claims
- Filter out opinions, predictions, rhetorical questions

### Step 3: Verify
- For each claim, search web via Tavily (600-char snippets, top 3 results)
- Send claim + search results to the Llama 70B model
- Model assigns verdict: TRUE / FALSE / MISLEADING / UNVERIFIABLE
- Return confidence score (60-100%), explanation, and source URLs

### Smart Features
- **Deduplication:** Skip >85% similar transcripts (prevents re-checking)
- **Speaker tracking:** Number speakers (SPEAKER_A, SPEAKER_B, etc.)
- **Fallback chain:** If paywall blocks Jina, try archive then search
- **Parallel search:** Search claims simultaneously for speed
- **Robust JSON parsing:** Recovers verdicts even when the model returns malformed or truncated JSON
- **Indonesian support:** Tier 1/2/3 source weighting for local accuracy

---

## Setup

Create a `.env` file in the project root:

```
DEEPINFRA_API_KEY=your_key_here
TAVILY_API_KEY=your_key_here
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Quick Start
```bash
python main.py
```

Choose a mode (1-4), provide input, and get instant prediction with sources.

# Testing something