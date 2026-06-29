import os
import queue
import threading
import subprocess
import tempfile
import concurrent.futures
from dotenv import load_dotenv
from fact_checker import fact_check
from display import show_results

load_dotenv() or load_dotenv(".env.example")

SAMPLE_RATE = 16000
CHUNK_SECONDS = 5
SILENCE_THRESHOLD = 0.01  # ponytail: raise if mic picks up too much background noise

audio_queue: queue.Queue = queue.Queue()
transcript_queue: queue.Queue[str] = queue.Queue()


# fact checker 

def fact_check_loop():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures: set = set()
        while True:
            try:
                transcript = transcript_queue.get(timeout=0.1)
                futures.add(pool.submit(fact_check, transcript))
            except queue.Empty:
                pass
            done = {f for f in futures if f.done()}
            for f in done:
                try:
                    results = f.result()
                    if results:
                        show_results(results)
                except Exception as e:
                    print(f"[error] {e}")
            futures -= done


# Feature #1: mic (DISABLED — Coming Soon)

def run_mic():
    print("\n⏳ Mode 1 (Microphone) — Coming Soon\n")
    print("This feature is under development. Requires:")
    print("  • faster-whisper (local transcription)")
    print("  • pyannote.audio (speaker diarization)")
    print("  • HUGGINGFACE_TOKEN environment variable\n")
    print("For now, use:")
    print("  • Mode 2: Live stream URLs")
    print("  • Mode 3: Article URLs")
    print("  • Mode 4: Paste text\n")


# Feature #2: URL video such as youtube etc

def _resolve_stream(url: str) -> str | None:
    r = subprocess.run(["yt-dlp", "-g", "-f", "bestaudio", url], capture_output=True, text=True)
    line = r.stdout.strip().split("\n")[0]
    return line or None

def _capture_stream_chunk(stream_url: str, seconds: int = 30) -> str | None:
    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    r = subprocess.run([
        "ffmpeg", "-y", "-i", stream_url,
        "-t", str(seconds), "-ar", "16000", "-ac", "1", "-f", "wav", out.name,
    ], capture_output=True)
    return out.name if r.returncode == 0 else None

def _stream_capture(url: str):
    from transcriber import transcribe_file
    import difflib
    print("Resolving stream URL...")
    stream_url = _resolve_stream(url)
    if not stream_url:
        print("Could not resolve stream. Make sure yt-dlp and ffmpeg are installed.")
        return
    print("Stream active. Capturing 30s chunks. Ctrl+C to stop.\n")
    fail_count = 0
    chunk_num = 0
    last_transcript = ""
    while True:
        path = _capture_stream_chunk(stream_url)
        if not path:
            fail_count += 1
            if fail_count >= 3:  # Exit after 3 consecutive failures (stream ended)
                print("\nStream ended.")
                break
            continue
        fail_count = 0
        chunk_num += 1
        result = transcribe_file(path)
        os.unlink(path)
        if result:
            # Skip if transcript is >85% similar to last one (duplicate/repetitive content)
            similarity = difflib.SequenceMatcher(None, result, last_transcript).ratio()
            if similarity > 0.85:
                continue
            last_transcript = result

            # Replace [UNKNOWN] with speaker label based on chunk order
            speaker_label = f"SPEAKER_{chr(64 + chunk_num)}"  # A, B, C, D, etc.
            result = result.replace("[UNKNOWN]", f"[{speaker_label}]")
            print(f"[transcript] {result}")
            transcript_queue.put(result)

def run_stream():
    url = input("Stream URL (YouTube live, BBC, etc.): ").strip()
    threading.Thread(target=_stream_capture, args=(url,), daemon=True).start()
    fact_check_loop()


# Feature #3: 

def _fetch_article(url: str) -> tuple[str | None, str]:
    from urllib.request import urlopen, Request
    import json

    warning = ""
    content = None

    # Tier 1: Jina reader
    try:
        req = Request(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain", "User-Agent": "Mozilla/5.0"},
        )
        content = urlopen(req, timeout=15).read().decode("utf-8")
        if len(content) >= 500:
            return content, ""
        warning = "⚠ Could not fully read that page (paywalled, bot-blocked, or JS-rendered).\n"
    except Exception:
        pass

    # Tier 2: Wayback Machine
    if not content or len(content) < 500:
        try:
            req = Request(
                f"https://archive.org/wayback/available?url={url}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp = json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
            snapshot_url = resp.get("archived_snapshots", {}).get("closest", {}).get("url")
            if snapshot_url:
                req = Request(snapshot_url, headers={"User-Agent": "Mozilla/5.0"})
                content = urlopen(req, timeout=15).read().decode("utf-8")
                if len(content) >= 500:
                    return content, warning + "  Fetched from Wayback Machine.\n" if warning else ""
        except Exception:
            pass

    # Tier 3: Tavily search
    if not content or len(content) < 500:
        try:
            from fact_checker import _tavily_
            domain = url.split("/")[2]
            results = _tavily_().search(f"site:{domain}", max_results=1)
            if results.get("results"):
                content = results["results"][0]["content"]
                if len(content) >= 500:
                    return content, warning + "  Fact-checking based on search coverage.\n" if warning else ""
        except Exception:
            pass

    return None, warning + "  Could not fetch any usable content.\n" if warning else "Could not fetch article.\n"

def _clean_article(raw: str) -> str:
    import re
    # strip Jina header (everything before and including "Markdown Content:")
    if "Markdown Content:" in raw:
        raw = raw.split("Markdown Content:", 1)[1].strip()
    content = raw
    # strip markdown noise (multiline-safe for images that span lines)
    content = re.sub(r"!\[[\s\S]*?\]\([^)]*\)", "", content)
    content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
    content = re.sub(r"https?://\S+", "", content)
    content = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE)
    content = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", content)
    paragraphs = [p.strip() for p in content.split("\n\n") if len(p.strip()) > 40]
    return "\n".join(f"[SPEAKER_A] {p}" for p in paragraphs[:25])

def run_article():
    url = input("Article URL: ").strip()
    print("Fetching article...")
    raw, warning = _fetch_article(url)
    if not raw:
        print(warning or "Could not fetch that URL.")
        return
    if warning:
        print(warning)
    text = _clean_article(raw)
    if not text:
        print("Could not extract article content.")
        return
    count = text.count("[SPEAKER_A]")
    print(f"Extracted {count} paragraphs. Fact-checking...\n")
    results = fact_check(text)
    if results:
        show_results(results)
    else:
        print("No checkable claims found.")


# Feature #4: Text

def run_text():
    print("Paste your text, then press Enter three times:")
    lines = []
    while True:
        line = input()
        if not line and lines and not lines[-1]:
            break
        lines.append(line)
    raw = "\n".join(lines).strip()
    if not raw:
        return
    text = _clean_article(raw)
    if not text:
        print("Could not extract any content.")
        return
    count = text.count("[SPEAKER_A]")
    print(f"\nExtracted {count} paragraphs. Fact-checking...\n")
    results = fact_check(text)
    if results:
        show_results(results)
    else:
        print("No checkable claims found.")


# ── entry point ───────────────────────────────────────────────────────────────

MODES = {
    "1": ("Microphone (live)",              run_mic),
    "2": ("Live stream URL (YouTube/news)", run_stream),
    "3": ("Article URL",                    run_article),
    "4": ("Paste text / paragraph",         run_text),
}

def main():
    print("Real-Time Fact Checker\n")
    for k, (label, _) in MODES.items():
        print(f"  {k}. {label}")
    choice = input("\n> ").strip()
    _, fn = MODES.get(choice, (None, None))
    if fn is None:
        print("Invalid choice.")
        return
    try:
        fn()
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
