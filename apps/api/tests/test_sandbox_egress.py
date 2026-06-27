"""test_sandbox_egress.py — code-editing v1, Step 2 (run-sandbox egress lock).

Flag-gated cutover: when AGENT_GATEWAY_EGRESS_ENABLED is on (and AGENT_BASE_IMAGE is
set), a PYTHON run gets gateway-only egress + the base image + NO run-time pip. Flag
off is byte-identical to today (deny-list + pip). node is never locked in v1. Falls
back safely if the base image is unset.
"""
from __future__ import annotations

import pytest

import services.sandbox as sb


@pytest.fixture
def capture(monkeypatch):
    cap = {"writes": {}}

    class _Res:
        stdout, stderr, exit_code = "ok", "", 0

    class _Sbx:
        sandbox_id = "sb-1"

        def __init__(self):
            self.files = self
            self.commands = self

        def write(self, path, content):
            cap["writes"][path] = content

        def run(self, cmd, timeout=None):
            cap["cmd"] = cmd
            return _Res()

        def kill(self):
            pass

    def _create(**kwargs):
        cap["create_kwargs"] = kwargs
        return _Sbx()

    monkeypatch.setattr("e2b.Sandbox.create", staticmethod(_create))
    # default-off, no base image, no leftover gateway host override
    monkeypatch.delenv("AGENT_GATEWAY_EGRESS_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_BASE_IMAGE", raising=False)
    return cap


def _run(runtime="python3.12"):
    return sb.E2BSandboxProvider()._run_sync("print(1)", runtime, {}, 60)


# ── flag OFF — byte-identical to today ──────────────────────────────────────────

def test_flag_off_uses_denylist_and_pip(capture):
    _run("python3.12")
    net = capture["create_kwargs"]["network"]
    assert net["deny_out"] == sb._DENY_EGRESS and "allow_out" not in net
    assert "template" not in capture["create_kwargs"]
    assert "pip install" in capture["cmd"]


# ── flag ON + base image — gateway-only, base template, no pip ───────────────────

def test_flag_on_python_locked(capture, monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_EGRESS_ENABLED", "1")
    monkeypatch.setenv("AGENT_BASE_IMAGE", "team/wayforth-agent-base-v1:default")
    _run("python3.12")
    ck = capture["create_kwargs"]
    assert ck["network"]["deny_out"] == ["0.0.0.0/0"]
    assert ck["network"]["allow_out"] == ["gateway.wayforth.io"]
    assert ck["template"] == "team/wayforth-agent-base-v1:default"
    assert capture["cmd"] == "python3 /home/user/agent.py"        # NO pip
    assert "pip install" not in capture["cmd"]


# ── flag ON + node — NOT locked in v1 ───────────────────────────────────────────

def test_flag_on_node_not_locked(capture, monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_EGRESS_ENABLED", "1")
    monkeypatch.setenv("AGENT_BASE_IMAGE", "team/base:default")
    _run("node20")
    ck = capture["create_kwargs"]
    assert ck["network"]["deny_out"] == sb._DENY_EGRESS          # node keeps deny-list
    assert "template" not in ck
    assert "npm install" in capture["cmd"]


# ── flag ON but base image UNSET — safe fallback (never break a run) ─────────────

def test_flag_on_without_base_image_falls_back(capture, monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_EGRESS_ENABLED", "1")
    monkeypatch.delenv("AGENT_BASE_IMAGE", raising=False)
    _run("python3.12")
    ck = capture["create_kwargs"]
    assert ck["network"]["deny_out"] == sb._DENY_EGRESS          # fell back to deny-list
    assert "template" not in ck
    assert "pip install" in capture["cmd"]                       # and to pip


@pytest.mark.parametrize("val", ["0", "false", "no", "", "off"])
def test_flag_values_treated_as_off(capture, monkeypatch, val):
    monkeypatch.setenv("AGENT_GATEWAY_EGRESS_ENABLED", val)
    monkeypatch.setenv("AGENT_BASE_IMAGE", "team/base:default")
    _run("python3.12")
    assert capture["create_kwargs"]["network"]["deny_out"] == sb._DENY_EGRESS
