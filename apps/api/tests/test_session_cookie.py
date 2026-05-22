"""tests/test_session_cookie.py — Browser session proxy (wf_session cookie).

Coverage:

  core/session.py helpers
    - create / get / refresh / revoke against a fake async-redis stub
    - Redis key uses sha256(token), not the raw token
    - cookie attributes: HttpOnly, Secure, SameSite=Strict, Path=/, Max-Age
    - clear cookie sets Max-Age=0

  SessionCookieMiddleware (ASGI)
    - valid cookie → stashes record on scope
    - no cookie → scope untouched, request proceeds
    - invalid / expired cookie → scope untouched
    - Redis lookup error → non-fatal, scope untouched

  Endpoints
    - POST /auth/session: invalid JWT → 401; valid JWT but unknown account → 401;
      valid JWT + linked account → 200 with Set-Cookie carrying wf_session
    - POST /auth/session/refresh: no session on scope → 401; with session →
      extends TTL and re-sets cookie
    - POST /auth/session/logout: idempotent, clears cookie
    - GET /auth/me: cookie path returns same shape as JWT path
    - POST /auth/mfa/disable (developer): cookie satisfies the post-TOTP
      "supabase session" gate (no Authorization header needed)

All tests are pure-unit — no live Redis, no live HTTP, no DB. The
async-redis stub is in-process. Tests run with `pytest --confcutdir=/tmp`
to bypass the conftest's live-deployment availability fixture.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.session import (  # noqa: E402
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    _SCOPE_KEY_RECORD,
    _SCOPE_KEY_TOKEN,
    _redis_key,
    _stash_on_scope,
    clear_session_cookie,
    create_session,
    get_request_session,
    get_request_session_token,
    get_session,
    refresh_session,
    revoke_session,
    set_session_cookie,
)


# ─────────────────────────────────────────────────────────────────────────────
# Session fixture: strip slowapi @limiter wrappers from the endpoints we test
# ─────────────────────────────────────────────────────────────────────────────
#
# slowapi's @limiter.limit raises if `request` isn't a real starlette Request,
# which is fine in production but blocks direct unit-test invocation with
# MagicMock stand-ins. We swap the bare __wrapped__ functions in for the
# duration of the session, then restore — same pattern as tests/test_mfa.py.

@pytest.fixture(autouse=True, scope="session")
def _unwrap_session_endpoints():
    import routers.auth as _a
    import routers.mfa as _m
    saved = {}
    for mod, names in (
        (_a, ("auth_session_create", "auth_session_refresh",
              "auth_session_logout", "auth_me")),
        (_m, ("mfa_disable",)),
    ):
        for name in names:
            fn = getattr(mod, name, None)
            if fn and hasattr(fn, "__wrapped__"):
                saved[(mod, name)] = fn
                setattr(mod, name, fn.__wrapped__)
    yield
    for (mod, name), fn in saved.items():
        setattr(mod, name, fn)


# ─────────────────────────────────────────────────────────────────────────────
# Fake async-redis stub
# ─────────────────────────────────────────────────────────────────────────────


class FakeRedis:
    """Minimal async-redis stub: SET with EX, GET, EXPIRE, DELETE.
    Stores TTL-seconds-from-now (monotonic) for expiry checks."""

    def __init__(self):
        self.store: dict[str, tuple[str, float]] = {}

    def _expired(self, key: str) -> bool:
        v = self.store.get(key)
        return bool(v and v[1] <= _time.monotonic())

    async def set(self, key, value, ex=None):
        deadline = _time.monotonic() + (ex if ex is not None else 10**9)
        self.store[key] = (value, deadline)
        return True

    async def get(self, key):
        if self._expired(key):
            self.store.pop(key, None)
            return None
        v = self.store.get(key)
        return v[0] if v else None

    async def expire(self, key, ex):
        v = self.store.get(key)
        if not v:
            return False
        self.store[key] = (v[0], _time.monotonic() + ex)
        return True

    async def delete(self, key):
        return 1 if self.store.pop(key, None) else 0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _scope_with_cookie(cookie_header: str | None) -> dict:
    headers: list[tuple[bytes, bytes]] = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode("latin-1")))
    return {
        "type": "http",
        "method": "GET",
        "path": "/auth/me",
        "headers": headers,
        "raw_path": b"/auth/me",
        "query_string": b"",
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "scheme": "https",
    }


# ─────────────────────────────────────────────────────────────────────────────
# core/session.py — store helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionStore:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_create_session_returns_unguessable_token(self):
        r = FakeRedis()
        tok = await create_session(r, "uid-1", "u@example.com", "pro", "sub-1")
        # urlsafe-base64 of 48 bytes ~ 64 chars; allow URL-safe alphabet.
        assert len(tok) >= 32
        assert re.match(r"^[A-Za-z0-9_\-]+$", tok)
        # Two consecutive calls give different tokens.
        tok2 = await create_session(r, "uid-1", "u@example.com", "pro", "sub-1")
        assert tok != tok2

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_redis_key_is_sha256_of_token(self):
        r = FakeRedis()
        tok = await create_session(r, "uid-1", "u@example.com", "pro", "sub-1")
        # The raw token must NOT appear as any key — a Redis dump should reveal
        # no active cookie values.
        keys = list(r.store.keys())
        assert all(tok not in k for k in keys), "raw token leaked into redis key"
        # And the key we DO use must be the sha256-prefixed form.
        expected = _redis_key(tok)
        assert expected in r.store

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_get_session_returns_record_fields(self):
        r = FakeRedis()
        tok = await create_session(r, "uid-1", "u@example.com", "pro", "sub-1")
        rec = await get_session(r, tok)
        assert rec is not None
        assert rec["user_id"] == "uid-1"
        assert rec["email"] == "u@example.com"
        assert rec["tier"] == "pro"
        assert rec["supabase_id"] == "sub-1"
        assert "created_at" in rec

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_get_session_returns_none_for_missing_token(self):
        r = FakeRedis()
        assert await get_session(r, "no-such-token") is None
        assert await get_session(r, "") is None

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_refresh_session_extends_ttl(self):
        r = FakeRedis()
        tok = await create_session(r, "uid", "e", "free", "sub")
        # Tamper with the stored deadline to simulate near-expiry.
        key = _redis_key(tok)
        value, _ = r.store[key]
        r.store[key] = (value, _time.monotonic() + 1)  # 1 sec left
        rec = await refresh_session(r, tok)
        assert rec is not None
        # TTL must be back near SESSION_TTL_SECONDS.
        _, new_deadline = r.store[key]
        assert new_deadline - _time.monotonic() > SESSION_TTL_SECONDS - 5

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_refresh_session_returns_none_for_missing(self):
        r = FakeRedis()
        assert await refresh_session(r, "unknown") is None

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_revoke_session_removes_record(self):
        r = FakeRedis()
        tok = await create_session(r, "uid", "e", "free", "sub")
        assert await get_session(r, tok) is not None
        await revoke_session(r, tok)
        assert await get_session(r, tok) is None

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_revoke_session_idempotent_on_missing(self):
        r = FakeRedis()
        # Should not raise.
        await revoke_session(r, "no-such-token")
        await revoke_session(r, "")


# ─────────────────────────────────────────────────────────────────────────────
# Cookie attribute checks
# ─────────────────────────────────────────────────────────────────────────────


class TestCookieAttributes:
    """Verify the cookie we set has every hardening attribute the brief calls for."""

    def _grab_set_cookie(self, response):
        # Starlette stores the Set-Cookie value on response.headers; for
        # multi-cookie responses it's listed under raw headers.
        for k, v in response.raw_headers:
            if k.lower() == b"set-cookie":
                return v.decode("latin-1")
        return None

    @pytest.mark.no_api_key
    def test_set_session_cookie_carries_all_hardening_attrs(self):
        from fastapi.responses import JSONResponse
        response = JSONResponse(content={"ok": True})
        set_session_cookie(response, raw_token="opaque-token-value")
        sc = self._grab_set_cookie(response)
        assert sc is not None, "no Set-Cookie header on response"
        # Single Set-Cookie header should contain all five attributes.
        assert sc.startswith("wf_session=opaque-token-value")
        low = sc.lower()
        assert "httponly" in low
        assert "secure" in low
        assert "samesite=strict" in low
        assert "path=/" in low
        assert f"max-age={SESSION_TTL_SECONDS}" in low

    @pytest.mark.no_api_key
    def test_clear_session_cookie_expires_at_zero(self):
        from fastapi.responses import JSONResponse
        response = JSONResponse(content={"ok": True})
        clear_session_cookie(response)
        sc = self._grab_set_cookie(response)
        assert sc is not None
        # `delete_cookie` clears via Max-Age=0 (and/or Expires in the past).
        low = sc.lower()
        assert "wf_session=" in sc
        assert "max-age=0" in low or "expires=" in low


# ─────────────────────────────────────────────────────────────────────────────
# SessionCookieMiddleware
# ─────────────────────────────────────────────────────────────────────────────


class _Sink:
    def __init__(self):
        self.start = None
        self.body_chunks = []

    async def __call__(self, msg):
        if msg["type"] == "http.response.start":
            self.start = msg
        elif msg["type"] == "http.response.body":
            self.body_chunks.append(msg.get("body", b""))


async def _stub_app(scope, receive, send):
    """Echo app — surfaces what the middleware stashed on scope."""
    payload = {
        "session": scope.get(_SCOPE_KEY_RECORD),
        "token_present": scope.get(_SCOPE_KEY_TOKEN) is not None,
    }
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body})


class TestSessionCookieMiddleware:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_no_cookie_leaves_scope_untouched(self):
        from main import SessionCookieMiddleware
        sink = _Sink()
        scope = _scope_with_cookie(None)
        # Even if redis is reachable, no cookie → no lookup.
        with patch("core.tier_gates._get_redis", return_value=FakeRedis()):
            await SessionCookieMiddleware(_stub_app)(scope, AsyncMock(), sink)
        body = json.loads(b"".join(sink.body_chunks))
        assert body["session"] is None
        assert body["token_present"] is False

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_valid_cookie_stashes_record(self):
        from main import SessionCookieMiddleware
        r = FakeRedis()
        tok = await create_session(r, "uid-7", "z@example.com", "starter", "sub-7")
        sink = _Sink()
        scope = _scope_with_cookie(f"wf_session={tok}")
        with patch("core.tier_gates._get_redis", return_value=r):
            await SessionCookieMiddleware(_stub_app)(scope, AsyncMock(), sink)
        body = json.loads(b"".join(sink.body_chunks))
        assert body["token_present"] is True
        assert body["session"]["user_id"] == "uid-7"
        assert body["session"]["email"] == "z@example.com"
        assert body["session"]["tier"] == "starter"

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_unknown_cookie_does_not_stash(self):
        from main import SessionCookieMiddleware
        sink = _Sink()
        scope = _scope_with_cookie("wf_session=bogus-token-not-in-redis")
        with patch("core.tier_gates._get_redis", return_value=FakeRedis()):
            await SessionCookieMiddleware(_stub_app)(scope, AsyncMock(), sink)
        body = json.loads(b"".join(sink.body_chunks))
        assert body["session"] is None
        assert body["token_present"] is False

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_redis_unavailable_does_not_break_request(self):
        from main import SessionCookieMiddleware
        sink = _Sink()
        scope = _scope_with_cookie("wf_session=any-token")
        # Simulate Redis being unconfigured/unreachable.
        with patch("core.tier_gates._get_redis", return_value=None):
            await SessionCookieMiddleware(_stub_app)(scope, AsyncMock(), sink)
        body = json.loads(b"".join(sink.body_chunks))
        # Request still served, just without a session attached.
        assert body["session"] is None
        assert sink.start["status"] == 200

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_other_cookies_in_jar_are_ignored(self):
        from main import SessionCookieMiddleware
        r = FakeRedis()
        tok = await create_session(r, "uid-9", "@", "free", "s")
        sink = _Sink()
        # Cookie header with multiple cookies; only wf_session should be picked up.
        scope = _scope_with_cookie(f"theme=dark; wf_session={tok}; sb-access=stale")
        with patch("core.tier_gates._get_redis", return_value=r):
            await SessionCookieMiddleware(_stub_app)(scope, AsyncMock(), sink)
        body = json.loads(b"".join(sink.body_chunks))
        assert body["session"] is not None
        assert body["session"]["user_id"] == "uid-9"


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints — POST /auth/session, /refresh, /logout
# ─────────────────────────────────────────────────────────────────────────────


def _stub_request(json_body: dict | None = None,
                  scope_extras: dict | None = None,
                  headers: dict | None = None) -> MagicMock:
    """Minimal Request stand-in for the auth endpoints."""
    req = MagicMock()
    req.headers = headers or {}
    req.scope = {"type": "http"}
    if scope_extras:
        req.scope.update(scope_extras)
    req.json = AsyncMock(return_value=json_body or {})
    return req


class TestAuthSessionCreate:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_missing_jwt_returns_400(self):
        from routers.auth import auth_session_create
        from fastapi import HTTPException
        with patch("core.tier_gates._get_redis", return_value=FakeRedis()):
            with pytest.raises(HTTPException) as exc:
                await auth_session_create(_stub_request({}), db=AsyncMock())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_400_response_includes_received_keys_for_diagnostics(self):
        """Frontend devs hit /auth/session with the wrong field name and need
        to see what keys they sent vs. what's accepted, without us echoing the
        actual JWT (values) into the response body."""
        from routers.auth import auth_session_create
        from fastapi import HTTPException
        with patch("core.tier_gates._get_redis", return_value=FakeRedis()):
            with pytest.raises(HTTPException) as exc:
                await auth_session_create(
                    _stub_request({"wrong_field": "abc", "another": "xyz"}),
                    db=AsyncMock(),
                )
        assert exc.value.status_code == 400
        detail = exc.value.detail
        assert detail["error"] == "supabase_jwt_required"
        assert detail["received_keys"] == ["another", "wrong_field"]  # sorted
        assert "supabase_jwt" in detail["accepted_fields"]
        assert "access_token" in detail["accepted_fields"]
        # Critically: actual JWT-like values must NOT appear anywhere in
        # detail (we only ever surface keys, not values).
        import json as _json
        rendered = _json.dumps(detail)
        assert "abc" not in rendered
        assert "xyz" not in rendered

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_400_logs_keys_not_values(self, caplog):
        """The diagnostic log line must include the received KEY NAMES but
        NEVER the values (which would be JWTs in normal flows)."""
        import logging as _logging
        from routers.auth import auth_session_create
        from fastapi import HTTPException
        with caplog.at_level(_logging.WARNING, logger="wayforth"):
            with patch("core.tier_gates._get_redis", return_value=FakeRedis()):
                with pytest.raises(HTTPException):
                    await auth_session_create(
                        _stub_request({"sb_token": "leak-me-not-1234567890"}),
                        db=AsyncMock(),
                    )
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert "sb_token" in log_text       # key name logged
        assert "leak-me-not" not in log_text  # value NEVER logged

    @pytest.mark.parametrize("field", ["supabase_jwt", "token", "access_token", "jwt"])
    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_jwt_field_aliases_all_accepted(self, field):
        """All four documented field names must be accepted. Lovable's Supabase
        JS client emits `access_token`; hand-rolled callers often use `token`
        or `jwt`; our docs reference `supabase_jwt`."""
        from routers.auth import auth_session_create
        r = FakeRedis()
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value={
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "user@example.com",
            "supabase_id": "sub-x",
            "tier": "free",
            "package_tier": "free",
            "lifetime_credits": 0,
        })
        with patch("core.tier_gates._get_redis", return_value=r), \
             patch("routers.auth.verify_supabase_jwt",
                   return_value={"sub": "sub-x", "email": "user@example.com"}):
            response = await auth_session_create(
                _stub_request({field: "valid-jwt-value"}),
                db=db,
            )
        assert json.loads(response.body) == {"ok": True}
        # Cookie was set regardless of which alias the caller used.
        set_cookies = [v.decode("latin-1") for k, v in response.raw_headers
                       if k.lower() == b"set-cookie"]
        assert any(c.startswith("wf_session=") for c in set_cookies)

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_supabase_jwt_wins_over_other_aliases(self):
        """When multiple known field names are present, supabase_jwt is used
        (per documented priority order). Verified by patching the verifier to
        accept ONLY a specific value and ensuring the right one is picked."""
        from routers.auth import auth_session_create
        r = FakeRedis()
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value={
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "user@example.com",
            "supabase_id": "sub-x",
            "tier": "free",
            "package_tier": "free",
            "lifetime_credits": 0,
        })

        def _verify_only_canonical(tok):
            if tok != "CANONICAL":
                raise Exception("wrong token used")
            return {"sub": "sub-x", "email": "user@example.com"}

        with patch("core.tier_gates._get_redis", return_value=r), \
             patch("routers.auth.verify_supabase_jwt",
                   side_effect=_verify_only_canonical):
            response = await auth_session_create(
                _stub_request({
                    "supabase_jwt": "CANONICAL",
                    "access_token": "FALLBACK",
                    "token": "ANOTHER",
                    "jwt": "YET-ANOTHER",
                }),
                db=db,
            )
        assert json.loads(response.body) == {"ok": True}

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_invalid_jwt_returns_401(self):
        from routers.auth import auth_session_create
        from fastapi import HTTPException
        with patch("core.tier_gates._get_redis", return_value=FakeRedis()), \
             patch("routers.auth.verify_supabase_jwt", side_effect=Exception("bad sig")):
            with pytest.raises(HTTPException) as exc:
                await auth_session_create(
                    _stub_request({"supabase_jwt": "broken.jwt.value"}),
                    db=AsyncMock(),
                )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_no_linked_account_returns_401(self):
        from routers.auth import auth_session_create
        from fastapi import HTTPException
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value=None)
        with patch("core.tier_gates._get_redis", return_value=FakeRedis()), \
             patch("routers.auth.verify_supabase_jwt",
                   return_value={"sub": "sub-x", "email": "x@example.com"}):
            with pytest.raises(HTTPException) as exc:
                await auth_session_create(
                    _stub_request({"supabase_jwt": "ok"}),
                    db=db,
                )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_redis_unavailable_returns_503(self):
        from routers.auth import auth_session_create
        from fastapi import HTTPException
        with patch("core.tier_gates._get_redis", return_value=None):
            with pytest.raises(HTTPException) as exc:
                await auth_session_create(
                    _stub_request({"supabase_jwt": "ok"}),
                    db=AsyncMock(),
                )
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_happy_path_sets_cookie_and_returns_ok(self):
        from routers.auth import auth_session_create
        r = FakeRedis()
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value={
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "user@example.com",
            "supabase_id": "sub-x",
            "tier": "starter",
            "package_tier": "starter",
            "lifetime_credits": 21000,
        })
        with patch("core.tier_gates._get_redis", return_value=r), \
             patch("routers.auth.verify_supabase_jwt",
                   return_value={"sub": "sub-x", "email": "user@example.com"}):
            response = await auth_session_create(
                _stub_request({"supabase_jwt": "ok"}),
                db=db,
            )
        # Response body has no token (cookie-only delivery is the whole point).
        assert json.loads(response.body) == {"ok": True}
        # Exactly one wf_session cookie set.
        set_cookies = [v.decode("latin-1") for k, v in response.raw_headers if k.lower() == b"set-cookie"]
        wf_cookies = [c for c in set_cookies if c.startswith("wf_session=")]
        assert len(wf_cookies) == 1
        # Redis got a record under the hashed key.
        assert len(r.store) == 1

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_email_mismatch_with_jwt_claim_returns_401(self):
        from routers.auth import auth_session_create
        from fastapi import HTTPException
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value={
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "real@example.com",
            "supabase_id": "sub-x",
            "tier": "free",
            "package_tier": "free",
            "lifetime_credits": 0,
        })
        with patch("core.tier_gates._get_redis", return_value=FakeRedis()), \
             patch("routers.auth.verify_supabase_jwt",
                   return_value={"sub": "sub-x", "email": "attacker@example.com"}):
            with pytest.raises(HTTPException) as exc:
                await auth_session_create(
                    _stub_request({"supabase_jwt": "ok"}),
                    db=db,
                )
        assert exc.value.status_code == 401


class TestAuthSessionRefresh:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_no_session_on_scope_returns_401(self):
        from routers.auth import auth_session_refresh
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await auth_session_refresh(_stub_request())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_refresh_extends_ttl_and_resets_cookie(self):
        from routers.auth import auth_session_refresh
        r = FakeRedis()
        tok = await create_session(r, "uid", "e@e", "pro", "sub")
        # Simulate near-expiry to verify the refresh pushes TTL out.
        key = _redis_key(tok)
        value, _ = r.store[key]
        r.store[key] = (value, _time.monotonic() + 1)
        req = _stub_request(scope_extras={
            _SCOPE_KEY_RECORD: {"user_id": "uid"},
            _SCOPE_KEY_TOKEN: tok,
        })
        with patch("core.tier_gates._get_redis", return_value=r):
            response = await auth_session_refresh(req)
        assert json.loads(response.body) == {"ok": True}
        # TTL pushed back near full window.
        _, new_deadline = r.store[key]
        assert new_deadline - _time.monotonic() > SESSION_TTL_SECONDS - 5
        # Cookie was re-issued.
        set_cookies = [v.decode("latin-1") for k, v in response.raw_headers if k.lower() == b"set-cookie"]
        assert any(c.startswith("wf_session=") for c in set_cookies)

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_refresh_after_revoke_returns_401(self):
        from routers.auth import auth_session_refresh
        from fastapi import HTTPException
        r = FakeRedis()
        tok = await create_session(r, "uid", "e@e", "pro", "sub")
        # Middleware-style stash succeeds, but the record is gone before refresh.
        await revoke_session(r, tok)
        req = _stub_request(scope_extras={
            _SCOPE_KEY_RECORD: {"user_id": "uid"},
            _SCOPE_KEY_TOKEN: tok,
        })
        with patch("core.tier_gates._get_redis", return_value=r):
            with pytest.raises(HTTPException) as exc:
                await auth_session_refresh(req)
        assert exc.value.status_code == 401


class TestAuthSessionLogout:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_logout_is_idempotent_without_session(self):
        from routers.auth import auth_session_logout
        response = await auth_session_logout(_stub_request())
        assert json.loads(response.body) == {"ok": True}
        set_cookies = [v.decode("latin-1") for k, v in response.raw_headers if k.lower() == b"set-cookie"]
        # Cookie is cleared regardless.
        assert any("wf_session=" in c and ("max-age=0" in c.lower() or "expires=" in c.lower()) for c in set_cookies)

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_logout_revokes_active_session(self):
        from routers.auth import auth_session_logout
        r = FakeRedis()
        tok = await create_session(r, "uid", "e", "pro", "sub")
        req = _stub_request(scope_extras={
            _SCOPE_KEY_RECORD: {"user_id": "uid"},
            _SCOPE_KEY_TOKEN: tok,
        })
        with patch("core.tier_gates._get_redis", return_value=r):
            response = await auth_session_logout(req)
        assert json.loads(response.body) == {"ok": True}
        assert await get_session(r, tok) is None


# ─────────────────────────────────────────────────────────────────────────────
# /auth/me — cookie path
# ─────────────────────────────────────────────────────────────────────────────


class TestAuthMeCookiePath:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_session_returns_user_payload(self):
        from routers.auth import auth_me
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value={
            "email": "u@e.com",
            "tier": "pro",
            "package_tier": "pro",
            "credits_balance": 9000,
            "lifetime_credits": 72000,
        })
        req = _stub_request(scope_extras={
            _SCOPE_KEY_RECORD: {"user_id": "uid-1", "email": "u@e.com",
                                "tier": "pro", "supabase_id": "sub-1"},
            _SCOPE_KEY_TOKEN: "tok",
        })
        response = await auth_me(req, db=db)
        body = json.loads(response.body)
        assert body["email"] == "u@e.com"
        assert body["tier"] == "pro"
        assert body["credits_remaining"] == 9000

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_with_missing_account_returns_401(self):
        from routers.auth import auth_me
        from fastapi import HTTPException
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value=None)
        req = _stub_request(scope_extras={
            _SCOPE_KEY_RECORD: {"user_id": "uid-gone", "email": "u@e",
                                "tier": "free", "supabase_id": "sub"},
            _SCOPE_KEY_TOKEN: "tok",
        })
        with pytest.raises(HTTPException) as exc:
            await auth_me(req, db=db)
        assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# /auth/mfa/disable — cookie satisfies the supabase-session gate
# ─────────────────────────────────────────────────────────────────────────────


class TestMFADisableCookiePath:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_developer_cookie_path_allows_disable(self):
        """Cookie carrying a session for the SAME user_id satisfies the
        post-TOTP "supabase session required" gate without an Authorization
        header. The middleware-validated record's user_id must match the
        account being modified — different user_id falls through to the
        Bearer/JWT requirement (covered in test_session_account_mismatch)."""
        import pyotp
        from routers.mfa import mfa_disable, MFADisableBody
        db = AsyncMock()
        db.execute = AsyncMock()
        secret = pyotp.random_base32()
        req = MagicMock()
        req.headers = {}
        req.scope = {
            _SCOPE_KEY_RECORD: {
                "user_id": "uid-cookie", "email": "u@e", "tier": "pro",
                "supabase_id": "sub-cookie",
            },
            _SCOPE_KEY_TOKEN: "tok",
        }
        with patch("routers.mfa._resolve_caller", return_value=(
            "user", "uid-cookie", "u@e", "developer",
            {"mfa_secret": secret, "mfa_enabled": True, "password_hash": None},
        )):
            result = await mfa_disable(req, MFADisableBody(code=pyotp.TOTP(secret).now()), db)
        assert result["mfa_enabled"] is False

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_for_different_user_falls_through_to_jwt_requirement(self):
        """A cookie for user A presented when modifying user B must NOT grant
        access — the cookie path requires user_id match. Without a matching
        cookie or a JWT, MFA disable must 401."""
        import pyotp
        from routers.mfa import mfa_disable, MFADisableBody
        from fastapi import HTTPException
        db = AsyncMock()
        secret = pyotp.random_base32()
        req = MagicMock()
        req.headers = {}  # no Authorization fallback
        req.scope = {
            _SCOPE_KEY_RECORD: {"user_id": "uid-A", "email": "a@e",
                                "tier": "free", "supabase_id": "sub-A"},
            _SCOPE_KEY_TOKEN: "tok",
        }
        with patch("routers.mfa._resolve_caller", return_value=(
            "user", "uid-B", "b@e", "developer",
            {"mfa_secret": secret, "mfa_enabled": True, "password_hash": None},
        )):
            with pytest.raises(HTTPException) as exc:
                await mfa_disable(req, MFADisableBody(code=pyotp.TOTP(secret).now()), db)
        assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Request helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestRequestHelpers:

    @pytest.mark.no_api_key
    def test_get_request_session_returns_stashed_record(self):
        req = MagicMock()
        req.scope = {_SCOPE_KEY_RECORD: {"user_id": "x"}}
        assert get_request_session(req) == {"user_id": "x"}

    @pytest.mark.no_api_key
    def test_get_request_session_returns_none_when_unset(self):
        req = MagicMock()
        req.scope = {}
        assert get_request_session(req) is None

    @pytest.mark.no_api_key
    def test_stash_on_scope_round_trips(self):
        scope: dict = {}
        _stash_on_scope(scope, {"user_id": "y"}, "tok")
        req = MagicMock()
        req.scope = scope
        assert get_request_session(req) == {"user_id": "y"}
        assert get_request_session_token(req) == "tok"
