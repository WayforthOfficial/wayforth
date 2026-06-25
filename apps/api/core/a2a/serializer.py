"""core/a2a/serializer.py — THE single A2A wire-format seam.

═══════════════════════════════════════════════════════════════════════════════
THE WIRE-ISOLATION BOUNDARY.
Every version-specific string in Wayforth's A2A surface lives HERE and ONLY here:
  • JSON-RPC method names      (message/send … vs SendMessage …)
  • message Role values        ("user"/"agent" vs ROLE_USER/ROLE_AGENT)
  • Task state values          ("completed" vs TASK_STATE_COMPLETED)
  • the JSON-RPC error model    (plain {code,message} vs google.rpc.Status)
  • protocolVersion            (the value we advertise + echo)

Everything above this module — the card builder, the JSON-RPC router, the client —
speaks the internal, version-agnostic enums below (Method / Role / TaskState /
ErrorCode) and NEVER sees a wire spelling. If a wire string appears anywhere
outside this file, the seam has leaked. test_a2a_serializer.py::test_no_wire_string_leak
enforces that mechanically.
═══════════════════════════════════════════════════════════════════════════════

DECISION (approved 2026-06-22), sourced:
  • EMIT v0.3.0 — slash-form methods, lowercase Role/TaskState, plain JSON-RPC
    errors. It is the form the deployed ecosystem (AWS AgentCore protocolVersion
    0.3.0, LangChain, IBM) actually parses. Confirmed against a real reference
    impl: a2a-sdk==0.3.26 emits Role ∈ {"user","agent"} and TaskState ∈
    {"submitted","working","input-required","completed","canceled","failed", …}.
  • ACCEPT BOTH v0.3.0 and v1.0 forms inbound (Postel's law) — so a v1.0
    (PascalCase + ROLE_*/TASK_STATE_* + google.rpc.Status) caller still interops.
  • protocolVersion = "0.3.0" — truthful for what we emit, not aspirational.

  v1.0 (latest spec, a2aproject/A2A docs/specification.md §9.4) genuinely uses
  PascalCase methods + ROLE_USER/TASK_STATE_* enums + google.rpc.Status. We do
  not emit it yet because the ecosystem isn't there. This is a deliberate posture,
  not a misread of the spec.

─── FLIP TEST: what moving OUTPUT to v1.0 costs ───────────────────────────────
Change ONLY, all within this file:
  1. the three OUT maps — _METHOD_OUT / _ROLE_OUT / _STATE_OUT,
  2. serialize_error()  (plain JSON-RPC  →  google.rpc.Status shape),
  3. WIRE_PROTOCOL_VERSION  ("0.3.0" → "1.0").
The inbound tables keep accepting BOTH forms (still correct). No caller changes —
router/client/card never named a wire string, so there is nothing else to edit.
That single-layer property is the whole point of this module; guard it.
"""
from __future__ import annotations

from enum import Enum, auto


# The wire version we EMIT. Single source of truth for the Agent Card's
# protocolVersion and any echo. One half of the flip (the other is the OUT maps).
WIRE_PROTOCOL_VERSION = "0.3.0"


# ════════════════════════════════════════════════════════════════════════════
# Internal, version-agnostic vocabulary. Callers use ONLY these. Plain Enum (not
# str-Enum) on purpose: a member's .value is meaningless to the wire and can never
# be accidentally serialized — only the OUT maps below produce wire strings.
# ════════════════════════════════════════════════════════════════════════════

class Method(Enum):
    SEND_MESSAGE = auto()
    STREAM_MESSAGE = auto()
    GET_TASK = auto()
    CANCEL_TASK = auto()
    RESUBSCRIBE = auto()
    PUSH_CONFIG_SET = auto()
    PUSH_CONFIG_GET = auto()
    PUSH_CONFIG_LIST = auto()
    PUSH_CONFIG_DELETE = auto()
    GET_AUTHENTICATED_EXTENDED_CARD = auto()


class Role(Enum):
    USER = auto()
    AGENT = auto()


class TaskState(Enum):
    SUBMITTED = auto()
    WORKING = auto()
    INPUT_REQUIRED = auto()
    COMPLETED = auto()
    CANCELED = auto()
    FAILED = auto()
    REJECTED = auto()
    AUTH_REQUIRED = auto()
    UNKNOWN = auto()


class ErrorCode(Enum):
    # Standard JSON-RPC 2.0 (shared by v0.3.0 + v1.0).
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # A2A-specific (v0.3.0 §8 error codes).
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
    UNSUPPORTED_OPERATION = -32004
    CONTENT_TYPE_NOT_SUPPORTED = -32005
    INVALID_AGENT_RESPONSE = -32006
    # Wayforth server-defined, in the JSON-RPC reserved server-error range
    # (-32000..-32099). A2A has no payment/rate-limit error; these carry our
    # /run 402 and 429 conditions. They validate as a generic JSON-RPC error
    # (a2a-sdk JSONRPCError accepts any int code) — verified by the interop gate.
    INSUFFICIENT_CREDITS = -32010
    RATE_LIMITED = -32011


class JsonRpcError(Exception):
    """Version-agnostic A2A error. Raise this anywhere above the seam with an
    internal ErrorCode; serialize_error() renders it to the wire error model.
    Callers never construct a wire error dict themselves."""

    def __init__(self, code: ErrorCode, message: str | None = None, data: object = None):
        self.code = code
        self.message = message or _DEFAULT_ERROR_MESSAGE.get(code, "Error")
        self.data = data
        super().__init__(f"{code.name}: {self.message}")


_DEFAULT_ERROR_MESSAGE: dict[ErrorCode, str] = {
    ErrorCode.PARSE_ERROR: "Invalid JSON payload",
    ErrorCode.INVALID_REQUEST: "Invalid JSON-RPC request",
    ErrorCode.METHOD_NOT_FOUND: "Method not found",
    ErrorCode.INVALID_PARAMS: "Invalid method parameters",
    ErrorCode.INTERNAL_ERROR: "Internal error",
    ErrorCode.TASK_NOT_FOUND: "Task not found",
    ErrorCode.TASK_NOT_CANCELABLE: "Task cannot be canceled",
    ErrorCode.PUSH_NOTIFICATION_NOT_SUPPORTED: "Push Notification is not supported",
    ErrorCode.UNSUPPORTED_OPERATION: "This operation is not supported",
    ErrorCode.CONTENT_TYPE_NOT_SUPPORTED: "Incompatible content types",
    ErrorCode.INVALID_AGENT_RESPONSE: "Invalid agent response",
    ErrorCode.INSUFFICIENT_CREDITS: "Insufficient credits",
    ErrorCode.RATE_LIMITED: "Rate limit exceeded",
}


# ════════════════════════════════════════════════════════════════════════════
# INBOUND tables: BOTH v0.3.0 and v1.0 wire forms → internal (Postel). Accepting
# both is permanent and version-independent; it does NOT change on a flip.
# ════════════════════════════════════════════════════════════════════════════

_METHOD_IN: dict[str, Method] = {
    # v0.3.0 slash-form (what we emit; what AgentCore/LangChain/IBM speak)
    "message/send": Method.SEND_MESSAGE,
    "message/stream": Method.STREAM_MESSAGE,
    "tasks/get": Method.GET_TASK,
    "tasks/cancel": Method.CANCEL_TASK,
    "tasks/resubscribe": Method.RESUBSCRIBE,
    "tasks/pushNotificationConfig/set": Method.PUSH_CONFIG_SET,
    "tasks/pushNotificationConfig/get": Method.PUSH_CONFIG_GET,
    "tasks/pushNotificationConfig/list": Method.PUSH_CONFIG_LIST,
    "tasks/pushNotificationConfig/delete": Method.PUSH_CONFIG_DELETE,
    "agent/getAuthenticatedExtendedCard": Method.GET_AUTHENTICATED_EXTENDED_CARD,
    # v1.0 PascalCase (accepted inbound, never emitted)
    "SendMessage": Method.SEND_MESSAGE,
    "SendStreamingMessage": Method.STREAM_MESSAGE,
    "GetTask": Method.GET_TASK,
    "CancelTask": Method.CANCEL_TASK,
    "SubscribeToTask": Method.RESUBSCRIBE,
    "TaskSubscription": Method.RESUBSCRIBE,
    "CreateTaskPushNotificationConfig": Method.PUSH_CONFIG_SET,
    "GetTaskPushNotificationConfig": Method.PUSH_CONFIG_GET,
    "ListTaskPushNotificationConfig": Method.PUSH_CONFIG_LIST,
    "DeleteTaskPushNotificationConfig": Method.PUSH_CONFIG_DELETE,
    "GetExtendedAgentCard": Method.GET_AUTHENTICATED_EXTENDED_CARD,
}

_ROLE_IN: dict[str, Role] = {
    "user": Role.USER, "agent": Role.AGENT,            # v0.3.0
    "ROLE_USER": Role.USER, "ROLE_AGENT": Role.AGENT,  # v1.0
}

_STATE_IN: dict[str, TaskState] = {
    # v0.3.0 lowercase / hyphenated
    "submitted": TaskState.SUBMITTED,
    "working": TaskState.WORKING,
    "input-required": TaskState.INPUT_REQUIRED,
    "completed": TaskState.COMPLETED,
    "canceled": TaskState.CANCELED,
    "failed": TaskState.FAILED,
    "rejected": TaskState.REJECTED,
    "auth-required": TaskState.AUTH_REQUIRED,
    "unknown": TaskState.UNKNOWN,
    # v1.0 proto-enum form (accept both US/UK 'cancel(l)ed' spellings defensively)
    "TASK_STATE_SUBMITTED": TaskState.SUBMITTED,
    "TASK_STATE_WORKING": TaskState.WORKING,
    "TASK_STATE_INPUT_REQUIRED": TaskState.INPUT_REQUIRED,
    "TASK_STATE_COMPLETED": TaskState.COMPLETED,
    "TASK_STATE_CANCELED": TaskState.CANCELED,
    "TASK_STATE_CANCELLED": TaskState.CANCELED,
    "TASK_STATE_FAILED": TaskState.FAILED,
    "TASK_STATE_REJECTED": TaskState.REJECTED,
    "TASK_STATE_AUTH_REQUIRED": TaskState.AUTH_REQUIRED,
    "TASK_STATE_UNKNOWN": TaskState.UNKNOWN,
}


# ════════════════════════════════════════════════════════════════════════════
# OUTBOUND maps: internal → v0.3.0 ONLY.  ◄── THE FLIP POINT ──►
# To emit v1.0 instead, these three maps (+ serialize_error + WIRE_PROTOCOL_VERSION)
# are the entire change. Nothing outside this file references a wire string.
# ════════════════════════════════════════════════════════════════════════════

_METHOD_OUT: dict[Method, str] = {
    Method.SEND_MESSAGE: "message/send",
    Method.STREAM_MESSAGE: "message/stream",
    Method.GET_TASK: "tasks/get",
    Method.CANCEL_TASK: "tasks/cancel",
    Method.RESUBSCRIBE: "tasks/resubscribe",
    Method.PUSH_CONFIG_SET: "tasks/pushNotificationConfig/set",
    Method.PUSH_CONFIG_GET: "tasks/pushNotificationConfig/get",
    Method.PUSH_CONFIG_LIST: "tasks/pushNotificationConfig/list",
    Method.PUSH_CONFIG_DELETE: "tasks/pushNotificationConfig/delete",
    Method.GET_AUTHENTICATED_EXTENDED_CARD: "agent/getAuthenticatedExtendedCard",
}

_ROLE_OUT: dict[Role, str] = {
    Role.USER: "user",
    Role.AGENT: "agent",
}

_STATE_OUT: dict[TaskState, str] = {
    TaskState.SUBMITTED: "submitted",
    TaskState.WORKING: "working",
    TaskState.INPUT_REQUIRED: "input-required",
    TaskState.COMPLETED: "completed",
    TaskState.CANCELED: "canceled",
    TaskState.FAILED: "failed",
    TaskState.REJECTED: "rejected",
    TaskState.AUTH_REQUIRED: "auth-required",
    TaskState.UNKNOWN: "unknown",
}


# ════════════════════════════════════════════════════════════════════════════
# Scalar mappers. deserialize_* accept either wire form; serialize_* emit v0.3.0.
# ════════════════════════════════════════════════════════════════════════════

def deserialize_method(wire: str) -> Method:
    try:
        return _METHOD_IN[wire]
    except KeyError:
        raise JsonRpcError(ErrorCode.METHOD_NOT_FOUND, f"Unknown method: {wire!r}")


def serialize_method(method: Method) -> str:
    return _METHOD_OUT[method]


def deserialize_role(wire: str) -> Role:
    try:
        return _ROLE_IN[wire]
    except KeyError:
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, f"Unknown message role: {wire!r}")


def serialize_role(role: Role) -> str:
    return _ROLE_OUT[role]


def deserialize_state(wire: str) -> TaskState:
    # Unknown/forward-compatible states degrade to UNKNOWN rather than erroring —
    # a peer on a newer enum must not break our client.
    return _STATE_IN.get(wire, TaskState.UNKNOWN)


def serialize_state(state: TaskState) -> str:
    return _STATE_OUT[state]


# ════════════════════════════════════════════════════════════════════════════
# JSON-RPC envelope + error model. The router/client build internal results and
# call these — they never assemble a {jsonrpc:...} or error dict by hand.
# ════════════════════════════════════════════════════════════════════════════

def parse_request(body: dict) -> tuple[object, Method, dict]:
    """Validate a JSON-RPC 2.0 request envelope and resolve its method to the
    internal Method enum. Returns (id, method, params). Raises JsonRpcError
    (INVALID_REQUEST / METHOD_NOT_FOUND) — the router renders it via
    make_error_response and never inspects wire method strings itself."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        raise JsonRpcError(ErrorCode.INVALID_REQUEST)
    method = deserialize_method(body["method"])
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, "params must be an object")
    return body.get("id"), method, params


def make_response(req_id: object, result: dict) -> dict:
    """Wrap an already-serialized (v0.3.0) result in a JSON-RPC success envelope."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def serialize_error(err: JsonRpcError) -> dict:
    """Render an internal JsonRpcError to the v0.3.0 wire error model: a plain
    JSON-RPC error object. ◄── FLIP POINT: v1.0 emits a google.rpc.Status shape."""
    out: dict = {"code": err.code.value, "message": err.message}
    if err.data is not None:
        out["data"] = err.data
    return out


def make_error_response(req_id: object, err: JsonRpcError) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": serialize_error(err)}


def deserialize_error(wire: dict) -> JsonRpcError:
    """Normalize a peer's error (when we are the client) to an internal
    JsonRpcError. Accepts BOTH the v0.3.0 plain JSON-RPC error {code,message,data}
    and the v1.0 google.rpc.Status {code,message,details} shapes — both carry an
    int code + message; details/data fold into .data."""
    raw_code = wire.get("code")
    try:
        code = ErrorCode(raw_code)
    except (ValueError, TypeError):
        code = ErrorCode.INTERNAL_ERROR
    data = wire.get("data", wire.get("details"))
    return JsonRpcError(code, wire.get("message"), data)


# ════════════════════════════════════════════════════════════════════════════
# Message / Task envelope mapping. The only version-variant fields are the role
# and task state enums; everything structural (parts, ids, metadata) is identical
# across v0.3.0 and v1.0 and passes through untouched. Routing role/state through
# the maps above is what keeps the variance contained to this file.
# ════════════════════════════════════════════════════════════════════════════

def deserialize_message(wire: dict) -> dict:
    """Wire message (either version) → internal: role becomes a Role enum, parts
    pass through. Callers read msg['role'] as Role, never a string."""
    msg = dict(wire)
    if "role" in msg and msg["role"] is not None:
        msg["role"] = deserialize_role(msg["role"])
    return msg


def serialize_message(internal: dict) -> dict:
    """Internal message → v0.3.0 wire: Role enum → "user"/"agent"."""
    msg = dict(internal)
    if isinstance(msg.get("role"), Role):
        msg["role"] = serialize_role(msg["role"])
    return msg


def serialize_task_status(state: TaskState, *, timestamp: str | None = None,
                          message: dict | None = None) -> dict:
    """Build a v0.3.0 TaskStatus object from an internal TaskState. message, when
    present, is an already-internal message and is serialized through the seam."""
    status: dict = {"state": serialize_state(state)}
    if timestamp is not None:
        status["timestamp"] = timestamp
    if message is not None:
        status["message"] = serialize_message(message)
    return status


# ════════════════════════════════════════════════════════════════════════════
# Task / Artifact / streaming-event builders. The wire 'kind' discriminators
# ("task", "artifact", "status-update", "artifact-update") and the camelCase
# field aliases (contextId/taskId/artifactId) are version-variant wire vocabulary
# and therefore live HERE, never in the router. Each builder's output is byte-
# parseable by a2a-sdk==0.3.26 (Task / Artifact / TaskStatusUpdateEvent /
# TaskArtifactUpdateEvent) — the interop gate asserts the round-trip both ways.
# ════════════════════════════════════════════════════════════════════════════

def make_text_part(text: str) -> dict:
    """v0.3.0 TextPart."""
    return {"kind": "text", "text": text}


def make_data_part(data: dict) -> dict:
    """v0.3.0 DataPart."""
    return {"kind": "data", "data": data}


def make_artifact(artifact_id: str, parts: list, *, name: str | None = None) -> dict:
    """v0.3.0 Artifact. parts are already-serialized Part dicts."""
    artifact: dict = {"artifactId": artifact_id, "parts": parts}
    if name is not None:
        artifact["name"] = name
    return artifact


def make_task(*, task_id: str, context_id: str, state: TaskState,
              artifacts: list | None = None, message: dict | None = None,
              timestamp: str | None = None) -> dict:
    """v0.3.0 Task envelope. status carries the terminal TaskState; artifacts carry
    the run output (each an already-serialized Artifact). message, when present, is
    an internal message serialized into status.message through the seam."""
    task: dict = {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": serialize_task_status(state, timestamp=timestamp, message=message),
    }
    if artifacts:
        task["artifacts"] = artifacts
    return task


def make_status_update_event(*, task_id: str, context_id: str, state: TaskState,
                             final: bool, message: dict | None = None,
                             timestamp: str | None = None) -> dict:
    """v0.3.0 TaskStatusUpdateEvent. final=True marks the terminal frame of a stream
    (the spec requires the last event be a status-update with a terminal state)."""
    return {
        "kind": "status-update",
        "taskId": task_id,
        "contextId": context_id,
        "status": serialize_task_status(state, timestamp=timestamp, message=message),
        "final": final,
    }


def make_artifact_update_event(*, task_id: str, context_id: str, artifact: dict,
                               append: bool, last_chunk: bool) -> dict:
    """v0.3.0 TaskArtifactUpdateEvent — streams incremental artifact content.
    append=True chains onto the named artifact; last_chunk closes it."""
    return {
        "kind": "artifact-update",
        "taskId": task_id,
        "contextId": context_id,
        "artifact": artifact,
        "append": append,
        "lastChunk": last_chunk,
    }
