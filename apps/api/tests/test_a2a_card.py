"""test_a2a_card.py — Agent Card structure authority (target 0.9.2).

Mirrors the serializer's contract for card *shape*: emit v0.3.0, parse BOTH
v0.3.0 and v1.0, and never hardcode the protocol version.
"""
from __future__ import annotations

from core.a2a import card as C
from core.a2a.serializer import WIRE_PROTOCOL_VERSION


_SKILLS = [{"id": "echo", "name": "Echo", "description": "echoes", "tags": ["demo"]}]


def _build() -> dict:
    return C.build_agent_card(
        name="Wayforth Gateway",
        description="Managed API execution agent",
        url="https://gateway.wayforth.io/a2a",
        version="0.9.2",
        skills=_SKILLS,
    )


# ── outbound: emits the v0.3.0 card shape ─────────────────────────────────────

def test_build_emits_v030_transport_shape():
    card = _build()
    assert card["url"] == "https://gateway.wayforth.io/a2a"
    assert card["preferredTransport"] == "JSONRPC"
    # v0.3.0 uses url+preferredTransport, NOT v1.0 supportedInterfaces.
    assert "supportedInterfaces" not in card


def test_protocol_version_sourced_from_serializer_not_hardcoded():
    # The advertised version is exactly the serializer's single constant.
    assert _build()["protocolVersion"] == WIRE_PROTOCOL_VERSION == "0.3.0"


def test_empty_ap2_extension_slot_present():
    caps = _build()["capabilities"]
    assert caps["streaming"] is True
    assert caps["extensions"] == []   # ready for an AP2 AgentExtension, no schema change


def test_security_scheme_is_api_key_header():
    card = _build()
    scheme = card["securitySchemes"]["wayforth-api-key"]
    assert scheme == {"type": "apiKey", "in": "header", "name": "X-Wayforth-API-Key"}
    assert card["security"] == [{"wayforth-api-key": []}]


def test_skills_and_modes_passthrough():
    card = _build()
    assert card["skills"] == _SKILLS
    assert "text/plain" in card["defaultInputModes"]
    assert "application/json" in card["defaultOutputModes"]


def test_build_has_no_signatures_block():
    # build is pure/unsigned; sign.py attaches signatures[] later.
    assert "signatures" not in _build()


# ── inbound: parse accepts BOTH card shapes (Postel) ──────────────────────────

def test_parse_v030_card_resolves_endpoint():
    wire = {
        "protocolVersion": "0.3.0", "name": "Peer", "description": "d",
        "url": "https://peer.example/a2a", "preferredTransport": "JSONRPC",
        "skills": [], "capabilities": {"streaming": True},
    }
    view = C.parse_agent_card(wire)
    assert view["endpoint"] == "https://peer.example/a2a"
    assert view["transport"] == "JSONRPC"
    assert view["protocol_version"] == "0.3.0"
    assert view["raw"] is wire   # raw retained for signature verification


def test_parse_v1_card_resolves_endpoint():
    wire = {
        "protocolVersion": "1.0", "name": "Peer", "description": "d",
        "supportedInterfaces": [
            {"url": "https://peer.example/grpc", "protocolBinding": "GRPC"},
            {"url": "https://peer.example/a2a", "protocolBinding": "JSONRPC"},
        ],
        "skills": [],
    }
    view = C.parse_agent_card(wire)
    # Picks the JSON-RPC interface out of a multi-transport v1.0 card.
    assert view["endpoint"] == "https://peer.example/a2a"
    assert view["transport"] == "JSONRPC"


def test_parse_prefers_jsonrpc_among_v030_additional_interfaces():
    wire = {
        "url": "https://peer.example/grpc", "preferredTransport": "GRPC",
        "additionalInterfaces": [
            {"url": "https://peer.example/a2a", "transport": "JSONRPC"},
        ],
        "name": "Peer",
    }
    view = C.parse_agent_card(wire)
    assert view["endpoint"] == "https://peer.example/a2a"
    assert view["transport"] == "JSONRPC"


def test_round_trip_build_then_parse():
    built = _build()
    view = C.parse_agent_card(built)
    assert view["endpoint"] == built["url"]
    assert view["transport"] == "JSONRPC"
    assert view["protocol_version"] == WIRE_PROTOCOL_VERSION
    assert view["skills"] == _SKILLS
