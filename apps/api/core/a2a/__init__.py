"""core/a2a — Wayforth's A2A (Agent2Agent) interop surface.

The wire-format version lives in ONE place: core.a2a.serializer. Everything else
in this package (card builder, JSON-RPC router, client, key management) speaks the
internal, version-agnostic vocabulary that serializer.py defines and never touches
a wire string. See serializer.py's module docstring for the flip-cost contract.
"""
