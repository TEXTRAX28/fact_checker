from dotenv import load_dotenv
from fact_checker import fact_check
from display import show_results

load_dotenv()


# Feature #1: mic
def run_mic(verbose: bool = False):
    from mic import run_mic as _run_mic
    _run_mic(verbose)


# Feature #2: Live stream (coming soon)
def run_stream():
    print("Mode 2 (Live stream) - Coming Soon")

MIN_PARAGRAPHS = 5

def _usable(raw: str | None, minimum: int = MIN_PARAGRAPHS) -> bool:
    # Gate on real content after cleaning, not raw length: a bot-blocked page can be 500+ chars of nav/menu junk that _clean_article strips down to almost nothing.
    return bool(raw) and _clean_article(raw).count("[SPEAKER_A]") >= minimum

def _fetch_article(url: str, verbose: bool = False) -> tuple[str | None, str]:
    from urllib.request import urlopen, Request
    from urllib.parse import urlparse
    import json
    import time

    def _vlog(label: str, start: float, extra: str = ""):
        if verbose:
            print(f"[v] {label} done in {time.perf_counter() - start:.2f}s{extra}")

    warning = ""
    content = None

    # Tier 1: Jina reader
    print("Trying Jina Reader...")
    start = time.perf_counter()
    try:
        req = Request(
            f"https://r.jina.ai/{url}",
            headers={
                "Accept": "text/plain",
                "User-Agent": "Mozilla/5.0",
                "X-Remove-Selector": "nav, footer, header, aside",
                "X-Retain-Images": "none",
            },
        )
        content = urlopen(req, timeout=15).read().decode("utf-8")
        _vlog("Jina fetch", start, f" ({len(content)} chars)")
        if _usable(content):
            return content, "Fetched from Jina AI.\n"
        warning = "[Warning] Could not fully read that page (paywalled, bot-blocked, or JS-rendered).\n"
    except Exception as e:
        _vlog("Jina fetch", start, f" (failed: {type(e).__name__}: {e})")

    # Tier 2: Wayback Machine
    if not _usable(content):
        print("Jina Reader unavailable, trying Wayback Machine...")
        try:
            start = time.perf_counter()
            req = Request(
                f"https://archive.org/wayback/available?url={url}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp = json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
            _vlog("Wayback availability check", start)
            snapshot_url = resp.get("archived_snapshots", {}).get("closest", {}).get("url")
            if snapshot_url:
                start = time.perf_counter()
                req = Request(snapshot_url, headers={"User-Agent": "Mozilla/5.0"})
                content = urlopen(req, timeout=15).read().decode("utf-8")
                _vlog("Wayback snapshot fetch", start, f" ({len(content)} chars)")
                if _usable(content):
                    return content, warning + "Fetched from Wayback Machine.\n"
        except Exception as e:
            _vlog("Wayback fetch", start, f" (failed: {type(e).__name__}: {e})")

    # Tier 3: Tavily search (last resort, accept any usable snippet, not full-article length)
    if not _usable(content):
        print("Wayback Machine unavailable, trying Tavily search...")
        try:
            from fact_checker import _tavily_
            normalized_url = url if "://" in url else f"https://{url}"
            domain = urlparse(normalized_url).netloc
            start = time.perf_counter()
            results = _tavily_().search(f"site:{domain}", max_results=1)
            _vlog("Tavily fallback search", start)
            if results.get("results"):
                content = results["results"][0]["content"]
                if _usable(content, minimum=1):
                    return content, f"{warning}Fetched from Tavily.\n"
        except Exception as e:
            _vlog("Tavily fallback search", start, f" (failed: {type(e).__name__}: {e})")

    return None, warning + "  Could not fetch any usable content.\n" if warning else "Could not fetch article.\n"

def _clean_article(raw: str) -> str:
    import re
    # strip Jina header (everything before and including "Markdown Content:")
    if "Markdown Content:" in raw:
        raw = raw.split("Markdown Content:", 1)[1].strip()
    content = raw
    # strip markdown noise (multiline-safe for images that span lines)
    content = re.sub(r"!\[[\s\S]*?\]\([^)]*\)", "", content) # Images
    content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content) # MD links but keep the content
    content = re.sub(r"https?://\S+", "", content) # URL's
    content = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE) # Markdown
    content = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", content) # Bold and italic format
    paragraphs = []
    for p in content.split("\n\n"):
        stripped = p.strip()
        if len(stripped) > 40:
            paragraphs.append(stripped)

    labeled_lines = []
    for p in paragraphs[:60]:  # Max of 60 paragraphs
        labeled_lines.append(f"[SPEAKER_A] {p}")

    return "\n".join(labeled_lines)

def _print_one_result(result: dict):
    # Passed as fact_check's on_result callback so each verdict prints the moment it's ready, instead of waiting for the whole batch.
    show_results([result])

def _run_fact_check(raw: str, verbose: bool, label: str):
    # Shared by run_article/run_text: they gather raw text differently (fetch vs paste),
    # but clean -> count -> fact_check is identical from here on.
    try:
        text = _clean_article(raw)
        if not text:
            print(f"ERROR: Could not extract any readable content from {label} (may be too short or heavily formatted)")
            return
    except Exception as e:
        print(f"ERROR while cleaning {label}: {type(e).__name__}: {e}")
        return

    count = text.count("[SPEAKER_A]")
    if count < 1:
        print(f"ERROR: No paragraphs extracted from {label}")
        return

    print(f"\nExtracted {count} paragraphs. Fact-checking...\n")
    fact_check(text, on_result=_print_one_result, verbose=verbose)

# Feature #3: URL
def run_article(verbose: bool = False):
    try:
        url = input("Article URL: ").strip()
        if not url:
            print("ERROR: URL cannot be empty")
            return

        print("Fetching article...")
        try:
            raw, warning = _fetch_article(url, verbose)
            if not raw:
                print(f"ERROR: {warning or 'Could not fetch that URL. Tried: Jina Reader → Wayback Machine → Tavily Search'}")
                return
            if warning:
                print(warning)
        except Exception as e:
            print(f"ERROR while fetching: {type(e).__name__}: {e}")
            return

        _run_fact_check(raw, verbose, "article")

    except KeyboardInterrupt:
        print("\nCancelled by user")
    except Exception as e:
        print(f"ERROR: Unexpected error: {type(e).__name__}: {e}")


# Feature #4: Text
def run_text(verbose: bool = False):
    try:
        print("Paste your text, then press Enter twice when done:")
        lines = []
        while True:
            line = input()
            if not line and lines and not lines[-1]:
                break
            lines.append(line)

        raw = "\n".join(lines).strip()
        if not raw:
            print("ERROR: No text provided")
            return

        _run_fact_check(raw, verbose, "text")

    except KeyboardInterrupt:
        print("\nCancelled by user")
    except Exception as e:
        print(f"ERROR: Unexpected error: {type(e).__name__}: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Real-Time Fact Checker")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="Log every external call (DeepInfra, Tavily, Jina, Wayback) with timing")
    args = parser.parse_args()

    print("Real-Time Fact Checker")
    print("Model: Llama 3.3 70B (DeepInfra)\n")

    # Mode selection
    print("Input mode:")
    print("  1. Microphone (live)")
    print("  2. Live stream URL (YouTube/news)")
    print("  3. Article URL")
    print("  4. Paste text / paragraph")
    choice = input("\n> ").strip()

    try:
        if choice == "1":
            run_mic(args.verbose)
        elif choice == "2":
            run_stream()
        elif choice == "3":
            run_article(args.verbose)
        elif choice == "4":
            run_text(args.verbose)
        else:
            print("ERROR: Invalid choice, pick 1-4")
    except KeyboardInterrupt:
        print("Stopped.")

if __name__ == "__main__":
    main()
