from __future__ import annotations

import copy
import os
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable


GEMINI_HEADER = "X-Gemini-Key"
TAVILY_HEADER = "X-Tavily-Key"
MIN_PROVIDER_KEY_LENGTH = 8
MAX_PROVIDER_KEY_LENGTH = 512
TAVILY_ADVANCED_SEARCH_CREDITS = 2


class InvalidProviderCredentials(ValueError):
    pass


class ProviderConcurrencyGate:
    """Process-wide limits shared by every hosted provider context."""

    def __init__(self, *, gemini_limit: int, tavily_limit: int):
        if gemini_limit < 1 or tavily_limit < 1:
            raise ValueError("Provider concurrency limits must be positive.")
        self._gemini = threading.BoundedSemaphore(gemini_limit)
        self._tavily = threading.BoundedSemaphore(tavily_limit)

    @contextmanager
    def gemini_slot(self, timeout: float | None = None):
        with self._slot(self._gemini, timeout):
            yield

    @contextmanager
    def tavily_slot(self, timeout: float | None = None):
        with self._slot(self._tavily, timeout):
            yield

    @staticmethod
    @contextmanager
    def _slot(semaphore: threading.BoundedSemaphore, timeout: float | None):
        acquired = semaphore.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError("Provider concurrency limit wait timed out.")
        try:
            yield
        finally:
            semaphore.release()


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
    tavily_api_key: str = field(repr=False)

    @classmethod
    def create(
        cls, gemini_api_key: str | None, tavily_api_key: str | None
    ) -> "ProviderCredentials":
        return cls(
            gemini_api_key=_validate_key(gemini_api_key, "Gemini"),
            tavily_api_key=_validate_key(tavily_api_key, "Tavily"),
        )

    @classmethod
    def from_environment(cls) -> "ProviderCredentials":
        return cls.create(
            os.getenv("GEMINI_API_KEY"),
            os.getenv("TAVILY_API_KEY"),
        )


def _usage_value(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class UsageLedger:
    def __init__(self, on_update: Callable[[dict[str, Any]], None] | None = None):
        self._lock = threading.RLock()
        self._on_update = on_update
        self._gemini_events: list[dict[str, Any]] = []
        self._tavily_events: list[dict[str, Any]] = []

    def record_gemini(
        self,
        *,
        model: str,
        stage: str,
        claim_index: int | None,
        attempt: int,
        usage: Any = None,
        succeeded: bool,
    ) -> None:
        prompt_tokens = _usage_value(usage, "prompt_tokens")
        completion_tokens = _usage_value(usage, "completion_tokens")
        total_tokens = _usage_value(usage, "total_tokens")
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        event = {
            "model": model,
            "stage": stage,
            "claim_index": claim_index,
            "attempt": attempt,
            "succeeded": bool(succeeded),
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "reported": total_tokens is not None,
        }
        with self._lock:
            self._gemini_events.append(event)
            snapshot = self._snapshot_locked()
        self._notify(snapshot)

    def record_tavily(
        self,
        *,
        stage: str,
        claim_index: int | None,
        attempt: int,
        succeeded: bool,
        search_depth: str,
    ) -> None:
        event = {
            "stage": stage,
            "claim_index": claim_index,
            "attempt": attempt,
            "succeeded": bool(succeeded),
            "search_depth": search_depth,
            "estimated_credits": (
                TAVILY_ADVANCED_SEARCH_CREDITS
                if succeeded and search_depth == "advanced"
                else 1 if succeeded else 0
            ),
        }
        with self._lock:
            self._tavily_events.append(event)
            snapshot = self._snapshot_locked()
        self._notify(snapshot)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        gemini = copy.deepcopy(self._gemini_events)
        tavily = copy.deepcopy(self._tavily_events)
        successful_gemini = [event for event in gemini if event["succeeded"]]
        gemini_complete = all(event["reported"] for event in gemini)
        tavily_complete = all(event["succeeded"] for event in tavily)
        return {
            "gemini": {
                "requests": len(gemini),
                "successful_requests": len(successful_gemini),
                "input_tokens": sum(event["input_tokens"] or 0 for event in gemini),
                "output_tokens": sum(event["output_tokens"] or 0 for event in gemini),
                "total_tokens": sum(event["total_tokens"] or 0 for event in gemini),
                "complete": gemini_complete,
                "events": gemini,
            },
            "tavily": {
                "search_attempts": len(tavily),
                "successful_searches": sum(event["succeeded"] for event in tavily),
                "estimated_credits": sum(event["estimated_credits"] for event in tavily),
                "complete": tavily_complete,
                "events": tavily,
            },
            "complete": gemini_complete and tavily_complete,
        }

    def _notify(self, snapshot: dict[str, Any]) -> None:
        if self._on_update is None:
            return
        try:
            self._on_update(copy.deepcopy(snapshot))
        except Exception:
            # Usage reporting is observational and must never fail a fact-check.
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
        self._tavily_client = None

    @classmethod
    def from_environment(cls) -> "ProviderContext":
        return cls(None)

    def _resolved_credentials(self) -> ProviderCredentials:
        with self._lock:
            if self._credentials is None:
                self._credentials = ProviderCredentials.from_environment()
            return self._credentials

    def gemini_client(self, *, base_url: str, timeout: float):
        with self._lock:
            if self._gemini_client is None:
                from openai import OpenAI

                credentials = self._resolved_credentials()
                self._gemini_client = OpenAI(
                    api_key=credentials.gemini_api_key,
                    base_url=base_url,
                    timeout=timeout,
                    max_retries=0,
                )
            return self._gemini_client

    def tavily_client(self):
        with self._lock:
            if self._tavily_client is None:
                from tavily import TavilyClient

                credentials = self._resolved_credentials()
                self._tavily_client = TavilyClient(
                    api_key=credentials.tavily_api_key
                )
            return self._tavily_client

    def gemini_slot(self, timeout: float | None = None):
        if self._concurrency_gate is None:
            return nullcontext()
        return self._concurrency_gate.gemini_slot(timeout)

    def tavily_slot(self, timeout: float | None = None):
        if self._concurrency_gate is None:
            return nullcontext()
        return self._concurrency_gate.tavily_slot(timeout)

    @property
    def concurrency_limited(self) -> bool:
        return self._concurrency_gate is not None

    def close(self) -> None:
        with self._lock:
            clients = (self._gemini_client, self._tavily_client)
            self._gemini_client = None
            self._tavily_client = None
            self._credentials = None
            self.usage.close()
        for client in clients:
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
    gemini_events = [
        *copy.deepcopy(first.get("gemini", {}).get("events", [])),
        *copy.deepcopy(second.get("gemini", {}).get("events", [])),
    ]
    tavily_events = [
        *copy.deepcopy(first.get("tavily", {}).get("events", [])),
        *copy.deepcopy(second.get("tavily", {}).get("events", [])),
    ]
    gemini_complete = bool(first.get("gemini", {}).get("complete", True)) and bool(
        second.get("gemini", {}).get("complete", True)
    )
    tavily_complete = bool(first.get("tavily", {}).get("complete", True)) and bool(
        second.get("tavily", {}).get("complete", True)
    )
    return {
        "gemini": {
            "requests": len(gemini_events),
            "successful_requests": sum(event.get("succeeded", False) for event in gemini_events),
            "input_tokens": sum(event.get("input_tokens") or 0 for event in gemini_events),
            "output_tokens": sum(event.get("output_tokens") or 0 for event in gemini_events),
            "total_tokens": sum(event.get("total_tokens") or 0 for event in gemini_events),
            "complete": gemini_complete,
            "events": gemini_events,
        },
        "tavily": {
            "search_attempts": len(tavily_events),
            "successful_searches": sum(event.get("succeeded", False) for event in tavily_events),
            "estimated_credits": sum(event.get("estimated_credits") or 0 for event in tavily_events),
            "complete": tavily_complete,
            "events": tavily_events,
        },
        "complete": gemini_complete and tavily_complete,
    }
