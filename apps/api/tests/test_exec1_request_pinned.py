"""EXEC-1 regression — server-side fetches of user URLs must be IP-pinned.

request_pinned() validates the URL, then connects to the validated IP literal
(no second DNS lookup at connect) while preserving the original Host + SNI. This
closes the DNS-rebind TOCTOU that validate_external_url alone cannot. Before the
fix the helper existed but was never called; webhook delivery, BYOK, and the
AssemblyAI HEAD now route through it.

Run: uv run pytest tests/test_exec1_request_pinned.py -v
"""
import pytest
from fastapi import HTTPException

from core import url_validation
from core.url_validation import request_pinned


class _FakeReq:
    def __init__(self):
        self.extensions = {}


class _FakeClient:
    """Records the URL the request was actually built against."""
    def __init__(self):
        self.built_url = None
        self.built_headers = None
        self.req = _FakeReq()

    def build_request(self, method, url, headers=None, **kw):
        self.built_url = url
        self.built_headers = headers or {}
        self.method = method
        return self.req

    async def send(self, request):
        return "SENT"

    async def request(self, method, url, headers=None, **kw):  # fallback path
        self.built_url = url
        return "FALLBACK"


async def test_pins_to_validated_ip_and_preserves_host(monkeypatch):
    # Stub resolution so we don't hit the network: host → a fixed public IP.
    monkeypatch.setattr(url_validation, "validate_external_url",
                        lambda url, field_name="url": ["93.184.216.34"])
    client = _FakeClient()
    out = await request_pinned(client, "POST", "https://example.com/hook", content=b"x")
    assert out == "SENT"
    # Connected to the IP literal, NOT the hostname (no connect-time re-resolution).
    assert "93.184.216.34" in client.built_url
    assert "example.com" not in client.built_url
    # Host header + SNI preserved as the real hostname.
    assert client.built_headers.get("Host") == "example.com"
    assert client.req.extensions.get("sni_hostname") == "example.com"


async def test_internal_target_is_refused():
    # A real internal URL must raise (validate_external_url fails closed) — no
    # request is built/sent.
    with pytest.raises(HTTPException) as exc:
        await request_pinned(_FakeClient(), "POST",
                             "https://169.254.169.254/latest/meta-data", content=b"x")
    assert exc.value.status_code == 422


async def test_post_pinned_delegates(monkeypatch):
    monkeypatch.setattr(url_validation, "validate_external_url",
                        lambda url, field_name="url": ["93.184.216.34"])
    client = _FakeClient()
    out = await url_validation.post_pinned(client, "https://example.com/x", content=b"y")
    assert out == "SENT"
    assert client.method == "POST"
    assert "93.184.216.34" in client.built_url
