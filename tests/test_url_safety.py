import ipaddress

import httpx
import pytest

import url_safety


def _public_dns(*_args, **_kwargs):
    return [ipaddress.ip_address("93.184.216.34")]


def _literal_aware_public_dns(hostname, *_args, **_kwargs):
    try:
        return [ipaddress.ip_address(hostname)]
    except ValueError:
        return _public_dns()


def test_redirect_resolution_validates_each_hop_and_returns_destination(monkeypatch):
    monkeypatch.setattr(url_safety, "_public_addresses", _literal_aware_public_dns)

    def respond(request):
        if request.url.host == "redirect.example":
            return httpx.Response(302, headers={"Location": "https://publisher.example/story"})
        return httpx.Response(200)

    resolved = url_safety.resolve_public_redirect(
        "https://redirect.example/token",
        transport=httpx.MockTransport(respond),
    )

    assert resolved == "https://publisher.example/story"


def test_redirect_resolution_rejects_private_destination(monkeypatch):
    monkeypatch.setattr(url_safety, "_public_addresses", _literal_aware_public_dns)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
    )

    with pytest.raises(url_safety.UnsafeUrlError, match="public"):
        url_safety.resolve_public_redirect(
            "https://redirect.example/token", transport=transport,
        )
