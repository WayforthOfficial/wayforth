"""tests/test_body_size_limit.py — BodySizeLimitMiddleware unit tests.

Pure-unit: the middleware is exercised directly against a stub ASGI app, so no
network, no DB, no live deployment required. Covers:

  - Content-Length above default limit → 413 (body never reaches the app)
  - Content-Length above large limit on /run and /execute → 413
  - Content-Length within limit → passes through
  - Large body on /run/intents (under 4 MB) → passes (4 MB applies to /run/*)
  - Small body on / (under 1 MB) → passes
  - GET / HEAD / OPTIONS bypass the size check entirely
  - Stream-byte counter truncates an oversized chunked-encoded body
  - Limit selector resolves prefixes correctly
"""
from __future__ import annotations

import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import BodySizeLimitMiddleware


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _stub_app(scope, receive, send):
    """Echo ASGI app that drains the body and returns its total length as 200."""
    total = 0
    while True:
        msg = await receive()
        if msg["type"] == "http.request":
            total += len(msg.get("body", b""))
            if not msg.get("more_body"):
                break
    body = json.dumps({"received": total}).encode()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def _scope(method: str, path: str, content_length: int | None = None) -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {
        "type": "http",
        "method": method.upper(),
        "path": path,
        "headers": headers,
        "raw_path": path.encode(),
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }


def _make_mw():
    return BodySizeLimitMiddleware(
        _stub_app,
        default_limit=1024,           # 1 KB for tests (mirrors the 1 MB:4 MB ratio)
        large_limit=4096,             # 4 KB
        large_path_prefixes=("/run", "/execute"),
    )


class _Sink:
    """Capture send() calls so tests can assert on the response."""
    def __init__(self):
        self.start: dict | None = None
        self.body_chunks: list[bytes] = []

    async def __call__(self, msg):
        if msg["type"] == "http.response.start":
            self.start = msg
        elif msg["type"] == "http.response.body":
            self.body_chunks.append(msg.get("body", b""))


def _make_receiver(body: bytes, chunk_size: int | None = None):
    """Return an async ASGI receive() that emits `body` in one or more chunks."""
    chunks: list[bytes]
    if chunk_size is None or chunk_size >= len(body) or chunk_size <= 0:
        chunks = [body]
    else:
        chunks = [body[i:i + chunk_size] for i in range(0, len(body), chunk_size)]
    state = {"i": 0}

    async def receive():
        i = state["i"]
        if i < len(chunks):
            state["i"] += 1
            return {
                "type": "http.request",
                "body": chunks[i],
                "more_body": i < len(chunks) - 1,
            }
        return {"type": "http.disconnect"}
    return receive


# ─────────────────────────────────────────────────────────────────────────────
# Limit selector
# ─────────────────────────────────────────────────────────────────────────────


def test_limit_default():
    assert _make_mw()._limit_for("/auth/register") == 1024


@pytest.mark.parametrize("path", ["/run", "/run/intents", "/execute", "/execute/batch"])
def test_limit_large_paths(path):
    assert _make_mw()._limit_for(path) == 4096


def test_limit_prefix_match_not_substring():
    # `/runtime` is NOT under the /run prefix — only exact match or `/run/...`.
    assert _make_mw()._limit_for("/runtime") == 1024


# ─────────────────────────────────────────────────────────────────────────────
# Content-Length enforcement
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_oversized_content_length_returns_413():
    sink = _Sink()
    recv = _make_receiver(b"x" * 5000)
    await _make_mw()(_scope("POST", "/auth/register", content_length=5000), recv, sink)
    assert sink.start["status"] == 413
    body = json.loads(b"".join(sink.body_chunks))
    assert body["error"] == "payload_too_large"
    assert body["limit_bytes"] == 1024
    assert body["size_bytes"] == 5000


@pytest.mark.asyncio
async def test_content_length_within_default_limit_passes():
    sink = _Sink()
    payload = b"y" * 500
    recv = _make_receiver(payload)
    await _make_mw()(_scope("POST", "/auth/register", content_length=500), recv, sink)
    assert sink.start["status"] == 200
    assert json.loads(b"".join(sink.body_chunks))["received"] == 500


@pytest.mark.asyncio
async def test_large_path_accepts_above_default_limit():
    """A 3 KB body must succeed on /run (4 KB limit) but would fail on / (1 KB)."""
    sink = _Sink()
    payload = b"z" * 3000
    recv = _make_receiver(payload)
    await _make_mw()(_scope("POST", "/run", content_length=3000), recv, sink)
    assert sink.start["status"] == 200


@pytest.mark.asyncio
async def test_large_path_still_rejects_above_large_limit():
    sink = _Sink()
    recv = _make_receiver(b"q" * 9000)
    await _make_mw()(_scope("POST", "/execute/batch", content_length=9000), recv, sink)
    assert sink.start["status"] == 413
    assert json.loads(b"".join(sink.body_chunks))["limit_bytes"] == 4096


# ─────────────────────────────────────────────────────────────────────────────
# Method bypass
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.asyncio
async def test_safe_methods_bypass_size_check(method):
    """GET/HEAD/OPTIONS rarely carry bodies and proxies often omit CL anyway —
    skip the check so we don't 413 a request that wouldn't have a payload."""
    sink = _Sink()
    recv = _make_receiver(b"")
    # Send a CL well above the limit; should still pass because of method bypass.
    await _make_mw()(_scope(method, "/auth/register", content_length=999_999), recv, sink)
    assert sink.start["status"] == 200


# ─────────────────────────────────────────────────────────────────────────────
# Stream-byte counter (chunked / missing Content-Length)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chunked_request_under_limit_passes():
    sink = _Sink()
    payload = b"a" * 800
    recv = _make_receiver(payload, chunk_size=200)
    # No content-length header → exercises Layer 2 (byte counter).
    await _make_mw()(_scope("POST", "/auth/register"), recv, sink)
    assert sink.start["status"] == 200
    assert json.loads(b"".join(sink.body_chunks))["received"] == 800


@pytest.mark.asyncio
async def test_chunked_request_over_limit_is_truncated():
    """When CL is absent and stream exceeds the limit, the body delivered to
    the app is truncated to (at most) `limit` bytes — the app then handles
    the malformed/short payload with its own validation."""
    sink = _Sink()
    payload = b"b" * 2500  # 2.5x the 1024-byte limit
    recv = _make_receiver(payload, chunk_size=512)
    await _make_mw()(_scope("POST", "/auth/register"), recv, sink)
    assert sink.start["status"] == 200
    received = json.loads(b"".join(sink.body_chunks))["received"]
    # Should NOT have received the full 2500 bytes.
    assert received <= 1024, f"middleware allowed {received} bytes through, limit was 1024"
