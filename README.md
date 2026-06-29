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

## Quick Start

\\\ash
python main.py
\\\

Choose a mode (1-4), provide input, and get instant verdicts with sources.

See [HANDOFF.md](HANDOFF.md) for detailed setup and troubleshooting.
