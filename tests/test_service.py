import socket
import threading

import httpx
import pytest

import service
from fact_checker import FactCheckResult


def public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def test_short_text_counts_non_whitespace_and_skips_pipeline(monkeypatch):
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(service, "fact_check", fail_if_called)

    outcome = service.check_text("a b c d e f g h i")

    assert outcome.status == "invalid_input"
    assert called is False


def test_ten_character_text_does_not_use_article_paragraph_filter(monkeypatch):
    captured = {}

    def fake_fact_check(text, **_kwargs):
        captured["text"] = text
        return FactCheckResult([], status="no_claims")

    monkeypatch.setattr(service, "fact_check", fake_fact_check)

    outcome = service.check_text("a b c d e f g h i j")

    assert outcome.status == "no_claims"
    assert captured["text"] == "a b c d e f g h i j"


def test_no_claims_is_not_reported_as_failure(monkeypatch):
    monkeypatch.setattr(
        service,
        "fact_check",
        lambda *_args, **_kwargs: FactCheckResult([], status="no_claims", claim_count=0),
    )

    outcome = service.check_text("This sentence is only an opinion.")

    assert outcome.status == "no_claims"
    assert outcome.results == []
    assert outcome.errors == []


def test_unreadable_jina_content_never_runs_fact_checker(monkeypatch):
    monkeypatch.setattr(service.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(service, "_fetch_jina_article", lambda _url: "Access denied")
    monkeypatch.setattr(
        service, "fact_check", lambda *_args, **_kwargs: pytest.fail("pipeline was called")
    )

    outcome = service.check_url("https://example.com/story")

    assert outcome.status == "unreadable"
    assert "Paste" in outcome.message


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "https://user:password@example.com/story",
        "http://127.0.0.1/story",
        "http://[::1]/story",
        "http://169.254.1.1/story",
        "http://192.0.2.10/story",
    ],
)
def test_url_validation_rejects_unsafe_urls(url):
    outcome = service.check_url(url)

    assert outcome.status == "invalid_input"


@pytest.mark.parametrize("url", ["http://224.0.0.1/story", "http://[ff02::1]/story"])
def test_url_validation_rejects_multicast_addresses(url):
    outcome = service.check_url(url)

    assert outcome.status == "invalid_input"


def test_url_validation_rejects_hostname_with_private_dns(monkeypatch):
    monkeypatch.setattr(
        service.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))
        ],
    )

    outcome = service.check_url("https://internal.example/story")

    assert outcome.status == "invalid_input"


def test_url_fragment_is_removed_before_jina_fetch(monkeypatch):
    fetched = []
    article = "\n\n".join(["A factual article paragraph with enough readable content " * 2] * 4)
    monkeypatch.setattr(service.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(
        service,
        "_fetch_jina_article",
        lambda url, **_kwargs: fetched.append(url) or article,
    )
    monkeypatch.setattr(
        service,
        "fact_check",
        lambda *_args, **_kwargs: FactCheckResult([], status="no_claims"),
    )

    outcome = service.check_url("HTTPS://Example.COM/story?q=1#section")

    assert outcome.status == "no_claims"
    assert fetched == ["https://example.com/story?q=1"]
    assert outcome.normalized_url == fetched[0]


def test_callback_exceptions_do_not_change_outcome(monkeypatch):
    verdict = {"claim": "Ten people attended.", "verdict": "TRUE"}

    def fake_fact_check(_text, *, on_progress, on_result, **_kwargs):
        on_progress({"stage": "searching"})
        on_result(verdict)
        return FactCheckResult([verdict], status="completed", claim_count=1)

    def broken_callback(_value):
        raise RuntimeError("observer failed")

    monkeypatch.setattr(service, "fact_check", fake_fact_check)

    outcome = service.check_text(
        "Ten people attended the meeting.",
        on_progress=broken_callback,
        on_result=broken_callback,
    )

    assert outcome.status == "completed"
    assert outcome.results == [verdict]


def test_callback_mutation_cannot_change_service_result(monkeypatch):
    verdict = {
        "claim": "Ten people attended.",
        "verdict": "TRUE",
        "sources": [{"url": "https://example.com"}],
    }

    def fake_fact_check(_text, *, on_result, **_kwargs):
        on_result(verdict)
        return FactCheckResult([verdict], status="completed", claim_count=1)

    def mutate_observation(value):
        value["verdict"] = "FALSE"
        value["sources"][0]["url"] = "https://attacker.invalid"

    monkeypatch.setattr(service, "fact_check", fake_fact_check)

    outcome = service.check_text(
        "Ten people attended the meeting.", on_result=mutate_observation
    )

    assert outcome.results[0]["verdict"] == "TRUE"
    assert outcome.results[0]["sources"][0]["url"] == "https://example.com"


def test_pre_cancelled_request_does_not_run_pipeline(monkeypatch):
    event = threading.Event()
    event.set()
    monkeypatch.setattr(
        service, "fact_check", lambda *_args, **_kwargs: pytest.fail("pipeline was called")
    )

    outcome = service.check_text("Ten people attended.", cancel_event=event)

    assert outcome.status == "cancelled"


def test_jina_timeout_has_distinct_status(monkeypatch):
    monkeypatch.setattr(service.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(
        service,
        "_fetch_jina_article",
        lambda _url, **_kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
    )

    outcome = service.check_url("https://example.com/story")

    assert outcome.status == "timeout"


def test_whole_job_deadline_includes_article_fetch(monkeypatch):
    monkeypatch.setattr(service.socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(
        service,
        "_fetch_jina_article",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            service.WholeJobDeadlineExceeded("expired")
        ),
    )

    outcome = service.check_url(
        "https://example.com/story", deadline=service.time.monotonic() - 1
    )

    assert outcome.status == "timeout"
    assert outcome.errors[0]["code"] == "job_deadline_exceeded"
    assert outcome.message == "The fact-check exceeded its time limit."


def test_jina_response_size_is_bounded(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-length": str(service.MAX_ARTICLE_BYTES + 1)}

        def iter_bytes(self):
            yield b"not reached"

    class Stream:
        def __enter__(self):
            return Response()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(service.httpx, "stream", lambda *_args, **_kwargs: Stream())

    with pytest.raises(service.ArticleTooLargeError):
        service._fetch_jina_article("https://example.com/story")


def test_jina_redirect_is_not_followed_or_accepted(monkeypatch):
    captured = {}

    class Response:
        status_code = 302
        headers = {"location": "https://example.com/elsewhere"}
        request = httpx.Request("GET", "https://r.jina.ai/https://example.com/story")

        def iter_bytes(self):
            pytest.fail("redirect body must not be read")

    class Stream:
        def __enter__(self):
            return Response()

        def __exit__(self, *_args):
            return False

    def fake_stream(*_args, **kwargs):
        captured.update(kwargs)
        return Stream()

    monkeypatch.setattr(service.httpx, "stream", fake_stream)

    with pytest.raises(httpx.HTTPStatusError):
        service._fetch_jina_article("https://example.com/story")

    assert captured["follow_redirects"] is False


def test_legacy_list_pipeline_result_is_rejected(monkeypatch):
    monkeypatch.setattr(service, "fact_check", lambda *_args, **_kwargs: [])

    outcome = service.check_text("A sufficiently long factual statement.")

    assert outcome.status == "failed"
    assert outcome.errors[0]["code"] == "invalid_pipeline_result"


def test_service_errors_are_stable_and_do_not_leak_provider_details(monkeypatch):
    secret = "upstream key abc123 was rejected"
    monkeypatch.setattr(
        service,
        "fact_check",
        lambda *_args, **_kwargs: FactCheckResult(
            [],
            status="rate_limited",
            claim_count=1,
            errors=[{
                "stage": "search",
                "code": "provider_rate_limited",
                "message": secret,
                "type": "UsageLimitExceededError",
            }],
        ),
    )

    outcome = service.check_text("A sufficiently long factual statement.")
    serialized = outcome.to_dict()

    assert outcome.status == "rate_limited"
    assert serialized["errors"] == [{
        "stage": "search",
        "code": "provider_rate_limited",
        "message": "A provider rate limit was reached.",
        "quota_category": "unknown",
    }]
    assert secret not in str(serialized)
