import re

COLORS = {
    "TRUE":         "\033[92m",  # bright green
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

        sources = r.get("sources", [])

        if sources:
            print(f"  Sources ({len(sources)}):")
            for s in sources:
                print(f"    = {_format_source(s)}")
                
                