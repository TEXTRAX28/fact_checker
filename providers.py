from __future__ import annotations

import copy
import os
import threading
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


class InvalidProviderCredentials(ValueError):
    pass


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
        events = copy.deepcopy(self._gemini_events)
        successful = [event for event in events if event["succeeded"]]
        complete = all(
            event["reported"] and event["search_query_count_reported"]
            for event in events
        )
        input_tokens = sum(event["input_tokens"] or 0 for event in events)
        output_tokens = sum(event["output_tokens"] or 0 for event in events)
        total_tokens = sum(event["total_tokens"] or 0 for event in events)
        query_count = sum(event["search_query_count"] or 0 for event in events)
        token_cost = (
            input_tokens * INPUT_USD_PER_MILLION_TOKENS
            + output_tokens * OUTPUT_USD_PER_MILLION_TOKENS
        ) / 1_000_000
        search_cost = query_count * GOOGLE_SEARCH_USD_PER_QUERY
        return {
            "gemini": {
                "requests": len(events),
                "successful_requests": len(successful),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "complete": complete,
                "events": events,
            },
            "google_search": {
                "query_count": query_count,
            },
            "estimated_cost_usd": round(token_cost + search_cost, 8),
            "pricing": {
                "label": ESTIMATE_LABEL,
                "model": PRICING_MODEL,
                "pricing_date": PRICING_DATE,
                "pricing_url": PRICING_URL,
                "input_usd_per_million_tokens": INPUT_USD_PER_MILLION_TOKENS,
                "output_usd_per_million_tokens": OUTPUT_USD_PER_MILLION_TOKENS,
                "google_search_usd_per_query": GOOGLE_SEARCH_USD_PER_QUERY,
            },
            "complete": complete,
        }

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

    @classmethod
    def from_environment(cls) -> "ProviderContext":
        return cls(None)

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
    complete = bool(first.get("gemini", {}).get("complete", True)) and bool(
        second.get("gemini", {}).get("complete", True)
    )
    input_tokens = sum(event.get("input_tokens") or 0 for event in events)
    output_tokens = sum(event.get("output_tokens") or 0 for event in events)
    total_tokens = sum(event.get("total_tokens") or 0 for event in events)
    query_count = sum(event.get("search_query_count") or 0 for event in events)
    complete = complete and all(
        event.get("search_query_count_reported", True) for event in events
    )
    token_cost = (
        input_tokens * INPUT_USD_PER_MILLION_TOKENS
        + output_tokens * OUTPUT_USD_PER_MILLION_TOKENS
    ) / 1_000_000
    return {
        "gemini": {
            "requests": len(events),
            "successful_requests": sum(event.get("succeeded", False) for event in events),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "complete": complete,
            "events": events,
        },
        "google_search": {"query_count": query_count},
        "estimated_cost_usd": round(
            token_cost + query_count * GOOGLE_SEARCH_USD_PER_QUERY, 8
        ),
        "pricing": {
            "label": ESTIMATE_LABEL,
            "model": PRICING_MODEL,
            "pricing_date": PRICING_DATE,
            "pricing_url": PRICING_URL,
            "input_usd_per_million_tokens": INPUT_USD_PER_MILLION_TOKENS,
            "output_usd_per_million_tokens": OUTPUT_USD_PER_MILLION_TOKENS,
            "google_search_usd_per_query": GOOGLE_SEARCH_USD_PER_QUERY,
        },
        "complete": complete,
    }
