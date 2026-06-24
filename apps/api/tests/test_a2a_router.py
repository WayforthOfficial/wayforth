"""test_a2a_router.py — A2A serving + dispatch surface (PR A, target 0.9.2).

No run pipeline / billing path here. Verifies the well-known card is served +
self-verifies against the served JWKS, and that JSON-RPC dispatch accepts both
wire dialects inbound, emits v0.3.0 errors, and is explicitly unimplemented (not
faked) for every method in PR A.
"""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.auth as auth
from core.a2a.sign import verify_card
from core.db import get_db
from routers.a2a import router as a2a_router


class FakeKeyDB:
    """In-memory model of the a2a_signing_keys queries (provision/get/jwks)."""
    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0

    async def fetchrow(self, q, *a):
        if "status = 'active'" in q:
            return next((r for r in self.rows if r["status"] == "active"), None)
        return None

    async def fetch(self, q, *a):
        if "IN ('active', 'retiring')" in q:
            sel = [r for r in self.rows if r["status"] in ("active", "retiring")]
            return sorted(sel, key=lambda r: r["created_at"], reverse=True)
        return []

    async def execute(self, q, *a):
        if "INSERT INTO a2a_signing_keys" in q:
            kid, alg, crv, pj, ct, ver = a[:6]
            self._seq += 1
            self.rows.append({
                "kid": kid, "alg": alg, "crv": crv, "public_jwk": json.loads(pj),
                "encrypted_private_key": ct, "key_version": ver,
                "status": "active", "created_at": self._seq,
            })


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(auth, "KEY_VERSIONS", {}, raising=False)
    monkeypatch.setattr("routers.a2a._agent_version", lambda: "0.9.2-test")

    fake = FakeKeyDB()
    app = FastAPI()
    app.include_router(a2a_router)
    app.dependency_overrides[get_db] = lambda: fake
    return TestClient(app)


# ── well-known ────────────────────────────────────────────────────────────────

def test_card_is_v030_and_signed(client):
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["protocolVersion"] == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["url"].endswith("/a2a")
    assert card["signatures"] and card["signatures"][0]["protected"]


def test_card_verifies_against_served_jwks(client):
    # End-to-end trust chain: fetch card + JWKS from the gateway, verify signature.
    card = client.get("/.well-known/agent-card.json").json()
    jwks = client.get("/.well-known/jwks.json").json()
    assert jwks["keys"] and "kid" in jwks["keys"][0]
    assert verify_card(card, jwks) is True


def test_jwks_is_public_only(client):
    jwks = client.get("/.well-known/jwks.json").json()
    for k in jwks["keys"]:
        assert "encrypted_private_key" not in k and "d" not in k
        assert k["kty"] == "EC" and k["use"] == "sig"


# ── JSON-RPC dispatch ─────────────────────────────────────────────────────────

def _rpc(client, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/a2a", json=body).json()


def test_methods_registered_but_unimplemented_v030(client):
    # A still-unimplemented v0.3.0 method (message/send + message/stream now route
    # to the run money path) → spec-compliant UNSUPPORTED_OPERATION, not faked.
    resp = _rpc(client, "tasks/get", {"id": "task-1"})
    assert resp["jsonrpc"] == "2.0" and resp["id"] == 1
    assert "result" not in resp
    assert resp["error"]["code"] == -32004   # UNSUPPORTED_OPERATION


def test_inbound_accepts_v1_method_spelling(client):
    # v1.0 PascalCase normalizes to the same internal method (Postel at the router).
    # GetTask is still unimplemented, so reaching dispatch yields UNSUPPORTED, not
    # METHOD_NOT_FOUND — proving the inbound normalization fired.
    resp = _rpc(client, "GetTask", {"id": "task-1"})
    assert resp["error"]["code"] == -32004   # reached dispatch, not METHOD_NOT_FOUND


def test_unknown_method_is_method_not_found(client):
    resp = _rpc(client, "tasks/teleport")
    assert resp["error"]["code"] == -32601

    resp2 = _rpc(client, "Teleport")
    assert resp2["error"]["code"] == -32601


def test_bad_envelope_is_invalid_request(client):
    resp = client.post("/a2a", json={"method": "message/send"}).json()  # no jsonrpc
    assert resp["error"]["code"] == -32600

    resp2 = client.post("/a2a", json={"jsonrpc": "1.0", "method": "message/send"}).json()
    assert resp2["error"]["code"] == -32600


def test_malformed_json_is_parse_error(client):
    resp = client.post("/a2a", content=b"{not json", headers={"content-type": "application/json"})
    assert resp.json()["error"]["code"] == -32700


def test_id_echoed_on_error(client):
    resp = _rpc(client, "message/send", rid="abc-123")
    assert resp["id"] == "abc-123"


def test_streaming_capability_matches_implementation(client):
    # The card must never advertise streaming while the stream method is
    # UNSUPPORTED. Guards the PR-A gap so it can't outlive PR B silently: when
    # streaming lands, the method stops returning UNSUPPORTED and this still holds;
    # if someone flips the flag without implementing, this fails.
    card = client.get("/.well-known/agent-card.json").json()
    advertises_streaming = card["capabilities"].get("streaming", False)
    resp = _rpc(client, "message/stream", {"message": {"role": "user", "parts": []}})
    method_unsupported = resp.get("error", {}).get("code") == -32004
    assert not (advertises_streaming and method_unsupported), (
        "Agent Card advertises streaming:true but the stream method is UNSUPPORTED — "
        "implement streaming or set streaming:false until it lands.")
