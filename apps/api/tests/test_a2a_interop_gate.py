"""test_a2a_interop_gate.py — A2A protocol conformance against the REAL a2a-sdk.

THE INTEROP GATE. Everything is verified against a2a-sdk==0.3.26 actual types
(imported below — a hard dependency declared in the `dev` dependency-group), never
against remembered symbol names. Both directions are exercised:

  • our message/send response byte-parses as the SDK's Task (+ round-trips equal),
  • an SDK-constructed SendMessageRequest dispatches through our handler,
  • our message/stream SSE events parse as the SDK's streaming update events
    (TaskStatusUpdateEvent / TaskArtifactUpdateEvent) with a single terminal
    final:true,
  • our JSON-RPC error envelopes (the /run 400/401/402/422/429/503 set) parse as
    the SDK's JSONRPCErrorResponse with the mapped codes.

This work is protocol-framing only — the money path (deduct/refund) is unchanged;
test_run_core_parity (the 8-case net + no-fork) is the guard that proves it.
"""
from __future__ import annotations

import asyncio
import json

import a2a.types as A  # hard dep: the gate verifies against the real SDK types

import core.a2a.serializer as S
import routers.a2a as a2a_router
import routers.execute as ex
from core.a2a.serializer import JsonRpcError, Method, TaskState

# Reuse the money-path test harness (mocks every external effect _run_core touches).
from tests.test_run_core_parity import (
    _FakeReq,
    _RunDB,
    _candidate,
    _install_mocks,
)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _dump(obj) -> dict:
    """Canonical SDK wire form: camelCase aliases, omit None — what a peer emits."""
    return obj.model_dump(mode="json", by_alias=True, exclude_none=True)


def _send(monkeypatch, *, intent="chat hello", user_input=None, candidates=None,
          exec_script=None, deduct_ok=True):
    """Drive A2A SEND_MESSAGE end-to-end through our dispatcher; return the result
    dict (the A2A Task envelope) or raise the JsonRpcError our handler would surface."""
    _install_mocks(monkeypatch,
                   exec_script=exec_script or {"groq": ({"content": "hi"}, None, 7)},
                   auth_raises=None, deduct_ok=deduct_ok)
    parts = [{"kind": "text", "text": intent}]
    if user_input is not None:
        parts.append({"kind": "data", "data": user_input})
    message = {"role": "user", "parts": parts}
    return asyncio.run(a2a_router._dispatch(
        Method.SEND_MESSAGE, {"message": message},
        _RunDB(candidates or [_candidate("groq")]), _FakeReq({}), "rpc-1"))


def _collect_stream(monkeypatch, tokens, *, candidates=None):
    """Drive A2A STREAM_MESSAGE with a mocked token source; return the parsed list
    of JSON-RPC SSE envelopes (each {jsonrpc,id,result})."""
    _install_mocks(monkeypatch, exec_script={}, auth_raises=None, deduct_ok=True)
    import services.managed as managed

    async def fake_stream(params, key):
        for tk in tokens:
            yield tk

    monkeypatch.setattr(managed, "stream_groq", fake_stream)
    monkeypatch.setattr(managed, "stream_together", fake_stream)
    monkeypatch.setattr(ex, "_active_streams", {})

    framer = a2a_router._make_a2a_sse_framer("task-1", "ctx-1", "art-1", "rpc-1")

    async def _go():
        resp = await ex.a2a_run_stream(
            _RunDB(candidates or [_candidate("groq")]), _FakeReq({}),
            intent="chat hi", input={"messages": [{"role": "user", "content": "hi"}]},
            prefs={}, agent_id=None, framer=framer)
        raw = []
        async for chunk in resp.body_iterator:
            raw.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(raw)

    body = asyncio.run(_go())
    envelopes = []
    for block in body.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            envelopes.append(json.loads(block[len("data: "):]))
    return envelopes


# ── direction 1: our send response → SDK Task (parse + round-trip both ways) ─────

def test_send_response_is_sdk_conformant_task(monkeypatch):
    task = _send(monkeypatch, user_input={"messages": [{"role": "user", "content": "hi"}]})

    # our output → SDK parse
    sdk_task = A.Task.model_validate(task)
    assert sdk_task.kind == "task"
    assert sdk_task.status.state == A.TaskState.completed
    assert sdk_task.artifacts and sdk_task.artifacts[0].parts[0].root.data["service_used"]["slug"] == "groq"

    # round-trip equality: SDK re-dump (canonical) == our serialized dict
    assert _dump(sdk_task) == task


def test_send_response_parses_in_response_union(monkeypatch):
    task = _send(monkeypatch, user_input={"messages": [{"role": "user", "content": "hi"}]})
    envelope = S.make_response("rpc-1", task)            # our JSON-RPC success wrap
    resp = A.SendMessageResponse.model_validate(envelope)  # SDK parses the whole envelope
    assert type(resp.root).__name__ == "SendMessageSuccessResponse"
    assert isinstance(resp.root.result, A.Task)


# ── direction 2: SDK-constructed request → our handler ───────────────────────────

def test_sdk_built_request_dispatches_through_our_handler(monkeypatch):
    _install_mocks(monkeypatch, exec_script={"groq": ({"content": "hi"}, None, 7)},
                   auth_raises=None, deduct_ok=True)

    # Build the request with SDK objects, serialize to wire as a real peer would.
    msg = A.Message(
        message_id="m1", role=A.Role.user,
        parts=[A.Part(root=A.TextPart(text="chat hello")),
               A.Part(root=A.DataPart(data={"messages": [{"role": "user", "content": "hi"}]}))],
    )
    sdk_req = A.SendMessageRequest(id="r1", params=A.MessageSendParams(message=msg))
    wire = _dump(sdk_req)

    # Through OUR envelope parser + dispatcher.
    req_id, method, params = S.parse_request(wire)
    assert method is Method.SEND_MESSAGE and req_id == "r1"
    out = asyncio.run(a2a_router._dispatch(
        method, params, _RunDB([_candidate("groq")]), _FakeReq({}), req_id))

    # What we produced is a valid SDK Task carrying the run result.
    sdk_task = A.Task.model_validate(out)
    assert sdk_task.status.state == A.TaskState.completed
    assert sdk_task.artifacts[0].parts[0].root.data["result"] == {"content": "hi"}


# ── direction 1+2: SSE events → SDK streaming update events ───────────────────────

def test_stream_events_are_sdk_conformant(monkeypatch):
    envelopes = _collect_stream(monkeypatch, tokens=["Hel", "lo"])
    assert envelopes, "stream produced no SSE events"

    results = []
    for env in envelopes:
        # Each SSE frame is a full JSON-RPC streaming response the SDK can parse.
        parsed = A.SendStreamingMessageResponse.model_validate(env)
        results.append(parsed.root.result)

    # First frame: a non-terminal working status.
    assert isinstance(results[0], A.TaskStatusUpdateEvent)
    assert results[0].status.state == A.TaskState.working and results[0].final is False

    # Token frames: artifact-update events carrying the streamed text.
    arts = [r for r in results if isinstance(r, A.TaskArtifactUpdateEvent)]
    assert [r.artifact.parts[0].root.text for r in arts] == ["Hel", "lo"]
    assert all(r.append is True and r.last_chunk is False for r in arts)

    # Exactly one terminal frame, and it is the LAST: completed + final:true.
    finals = [r for r in results if isinstance(r, A.TaskStatusUpdateEvent) and r.final]
    assert len(finals) == 1
    assert finals[0].status.state == A.TaskState.completed
    assert isinstance(results[-1], A.TaskStatusUpdateEvent) and results[-1].final is True


# ── error mapping: our envelopes → SDK JSONRPCErrorResponse ──────────────────────

def test_http_error_mapping_matches_sdk(monkeypatch):
    from fastapi import HTTPException

    cases = {
        400: -32602,  # INVALID_PARAMS
        401: -32600,  # INVALID_REQUEST
        402: -32010,  # INSUFFICIENT_CREDITS (server-defined)
        422: -32602,  # INVALID_PARAMS
        429: -32011,  # RATE_LIMITED (server-defined)
        503: -32603,  # INTERNAL_ERROR
    }
    for status, expected_code in cases.items():
        err = a2a_router._http_to_jsonrpc(
            HTTPException(status_code=status, detail={"error": f"e{status}", "x": status}))
        envelope = S.make_error_response("rpc-1", err)
        sdk_err = A.JSONRPCErrorResponse.model_validate(envelope)   # SDK parses it
        assert sdk_err.error.code == expected_code
        assert sdk_err.error.data == {"error": f"e{status}", "x": status}  # detail preserved
        # also valid as the send-response error union
        assert type(A.SendMessageResponse.model_validate(envelope).root).__name__ == "JSONRPCErrorResponse"


def test_insufficient_credits_end_to_end_is_sdk_error(monkeypatch):
    # deduct refused → our dispatcher raises JsonRpcError → envelope parses in SDK.
    try:
        _send(monkeypatch, user_input={"messages": [{"role": "user", "content": "hi"}]},
              deduct_ok=False)
        assert False, "expected JsonRpcError for insufficient credits"
    except JsonRpcError as e:
        envelope = S.make_error_response("rpc-1", e)
        sdk_err = A.JSONRPCErrorResponse.model_validate(envelope)
        assert sdk_err.error.code == -32010   # INSUFFICIENT_CREDITS


def test_no_managed_service_end_to_end_is_invalid_params(monkeypatch):
    # 422 no_managed_service → INVALID_PARAMS, parses as an SDK error envelope.
    try:
        _send(monkeypatch, candidates=[_candidate("some-unmanaged-catalog-slug")],
              exec_script={})
        assert False, "expected JsonRpcError for no managed service"
    except JsonRpcError as e:
        envelope = S.make_error_response("rpc-1", e)
        sdk_err = A.JSONRPCErrorResponse.model_validate(envelope)
        assert sdk_err.error.code == -32602   # INVALID_PARAMS
        assert sdk_err.error.data.get("error") == "no_managed_service"
