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

import json as _json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.a2a import keys as a2a_keys
from core.a2a import serializer as S
from core.a2a.serializer import ErrorCode, JsonRpcError, Method, TaskState
from core.a2a.sign import build_signed_card
from core.db import get_db

logger = logging.getLogger("wayforth")
router = APIRouter()

# Gateway base for the advertised A2A endpoint. The card is SERVED here, and the
# signature's jku names this same origin's JWKS (see keys.SIGNING_JKU) — no apex.
_GATEWAY_BASE = os.environ.get("WAYFORTH_GATEWAY_BASE", "https://gateway.wayforth.io").rstrip("/")
_A2A_ENDPOINT = f"{_GATEWAY_BASE}/a2a"

# The card must not claim a capability the dispatcher can't honor end-to-end.
# STREAM_MESSAGE routes to the SSE pipeline AND frames its events as A2A-conformant
# TaskStatusUpdateEvent / TaskArtifactUpdateEvent (terminal final:true), verified
# both directions against a2a-sdk==0.3.26 by the interop gate — so streaming is now
# advertised TRUE, honestly. test_a2a_router enforces this can't drift (it fails if
# the card advertises streaming:true while the method is UNSUPPORTED).
_STREAMING_SUPPORTED = True

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
                    "merit-based routing (no paid placement), and automatic provider failover.",
        url=_A2A_ENDPOINT,
        version=_agent_version(),
        skills=_SKILLS,
        streaming=_STREAMING_SUPPORTED,
        documentation_url=f"{_GATEWAY_BASE}/docs",
    )
    # Served from the gateway; cacheable. content-type per A2A well-known convention.
    return JSONResponse(card, headers={"Cache-Control": "public, max-age=300"})


# ── well-known: JWKS (public signing keys; the card's jku names this URL) ─────

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
        result = await _dispatch(method, params, db, request, req_id)
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_context_ids(message: dict) -> tuple[str, str]:
    """Reuse the inbound message's taskId/contextId when the caller continues an
    existing task; otherwise mint fresh ones."""
    task_id = message.get("taskId") or str(_uuid.uuid4())
    context_id = message.get("contextId") or str(_uuid.uuid4())
    return task_id, context_id


def _run_id_for(message: dict, context_id: str) -> str:
    """The loop-budget run_id on the A2A path. Defaults to the inbound contextId (the
    loop/session id) so a budget attached to a conversation is enforced across its
    turns; an explicit message.metadata.run_id overrides. A freshly-minted contextId
    simply has no run_budgets row → unbudgeted, exactly as today."""
    meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return meta.get("run_id") or context_id


def _result_as_task(result: dict, task_id: str, context_id: str) -> dict:
    """Wrap a /run result dict as an A2A-conformant completed Task (v0.3.0). The run
    output rides as a single data Artifact; state is terminal `completed`. Built via
    the serializer seam — byte-parseable as an a2a-sdk Task (the interop gate asserts
    the round-trip both ways)."""
    artifact = S.make_artifact(
        str(_uuid.uuid4()), [S.make_data_part(result)], name="result")
    return S.make_task(
        task_id=task_id, context_id=context_id, state=TaskState.COMPLETED,
        artifacts=[artifact], timestamp=_now_iso())


# HTTP status (from the /run money path) → A2A JSON-RPC error code. A2A defines no
# payment/rate-limit error, so 402/429 use Wayforth server-defined codes in the
# JSON-RPC reserved -32000..-32099 range; the original /run detail rides in
# error.data. The interop gate asserts every envelope parses as a2a-sdk JSONRPCError.
_HTTP_TO_ERRORCODE: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_PARAMS,        # bad request body / client error
    401: ErrorCode.INVALID_REQUEST,       # missing/invalid API key (no A2A auth code)
    402: ErrorCode.INSUFFICIENT_CREDITS,  # server-defined (-32010)
    422: ErrorCode.INVALID_PARAMS,        # missing_param / no_managed_service
    429: ErrorCode.RATE_LIMITED,          # server-defined (-32011)
    503: ErrorCode.INTERNAL_ERROR,        # all providers failed
}


def _http_to_jsonrpc(e: HTTPException) -> JsonRpcError:
    """Map a /run HTTPException onto the correct A2A JSON-RPC error. The original
    detail rides in .data so nothing is lost; unmapped statuses → INTERNAL_ERROR."""
    code = _HTTP_TO_ERRORCODE.get(e.status_code, ErrorCode.INTERNAL_ERROR)
    detail = e.detail
    msg = detail.get("error") if isinstance(detail, dict) else str(detail)
    return JsonRpcError(code, msg or "Run failed", data=detail)


def _make_a2a_sse_framer(task_id: str, context_id: str, artifact_id: str, req_id):
    """Build the SSE framer the A2A stream hands into the /run pipeline. Renders each
    structured run event as a JSON-RPC streaming-response envelope whose result is
    an A2A update event:
      • first frame → a `working`   TaskStatusUpdateEvent  (final=False)
      • token       → a             TaskArtifactUpdateEvent (append, not last)
      • final       → a `completed` TaskStatusUpdateEvent  (final=True)
      • error       → a `failed`    TaskStatusUpdateEvent  (final=True)
    Framing only — the deduct/refund tail inside _run_sse_stream is untouched."""
    state = {"started": False}

    def _sse(event: dict) -> str:
        return f"data: {_json.dumps(S.make_response(req_id, event))}\n\n"

    def frame(kind: str, payload: dict) -> str:
        out: list[str] = []
        if not state["started"]:
            state["started"] = True
            out.append(_sse(S.make_status_update_event(
                task_id=task_id, context_id=context_id,
                state=TaskState.WORKING, final=False)))
        if kind == "token":
            artifact = S.make_artifact(
                artifact_id, [S.make_text_part(payload["token"])], name="response")
            out.append(_sse(S.make_artifact_update_event(
                task_id=task_id, context_id=context_id, artifact=artifact,
                append=True, last_chunk=False)))
        elif kind == "error":
            out.append(_sse(S.make_status_update_event(
                task_id=task_id, context_id=context_id,
                state=TaskState.FAILED, final=True)))
        else:  # "final"
            out.append(_sse(S.make_status_update_event(
                task_id=task_id, context_id=context_id,
                state=TaskState.COMPLETED, final=True)))
        return "".join(out)

    return frame


async def _dispatch(method: Method, params: dict, db, request: Request, req_id):
    """Dispatch a parsed JSON-RPC method to its handler.

    SEND_MESSAGE and STREAM_MESSAGE route through the SHARED /run money path
    (execute.a2a_run_send → _run_core; execute.a2a_run_stream → the existing SSE
    pipeline) and return A2A-conformant envelopes — a completed Task for send, a
    stream of TaskStatusUpdateEvent / TaskArtifactUpdateEvent for stream. Every
    other method stays registered but explicitly unimplemented (spec-compliant
    UNSUPPORTED_OPERATION, never faked)."""
    from routers import execute as _ex

    if method is Method.SEND_MESSAGE:
        message = params.get("message") or {}
        intent, input_dict, prefs, agent_id = _extract_run_args(message)
        task_id, context_id = _task_context_ids(message)
        # The loop/session contextId IS the run budget id on the A2A path (a caller
        # may also pin one explicitly via message.metadata.run_id).
        run_id = _run_id_for(message, context_id)
        try:
            result = await _ex.a2a_run_send(
                db, request, intent=intent, input=input_dict, prefs=prefs,
                agent_id=agent_id, run_id=run_id)
        except HTTPException as e:
            raise _http_to_jsonrpc(e)
        return _result_as_task(result, task_id, context_id)

    if method is Method.STREAM_MESSAGE:
        message = params.get("message") or {}
        intent, input_dict, prefs, agent_id = _extract_run_args(message)
        task_id, context_id = _task_context_ids(message)
        run_id = _run_id_for(message, context_id)
        framer = _make_a2a_sse_framer(task_id, context_id, str(_uuid.uuid4()), req_id)
        try:
            return await _ex.a2a_run_stream(
                db, request, intent=intent, input=input_dict, prefs=prefs,
                agent_id=agent_id, framer=framer, run_id=run_id)
        except HTTPException as e:
            raise _http_to_jsonrpc(e)

    raise JsonRpcError(
        ErrorCode.UNSUPPORTED_OPERATION,
        "This A2A method is not yet implemented on the Wayforth gateway. "
        "The Agent Card, JWKS, and JSON-RPC envelope are live; execution methods "
        "land in a follow-up.",
    )
