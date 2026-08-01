import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from types import SimpleNamespace

import fact_checker
from providers import ProviderConcurrencyGate, ProviderContext, ProviderCredentials


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


def fake_provider_context(*, gemini=None):
    context = ProviderContext(ProviderCredentials.create("gemini-test-key"))
    context._gemini_client = gemini
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


def test_domain_normalizes_actual_hostname_not_userinfo_or_port():
    assert fact_checker._domain(
        "https://reuters.com@ATTACKER.EXAMPLE:8443/story"
    ) == "attacker.example"
    assert fact_checker._domain("https://WWW.REUTERS.COM.:443/story") == "reuters.com"
    assert fact_checker._domain("https://[bad") == ""


def test_grounded_sources_skip_malformed_citation_urls():
    response = SimpleNamespace(
        candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[
                SimpleNamespace(web=SimpleNamespace(
                    uri="https://[bad", title="reuters.com"
                )),
                SimpleNamespace(web=SimpleNamespace(
                    uri="https://valid.example/story", title="Valid"
                )),
            ],
            grounding_supports=[
                SimpleNamespace(
                    segment=SimpleNamespace(text="Malformed source text"),
                    grounding_chunk_indices=[0],
                ),
                SimpleNamespace(
                    segment=SimpleNamespace(text="Valid mapped segment"),
                    grounding_chunk_indices=[1],
                ),
            ],
            web_search_queries=["query"],
        ))],
    )

    sources = fact_checker._grounded_sources(response)
    assert [source["url"] for source in sources] == [
        "https://valid.example/story"
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
        if query in {"query-0", "claim-0"}:
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


def _grounded_response(text="grounded evidence", *, queries=None):
    chunks = [
        SimpleNamespace(web=SimpleNamespace(uri="https://official.gov/a", title="Official")),
        SimpleNamespace(web=SimpleNamespace(uri="https://news.example/b", title="News")),
        SimpleNamespace(web=SimpleNamespace(uri="https://reddit.com/blocked", title="reddit.com")),
    ]
    supports = [
        SimpleNamespace(
            segment=SimpleNamespace(text="Official evidence " + "R" * 5_000),
            grounding_chunk_indices=[0],
        ),
        SimpleNamespace(
            segment=SimpleNamespace(text="Independent report"),
            grounding_chunk_indices=[1],
        ),
    ]
    metadata = SimpleNamespace(
        grounding_chunks=chunks,
        grounding_supports=supports,
        web_search_queries=queries or ["query one", "query two", "query one", ""],
    )
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(grounding_metadata=metadata)],
        usage_metadata=SimpleNamespace(
            prompt_token_count=40,
            candidates_token_count=8,
            total_token_count=48,
        ),
    )


def test_grounded_search_preserves_citations_bounds_evidence_and_counts_queries():
    captured = {}
    response = _grounded_response()
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: captured.update(kwargs) or response,
    ))
    providers = fake_provider_context(gemini=client)

    text, sources = fact_checker._search("bounded evidence", provider_context=providers)
    blocks = text.split("\n\n")

    assert captured["model"] == fact_checker.MODEL
    config = captured["config"]
    assert config.tools[0].google_search is not None
    assert config.temperature is None
    assert config.thinking_config.thinking_level.value == "MINIMAL"
    assert config.http_options.timeout == fact_checker.GEMINI_TIMEOUT_SECONDS * 1000
    assert [source["url"] for source in sources] == [
        "https://official.gov/a", "https://news.example/b"
    ]
    assert all(source["is_full_content"] is False for source in sources)
    assert "reddit.com" not in text
    assert len(text) <= fact_checker.MAX_EVIDENCE_TOTAL_CHARS
    assert all(
        len(block.split("\n", 2)[2]) <= fact_checker.MAX_EVIDENCE_PER_SOURCE_CHARS
        for block in blocks
    )
    for index, block in enumerate(blocks):
        assert block.startswith(f"[{index}] {sources[index]['url']}\n")
    usage = providers.usage.snapshot()
    assert usage["gemini"]["total_tokens"] == 48
    assert usage["google_search"]["query_count"] == 2


def test_grounded_sources_omit_unbound_chunks_and_never_reuse_whole_response():
    response = SimpleNamespace(
        text="WHOLE_RESPONSE_MUST_NOT_BECOME_EVIDENCE",
        candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
            grounding_chunks=[
                SimpleNamespace(web=SimpleNamespace(
                    uri="https://bound.example/a", title="bound.example"
                )),
                SimpleNamespace(web=SimpleNamespace(
                    uri="https://unbound.example/b", title="unbound.example"
                )),
            ],
            grounding_supports=[SimpleNamespace(
                segment=SimpleNamespace(text="Mapped grounded segment"),
                grounding_chunk_indices=[0],
            )],
            web_search_queries=["bound query"],
        ))],
    )
    sources = fact_checker._grounded_sources(response)
    assert [source["url"] for source in sources] == ["https://bound.example/a"]
    assert sources[0]["content"] == "Mapped grounded segment"
    assert "WHOLE_RESPONSE" not in sources[0]["content"]
    assert sources[0]["evidence_kind"] == "grounded_summary"


def test_source_title_cannot_spoof_high_quality_ranking():
    ranked = fact_checker._filter_sources([
        {
            "url": "https://attacker.example/a",
            "title": "reuters.com",
            "content": "x",
        },
        {
            "url": "https://en.wikipedia.org/wiki/Example",
            "title": "Wikipedia",
            "content": "y",
        },
    ])
    assert ranked[0]["url"].startswith("https://en.wikipedia.org/")


def test_trusted_grounding_redirect_uses_publisher_title_for_ranking():
    response = SimpleNamespace(candidates=[SimpleNamespace(
        grounding_metadata=SimpleNamespace(
            grounding_chunks=[SimpleNamespace(web=SimpleNamespace(
                uri="https://vertexaisearch.cloud.google.com/grounding-api-redirect/token",
                title="whitehouse.gov",
            ))],
            grounding_supports=[SimpleNamespace(
                segment=SimpleNamespace(text="Official announcement."),
                grounding_chunk_indices=[0],
            )],
        ),
    )])

    sources = fact_checker._grounded_sources(response)

    assert sources[0]["domain"] == "whitehouse.gov"
    assert sources[0]["provider_domain"] == "vertexaisearch.cloud.google.com"
    assert fact_checker._source_quality(fact_checker._source_domain(sources[0]))[0] == 1


def test_prepare_source_replaces_provider_redirect_with_canonical_url(monkeypatch):
    monkeypatch.setattr(
        fact_checker,
        "resolve_public_redirect",
        lambda _url, **_kwargs: "https://www.whitehouse.gov/briefing-room/statement/",
    )

    prepared = fact_checker._prepare_source({
        "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token",
        "provider_url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token",
        "publisher_domain": "whitehouse.gov",
        "title": "whitehouse.gov",
    })

    assert prepared["url"] == "https://www.whitehouse.gov/briefing-room/statement/"
    assert prepared["canonical_url"] == prepared["url"]
    assert prepared["provider_url"].startswith("https://vertexaisearch.cloud.google.com/")
    assert prepared["source_type"] == "official_or_primary"
    assert prepared["quality_tier"] == 1


def test_search_claim_uses_one_fallback_only_after_empty_evidence(monkeypatch):
    searched = []

    def search(query, *_args):
        searched.append(query)
        if len(searched) == 1:
            return "", []
        return "evidence", [one_source()]

    monkeypatch.setattr(fact_checker, "_search", search)
    text, sources, attempted = fact_checker._search_claim({
        "claim": "The policy took effect in May.",
        "query": "official policy effective date May",
    })

    assert text == "evidence"
    assert attempted == searched == [
        "official policy effective date May", "The policy took effect in May.",
    ]
    assert sources[0]["search_attempt"] == 2


def test_search_claim_does_not_retry_provider_failure(monkeypatch):
    searched = []

    def search(query, *_args):
        searched.append(query)
        raise TimeoutError("provider failed")

    monkeypatch.setattr(fact_checker, "_search", search)
    with __import__("pytest").raises(TimeoutError):
        fact_checker._search_claim({"claim": "claim text", "query": "first query"})
    assert searched == ["first query"]


def test_claim_deduplication_is_conservative_and_keeps_provenance():
    claims = [
        {"claim": "The rate increased to 12 percent in May.", "query": "q1"},
        {"claim": "The rate increased to 12 percent in May", "query": "q2"},
        {"claim": "The rate increased to 13 percent in May.", "query": "q3"},
    ]

    deduplicated = fact_checker._deduplicate_claims(claims)

    assert len(deduplicated) == 2
    assert deduplicated[0]["merged_from"] == [0, 1]
    assert deduplicated[0]["original_claims"] == [
        claims[0]["claim"], claims[1]["claim"],
    ]
    assert deduplicated[1]["claim"] == claims[2]["claim"]


def test_grounded_search_timeout_is_bounded_by_remaining_deadline():
    captured = {}
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: captured.update(kwargs) or _grounded_response(),
    ))
    providers = fake_provider_context(gemini=client)
    fact_checker._search(
        "deadline query",
        deadline=time.monotonic() + 2,
        provider_context=providers,
    )
    assert 0 < captured["config"].http_options.timeout <= 2_000


def test_grounded_search_timeout_retries_are_bounded(monkeypatch):
    attempts = 0

    def generate_content(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= fact_checker.PROVIDER_MAX_RETRIES:
            raise TimeoutError("secret upstream timeout details")
        return SimpleNamespace(
            text="",
            candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(
                grounding_chunks=[], grounding_supports=[], web_search_queries=[]
            ))],
            usage_metadata=None,
        )

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    providers = fake_provider_context(gemini=client)
    monkeypatch.setattr(fact_checker.time, "sleep", lambda _seconds: None)
    assert fact_checker._search("retry query", provider_context=providers) == ("", [])
    assert attempts == fact_checker.PROVIDER_MAX_RETRIES + 1


def test_gemini_chat_uses_official_generate_content_and_json_schema():
    captured = {}
    response = SimpleNamespace(
        text="[]",
        usage_metadata=SimpleNamespace(
            prompt_token_count=4, candidates_token_count=1, total_token_count=5
        ),
    )
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: captured.update(kwargs) or response,
    ))
    providers = fake_provider_context(gemini=client)
    response_format = fact_checker.EXTRACT_RESPONSE_FORMAT
    assert fact_checker._chat(
        "system", "user", 12,
        response_format=response_format,
        provider_context=providers,
    ) == "[]"
    config = captured["config"]
    assert captured["model"] == fact_checker.MODEL
    assert captured["contents"] == "user"
    assert config.system_instruction == "system"
    assert config.max_output_tokens == 12
    assert config.temperature is None
    assert config.thinking_config.thinking_level.value == "MINIMAL"
    assert config.http_options.timeout == fact_checker.GEMINI_TIMEOUT_SECONDS * 1000
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == response_format["json_schema"]["schema"]


def test_gemini_usage_is_recorded_by_stage_and_claim():
    response = SimpleNamespace(
        text="[]",
        usage_metadata=SimpleNamespace(
            prompt_token_count=40,
            candidates_token_count=8,
            total_token_count=48,
        ),
    )
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **_kwargs: response,
    ))
    providers = fake_provider_context(gemini=client)
    fact_checker._chat(
        "system", "user", 12,
        provider_context=providers,
        stage="verification",
        claim_index=3,
    )
    usage = providers.usage.snapshot()
    assert usage["gemini"]["total_tokens"] == 48
    assert usage["gemini"]["events"][0]["stage"] == "verification"
    assert usage["gemini"]["events"][0]["claim_index"] == 3
    assert usage["complete"] is True


def test_gemini_checks_whole_job_deadline_after_response(monkeypatch):
    checks = 0
    response = SimpleNamespace(
        text="[]",
        usage_metadata=SimpleNamespace(
            prompt_token_count=4, candidates_token_count=1, total_token_count=5
        ),
    )
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **_kwargs: response,
    ))

    def remaining(_deadline):
        nonlocal checks
        checks += 1
        if checks == 1:
            return 5.0
        raise fact_checker.WholeJobDeadlineExceeded()

    providers = fake_provider_context(gemini=client)
    monkeypatch.setattr(fact_checker, "_remaining_seconds", remaining)
    with __import__("pytest").raises(fact_checker.WholeJobDeadlineExceeded):
        fact_checker._chat(
            "system", "user", 12,
            deadline=time.monotonic() + 5,
            provider_context=providers,
        )
    assert checks == 2
    usage = providers.usage.snapshot()
    assert usage["gemini"]["requests"] == 1
    assert usage["gemini"]["total_tokens"] == 5


def test_gemini_does_not_retry_after_whole_job_deadline(monkeypatch):
    attempts = 0

    def fail(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("provider slow")

    client = SimpleNamespace(models=SimpleNamespace(generate_content=fail))
    providers = fake_provider_context(gemini=client)
    with __import__("pytest").raises(fact_checker.WholeJobDeadlineExceeded):
        fact_checker._chat(
            "system", "user", 12,
            deadline=time.monotonic() + 0.02,
            provider_context=providers,
        )
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


def test_verify_one_rejects_top_level_fields_without_source_analysis(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: json.dumps([{
        "claim": "x", "supported": True, "contradicted": False, "verdict": "TRUE",
        "confidence": 80, "explanation": "supported",
    }]))
    with __import__("pytest").raises(
        fact_checker.ProviderProtocolError, match="source analysis"
    ):
        fact_checker._verify_one(
            {"claim": "The event happened.", "speaker": "X"},
            "evidence", [one_source()],
        )


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


def test_verify_one_recalculates_confidence_from_auditable_evidence(monkeypatch):
    monkeypatch.setattr(fact_checker, "_chat", lambda *_args, **_kwargs: json.dumps([{
        "claim": "x", "supported": True, "contradicted": False, "verdict": "TRUE",
        "confidence": 99, "explanation": "supported",
        "source_analysis": [
            {"source_index": 0, "stance": "SUPPORTS", "directness": "DIRECT"},
        ],
    }]))
    official = fact_checker._verify_one(
        {"claim": "The event happened.", "speaker": "X"}, "evidence",
        [one_source(url="https://agency.gov/report", quality_tier=1)],
    )
    weak = fact_checker._verify_one(
        {"claim": "The event happened.", "speaker": "X"}, "evidence",
        [one_source(url="https://unknown.example/post", quality_tier=4)],
    )

    assert official["confidence"] > weak["confidence"]
    assert official["confidence"] != 99
    assert official["confidence_factors"]["model_score_used"] is False
    assert official["confidence_factors"]["meaning"] == (
        "evidence_strength_not_truth_probability"
    )


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
        "sources": [{**source, "search_query": "query-0", "search_attempt": 1}],
        "search_queries": ["query-0"],
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
        "quota_category": "unknown",
        "retry_after_seconds": 60,
    }]
    assert "secret" not in str(result.errors)


def test_retry_delay_uses_exponential_jitter_and_honors_retry_after(monkeypatch):
    monkeypatch.setattr(fact_checker.random, "uniform", lambda _low, _high: 0.25)
    exception = RuntimeError("rate limited")
    exception.response = SimpleNamespace(
        status_code=429,
        headers={"Retry-After": "7"},
    )

    assert fact_checker._retry_delay_seconds(0, exception) == 7
    assert fact_checker._retry_delay_seconds(3, RuntimeError("temporary")) == 8.25


def test_retry_sleep_applies_the_calculated_delay_without_passing_deadline(
    monkeypatch,
):
    slept = []
    monkeypatch.setattr(fact_checker.random, "uniform", lambda _low, _high: 0.0)
    monkeypatch.setattr(fact_checker.time, "sleep", slept.append)

    assert fact_checker._sleep_before_retry(
        2,
        time.monotonic() + 10,
        TimeoutError("temporary"),
    ) is True
    assert slept == [4.0]


def test_first_rate_limit_stops_new_claim_work_and_marks_all_unfinished_retryable(
    monkeypatch,
):
    providers = fake_provider_context()
    searched = []

    class RateLimited(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=429, headers={"Retry-After": "9"})
            self.details = {
                "error": {
                    "details": [{
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaMetric": "requests_per_minute"}],
                    }],
                },
            }
            super().__init__("private provider details")

    monkeypatch.setattr(
        fact_checker, "_chat",
        lambda *_args, **_kwargs: extraction_payload(6),
    )

    def rate_limited_search(
        _query, _deadline, context, claim_index, _cancel_event=None,
    ):
        searched.append(claim_index)
        exception = RateLimited()
        context.trip_rate_limit(exception)
        raise exception

    monkeypatch.setattr(fact_checker, "_search", rate_limited_search)

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.",
        search_workers=1,
        verify_workers=1,
        provider_context=providers,
    )

    assert result.status == "rate_limited"
    assert searched == [0]
    assert [error["claim_index"] for error in result.errors] == list(range(6))
    assert all(error["code"] == "provider_rate_limited" for error in result.errors)
    assert all(error["quota_category"] == "RPM" for error in result.errors)
    assert all(error["retry_after_seconds"] == 9 for error in result.errors)
    assert "private" not in str(result.errors)


def test_open_rate_limit_circuit_terminates_without_sleep_or_provider_call(
    monkeypatch,
):
    calls = []
    sleeps = []
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **_kwargs: calls.append(True),
    ))
    providers = fake_provider_context(gemini=client)
    exception = RuntimeError("private provider details")
    exception.response = SimpleNamespace(status_code=429, headers={})
    providers.trip_rate_limit(exception)
    monkeypatch.setattr(fact_checker.time, "sleep", sleeps.append)

    with __import__("pytest").raises(
        fact_checker.ProviderRateLimitCircuitOpen
    ):
        fact_checker._chat(
            "system", "user", 12, provider_context=providers,
        )

    assert calls == []
    assert sleeps == []
    assert providers.usage.snapshot()["gemini"]["requests"] == 0


def test_first_429_opens_circuit_without_retrying_owner_or_sibling(monkeypatch):
    calls = 0

    class RateLimited(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=429, headers={})
            self.details = {}
            super().__init__("private quota details")

    def generate_content(**_kwargs):
        nonlocal calls
        calls += 1
        raise RateLimited()

    providers = fake_provider_context(gemini=SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content),
    ))
    monkeypatch.setattr(fact_checker.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fact_checker.random, "uniform", lambda _low, _high: 0)

    with __import__("pytest").raises(RateLimited):
        fact_checker._chat(
            "system", "owner", 12, provider_context=providers,
        )
    with __import__("pytest").raises(
        fact_checker.ProviderRateLimitCircuitOpen
    ):
        fact_checker._chat(
            "system", "sibling", 12, provider_context=providers,
        )

    assert calls == 1
    assert providers.usage.snapshot()["gemini"]["requests"] == 1


def test_cancellation_interrupts_backoff_before_another_provider_call(
    monkeypatch,
):
    calls = 0
    cancel_event = threading.Event()

    def generate_content(**_kwargs):
        nonlocal calls
        calls += 1
        cancel_event.set()
        raise TimeoutError("temporary")

    providers = fake_provider_context(gemini=SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content),
    ))
    with __import__("pytest").raises(fact_checker.ProviderBackoffCancelled):
        fact_checker._chat(
            "system",
            "user",
            12,
            provider_context=providers,
            cancel_event=cancel_event,
        )

    assert calls == 1


def test_cancellation_while_waiting_for_slot_prevents_provider_call():
    gate = ProviderConcurrencyGate(gemini_limit=1)
    providers = ProviderContext(
        ProviderCredentials.create("gemini-test-key"),
        concurrency_gate=gate,
    )
    provider_calls = []
    providers._gemini_client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **_kwargs: provider_calls.append(True),
    ))
    occupied = threading.Event()
    release = threading.Event()
    cancel_event = threading.Event()

    def hold_slot():
        with gate.gemini_slot(timeout=1):
            occupied.set()
            release.wait(1)

    holder = threading.Thread(target=hold_slot)
    holder.start()
    assert occupied.wait(1)
    outcome = []

    def queued_request():
        try:
            fact_checker._chat(
                "system",
                "queued",
                12,
                provider_context=providers,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            outcome.append(exc)

    queued = threading.Thread(target=queued_request)
    queued.start()
    cancel_event.set()
    release.set()
    holder.join(timeout=1)
    queued.join(timeout=1)

    assert len(outcome) == 1
    assert isinstance(outcome[0], fact_checker.ProviderBackoffCancelled)
    assert provider_calls == []


def test_circuit_preserves_late_grounded_evidence_for_verification_only_retry(
    monkeypatch,
):
    providers = fake_provider_context()
    evidence_seen = []
    release_late_search = threading.Event()
    both_searches_started = threading.Barrier(2)

    class RateLimited(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=429, headers={})
            self.details = {}
            super().__init__("private quota details")

    monkeypatch.setattr(
        fact_checker, "_chat",
        lambda *_args, **_kwargs: extraction_payload(2),
    )

    def search(_query, _deadline, context, claim_index, _cancel_event=None):
        both_searches_started.wait(timeout=1)
        if claim_index == 0:
            exception = RateLimited()
            context.trip_rate_limit(exception)
            release_late_search.set()
            raise exception
        assert release_late_search.wait(1)
        time.sleep(0.01)
        return "Already-paid grounded evidence.", [one_source()]

    monkeypatch.setattr(fact_checker, "_search", search)

    result = fact_checker.fact_check(
        "A sufficiently long factual sentence.",
        search_workers=2,
        verify_workers=1,
        provider_context=providers,
        on_evidence=evidence_seen.append,
    )

    assert result.status == "rate_limited"
    assert evidence_seen == [{
        "claim_index": 1,
        "search_text": "Already-paid grounded evidence.",
        "sources": [{**one_source(), "search_query": "query-1", "search_attempt": 1}],
        "search_queries": ["query-1"],
    }]
    error_by_claim = {error["claim_index"]: error for error in result.errors}
    assert error_by_claim[0]["stage"] == "search"
    assert error_by_claim[1]["stage"] == "verification"
