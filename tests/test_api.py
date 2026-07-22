from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import api
import jobs


class Outcome:
    def __init__(self, status="completed", results=None):
        self.value = {
            "status": status,
            "results": results or [],
            "claim_count": len(results or []),
            "completed_count": len(results or []),
            "errors": [],
            "message": "Fact-check completed.",
            "normalized_url": None,
            "metadata": None,
        }

    def to_dict(self):
        return self.value


def wait_for_status(client: TestClient, job_id: str, status: str, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/v1/checks/{job_id}")
        if response.json()["status"] == status:
            return response.json()
        time.sleep(0.01)
    pytest.fail(f"Job did not reach {status}.")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("FACT_CHECK_API_TOKEN", raising=False)
    monkeypatch.setattr(jobs.service, "check_text", lambda *_args, **_kwargs: Outcome())
    manager = jobs.JobManager(max_workers=1, capacity=8, rate_limit=20)
    application = api.create_app(lambda: manager)
    with TestClient(application) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"type": "other", "text": "x"},
        {"type": "url"},
        {"type": "url", "url": "x" * 2049},
        {"type": "text", "text": "x" * 200_001},
        {"type": "page", "text": "ok", "title": "x" * 501},
        {"type": "text", "text": "secret", "extra": True},
    ],
)
def test_validation_rejects_invalid_variants_without_echoing_content(client, body):
    response = client.post("/v1/checks", json=body)
    assert response.status_code == 422
    assert "secret" not in response.text
    assert "200001" not in response.text


def test_post_is_202_and_nonblocking(monkeypatch):
    release = threading.Event()

    def blocked(*_args, **_kwargs):
        release.wait(2)
        return Outcome()

    monkeypatch.setattr(jobs.service, "check_text", blocked)
    manager = jobs.JobManager(rate_limit=20)
    with TestClient(api.create_app(lambda: manager)) as client:
        started = time.monotonic()
        response = client.post("/v1/checks", json={"type": "text", "text": "input"})
        elapsed = time.monotonic() - started
        assert response.status_code == 202
        assert elapsed < 0.5
        assert response.json()["status"] in {"queued", "running"}
        release.set()


def test_page_url_text_dispatch_and_snapshot_results_progress(client, monkeypatch):
    calls = []

    def check_text(text, **kwargs):
        calls.append((text, kwargs["metadata"]))
        kwargs["on_progress"]({"stage": "search", "state": "started"})
        kwargs["on_result"]({"claim": "one"})
        return Outcome(results=[{"claim": "one"}])

    def check_url(url, **kwargs):
        calls.append((url, None))
        return Outcome()

    monkeypatch.setattr(jobs.service, "check_text", check_text)
    monkeypatch.setattr(jobs.service, "check_url", check_url)

    page = client.post(
        "/v1/checks",
        json={"type": "page", "url": "https://example.com", "title": "T", "text": "body"},
    ).json()
    snapshot = wait_for_status(client, page["id"], "completed")
    assert snapshot["progress"]["stage"] == "search"
    assert snapshot["results"] == [{"claim": "one"}]
    assert calls[0][1] == {
        "source": "page",
        "url": "https://example.com",
        "title": "T",
    }

    url = client.post("/v1/checks", json={"type": "url", "url": "https://example.com"}).json()
    wait_for_status(client, url["id"], "completed")
    text = client.post("/v1/checks", json={"type": "text", "text": "plain"}).json()
    wait_for_status(client, text["id"], "completed")
    assert calls[-2:] == [("https://example.com", None), ("plain", {"source": "text"})]


def test_extracted_claims_and_per_claim_progress_are_visible_without_queries(monkeypatch):
    claims_ready = threading.Event()
    release = threading.Event()

    def checking(*_args, **kwargs):
        kwargs["on_claims"]([
            {"claim": "First detected claim", "query": "private first query", "speaker": "A"},
            {"claim": "Second detected claim", "query": "private second query", "speaker": "B"},
        ])
        kwargs["on_progress"]({
            "stage": "searching", "state": "started", "claim_index": 0, "claim_count": 2,
        })
        claims_ready.set()
        release.wait(2)
        return Outcome()

    monkeypatch.setattr(jobs.service, "check_text", checking)
    manager = jobs.JobManager(max_workers=1, rate_limit=20)
    with TestClient(api.create_app(lambda: manager)) as test_client:
        created = test_client.post(
            "/v1/checks", json={"type": "text", "text": "factual input"}
        ).json()
        try:
            assert claims_ready.wait(1)
            snapshot = test_client.get(f"/v1/checks/{created['id']}").json()
            assert snapshot["claim_manifest"] == [
                {"claim_index": 0, "claim": "First detected claim", "speaker": "A"},
                {"claim_index": 1, "claim": "Second detected claim", "speaker": "B"},
            ]
            assert snapshot["claim_progress"] == {"0": "searching", "1": "waiting"}
            assert "private first query" not in str(snapshot)
            assert "private second query" not in str(snapshot)
        finally:
            release.set()


def test_active_capacity_and_creation_rate_limits(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(
        jobs.service,
        "check_text",
        lambda *_args, **_kwargs: (release.wait(2), Outcome())[1],
    )
    manager = jobs.JobManager(max_workers=1, capacity=2, rate_limit=2)
    manager.submit("text", {"text": "a"}, "client-a")
    with pytest.raises(jobs.ActiveJobError):
        manager.submit("text", {"text": "b"}, "client-a")
    manager.submit("text", {"text": "b"}, "client-b")
    with pytest.raises(jobs.CapacityError):
        manager.submit("text", {"text": "c"}, "client-c")
    release.set()
    manager.shutdown()

    manager = jobs.JobManager(rate_limit=1)
    monkeypatch.setattr(jobs.service, "check_text", lambda *_a, **_kw: Outcome())
    first = manager.submit("text", {"text": "a"}, "client")
    deadline = time.monotonic() + 2
    while manager.get(first["id"])["status"] != "completed" and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(jobs.CreationRateLimitError):
        manager.submit("text", {"text": "b"}, "client")
    manager.shutdown()


def test_delete_is_cancelling_until_service_returns(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def cancellable(*_args, cancel_event, **_kwargs):
        entered.set()
        assert cancel_event.wait(1)
        release.wait(2)
        return Outcome("cancelled")

    monkeypatch.setattr(jobs.service, "check_text", cancellable)
    manager = jobs.JobManager(rate_limit=20)
    with TestClient(api.create_app(lambda: manager)) as client:
        created = client.post("/v1/checks", json={"type": "text", "text": "body"}).json()
        assert entered.wait(1)
        response = client.delete(f"/v1/checks/{created['id']}")
        assert response.status_code == 202
        assert response.json()["status"] == "cancelling"
        assert client.get(f"/v1/checks/{created['id']}").json()["status"] == "cancelling"
        release.set()
        wait_for_status(client, created["id"], "cancelled")


def test_sse_replays_after_last_event_and_ends_at_terminal(client, monkeypatch):
    def emitting(*_args, **kwargs):
        kwargs["on_claims"]([{
            "claim": "result", "query": "private result query", "speaker": "A",
        }])
        kwargs["on_progress"]({"stage": "one"})
        kwargs["on_result"]({"claim": "result"})
        return Outcome(results=[{"claim": "result"}])

    monkeypatch.setattr(jobs.service, "check_text", emitting)
    created = client.post("/v1/checks", json={"type": "text", "text": "body"}).json()
    done = wait_for_status(client, created["id"], "completed")
    response = client.get(
        f"/v1/checks/{created['id']}/events", headers={"Last-Event-ID": "2"}
    )
    assert response.status_code == 200
    assert "event: claims" in response.text
    assert "event: progress" in response.text
    assert "event: result" in response.text
    assert "event: terminal" in response.text
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids)
    assert all(event_id > 2 for event_id in ids)
    assert ids[-1] == done["sequence"]
    assert "private result query" not in response.text


def test_optional_bearer_auth_is_constant_interface(monkeypatch):
    monkeypatch.setenv("API_BEARER_TOKEN", "correct-token")
    manager = jobs.JobManager(rate_limit=20)
    with TestClient(api.create_app(lambda: manager)) as client:
        denied = client.get("/v1/checks/missing")
        assert denied.status_code == 401
        assert denied.json() == {"detail": "Unauthorized."}
        allowed = client.get(
            "/v1/checks/missing", headers={"Authorization": "Bearer correct-token"}
        )
        assert allowed.status_code == 404
        assert client.get("/health").status_code == 200


def test_404s_and_invalid_last_event_id(client):
    assert client.get("/v1/checks/not-found").status_code == 404
    assert client.delete("/v1/checks/not-found").status_code == 404
    created = client.post("/v1/checks", json={"type": "text", "text": "body"}).json()
    response = client.get(
        f"/v1/checks/{created['id']}/events", headers={"Last-Event-ID": "private"}
    )
    assert response.status_code == 400
    assert "private" not in response.text


def test_failed_claim_can_be_retried_without_rerunning_extraction(monkeypatch):
    class PartialOutcome:
        def to_dict(self):
            return {
                "status": "partial",
                "results": [{"claim_index": 0, "claim": "claim zero", "verdict": "TRUE"}],
                "claim_count": 2,
                "completed_count": 1,
                "errors": [{
                    "stage": "verification",
                    "code": "provider_timeout",
                    "message": "A provider request timed out.",
                    "claim_index": 1,
                }],
                "message": "Fact-check completed with partial results.",
                "normalized_url": None,
                "metadata": {"source": "text"},
            }

    captured = {}
    retry_started = threading.Event()
    release_retry = threading.Event()

    def initial_check(*_args, **kwargs):
        kwargs["on_claims"]([
            {"claim": "claim zero", "query": "query zero", "speaker": "A"},
            {"claim": "claim one", "query": "private retry query", "speaker": "A"},
        ])
        kwargs["on_evidence"]({
            "claim_index": 1,
            "search_text": "saved evidence",
            "sources": [{"url": "https://example.com", "content": "saved evidence"}],
        })
        return PartialOutcome()

    def retry_claim(claim, claim_index, **kwargs):
        captured.update({"claim": claim, "claim_index": claim_index, "evidence": kwargs["evidence"]})
        retry_started.set()
        if not release_retry.wait(timeout=2):
            raise TimeoutError("test did not release retry worker")
        return Outcome(results=[{
            "claim_index": claim_index,
            "claim": claim["claim"],
            "verdict": "TRUE",
        }])

    monkeypatch.setattr(jobs.service, "check_text", initial_check)
    monkeypatch.setattr(jobs.service, "retry_claim", retry_claim)
    manager = jobs.JobManager(max_workers=1, rate_limit=20)
    with TestClient(api.create_app(lambda: manager)) as test_client:
        created = test_client.post(
            "/v1/checks", json={"type": "text", "text": "factual input"}
        ).json()
        partial = wait_for_status(test_client, created["id"], "partial")
        assert partial["claim_manifest"][1]["claim"] == "claim one"
        assert "private retry query" not in str(partial)

        started_at = time.monotonic()
        response = test_client.post(f"/v1/checks/{created['id']}/claims/1/retry")
        elapsed = time.monotonic() - started_at
        try:
            assert response.status_code == 202
            assert elapsed < 0.5
            assert retry_started.wait(timeout=1)
            assert response.json()["status"] == "running"
            assert response.json()["progress"]["stage"] == "verifying"
        finally:
            release_retry.set()
        completed = wait_for_status(test_client, created["id"], "completed")

    assert [result["claim_index"] for result in completed["results"]] == [0, 1]
    assert completed["errors"] == []
    assert captured["claim_index"] == 1
    assert captured["claim"]["query"] == "private retry query"
    assert captured["evidence"]["search_text"] == "saved evidence"


def test_retry_rejects_claim_that_already_has_a_result(client):
    created = client.post("/v1/checks", json={"type": "text", "text": "body"}).json()
    wait_for_status(client, created["id"], "completed")
    response = client.post(f"/v1/checks/{created['id']}/claims/0/retry")
    assert response.status_code == 409


def test_development_cors_and_private_network_preflight(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    manager = jobs.JobManager(rate_limit=20)
    origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    with TestClient(api.create_app(lambda: manager)) as client:
        response = client.options(
            "/v1/checks",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers["access-control-allow-private-network"] == "true"
        denied = client.options(
            "/v1/checks",
            headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in denied.headers


def test_production_cors_uses_exact_origins(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CORS_ORIGINS", "https://allowed.example")
    manager = jobs.JobManager(rate_limit=20)
    with TestClient(api.create_app(lambda: manager)) as client:
        allowed = client.get("/health", headers={"Origin": "https://allowed.example"})
        denied = client.get("/health", headers={"Origin": "https://other.example"})
        assert allowed.headers["access-control-allow-origin"] == "https://allowed.example"
        assert "access-control-allow-origin" not in denied.headers


def test_worker_exception_is_redacted(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("private article text and provider key")

    monkeypatch.setattr(jobs.service, "check_text", fail)
    created = client.post("/v1/checks", json={"type": "text", "text": "private body"}).json()
    snapshot = wait_for_status(client, created["id"], "failed")
    serialized = str(snapshot)
    assert "private article" not in serialized
    assert "provider key" not in serialized
    assert snapshot["outcome"]["errors"][0]["code"] == "pipeline_error"


def test_shutdown_signals_running_jobs(monkeypatch):
    observed = threading.Event()

    def running(*_args, cancel_event, **_kwargs):
        cancel_event.wait(2)
        observed.set()
        return Outcome("cancelled")

    monkeypatch.setattr(jobs.service, "check_text", running)
    manager = jobs.JobManager(rate_limit=20)
    manager.submit("text", {"text": "body"}, "client")
    manager.shutdown()
    assert observed.is_set()
