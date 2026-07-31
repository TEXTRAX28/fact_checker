import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from providers import (
    InvalidProviderCredentials,
    ProviderConcurrencyGate,
    ProviderContext,
    ProviderCredentials,
    UsageLedger,
    merge_usage,
    public_rate_limit_info,
)


def test_credentials_validate_and_hide_secret_values():
    credentials = ProviderCredentials.create("gemini-private-key")
    assert "gemini-private-key" not in repr(credentials)
    with pytest.raises(InvalidProviderCredentials, match="Gemini"):
        ProviderCredentials.create("short")


def test_usage_ledger_prices_tool_prompt_and_thinking_tokens_thread_safely():
    ledger = UsageLedger()

    def record(index):
        ledger.record_gemini(
            model="model",
            stage="search",
            claim_index=index,
            attempt=1,
            usage={
                "prompt_token_count": 10,
                "tool_use_prompt_token_count": 3,
                "candidates_token_count": 2,
                "thoughts_token_count": 5,
                "total_token_count": 17,
            },
            succeeded=True,
            search_query_count=2,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(20)))

    snapshot = ledger.snapshot()
    assert snapshot["gemini"]["requests"] == 20
    assert snapshot["gemini"]["input_tokens"] == 260
    assert snapshot["gemini"]["output_tokens"] == 140
    assert snapshot["gemini"]["total_tokens"] == 400
    assert snapshot["google_search"]["query_count"] == 40
    assert snapshot["estimated_cost_usd"] == pytest.approx(
        260 * 0.30 / 1_000_000
        + 140 * 2.50 / 1_000_000
        + 40 * 0.014
    )
    assert snapshot["complete"] is True

    ledger.record_gemini(
        model="model",
        stage="verification",
        claim_index=20,
        attempt=1,
        succeeded=False,
    )
    assert ledger.snapshot()["complete"] is False


def test_usage_estimate_is_partial_when_search_query_metadata_is_missing():
    ledger = UsageLedger()
    ledger.record_gemini(
        model="model",
        stage="search",
        claim_index=0,
        attempt=1,
        usage={"prompt_token_count": 10, "candidates_token_count": 2},
        succeeded=True,
        search_query_count=None,
    )
    snapshot = ledger.snapshot()
    assert snapshot["google_search"]["query_count"] == 0
    assert snapshot["complete"] is False
    assert snapshot["pricing"]["model"] == "gemini-3.5-flash-lite"
    assert snapshot["pricing"]["pricing_date"] == "2026-07"
    assert snapshot["pricing"]["pricing_url"].startswith("https://ai.google.dev/")


def test_merge_usage_adds_retry_totals_and_search_queries_once():
    first = UsageLedger()
    second = UsageLedger()
    first.record_gemini(
        model="model",
        stage="extraction",
        claim_index=None,
        attempt=1,
        usage={"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 15},
        succeeded=True,
    )
    second.record_gemini(
        model="model",
        stage="search",
        claim_index=1,
        attempt=1,
        usage={"prompt_token_count": 8, "candidates_token_count": 4, "total_token_count": 12},
        succeeded=True,
        search_query_count=2,
    )

    merged = merge_usage(first.snapshot(), second.snapshot())
    assert merged["gemini"]["total_tokens"] == 27
    assert merged["google_search"]["query_count"] == 2
    assert merged["estimated_cost_usd"] > 0.028
    assert merged["complete"] is True


def test_provider_contexts_construct_official_sdk_clients_with_their_own_keys(monkeypatch):
    created_keys = []

    class FakeClient:
        def __init__(self, *, api_key, **_kwargs):
            created_keys.append(api_key)

    monkeypatch.setattr("google.genai.Client", FakeClient)
    contexts = [
        ProviderContext(ProviderCredentials.create("gemini-first-key")),
        ProviderContext(ProviderCredentials.create("gemini-second-key")),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda context: context.gemini_client(timeout=1), contexts))
    assert set(created_keys) == {"gemini-first-key", "gemini-second-key"}


def test_provider_concurrency_gate_caps_process_wide_work():
    gate = ProviderConcurrencyGate(gemini_limit=2)
    lock = threading.Lock()
    active = 0
    peak = 0

    def run():
        nonlocal active, peak
        with gate.gemini_slot(timeout=1):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: run(), range(12)))
    assert peak == 2


def test_rate_limit_info_uses_only_structured_quota_metadata_and_safe_delay():
    exception = RuntimeError("secret project and account details; TPM maybe")
    exception.response = SimpleNamespace(
        status_code=429,
        headers={"Retry-After": "9999"},
    )
    exception.details = {
        "error": {
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{
                        "quotaMetric": "generativelanguage.googleapis.com/"
                        "generate_content_input_tokens_per_minute",
                    }],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "42.2s",
                },
            ],
        },
    }

    info = public_rate_limit_info(exception)

    assert info.category == "TPM"
    # The explicit header is honored first but remains bounded for public output.
    assert info.retry_after_seconds == 300
    assert "secret" not in str(info.to_dict())


def test_rate_limit_info_does_not_classify_unstructured_provider_message():
    exception = RuntimeError("requests per minute for project secret-project")
    exception.response = SimpleNamespace(status_code=429, headers={})
    exception.details = {"error": {"message": str(exception)}}

    assert public_rate_limit_info(exception).to_dict() == {
        "category": "unknown",
        "retry_after_seconds": 60,
    }


def test_daily_quota_without_retry_metadata_gets_long_bounded_cooldown():
    exception = RuntimeError("private details")
    exception.response = SimpleNamespace(status_code=429, headers={})
    exception.details = {
        "error": {
            "details": [{
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": "requests_per_day"}],
            }],
        },
    }

    assert public_rate_limit_info(exception).to_dict() == {
        "category": "daily",
        "retry_after_seconds": 300,
    }


def test_provider_context_close_drops_credentials_client_and_callback():
    updates = []

    class FakeClient:
        closed = False

        def close(self):
            self.closed = True

    context = ProviderContext(
        ProviderCredentials.create("gemini-private-key"),
        on_usage=updates.append,
    )
    client = FakeClient()
    context._gemini_client = client
    context.close()
    context.usage.record_gemini(
        model="model",
        stage="search",
        claim_index=0,
        attempt=1,
        succeeded=False,
    )
    assert context._credentials is None
    assert context._gemini_client is None
    assert client.closed is True
    assert updates == []
