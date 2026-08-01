from __future__ import annotations

import copy
import math
import os
import re
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable


GEMINI_HEADER = "X-Gemini-Key"
MIN_PROVIDER_KEY_LENGTH = 8
MAX_PROVIDER_KEY_LENGTH = 512

# Approximate public list prices requested for the BYOK usage display. The
# user's free allowance and billing arrangement can make the actual charge $0.
INPUT_USD_PER_MILLION_TOKENS = 0.30
OUTPUT_USD_PER_MILLION_TOKENS = 2.50
GOOGLE_SEARCH_USD_PER_QUERY = 0.014
PRICING_MODEL = "gemini-3.5-flash-lite"
PRICING_DATE = "2026-07"
PRICING_URL = "https://ai.google.dev/gemini-api/docs/pricing"
ESTIMATE_LABEL = "Estimated list-price equivalent (before free quota)"
MAX_PUBLIC_RETRY_AFTER_SECONDS = 300
QUOTA_CATEGORIES = frozenset({"RPM", "TPM", "daily", "spend", "unknown"})
DEFAULT_RETRY_AFTER_SECONDS = {
    "RPM": 60,
    "TPM": 60,
    "unknown": 60,
    "daily": 300,
    "spend": 300,
}


class InvalidProviderCredentials(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RateLimitInfo:
    category: str = "unknown"
    retry_after_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"category": self.category}
        if self.retry_after_seconds is not None:
            value["retry_after_seconds"] = self.retry_after_seconds
        return value


class ProviderRateLimitCircuitOpen(RuntimeError):
    """Safe internal signal that this job must not make another provider call."""

    status_code = 429

    def __init__(self, info: RateLimitInfo):
        self.quota_category = info.category
        self.retry_after_seconds = info.retry_after_seconds
        super().__init__("Provider rate-limit circuit is open.")


def _detail_nodes(value: Any, *, depth: int = 0):
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _detail_nodes(nested, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value[:100]:
            yield from _detail_nodes(nested, depth=depth + 1)


def _bounded_retry_seconds(value: Any) -> int | None:
    seconds: float | None = None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s?\s*", value)
        if match:
            seconds = float(match.group(1))
    elif isinstance(value, dict):
        raw_seconds = value.get("seconds", 0)
        raw_nanos = value.get("nanos", 0)
        try:
            seconds = float(raw_seconds) + float(raw_nanos) / 1_000_000_000
        except (TypeError, ValueError):
            return None
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return min(MAX_PUBLIC_RETRY_AFTER_SECONDS, max(1, math.ceil(seconds)))


def _retry_after_header(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    seconds = _bounded_retry_seconds(value)
    if seconds is not None:
        return seconds
    if not isinstance(value, str):
        return None
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return _bounded_retry_seconds(delay)


def _retry_after_details(exc: Exception) -> int | None:
    for node in _detail_nodes(getattr(exc, "details", None)):
        type_name = str(node.get("@type", node.get("type", "")))
        if not type_name.endswith("google.rpc.RetryInfo"):
            continue
        delay = _bounded_retry_seconds(
            node.get("retryDelay", node.get("retry_delay"))
        )
        if delay is not None:
            return delay
    return None


def _structured_quota_category(exc: Exception) -> str:
    """Classify only explicit google.rpc.QuotaFailure metadata."""
    candidates: list[str] = []
    for node in _detail_nodes(getattr(exc, "details", None)):
        type_name = str(node.get("@type", node.get("type", "")))
        if not type_name.endswith("google.rpc.QuotaFailure"):
            continue
        for violation in node.get("violations", []) or []:
            if not isinstance(violation, dict):
                continue
            candidates.extend(
                str(violation.get(field, "")).lower()
                for field in ("quotaMetric", "quota_metric", "quotaId", "quota_id")
            )
    joined = " ".join(candidates)
    if not joined.strip():
        return "unknown"
    if "spend" in joined or "cost" in joined:
        return "spend"
    if (
        "per_day" in joined
        or "perday" in joined
        or "daily" in joined
        or "requests_per_day" in joined
    ):
        return "daily"
    if (
        "token" in joined
        and ("per_minute" in joined or "perminute" in joined or "tpm" in joined)
    ):
        return "TPM"
    if (
        "request" in joined
        and ("per_minute" in joined or "perminute" in joined or "rpm" in joined)
    ):
        return "RPM"
    return "unknown"


def public_rate_limit_info(exc: Exception) -> RateLimitInfo:
    category = getattr(exc, "quota_category", None)
    if category not in QUOTA_CATEGORIES:
        category = _structured_quota_category(exc)
    retry_after = getattr(exc, "retry_after_seconds", None)
    retry_after = (
        _bounded_retry_seconds(retry_after)
        or _retry_after_header(exc)
        or _retry_after_details(exc)
    )
    if retry_after is None:
        retry_after = DEFAULT_RETRY_AFTER_SECONDS[category]
    return RateLimitInfo(category=category, retry_after_seconds=retry_after)


class ProviderConcurrencyGate:
    """Process-wide Gemini request limit shared by every hosted job."""

    def __init__(self, *, gemini_limit: int):
        if gemini_limit < 1:
            raise ValueError("Provider concurrency limits must be positive.")
        self._gemini = threading.BoundedSemaphore(gemini_limit)

    @contextmanager
    def gemini_slot(self, timeout: float | None = None):
        acquired = self._gemini.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError("Provider concurrency limit wait timed out.")
        try:
            yield
        finally:
            self._gemini.release()


_DEFAULT_PROVIDER_GATE = ProviderConcurrencyGate(gemini_limit=3)


def _validate_key(value: str | None, provider: str) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not (MIN_PROVIDER_KEY_LENGTH <= len(key) <= MAX_PROVIDER_KEY_LENGTH):
        raise InvalidProviderCredentials(f"Enter a valid {provider} API key.")
    if any(character.isspace() or ord(character) < 32 for character in key):
        raise InvalidProviderCredentials(f"Enter a valid {provider} API key.")
    return key


@dataclass(frozen=True, slots=True)
class ProviderCredentials:
    gemini_api_key: str = field(repr=False)

    @classmethod
    def create(cls, gemini_api_key: str | None) -> "ProviderCredentials":
        return cls(gemini_api_key=_validate_key(gemini_api_key, "Gemini"))

    @classmethod
    def from_environment(cls) -> "ProviderCredentials":
        return cls.create(os.getenv("GEMINI_API_KEY"))


def _usage_value(usage: Any, *names: str) -> int | None:
    if usage is None:
        return None
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, bool) or value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _usage_snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    events = copy.deepcopy(events)
    successful = [event for event in events if event.get("succeeded")]
    token_complete = all(event.get("reported", False) for event in events)
    query_complete = all(
        event.get("search_query_count_reported", False) for event in events
    )
    usage_complete = token_complete and query_complete
    input_tokens = sum(event.get("input_tokens") or 0 for event in events)
    output_tokens = sum(event.get("output_tokens") or 0 for event in events)
    total_tokens = sum(event.get("total_tokens") or 0 for event in events)
    query_count = sum(event.get("search_query_count") or 0 for event in events)
    token_cost = (
        input_tokens * INPUT_USD_PER_MILLION_TOKENS
        + output_tokens * OUTPUT_USD_PER_MILLION_TOKENS
    ) / 1_000_000
    estimated_cost = round(token_cost + query_count * GOOGLE_SEARCH_USD_PER_QUERY, 8)
    cost_type = (
        "calculated_list_price_equivalent"
        if usage_complete else "partial_list_price_estimate"
    )
    return {
        "gemini": {
            "requests": len(events),
            "successful_requests": len(successful),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "request_accounting_complete": True,
            "token_accounting_complete": token_complete,
            # Backward compatibility: this historically meant all priced usage fields.
            "complete": usage_complete,
            "events": events,
        },
        "google_search": {
            "query_count": query_count,
            "query_accounting_complete": query_complete,
        },
        "estimated_cost_usd": estimated_cost,
        "cost": {
            "amount_usd": estimated_cost,
            "type": cost_type,
            "usage_complete": usage_complete,
            "includes_free_quota": False,
            "is_actual_bill": False,
            "pricing_date": PRICING_DATE,
        },
        "pricing": {
            "label": ESTIMATE_LABEL,
            "model": PRICING_MODEL,
            "pricing_date": PRICING_DATE,
            "pricing_url": PRICING_URL,
            "input_usd_per_million_tokens": INPUT_USD_PER_MILLION_TOKENS,
            "output_usd_per_million_tokens": OUTPUT_USD_PER_MILLION_TOKENS,
            "google_search_usd_per_query": GOOGLE_SEARCH_USD_PER_QUERY,
        },
        "usage_accounting_complete": usage_complete,
        "cost_estimate_complete": usage_complete,
        "complete": usage_complete,
    }


class UsageLedger:
    def __init__(self, on_update: Callable[[dict[str, Any]], None] | None = None):
        self._lock = threading.RLock()
        self._on_update = on_update
        self._gemini_events: list[dict[str, Any]] = []

    def record_gemini(
        self,
        *,
        model: str,
        stage: str,
        claim_index: int | None,
        attempt: int,
        usage: Any = None,
        succeeded: bool,
        search_query_count: int | None = 0,
    ) -> None:
        prompt_tokens = _usage_value(usage, "prompt_token_count", "prompt_tokens")
        tool_use_prompt_tokens = _usage_value(usage, "tool_use_prompt_token_count")
        candidate_tokens = _usage_value(
            usage, "candidates_token_count", "completion_tokens"
        )
        thoughts_tokens = _usage_value(usage, "thoughts_token_count")
        provider_total_tokens = _usage_value(
            usage, "total_token_count", "total_tokens"
        )
        # Google prices tool-use prompt tokens as input and thinking tokens as
        # output. Missing optional counters mean no such tokens were reported.
        input_tokens = (
            prompt_tokens + (tool_use_prompt_tokens or 0)
            if prompt_tokens is not None else None
        )
        output_tokens = (
            candidate_tokens + (thoughts_tokens or 0)
            if candidate_tokens is not None else None
        )
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None else None
        )
        query_count_reported = search_query_count is not None
        event = {
            "model": model,
            "stage": stage,
            "claim_index": claim_index,
            "attempt": attempt,
            "succeeded": bool(succeeded),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "provider_total_tokens": provider_total_tokens,
            "prompt_tokens": prompt_tokens,
            "tool_use_prompt_tokens": tool_use_prompt_tokens or 0,
            "candidate_tokens": candidate_tokens,
            "thoughts_tokens": thoughts_tokens or 0,
            "search_query_count": (
                max(0, int(search_query_count)) if query_count_reported else None
            ),
            "search_query_count_reported": query_count_reported,
            "reported": input_tokens is not None and output_tokens is not None,
        }
        with self._lock:
            self._gemini_events.append(event)
            snapshot = self._snapshot_locked()
        self._notify(snapshot)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        return _usage_snapshot(self._gemini_events)

    def _notify(self, snapshot: dict[str, Any]) -> None:
        if self._on_update is None:
            return
        try:
            self._on_update(copy.deepcopy(snapshot))
        except Exception:
            return

    def close(self) -> None:
        with self._lock:
            self._on_update = None


class ProviderContext:
    def __init__(
        self,
        credentials: ProviderCredentials | None,
        *,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        concurrency_gate: ProviderConcurrencyGate | None = None,
    ):
        self._credentials = credentials
        self.usage = UsageLedger(on_usage)
        self._concurrency_gate = concurrency_gate
        self._lock = threading.RLock()
        self._gemini_client = None
        self._rate_limit_info: RateLimitInfo | None = None

    @classmethod
    def from_environment(cls) -> "ProviderContext":
        return cls(None, concurrency_gate=_DEFAULT_PROVIDER_GATE)

    def _resolved_credentials(self) -> ProviderCredentials:
        with self._lock:
            if self._credentials is None:
                self._credentials = ProviderCredentials.from_environment()
            return self._credentials

    def gemini_client(self, *, timeout: float):
        with self._lock:
            if self._gemini_client is None:
                from google import genai
                from google.genai import types

                credentials = self._resolved_credentials()
                self._gemini_client = genai.Client(
                    api_key=credentials.gemini_api_key,
                    http_options=types.HttpOptions(
                        timeout=int(timeout * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                )
            return self._gemini_client

    def gemini_slot(self, timeout: float | None = None):
        if self._concurrency_gate is None:
            return nullcontext()
        return self._concurrency_gate.gemini_slot(timeout)

    def trip_rate_limit(self, exc: Exception) -> RateLimitInfo:
        info = public_rate_limit_info(exc)
        with self._lock:
            if self._rate_limit_info is None:
                self._rate_limit_info = info
            return self._rate_limit_info

    def raise_if_rate_limited(self) -> None:
        with self._lock:
            info = self._rate_limit_info
        if info is not None:
            raise ProviderRateLimitCircuitOpen(info)

    @property
    def rate_limit_info(self) -> RateLimitInfo | None:
        with self._lock:
            return self._rate_limit_info

    @property
    def concurrency_limited(self) -> bool:
        return self._concurrency_gate is not None

    def close(self) -> None:
        with self._lock:
            client = self._gemini_client
            self._gemini_client = None
            self._credentials = None
            self.usage.close()
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def merge_usage(
    first: dict[str, Any] | None, second: dict[str, Any] | None
) -> dict[str, Any]:
    first = first if isinstance(first, dict) else {}
    second = second if isinstance(second, dict) else {}
    events = [
        *copy.deepcopy(first.get("gemini", {}).get("events", [])),
        *copy.deepcopy(second.get("gemini", {}).get("events", [])),
    ]
    return _usage_snapshot(events)
