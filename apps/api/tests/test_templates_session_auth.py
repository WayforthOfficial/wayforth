"""Regression — POST /templates/{id}/deploy must accept session auth.

The dashboard authenticates by session (wf_session cookie), not by pushing the
API key into the browser. deploy_template was API-key-only (401
"X-Wayforth-API-Key required"); it now uses resolve_dashboard_caller — the same
session-OR-key dependency /account/* and /cloud/* use.

Run: uv run pytest tests/test_templates_session_auth.py -v
"""
import pytest
from fastapi import HTTPException

import routers.templates as t


class _Req:
    """Request with NO X-Wayforth-API-Key — i.e. a browser/session caller."""
    headers: dict = {}


class _FakeDB:
    async def fetchrow(self, *_a, **_k):
        return None


def _fake_caller(tier):
    async def _caller(request, db):
        return {"user_id": "11111111-1111-1111-1111-111111111111",
                "tier": tier, "api_key_id": None}
    return _caller


async def test_session_caller_passes_auth_without_api_key(monkeypatch):
    # Session caller (starter tier), no API key header. Stop at template lookup so
    # we isolate the auth+tier path: reaching 404 proves auth did NOT 401.
    monkeypatch.setattr(t, "resolve_dashboard_caller", _fake_caller("starter"))
    monkeypatch.setattr(t, "get_template", lambda _id: None)
    body = t.DeployTemplateRequest(name="my-agent")
    with pytest.raises(HTTPException) as exc:
        await t.deploy_template("nope", body, _Req(), _FakeDB())
    assert exc.value.status_code == 404           # template_not_found, NOT 401
    assert exc.value.detail.get("error") == "template_not_found"


async def test_unauthenticated_is_rejected(monkeypatch):
    # No session and no key → the shared dependency raises 401; deploy propagates it.
    async def _reject(request, db):
        raise HTTPException(status_code=401, detail="auth required")
    monkeypatch.setattr(t, "resolve_dashboard_caller", _reject)
    body = t.DeployTemplateRequest(name="my-agent")
    with pytest.raises(HTTPException) as exc:
        await t.deploy_template("x", body, _Req(), _FakeDB())
    assert exc.value.status_code == 401
