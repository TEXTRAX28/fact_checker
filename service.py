from __future__ import annotations

import copy
import ipaddress
import logging
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from fact_checker import (
    MIN_INPUT_NON_WHITESPACE,
    WHOLE_JOB_DEADLINE_SECONDS,
    FactCheckResult,
    WholeJobDeadlineExceeded,
    fact_check,
    retry_claim as retry_fact_claim,
)
from providers import ProviderContext

JINA_TIMEOUT_SECONDS = 12.0
MAX_ARTICLE_BYTES = 2 * 1024 * 1024
MIN_ARTICLE_NON_WHITESPACE = 200

logger = logging.getLogger(__name__)

_PUBLIC_ERROR_MESSAGES = {
    "article_timeout": "The article reader timed out.",
    "article_unreadable": "The article could not be read.",
    "invalid_pipeline_result": "The fact-check pipeline returned an invalid result.",
    "pipeline_error": "The fact-check pipeline failed.",
    "provider_error": "A provider request failed.",
    "provider_protocol_error": "A provider returned malformed output.",
    "provider_rate_limited": "A provider rate limit was reached.",
    "provider_timeout": "A provider request timed out.",
    "job_deadline_exceeded": "The fact-check exceeded its time limit.",
    "claim_no_evidence": "No sufficient evidence was found for this claim.",
}
_PUBLIC_ERROR_STAGES = {"article_fetch", "extraction", "pipeline", "search", "verification"}


class InvalidInputError(ValueError):
    pass


class ArticleTooLargeError(ValueError):
    pass


@dataclass
class CheckOutcome:
    status: str
    results: list[dict] = field(default_factory=list)
    claim_count: int = 0
    errors: list[dict] = field(default_factory=list)
    message: str = ""
    normalized_url: str | None = None
    metadata: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def completed_count(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["errors"] = [_sanitize_error(error) for error in self.errors]
        value["completed_count"] = self.completed_count
        return value


def _safe_callback(callback: Callable | None, value: Any, label: str) -> None:
    if callback is None:
        return
    try:
        callback(copy.deepcopy(value))
    except Exception as exc:
        logger.warning("%s callback failed (%s)", label, type(exc).__name__)


def _callback_wrapper(callback: Callable | None, label: str) -> Callable | None:
    if callback is None:
        return None

    def wrapped(value: Any) -> None:
        _safe_callback(callback, value, label)

    return wrapped


def _cancelled(cancel_event) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _resolve_deadline(deadline: float | None) -> float:
    return deadline if deadline is not None else time.monotonic() + WHOLE_JOB_DEADLINE_SECONDS


def _remaining_timeout(limit: float, deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WholeJobDeadlineExceeded("The fact-check exceeded its time limit.")
    return min(limit, remaining)


def _non_whitespace_length(value: str) -> int:
    return sum(not char.isspace() for char in value)


def _normalize_text(text: str) -> str:
    if not isinstance(text, str):
        raise InvalidInputError("Text must be a string.")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if _non_whitespace_length(normalized) < MIN_INPUT_NON_WHITESPACE:
        raise InvalidInputError(
            f"Text must contain at least {MIN_INPUT_NON_WHITESPACE} non-whitespace characters."
        )
    return normalized


def _sanitize_error(error: Any) -> dict[str, Any]:
    if not isinstance(error, dict):
        return {"stage": "pipeline", "code": "pipeline_error",
                "message": _PUBLIC_ERROR_MESSAGES["pipeline_error"]}
    code = error.get("code")
    if code not in _PUBLIC_ERROR_MESSAGES:
        code = "pipeline_error"
    stage = error.get("stage")
    if stage not in _PUBLIC_ERROR_STAGES:
        stage = "pipeline"
    sanitized = {
        "stage": stage,
        "code": code,
        "message": _PUBLIC_ERROR_MESSAGES[code],
    }
    claim_index = error.get("claim_index")
    if isinstance(claim_index, int) and claim_index >= 0:
        sanitized["claim_index"] = claim_index
    return sanitized


def _error(stage: str, code: str, *, claim_index: int | None = None) -> dict[str, Any]:
    value = {"stage": stage, "code": code, "message": _PUBLIC_ERROR_MESSAGES[code]}
    if claim_index is not None:
        value["claim_index"] = claim_index
    return value


def _is_rate_limited(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = (getattr(response, "status_code", None)
                   or getattr(exc, "status_code", None))
    return (status_code == 429
            or type(exc).__name__ in {
                "RateLimitError", "UsageLimitExceededError", "TavilyKeylessLimitError"
            })


def _is_timeout(exc: Exception) -> bool:
    return (isinstance(exc, (TimeoutError, httpx.TimeoutException))
            or "timeout" in type(exc).__name__.lower())


def _public_addresses(hostname: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(hostname)]
    except ValueError:
        pass

    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise InvalidInputError("URL host could not be resolved.") from exc

    addresses = []
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise InvalidInputError("URL host did not resolve to an address.")
    return addresses


def _is_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or getattr(address, "is_site_local", False)
        or not address.is_global
    )


def _normalize_public_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise InvalidInputError("URL cannot be empty.")
    if any(char.isspace() for char in url):
        raise InvalidInputError("URL cannot contain whitespace.")

    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise InvalidInputError("URL is malformed.") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise InvalidInputError("URL must use http or https.")
    if not parsed.netloc or not parsed.hostname:
        raise InvalidInputError("URL must include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidInputError("URLs containing credentials are not allowed.")

    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidInputError("URL host is invalid.") from exc
    if not hostname:
        raise InvalidInputError("URL must include a host.")

    effective_port = port or (443 if scheme == "https" else 80)
    addresses = _public_addresses(hostname, effective_port)
    if any(not _is_public_unicast(address) for address in addresses):
        raise InvalidInputError("URL host must resolve only to public addresses.")

    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_for_url if port is None else f"{host_for_url}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _fetch_jina_article(url: str, *, deadline: float | None = None) -> str:
    """Fetch a direct Jina response without following local HTTP redirects.

    Jina retrieves the original site remotely, so redirects followed by Jina while
    resolving the target URL are opaque to this service.
    """
    headers = {
        "Accept": "text/plain",
        "User-Agent": "fact-checker/1.0",
        "X-Remove-Selector": "nav, footer, header, aside",
        "X-Retain-Images": "none",
    }
    deadline = _resolve_deadline(deadline)
    effective_timeout = _remaining_timeout(JINA_TIMEOUT_SECONDS, deadline)
    chunks = []
    size = 0
    started = time.monotonic()
    with httpx.stream(
        "GET",
        f"https://r.jina.ai/{url}",
        headers=headers,
        timeout=httpx.Timeout(effective_timeout),
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                "Jina Reader returned a non-200 response.",
                request=response.request,
                response=response,
            )
        if (time.monotonic() - started > effective_timeout
                or time.monotonic() >= deadline):
            raise httpx.ReadTimeout("Jina response exceeded the article deadline.")
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > MAX_ARTICLE_BYTES:
                raise ArticleTooLargeError("Jina response exceeded the article size limit.")

        for chunk in response.iter_bytes():
            if (time.monotonic() - started > effective_timeout
                    or time.monotonic() >= deadline):
                raise httpx.ReadTimeout("Jina response exceeded the article deadline.")
            size += len(chunk)
            if size > MAX_ARTICLE_BYTES:
                raise ArticleTooLargeError("Jina response exceeded the article size limit.")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _clean_article(raw: str) -> str:
    if "Markdown Content:" in raw:
        raw = raw.split("Markdown Content:", 1)[1]
    content = re.sub(r"!\[[\s\S]*?\]\([^)]*\)", "", raw)
    content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
    content = re.sub(r"https?://\S+", "", content)
    content = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE)
    content = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", content)

    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", content):
        stripped = paragraph.strip()
        if len(stripped) > 40:
            paragraphs.append(stripped)
    return "\n".join(f"[SPEAKER_A] {paragraph}" for paragraph in paragraphs[:60])


def _outcome_from_pipeline(pipeline_result, *, metadata=None,
                           normalized_url: str | None = None) -> CheckOutcome:
    if isinstance(pipeline_result, FactCheckResult):
        status = pipeline_result.status
        claim_count = pipeline_result.claim_count
        errors = [_sanitize_error(error) for error in pipeline_result.errors]
    else:
        return CheckOutcome(
            status="failed",
            errors=[_error("pipeline", "invalid_pipeline_result")],
            message="Fact-check pipeline returned an invalid result.",
            metadata=metadata,
            normalized_url=normalized_url,
        )

    messages = {
        "completed": "Fact-check completed.",
        "no_claims": "No checkable factual claims were found.",
        "no_evidence": "Claims were found, but no sufficient evidence was available.",
        "partial": "Fact-check completed with partial results.",
        "invalid_input": "The input is invalid.",
        "timeout": "A provider timed out.",
        "rate_limited": "A provider rate limit was reached.",
        "cancelled": "Fact-check was cancelled.",
        "failed": "Fact-check failed.",
    }
    if any(error.get("code") == "job_deadline_exceeded" for error in errors):
        message = "The fact-check exceeded its time limit."
    else:
        message = messages.get(status, "Fact-check finished.")
    return CheckOutcome(
        status=status,
        results=list(pipeline_result),
        claim_count=claim_count,
        errors=errors,
        message=message,
        metadata=metadata,
        normalized_url=normalized_url,
        usage=copy.deepcopy(getattr(pipeline_result, "usage", {})),
    )


def _run_pipeline(text: str, *, metadata=None, normalized_url=None,
                  on_progress=None, on_result=None, cancel_event=None,
                  on_claims=None, on_evidence=None,
                  deadline: float | None = None,
                  provider_context: ProviderContext | None = None) -> CheckOutcome:
    if _cancelled(cancel_event):
        return CheckOutcome(status="cancelled", message="Fact-check was cancelled.",
                            metadata=metadata, normalized_url=normalized_url)
    try:
        pipeline_result = fact_check(
            text,
            on_progress=_callback_wrapper(on_progress, "Progress"),
            on_result=_callback_wrapper(on_result, "Result"),
            on_claims=_callback_wrapper(on_claims, "Claims"),
            on_evidence=_callback_wrapper(on_evidence, "Evidence"),
            cancel_event=cancel_event,
            deadline=deadline,
            provider_context=provider_context,
        )
    except Exception as exc:
        logger.warning("Fact-check pipeline failed (%s)", type(exc).__name__)
        if _is_rate_limited(exc):
            status, code = "rate_limited", "provider_rate_limited"
        elif _is_timeout(exc):
            status, code = "timeout", "provider_timeout"
        else:
            status, code = "failed", "pipeline_error"
        return CheckOutcome(
            status=status,
            errors=[_error("pipeline", code)],
            message={
                "rate_limited": "A provider rate limit was reached.",
                "timeout": "A provider timed out.",
            }.get(status, "Fact-check failed."),
            metadata=metadata,
            normalized_url=normalized_url,
        )
    return _outcome_from_pipeline(
        pipeline_result, metadata=metadata, normalized_url=normalized_url
    )


def check_text(text: str, *, metadata: dict[str, Any] | None = None,
               on_progress=None, on_result=None, cancel_event=None,
               on_claims=None, on_evidence=None,
               deadline: float | None = None,
               provider_context: ProviderContext | None = None) -> CheckOutcome:
    deadline = _resolve_deadline(deadline)
    progress = _callback_wrapper(on_progress, "Progress")
    _safe_callback(progress, {"stage": "validating", "state": "started"}, "Progress")
    if _cancelled(cancel_event):
        return CheckOutcome(status="cancelled", message="Fact-check was cancelled.",
                            metadata=metadata)
    try:
        normalized = _normalize_text(text)
    except InvalidInputError as exc:
        return CheckOutcome(status="invalid_input", message=str(exc), metadata=metadata)
    _safe_callback(progress, {"stage": "validating", "state": "completed"}, "Progress")
    return _run_pipeline(
        normalized,
        metadata=metadata,
        on_progress=progress,
        on_result=on_result,
        on_claims=on_claims,
        on_evidence=on_evidence,
        cancel_event=cancel_event,
        deadline=deadline,
        provider_context=provider_context,
    )


def check_url(url: str, *, on_progress=None, on_result=None, cancel_event=None,
              on_claims=None, on_evidence=None,
              deadline: float | None = None,
              provider_context: ProviderContext | None = None) -> CheckOutcome:
    deadline = _resolve_deadline(deadline)
    progress = _callback_wrapper(on_progress, "Progress")
    _safe_callback(progress, {"stage": "validating_url", "state": "started"}, "Progress")
    if _cancelled(cancel_event):
        return CheckOutcome(status="cancelled", message="Fact-check was cancelled.")
    try:
        normalized_url = _normalize_public_url(url)
    except InvalidInputError as exc:
        return CheckOutcome(status="invalid_input", message=str(exc))
    _safe_callback(
        progress,
        {"stage": "validating_url", "state": "completed", "url": normalized_url},
        "Progress",
    )

    if _cancelled(cancel_event):
        return CheckOutcome(status="cancelled", message="Fact-check was cancelled.",
                            normalized_url=normalized_url)
    _safe_callback(progress, {"stage": "fetching_article", "state": "started"}, "Progress")
    try:
        raw = _fetch_jina_article(normalized_url, deadline=deadline)
    except WholeJobDeadlineExceeded:
        return CheckOutcome(
            status="timeout",
            errors=[_error("pipeline", "job_deadline_exceeded")],
            message="The fact-check exceeded its time limit.",
            normalized_url=normalized_url,
        )
    except httpx.TimeoutException:
        logger.warning("Jina Reader timed out")
        return CheckOutcome(
            status="timeout",
            errors=[_error("article_fetch", "article_timeout")],
            message="Jina Reader timed out.",
            normalized_url=normalized_url,
        )
    except Exception as exc:
        logger.warning("Jina Reader failed (%s)", type(exc).__name__)
        return CheckOutcome(
            status="unreadable",
            errors=[_error("article_fetch", "article_unreadable")],
            message="The full article could not be read. Paste the page text instead.",
            normalized_url=normalized_url,
        )

    if _cancelled(cancel_event):
        return CheckOutcome(status="cancelled", message="Fact-check was cancelled.",
                            normalized_url=normalized_url)
    article = _clean_article(raw)
    readable_text = article.replace("[SPEAKER_A]", "")
    if _non_whitespace_length(readable_text) < MIN_ARTICLE_NON_WHITESPACE:
        return CheckOutcome(
            status="unreadable",
            message="The full article could not be read. Paste the page text instead.",
            normalized_url=normalized_url,
        )
    _safe_callback(
        progress,
        {"stage": "fetching_article", "state": "completed", "url": normalized_url},
        "Progress",
    )
    return _run_pipeline(
        article,
        metadata={"source": "url", "url": normalized_url},
        normalized_url=normalized_url,
        on_progress=progress,
        on_result=on_result,
        on_claims=on_claims,
        on_evidence=on_evidence,
        cancel_event=cancel_event,
        deadline=deadline,
        provider_context=provider_context,
    )


def retry_claim(claim: dict, claim_index: int, *, evidence: dict | None = None,
                on_progress=None, on_evidence=None, cancel_event=None,
                deadline: float | None = None,
                provider_context: ProviderContext | None = None) -> CheckOutcome:
    pipeline_result = retry_fact_claim(
        claim,
        claim_index,
        evidence=evidence,
        on_progress=_callback_wrapper(on_progress, "Progress"),
        on_evidence=_callback_wrapper(on_evidence, "Evidence"),
        cancel_event=cancel_event,
        deadline=deadline,
        provider_context=provider_context,
    )
    return _outcome_from_pipeline(pipeline_result)
