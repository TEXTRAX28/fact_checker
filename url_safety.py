from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class UnsafeUrlError(ValueError):
    """Raised when a URL is malformed or resolves outside the public internet."""


def _public_addresses(hostname: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(hostname)]
    except ValueError:
        pass

    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrlError("URL host could not be resolved.") from exc

    addresses = []
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise UnsafeUrlError("URL host did not resolve to an address.")
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


def normalize_public_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL cannot be empty.")
    if any(char.isspace() for char in url):
        raise UnsafeUrlError("URL cannot contain whitespace.")

    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL is malformed.") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError("URL must use http or https.")
    if not parsed.netloc or not parsed.hostname:
        raise UnsafeUrlError("URL must include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs containing credentials are not allowed.")

    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError("URL host is invalid.") from exc
    if not hostname:
        raise UnsafeUrlError("URL must include a host.")

    effective_port = port or (443 if scheme == "https" else 80)
    addresses = _public_addresses(hostname, effective_port)
    if any(not _is_public_unicast(address) for address in addresses):
        raise UnsafeUrlError("URL host must resolve only to public addresses.")

    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_for_url if port is None else f"{host_for_url}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def resolve_public_redirect(
    url: str,
    *,
    timeout_seconds: float = 4.0,
    max_redirects: int = 4,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Resolve redirects without reading response bodies, validating every hop."""
    current = normalize_public_url(url)
    with httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_seconds),
        transport=transport,
        headers={"User-Agent": "fact-checker/1.0"},
    ) as client:
        for redirect_count in range(max_redirects + 1):
            with client.stream("HEAD", current) as response:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return current
                location = response.headers.get("location")
            if not location:
                raise UnsafeUrlError("Redirect response omitted its destination.")
            if redirect_count >= max_redirects:
                raise UnsafeUrlError("URL exceeded the redirect limit.")
            current = normalize_public_url(urljoin(current, location))
    raise UnsafeUrlError("URL could not be resolved.")
