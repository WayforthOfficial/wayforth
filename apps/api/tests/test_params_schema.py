"""test_params_schema.py — agent parameters v1, Step 1 (schema engine).

Heavily exercises the REJECTIONS (cycle, unknown ref, self-ref, dupe, malformed,
non-literal extraction) as well as the happy path. Pure — no DB, no app.
"""
from __future__ import annotations

import pytest

from core import params_schema as ps
from core.params_schema import ParamsSchemaError, compile_params, extract_params, validate_schema

STOCK_CODE = '''
import os, json
PARAMS = [
  {"name": "ticker", "type": "string", "label": "Ticker", "required": True,
   "validation": {"regex": "^[A-Z]{1,5}$", "maxLength": 5}},
  {"name": "interval", "type": "enum", "label": "Interval", "required": True,
   "default": "daily",
   "validation": {"options": [
     {"value": "daily", "label": "Daily"},
     {"value": "weekly", "label": "Weekly"},
     {"value": "intraday", "label": "Intraday"}]}},
  {"name": "intraday_minutes", "type": "enum", "label": "Resolution",
   "show_if": {"field": "interval", "equals": "intraday"},
   "required_if": {"field": "interval", "equals": "intraday"},
   "validation": {"options": [
     {"value": "1min", "label": "1 min"}, {"value": "5min", "label": "5 min"}]}},
  {"name": "use_date_range", "type": "boolean", "label": "Date range", "default": False},
  {"name": "start_date", "type": "date", "label": "Start",
   "show_if": {"field": "use_date_range", "equals": True},
   "required_if": {"field": "use_date_range", "equals": True},
   "validation": {"max": "today"}},
  {"name": "end_date", "type": "date", "label": "End",
   "show_if": {"field": "use_date_range", "equals": True},
   "validation": {"min_field": "start_date", "max": "today"}},
]
params = json.loads(os.environ.get("WAYFORTH_PARAMS", "{}"))
'''


# ── happy path ──────────────────────────────────────────────────────────────────

def test_stock_schema_compiles():
    compiled = compile_params(STOCK_CODE)
    assert compiled is not None
    names = [f["name"] for f in compiled["fields"]]
    assert names[:2] == ["ticker", "interval"]
    # dependents come AFTER their dependency in topo order
    order = compiled["order"]
    assert order.index("interval") < order.index("intraday_minutes")
    assert order.index("use_date_range") < order.index("start_date")
    # deps captured from show_if/required_if
    assert compiled["deps"]["intraday_minutes"] == ["interval"]
    assert compiled["deps"]["start_date"] == ["use_date_range"]


def test_no_params_returns_none():
    assert compile_params("import os\nx = 1\n") is None


def test_normalization_defaults():
    c = compile_params('PARAMS = [{"name": "a", "type": "string"}]')
    f = c["fields"][0]
    assert f["label"] == "a" and f["required"] is False and f["default"] is None


# ── extraction: static, no execution ────────────────────────────────────────────

def test_non_literal_params_rejected_function_call():
    with pytest.raises(ParamsSchemaError, match="pure literal"):
        extract_params('PARAMS = build_params()')


def test_non_literal_params_rejected_name_ref():
    with pytest.raises(ParamsSchemaError, match="pure literal"):
        extract_params('SHARED = []\nPARAMS = SHARED')


def test_non_literal_params_rejected_comprehension():
    with pytest.raises(ParamsSchemaError, match="pure literal"):
        extract_params('PARAMS = [x for x in range(3)]')


def test_syntax_error_code_rejected():
    with pytest.raises(ParamsSchemaError, match="could not parse"):
        extract_params('PARAMS = [')


def test_unsupported_language_rejected():
    with pytest.raises(ParamsSchemaError, match="not supported for language"):
        extract_params('const PARAMS = []', language="node")


def test_params_must_be_top_level():
    # PARAMS assigned inside a function is not a top-level decl → treated as absent
    assert compile_params('def f():\n    PARAMS = [1]\n    return PARAMS\n') is None


# ── malformed root / field ──────────────────────────────────────────────────────

def test_root_not_list():
    with pytest.raises(ParamsSchemaError, match="must be a list"):
        validate_schema({"name": "a"})


def test_field_not_object():
    with pytest.raises(ParamsSchemaError, match="must be an object"):
        validate_schema(["nope"])


def test_missing_name():
    with pytest.raises(ParamsSchemaError, match="invalid or missing 'name'"):
        validate_schema([{"type": "string"}])


def test_bad_name_format():
    with pytest.raises(ParamsSchemaError, match="invalid or missing 'name'"):
        validate_schema([{"name": "Bad-Name", "type": "string"}])


def test_missing_or_bad_type():
    with pytest.raises(ParamsSchemaError, match="invalid or missing 'type'"):
        validate_schema([{"name": "a", "type": "array"}])


def test_unknown_field_key_rejected():
    with pytest.raises(ParamsSchemaError, match="unknown keys"):
        validate_schema([{"name": "a", "type": "string", "lable": "typo"}])


def test_duplicate_name_rejected():
    with pytest.raises(ParamsSchemaError, match="duplicate field name 'a'"):
        validate_schema([{"name": "a", "type": "string"}, {"name": "a", "type": "number"}])


# ── validation block ────────────────────────────────────────────────────────────

def test_invalid_validation_key_for_type():
    with pytest.raises(ParamsSchemaError, match="invalid validation keys"):
        validate_schema([{"name": "a", "type": "string", "validation": {"min": 1}}])


def test_bad_regex_rejected():
    with pytest.raises(ParamsSchemaError, match="regex is invalid"):
        validate_schema([{"name": "a", "type": "string", "validation": {"regex": "([a-z"}}])


def test_minlen_gt_maxlen_rejected():
    with pytest.raises(ParamsSchemaError, match="minLength > maxLength"):
        validate_schema([{"name": "a", "type": "string",
                          "validation": {"minLength": 5, "maxLength": 2}}])


def test_number_min_gt_max_rejected():
    with pytest.raises(ParamsSchemaError, match="min > max"):
        validate_schema([{"name": "a", "type": "number", "validation": {"min": 9, "max": 1}}])


def test_enum_without_options_rejected():
    with pytest.raises(ParamsSchemaError, match="requires validation.options"):
        validate_schema([{"name": "a", "type": "enum"}])


def test_enum_duplicate_option_rejected():
    with pytest.raises(ParamsSchemaError, match="duplicate option value"):
        validate_schema([{"name": "a", "type": "enum", "validation": {"options": [
            {"value": "x", "label": "X"}, {"value": "x", "label": "X2"}]}}])


def test_default_not_in_enum_rejected():
    with pytest.raises(ParamsSchemaError, match="not one of its enum options"):
        validate_schema([{"name": "a", "type": "enum", "default": "z",
                          "validation": {"options": [{"value": "x", "label": "X"}]}}])


def test_default_type_mismatch_rejected():
    with pytest.raises(ParamsSchemaError, match="default must be an integer"):
        validate_schema([{"name": "a", "type": "integer", "default": 1.5}])


def test_date_bad_min_rejected():
    with pytest.raises(ParamsSchemaError, match="ISO date"):
        validate_schema([{"name": "a", "type": "date", "validation": {"min": "01/02/2024"}}])


# ── predicate validation ────────────────────────────────────────────────────────

def test_predicate_unknown_field_rejected():
    with pytest.raises(ParamsSchemaError, match="references unknown field 'ghost'"):
        validate_schema([{"name": "a", "type": "string",
                          "show_if": {"field": "ghost", "equals": 1}}])


def test_predicate_self_reference_rejected():
    with pytest.raises(ParamsSchemaError, match="cannot reference itself"):
        validate_schema([{"name": "a", "type": "string",
                          "show_if": {"field": "a", "equals": 1}}])


def test_predicate_unknown_operator_rejected():
    with pytest.raises(ParamsSchemaError, match="exactly one operator"):
        validate_schema([{"name": "a", "type": "string"},
                         {"name": "b", "type": "string",
                          "show_if": {"field": "a", "matches": "x"}}])


def test_predicate_in_requires_list():
    with pytest.raises(ParamsSchemaError, match="value must be a list"):
        validate_schema([{"name": "a", "type": "string"},
                         {"name": "b", "type": "string",
                          "show_if": {"field": "a", "in": "x"}}])


def test_predicate_combinator_collects_refs_and_validates():
    c = validate_schema([
        {"name": "a", "type": "string"},
        {"name": "b", "type": "string"},
        {"name": "c", "type": "string",
         "show_if": {"all_of": [{"field": "a", "not_empty": True},
                                {"field": "b", "equals": "x"}]}},
    ])
    assert c["deps"]["c"] == ["a", "b"]


def test_predicate_empty_combinator_rejected():
    with pytest.raises(ParamsSchemaError, match="must be a non-empty list"):
        validate_schema([{"name": "a", "type": "string"},
                         {"name": "b", "type": "string", "show_if": {"any_of": []}}])


# ── cross-field validation refs ─────────────────────────────────────────────────

def test_min_field_unknown_rejected():
    with pytest.raises(ParamsSchemaError, match="references unknown field 'nope'"):
        validate_schema([{"name": "a", "type": "date", "validation": {"min_field": "nope"}}])


def test_min_field_self_rejected():
    with pytest.raises(ParamsSchemaError, match="cannot reference itself"):
        validate_schema([{"name": "a", "type": "date", "validation": {"min_field": "a"}}])


# ── cycle detection ─────────────────────────────────────────────────────────────

def test_direct_cycle_rejected():
    with pytest.raises(ParamsSchemaError, match="cyclic .* a, b"):
        validate_schema([
            {"name": "a", "type": "string", "show_if": {"field": "b", "equals": 1}},
            {"name": "b", "type": "string", "show_if": {"field": "a", "equals": 1}},
        ])


def test_indirect_cycle_rejected():
    with pytest.raises(ParamsSchemaError, match="cyclic"):
        validate_schema([
            {"name": "a", "type": "string", "show_if": {"field": "b", "equals": 1}},
            {"name": "b", "type": "string", "show_if": {"field": "c", "equals": 1}},
            {"name": "c", "type": "string", "show_if": {"field": "a", "equals": 1}},
        ])


def test_cycle_across_show_if_and_required_if():
    # a.show_if depends on b; b.required_if depends on a → still a cycle
    with pytest.raises(ParamsSchemaError, match="cyclic"):
        validate_schema([
            {"name": "a", "type": "string", "show_if": {"field": "b", "equals": 1}},
            {"name": "b", "type": "string", "required_if": {"field": "a", "equals": 1}},
        ])


def test_diamond_dependency_is_not_a_cycle():
    # a → b, a → c, b → d, c → d : a DAG, must compile with valid topo order
    c = validate_schema([
        {"name": "d", "type": "string"},
        {"name": "b", "type": "string", "show_if": {"field": "d", "not_empty": True}},
        {"name": "c", "type": "string", "show_if": {"field": "d", "not_empty": True}},
        {"name": "a", "type": "string",
         "show_if": {"all_of": [{"field": "b", "not_empty": True},
                                {"field": "c", "not_empty": True}]}},
    ])
    order = c["order"]
    assert order.index("d") < order.index("b") < order.index("a")
    assert order.index("d") < order.index("c") < order.index("a")
