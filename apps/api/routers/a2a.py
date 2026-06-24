"""routers/a2a.py — A2A (Agent2Agent) interop endpoints.

The serving + dispatch surface. Three endpoints:

  GET  /.well-known/agent-card.json  → the signed v0.3.0 Agent Card
  GET  /.well-known/jwks.json        → the gateway JWKS (public signing keys)
  POST /a2a                          → the JSON-RPC 2.0 endpoint

PR B (this change): the SEND_MESSAGE / STREAM_MESSAGE methods now dispatch through
the SHARED /run money path (execute.a2a_run_send → _run_core and
execute.a2a_run_stream → the existing SSE pipeline) — one billing path, no fork.
Every OTHER method stays dispatch-registered but explicitly unimplemented: a
spec-compliant UNSUPPORTED_OPERATION, never a faked result. The remaining wire
conformance — message→Task envelope, SSE TaskStatusUpdateEvent framing, and the
A2A error mapping verified against the a2a-sdk — lands with the interop gate;
until it does, the card advertises streaming FALSE (we under-claim, never over-).

WIRE DISCIPLINE: this module names no wire string. It dispatches on the internal
serializer.Method enum and renders via serializer.{parse_request, make_response,
make_error_response}; the leak guard scans it. The only version-bearing value it
emits, protocolVersion, comes from the signed card (card.py → WIRE_PROTOCOL_VERSION).
"""
from __future__ import annotations

import logging
import os
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.a2a import keys as a2a_keys
from core.a2a import serializer as S
from core.a2a.serializer import ErrorCode, JsonRpcError, Method, Role
from core.a2a.sign import build_signed_card
from core.db import get_db

logger = logging.getLogger("wayforth")
router = APIRouter()

# Gateway base for the advertised A2A endpoint. The card is SERVED here; the
# signature's jku still names the brand apex (see keys.APEX_JKU).
_GATEWAY_BASE = os.environ.get("WAYFORTH_GATEWAY_BASE", "https://gateway.wayforth.io").rstrip("/")
_A2A_ENDPOINT = f"{_GATEWAY_BASE}/a2a"

# The card must not claim a capability the dispatcher can't honor end-to-end.
# STREAM_MESSAGE now routes to the SSE pipeline, but the SSE *events* are not yet
# A2A-conformant (TaskStatusUpdateEvent framing is interop-gate work), so we keep
# advertising streaming FALSE — under-claiming is safe; over-claiming is not.
# Flipped to True with the interop gate. test_a2a_router enforces this can't drift
# (it only fails if the card advertises streaming:true while the method is
# UNSUPPORTED — never the reverse).
_STREAMING_SUPPORTED = False

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
        streaming=_STREAMING_SUPPORTED,
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
        result = await _dispatch(method, params, db, request)
        # STREAM_MESSAGE returns a live SSE response, not a JSON-RPC result body.
        if isinstance(result, StreamingResponse):
            return result
        return JSONResponse(S.make_response(req_id, result))
    except JsonRpcError as e:
        return JSONResponse(S.make_error_response(req_id, e))
    except Exception:  # pragma: no cover - defensive
        logger.exception("a2a dispatch error")
        return JSONResponse(
            S.make_error_response(req_id, JsonRpcError(ErrorCode.INTERNAL_ERROR)))


def _extract_run_args(message: dict) -> tuple[str, dict, dict, object]:
    """A2A message → (intent, input, prefs, agent_id) for the /run money path.

    Structural only — no version-specific wire string (parts/metadata shapes are
    identical across A2A versions). Text parts join into the intent; data parts
    merge into the run input; Wayforth extensions (preferences, agent_id) ride in
    message.metadata."""
    parts = message.get("parts") or []
    texts: list[str] = []
    data: dict = {}
    for p in parts:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind") or p.get("type")
        if kind == "text" and p.get("text"):
            texts.append(str(p["text"]))
        elif kind == "data" and isinstance(p.get("data"), dict):
            data.update(p["data"])
    meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    intent = " ".join(texts).strip()
    input_dict = data or (meta.get("input") if isinstance(meta.get("input"), dict) else {}) or {}
    prefs = meta.get("preferences") if isinstance(meta.get("preferences"), dict) else {}
    agent_id = meta.get("agent_id")
    return intent, input_dict, prefs, agent_id


def _wrap_result_as_message(result: dict) -> dict:
    """Wrap a /run result dict as a minimal A2A agent Message (v0.3.0 via the seam).
    Spec-conformant message→Task framing is the interop gate's job; this only proves
    the method returns a real result, not UNSUPPORTED."""
    internal = {
        "messageId": str(_uuid.uuid4()),
        "role": Role.AGENT,
        "parts": [{"kind": "data", "data": result}],
        "kind": "message",
    }
    return S.serialize_message(internal)


def _http_to_jsonrpc(e: HTTPException) -> JsonRpcError:
    """Map a /run HTTPException onto a JSON-RPC error. 4xx caller faults →
    INVALID_PARAMS; everything else → INTERNAL_ERROR. The original detail rides in
    .data so nothing is lost. (Richer A2A error/Task mapping lands with the gate.)"""
    code = ErrorCode.INVALID_PARAMS if 400 <= e.status_code < 500 else ErrorCode.INTERNAL_ERROR
    detail = e.detail
    msg = detail.get("error") if isinstance(detail, dict) else str(detail)
    return JsonRpcError(code, msg or "Run failed", data=detail)


async def _dispatch(method: Method, params: dict, db, request: Request):
    """Dispatch a parsed JSON-RPC method to its handler.

    SEND_MESSAGE and STREAM_MESSAGE route through the SHARED /run money path
    (execute.a2a_run_send → _run_core; execute.a2a_run_stream → the existing SSE
    pipeline) — no second billing path. Every other method is still registered but
    explicitly unimplemented (spec-compliant UNSUPPORTED_OPERATION, never faked);
    they wire up with the interop gate."""
    from routers import execute as _ex

    if method is Method.SEND_MESSAGE:
        message = params.get("message") or {}
        intent, input_dict, prefs, agent_id = _extract_run_args(message)
        try:
            result = await _ex.a2a_run_send(
                db, request, intent=intent, input=input_dict, prefs=prefs, agent_id=agent_id)
        except HTTPException as e:
            raise _http_to_jsonrpc(e)
        return _wrap_result_as_message(result)

    if method is Method.STREAM_MESSAGE:
        message = params.get("message") or {}
        intent, input_dict, prefs, agent_id = _extract_run_args(message)
        try:
            return await _ex.a2a_run_stream(
                db, request, intent=intent, input=input_dict, prefs=prefs, agent_id=agent_id)
        except HTTPException as e:
            raise _http_to_jsonrpc(e)

    raise JsonRpcError(
        ErrorCode.UNSUPPORTED_OPERATION,
        "This A2A method is not yet implemented on the Wayforth gateway. "
        "The Agent Card, JWKS, and JSON-RPC envelope are live; execution methods "
        "land in a follow-up.",
    )
