import re

_LOW_QUALITY = (
    "facebook.com", "youtube.com", "youtu.be", "twitter.com", "x.com",
    "instagram.com", "tiktok.com", "reddit.com", "quora.com",
    "pinterest.com", "threads.net", "medium.com", "linkedin.com",
)

_PRIMARY_QUALITY = (
    ".gov", ".gov.uk", ".go.id", ".gc.ca", "canada.ca", "who.int",
    "worldbank.org", "un.org", "imf.org",
)

_REPUTABLE_NEWS = (
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com",
)

_RESEARCH_QUALITY = (
    "nature.com", "sciencedirect.com",
)

_INTERNATIONAL_ORGANIZATIONS = (
    "europa.eu", "oecd.org", "nato.int", "wto.org",
)

_GROUNDING_REDIRECT_DOMAINS = (
    "vertexaisearch.cloud.google.com",
)

# Not blocked, ranked below explicit high-quality sources but above unrecognized domains
_MEDIUM_QUALITY = (
    "wikipedia.org",
)

def _domain(url: str) -> str:
    from urllib.parse import urlparse
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname

def _domain_matches(netloc: str, domain: str) -> bool:
    # Matches the real host, not a substring anywhere in the URL - a query param
    # like "?ref=reuters.com" on a spam domain must NOT count as reuters.com.
    if domain.startswith("."):
        return netloc.endswith(domain)
    return netloc == domain or netloc.endswith("." + domain)


def _title_domain(title) -> str:
    if not isinstance(title, str):
        return ""
    value = title.strip().lower().removeprefix("www.").rstrip(".")
    if not value or " " in value or not re.fullmatch(r"[a-z0-9.-]+", value):
        return ""
    return value if "." in value else ""


def _source_domain(source: dict) -> str:
    return (
        _domain(source.get("canonical_url", ""))
        or str(source.get("publisher_domain") or "")
        or str(source.get("domain") or "")
        or _domain(source.get("url", ""))
    )


def _source_quality(domain: str) -> tuple[int, str]:
    labels = domain.split(".") if domain else []
    official_suffix = (
        domain.endswith((".gov", ".mil", ".int"))
        or bool(re.search(r"\.(?:gov|gob|go)\.[a-z]{2,3}$", domain))
    )
    academic_suffix = (
        domain.endswith(".edu")
        or bool(re.search(r"\.(?:ac|edu)\.[a-z]{2,3}$", domain))
    )
    if (
        official_suffix
        or any(_domain_matches(domain, item) for item in _PRIMARY_QUALITY)
        or any(
            _domain_matches(domain, item)
            for item in _INTERNATIONAL_ORGANIZATIONS
        )
    ):
        return 1, "official_or_primary"
    if academic_suffix and len(labels) >= 2:
        return 2, "academic_or_research"
    if any(_domain_matches(domain, item) for item in _REPUTABLE_NEWS):
        return 2, "reputable_news"
    if any(_domain_matches(domain, item) for item in _RESEARCH_QUALITY):
        return 2, "research"
    if any(_domain_matches(domain, item) for item in _MEDIUM_QUALITY):
        return 3, "secondary_reference"
    return 4, "other"

def _filter_sources(results: list[dict]) -> list[dict]:
    # Prefer high-quality domains among the grounded citations that remain after filtering.
    def rank(r):
        return _source_quality(_source_domain(r))[0]

    results.sort(key=rank)
    return results[:3]

def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _grounding_metadata(response):
    candidates = _field(response, "candidates", []) or []
    if not candidates:
        return None
    return _field(candidates[0], "grounding_metadata")


def _unique_search_query_count(metadata) -> int | None:
    queries = _field(metadata, "web_search_queries")
    if queries is None:
        return None
    return len({
        str(query).strip()
        for query in queries
        if isinstance(query, str) and query.strip()
    })


def _grounded_sources(response) -> list[dict]:
    metadata = _grounding_metadata(response)
    chunks = _field(metadata, "grounding_chunks", []) or []
    supports = _field(metadata, "grounding_supports", []) or []
    source_segments: dict[int, list[str]] = {}
    for support in supports:
        segment = _field(support, "segment")
        text = _field(segment, "text", "")
        if not isinstance(text, str) or not text.strip():
            continue
        for index in _field(support, "grounding_chunk_indices", []) or []:
            if isinstance(index, int):
                source_segments.setdefault(index, []).append(text.strip())

    results = []
    seen_urls = set()
    for index, chunk in enumerate(chunks):
        web = _field(chunk, "web")
        url = _field(web, "uri")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        title = _field(web, "title") or None
        actual_domain = _domain(url)
        if not actual_domain:
            continue
        title_domain = _title_domain(title)
        trusted_redirect = any(
            _domain_matches(actual_domain, domain)
            for domain in _GROUNDING_REDIRECT_DOMAINS
        )
        publisher_domain = title_domain if trusted_redirect and title_domain else actual_domain
        if url in seen_urls or any(
            _domain_matches(actual_domain, blocked)
            or (title_domain and _domain_matches(title_domain, blocked))
            for blocked in _LOW_QUALITY
        ):
            continue
        seen_urls.add(url)
        segments = list(dict.fromkeys(source_segments.get(index, [])))
        content = "\n".join(segments).strip()
        if not content:
            # A citation without a support-segment mapping is not evidence.
            continue
        results.append({
            "url": url,
            "provider_url": url if trusted_redirect else None,
            "canonical_url": None,
            "title": title,
            "domain": publisher_domain,
            "publisher_domain": publisher_domain,
            "provider_domain": actual_domain,
            "content": content,
            "is_full_content": False,
            "evidence_kind": "grounded_summary",
        })
    return results
