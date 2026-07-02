from dotenv import load_dotenv
from fact_checker import fact_check
from display import show_results

load_dotenv() or load_dotenv(".env")


# Feature #1: mic (coming soon)
def run_mic():
    print("Mode 1 (Microphone) — Coming Soon")


# Feature #2: Live stream (coming soon)
def run_stream():
    print("Mode 2 (Live stream) — Coming Soon")


# Feature #3: URL
MIN_PARAGRAPHS = 5

def _usable(raw: str | None, minimum: int = MIN_PARAGRAPHS) -> bool:
    # Gate on real content after cleaning, not raw length: a bot-blocked page can be 500+ chars of nav/menu junk that _clean_article strips down to almost nothing.
    return bool(raw) and _clean_article(raw).count("[SPEAKER_A]") >= minimum

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
        if _usable(content):
            return content, ""
        warning = "[Warning] Could not fully read that page (paywalled, bot-blocked, or JS-rendered).\n"
    except Exception:
        pass

    # Tier 2: Wayback Machine
    if not _usable(content):
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
                if _usable(content):
                    return content, warning + "  Fetched from Wayback Machine.\n" if warning else ""
        except Exception:
            pass

    # Tier 3: Tavily search (last resort, accept any usable snippet, not full-article length)
    if not _usable(content):
        try:
            from fact_checker import _tavily_
            domain = url.split("/")[2]
            results = _tavily_().search(f"site:{domain}", max_results=1)
            if results.get("results"):
                content = results["results"][0]["content"]
                if _usable(content, minimum=1):
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
    # Passed as fact_check's on_result callback so each verdict prints the
    # moment it's ready, instead of waiting for the whole batch.
    show_results([result])

def run_article():
    try:
        url = input("Article URL: ").strip()
        if not url:
            print("ERROR: URL cannot be empty")
            return

        print("Fetching article...")
        try:
            raw, warning = _fetch_article(url)
            if not raw:
                print(f"ERROR: {warning or 'Could not fetch that URL. Tried: Jina Reader → Wayback Machine → Tavily Search'}")
                return
            if warning:
                print(warning)
        except Exception as e:
            print(f"ERROR while fetching: {type(e).__name__}: {e}")
            return

        try:
            text = _clean_article(raw)
            if not text:
                print("ERROR: Could not extract any readable content from article (may be too short or heavily formatted)")
                return
        except Exception as e:
            print(f"ERROR while cleaning article: {type(e).__name__}: {e}")
            return

        count = text.count("[SPEAKER_A]")
        if count < 1:
            print("ERROR: No paragraphs extracted from article")
            return

        print(f"Extracted {count} paragraphs. Fact-checking...\n")
        fact_check(text, on_result=_print_one_result, verbose=True)

    except KeyboardInterrupt:
        print("\nCancelled by user")
    except Exception as e:
        print(f"ERROR: Unexpected error: {type(e).__name__}: {e}")


# Feature #4: Text
def run_text():
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

        try:
            text = _clean_article(raw)
            if not text:
                print("ERROR: Could not extract any content (text may be too short or heavily formatted)")
                return
        except Exception as e:
            print(f"ERROR while cleaning text: {type(e).__name__}: {e}")
            return

        count = text.count("[SPEAKER_A]")
        if count < 1:
            print("ERROR: No paragraphs extracted from text")
            return

        print(f"\nExtracted {count} paragraphs. Fact-checking...\n")
        fact_check(text, on_result=_print_one_result, verbose=True)

    except KeyboardInterrupt:
        print("\nCancelled by user")
    except Exception as e:
        print(f"ERROR: Unexpected error: {type(e).__name__}: {e}")
        

def main():
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
            run_mic()
        elif choice == "2":
            run_stream()
        elif choice == "3":
            run_article()
        elif choice == "4":
            run_text()
        else:
            print("Invalid choice.")
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
