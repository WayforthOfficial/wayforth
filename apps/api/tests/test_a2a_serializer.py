"""test_a2a_serializer.py — the A2A wire-isolation seam (target 0.9.2).

Proves the two properties the seam exists to guarantee:
  (1) nothing above the serializer carries a wire string — inbound BOTH v0.3.0 and
      v1.0 forms normalize to one internal vocabulary; outbound emits ONLY v0.3.0;
  (2) a future v1.0 flip is provably this one layer — every enum member has an OUT
      mapping, and no wire literal appears in any sibling a2a module (the leak guard).
"""
from __future__ import annotations

import pathlib

import pytest

from core.a2a import serializer as S
from core.a2a.serializer import (
    ErrorCode, JsonRpcError, Method, Role, TaskState, WIRE_PROTOCOL_VERSION,
)


# ── 0. the version we emit is truthful + singular ─────────────────────────────

def test_protocol_version_is_v030():
    assert WIRE_PROTOCOL_VERSION == "0.3.0"


# ── 1. outbound emits ONLY v0.3.0 wire forms ──────────────────────────────────

def test_methods_serialize_to_v030_slash_form():
    assert S.serialize_method(Method.SEND_MESSAGE) == "message/send"
    assert S.serialize_method(Method.STREAM_MESSAGE) == "message/stream"
    assert S.serialize_method(Method.GET_TASK) == "tasks/get"
    assert S.serialize_method(Method.CANCEL_TASK) == "tasks/cancel"
    assert S.serialize_method(Method.RESUBSCRIBE) == "tasks/resubscribe"
    assert S.serialize_method(Method.PUSH_CONFIG_SET) == "tasks/pushNotificationConfig/set"
    assert S.serialize_method(Method.GET_AUTHENTICATED_EXTENDED_CARD) == "agent/getAuthenticatedExtendedCard"


def test_roles_serialize_to_v030_lowercase():
    assert S.serialize_role(Role.USER) == "user"
    assert S.serialize_role(Role.AGENT) == "agent"


def test_states_serialize_to_v030_lowercase_hyphenated():
    # Exactly the values a2a-sdk==0.3.26 emits for Role/TaskState (sourced).
    assert S.serialize_state(TaskState.SUBMITTED) == "submitted"
    assert S.serialize_state(TaskState.INPUT_REQUIRED) == "input-required"
    assert S.serialize_state(TaskState.COMPLETED) == "completed"
    assert S.serialize_state(TaskState.CANCELED) == "canceled"
    assert S.serialize_state(TaskState.AUTH_REQUIRED) == "auth-required"


def test_outbound_never_emits_v1_forms():
    emitted = (
        [S.serialize_method(m) for m in Method]
        + [S.serialize_role(r) for r in Role]
        + [S.serialize_state(s) for s in TaskState]
    )
    for w in emitted:
        assert "ROLE_" not in w and "TASK_STATE_" not in w  # no proto-enum forms
        assert not (w[:1].isupper() and "/" not in w)        # no PascalCase methods


# ── 2. inbound accepts BOTH forms (Postel) → one internal vocabulary ──────────

@pytest.mark.parametrize("v030,v1,internal", [
    ("message/send", "SendMessage", Method.SEND_MESSAGE),
    ("message/stream", "SendStreamingMessage", Method.STREAM_MESSAGE),
    ("tasks/get", "GetTask", Method.GET_TASK),
    ("tasks/cancel", "CancelTask", Method.CANCEL_TASK),
    ("tasks/resubscribe", "SubscribeToTask", Method.RESUBSCRIBE),
    ("agent/getAuthenticatedExtendedCard", "GetExtendedAgentCard",
     Method.GET_AUTHENTICATED_EXTENDED_CARD),
])
def test_method_inbound_accepts_both(v030, v1, internal):
    assert S.deserialize_method(v030) is internal
    assert S.deserialize_method(v1) is internal


@pytest.mark.parametrize("v030,v1,internal", [
    ("user", "ROLE_USER", Role.USER),
    ("agent", "ROLE_AGENT", Role.AGENT),
])
def test_role_inbound_accepts_both(v030, v1, internal):
    assert S.deserialize_role(v030) is internal
    assert S.deserialize_role(v1) is internal


@pytest.mark.parametrize("v030,v1,internal", [
    ("completed", "TASK_STATE_COMPLETED", TaskState.COMPLETED),
    ("input-required", "TASK_STATE_INPUT_REQUIRED", TaskState.INPUT_REQUIRED),
    ("canceled", "TASK_STATE_CANCELED", TaskState.CANCELED),
    ("canceled", "TASK_STATE_CANCELLED", TaskState.CANCELED),  # UK spelling tolerated
])
def test_state_inbound_accepts_both(v030, v1, internal):
    assert S.deserialize_state(v030) is internal
    assert S.deserialize_state(v1) is internal


def test_unknown_state_degrades_not_raises():
    # A peer on a newer enum must not break us.
    assert S.deserialize_state("TASK_STATE_SOMETHING_NEW") is TaskState.UNKNOWN
    assert S.deserialize_state("brand-new") is TaskState.UNKNOWN


def test_unknown_method_raises_method_not_found():
    with pytest.raises(JsonRpcError) as e:
        S.deserialize_method("tasks/teleport")
    assert e.value.code is ErrorCode.METHOD_NOT_FOUND


# ── 3. round-trip: internal → v0.3.0 → internal is identity ───────────────────

def test_round_trip_all_members():
    for m in Method:
        assert S.deserialize_method(S.serialize_method(m)) is m
    for r in Role:
        assert S.deserialize_role(S.serialize_role(r)) is r
    for s in TaskState:
        assert S.deserialize_state(S.serialize_state(s)) is s


# ── 4. completeness: every internal member has an OUT mapping (flip-safety) ────

def test_every_member_has_outbound_mapping():
    # If a new enum member is added without wiring the OUT map, serialize_* raises
    # KeyError here — the flip layer can never be silently incomplete.
    for m in Method:
        assert isinstance(S.serialize_method(m), str)
    for r in Role:
        assert isinstance(S.serialize_role(r), str)
    for s in TaskState:
        assert isinstance(S.serialize_state(s), str)


# ── 5. error model: v0.3.0 plain JSON-RPC out; both shapes in ─────────────────

def test_serialize_error_is_plain_jsonrpc():
    err = JsonRpcError(ErrorCode.TASK_NOT_FOUND, data={"taskId": "t1"})
    wire = S.serialize_error(err)
    assert wire == {"code": -32001, "message": "Task not found", "data": {"taskId": "t1"}}
    # NOT a google.rpc.Status — no 'details', no 'status' wrapper.
    assert "details" not in wire and "status" not in wire


def test_deserialize_error_accepts_v030_and_v1_status():
    v030 = S.deserialize_error({"code": -32001, "message": "gone", "data": {"x": 1}})
    assert v030.code is ErrorCode.TASK_NOT_FOUND and v030.data == {"x": 1}
    # v1.0 google.rpc.Status: details fold into .data; unknown code → INTERNAL_ERROR.
    v1 = S.deserialize_error({"code": 7, "message": "denied", "details": [{"a": 1}]})
    assert v1.code is ErrorCode.INTERNAL_ERROR and v1.data == [{"a": 1}]


def test_make_response_and_error_envelopes():
    assert S.make_response("id1", {"ok": True}) == {
        "jsonrpc": "2.0", "id": "id1", "result": {"ok": True}}
    env = S.make_error_response("id2", JsonRpcError(ErrorCode.METHOD_NOT_FOUND))
    assert env["jsonrpc"] == "2.0" and env["id"] == "id2"
    assert env["error"]["code"] == -32601


# ── 6. parse_request: envelope validation, version-agnostic to the router ─────

def test_parse_request_resolves_internal_method():
    rid, method, params = S.parse_request(
        {"jsonrpc": "2.0", "id": 9, "method": "message/send", "params": {"x": 1}})
    assert rid == 9 and method is Method.SEND_MESSAGE and params == {"x": 1}
    # And the v1.0 spelling resolves to the SAME internal method.
    _, m2, _ = S.parse_request({"jsonrpc": "2.0", "id": 9, "method": "SendMessage"})
    assert m2 is Method.SEND_MESSAGE


def test_parse_request_rejects_bad_envelope():
    for bad in ({"method": "message/send"}, {"jsonrpc": "1.0", "method": "message/send"},
                {"jsonrpc": "2.0"}):
        with pytest.raises(JsonRpcError) as e:
            S.parse_request(bad)
        assert e.value.code is ErrorCode.INVALID_REQUEST


# ── 7. message envelope routes role through the seam ──────────────────────────

def test_message_round_trip_maps_role():
    wire = {"role": "ROLE_USER", "parts": [{"kind": "text", "text": "hi"}], "messageId": "m1"}
    internal = S.deserialize_message(wire)
    assert internal["role"] is Role.USER
    assert internal["parts"] == wire["parts"]      # structural fields pass through
    back = S.serialize_message(internal)
    assert back["role"] == "user"                  # always emits v0.3.0
    assert back["messageId"] == "m1"


def test_task_status_serializes_state():
    st = S.serialize_task_status(TaskState.COMPLETED, timestamp="2026-06-22T00:00:00Z")
    assert st == {"state": "completed", "timestamp": "2026-06-22T00:00:00Z"}


# ── 8. THE LEAK GUARD: every version-variant literal has exactly ONE authority ─

# Message *vocabulary* — methods/roles/states. Authority: serializer.py.
_MESSAGE_WIRE_LITERALS = [
    # v0.3.0 methods
    "message/send", "message/stream", "tasks/get", "tasks/cancel", "tasks/resubscribe",
    "tasks/pushNotificationConfig/", "agent/getAuthenticatedExtendedCard",
    # v1.0 methods
    "SendMessage", "SendStreamingMessage", "GetTask", "CancelTask", "SubscribeToTask",
    "GetExtendedAgentCard",
    # enum wire forms
    "ROLE_USER", "ROLE_AGENT", "TASK_STATE_",
]

# Agent Card *structure* — the field names that differ across versions.
# Authority: card.py. A v1.0 card-shape flip must be just as isolated as the
# message wire, so the structural variants are guarded the same way.
_CARD_SHAPE_LITERALS = [
    "supportedInterfaces",   # v1.0
    "additionalInterfaces",  # v0.3.0
    "preferredTransport",    # v0.3.0
    "protocolBinding",       # v1.0 interface field
]

# The advertised version string itself — must never be hardcoded; everyone
# imports WIRE_PROTOCOL_VERSION. Authority: serializer.py (its single definition).
# Quoted forms only, so prose like "v0.3.0" in a docstring doesn't trip it.
_VERSION_STRING_LITERALS = ['"0.3.0"', "'0.3.0'"]

# Per-file authority: a literal may appear ONLY in its authority module; anywhere
# else under core/a2a/ (or routers/a2a.py) is a leak.
_AUTHORITY: dict[str, list[str]] = {
    "serializer.py": _MESSAGE_WIRE_LITERALS + _VERSION_STRING_LITERALS,
    "card.py": _CARD_SHAPE_LITERALS,
}

_ALL_GUARDED_LITERALS = (
    _MESSAGE_WIRE_LITERALS + _CARD_SHAPE_LITERALS + _VERSION_STRING_LITERALS
)


def _a2a_source_files() -> list[pathlib.Path]:
    api_root = pathlib.Path(__file__).resolve().parent.parent
    files = list((api_root / "core" / "a2a").glob("*.py"))
    router = api_root / "routers" / "a2a.py"
    if router.exists():
        files.append(router)
    return files


def test_no_wire_string_leak():
    offenders: list[str] = []
    for path in _a2a_source_files():
        allowed = set(_AUTHORITY.get(path.name, []))
        text = path.read_text()
        for literal in _ALL_GUARDED_LITERALS:
            if literal in text and literal not in allowed:
                offenders.append(f"{path.name}: {literal!r}")
    assert not offenders, (
        "A2A version-variant literal leaked outside its authority module — the seam "
        "is broken. Route message vocabulary through serializer.py and card structure "
        "through card.py; import WIRE_PROTOCOL_VERSION, never hardcode it:\n  "
        + "\n  ".join(offenders)
    )
