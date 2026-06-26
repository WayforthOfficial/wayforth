"""test_templates_params.py — agent params v1: template-deploy extraction guard.

The launch-blocker: a deployed template must show its form, so its PARAMS must
extract cleanly. Since templates are FIRST-PARTY content, an invalid PARAMS is a ship
blocker — caught here at test time, not at user deploy (deploy fails soft + logs).
"""
from __future__ import annotations

import pytest

from core.params_schema import ParamsSchemaError, compile_params
from services.templates import TEMPLATES


def test_all_bundled_templates_python_params_compile():
    """Every bundled template's python code must compile (no PARAMS → None is fine;
    a PARAMS block must be a valid schema). A failure here means a template would
    deploy without its form — fix the template before shipping."""
    failures = []
    for tid, t in TEMPLATES.items():
        for runtime, code in (t.get("code") or {}).items():
            if not runtime.startswith("python"):
                continue
            try:
                compile_params(code, "python")
            except ParamsSchemaError as e:
                failures.append(f"{tid}/{runtime}: {e}")
    assert not failures, "bundled templates with invalid PARAMS:\n" + "\n".join(failures)


def test_template_with_params_block_extracts_a_schema():
    # Mirrors what deploy_template extracts: a python template declaring PARAMS yields
    # a compiled schema (so the deployed agent renders a form).
    code = (
        'import os, json\n'
        'PARAMS = [{"name": "ticker", "type": "string", "required": True}]\n'
        'p = json.loads(os.environ.get("WAYFORTH_PARAMS", "{}"))\n'
    )
    schema = compile_params(code, "python")
    assert schema is not None
    assert [f["name"] for f in schema["fields"]] == ["ticker"]
