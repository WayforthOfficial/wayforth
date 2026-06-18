"""Cloud deploy: session-OR-key auth + server-side runtime-key provisioning.

The dashboard authenticates by Supabase session. Deploy endpoints
(/templates/{id}/deploy and the "from code" /cloud/agents) accept the same
session-OR-key dependency /account/* uses, and provision the agent's runtime key
SERVER-SIDE — the browser never sends a raw key. provision_runner_key:
  * SDK caller (header present) → encrypt the header key
  * session caller (no header) → reuse the user's own stored key ciphertext
  * neither → (None, 1)

Run: uv run pytest tests/test_deploy_session_runtime_key.py -v
"""
import pytest
from fastapi import HTTPException

import core.auth as auth
from core.auth import provision_runner_key
import routers.templates as t


class _Req:
    def __init__(self, headers):
        self.headers = headers


class _FakeDB:
    def __init__(self, row=None):
        self._row = row
        self.queried = False

    async def fetchrow(self, *_a, **_k):
        self.queried = True
        return self._row


# ── provision_runner_key (the server-side runtime-key logic) ──────────────────

async def test_header_key_is_encrypted_db_not_consulted(monkeypatch):
    monkeypatch.setattr(auth, "encrypt_api_key", lambda raw, version=1: (f"CT::{raw}", 1))
    db = _FakeDB(None)
    ct, ver = await provision_runner_key(_Req({"X-Wayforth-API-Key": "wf_live_xyz"}), db, "uid")
    assert ct == "CT::wf_live_xyz" and ver == 1
    assert db.queried is False   # SDK path never touches the DB


async def test_session_caller_reuses_stored_ciphertext():
    # No header → session caller → reuse the user's own encrypted_key server-side.
    db = _FakeDB({"encrypted_key": "STORED_CIPHERTEXT", "key_version": 2})
    ct, ver = await provision_runner_key(_Req({}), db, "uid")
    assert ct == "STORED_CIPHERTEXT" and ver == 2
    assert db.queried is True


async def test_no_key_available_returns_none():
    db = _FakeDB(None)
    ct, ver = await provision_runner_key(_Req({}), db, "uid")
    assert ct is None and ver == 1


# ── deploy auth: session (no API key) is accepted ────────────────────────────

async def test_deploy_accepts_session_without_api_key(monkeypatch):
    async def _caller(request, db):
        return {"user_id": "11111111-1111-1111-1111-111111111111",
                "tier": "starter", "api_key_id": None}
    monkeypatch.setattr(t, "resolve_dashboard_caller", _caller)
    monkeypatch.setattr(t, "get_template", lambda _id: None)  # 404 right after auth
    body = t.DeployTemplateRequest(name="my-agent")
    with pytest.raises(HTTPException) as exc:
        await t.deploy_template("nope", body, _Req({}), _FakeDB())
    assert exc.value.status_code == 404            # template_not_found, NOT 401
    assert exc.value.detail.get("error") == "template_not_found"
