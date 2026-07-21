import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from types import SimpleNamespace

import fact_checker


def extraction_payload(count):
    return json.dumps([
        {"claim": f"claim-{index}", "query": f"query-{index}", "speaker": f"speaker-{index}"}
        for index in range(count)
    ])


def verdict_for(claim):
    return {
        "claim": "provider supplied wrong claim",
        "speaker": "provider supplied wrong speaker",
        "supported": True,
        "contradicted": False,
        "verdict": "TRUE",
        "confidence": 90,
        "explanation": claim["claim"],
        "sources": ["https://example.com/evidence"],
    }


def test_callback_exceptions_do_not_discard_pipeline_results(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(1))
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", ["https://example.com"])
    )
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    def broken_callback(_value):
        raise RuntimeError("observer failed")

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.",
        on_progress=broken_callback,
        on_result=broken_callback,
        verbose=True,
    )

    assert result.status == "completed"
    assert len(result) == 1


def test_legacy_positional_on_result_and_verbose_arguments_still_work(monkeypatch):
    emitted = []
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(1))
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", ["https://example.com"])
    )
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.", emitted.append, True
    )

    assert result.status == "completed"
    assert emitted == list(result)


def test_extraction_with_no_claims_has_distinct_status(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: "[]")

    result = fact_checker.fact_check("This is a sufficiently long opinion.", verbose=True)

    assert result.status == "no_claims"
    assert result.claim_count == 0


def test_cancellation_stops_unscheduled_verification(monkeypatch):
    event = threading.Event()
    verification_called = False
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(5))

    def cancel_during_search(*_args, **_kwargs):
        event.set()
        return "evidence", ["https://example.com"]

    def verify(*_args, **_kwargs):
        nonlocal verification_called
        verification_called = True

    monkeypatch.setattr(fact_checker, "_search", cancel_during_search)
    monkeypatch.setattr(fact_checker, "_verify_one", verify)

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.", cancel_event=event, verbose=True
    )

    assert result.status == "cancelled"
    assert verification_called is False


def test_search_and_verification_concurrency_are_bounded(monkeypatch):
    lock = threading.Lock()
    active = {"search": 0, "verify": 0}
    peak = {"search": 0, "verify": 0}
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(12))

    def measured(kind, value):
        with lock:
            active[kind] += value
            peak[kind] = max(peak[kind], active[kind])

    def search(*_args, **_kwargs):
        measured("search", 1)
        time.sleep(0.03)
        measured("search", -1)
        return "evidence", ["https://example.com"]

    def verify(claim, *_args, **_kwargs):
        measured("verify", 1)
        time.sleep(0.03)
        measured("verify", -1)
        return verdict_for(claim)

    monkeypatch.setattr(fact_checker, "_search", search)
    monkeypatch.setattr(fact_checker, "_verify_one", verify)

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "completed"
    assert peak["search"] <= fact_checker.SEARCH_WORKERS
    assert peak["verify"] <= fact_checker.VERIFY_WORKERS
    assert peak["search"] > 1
    assert peak["verify"] > 1


def test_results_emit_in_completion_order_with_stable_claim_pairing(monkeypatch):
    search_three_done = threading.Event()
    emitted_before_last_search = []
    emitted = []
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(3))

    def search(query, *_args, **_kwargs):
        index = int(query.rsplit("-", 1)[1])
        time.sleep({0: 0.01, 1: 0.02, 2: 0.25}[index])
        if index == 2:
            search_three_done.set()
        return f"evidence-{index}", [f"https://example.com/{index}"]

    def verify(claim, *_args, **_kwargs):
        index = int(claim["claim"].rsplit("-", 1)[1])
        time.sleep({0: 0.15, 1: 0.01, 2: 0.01}[index])
        return verdict_for(claim)

    def on_result(verdict):
        emitted.append(verdict)
        if verdict["claim_index"] == 1:
            emitted_before_last_search.append(not search_three_done.is_set())

    monkeypatch.setattr(fact_checker, "_search", search)
    monkeypatch.setattr(fact_checker, "_verify_one", verify)

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.", on_result=on_result, verbose=True
    )

    assert [item["claim_index"] for item in emitted] == [1, 0, 2]
    assert emitted_before_last_search == [True]
    assert [item["claim"] for item in result] == ["claim-1", "claim-0", "claim-2"]
    assert [item["speaker"] for item in result] == ["speaker-1", "speaker-0", "speaker-2"]


def test_no_search_evidence_has_distinct_status(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(2))
    monkeypatch.setattr(fact_checker, "_search", lambda *_args, **_kwargs: ("", []))

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "no_evidence"
    assert result.claim_count == 2
    assert result == []


def test_per_future_failure_preserves_partial_result(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(2))

    def search(query, *_args, **_kwargs):
        if query == "query-0":
            raise RuntimeError("search failed")
        return "evidence", ["https://example.com"]

    monkeypatch.setattr(fact_checker, "_search", search)
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "partial"
    assert [item["claim_index"] for item in result] == [1]
    assert result.errors[0]["claim_index"] == 0


def test_provider_timeout_has_distinct_status(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(1))
    monkeypatch.setattr(
        fact_checker,
        "_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("provider slow")),
    )

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "timeout"


def test_claims_are_hard_capped_in_code(monkeypatch):
    searched = []
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(30))

    def search(query, *_args, **_kwargs):
        searched.append(query)
        return "evidence", ["https://example.com"]

    monkeypatch.setattr(fact_checker, "_search", search)
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "completed"
    assert result.claim_count == fact_checker.MAX_CLAIMS
    assert len(searched) == fact_checker.MAX_CLAIMS


def test_result_callback_receives_a_deep_copy(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(1))
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", ["https://example.com"])
    )
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    def mutate(value):
        value["verdict"] = "FALSE"
        value["sources"][0] = "https://attacker.invalid"

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.", on_result=mutate, verbose=True
    )

    assert result[0]["verdict"] == "TRUE"
    assert result[0]["sources"] == ["https://example.com/evidence"]


def test_cancellation_waits_for_running_provider_threads_and_ignores_late_results(monkeypatch):
    event = threading.Event()
    search_started = threading.Event()
    release_search = threading.Event()
    emitted = []
    holder = {}
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(4))

    def search(*_args, **_kwargs):
        search_started.set()
        release_search.wait(timeout=2)
        return "late evidence", ["https://example.com"]

    monkeypatch.setattr(fact_checker, "_search", search)
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    worker = threading.Thread(
        target=lambda: holder.setdefault(
            "result",
            fact_checker.fact_check(
                "A sufficiently long factual sentence.",
                on_result=emitted.append,
                cancel_event=event,
                verbose=True,
            ),
        )
    )
    worker.start()
    assert search_started.wait(timeout=1)
    event.set()
    time.sleep(0.08)
    assert worker.is_alive(), "fact_check returned while a provider thread was still running"
    release_search.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert holder["result"].status == "cancelled"
    assert holder["result"] == []
    assert emitted == []
    assert not [t for t in threading.enumerate() if t.name.startswith("fact-")]


def test_verify_future_backlog_is_bounded(monkeypatch):
    lock = threading.Lock()
    outstanding = {"fact-search": 0, "fact-verify": 0}
    peak = {"fact-search": 0, "fact-verify": 0}

    class TrackingExecutor(RealThreadPoolExecutor):
        def __init__(self, *args, thread_name_prefix="", **kwargs):
            self.prefix = thread_name_prefix
            super().__init__(*args, thread_name_prefix=thread_name_prefix, **kwargs)

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            with lock:
                outstanding[self.prefix] += 1
                peak[self.prefix] = max(peak[self.prefix], outstanding[self.prefix])

            def completed(_future):
                with lock:
                    outstanding[self.prefix] -= 1

            future.add_done_callback(completed)
            return future

    monkeypatch.setattr(fact_checker, "ThreadPoolExecutor", TrackingExecutor)
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(15))
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", ["https://example.com"])
    )

    def verify(claim, *_args, **_kwargs):
        time.sleep(0.02)
        return verdict_for(claim)

    monkeypatch.setattr(fact_checker, "_verify_one", verify)

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "completed"
    assert peak["fact-verify"] <= (
        fact_checker.VERIFY_BACKLOG_MULTIPLIER * fact_checker.VERIFY_WORKERS
    )


def test_tavily_raw_content_is_preferred_and_evidence_is_bounded(monkeypatch):
    captured = {}

    class FakeTavily:
        def search(self, query, **kwargs):
            captured["query"] = query
            captured.update(kwargs)
            return {"results": [
                {
                    "url": "https://one.example/a",
                    "score": 0.9,
                    "raw_content": "R" * 6_000,
                    "content": "SNIPPET_MUST_NOT_APPEAR",
                },
                {
                    "url": "https://two.example/b",
                    "score": 0.8,
                    "raw_content": None,
                    "content": "FALLBACK_SNIPPET",
                },
                {
                    "url": "https://three.example/c",
                    "score": 0.7,
                    "raw_content": "T" * 8_000,
                    "content": "OTHER_SNIPPET_MUST_NOT_APPEAR",
                },
            ]}

    monkeypatch.setattr(fact_checker, "_tavily_", lambda: FakeTavily())

    text, urls = fact_checker._search("bounded evidence")
    blocks = text.split("\n\n")

    assert captured["include_raw_content"] == "markdown"
    assert captured["timeout"] == fact_checker.TAVILY_TIMEOUT_SECONDS
    assert urls == [
        "https://one.example/a", "https://two.example/b", "https://three.example/c"
    ]
    assert "SNIPPET_MUST_NOT_APPEAR" not in text
    assert "OTHER_SNIPPET_MUST_NOT_APPEAR" not in text
    assert "FALLBACK_SNIPPET" in text
    assert len(text) <= fact_checker.MAX_EVIDENCE_TOTAL_CHARS
    assert all(
        len(block.split("\n", 1)[1]) <= fact_checker.MAX_EVIDENCE_PER_SOURCE_CHARS
        for block in blocks
    )


def test_tavily_timeout_retries_are_bounded(monkeypatch):
    attempts = 0

    class FakeTavily:
        def search(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= fact_checker.PROVIDER_MAX_RETRIES:
                raise TimeoutError("secret upstream timeout details")
            return {"results": []}

    monkeypatch.setattr(fact_checker, "_tavily_", lambda: FakeTavily())
    monkeypatch.setattr(fact_checker.time, "sleep", lambda _seconds: None)

    assert fact_checker._search("retry query") == ("", [])
    assert attempts == fact_checker.PROVIDER_MAX_RETRIES + 1


def test_deepinfra_chat_has_an_explicit_request_timeout(monkeypatch):
    captured = {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))]
    )
    completions = SimpleNamespace(
        create=lambda **kwargs: captured.update(kwargs) or response
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(fact_checker, "_deepinfra_", lambda: client)

    assert fact_checker._chat("system", "user", 12) == "[]"
    assert captured["timeout"] == fact_checker.DEEPINFRA_TIMEOUT_SECONDS


def test_all_malformed_verification_outputs_fail(monkeypatch):
    def chat(system, *_args, **_kwargs):
        if system == fact_checker.EXTRACT_PROMPT:
            return extraction_payload(2)
        return "raw malformed provider body"

    monkeypatch.setattr(fact_checker, "_chat", chat)
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", ["https://example.com"])
    )

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "failed"
    assert {error["code"] for error in result.errors} == {"provider_protocol_error"}
    assert "raw malformed provider body" not in str(result.errors)


def test_malformed_verification_with_success_is_partial(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(2))
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", ["https://example.com"])
    )

    def verify(claim, *_args, **_kwargs):
        if claim["claim"] == "claim-0":
            return verdict_for(claim)
        raise fact_checker.ProviderProtocolError("malformed")

    monkeypatch.setattr(fact_checker, "_verify_one", verify)

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "partial"
    assert [item["claim"] for item in result] == ["claim-0"]


def test_malformed_extraction_is_not_no_claims(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: "not JSON")

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "failed"
    assert result.errors[0]["code"] == "provider_protocol_error"


def test_rate_limit_status_and_errors_are_sanitized(monkeypatch):
    class RateLimited(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=429)
            super().__init__("secret provider quota and account details")

    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(1))
    monkeypatch.setattr(
        fact_checker,
        "_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RateLimited()),
    )

    result = fact_checker.fact_check("A sufficiently long factual sentence.", verbose=True)

    assert result.status == "rate_limited"
    assert result.errors == [{
        "stage": "search",
        "code": "provider_rate_limited",
        "message": "A provider rate limit was reached.",
        "claim_index": 0,
    }]
    assert "secret" not in str(result.errors)
