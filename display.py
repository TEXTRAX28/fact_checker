import re

COLORS = {
    "TRUE":         "\033[92m",
    "FALSE":        "\033[91m",
    "MISLEADING":   "\033[93m",
    "UNVERIFIABLE": "\033[90m",
}
RESET = "\033[0m"

def _bar(confidence: int) -> str:
    filled = int(confidence) // 5
    return f"[{'=' * filled}{'-' * (20 - filled)}] {confidence}%"

def _format_source(url: str) -> str:
    note = " [Note: Wikipedia, community-edited]" if "wikipedia.org" in url else ""
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
        sources = r.get("sources") or ([r["source"]] if r.get("source") else [])
        if sources:
            print(f"  Sources ({len(sources)}):")
            for s in sources:
                print(f"    - {_format_source(s)}")

def show_comparison(comparison: dict):
    deepinfra = comparison.get("deepinfra", [])
    gemma = comparison.get("gemma", [])

    print("\n" + "="*80)
    print("COMPARISON: DeepInfra Llama 70B vs Gemma 4 12B")
    print("="*80 + "\n")

    if not deepinfra and not gemma:
        print("No results from either model.")
        return

    max_claims = max(len(deepinfra), len(gemma))

    for i in range(max_claims):
        d = deepinfra[i] if i < len(deepinfra) else None
        g = gemma[i] if i < len(gemma) else None

        if d and g:
            print(f"\nClaim {i+1}: {d.get('claim', g.get('claim', 'N/A'))}\n")
            print(f"[DeepInfra Llama 70B]")
            if d:
                verdict = d.get("verdict", "?")
                color = COLORS.get(verdict, "")
                print(f"  {color}[{verdict}]{RESET} Conf: {_bar(d.get('confidence', 0))}")
                explanation = re.sub(r'https?://\S+', '', d.get('explanation', '')).strip()
                print(f"  {explanation[:120]}...")
            else:
                print("  No result")

            print(f"\n[Gemma 4 12B (Local)]")
            if g:
                verdict = g.get("verdict", "?")
                color = COLORS.get(verdict, "")
                print(f"  {color}[{verdict}]{RESET} Conf: {_bar(g.get('confidence', 0))}")
                explanation = re.sub(r'https?://\S+', '', g.get('explanation', '')).strip()
                print(f"  {explanation[:120]}...")
            else:
                print("  No result")
        elif d:
            print(f"\nClaim {i+1}: {d.get('claim', 'N/A')}")
            print(f"[DeepInfra] {d.get('verdict', '?')} | [Gemma] No result")
        elif g:
            print(f"\nClaim {i+1}: {g.get('claim', 'N/A')}")
            print(f"[DeepInfra] No result | [Gemma] {g.get('verdict', '?')}")

    print("\n" + "="*80)
    print(f"DeepInfra: {len(deepinfra)} claims | Gemma: {len(gemma)} claims")
    print("="*80)
