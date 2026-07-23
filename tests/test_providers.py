import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from providers import (
    InvalidProviderCredentials,
    ProviderConcurrencyGate,
    ProviderContext,
    ProviderCredentials,
    UsageLedger,
    merge_usage,
)


def test_credentials_validate_and_hide_secret_values():
    credentials = ProviderCredentials.create(
        "deepinfra-private-key", "tavily-private-key"
    )
    rendered = repr(credentials)
    assert "deepinfra-private-key" not in rendered
    assert "tavily-private-key" not in rendered

    with pytest.raises(InvalidProviderCredentials, match="DeepInfra"):
        ProviderCredentials.create("short", "tavily-private-key")
    with pytest.raises(InvalidProviderCredentials, match="Tavily"):
        ProviderCredentials.create("deepinfra-private-key", "bad key")


def test_usage_ledger_is_thread_safe_and_marks_unreported_usage_partial():
    ledger = UsageLedger()

    def record(index):
        ledger.record_deepinfra(
            model="model",
            stage="verification",
            claim_index=index,
            attempt=1,
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
            succeeded=True,
        )
        ledger.record_tavily(
            stage="search",
            claim_index=index,
            attempt=1,
            succeeded=True,
            search_depth="advanced",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(20)))

    snapshot = ledger.snapshot()
    assert snapshot["deepinfra"]["requests"] == 20
    assert snapshot["deepinfra"]["total_tokens"] == 240
    assert snapshot["tavily"]["successful_searches"] == 20
    assert snapshot["tavily"]["estimated_credits"] == 40
    assert snapshot["complete"] is True

    ledger.record_deepinfra(
        model="model",
        stage="verification",
        claim_index=20,
        attempt=1,
        succeeded=False,
    )
    assert ledger.snapshot()["complete"] is False


def test_merge_usage_adds_retry_totals_once():
    first = UsageLedger()
    second = UsageLedger()
    first.record_deepinfra(
        model="model",
        stage="extraction",
        claim_index=None,
        attempt=1,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        succeeded=True,
    )
    second.record_tavily(
        stage="search",
        claim_index=1,
        attempt=1,
        succeeded=True,
        search_depth="advanced",
    )

    merged = merge_usage(first.snapshot(), second.snapshot())
    assert merged["deepinfra"]["total_tokens"] == 15
    assert merged["tavily"]["estimated_credits"] == 2
    assert merged["complete"] is True


def test_provider_contexts_construct_clients_with_their_own_keys(monkeypatch):
    created_keys = []

    class FakeOpenAI:
        def __init__(self, *, api_key, **_kwargs):
            created_keys.append(api_key)

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    first = ProviderContext(
        ProviderCredentials.create("deepinfra-first-key", "tavily-first-key")
    )
    second = ProviderContext(
        ProviderCredentials.create("deepinfra-second-key", "tavily-second-key")
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda context: context.deepinfra_client(
                    base_url="https://api.deepinfra.com/v1/openai",
                    timeout=1,
                ),
                [first, second],
            )
        )

    assert set(created_keys) == {"deepinfra-first-key", "deepinfra-second-key"}


def test_provider_concurrency_gate_caps_process_wide_work():
    gate = ProviderConcurrencyGate(deepinfra_limit=2, tavily_limit=1)
    lock = threading.Lock()
    active = 0
    peak = 0

    def run():
        nonlocal active, peak
        with gate.deepinfra_slot(timeout=1):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: run(), range(12)))

    assert peak == 2


def test_provider_context_close_drops_credentials_clients_and_callback():
    updates = []

    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    context = ProviderContext(
        ProviderCredentials.create("deepinfra-private-key", "tavily-private-key"),
        on_usage=updates.append,
    )
    deepinfra = FakeClient()
    tavily = FakeClient()
    context._deepinfra_client = deepinfra
    context._tavily_client = tavily

    context.close()
    context.usage.record_tavily(
        stage="search",
        claim_index=0,
        attempt=1,
        succeeded=True,
        search_depth="advanced",
    )

    assert context._credentials is None
    assert context._deepinfra_client is None
    assert context._tavily_client is None
    assert deepinfra.closed is True
    assert tavily.closed is True
    assert updates == []
