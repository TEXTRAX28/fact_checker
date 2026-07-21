from __future__ import annotations

import copy
import secrets
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import service


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
    status: str = "queued"
    updated_at: float = 0.0
    progress: dict[str, Any] | None = None
    results: list[Any] = field(default_factory=list)
    outcome: dict[str, Any] | None = None
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
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_workers < 1 or capacity < 1 or history_limit < 1:
            raise ValueError("Worker, capacity, and history settings must be positive.")
        if rate_limit < 1 or rate_window_seconds <= 0 or ttl_seconds <= 0:
            raise ValueError("Rate and TTL settings must be positive.")
        self.max_workers = max_workers
        self.capacity = capacity
        self.rate_limit = rate_limit
        self.rate_window_seconds = rate_window_seconds
        self.ttl_seconds = ttl_seconds
        self.history_limit = history_limit
        self._clock = clock
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._creation_times: dict[str, deque[float]] = defaultdict(deque)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="fact-check"
        )
        self._closed = False

    def submit(
        self, kind: str, payload: dict[str, Any], client_id: str
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
            attempts = self._creation_times[client_id]
            cutoff = now - self.rate_window_seconds
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= self.rate_limit:
                raise CreationRateLimitError("The check creation rate was exceeded.")

            job = Job(
                id=secrets.token_urlsafe(24),
                kind=kind,
                client_id=client_id,
                created_at=now,
                history_limit=self.history_limit,
            )
            self._jobs[job.id] = job
            attempts.append(now)
            self._append_event(job, "status", {"status": "queued"})
            try:
                self._executor.submit(self._run, job, copy.deepcopy(payload))
            except Exception:
                del self._jobs[job.id]
                attempts.pop()
                raise
            return self._snapshot(job)

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

    def _run(self, job: Job, payload: dict[str, Any]) -> None:
        with job.condition:
            if not job.cancel_event.is_set():
                job.status = "running"
                job.updated_at = self._clock()
                self._append_event(job, "status", {"status": "running"})

        def on_progress(value: Any) -> None:
            safe_value = copy.deepcopy(value) if isinstance(value, dict) else {"state": "updated"}
            with job.condition:
                job.progress = safe_value
                job.updated_at = self._clock()
                self._append_event(job, "progress", safe_value)

        def on_result(value: Any) -> None:
            safe_value = copy.deepcopy(value)
            with job.condition:
                job.results.append(safe_value)
                job.updated_at = self._clock()
                self._append_event(job, "result", safe_value)

        try:
            if job.kind == "url":
                outcome = service.check_url(
                    payload["url"],
                    on_progress=on_progress,
                    on_result=on_result,
                    cancel_event=job.cancel_event,
                )
            else:
                outcome = service.check_text(
                    payload["text"],
                    metadata=payload.get("metadata"),
                    on_progress=on_progress,
                    on_result=on_result,
                    cancel_event=job.cancel_event,
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
            }

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
            job.outcome = copy.deepcopy(outcome_dict)
            if isinstance(outcome_dict.get("results"), list):
                job.results = copy.deepcopy(outcome_dict["results"])
            job.updated_at = self._clock()
            self._append_event(
                job,
                "terminal",
                {"status": status, "outcome": copy.deepcopy(outcome_dict)},
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

    @staticmethod
    def _snapshot_locked(job: Job) -> dict[str, Any]:
        snapshot = {
            "id": job.id,
            "type": job.kind,
            "status": job.status,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "sequence": job.sequence,
            "progress": copy.deepcopy(job.progress),
            "results": copy.deepcopy(job.results),
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
