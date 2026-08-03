import logging
import re

import httpx

from providers import ProviderContext, ProviderRateLimitCircuitOpen
from url_safety import UnsafeUrlError

from .config import (
    GEMINI_TIMEOUT_SECONDS,
    MAX_EMPTY_SEARCH_PASSES,
    MAX_EVIDENCE_PER_SOURCE_CHARS,
    MAX_EVIDENCE_TOTAL_CHARS,
    PROVIDER_MAX_RETRIES,
)
from .models import ProviderBackoffCancelled, WholeJobDeadlineExceeded
from .prompts import MODEL, SEARCH_PROMPT
from .sources import (
    _GROUNDING_REDIRECT_DOMAINS,
    _domain,
    _domain_matches,
    _filter_sources,
    _grounded_sources,
    _grounding_metadata,
    _source_domain,
    _source_quality,
    _unique_search_query_count,
)

logger = logging.getLogger(__name__)

def prepare_source(
    source: dict,
    deadline: float | None = None,
    *,
    bounded_timeout,
    resolve_redirect,
) -> dict:
    """Attach auditable source metadata and resolve trusted provider redirects."""
    prepared = dict(source)
    provider_url = prepared.get("provider_url")
    if provider_url:
        try:
            canonical_url = resolve_redirect(
                provider_url,
                timeout_seconds=bounded_timeout(2.0, deadline),
            )
        except WholeJobDeadlineExceeded:
            raise
        except (UnsafeUrlError, httpx.HTTPError, OSError, TimeoutError) as exc:
            logger.warning("Canonical source URL resolution failed (%s)", type(exc).__name__)
            prepared["canonical_url"] = None
            prepared["canonical_resolution"] = "failed"
            prepared["public_url"] = None
        else:
            canonical_domain = _domain(canonical_url)
            if canonical_domain and not any(
                _domain_matches(canonical_domain, item)
                for item in _GROUNDING_REDIRECT_DOMAINS
            ):
                prepared["canonical_url"] = canonical_url
                prepared["url"] = canonical_url
                prepared["domain"] = canonical_domain
                prepared["publisher_domain"] = canonical_domain
                prepared["canonical_resolution"] = "resolved"
                prepared["public_url"] = canonical_url
            else:
                prepared["canonical_url"] = None
                prepared["canonical_resolution"] = "failed"
                prepared["public_url"] = None
    else:
        prepared["canonical_url"] = prepared.get("url")
        prepared["canonical_resolution"] = "not_required"
        prepared["public_url"] = prepared.get("url")

    quality_tier, source_type = _source_quality(_source_domain(prepared))
    prepared["quality_tier"] = quality_tier
    prepared["source_type"] = source_type
    return prepared

def search(
    query: str,
    deadline: float | None = None,
    provider_context: ProviderContext | None = None,
    claim_index: int | None = None,
    cancel_event=None,
    *,
    bounded_timeout,
    remaining_seconds,
    is_rate_limited,
    is_retryable,
    sleep_before_retry,
    prepare_source_func,
) -> tuple[str, list[dict]]:
    providers = provider_context or ProviderContext.from_environment()
    for attempt in range(PROVIDER_MAX_RETRIES + 1):
        usage_recorded = False
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderBackoffCancelled("Provider request was cancelled.")
            providers.raise_if_rate_limited()
            request_timeout = bounded_timeout(GEMINI_TIMEOUT_SECONDS, deadline)
            slot_timeout = (
                request_timeout
                if providers.concurrency_limited
                else None
            )
            with providers.gemini_slot(slot_timeout):
                if cancel_event is not None and cancel_event.is_set():
                    raise ProviderBackoffCancelled("Provider request was cancelled.")
                providers.raise_if_rate_limited()
                from google.genai import types

                response = providers.gemini_client(
                    timeout=GEMINI_TIMEOUT_SECONDS,
                ).models.generate_content(
                    model=MODEL,
                    contents=SEARCH_PROMPT + query,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())],
                        max_output_tokens=1200,
                        thinking_config=types.ThinkingConfig(
                            thinking_level=types.ThinkingLevel.MINIMAL
                        ),
                        http_options=types.HttpOptions(
                            timeout=max(1, int(request_timeout * 1000)),
                            retry_options=types.HttpRetryOptions(attempts=1),
                        ),
                    ),
                )
            metadata = _grounding_metadata(response)
            providers.usage.record_gemini(
                model=MODEL,
                stage="search",
                claim_index=claim_index,
                attempt=attempt + 1,
                usage=getattr(response, "usage_metadata", None),
                succeeded=True,
                search_query_count=_unique_search_query_count(metadata),
            )
            usage_recorded = True
            remaining_seconds(deadline)
            break
        except Exception as exc:
            if isinstance(exc, (ProviderRateLimitCircuitOpen, ProviderBackoffCancelled)):
                raise
            if (
                is_rate_limited(exc)
            ):
                providers.trip_rate_limit(exc)
                if not usage_recorded:
                    providers.usage.record_gemini(
                        model=MODEL,
                        stage="search",
                        claim_index=claim_index,
                        attempt=attempt + 1,
                        succeeded=False,
                    )
                raise
            if not usage_recorded and not isinstance(
                exc, ProviderRateLimitCircuitOpen
            ):
                providers.usage.record_gemini(
                    model=MODEL,
                    stage="search",
                    claim_index=claim_index,
                    attempt=attempt + 1,
                    succeeded=False,
                )
            logger.warning(
                "Gemini grounded-search attempt %d failed (%s)",
                attempt + 1,
                type(exc).__name__,
            )
            if (isinstance(exc, WholeJobDeadlineExceeded)
                    or attempt >= PROVIDER_MAX_RETRIES
                    or not is_retryable(exc)):
                raise
            if not sleep_before_retry(attempt, deadline, exc, cancel_event):
                if not is_rate_limited(exc):
                    raise WholeJobDeadlineExceeded(
                        "The fact-check exceeded its time limit."
                    ) from exc
                raise
    accepted = [
        prepare_source_func(source, deadline)
        for source in _filter_sources(_grounded_sources(response))
    ]

    # sources[i] and the "[i] ..." block in the evidence text refer to the same
    # source by construction - this index (not the URL) is what the model is
    # asked to cite in source_analysis, so a malformed/truncated URL in the
    # model's output can never misattribute an analysis to the wrong source.
    sources = []
    text_parts = []
    evidence_size = 0
    for r in accepted:
        url = r.get("public_url")
        is_full_content = bool(r.get("is_full_content"))
        body = r.get("content", "")
        if not isinstance(body, str) or not body.strip():
            continue
        index = len(sources)
        source_label = url or (
            f"non-displayable source ({r.get('publisher_domain') or 'unknown publisher'})"
        )
        header = (
            f"[{index}] {source_label}\n"
            "GROUNDED SUMMARY (Gemini-synthesized; not a verbatim publisher excerpt):\n"
        )
        separator_size = 2 if text_parts else 0
        remaining = (MAX_EVIDENCE_TOTAL_CHARS - evidence_size
                     - separator_size - len(header))
        if remaining <= 0:
            break
        excerpt = body[:min(MAX_EVIDENCE_PER_SOURCE_CHARS, remaining)]
        sources.append({
            "url": url,
            "canonical_url": r.get("canonical_url"),
            "provider_url": r.get("provider_url"),
            "title": r.get("title") or None,
            "domain": r.get("domain") or _domain(url),
            "source_type": r.get("source_type", "other"),
            "quality_tier": r.get("quality_tier", 4),
            "canonical_resolution": r.get("canonical_resolution", "not_required"),
            "displayable": bool(url),
            "content": excerpt,
            "is_full_content": is_full_content,
            "evidence_kind": "grounded_summary",
        })
        text_parts.append(header + excerpt)
        evidence_size += separator_size + len(header) + len(excerpt)

    text = "\n\n".join(text_parts)
    return text, sources

def search_claim(
    claim: dict,
    deadline: float | None = None,
    provider_context: ProviderContext | None = None,
    claim_index: int | None = None,
    cancel_event=None,
    *,
    search_func,
) -> tuple[str, list[dict], list[str]]:
    """Search once normally, then use the claim text only when no evidence survives."""
    candidates = []
    for value in (claim.get("query"), claim.get("claim")):
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = re.sub(r"\s+", " ", value).strip()
        if normalized.casefold() not in {item.casefold() for item in candidates}:
            candidates.append(normalized)

    attempted = []
    for query in candidates[:MAX_EMPTY_SEARCH_PASSES]:
        attempted.append(query)
        text, sources = search_func(
            query, deadline, provider_context, claim_index, cancel_event,
        )
        if text:
            annotated_sources = []
            for source in sources:
                if isinstance(source, dict):
                    source = dict(source)
                    source["search_query"] = query
                    source["search_attempt"] = len(attempted)
                annotated_sources.append(source)
            return text, annotated_sources, attempted
        logger.info("No usable grounded evidence for claim %s on search pass %d",
                    claim_index, len(attempted))
    return "", [], attempted
