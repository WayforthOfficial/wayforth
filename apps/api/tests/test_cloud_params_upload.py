"""test_cloud_params_upload.py — agent params v1, Step 2 (extract-on-upload wiring).

Tests the decision _params_schema_for_upload makes for PUT /cloud/agents/{id}/code:
python agents → compile (or reject); node agents → skipped (no schema). The schema
validation itself is covered exhaustively in test_params_schema (Step 1).
"""
from __future__ import annotations

import pytest

from core.params_schema import ParamsSchemaError
import routers.cloud as cloud

VALID = '''
PARAMS = [
  {"name": "ticker", "type": "string", "required": True,
   "validation": {"regex": "^[A-Z]{1,5}$"}},
  {"name": "interval", "type": "enum", "default": "daily",
   "validation": {"options": [{"value": "daily", "label": "D"},
                              {"value": "intraday", "label": "I"}]}},
  {"name": "res", "type": "string",
   "show_if": {"field": "interval", "equals": "intraday"}},
]
'''

CYCLE = '''
PARAMS = [
  {"name": "a", "type": "string", "show_if": {"field": "b", "equals": 1}},
  {"name": "b", "type": "string", "show_if": {"field": "a", "equals": 1}},
]
'''


def test_python_valid_params_compiled():
    schema = cloud._params_schema_for_upload("python3.12", VALID)
    assert schema is not None
    names = [f["name"] for f in schema["fields"]]
    assert names == ["ticker", "interval", "res"]
    assert schema["order"].index("interval") < schema["order"].index("res")


def test_python_no_params_returns_none():
    assert cloud._params_schema_for_upload("python3.12", "import os\nx = 1\n") is None


def test_python_invalid_params_raises():
    with pytest.raises(ParamsSchemaError, match="cyclic"):
        cloud._params_schema_for_upload("python3.12", CYCLE)


def test_python_syntax_error_raises():
    # Upload of unparseable Python is rejected (mapped to 422 by the endpoint).
    with pytest.raises(ParamsSchemaError, match="could not parse"):
        cloud._params_schema_for_upload("python3.12", "PARAMS = [")


def test_node_runtime_skips_extraction():
    # node agents get no schema in v1 — even if the code contains a PARAMS-looking block.
    assert cloud._params_schema_for_upload("node20", "const PARAMS = [{x:1}]") is None
    assert cloud._params_schema_for_upload("node20", VALID) is None
