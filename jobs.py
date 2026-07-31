from __future__ import annotations

import copy
import hashlib
import math
import secrets
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import service
from providers import (
    ProviderConcurrencyGate,
    ProviderContext,
    ProviderCredentials,
    merge_usage,
)


TERMINAL_STATUSES = {
    "cancelled",
    "completed",
    "failed",
    "invalid_input",
    "no_claims",
    "no_evidence",
    "partial",
    "rate_limited",
    "timeout",
    "unreadable",
}


class CapacityError(RuntimeError):
    pass


class ActiveJobError(RuntimeError):
    pass


class CreationRateLimitError(RuntimeError):
    pass


class ClaimRetryError(RuntimeError):
    pass


class ClaimRetryCooldownError(ClaimRetryError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Wait {retry_after_seconds} seconds before retrying this claim."
        )


RETRYABLE_CLAIM_ERROR_CODES = {
    "provider_error",
    "provider_protocol_error",
    "provider_rate_limited",
    "provider_timeout",
}


@dataclass(frozen=True)
class JobEvent:
    sequence: int
    event: str
    data: Any
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event": self.event,
            "data": copy.deepcopy(self.data),
            "created_at": self.created_at,
        }


@dataclass
class Job:
    id: str
    kind: str
    client_id: str
    created_at: float
    history_limit: int
    access_token_hash: bytes = field(repr=False)
    status: str = "queued"
    updated_at: float = 0.0
    progress: dict[str, Any] | None = None
    results: list[Any] = field(default_factory=list)
    outcome: dict[str, Any] | None = None
    claims: list[dict[str, Any]] = field(default_factory=list)
    claim_progress: dict[int, str] = field(default_factory=dict)
    evidence_by_claim: dict[int, dict[str, Any]] = field(default_factory=dict)
    retry_attempts: dict[int, int] = field(default_factory=dict)
    retrying_claim_index: int | None = None
    retry_not_before: dict[int, float] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    events: deque[JobEvent] = field(init=False)
    condition: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self.updated_at = self.created_at
        self.events = deque(maxlen=self.history_limit)
        self.condition = threading.Condition(self.lock)

    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class JobManager:
    def __init__(
        self,
        *,
        max_workers: int = 1,
        capacity: int = 8,
        rate_limit: int = 5,
        rate_window_seconds: float = 60.0,
        ttl_seconds: float = 3600.0,
        history_limit: int = 256,
        claim_retry_limit: int = 2,
        gemini_concurrency: int = 1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            max_workers < 1
            or capacity < 1
            or history_limit < 1
            or claim_retry_limit < 1
            or gemini_concurrency < 1
        ):
            raise ValueError("Worker, capacity, and history settings must be positive.")
        if rate_limit < 1 or rate_window_seconds <= 0 or ttl_seconds <= 0:
            raise ValueError("Rate and TTL settings must be positive.")
        self.max_workers = max_workers
        self.capacity = capacity
        self.rate_limit = rate_limit
        self.rate_window_seconds = rate_window_seconds
        self.ttl_seconds = ttl_seconds
        self.history_limit = history_limit
        self.claim_retry_limit = claim_retry_limit
        # The pipeline intentionally supports one actual Gemini request at a
        # time. Higher configured values are clamped to prevent in-flight work
        # from succeeding after a sibling opens the 429 circuit.
        self._provider_gate = ProviderConcurrencyGate(
            gemini_limit=2,
        )
        self._clock = clock
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._creation_times: dict[str, deque[float]] = defaultdict(deque)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="fact-check"
        )
        self._closed = False

    def submit(
        self,
        kind: str,
        payload: dict[str, Any],
        client_id: str,
        credentials: ProviderCredentials | None = None,
        *,
        rate_keys: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            if self._closed:
                raise RuntimeError("Job manager is shutting down.")
            self._purge_locked(now)
            if any(
                job.client_id == client_id and not job.terminal()
                for job in self._jobs.values()
            ):
                raise ActiveJobError("A check is already active for this client.")
            outstanding = sum(not job.terminal() for job in self._jobs.values())
            if outstanding >= self.capacity:
                raise CapacityError("The check queue is full.")
            normalized_rate_keys = rate_keys or (client_id,)
            self._check_rate_limits_locked(normalized_rate_keys, now)

            access_token = secrets.token_urlsafe(32)
            job = Job(
                id=secrets.token_urlsafe(24),
                kind=kind,
                client_id=client_id,
                created_at=now,
                history_limit=self.history_limit,
                access_token_hash=self._token_hash(access_token),
            )
            self._jobs[job.id] = job
            self._record_rate_attempt_locked(normalized_rate_keys, now)
            self._append_event(job, "status", {"status": "queued"})
            try:
                self._executor.submit(
                    self._run, job, copy.deepcopy(payload), credentials
                )
            except Exception:
                del self._jobs[job.id]
                self._remove_rate_attempt_locked(normalized_rate_keys, now)
                raise
            return {**self._snapshot(job), "job_token": access_token}

    def authorize(self, job_id: str, access_token: str) -> bool:
        if not isinstance(access_token, str) or not access_token:
            return False
        with self._lock:
            self._purge_locked(self._clock())
            job = self._jobs.get(job_id)
        if job is None:
            return False
        supplied_hash = self._token_hash(access_token)
        return secrets.compare_digest(supplied_hash, job.access_token_hash)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._purge_locked(self._clock())
            job = self._jobs.get(job_id)
        return None if job is None else self._snapshot(job)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._purge_locked(self._clock())
            job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.condition:
            if not job.terminal() and job.status != "cancelling":
                job.cancel_event.set()
                job.status = "cancelling"
                job.updated_at = self._clock()
                self._append_event(job, "status", {"status": "cancelling"})
            return self._snapshot_locked(job)

    def retry_claim(
        self,
        job_id: str,
        claim_index: int,
        client_id: str,
        credentials: ProviderCredentials | None = None,
        *,
        rate_keys: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if self._closed:
                raise RuntimeError("Job manager is shutting down.")
            if any(
                other.client_id == client_id and not other.terminal() and other.id != job_id
                for other in self._jobs.values()
            ):
                raise ActiveJobError("A check is already active for this client.")
            outstanding = sum(not other.terminal() for other in self._jobs.values())
            if outstanding >= self.capacity:
                raise CapacityError("The check queue is full.")

            normalized_rate_keys = rate_keys or (client_id,)
            self._check_rate_limits_locked(normalized_rate_keys, now)

            with job.condition:
                if not job.terminal():
                    raise ActiveJobError("This check is already active.")
                if not isinstance(claim_index, int) or not (0 <= claim_index < len(job.claims)):
                    raise ClaimRetryError("The failed claim is not available for retry.")
                if any(result.get("claim_index") == claim_index for result in job.results
                       if isinstance(result, dict)):
                    raise ClaimRetryError("This claim already has a result.")
                errors = (job.outcome or {}).get("errors", [])
                retry_error = next((
                    error for error in errors
                    if isinstance(error, dict)
                    and error.get("claim_index") == claim_index
                    and error.get("code") in RETRYABLE_CLAIM_ERROR_CODES
                ), None)
                if retry_error is None:
                    raise ClaimRetryError("This claim did not fail with a retryable provider error.")
                if job.retry_attempts.get(claim_index, 0) >= self.claim_retry_limit:
                    raise ClaimRetryError("This claim has reached its retry limit.")
                retry_not_before = job.retry_not_before.get(claim_index, 0.0)
                retry_after = math.ceil(retry_not_before - now)
                if retry_after > 0:
                    raise ClaimRetryCooldownError(retry_after)

                self._record_rate_attempt_locked(normalized_rate_keys, now)
                job.retry_attempts[claim_index] = job.retry_attempts.get(claim_index, 0) + 1
                job.retrying_claim_index = claim_index
                job.cancel_event.clear()
                job.status = "running"
                job.progress = {
                    "stage": ("verifying" if retry_error.get("stage") == "verification"
                              else "searching"),
                    "state": "started",
                    "claim_index": claim_index,
                    "claim_count": len(job.claims),
                    "retry": True,
                }
                job.claim_progress[claim_index] = job.progress["stage"]
                job.updated_at = now
                self._append_event(job, "status", {"status": "running"})
                self._append_event(job, "progress", job.progress)
                try:
                    self._executor.submit(
                        self._run_claim_retry,
                        job,
                        claim_index,
                        retry_error.get("stage") == "verification",
                        credentials,
                    )
                except Exception:
                    job.status = (job.outcome or {}).get("status", "partial")
                    job.retrying_claim_index = None
                    self._remove_rate_attempt_locked(normalized_rate_keys, now)
                    raise
                return self._snapshot_locked(job)

    def wait_for_events(
        self, job_id: str, sequence: int, timeout: float
    ) -> tuple[list[dict[str, Any]], bool] | None:
        with self._lock:
            self._purge_locked(self._clock())
            job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.condition:
            available = [event for event in job.events if event.sequence > sequence]
            if not available and not job.terminal():
                job.condition.wait(timeout=timeout)
                available = [event for event in job.events if event.sequence > sequence]
            return [event.to_dict() for event in available], job.terminal()

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.condition:
                if not job.terminal():
                    job.cancel_event.set()
                    if job.status != "cancelling":
                        job.status = "cancelling"
                        job.updated_at = self._clock()
                        self._append_event(job, "status", {"status": "cancelling"})
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _run(
        self,
        job: Job,
        payload: dict[str, Any],
        credentials: ProviderCredentials | None,
    ) -> None:
        with job.condition:
            if not job.cancel_event.is_set():
                job.status = "running"
                job.updated_at = self._clock()
                self._append_event(job, "status", {"status": "running"})

        def on_progress(value: Any) -> None:
            safe_value = copy.deepcopy(value) if isinstance(value, dict) else {"state": "updated"}
            with job.condition:
                job.progress = safe_value
                claim_index = safe_value.get("claim_index")
                stage = safe_value.get("stage")
                if (isinstance(claim_index, int) and claim_index >= 0
                        and stage in {"searching", "verifying"}):
                    job.claim_progress[claim_index] = stage
                job.updated_at = self._clock()
                self._append_event(job, "progress", safe_value)

        def on_result(value: Any) -> None:
            safe_value = copy.deepcopy(value)
            with job.condition:
                job.results.append(safe_value)
                claim_index = safe_value.get("claim_index") if isinstance(safe_value, dict) else None
                if isinstance(claim_index, int) and claim_index >= 0:
                    job.claim_progress[claim_index] = "complete"
                job.updated_at = self._clock()
                self._append_event(job, "result", safe_value)

        def on_claims(value: Any) -> None:
            if not isinstance(value, list):
                return
            safe_claims = [
                copy.deepcopy(claim) for claim in value
                if isinstance(claim, dict) and claim.get("claim") and claim.get("query")
            ]
            with job.condition:
                job.claims = safe_claims
                job.claim_progress = {index: "waiting" for index in range(len(safe_claims))}
                job.updated_at = self._clock()
                public_claims = [
                    {
                        "claim_index": index,
                        "claim": claim.get("claim", ""),
                        "speaker": claim.get("speaker", "UNKNOWN"),
                    }
                    for index, claim in enumerate(safe_claims)
                ]
                self._append_event(job, "claims", {
                    "claim_count": len(public_claims),
                    "claims": public_claims,
                })

        def on_evidence(value: Any) -> None:
            if not isinstance(value, dict):
                return
            claim_index = value.get("claim_index")
            if not isinstance(claim_index, int) or claim_index < 0:
                return
            with job.condition:
                job.evidence_by_claim[claim_index] = copy.deepcopy(value)
                job.updated_at = self._clock()

        def on_usage(value: Any) -> None:
            if not isinstance(value, dict):
                return
            with job.condition:
                job.usage = copy.deepcopy(value)
                job.updated_at = self._clock()
                self._append_event(job, "usage", job.usage)

        provider_context = ProviderContext(
            credentials,
            on_usage=on_usage,
            concurrency_gate=self._provider_gate,
        )

        try:
            if job.kind == "url":
                outcome = service.check_url(
                    payload["url"],
                    on_progress=on_progress,
                    on_result=on_result,
                    on_claims=on_claims,
                    on_evidence=on_evidence,
                    cancel_event=job.cancel_event,
                    provider_context=provider_context,
                )
            else:
                outcome = service.check_text(
                    payload["text"],
                    metadata=payload.get("metadata"),
                    on_progress=on_progress,
                    on_result=on_result,
                    on_claims=on_claims,
                    on_evidence=on_evidence,
                    cancel_event=job.cancel_event,
                    provider_context=provider_context,
                )
            outcome_dict = outcome.to_dict()
            if not isinstance(outcome_dict, dict):
                raise TypeError("Invalid service outcome.")
        except Exception:
            outcome_dict = {
                "status": "failed",
                "results": [],
                "claim_count": 0,
                "completed_count": 0,
                "errors": [
                    {
                        "stage": "pipeline",
                        "code": "pipeline_error",
                        "message": "The fact-check pipeline failed.",
                    }
                ],
                "message": "Fact-check failed.",
                "normalized_url": None,
                "metadata": None,
                "usage": provider_context.usage.snapshot(),
            }
        finally:
            provider_context.close()

        with job.condition:
            if job.cancel_event.is_set():
                outcome_dict = {
                    **outcome_dict,
                    "status": "cancelled",
                    "message": "Fact-check was cancelled.",
                }
            status = outcome_dict.get("status")
            if status not in TERMINAL_STATUSES:
                status = "failed"
                outcome_dict = {
                    **outcome_dict,
                    "status": status,
                    "message": "Fact-check failed.",
                    "errors": [
                        {
                            "stage": "pipeline",
                            "code": "pipeline_error",
                            "message": "The fact-check pipeline failed.",
                        }
                    ],
                }
            job.status = status
            outcome_usage = outcome_dict.get("usage")
            if not isinstance(outcome_usage, dict) or not outcome_usage:
                outcome_usage = provider_context.usage.snapshot()
                outcome_dict["usage"] = copy.deepcopy(outcome_usage)
            job.outcome = copy.deepcopy(outcome_dict)
            job.usage = copy.deepcopy(outcome_usage)
            if isinstance(outcome_dict.get("results"), list):
                job.results = copy.deepcopy(outcome_dict["results"])
            job.updated_at = self._clock()
            for error in outcome_dict.get("errors", []):
                if not isinstance(error, dict):
                    continue
                claim_index = error.get("claim_index")
                retry_after = error.get("retry_after_seconds")
                if (
                    error.get("code") == "provider_rate_limited"
                    and isinstance(claim_index, int)
                    and isinstance(retry_after, int)
                    and not isinstance(retry_after, bool)
                    and retry_after > 0
                ):
                    job.retry_not_before[claim_index] = (
                        job.updated_at + retry_after
                    )
            failed_verifications = {
                error.get("claim_index") for error in outcome_dict.get("errors", [])
                if isinstance(error, dict) and error.get("stage") == "verification"
                and isinstance(error.get("claim_index"), int)
            }
            for error in outcome_dict.get("errors", []):
                claim_index = error.get("claim_index") if isinstance(error, dict) else None
                if (
                    isinstance(claim_index, int)
                    and claim_index >= 0
                    and claim_index not in {
                        result.get("claim_index") for result in job.results
                        if isinstance(result, dict)
                    }
                ):
                    job.claim_progress[claim_index] = "failed"
            job.evidence_by_claim = {
                index: evidence for index, evidence in job.evidence_by_claim.items()
                if index in failed_verifications
            }
            self._append_event(
                job,
                "terminal",
                {"status": status, "outcome": copy.deepcopy(outcome_dict)},
            )

    def _run_claim_retry(
        self,
        job: Job,
        claim_index: int,
        reuse_evidence: bool,
        credentials: ProviderCredentials | None,
    ) -> None:
        def on_progress(value: Any) -> None:
            safe_value = copy.deepcopy(value) if isinstance(value, dict) else {"state": "updated"}
            safe_value["retry"] = True
            with job.condition:
                job.progress = safe_value
                stage = safe_value.get("stage")
                if stage in {"searching", "verifying"}:
                    job.claim_progress[claim_index] = stage
                job.updated_at = self._clock()
                self._append_event(job, "progress", safe_value)

        def on_evidence(value: Any) -> None:
            if not isinstance(value, dict):
                return
            with job.condition:
                job.evidence_by_claim[claim_index] = copy.deepcopy(value)
                job.updated_at = self._clock()

        retry_usage: dict[str, Any] = {}

        def on_usage(value: Any) -> None:
            nonlocal retry_usage
            if not isinstance(value, dict):
                return
            retry_usage = copy.deepcopy(value)
            with job.condition:
                job.usage = merge_usage(
                    (job.outcome or {}).get("usage"), retry_usage
                )
                job.updated_at = self._clock()
                self._append_event(job, "usage", job.usage)

        provider_context = ProviderContext(
            credentials,
            on_usage=on_usage,
            concurrency_gate=self._provider_gate,
        )

        with job.condition:
            claim = copy.deepcopy(job.claims[claim_index])
            evidence = copy.deepcopy(job.evidence_by_claim.get(claim_index)) if reuse_evidence else None

        try:
            outcome = service.retry_claim(
                claim,
                claim_index,
                evidence=evidence,
                on_progress=on_progress,
                on_evidence=on_evidence,
                cancel_event=job.cancel_event,
                provider_context=provider_context,
            ).to_dict()
        except Exception:
            outcome = {
                "status": "failed",
                "results": [],
                "errors": [{
                    "stage": "verification",
                    "code": "provider_error",
                    "message": "A provider request failed.",
                    "claim_index": claim_index,
                }],
                "usage": provider_context.usage.snapshot(),
            }
        finally:
            provider_context.close()

        with job.condition:
            previous = copy.deepcopy(job.outcome or {})
            previous_errors = [
                error for error in previous.get("errors", [])
                if not (isinstance(error, dict) and error.get("claim_index") == claim_index)
            ]
            retry_errors = [
                {**error, "claim_index": claim_index}
                for error in outcome.get("errors", []) if isinstance(error, dict)
            ]

            if outcome.get("results"):
                verdict = copy.deepcopy(outcome["results"][0])
                verdict["claim_index"] = claim_index
                job.results = [
                    result for result in job.results
                    if not (isinstance(result, dict) and result.get("claim_index") == claim_index)
                ]
                job.results.append(verdict)
                job.results.sort(key=lambda value: value.get("claim_index", 0))
                job.claim_progress[claim_index] = "complete"
                job.retry_not_before.pop(claim_index, None)
                self._append_event(job, "result", verdict)
                errors = previous_errors
            elif outcome.get("status") == "cancelled":
                errors = previous.get("errors", [])
            else:
                errors = previous_errors + (retry_errors or [
                    error for error in previous.get("errors", [])
                    if isinstance(error, dict) and error.get("claim_index") == claim_index
                ])
                rate_limit_error = next((
                    error for error in retry_errors
                    if error.get("code") == "provider_rate_limited"
                    and isinstance(error.get("retry_after_seconds"), int)
                    and error.get("retry_after_seconds") > 0
                ), None)
                if rate_limit_error is not None:
                    job.retry_not_before[claim_index] = (
                        self._clock() + rate_limit_error["retry_after_seconds"]
                    )
                else:
                    job.retry_not_before.pop(claim_index, None)

            claim_count = len(job.claims)
            completed_count = len(job.results)
            if completed_count == claim_count:
                status = "completed"
                message = "Fact-check completed."
            elif completed_count:
                status = "partial"
                message = "Fact-check completed with partial results."
            else:
                status = outcome.get("status", "failed")
                message = outcome.get("message", "Fact-check failed.")
            if status not in TERMINAL_STATUSES:
                status = "failed"

            job.status = status
            job.retrying_claim_index = None
            job.progress = {
                "stage": "complete",
                "state": status,
                "claim_index": claim_index,
                "claim_count": claim_count,
                "completed_count": completed_count,
                "retry": True,
            }
            job.outcome = {
                **previous,
                "status": status,
                "results": copy.deepcopy(job.results),
                "claim_count": claim_count,
                "completed_count": completed_count,
                "errors": copy.deepcopy(errors),
                "message": message,
                "usage": merge_usage(
                    previous.get("usage"), outcome.get("usage")
                ),
            }
            job.usage = copy.deepcopy(job.outcome["usage"])
            job.updated_at = self._clock()
            verification_failed = any(
                error.get("stage") == "verification" for error in retry_errors
            )
            if outcome.get("results") or not verification_failed:
                job.evidence_by_claim.pop(claim_index, None)
            self._append_event(
                job,
                "terminal",
                {"status": status, "outcome": copy.deepcopy(job.outcome)},
            )

    def _append_event(self, job: Job, event: str, data: Any) -> None:
        with job.condition:
            job.sequence += 1
            job.events.append(
                JobEvent(job.sequence, event, copy.deepcopy(data), self._clock())
            )
            job.condition.notify_all()

    def _snapshot(self, job: Job) -> dict[str, Any]:
        with job.lock:
            return self._snapshot_locked(job)

    def _snapshot_locked(self, job: Job) -> dict[str, Any]:
        errors = copy.deepcopy((job.outcome or {}).get("errors", []))
        now = self._clock()
        for error in errors:
            claim_index = error.get("claim_index") if isinstance(error, dict) else None
            if not isinstance(claim_index, int):
                continue
            retry_after = math.ceil(job.retry_not_before.get(claim_index, 0.0) - now)
            if retry_after > 0:
                error["retry_after_seconds"] = retry_after
            else:
                error.pop("retry_after_seconds", None)
        snapshot = {
            "id": job.id,
            "type": job.kind,
            "status": job.status,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "sequence": job.sequence,
            "progress": copy.deepcopy(job.progress),
            "results": copy.deepcopy(job.results),
            "claim_count": ((job.outcome or {}).get("claim_count")
                            if job.outcome is not None else len(job.claims)),
            "errors": errors,
            "claim_manifest": [
                {
                    "claim_index": index,
                    "claim": claim.get("claim", ""),
                    "speaker": claim.get("speaker", "UNKNOWN"),
                }
                for index, claim in enumerate(job.claims)
            ],
            "claim_progress": copy.deepcopy(job.claim_progress),
            "retrying_claim_index": job.retrying_claim_index,
            "retry_attempts": copy.deepcopy(job.retry_attempts),
            "claim_retry_limit": self.claim_retry_limit,
            "usage": copy.deepcopy(job.usage),
        }
        if job.outcome is not None:
            snapshot["outcome"] = copy.deepcopy(job.outcome)
        return snapshot

    def _purge_locked(self, now: float) -> None:
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.terminal() and now - job.updated_at >= self.ttl_seconds
        ]
        for job_id in expired:
            del self._jobs[job_id]
        cutoff = now - self.rate_window_seconds
        empty = []
        for client_id, attempts in self._creation_times.items():
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                empty.append(client_id)
        for client_id in empty:
            del self._creation_times[client_id]

    def _check_rate_limits_locked(
        self, rate_keys: tuple[str, ...], now: float
    ) -> None:
        cutoff = now - self.rate_window_seconds
        for key in dict.fromkeys(rate_keys):
            attempts = self._creation_times[key]
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= self.rate_limit:
                raise CreationRateLimitError("The check creation rate was exceeded.")

    def _record_rate_attempt_locked(
        self, rate_keys: tuple[str, ...], now: float
    ) -> None:
        for key in dict.fromkeys(rate_keys):
            self._creation_times[key].append(now)

    def _remove_rate_attempt_locked(
        self, rate_keys: tuple[str, ...], now: float
    ) -> None:
        for key in dict.fromkeys(rate_keys):
            attempts = self._creation_times.get(key)
            if attempts and attempts[-1] == now:
                attempts.pop()

    @staticmethod
    def _token_hash(access_token: str) -> bytes:
        return hashlib.sha256(access_token.encode("utf-8")).digest()
