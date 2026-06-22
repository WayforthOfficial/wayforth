"""core/a2a/card.py — THE Agent Card structure authority.

The card-shape analog of serializer.py. Where the serializer is the only place
message *vocabulary* (methods/roles/states/errors) is decided, THIS module is the
only place Agent Card *structure* is decided — the field names that differ across
A2A versions:

    v0.3.0 (emit) : url + preferredTransport + additionalInterfaces[]
    v1.0          : supportedInterfaces[] (each url + protocolBinding)

It mirrors the serializer's split:
  • build_agent_card(...)  → emits the v0.3.0 card shape (outbound, single form);
  • parse_agent_card(wire) → accepts BOTH v0.3.0 and v1.0 card shapes (inbound,
    Postel) and returns an internal, version-agnostic view the client uses.

protocolVersion is NOT hardcoded here — it is imported from the serializer
(WIRE_PROTOCOL_VERSION), so the version we advertise has exactly one source.

FLIP TEST (move the emitted card to v1.0 shape): change ONLY build_agent_card's
output field names here (+ the serializer's WIRE_PROTOCOL_VERSION). parse stays
(still accepts both); no router/client edit, because they read the internal view
from parse_agent_card and never name a card field. test_no_wire_string_leak
enforces that the structural field literals appear in NO other a2a module.

The signed `signatures[]` block is attached by core/a2a/sign.py, not here — this
module produces the canonical unsigned card that signing canonicalizes (JCS) and
covers. Keeping build pure (no key access) keeps signing's input deterministic.
"""
from __future__ import annotations

from core.a2a.serializer import WIRE_PROTOCOL_VERSION

# The well-known path the card is served at (RFC 8615). Same in v0.3.0 and v1.0,
# so it is not a version-variant literal.
WELL_KNOWN_CARD_PATH = "/.well-known/agent-card.json"

# v0.3.0 TransportProtocol value for JSON-RPC. (GRPC / HTTP+JSON deferred.)
_TRANSPORT_JSONRPC = "JSONRPC"


def build_agent_card(
    *,
    name: str,
    description: str,
    url: str,
    version: str,
    skills: list[dict],
    security_scheme_name: str = "wayforth-api-key",
    api_key_header: str = "X-Wayforth-API-Key",
    default_input_modes: list[str] | None = None,
    default_output_modes: list[str] | None = None,
    streaming: bool = True,
    documentation_url: str | None = None,
    provider: dict | None = None,
) -> dict:
    """Build Wayforth's unsigned v0.3.0 Agent Card.

    `url` is the JSON-RPC endpoint (preferredTransport=JSONRPC). `capabilities.
    extensions` is an empty AP2 slot, ready for a payment extension without a
    schema change. Returns a plain dict; sign.py attaches `signatures[]`.
    """
    card: dict = {
        # Single source of the advertised version — imported, never literal.
        "protocolVersion": WIRE_PROTOCOL_VERSION,
        "name": name,
        "description": description,
        # v0.3.0 transport shape: a single preferred endpoint + its transport.
        "url": url,
        "preferredTransport": _TRANSPORT_JSONRPC,
        "version": version,
        "capabilities": {
            "streaming": streaming,
            # Empty AP2 slot. AgentExtension entries (uri/description/required/
            # params) drop in here later with no card-schema change.
            "extensions": [],
        },
        "securitySchemes": {
            security_scheme_name: {
                "type": "apiKey",
                "in": "header",
                "name": api_key_header,
            },
        },
        "security": [{security_scheme_name: []}],
        "defaultInputModes": default_input_modes or ["text/plain", "application/json"],
        "defaultOutputModes": default_output_modes or ["text/plain", "application/json"],
        "skills": skills,
    }
    if documentation_url is not None:
        card["documentationUrl"] = documentation_url
    if provider is not None:
        card["provider"] = provider
    return card


def parse_agent_card(wire: dict) -> dict:
    """Normalize a remote Agent Card (v0.3.0 OR v1.0 shape) to an internal view.

    Resolves the JSON-RPC endpoint regardless of which transport shape the peer
    used — v0.3.0 `url`+`preferredTransport`(+`additionalInterfaces[]`) or v1.0
    `supportedInterfaces[]` (each `url`+`protocolBinding`). The client reads
    `endpoint` / `transport` from this view and never inspects a card field.
    """
    interfaces = _collect_interfaces(wire)
    jsonrpc = next((i for i in interfaces if _is_jsonrpc(i["transport"])), None)
    chosen = jsonrpc or (interfaces[0] if interfaces else None)
    return {
        "name": wire.get("name"),
        "description": wire.get("description"),
        "protocol_version": wire.get("protocolVersion"),
        "endpoint": chosen["url"] if chosen else wire.get("url"),
        "transport": chosen["transport"] if chosen else None,
        "interfaces": interfaces,
        "capabilities": wire.get("capabilities") or {},
        "skills": wire.get("skills") or [],
        "security_schemes": wire.get("securitySchemes") or {},
        "signatures": wire.get("signatures") or [],
        # Raw card retained so signature verification covers the bytes as received.
        "raw": wire,
    }


def _collect_interfaces(wire: dict) -> list[dict]:
    """Flatten either card transport shape into a uniform [{url, transport}]."""
    out: list[dict] = []
    # v0.3.0: a primary url + preferredTransport, plus additionalInterfaces[].
    if wire.get("url"):
        out.append({
            "url": wire["url"],
            "transport": wire.get("preferredTransport") or _TRANSPORT_JSONRPC,
        })
    for itf in wire.get("additionalInterfaces") or []:
        if isinstance(itf, dict) and itf.get("url"):
            out.append({"url": itf["url"], "transport": itf.get("transport")})
    # v1.0: supportedInterfaces[] with url + protocolBinding.
    for itf in wire.get("supportedInterfaces") or []:
        if isinstance(itf, dict) and itf.get("url"):
            out.append({"url": itf["url"], "transport": itf.get("protocolBinding")})
    return out


def _is_jsonrpc(transport: str | None) -> bool:
    if not transport:
        return False
    t = transport.strip().lower().replace("-", "").replace("_", "")
    return t in ("jsonrpc", "jsonrpc2", "jsonrpc20")
