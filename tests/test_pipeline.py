import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from types import SimpleNamespace

import fact_checker
from providers import ProviderContext, ProviderCredentials


class FakeChatStream:
    def __init__(self, *parts):
        self.parts = parts
        self.closed = False

    def __iter__(self):
        return iter([
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])
            for part in self.parts
        ])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True


def fake_provider_context(*, gemini=None, tavily=None):
    context = ProviderContext(
        ProviderCredentials.create("gemini-test-key", "tavily-test-key")
    )
    context._gemini_client = gemini
    context._tavily_client = tavily
    return context


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


def one_source(url="https://example.com", content="Evidence text.", **overrides):
    # The real shape _search() returns as of the source_analysis work - source_index
    # references in a mocked verify response must line up with this list's order.
    source = {"url": url, "title": None, "domain": url.split("//")[-1].split("/")[0], "score": 0.9,
              "content": content, "is_full_content": True}
    source.update(overrides)
    return source


def test_provider_json_parser_repairs_invalid_backslash_escapes():
    raw = (
        '[{"claim":"The citation is 607 U.S. \\_\\_\\_ (2025).",'
        '"query":"607 U.S. 2025 citation","speaker":"SPEAKER_A"}]'
    )

    parsed, protocol_valid = fact_checker._parse_provider_array(raw)

    assert protocol_valid is True
    assert parsed == [{
        "claim": "The citation is 607 U.S. ___ (2025).",
        "query": "607 U.S. 2025 citation",
        "speaker": "SPEAKER_A",
    }]


def test_provider_json_parser_repairs_common_protocol_damage():
    cases = [
        ('[{"claim":"a","verdict":TRUE}]', 1),
        ('```json\n[{"claim":"a","verdict":"TRUE"}]\n```', 1),
        ('[{"claim":"a","verdict":"TRUE"},]', 1),
        (
            '[{"claim":"a","verdict":TRUE,"meta":{"weight":2}},'
            '{"claim":"b","verdict":',
            1,
        ),
    ]

    for raw, expected_count in cases:
        parsed, protocol_valid = fact_checker._parse_provider_array(raw)
        assert protocol_valid is True
        assert len(parsed) == expected_count

    nested, _ = fact_checker._parse_provider_array(cases[-1][0])
    assert nested[0]["meta"]["weight"] == 2


def test_source_ranking_uses_hostname_not_url_substrings():
    ranked = fact_checker._filter_sources([
        {"url": "https://spam.example/?ref=reuters.com"},
        {"url": "https://en.wikipedia.org/wiki/Example"},
        {"url": "https://reuters.com/real"},
        {"url": "https://unranked.example/story"},
    ])

    assert [result["url"] for result in ranked] == [
        "https://reuters.com/real",
        "https://en.wikipedia.org/wiki/Example",
        "https://spam.example/?ref=reuters.com",
    ]


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
    )

    assert result.status == "completed"
    assert len(result) == 1


def test_positional_on_result_argument_still_works(monkeypatch):
    emitted = []
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(1))
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", ["https://example.com"])
    )
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.", emitted.append
    )

    assert result.status == "completed"
    assert emitted == list(result)


def test_extraction_with_no_claims_has_distinct_status(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: "[]")

    result = fact_checker.fact_check("This is a sufficiently long opinion.")

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
        "A sufficiently long factual sentence.", cancel_event=event
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

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

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
        "A sufficiently long factual sentence.", on_result=on_result
    )

    assert [item["claim_index"] for item in emitted] == [1, 0, 2]
    assert emitted_before_last_search == [True]
    assert [item["claim"] for item in result] == ["claim-1", "claim-0", "claim-2"]
    assert [item["speaker"] for item in result] == ["speaker-1", "speaker-0", "speaker-2"]


def test_no_search_evidence_has_distinct_status(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(2))
    monkeypatch.setattr(fact_checker, "_search", lambda *_args, **_kwargs: ("", []))
    emitted = []

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.", on_result=emitted.append
    )

    assert result.status == "no_evidence"
    assert result.claim_count == 2
    assert len(result) == 2
    assert result == emitted
    assert {item["claim_index"] for item in result} == {0, 1}
    assert {item["verdict"] for item in result} == {"UNVERIFIABLE"}
    assert all(item["sources"] == [] for item in result)


def test_one_evidence_miss_does_not_leave_a_partial_blank_claim(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(2))

    def search(query, *_args, **_kwargs):
        if query == "query-0":
            return "", []
        return "evidence", [one_source()]

    monkeypatch.setattr(fact_checker, "_search", search)
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

    assert result.status == "completed"
    by_index = {item["claim_index"]: item for item in result}
    assert by_index[0]["verdict"] == "UNVERIFIABLE"
    assert by_index[0]["sources"] == []
    assert by_index[1]["verdict"] == "TRUE"


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

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

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

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

    assert result.status == "timeout"


def test_expired_whole_job_deadline_stops_before_extraction(monkeypatch):
    monkeypatch.setattr(
        fact_checker,
        "_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider was called")),
    )

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.",
        deadline=time.monotonic() - 1,
    )

    assert result.status == "timeout"
    assert result.errors == [{
        "stage": "pipeline",
        "code": "job_deadline_exceeded",
        "message": "The fact-check exceeded its time limit.",
    }]


def test_deadline_expiring_during_extraction_does_not_start_search(monkeypatch):
    def slow_extraction(*_args, **_kwargs):
        time.sleep(0.03)
        return extraction_payload(1)

    monkeypatch.setattr(fact_checker, "_chat", slow_extraction)
    monkeypatch.setattr(
        fact_checker,
        "_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("search was called")),
    )

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.",
        deadline=time.monotonic() + 0.01,
    )

    assert result.status == "timeout"
    assert result.claim_count == 1
    assert result.errors[0]["code"] == "job_deadline_exceeded"


def test_whole_job_deadline_preserves_completed_verdicts(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(2))
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", [one_source()])
    )

    def verify(claim, *_args, **_kwargs):
        if claim["claim"] == "claim-1":
            time.sleep(0.4)
        return verdict_for(claim)

    monkeypatch.setattr(fact_checker, "_verify_one", verify)

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.",
        verify_workers=1,
        deadline=time.monotonic() + 0.2,
    )

    assert result.status == "partial"
    assert [item["claim"] for item in result] == ["claim-0"]
    assert result.errors[0]["code"] == "job_deadline_exceeded"


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

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

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
        "A sufficiently long factual sentence.", on_result=mutate
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

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

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

    providers = fake_provider_context(tavily=FakeTavily())

    # _search() now returns structured source dicts, not bare URL strings - source_index
    # references in VERIFY_PROMPT's source_analysis rely on this list's order matching
    # the "[N] url" headers in the evidence text exactly.
    text, sources = fact_checker._search(
        "bounded evidence", provider_context=providers
    )
    blocks = text.split("\n\n")

    assert captured["include_raw_content"] == "markdown"
    assert captured["timeout"] == fact_checker.TAVILY_TIMEOUT_SECONDS
    assert [s["url"] for s in sources] == [
        "https://one.example/a", "https://two.example/b", "https://three.example/c"
    ]
    assert [s["is_full_content"] for s in sources] == [True, False, True], \
        "raw_content used when present and non-empty; falls back to content only when raw_content is missing"
    assert "SNIPPET_MUST_NOT_APPEAR" not in text
    assert "OTHER_SNIPPET_MUST_NOT_APPEAR" not in text
    assert "FALLBACK_SNIPPET" in text
    assert len(text) <= fact_checker.MAX_EVIDENCE_TOTAL_CHARS
    assert all(
        len(block.split("\n", 1)[1]) <= fact_checker.MAX_EVIDENCE_PER_SOURCE_CHARS
        for block in blocks
    )
    # Evidence text is indexed [0], [1], [2]... in the same order as `sources`, so a
    # model-cited source_index maps back to the correct source unambiguously.
    for index, block in enumerate(blocks):
        assert block.startswith(f"[{index}] {sources[index]['url']}\n")
    assert providers.usage.snapshot()["tavily"]["estimated_credits"] == 2


def test_tavily_timeout_retries_are_bounded(monkeypatch):
    attempts = 0

    class FakeTavily:
        def search(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= fact_checker.PROVIDER_MAX_RETRIES:
                raise TimeoutError("secret upstream timeout details")
            return {"results": []}

    providers = fake_provider_context(tavily=FakeTavily())
    monkeypatch.setattr(fact_checker.time, "sleep", lambda _seconds: None)

    assert fact_checker._search(
        "retry query", provider_context=providers
    ) == ("", [])
    assert attempts == fact_checker.PROVIDER_MAX_RETRIES + 1


def test_gemini_chat_has_an_explicit_request_timeout(monkeypatch):
    captured = {}
    response = FakeChatStream("[", "]")
    completions = SimpleNamespace(
        create=lambda **kwargs: captured.update(kwargs) or response
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    providers = fake_provider_context(gemini=client)

    response_format = {"type": "json_object"}
    assert fact_checker._chat(
        "system", "user", 12,
        response_format=response_format,
        provider_context=providers,
    ) == "[]"
    assert captured["timeout"] == fact_checker.GEMINI_TIMEOUT_SECONDS
    assert "temperature" not in captured
    assert captured["reasoning_effort"] == "minimal"
    assert captured["response_format"] == response_format
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}
    assert response.closed is True


def test_gemini_stream_usage_is_recorded_by_stage_and_claim():
    class UsageStream(FakeChatStream):
        def __iter__(self):
            events = list(super().__iter__())
            events.append(SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(
                    prompt_tokens=40,
                    completion_tokens=8,
                    total_tokens=48,
                ),
            ))
            return iter(events)

    response = UsageStream("[]")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_kwargs: response,
    )))
    providers = fake_provider_context(gemini=client)

    fact_checker._chat(
        "system",
        "user",
        12,
        provider_context=providers,
        stage="verification",
        claim_index=3,
    )

    usage = providers.usage.snapshot()
    assert usage["gemini"]["total_tokens"] == 48
    assert usage["gemini"]["events"][0]["stage"] == "verification"
    assert usage["gemini"]["events"][0]["claim_index"] == 3
    assert usage["complete"] is True


def test_gemini_timeout_uses_only_the_remaining_job_budget(monkeypatch):
    captured = {}
    response = FakeChatStream("[]")
    completions = SimpleNamespace(
        create=lambda **kwargs: captured.update(kwargs) or response
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    providers = fake_provider_context(gemini=client)

    deadline = time.monotonic() + 5
    assert fact_checker._chat(
        "system", "user", 12, deadline=deadline, provider_context=providers
    ) == "[]"
    assert 0 < captured["timeout"] <= 5


def test_gemini_stream_closes_when_job_deadline_expires(monkeypatch):
    remaining_checks = 0
    response = FakeChatStream("partial", " response")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_kwargs: response,
    )))

    def remaining(_deadline):
        nonlocal remaining_checks
        remaining_checks += 1
        if remaining_checks == 1:
            return 5.0
        raise fact_checker.WholeJobDeadlineExceeded()

    providers = fake_provider_context(gemini=client)
    monkeypatch.setattr(fact_checker, "_remaining_seconds", remaining)

    try:
        fact_checker._chat(
            "system", "user", 12,
            deadline=time.monotonic() + 5,
            provider_context=providers,
        )
    except fact_checker.WholeJobDeadlineExceeded:
        pass
    else:
        raise AssertionError("expired whole-job deadline was not raised")

    assert response.closed is True
    assert remaining_checks == 2


def test_gemini_does_not_retry_after_whole_job_deadline(monkeypatch):
    attempts = 0

    def fail(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("provider slow")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail))
    )
    providers = fake_provider_context(gemini=client)

    try:
        fact_checker._chat(
            "system", "user", 12,
            deadline=time.monotonic() + 0.02,
            provider_context=providers,
        )
    except fact_checker.WholeJobDeadlineExceeded:
        pass
    else:
        raise AssertionError("expired whole-job deadline was not raised")

    assert attempts == 1


def test_all_malformed_verification_outputs_fail(monkeypatch):
    def chat(system, *_args, **_kwargs):
        if system == fact_checker.EXTRACT_PROMPT:
            return extraction_payload(2)
        return "raw malformed provider body"

    monkeypatch.setattr(fact_checker, "_chat", chat)
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", [one_source()])
    )

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

    assert result.status == "failed"
    assert {error["code"] for error in result.errors} == {"provider_protocol_error"}
    assert "raw malformed provider body" not in str(result.errors)


def test_malformed_verification_with_success_is_partial(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(2))
    monkeypatch.setattr(
        fact_checker, "_search", lambda *_args, **_kwargs: ("evidence", [one_source()])
    )

    def verify(claim, *_args, **_kwargs):
        if claim["claim"] == "claim-0":
            return verdict_for(claim)
        raise fact_checker.ProviderProtocolError("malformed")

    monkeypatch.setattr(fact_checker, "_verify_one", verify)

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

    assert result.status == "partial"
    assert [item["claim"] for item in result] == ["claim-0"]


def test_verify_one_source_analysis_overrides_inconsistent_model_fields(monkeypatch):
    # The whole point of source_analysis: the model's own top-level supported/
    # contradicted must not be trusted if it disagrees with its own per-source
    # breakdown - Python derives the real values from source_analysis instead.
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: json.dumps([{
        "claim": "x", "supported": True, "contradicted": False, "verdict": "TRUE",
        "confidence": 95, "explanation": "looks true",
        "source_analysis": [
            {"source_index": 0, "stance": "CONTRADICTS", "directness": "DIRECT",
             "reason": "source denies it", "evidence_excerpt": "this did not happen"},
        ],
    }]))
    verdict = fact_checker._verify_one(
        {"claim": "The event happened.", "speaker": "X"},
        "evidence", [one_source(content="Officials say this did not happen.")],
    )
    assert verdict["supported"] is False
    assert verdict["contradicted"] is True
    assert verdict["verdict"] == "FALSE", "model said TRUE, but its own source_analysis says CONTRADICTS"


def test_verify_one_falls_back_to_top_level_fields_without_source_analysis(monkeypatch):
    # Graceful degradation: a missing/unusable source_analysis must not fail or
    # retry the claim - fall back to the model's own supported/contradicted.
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: json.dumps([{
        "claim": "x", "supported": True, "contradicted": False, "verdict": "TRUE",
        "confidence": 80, "explanation": "supported",
    }]))
    verdict = fact_checker._verify_one(
        {"claim": "The event happened.", "speaker": "X"},
        "evidence", [one_source()],
    )
    assert verdict["verdict"] == "TRUE"
    assert verdict["source_analysis"] == []


def test_verify_one_caps_confidence_without_direct_evidence(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: json.dumps([{
        "claim": "x", "supported": True, "contradicted": False, "verdict": "TRUE",
        "confidence": 97, "explanation": "supported",
        "source_analysis": [
            {"source_index": 0, "stance": "SUPPORTS", "directness": "INDIRECT",
             "reason": "generally on topic"},
        ],
    }]))
    verdict = fact_checker._verify_one(
        {"claim": "The event happened.", "speaker": "X"}, "evidence", [one_source()],
    )
    # Confidence is capped silently - no user-facing warning text is produced.
    assert verdict["confidence"] <= fact_checker._CONFIDENCE_CAP_NO_DIRECT_SOURCE
    assert "warning" not in verdict


def test_verify_one_attaches_source_titles_not_bare_urls(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: json.dumps([{
        "claim": "x", "supported": True, "contradicted": False, "verdict": "TRUE",
        "confidence": 90, "explanation": "supported",
        "source_analysis": [{"source_index": 0, "stance": "SUPPORTS", "directness": "DIRECT"}],
    }]))
    verdict = fact_checker._verify_one(
        {"claim": "x", "speaker": "X"}, "evidence",
        [one_source(url="https://reuters.com/a", title="Tariffs announced")],
    )
    assert verdict["sources"] == [{
        "url": "https://reuters.com/a", "title": "Tariffs announced", "domain": "reuters.com",
    }]


def test_verify_one_uses_one_based_source_references_in_explanation(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: json.dumps([{
        "claim": "x", "supported": True, "contradicted": False, "verdict": "TRUE",
        "confidence": 90,
        "explanation": "Source [0] supports the claim; source [1] adds context. Keep [99].",
        "source_analysis": [
            {"source_index": 0, "stance": "SUPPORTS", "directness": "DIRECT"},
            {"source_index": 1, "stance": "PARTIAL", "directness": "DIRECT"},
        ],
    }]))
    verdict = fact_checker._verify_one(
        {"claim": "x", "speaker": "X"}, "evidence",
        [one_source(url="https://one.example"), one_source(url="https://two.example")],
    )
    assert verdict["explanation"] == (
        "Source [1] supports the claim; source [2] adds context. Keep [99]."
    )


def test_verify_one_enforces_english_when_model_replies_in_indonesian(monkeypatch):
    captured = {}

    def chat(_system, user, *_args, **kwargs):
        captured["user"] = user
        captured["response_format"] = kwargs.get("response_format")
        return json.dumps([{
            "claim": "x", "supported": True, "contradicted": False, "verdict": "TRUE",
            "confidence": 90,
            "explanation": "Pernyataan ini didukung oleh bukti yang tersedia dalam sumber.",
            "source_analysis": [
                {"source_index": 0, "stance": "SUPPORTS", "directness": "DIRECT"},
            ],
        }])

    monkeypatch.setattr(fact_checker, "_chat", chat)
    verdict = fact_checker._verify_one(
        {"claim": "The available medical evidence directly supports this claim.",
         "speaker": "X"},
        "evidence", [one_source()],
    )

    assert "REQUIRED OUTPUT LANGUAGE: English" in captured["user"]
    assert captured["response_format"] == fact_checker.VERIFY_RESPONSE_FORMAT
    assert verdict["explanation"] == (
        "The accepted evidence in source [1] directly supports this claim."
    )


def test_retry_claim_reuses_saved_evidence_without_searching(monkeypatch):
    monkeypatch.setattr(
        fact_checker,
        "_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("search reran")),
    )
    monkeypatch.setattr(
        fact_checker,
        "_verify_one",
        lambda claim, *_args, **_kwargs: {
            "claim": claim["claim"],
            "verdict": "TRUE",
            "supported": True,
            "contradicted": False,
            "confidence": 90,
            "explanation": "Supported by the saved evidence.",
            "sources": [],
        },
    )

    result = fact_checker.retry_claim(
        {"claim": "The event happened.", "query": "event happened", "speaker": "A"},
        3,
        evidence={"search_text": "saved evidence", "sources": [one_source()]},
    )

    assert result.status == "completed"
    assert result[0]["claim_index"] == 3
    assert result[0]["speaker"] == "A"


def test_fact_check_exposes_private_claims_and_evidence_to_job_storage(monkeypatch):
    claims_seen = []
    evidence_seen = []
    source = one_source(content="Saved evidence.")
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: extraction_payload(1))
    monkeypatch.setattr(fact_checker, "_search", lambda *_args, **_kwargs: ("Saved evidence.", [source]))
    monkeypatch.setattr(
        fact_checker, "_verify_one", lambda claim, *_args, **_kwargs: verdict_for(claim)
    )

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.",
        on_claims=claims_seen.append,
        on_evidence=evidence_seen.append,
    )

    assert result.status == "completed"
    assert claims_seen == [[{
        "claim": "claim-0", "query": "query-0", "speaker": "speaker-0",
    }]]
    assert evidence_seen == [{
        "claim_index": 0,
        "search_text": "Saved evidence.",
        "sources": [source],
    }]


def test_retry_claim_reports_insufficient_evidence_as_non_provider_failure(monkeypatch):
    monkeypatch.setattr(fact_checker, "_search", lambda *_args, **_kwargs: ("", []))

    result = fact_checker.retry_claim(
        {"claim": "The event happened.", "query": "event happened"}, 1,
    )

    assert result.status == "no_evidence"
    assert result.errors == [{
        "stage": "search",
        "code": "claim_no_evidence",
        "message": "No sufficient evidence was found for this claim.",
        "claim_index": 1,
    }]


def test_malformed_extraction_is_not_no_claims(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: "not JSON")

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

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

    result = fact_checker.fact_check("A sufficiently long factual sentence.")

    assert result.status == "rate_limited"
    assert result.errors == [{
        "stage": "search",
        "code": "provider_rate_limited",
        "message": "A provider rate limit was reached.",
        "claim_index": 0,
    }]
    assert "secret" not in str(result.errors)
