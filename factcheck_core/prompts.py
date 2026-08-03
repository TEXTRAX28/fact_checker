MODEL = "gemini-3.5-flash-lite"

SEARCH_PROMPT = """Search the web for evidence about the factual claim below.
Return a concise evidence brief containing only facts that the cited web sources actually state.
Prefer primary, official, academic, and established news sources. Use multiple independent sources
when available. Do not decide a TRUE/FALSE verdict and do not rely on your training knowledge.
The separate verification stage will judge the claim using only this grounded evidence.

CLAIM:
"""

EXTRACT_PROMPT = """Extract up to 15 most specific and verifiable factual claims from the text.
Return a JSON array. Each item must have:
  "claim": a faithful, self-contained version of the factual claim. Preserve names, numbers,
           units, dates, and the source's wording wherever possible so the claim can still be
           located in the original text. Remove rhetorical intensifiers or subjective framing
           (such as "dangerous metabolic tidal wave", "violent", or "brutal") that cannot be
           independently verified. Never add a fact or inference that the source did not state.
  "query": a short search query that targets the underlying FACT, not just the names in the claim.
           e.g. for "X is president of Indonesia" use "current president of Indonesia" so the real
           answer is findable and the claim can be disproved if false.
           Include the specific named person, company, or organization tied to the claim so the
           query surfaces primary/official sources instead of generic aggregator or stats sites.
           e.g. for "ChatGPT reached one million users in five days" use
           "OpenAI Sam Altman ChatGPT one million users five days", not just "ChatGPT million users".
  "speaker": the name or label of who made the claim (e.g. "Senator Davis", "SPEAKER_A"), use "UNKNOWN" only if truly unidentifiable

Only include: statistics, numbers, dates, named events, quotes, scientific/medical/legal/historical
facts, and factual-sounding assertions about a named person or entity (identity, role, personal
attributes) even if sensitive or likely hard to verify.
Skip: subjective value judgments (e.g. "X is a bad person"), predictions, vague statements,
rhetorical questions. Do NOT skip a factual-sounding claim just because it's sensitive, personal,
or likely unverifiable (e.g. a claim about someone's identity, orientation, or private life) -
extract it; the verifier will return UNVERIFIABLE if no evidence exists either way.
If one sentence mixes checkable facts with rhetoric, extract only the factual core. If it combines
independent factual assertions that could receive different verdicts, split them into separate
claims. Keep tightly related quantities together only when they describe one calculation or whole.
Also skip routine procedural narration the source already states as plain, undisputed fact (a plea
entered, a filing date, a standard step in a legal/administrative process), UNLESS it contains a
specific number, quote, or attribution that could plausibly be misreported. Routine narration has
near-zero misinformation risk and isn't worth a search+verify call.

If two or more figures are stated as complementary parts of one whole (a percentage split, a
budget breakdown, a ratio that sums to a total), extract them as ONE claim describing the full
breakdown, not one claim per figure. e.g. for "60% from nilai manfaat, 40% from Bipih" extract
a single claim "the scheme is 60% nilai manfaat and 40% Bipih", not two separate claims. Splitting
them risks one being verified TRUE and the other FALSE even though they're the same fact.
For a claim about a named authority's limit, guideline, or recommendation, make the query target
that authority's official guidance and include the quantity being compared. For a calculated claim,
query for the underlying per-unit values and the named benchmark, not the dramatic conclusion.
Do not return duplicate or substantially equivalent claims when the text restates the same event,
ruling, policy, quotation, or statistic. Keep the clearest self-contained formulation and preserve
the earliest occurrence. Before returning, audit every proposed claim: if it contains independent
clauses that could receive different verdicts or need different evidence, split them into separate
claims. In particular, separate different rates, policies, dates, institutions, or actions joined by
"and", "while", "alongside", or a semicolon. Do not split a date, unit, attribution, or necessary
qualifier away from the proposition it describes.
For policy claims, preserve whether the text says announced, signed, scheduled, effective,
implemented, suspended, or currently active. Do not rewrite one temporal state as another.
Return [] if nothing is checkable.
Return ONLY the JSON array, no other text."""

VERIFY_PROMPT = """You are a fact-checker. Output ONLY a JSON array. No markdown. No analysis. No prose. Just the JSON array.

Each object in the array must have:
  "speaker": the speaker label
  "claim": the original claim text
  "supported": true ONLY if the evidence explicitly and directly confirms the claim. False otherwise,
               including when the evidence is simply silent or missing, silence is not support.
  "contradicted": true ONLY if the evidence explicitly and directly contradicts the claim (denies
                   the event, states a number that changes the claim's meaning, not just a rounding/
                   date/conversion difference, see NUMERIC TOLERANCE below before marking any numeric
                   mismatch as contradicted), OR the claim is comparative/superlative and fails the
                   historical-comparison test below. False if the evidence is merely silent, absent,
                   or doesn't mention the claim at all, absence is not contradiction.
  "verdict": one of TRUE / UNVERIFIABLE / FALSE, this field is informational only and will be
             recomputed from "supported"/"contradicted" downstream, but fill it in consistently:
             supported=true & contradicted=false -> TRUE
             contradicted=true & supported=false -> FALSE
             anything else (neither, or both) -> UNVERIFIABLE
  "explanation": 1-3 sentences, written in the SAME language as the claim text (e.g. claim in
                 Indonesian -> explanation in Indonesian, claim in English -> explanation in
                 English). Never mix languages within one explanation, regardless of what
                 language the search results/sources are in. Write each source_analysis "reason"
                 in that language too.
  "source_analysis": array with exactly one entry per numbered source shown in SEARCH
                      RESULTS below. Reference sources ONLY by the [N] index shown there -
                      never invent an index and never use a URL. Each entry:
                        "source_index": the [N] number of the source
                        "stance": one of SUPPORTS / CONTRADICTS / PARTIAL / IRRELEVANT / INSUFFICIENT
                        "directness": DIRECT if the source addresses the exact entity, number,
                                      and timeframe in the claim; INDIRECT if only generally related
                        "reason": one short sentence
                        "evidence_excerpt": a short exact quote (under 25 words) from the
                                            supplied GROUNDED SUMMARY supporting the stance,
                                            or null if the stance is IRRELEVANT or
                                            INSUFFICIENT. The supplied text is a
                                            Gemini-synthesized grounded segment, not a
                                            verbatim quote from the publisher.

SOURCE_ANALYSIS DEFINITIONS:
  SUPPORTS = this source directly and explicitly confirms the claim.
  CONTRADICTS = this source directly and explicitly denies or contradicts the claim.
  PARTIAL = this source confirms only part of a compound or qualified claim, not all of it.
  IRRELEVANT = this source discusses the same general topic but does not address the
               claim's specific entity, number, or timeframe - topical relevance alone is
               never SUPPORTS.
  INSUFFICIENT = this source might be relevant but lacks enough detail to decide either way.

"supported" and "contradicted" above must be consistent with source_analysis: set
supported=true only if at least one entry is SUPPORTS, and contradicted=true only if at
least one entry is CONTRADICTS. These two fields will also be recomputed downstream
directly from source_analysis, so an inconsistency here will be corrected automatically -
but make them agree in the first place.

The most common mistake is treating "I found no evidence either way" as FALSE. It is not, that is
UNVERIFIABLE. Only mark FALSE when the evidence actively says something different from the claim,
never because the evidence is merely absent or thin.

NUMERIC TOLERANCE (apply this BEFORE setting contradicted=true on any numeric claim):
A numeric claim is NOT contradicted just because a source states a different specific number.
Sources routinely round, convert currency, measure from a different baseline, or report a different
date. Only set contradicted=true if the gap is large enough to change what a reader would take away
from the claim, roughly: off by more than ~10%, off by an order of magnitude, or it crosses a
meaningful threshold the claim depends on (e.g. "over 1 million" vs an actual 800,000 changes the
claim's meaning; "1.05 million" vs an actual 1 million does not). If multiple sources disagree
slightly among themselves (e.g. 324m vs 330m for the same structure), that spread itself signals
normal measurement/rounding variance, not grounds to contradict a claim landing near that range.
This applies to ANY numeric claim, not just money: heights, distances, dates, counts, percentages.
e.g. a claim of "$125m in losses" against a source saying "$120m" is supported, not contradicted,
and a claim of "approximately 335 meters" against sources saying 324m-330m is supported, not
contradicted, small measurement variance is not a fabrication.

CALCULATIONS AND GUIDELINE COMPARISONS:
Perform basic arithmetic and standard unit conversions when the accepted evidence supplies the
inputs. A source that states a per-unit amount can directly support a claimed total for multiple
identical units; do not mark the total UNVERIFIABLE merely because the source did not print the
multiplication result. Show the calculation briefly in the explanation. For example, four packages
with 25 g each directly support a 100 g total. Likewise, recalculate a percentage comparison before
choosing a stance: 87 compared with a limit of 30 is 290%, which reasonably supports
"nearly 300%."

When a claim names an organization's recommendation, distinguish its main recommendation from a
stricter conditional or aspirational target. Judge the claim against the benchmark it actually
names; do not silently replace that benchmark with a different recommendation. Before returning,
check that every calculation in the explanation agrees with the selected source stances,
supported/contradicted fields, and verdict.

EVIDENCE ABOUT A DIFFERENT INSTANCE IS NOT THE SAME AS NO EVIDENCE:
If the search results discuss a different specific instance of a similar recurring subject (a
different session, year, edition, or version than the one named in the claim), that is not the
same as finding zero evidence. Do not use it to support or contradict the claim (still
UNVERIFIABLE, leave both false), but say so explicitly in the explanation, e.g. "sources cover the
78th session, not the 80th session named in the claim" rather than a generic "no evidence found."

COMPARATIVE AND SUPERLATIVE CLAIMS (CRITICAL):
If the claim contains comparative or superlative phrases like "nearer than ever before", "best ever", "highest ever", "more than before", "closest ever", "farthest ever", "one of the largest", "unprecedented", it requires DIFFERENT evidence than general claims.

For these claims:
  - Do NOT accept general trend data (e.g., "improving" or "growing") as proof of "closer than ever"
  - Require explicit historical comparison: minimum/maximum values, time-series data, or direct statements comparing current vs past
  - If the sources show ONLY that the subject is improving but provide NO historical minimum/maximum or time-series comparison, leave both supported and contradicted false (UNVERIFIABLE) - the specific superlative hasn't been proven, but general trend data isn't evidence against it either
  - If the sources provide clear historical data showing this IS the closest/best/highest point, set supported=true
  - Only set contradicted=true if the sources show the OPPOSITE: historical data proving some past point was actually closer/better/higher than now

Example: "Indonesia is nearer than ever before to ending poverty" + sources showing only "progress in poverty reduction" = UNVERIFIABLE (progress is not the same as "closest ever," but it's not evidence against it either). Only set supported=true if you find historical poverty rates proving current levels are lowest ever; only set contradicted=true if you find historical rates proving a past level was actually lower.

If the claim is about a CURRENT or ONGOING state (who currently holds office, live negotiations, present-day support), and the sources do not contain recent, direct evidence, leave both supported and contradicted false (UNVERIFIABLE). Do NOT infer a verdict from general or historical information.

ATTRIBUTIONS AND TEMPORAL STATUS:
For "Person said X", first decide whether the evidence establishes that the person made the
statement. Do not claim that X itself is objectively true unless the claim asks for that separate
proposition and the evidence establishes it. For policies, distinguish announcement, signing,
legal imposition, effective date, collection, suspension, expiration, and current status. Treat a
source that proves only a different temporal state as PARTIAL, and name the qualification in the
explanation. Explain what the strongest source actually states and why that evidence produces the
verdict; do not use a bare phrase such as "multiple sources confirm the claim."

YOUR ENTIRE RESPONSE MUST BE A VALID JSON ARRAY STARTING WITH [ AND ENDING WITH ]. NOTHING ELSE."""


EXTRACT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "extracted_claims",
        "strict": True,
        "schema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "query": {"type": "string"},
                    "speaker": {"type": "string"},
                },
                "required": ["claim", "query", "speaker"],
                "additionalProperties": False,
            },
        },
    },
}

VERIFY_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "claim_verification",
        "strict": True,
        "schema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "claim": {"type": "string"},
                    "supported": {"type": "boolean"},
                    "contradicted": {"type": "boolean"},
                    "verdict": {
                        "type": "string",
                        "enum": ["TRUE", "FALSE", "UNVERIFIABLE"],
                    },
                    "explanation": {"type": "string"},
                    "source_analysis": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_index": {"type": "integer", "minimum": 0},
                                "stance": {
                                    "type": "string",
                                    "enum": [
                                        "SUPPORTS", "CONTRADICTS", "PARTIAL",
                                        "IRRELEVANT", "INSUFFICIENT",
                                    ],
                                },
                                "directness": {
                                    "type": "string",
                                    "enum": ["DIRECT", "INDIRECT"],
                                },
                                "reason": {"type": "string"},
                                "evidence_excerpt": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                },
                            },
                            "required": [
                                "source_index", "stance", "directness", "reason",
                                "evidence_excerpt",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "speaker", "claim", "supported", "contradicted", "verdict",
                    "explanation", "source_analysis",
                ],
                "additionalProperties": False,
            },
        },
    },
}
