import copy
import difflib
import logging
import re

from providers import ProviderContext

from .models import NoEvidenceError, ProviderProtocolError
from .prompts import VERIFY_PROMPT, VERIFY_RESPONSE_FORMAT
from .sources import _source_domain, _source_quality

logger = logging.getLogger(__name__)


_LANGUAGE_MARKERS = {
    "English": frozenset({
        "the", "and", "are", "is", "was", "were", "this", "that", "these",
        "those", "to", "from", "for", "with", "without", "into", "because",
        "when", "which", "can", "cannot", "could", "would", "should", "will",
        "has", "have", "had", "you", "your", "their", "our", "not", "claim",
        "evidence", "source", "supports", "contradicts",
    }),
    "Indonesian": frozenset({
        "yang", "dan", "adalah", "merupakan", "ini", "itu", "tersebut", "dengan",
        "tanpa", "karena", "ketika", "dari", "untuk", "pada", "dalam", "dapat",
        "bisa", "tidak", "bukan", "akan", "telah", "sudah", "memiliki", "klaim",
        "bukti", "sumber", "menyatakan", "namun", "tetapi", "secara", "bahwa",
        "oleh", "hanya", "mendukung", "membantah",
    }),
}

_VALID_STANCES = {"SUPPORTS", "CONTRADICTS", "PARTIAL", "IRRELEVANT", "INSUFFICIENT"}
_VALID_DIRECTNESS = {"DIRECT", "INDIRECT"}

# Confidence ceilings below are deliberately simple, named constants (not a weighted
# formula) - each one is a cap, not a deduction, and callers combine them with min().
_CONFIDENCE_CAP_NO_DIRECT_SOURCE = 75
_CONFIDENCE_CAP_SINGLE_SUPPORT = 85
_CONFIDENCE_CAP_UNRESOLVED_CONTRADICTION = 70
_CONFIDENCE_CAP_SNIPPETS_ONLY = 85

_DUPLICATE_CONTENT_THRESHOLD = 0.85


def _validate_source_analysis(entries, source_count: int) -> list[dict]:
    # Drops malformed entries rather than failing the whole claim or spending a retry -
    # a partially-malformed source_analysis is still useful signal, and retrying costs
    # a real Gemini call for something usually recoverable.
    valid = []
    if not isinstance(entries, list):
        return valid
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("source_index")
        if not isinstance(index, int) or not (0 <= index < source_count):
            continue
        stance = str(entry.get("stance", "")).upper()
        if stance not in _VALID_STANCES:
            continue
        directness = str(entry.get("directness", "")).upper()
        if directness not in _VALID_DIRECTNESS:
            directness = "INDIRECT"  # safe default rather than dropping an otherwise-valid entry
        excerpt = entry.get("evidence_excerpt")
        valid.append({
            "source_index": index,
            "stance": stance,
            "directness": directness,
            "reason": str(entry.get("reason", ""))[:300],
            "evidence_excerpt": excerpt if isinstance(excerpt, str) and excerpt.strip() else None,
        })
    return valid


def _aggregate_stance(source_analysis: list[dict]) -> tuple[bool, bool]:
    # This is what makes source_analysis authoritative rather than decorative: supported/
    # contradicted are derived from the validated per-source stances, not trusted from the
    # model's own top-level fields - the same "recompute, don't trust" pattern already used
    # one level up for the final TRUE/FALSE/UNVERIFIABLE verdict.
    supported = any(entry["stance"] == "SUPPORTS" for entry in source_analysis)
    contradicted = any(entry["stance"] == "CONTRADICTS" for entry in source_analysis)
    return supported, contradicted


def _verify_evidence_excerpt(excerpt: str | None, source_content: str) -> bool:
    # Soft hallucination signal, not proof: a false result means the excerpt doesn't
    # appear verbatim (after whitespace normalization) in what was actually retrieved -
    # models often paraphrase even when asked to quote, so this is a warning input,
    # never grounds to discard a stance outright.
    if not excerpt or not source_content:
        return False
    normalize = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    return normalize(excerpt) in normalize(source_content)


def _one_based_source_references(explanation, source_count: int) -> str:
    """Convert the model's zero-based evidence citations for display."""
    text = explanation if isinstance(explanation, str) else ""

    def replace(match: re.Match) -> str:
        index = int(match.group(1))
        return f"[{index + 1}]" if index < source_count else match.group(0)

    return re.sub(r"\[(\d+)\]", replace, text)


def _detect_supported_language(text, *, minimum_score: int = 3) -> str | None:
    """Identify clear English/Indonesian prose without adding a runtime dependency."""
    if not isinstance(text, str):
        return None
    words = re.findall(r"[^\W\d_]+", text.lower(), flags=re.UNICODE)
    scores = {
        language: sum(word in markers for word in words)
        for language, markers in _LANGUAGE_MARKERS.items()
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (language, score), (_, runner_up) = ranked
    if score < minimum_score or score - runner_up < 2:
        return None
    return language


def _source_reference_list(indices: list[int], language: str) -> str:
    references = [f"[{index}]" for index in indices]
    if len(references) < 2:
        return references[0] if references else ""
    conjunction = " dan " if language == "Indonesian" else " and "
    return ", ".join(references[:-1]) + conjunction + references[-1]


def _language_safe_explanation(verdict: str, source_analysis: list[dict],
                               language: str) -> str:
    """Produce a truthful fallback when model prose uses the wrong language."""
    if verdict == "TRUE":
        indices = [
            entry["source_index"] for entry in source_analysis
            if entry["stance"] == "SUPPORTS"
        ]
        references = _source_reference_list(indices, language)
        if language == "Indonesian":
            location = f" dalam sumber {references}" if references else ""
            return f"Bukti yang diterima{location} secara langsung mendukung klaim ini."
        location = f" in source {references}" if references else ""
        return f"The accepted evidence{location} directly supports this claim."

    if verdict == "FALSE":
        indices = [
            entry["source_index"] for entry in source_analysis
            if entry["stance"] == "CONTRADICTS"
        ]
        references = _source_reference_list(indices, language)
        if language == "Indonesian":
            location = f" dalam sumber {references}" if references else ""
            return f"Bukti yang diterima{location} secara langsung membantah klaim ini."
        location = f" in source {references}" if references else ""
        return f"The accepted evidence{location} directly contradicts this claim."

    partial_indices = [
        entry["source_index"] for entry in source_analysis
        if entry["stance"] == "PARTIAL"
    ]
    references = _source_reference_list(partial_indices, language)
    if language == "Indonesian":
        if references:
            return (f"Sumber {references} hanya membahas sebagian klaim, sehingga keseluruhan "
                    "klaim tidak dapat diverifikasi.")
        return ("Sumber yang tersedia tidak secara langsung membuktikan atau membantah "
                "keseluruhan klaim ini.")
    if references:
        return (f"Source {references} addresses only part of the claim, so the full claim "
                "cannot be verified.")
    return ("The available sources do not directly establish or contradict the full claim, "
            "so it remains unverifiable.")


def _no_evidence_verdict(claim: dict, claim_index: int,
                         document_language: str | None = None,
                         search_queries: list[str] | None = None) -> dict:
    """Represent an evidence miss explicitly instead of leaving a blank claim slot."""
    language = _detect_supported_language(claim.get("claim")) or document_language or "English"
    if language == "Indonesian":
        explanation = ("Tidak ditemukan bukti yang cukup relevan, sehingga klaim ini tidak "
                       "dapat diverifikasi.")
    else:
        explanation = ("No sufficiently relevant evidence was retrieved, so this claim cannot "
                       "be verified.")
    result = {
        "claim": claim["claim"],
        "speaker": claim.get("speaker", "UNKNOWN"),
        "claim_index": claim_index,
        "supported": False,
        "contradicted": False,
        "verdict": "UNVERIFIABLE",
        "confidence": 60,
        "explanation": explanation,
        "evidence_status": "insufficient_after_search",
        "search_attempts": len(search_queries or []),
        "search_queries": list(search_queries or []),
        "source_analysis": [],
        "sources": [],
    }
    for field in ("merged_from", "original_claims"):
        if field in claim:
            result[field] = copy.deepcopy(claim[field])
    return result


def _group_duplicate_sources(sources: list[dict]) -> list[int]:
    # Returns, per source, the index of the first source in its near-duplicate group
    # (itself, if it's first) - groups syndicated/wire-service copies so they don't
    # each count as independent confirmation, without hiding either URL from the
    # user-facing source list. Pure stdlib (difflib), no new dependency.
    group_of = list(range(len(sources)))
    for i in range(len(sources)):
        for j in range(i):
            if group_of[j] != j:
                continue  # only compare against each group's representative
            similarity = difflib.SequenceMatcher(
                None, sources[i].get("content", ""), sources[j].get("content", "")
            ).ratio()
            if similarity >= _DUPLICATE_CONTENT_THRESHOLD:
                group_of[i] = j
                break
    return group_of


def _cap_confidence(confidence: int, *, independent_supports: int, has_contradiction: bool,
                     any_direct: bool, all_snippets: bool) -> int:
    # Ceilings, not deductions: each condition caps the model's own number rather than
    # discarding the result. A single weak signal shouldn't fail a claim outright. Silent
    # by design - no user-facing explanation string is produced; regular users found the
    # warning text more confusing than useful, so only the calibrated number is kept.
    cap = 100
    if not any_direct:
        cap = min(cap, _CONFIDENCE_CAP_NO_DIRECT_SOURCE)
    if independent_supports <= 1:
        cap = min(cap, _CONFIDENCE_CAP_SINGLE_SUPPORT)
    if has_contradiction:
        cap = min(cap, _CONFIDENCE_CAP_UNRESOLVED_CONTRADICTION)
    if all_snippets:
        cap = min(cap, _CONFIDENCE_CAP_SNIPPETS_ONLY)
    return min(int(confidence), cap)


def _calculate_confidence(
    source_analysis: list[dict],
    sources: list[dict],
    *,
    supported: bool,
    contradicted: bool,
) -> tuple[int, dict]:
    """Return a repeatable evidence-strength score and the factors behind it.

    This is deliberately an evidence-quality indicator, not a probability that the
    claim is true. The model's self-reported number is ignored.
    """
    duplicate_group = _group_duplicate_sources(sources)
    decisive_stance = "SUPPORTS" if supported and not contradicted else (
        "CONTRADICTS" if contradicted and not supported else None
    )
    decisive = [
        entry for entry in source_analysis
        if decisive_stance and entry["stance"] == decisive_stance
    ]
    independent_sources = len({
        duplicate_group[entry["source_index"]] for entry in decisive
    })
    any_direct = any(entry["directness"] == "DIRECT" for entry in decisive)
    valid_excerpt = any(entry.get("evidence_excerpt_valid") for entry in decisive)
    all_snippets = bool(sources) and all(not source.get("is_full_content") for source in sources)
    quality_tiers = [
        int(sources[entry["source_index"]].get("quality_tier") or
            _source_quality(_source_domain(sources[entry["source_index"]]))[0])
        for entry in decisive
    ]
    best_quality_tier = min(quality_tiers, default=None)

    if decisive_stance:
        score = 62
        score += {1: 14, 2: 10, 3: 5}.get(best_quality_tier, 0)
        score += 8 if any_direct else 0
        score += 10 if independent_sources >= 3 else (7 if independent_sources >= 2 else 0)
        score += 4 if valid_excerpt else 0
        score -= 5 if all_snippets else 0
        score = _cap_confidence(
            score,
            independent_supports=independent_sources,
            has_contradiction=supported and contradicted,
            any_direct=any_direct,
            all_snippets=all_snippets,
        )
        score = max(60, min(95, score))
    else:
        # Confidence here means confidence that available evidence is insufficient or
        # conflicting, not confidence that the underlying claim is false.
        analyzed_groups = len({
            duplicate_group[entry["source_index"]] for entry in source_analysis
        })
        score = min(75, 60 + (8 if supported and contradicted else 0)
                    + (4 if analyzed_groups >= 2 else 0))

    return score, {
        "meaning": "evidence_strength_not_truth_probability",
        "model_score_used": False,
        "decisive_stance": decisive_stance,
        "independent_decisive_sources": independent_sources,
        "best_quality_tier": best_quality_tier,
        "has_direct_evidence": any_direct,
        "has_validated_excerpt": valid_excerpt,
        "snippets_only": all_snippets,
        "conflicting_decisive_evidence": supported and contradicted,
    }


def verify_one(claim: dict, search_text: str, sources: list[dict],
               deadline: float | None = None,
               document_language: str | None = None,
               provider_context: ProviderContext | None = None,
               claim_index: int | None = None,
               cancel_event=None, *,
               chat_func, parse_provider_array) -> dict | None:
    # One verify call per claim: keeps each verdict paired with its own search results (no positional zip drift) and lets callers reveal results as they land.
    if not search_text:
        # No search evidence at all (search failed or returned zero usable sources). Do not
        # send an empty evidence block to the model - verified live that it will still answer
        # confidently from its own training knowledge and fabricate source URLs that were
        # never actually retrieved. Raise instead of guessing; the caller's existing per-claim
        # error handling drops this claim from the results rather than showing a fake verdict.
        raise NoEvidenceError(f"no search evidence for claim: {claim['claim'][:60]!r}")

    required_language = (
        _detect_supported_language(claim["claim"]) or document_language
    )
    language_instruction = (
        f"REQUIRED OUTPUT LANGUAGE: {required_language}. Write all generated prose in "
        f"{required_language}, even if the sources use another language.\n"
        if required_language else
        "REQUIRED OUTPUT LANGUAGE: exactly match the language of the CLAIM, not the sources.\n"
    )
    context = (f"SPEAKER: {claim.get('speaker', 'UNKNOWN')}\n"
               f"CLAIM: {claim['claim']}\n"
               f"SEARCH RESULTS:\n{search_text}\n\n"
               f"{language_instruction}")
    raw_reply = chat_func(
        VERIFY_PROMPT,
        context,
        max_tokens=1000,
        deadline=deadline,
        response_format=VERIFY_RESPONSE_FORMAT,
        provider_context=provider_context,
        stage="verification",
        claim_index=claim_index,
        cancel_event=cancel_event,
    )
    raw_items, protocol_valid = parse_provider_array(raw_reply)
    if not protocol_valid:
        logger.error(
            "Verification protocol returned no JSON array or object."
        )
        raise ProviderProtocolError("verification response was not a JSON array")

    parsed = []
    for v in raw_items:
        if isinstance(v, dict) and "supported" in v and "contradicted" in v:
            parsed.append(v)

    if not parsed:
        if raw_items:
            logger.error(
                "Verification protocol parsed %d object(s), but none had "
                "supported/contradicted.",
                len(raw_items),
            )
            raise ProviderProtocolError("verification response omitted required fields")
        return None
    verdict = parsed[0]

    # source_analysis is what makes supported/contradicted authoritative rather than
    # decorative: aggregated per-source stances become these two fields, not whatever
    # the model returned at the top level directly - otherwise the model could report
    # supported=true while its own source_analysis says every source CONTRADICTS or is
    # IRRELEVANT, and nothing would catch it.
    source_analysis = _validate_source_analysis(verdict.get("source_analysis"), len(sources))
    if not source_analysis:
        logger.error("Verification response had no valid source_analysis entries.")
        raise ProviderProtocolError("verification response omitted valid source analysis")
    supported, contradicted = _aggregate_stance(source_analysis)

    for entry in source_analysis:
        source = sources[entry["source_index"]]
        entry["evidence_excerpt_valid"] = _verify_evidence_excerpt(
            entry["evidence_excerpt"], source.get("content", "")
        )

    confidence, confidence_factors = _calculate_confidence(
        source_analysis,
        sources,
        supported=supported,
        contradicted=contradicted,
    )

    verdict["supported"] = supported
    verdict["contradicted"] = contradicted
    verdict["source_analysis"] = source_analysis
    # extension/state.js::normalizeSource already falls back title -> name -> domain ->
    # "Source N" for object-shaped sources - including domain here means a source with
    # no grounding-supplied title shows its domain instead of a generic placeholder.
    public_source_fields = (
        "url", "canonical_url", "provider_url", "title", "domain", "source_type",
        "quality_tier", "canonical_resolution", "displayable", "search_query",
        "search_attempt", "evidence_kind", "is_full_content",
    )
    verdict["sources"] = []
    for source in sources:
        public_source = {
            field: source.get(field)
            for field in public_source_fields
            if source.get(field) is not None
        }
        if source.get("canonical_resolution") == "failed":
            public_source.pop("provider_url", None)
            public_source["url"] = None
            public_source["displayable"] = False
        verdict["sources"].append(public_source)
    verdict["confidence"] = confidence
    verdict["confidence_factors"] = confidence_factors
    # No user-facing "warning" text - removed deliberately, see _cap_confidence's
    # comment. Pop defensively in case the model ever spontaneously includes one
    # (VERIFY_PROMPT never asks for it, so this should be a no-op in practice).
    verdict.pop("warning", None)

    # Deterministic verdict: derived from supported/contradicted (themselves now derived
    # from source_analysis, not trusted from the model directly) rather than trusting the
    # model's own "verdict" field - closes the "no evidence found -> FALSE" failure mode
    # at the code level instead of just asking the model not to do it.
    model_verdict = str(verdict.get("verdict", "")).upper()
    if supported and not contradicted:
        verdict["verdict"] = "TRUE"
    elif contradicted and not supported:
        verdict["verdict"] = "FALSE"
    else:
        verdict["verdict"] = "UNVERIFIABLE"
    verdict["evidence_status"] = (
        "sufficient"
        if verdict["verdict"] in {"TRUE", "FALSE"}
        else ("conflicting" if supported and contradicted else "insufficient_or_partial")
    )

    # The model's explanation was written to justify model_verdict, not necessarily the recomputed one. If we overrode it, say so, otherwise the label and explanation can
    # read as contradicting each other (e.g. "[TRUE] Why: no direct evidence found").
    if model_verdict and model_verdict != verdict["verdict"]:
        verdict["explanation"] = (verdict.get("explanation", "").rstrip() +
            f" (Verdict corrected to {verdict['verdict']} from the evidence fields.)")

    explanation = verdict.get("explanation")
    explanation_language = _detect_supported_language(explanation, minimum_score=2)
    if (required_language and
            (not isinstance(explanation, str) or not explanation.strip()
             or (explanation_language and explanation_language != required_language))):
        verdict["explanation"] = _language_safe_explanation(
            verdict["verdict"], source_analysis, required_language
        )

    verdict["explanation"] = _one_based_source_references(
        verdict.get("explanation"), len(sources)
    )

    return verdict
