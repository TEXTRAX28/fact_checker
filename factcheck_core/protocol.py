import json
import re

def _parse_provider_array(text: str) -> tuple[list, bool]:
    text = re.sub(r"```(?:json)?\n?|```", "", text)
    # quote bare enums (e.g. "verdict": TRUE -> "verdict": "TRUE"). Anchored to the "verdict" key specifically, not just any ": TRUE"/": FALSE" otherwise a colon inside an explanation string gets corrupted too.
    text = re.sub(r'("verdict"\s*:\s*)(UNVERIFIABLE|TRUE|FALSE)\b', r'\1"\2"', text)
    text = re.sub(r",\s*([}\]])", r"\1", text)  # Remove trailing commas
    # Strip backslash-escapes JSON doesn't recognize (e.g. "\_", a markdown-style
    # underscore escape the model sometimes emits in legal citations like "607 U.S. \_\_\_"),
    # a real, live-reproduced cause of raw_decode failing on an otherwise-valid response.
    # Consumes escape pairs atomically so a valid "\\" isn't split into "\" + a stray second
    # backslash that then looks invalid on its own.
    text = re.sub(r'\\(.)', lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu' else m.group(1), text)

    # Full array parse first, via raw_decode from the first '[' rather than a regex
    # spanning greedily to the LAST ']' in the text - a stray bracket in trailing
    # prose after a genuinely complete array must not corrupt an otherwise-good match.
    decoder = json.JSONDecoder()
    start = text.find("[")
    if start != -1:
        try:
            parsed, _ = decoder.raw_decode(text, start)
            if isinstance(parsed, list):
                return parsed, True
        except json.JSONDecodeError:
            pass

    # Fallback: scan for complete top-level objects using the JSON parser itself, not
    # a `{[^{}]*}` regex - that regex explicitly excludes nested braces, so a claim
    # or sources value that isn't a plain string (a real, confirmed cause of
    # ProviderProtocolError, not a guess) silently matched nothing. Runs even when
    # the array is truncated mid-output (no closing ]); keeps every complete object
    # found before the cutoff, same as before.
    objects = []
    pos = 0
    while pos < len(text):
        brace = text.find("{", pos)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace)
            if isinstance(obj, dict):
                objects.append(obj)
            pos = end
        except json.JSONDecodeError:
            pos = brace + 1
    return objects, bool(objects)

