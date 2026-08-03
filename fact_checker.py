import copy
import logging
import random
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from providers import (
    MAX_PUBLIC_RETRY_AFTER_SECONDS,
    ProviderContext,
    ProviderRateLimitCircuitOpen,
    RateLimitInfo,
    public_rate_limit_info,
)
from url_safety import UnsafeUrlError, resolve_public_redirect

from factcheck_core import retrieval as _retrieval
from factcheck_core import verification as _verification
from factcheck_core.claims import _deduplicate_claims
from factcheck_core.config import (
    CLAIM_RETRY_DEADLINE_SECONDS,
    GEMINI_TIMEOUT_SECONDS,
    MAX_CLAIMS,
    MAX_EMPTY_SEARCH_PASSES,
    MAX_EVIDENCE_PER_SOURCE_CHARS,
    MAX_EVIDENCE_TOTAL_CHARS,
    MIN_INPUT_NON_WHITESPACE,
    PROVIDER_MAX_RETRIES,
    PROVIDER_RETRY_INITIAL_SECONDS,
    PROVIDER_RETRY_JITTER_SECONDS,
    PROVIDER_RETRY_MAX_SECONDS,
    SEARCH_WORKERS,
    VERIFY_BACKLOG_MULTIPLIER,
    VERIFY_WORKERS,
    WHOLE_JOB_DEADLINE_SECONDS,
)
from factcheck_core.models import (
    FactCheckResult,
    NoEvidenceError,
    ProviderBackoffCancelled,
    ProviderProtocolError,
    WholeJobDeadlineExceeded,
)
from factcheck_core.prompts import (
    EXTRACT_PROMPT,
    EXTRACT_RESPONSE_FORMAT,
    MODEL,
    SEARCH_PROMPT,
    VERIFY_PROMPT,
    VERIFY_RESPONSE_FORMAT,
)
from factcheck_core.protocol import _parse_provider_array
from factcheck_core.sources import (
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
from factcheck_core.verification import (
    _CONFIDENCE_CAP_NO_DIRECT_SOURCE,
    _aggregate_stance,
    _calculate_confidence,
    _detect_supported_language,
    _language_safe_explanation,
    _no_evidence_verdict,
    _one_based_source_references,
    _validate_source_analysis,
    _verify_evidence_excerpt,
)

logger = logging.getLogger(__name__)

def _safe_callback(callback, value, label: str) -> None:
    if callback is None:
        return
    try:
        callback(copy.deepcopy(value))
    except Exception as exc:
        logger.warning("%s callback failed (%s)", label, type(exc).__name__)


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return (
        getattr(response, "status_code", None)
        or getattr(exc, "status_code", None)
        or getattr(exc, "code", None)
    )


def _is_rate_limited(exc: Exception) -> bool:
    return (_status_code(exc) == 429
            or type(exc).__name__ in {
                "RateLimitError", "UsageLimitExceededError"
            })


def _is_retryable(exc: Exception) -> bool:
    status_code = _status_code(exc)
    return (_is_timeout(exc) or _is_rate_limited(exc)
            or status_code in {408, 409}
            or (status_code is not None and status_code >= 500))


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WholeJobDeadlineExceeded("The fact-check exceeded its time limit.")
    return remaining


def _bounded_timeout(limit: float, deadline: float | None) -> float:
    remaining = _remaining_seconds(deadline)
    return limit if remaining is None else min(limit, remaining)


def _retry_delay_seconds(attempt: int, exc: Exception) -> float:
    exponential = min(
        PROVIDER_RETRY_MAX_SECONDS,
        PROVIDER_RETRY_INITIAL_SECONDS * (2 ** max(0, attempt)),
    )
    fallback = exponential + random.uniform(0.0, PROVIDER_RETRY_JITTER_SECONDS)
    provider_delay = (
        public_rate_limit_info(exc).retry_after_seconds
        if _is_rate_limited(exc) else None
    )
    return min(
        MAX_PUBLIC_RETRY_AFTER_SECONDS,
        max(fallback, float(provider_delay or 0)),
    )


def _sleep_before_retry(
    attempt: int, deadline: float | None, exc: Exception, cancel_event=None
) -> bool:
    delay = _retry_delay_seconds(attempt, exc)
    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining <= delay:
        return False
    if cancel_event is not None:
        if cancel_event.wait(delay):
            raise ProviderBackoffCancelled("Provider retry was cancelled.")
    else:
        time.sleep(delay)
    return True


def _public_provider_error(stage: str, exc: Exception,
                           claim_index: int | None = None) -> dict:
    if isinstance(exc, WholeJobDeadlineExceeded):
        code, message = "job_deadline_exceeded", "The fact-check exceeded its time limit."
    elif _is_rate_limited(exc):
        code, message = "provider_rate_limited", "A provider rate limit was reached."
    elif _is_timeout(exc):
        code, message = "provider_timeout", "A provider request timed out."
    elif isinstance(exc, ProviderProtocolError):
        code, message = "provider_protocol_error", "A provider returned malformed output."
    else:
        code, message = "provider_error", "A provider request failed."
    error = {"stage": stage, "code": code, "message": message}
    if claim_index is not None:
        error["claim_index"] = claim_index
    if code == "provider_rate_limited":
        info = public_rate_limit_info(exc)
        error["quota_category"] = info.category
        if info.retry_after_seconds is not None:
            error["retry_after_seconds"] = info.retry_after_seconds
    return error

def _chat(system: str, user: str, max_tokens: int, *, deadline: float | None = None,
          response_format: dict | None = None,
          provider_context: ProviderContext | None = None,
          stage: str = "unknown", claim_index: int | None = None,
          cancel_event=None) -> str:
    providers = provider_context or ProviderContext.from_environment()
    for attempt in range(PROVIDER_MAX_RETRIES + 1):
        usage_recorded = False
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderBackoffCancelled("Provider request was cancelled.")
            providers.raise_if_rate_limited()
            request_timeout = _bounded_timeout(GEMINI_TIMEOUT_SECONDS, deadline)
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

                config = {
                    "system_instruction": system,
                    "max_output_tokens": max_tokens,
                    "thinking_config": types.ThinkingConfig(
                        thinking_level=types.ThinkingLevel.MINIMAL
                    ),
                    "http_options": types.HttpOptions(
                        timeout=max(1, int(request_timeout * 1000)),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                }
                if response_format is not None:
                    config["response_mime_type"] = "application/json"
                    config["response_json_schema"] = response_format["json_schema"]["schema"]
                response = providers.gemini_client(
                    timeout=GEMINI_TIMEOUT_SECONDS,
                ).models.generate_content(
                    model=MODEL,
                    contents=user,
                    config=types.GenerateContentConfig(**config),
                )
            providers.usage.record_gemini(
                model=MODEL,
                stage=stage,
                claim_index=claim_index,
                attempt=attempt + 1,
                usage=getattr(response, "usage_metadata", None),
                succeeded=True,
            )
            usage_recorded = True
            _remaining_seconds(deadline)
            return response.text or ""
        except Exception as exc:
            if isinstance(exc, (ProviderRateLimitCircuitOpen, ProviderBackoffCancelled)):
                raise
            if (
                _is_rate_limited(exc)
            ):
                providers.trip_rate_limit(exc)
                if not usage_recorded:
                    providers.usage.record_gemini(
                        model=MODEL,
                        stage=stage,
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
                    stage=stage,
                    claim_index=claim_index,
                    attempt=attempt + 1,
                    succeeded=False,
                )
            logger.warning(
                "Gemini attempt %d failed (%s)",
                attempt + 1,
                type(exc).__name__,
            )
            if (isinstance(exc, WholeJobDeadlineExceeded)
                    or attempt >= PROVIDER_MAX_RETRIES
                    or not _is_retryable(exc)):
                raise
            if not _sleep_before_retry(attempt, deadline, exc, cancel_event):
                if not _is_rate_limited(exc):
                    raise WholeJobDeadlineExceeded(
                        "The fact-check exceeded its time limit."
                    ) from exc
                raise
    raise AssertionError("Gemini retry loop exhausted without returning or raising.")

def _prepare_source(source: dict, deadline: float | None = None) -> dict:
    return _retrieval.prepare_source(
        source,
        deadline,
        bounded_timeout=_bounded_timeout,
        resolve_redirect=resolve_public_redirect,
    )


def _search(
    query: str,
    deadline: float | None = None,
    provider_context: ProviderContext | None = None,
    claim_index: int | None = None,
    cancel_event=None,
) -> tuple[str, list[dict]]:
    return _retrieval.search(
        query,
        deadline,
        provider_context,
        claim_index,
        cancel_event,
        bounded_timeout=_bounded_timeout,
        remaining_seconds=_remaining_seconds,
        is_rate_limited=_is_rate_limited,
        is_retryable=_is_retryable,
        sleep_before_retry=_sleep_before_retry,
        prepare_source_func=_prepare_source,
    )


def _search_claim(
    claim: dict,
    deadline: float | None = None,
    provider_context: ProviderContext | None = None,
    claim_index: int | None = None,
    cancel_event=None,
) -> tuple[str, list[dict], list[str]]:
    return _retrieval.search_claim(
        claim,
        deadline,
        provider_context,
        claim_index,
        cancel_event,
        search_func=_search,
    )

def _verify_one(claim: dict, search_text: str, sources: list[dict],
                deadline: float | None = None,
                document_language: str | None = None,
                provider_context: ProviderContext | None = None,
                claim_index: int | None = None,
                cancel_event=None) -> dict | None:
    return _verification.verify_one(
        claim,
        search_text,
        sources,
        deadline,
        document_language,
        provider_context,
        claim_index,
        cancel_event,
        chat_func=_chat,
        parse_provider_array=_parse_provider_array,
    )

def fact_check(transcript: str, on_result=None, *, on_progress=None,
               on_claims=None, on_evidence=None, cancel_event=None,
               search_workers: int = SEARCH_WORKERS,
               verify_workers: int = VERIFY_WORKERS,
               deadline: float | None = None,
               provider_context: ProviderContext | None = None) -> FactCheckResult:
    """Extract, search, and verify claims while retaining the legacy list interface."""
    errors: list[dict] = []
    providers = provider_context or ProviderContext.from_environment()
    if deadline is None:
        deadline = time.monotonic() + WHOLE_JOB_DEADLINE_SECONDS
    deadline_reported = False
    extraction_stats = {
        "provider_claim_count": 0,
        "split_count": None,
        "deduplication_merge_count": 0,
        "final_claim_count": 0,
    }

    def progress(stage: str, **details) -> None:
        _safe_callback(on_progress, {"stage": stage, **details}, "Progress")

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def deadline_expired() -> bool:
        return time.monotonic() >= deadline

    def report_deadline() -> None:
        nonlocal deadline_reported
        if deadline_reported:
            return
        deadline_reported = True
        errors.append(_public_provider_error("pipeline", WholeJobDeadlineExceeded()))

    def finish(status: str, values=(), claim_count: int = 0) -> FactCheckResult:
        result = FactCheckResult(
            values,
            status=status,
            claim_count=claim_count,
            errors=errors,
            usage=providers.usage.snapshot(),
            extraction_stats=copy.deepcopy(extraction_stats),
        )
        progress("complete", status=status, claim_count=claim_count,
                 completed_count=len(result))
        return result

    if cancelled():
        return finish("cancelled")
    if deadline_expired():
        report_deadline()
        return finish("timeout")
    if (not isinstance(transcript, str)
            or sum(not char.isspace() for char in transcript) < MIN_INPUT_NON_WHITESPACE):
        return finish("invalid_input")

    document_language = _detect_supported_language(transcript)

    progress("extracting_claims", state="started")
    try:
        raw = _chat(
            EXTRACT_PROMPT,
            transcript,
            max_tokens=4000,
            deadline=deadline,
            response_format=EXTRACT_RESPONSE_FORMAT,
            provider_context=providers,
            stage="extraction",
            cancel_event=cancel_event,
        )
    except Exception as exc:
        if cancelled() or isinstance(exc, ProviderBackoffCancelled):
            return finish("cancelled")
        logger.warning("Claim extraction failed (%s)", type(exc).__name__)
        errors.append(_public_provider_error("extraction", exc))
        if _is_rate_limited(exc):
            return finish("rate_limited")
        return finish("timeout" if _is_timeout(exc) else "failed")

    if cancelled():
        return finish("cancelled")

    raw_claims, protocol_valid = _parse_provider_array(raw)
    if not protocol_valid:
        exc = ProviderProtocolError("extraction response was not a JSON array")
        errors.append(_public_provider_error("extraction", exc))
        return finish("failed")

    valid_claims = [
        claim for claim in raw_claims
        if isinstance(claim, dict) and claim.get("claim") and claim.get("query")
    ]
    extraction_stats["provider_claim_count"] = len(valid_claims)
    deduplicated_claims = _deduplicate_claims(valid_claims)
    extraction_stats["deduplication_merge_count"] = (
        len(valid_claims) - len(deduplicated_claims)
    )
    claims = deduplicated_claims[:MAX_CLAIMS]
    extraction_stats["final_claim_count"] = len(claims)
    if raw_claims and not claims:
        exc = ProviderProtocolError("extraction response omitted required fields")
        errors.append(_public_provider_error("extraction", exc))
        return finish("failed")
    progress("extracting_claims", state="completed", claim_count=len(claims))
    _safe_callback(on_claims, [dict(claim) for claim in claims], "Claims")
    if not claims:
        return finish("no_claims")
    if providers.rate_limit_info is not None:
        circuit_error = ProviderRateLimitCircuitOpen(providers.rate_limit_info)
        errors.extend(
            _public_provider_error("search", circuit_error, claim_index)
            for claim_index in range(len(claims))
        )
        return finish("rate_limited", claim_count=len(claims))
    if deadline_expired():
        report_deadline()
        return finish("timeout", claim_count=len(claims))

    search_limit = max(1, min(int(search_workers), len(claims)))
    verify_limit = max(1, min(int(verify_workers), len(claims)))
    verify_backlog_limit = min(
        len(claims), max(1, VERIFY_BACKLOG_MULTIPLIER * verify_limit)
    )
    search_pool = ThreadPoolExecutor(max_workers=search_limit,
                                     thread_name_prefix="fact-search")
    verify_pool = ThreadPoolExecutor(max_workers=verify_limit,
                                     thread_name_prefix="fact-verify")
    search_futures = {}
    verify_futures = {}
    pending_verifications = []
    next_search = 0
    results: list[dict] = []
    no_evidence_count = 0
    failed_count = 0
    timeout_count = 0
    rate_limited_count = 0
    dropped_count = 0
    was_cancelled = False
    deadline_reached = False
    rate_limit_triggered = False
    retry_stage_by_index: dict[int, str] = {}

    def record_error(stage: str, claim_index: int, exc: Exception) -> None:
        nonlocal failed_count, timeout_count, rate_limited_count
        logger.warning("%s failed (%s)", stage.title(), type(exc).__name__)
        errors.append(_public_provider_error(stage, exc, claim_index))
        if _is_rate_limited(exc):
            rate_limited_count += 1
        elif _is_timeout(exc):
            timeout_count += 1
        else:
            failed_count += 1

    def submit_searches() -> None:
        nonlocal next_search
        while (not cancelled() and not deadline_expired()
               and providers.rate_limit_info is None
               and next_search < len(claims)
               and len(search_futures) < search_limit
               and (len(search_futures) + len(verify_futures)
                    + len(pending_verifications)) < verify_backlog_limit):
            claim_index = next_search
            claim = claims[claim_index]
            progress("searching", state="started", claim_index=claim_index,
                     claim_count=len(claims))
            future = search_pool.submit(
                _search_claim, claim, deadline, providers, claim_index,
                cancel_event,
            )
            search_futures[future] = claim_index
            next_search += 1

    def submit_verifications() -> None:
        while (not cancelled() and not deadline_expired()
               and providers.rate_limit_info is None
               and pending_verifications
               and len(verify_futures) < verify_backlog_limit):
            claim_index, search_text, sources = pending_verifications.pop(0)
            progress("verifying", state="started", claim_index=claim_index,
                     claim_count=len(claims))
            if cancelled():
                return
            future = verify_pool.submit(
                _verify_one, claims[claim_index], search_text, sources,
                deadline, document_language, providers, claim_index,
                cancel_event,
            )
            verify_futures[future] = claim_index

    submit_searches()
    try:
        while (next_search < len(claims) or search_futures
               or verify_futures or pending_verifications):
            if cancelled():
                was_cancelled = True
                break
            if providers.rate_limit_info is not None and not (
                search_futures or verify_futures
            ):
                rate_limit_triggered = True
                break

            submit_verifications()
            submit_searches()

            active_futures = set(search_futures) | set(verify_futures)
            remaining = max(0.0, deadline - time.monotonic())
            done, _ = wait(
                active_futures,
                timeout=min(0.05, remaining),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                if deadline_expired():
                    deadline_reached = True
                    report_deadline()
                    break
                continue

            for future in done:
                if cancelled():
                    was_cancelled = True
                    break
                if future in search_futures:
                    claim_index = search_futures.pop(future)
                    progress("searching", state="completed", claim_index=claim_index,
                             claim_count=len(claims))
                    if cancelled():
                        was_cancelled = True
                        break
                    try:
                        search_text, sources, attempted_queries = future.result()
                    except WholeJobDeadlineExceeded:
                        deadline_reached = True
                        report_deadline()
                        continue
                    except Exception as exc:
                        if cancelled():
                            was_cancelled = True
                            break
                        record_error("search", claim_index, exc)
                        if _is_rate_limited(exc):
                            retry_stage_by_index[claim_index] = "search"
                            rate_limit_triggered = True
                            break
                    else:
                        if cancelled():
                            was_cancelled = True
                            break
                        if not search_text:
                            no_evidence_count += 1
                            verdict = _no_evidence_verdict(
                                claims[claim_index], claim_index, document_language,
                                attempted_queries,
                            )
                            results.append(verdict)
                            if not cancelled():
                                _safe_callback(on_result, verdict, "Result")
                        else:
                            _safe_callback(on_evidence, {
                                "claim_index": claim_index,
                                "search_text": search_text,
                                "sources": sources,
                                "search_queries": attempted_queries,
                            }, "Evidence")
                            pending_verifications.append((claim_index, search_text, sources))
                            submit_verifications()
                    submit_searches()
                    continue

                claim_index = verify_futures.pop(future)
                progress("verifying", state="completed", claim_index=claim_index,
                         claim_count=len(claims))
                if cancelled():
                    was_cancelled = True
                    break
                try:
                    verdict = future.result()
                except NoEvidenceError:
                    if cancelled():
                        was_cancelled = True
                        break
                    no_evidence_count += 1
                    verdict = _no_evidence_verdict(
                        claims[claim_index], claim_index, document_language
                    )
                    results.append(verdict)
                    if not cancelled():
                        _safe_callback(on_result, verdict, "Result")
                    submit_searches()
                    continue
                except WholeJobDeadlineExceeded:
                    deadline_reached = True
                    report_deadline()
                    continue
                except Exception as exc:
                    if cancelled():
                        was_cancelled = True
                        break
                    record_error("verification", claim_index, exc)
                    if _is_rate_limited(exc):
                        retry_stage_by_index[claim_index] = "verification"
                        rate_limit_triggered = True
                        break
                    submit_searches()
                    continue

                if cancelled():
                    was_cancelled = True
                    break
                if verdict is None:
                    dropped_count += 1
                    submit_searches()
                    continue

                # Provider output cannot change which extracted claim this future belongs to.
                verdict["claim"] = claims[claim_index]["claim"]
                verdict["speaker"] = claims[claim_index].get("speaker", "UNKNOWN")
                verdict["claim_index"] = claim_index
                for field in ("merged_from", "original_claims"):
                    if field in claims[claim_index]:
                        verdict[field] = copy.deepcopy(claims[claim_index][field])
                if cancelled():
                    was_cancelled = True
                    break
                results.append(verdict)
                if not cancelled():
                    _safe_callback(on_result, verdict, "Result")
                submit_searches()
            if deadline_reached or rate_limit_triggered:
                break
    finally:
        if rate_limit_triggered:
            for claim_index in search_futures.values():
                retry_stage_by_index.setdefault(claim_index, "search")
            for claim_index in verify_futures.values():
                retry_stage_by_index.setdefault(claim_index, "verification")
            for claim_index, _search_text, _sources in pending_verifications:
                retry_stage_by_index.setdefault(claim_index, "verification")
            for claim_index in range(next_search, len(claims)):
                retry_stage_by_index.setdefault(claim_index, "search")
            for future in list(search_futures) + list(verify_futures):
                future.cancel()
            pending_verifications.clear()
            search_pool.shutdown(wait=True, cancel_futures=True)
            verify_pool.shutdown(wait=True, cancel_futures=True)
        elif was_cancelled or cancelled() or deadline_reached:
            was_cancelled = True
            for future in list(search_futures) + list(verify_futures):
                future.cancel()
            pending_verifications.clear()
            search_pool.shutdown(wait=True, cancel_futures=True)
            verify_pool.shutdown(wait=True, cancel_futures=True)
        else:
            search_pool.shutdown(wait=True, cancel_futures=True)
            verify_pool.shutdown(wait=True, cancel_futures=True)

    if rate_limit_triggered:
        # A request already in flight when the circuit opened may still finish.
        # Consume that paid work after shutdown: grounded search evidence is
        # retained for a verification-only targeted retry, and completed
        # verification verdicts are published instead of being charged twice.
        completed_indices = {
            result.get("claim_index") for result in results
            if isinstance(result, dict) and isinstance(result.get("claim_index"), int)
        }
        for future, claim_index in list(search_futures.items()):
            if claim_index in completed_indices or future.cancelled():
                continue
            try:
                search_text, sources, attempted_queries = future.result()
            except Exception:
                continue
            if search_text:
                _safe_callback(on_evidence, {
                    "claim_index": claim_index,
                    "search_text": search_text,
                    "sources": sources,
                    "search_queries": attempted_queries,
                }, "Evidence")
                retry_stage_by_index[claim_index] = "verification"
            else:
                verdict = _no_evidence_verdict(
                    claims[claim_index], claim_index, document_language,
                    attempted_queries,
                )
                results.append(verdict)
                completed_indices.add(claim_index)
                _safe_callback(on_result, verdict, "Result")
        for future, claim_index in list(verify_futures.items()):
            if claim_index in completed_indices or future.cancelled():
                continue
            try:
                verdict = future.result()
            except Exception:
                continue
            if verdict is None:
                continue
            verdict["claim"] = claims[claim_index]["claim"]
            verdict["speaker"] = claims[claim_index].get("speaker", "UNKNOWN")
            verdict["claim_index"] = claim_index
            for field in ("merged_from", "original_claims"):
                if field in claims[claim_index]:
                    verdict[field] = copy.deepcopy(claims[claim_index][field])
            results.append(verdict)
            completed_indices.add(claim_index)
            _safe_callback(on_result, verdict, "Result")

        existing_rate_limit_indices = {
            error.get("claim_index") for error in errors
            if error.get("code") == "provider_rate_limited"
        }
        circuit_info = providers.rate_limit_info
        circuit_error = ProviderRateLimitCircuitOpen(circuit_info or RateLimitInfo())
        for claim_index in range(len(claims)):
            if (
                claim_index in completed_indices
                or claim_index in existing_rate_limit_indices
            ):
                continue
            errors.append(_public_provider_error(
                retry_stage_by_index.get(claim_index, "search"),
                circuit_error,
                claim_index,
            ))
        return finish(
            "partial" if results else "rate_limited",
            results,
            len(claims),
        )

    if deadline_reached:
        return finish("partial" if results else "timeout", results, len(claims))
    if was_cancelled:
        return finish("cancelled", results, len(claims))
    if len(results) == len(claims) and no_evidence_count == len(claims):
        return finish("no_evidence", results, len(claims))
    if len(results) == len(claims):
        return finish("completed", results, len(claims))
    if results:
        return finish("partial", results, len(claims))
    if rate_limited_count:
        return finish("rate_limited", claim_count=len(claims))
    if timeout_count:
        return finish("timeout", claim_count=len(claims))
    if failed_count:
        return finish("failed", claim_count=len(claims))

    return finish("no_evidence", claim_count=len(claims))


def retry_claim(claim: dict, claim_index: int, *, evidence: dict | None = None,
                on_progress=None, on_evidence=None, cancel_event=None,
                deadline: float | None = None,
                provider_context: ProviderContext | None = None) -> FactCheckResult:
    """Retry one previously extracted claim without running extraction again."""
    providers = provider_context or ProviderContext.from_environment()
    if deadline is None:
        deadline = time.monotonic() + CLAIM_RETRY_DEADLINE_SECONDS

    def finish(status: str, values=(), errors=()) -> FactCheckResult:
        result = FactCheckResult(
            values,
            status=status,
            claim_count=1,
            errors=list(errors),
            usage=providers.usage.snapshot(),
        )
        _safe_callback(on_progress, {
            "stage": "complete",
            "status": status,
            "claim_index": claim_index,
            "completed_count": len(result),
        }, "Progress")
        return result

    def failed(stage: str, exc: Exception) -> FactCheckResult:
        error = _public_provider_error(stage, exc, claim_index)
        if _is_rate_limited(exc):
            status = "rate_limited"
        elif _is_timeout(exc):
            status = "timeout"
        else:
            status = "failed"
        return finish(status, errors=[error])

    if (not isinstance(claim, dict) or not claim.get("claim")
            or not claim.get("query") or not isinstance(claim_index, int)
            or claim_index < 0):
        return failed("verification", ProviderProtocolError("invalid saved claim"))
    if cancel_event is not None and cancel_event.is_set():
        return finish("cancelled")

    search_text = evidence.get("search_text") if isinstance(evidence, dict) else None
    sources = evidence.get("sources") if isinstance(evidence, dict) else None
    if not isinstance(search_text, str) or not search_text.strip() or not isinstance(sources, list):
        _safe_callback(on_progress, {
            "stage": "searching", "state": "started", "claim_index": claim_index,
        }, "Progress")
        try:
            search_text, sources, attempted_queries = _search_claim(
                claim,
                deadline=deadline,
                provider_context=providers,
                claim_index=claim_index,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            if (
                isinstance(exc, ProviderBackoffCancelled)
                or (cancel_event is not None and cancel_event.is_set())
            ):
                return finish("cancelled")
            return failed("search", exc)
        _safe_callback(on_progress, {
            "stage": "searching", "state": "completed", "claim_index": claim_index,
        }, "Progress")
        if not search_text:
            return finish("no_evidence", errors=[{
                "stage": "search",
                "code": "claim_no_evidence",
                "message": "No sufficient evidence was found for this claim.",
                "claim_index": claim_index,
            }])
        _safe_callback(on_evidence, {
            "claim_index": claim_index,
            "search_text": search_text,
            "sources": sources,
            "search_queries": attempted_queries,
        }, "Evidence")

    if cancel_event is not None and cancel_event.is_set():
        return finish("cancelled")

    _safe_callback(on_progress, {
        "stage": "verifying", "state": "started", "claim_index": claim_index,
    }, "Progress")
    try:
        verdict = _verify_one(
            claim,
            search_text,
            sources,
            deadline=deadline,
            provider_context=providers,
            claim_index=claim_index,
            cancel_event=cancel_event,
        )
    except NoEvidenceError:
        return finish("no_evidence", errors=[{
            "stage": "verification",
            "code": "claim_no_evidence",
            "message": "No sufficient evidence was found for this claim.",
            "claim_index": claim_index,
        }])
    except Exception as exc:
        if (
            isinstance(exc, ProviderBackoffCancelled)
            or (cancel_event is not None and cancel_event.is_set())
        ):
            return finish("cancelled")
        return failed("verification", exc)
    if cancel_event is not None and cancel_event.is_set():
        return finish("cancelled")
    if verdict is None:
        return failed(
            "verification", ProviderProtocolError("verification returned no verdict")
        )

    verdict["claim"] = claim["claim"]
    verdict["speaker"] = claim.get("speaker", "UNKNOWN")
    verdict["claim_index"] = claim_index
    for field in ("merged_from", "original_claims"):
        if field in claim:
            verdict[field] = copy.deepcopy(claim[field])
    _safe_callback(on_progress, {
        "stage": "verifying", "state": "completed", "claim_index": claim_index,
    }, "Progress")
    return finish("completed", [verdict])
