"""routers/a2a.py — A2A (Agent2Agent) interop endpoints.

PR A (this file): the serving + dispatch surface, with NO run-pipeline / billing
path. Three endpoints:

  GET  /.well-known/agent-card.json  → the signed v0.3.0 Agent Card
  GET  /.well-known/jwks.json        → the gateway JWKS (public signing keys)
  POST /a2a                          → the JSON-RPC 2.0 endpoint

Every JSON-RPC method is dispatch-registered but **explicitly unimplemented** in
PR A — it returns a spec-compliant UNSUPPORTED_OPERATION error, never a faked
result. The message send/stream methods get wired to the run pipeline in PR B
(the _run_core extraction), reviewed separately so the money-path refactor and
the card-signature canonicalization proof stay independent risks.

WIRE DISCIPLINE: this module names no wire string. It dispatches on the internal
serializer.Method enum and renders via serializer.{parse_request, make_response,
make_error_response}; the leak guard scans it. The only version-bearing value it
emits, protocolVersion, comes from the signed card (card.py → WIRE_PROTOCOL_VERSION).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from core.a2a import keys as a2a_keys
from core.a2a import serializer as S
from core.a2a.serializer import ErrorCode, JsonRpcError, Method
from core.a2a.sign import build_signed_card
from core.db import get_db

logger = logging.getLogger("wayforth")
router = APIRouter()

# Gateway base for the advertised A2A endpoint. The card is SERVED here; the
# signature's jku still names the brand apex (see keys.APEX_JKU).
_GATEWAY_BASE = os.environ.get("WAYFORTH_GATEWAY_BASE", "https://gateway.wayforth.io").rstrip("/")
_A2A_ENDPOINT = f"{_GATEWAY_BASE}/a2a"

# Wayforth's advertised skill set. Structural only (AgentSkill shape is identical
# across A2A versions), so it carries no version-variant literal.
_SKILLS = [
    {
        "id": "execute-api",
        "name": "Execute API",
        "description": "Run any of 300+ verified APIs (search, inference, data, "
                       "image, audio, translation) with managed credentials.",
        "tags": ["api", "tools", "execution", "search", "inference"],
    },
]


def _agent_version() -> str:
    # Lazy import avoids a router↔main import cycle at module load.
    from main import VERSION
    return VERSION


async def _ensure_signing_key(db):
    """Idempotent + race-safe (one-active partial unique index). On-demand so PR A
    needs no startup wiring; the first card/JWKS request provisions if absent."""
    await a2a_keys.provision_signing_key(db)


# ── well-known: signed Agent Card ─────────────────────────────────────────────

@router.get("/.well-known/agent-card.json", include_in_schema=False)
async def agent_card(db=Depends(get_db)):
    await _ensure_signing_key(db)
    card = await build_signed_card(
        db,
        name="Wayforth Gateway",
        description="Agent gateway to 300+ verified APIs with managed credentials, "
                    "WayforthRank routing, and automatic provider failover.",
        url=_A2A_ENDPOINT,
        version=_agent_version(),
        skills=_SKILLS,
        documentation_url=f"{_GATEWAY_BASE}/docs",
    )
    # Served from the gateway; cacheable. content-type per A2A well-known convention.
    return JSONResponse(card, headers={"Cache-Control": "public, max-age=300"})


# ── well-known: JWKS (public signing keys; apex rewrites to this) ─────────────

@router.get("/.well-known/jwks.json", include_in_schema=False)
async def jwks(db=Depends(get_db)):
    await _ensure_signing_key(db)
    keys = await a2a_keys.get_jwks(db)
    return JSONResponse(keys, headers={"Cache-Control": "public, max-age=300"})


# ── JSON-RPC 2.0 endpoint ─────────────────────────────────────────────────────

@router.post("/a2a")
async def a2a_jsonrpc(request: Request, db=Depends(get_db)):
    """JSON-RPC 2.0 dispatch. Accepts both v0.3.0 and v1.0 method spellings (the
    serializer normalizes inbound); emits v0.3.0. Always HTTP 200 with a JSON-RPC
    envelope — transport-level errors are carried in the `error` member, per spec."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            S.make_error_response(None, JsonRpcError(ErrorCode.PARSE_ERROR)))

    try:
        req_id, method, params = S.parse_request(body)
    except JsonRpcError as e:
        # id may be unknown on a malformed envelope — echo it if present.
        return JSONResponse(
            S.make_error_response(body.get("id") if isinstance(body, dict) else None, e))

    try:
        result = await _dispatch(method, params, db)
        return JSONResponse(S.make_response(req_id, result))
    except JsonRpcError as e:
        return JSONResponse(S.make_error_response(req_id, e))
    except Exception:  # pragma: no cover - defensive
        logger.exception("a2a dispatch error")
        return JSONResponse(
            S.make_error_response(req_id, JsonRpcError(ErrorCode.INTERNAL_ERROR)))


async def _dispatch(method: Method, params: dict, db) -> dict:
    """PR A: every method is registered but not yet implemented — a spec-compliant
    UNSUPPORTED_OPERATION, explicit, never faked. PR B wires the message send/
    stream methods to _run_core and the SSE path."""
    raise JsonRpcError(
        ErrorCode.UNSUPPORTED_OPERATION,
        "This A2A method is not yet implemented on the Wayforth gateway. "
        "The Agent Card, JWKS, and JSON-RPC envelope are live; execution methods "
        "land in a follow-up.",
    )
