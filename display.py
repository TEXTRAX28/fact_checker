import re

COLORS = {
    "TRUE":         "\033[92m",  # bright green
    "MOSTLY TRUE":  "\033[32m",  # green
    "PARTLY TRUE":  "\033[33m",  # yellow
    "MISLEADING":   "\033[93m",  # bright yellow
    "UNVERIFIABLE": "\033[90m",  # grey
    "FALSE":        "\033[91m",  # red
}
RESET = "\033[0m"

def _bar(confidence: int) -> str:
    filled = int(confidence) // 5
    return f"[{'=' * filled}{'-' * (20 - filled)}] {confidence}%"

def _format_source(url: str) -> str:
    if "wikipedia.org" in url:
        note = " [Note: Wikipedia, community-edited]"
    else:
        note = ""
    return f"{url}{note}"

def show_results(results: list[dict]):
    for r in results:
        verdict = r.get("verdict", "?")
        color = COLORS.get(verdict, "")
        print(f"\n{color}[{verdict}]{RESET}  {r.get('speaker', '?')}")
        print(f"  Claim:  {r.get('claim', '')}")
        print(f"  Conf:   {_bar(r.get('confidence', 0))}")
        explanation = re.sub(r'https?://\S+', '', r.get('explanation', '')).strip()
        print(f"  Why:    {explanation}")

        # support both "sources" (array) and legacy "source" (string)
        sources = r.get("sources")
        if not sources:
            if r.get("source"):
                sources = [r["source"]]
            else:
                sources = []

        if sources:
            print(f"  Sources ({len(sources)}):")
            for s in sources:
                print(f"    - {_format_source(s)}")

