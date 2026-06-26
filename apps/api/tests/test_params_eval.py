"""test_params_eval.py — agent params v1, Step 3 (authoritative evaluator).

The security-critical gate: coercion table, hidden-fields-dropped, required_if
composition, validation, unknown-param rejection. Server is sole authority.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.params_schema import validate_schema
from core.params_eval import validate_and_resolve

TODAY = date(2024, 6, 15)


def _c(fields):
    return validate_schema(fields)


def _resolve(fields, values, today=TODAY):
    return validate_and_resolve(_c(fields), values, today=today)


def _codes(errors):
    return {(e["field"], e["code"]) for e in errors}


# ── coercion table ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (5, 5), (5.0, 5), ("5", 5), ("-3", -3),
])
def test_integer_coercion_ok(raw, expected):
    r, e = _resolve([{"name": "n", "type": "integer"}], {"n": raw})
    assert e == [] and r["n"] == expected


@pytest.mark.parametrize("raw", [5.5, "5.5", "abc", True])
def test_integer_coercion_rejects_non_integer_no_floor(raw):
    r, e = _resolve([{"name": "n", "type": "integer"}], {"n": raw})
    assert ("n", "type") in _codes(e) and "n" not in r   # rejected, never floored


@pytest.mark.parametrize("raw,expected", [(5, 5), (5.5, 5.5), ("5.5", 5.5), ("7", 7)])
def test_number_coercion_ok(raw, expected):
    r, e = _resolve([{"name": "n", "type": "number"}], {"n": raw})
    assert e == [] and r["n"] == expected


def test_number_rejects_bool_and_garbage():
    assert ("n", "type") in _codes(_resolve([{"name": "n", "type": "number"}], {"n": True})[1])
    assert ("n", "type") in _codes(_resolve([{"name": "n", "type": "number"}], {"n": "x"})[1])


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False), ("true", True), ("FALSE", False),
])
def test_boolean_coercion_ok(raw, expected):
    r, e = _resolve([{"name": "b", "type": "boolean"}], {"b": raw})
    assert e == [] and r["b"] is expected


def test_boolean_rejects_other_strings():
    assert ("b", "type") in _codes(_resolve([{"name": "b", "type": "boolean"}], {"b": "yes"})[1])


def test_string_rejects_non_string():
    assert ("s", "type") in _codes(_resolve([{"name": "s", "type": "string"}], {"s": 5})[1])


def test_date_format():
    assert _resolve([{"name": "d", "type": "date"}], {"d": "2024-01-02"})[1] == []
    assert ("d", "type") in _codes(_resolve([{"name": "d", "type": "date"}], {"d": "01/02/2024"})[1])


# ── absent / defaults / required ─────────────────────────────────────────────────

def test_empty_string_is_absent_default_applies():
    r, e = _resolve([{"name": "x", "type": "string", "default": "hi"}], {"x": ""})
    assert e == [] and r["x"] == "hi"


def test_missing_required_errors():
    r, e = _resolve([{"name": "x", "type": "string", "required": True}], {})
    assert ("x", "required") in _codes(e)


def test_absent_optional_omitted():
    r, e = _resolve([{"name": "x", "type": "string"}], {})
    assert e == [] and "x" not in r


def test_date_default_today_resolves():
    r, e = _resolve([{"name": "d", "type": "date", "default": "today"}], {})
    assert e == [] and r["d"] == "2024-06-15"


# ── hidden fields dropped (not validated, not defaulted) ─────────────────────────

HIDE = [
    {"name": "mode", "type": "enum", "default": "basic",
     "validation": {"options": [{"value": "basic", "label": "B"},
                                {"value": "adv", "label": "A"}]}},
    {"name": "extra", "type": "integer", "required": True, "default": 99,
     "show_if": {"field": "mode", "equals": "adv"}},
]


def test_hidden_required_field_not_required():
    r, e = _resolve(HIDE, {"mode": "basic"})        # extra hidden
    assert e == [] and "extra" not in r              # not required, default NOT applied


def test_hidden_field_invalid_value_not_validated():
    # even a garbage value for a hidden field is ignored (dropped)
    r, e = _resolve(HIDE, {"mode": "basic", "extra": "not-an-int"})
    assert e == [] and "extra" not in r


def test_visible_field_then_validated():
    r, e = _resolve(HIDE, {"mode": "adv", "extra": 3})
    assert e == [] and r["extra"] == 3
    # visible + required + absent → required error (default IS applied here though)
    r2, e2 = _resolve(HIDE, {"mode": "adv"})
    assert e2 == [] and r2["extra"] == 99            # default applies when visible


# ── required_if composition ─────────────────────────────────────────────────────

REQIF = [
    {"name": "interval", "type": "enum", "default": "daily",
     "validation": {"options": [{"value": "daily", "label": "D"},
                                {"value": "intraday", "label": "I"}]}},
    {"name": "res", "type": "string",
     "show_if": {"field": "interval", "equals": "intraday"},
     "required_if": {"field": "interval", "equals": "intraday"}},
]


def test_required_if_fires_only_when_visible_and_predicate_true():
    # intraday → res visible + required, absent → error
    assert ("res", "required") in _codes(_resolve(REQIF, {"interval": "intraday"})[1])
    # intraday + provided → ok
    assert _resolve(REQIF, {"interval": "intraday", "res": "5min"})[1] == []
    # daily → res hidden → not required, dropped
    r, e = _resolve(REQIF, {"interval": "daily"})
    assert e == [] and "res" not in r


# ── validation rules ─────────────────────────────────────────────────────────────

def test_regex_and_length():
    f = [{"name": "t", "type": "string", "validation": {"regex": "^[A-Z]{1,5}$", "maxLength": 5}}]
    assert _resolve(f, {"t": "AAPL"})[1] == []
    assert ("t", "regex") in _codes(_resolve(f, {"t": "aapl"})[1])


def test_number_bounds():
    f = [{"name": "n", "type": "integer", "validation": {"min": 1, "max": 10}}]
    assert ("n", "max") in _codes(_resolve(f, {"n": 11})[1])
    assert ("n", "min") in _codes(_resolve(f, {"n": 0})[1])


def test_enum_membership():
    f = [{"name": "e", "type": "enum",
          "validation": {"options": [{"value": "x", "label": "X"}]}}]
    assert ("e", "enum") in _codes(_resolve(f, {"e": "z"})[1])


def test_date_max_today_and_cross_field():
    f = [
        {"name": "start", "type": "date"},
        {"name": "end", "type": "date", "validation": {"min_field": "start", "max": "today"}},
    ]
    # end before start → min_field error
    assert ("end", "min_field") in _codes(
        _resolve(f, {"start": "2024-03-01", "end": "2024-02-01"})[1])
    # end after today → max error
    assert ("end", "max") in _codes(_resolve(f, {"start": "2024-01-01", "end": "2024-12-31"})[1])
    # ok
    assert _resolve(f, {"start": "2024-01-01", "end": "2024-02-01"})[1] == []


# ── unknown params + body shape ─────────────────────────────────────────────────

def test_unknown_param_rejected():
    assert ("ghost", "unknown_param") in _codes(
        _resolve([{"name": "x", "type": "string"}], {"x": "a", "ghost": 1})[1])


def test_non_dict_body_rejected():
    r, e = validate_and_resolve(_c([{"name": "x", "type": "string"}]), ["nope"])
    assert e and e[0]["code"] == "invalid_body"


# ── full Stock-like flow ─────────────────────────────────────────────────────────

def test_full_stock_flow_resolves_visible_only():
    fields = [
        {"name": "ticker", "type": "string", "required": True,
         "validation": {"regex": "^[A-Z]{1,5}$"}},
        {"name": "interval", "type": "enum", "default": "daily",
         "validation": {"options": [{"value": "daily", "label": "D"},
                                    {"value": "intraday", "label": "I"}]}},
        {"name": "res", "type": "enum",
         "show_if": {"field": "interval", "equals": "intraday"},
         "required_if": {"field": "interval", "equals": "intraday"},
         "validation": {"options": [{"value": "5min", "label": "5"}]}},
        {"name": "use_range", "type": "boolean", "default": False},
        {"name": "start", "type": "date",
         "show_if": {"field": "use_range", "equals": True},
         "required_if": {"field": "use_range", "equals": True}},
    ]
    r, e = _resolve(fields, {"ticker": "AAPL", "interval": "intraday", "res": "5min"})
    assert e == []
    assert r == {"ticker": "AAPL", "interval": "intraday", "res": "5min", "use_range": False}
    # 'start' dropped (use_range default False → hidden)
    assert "start" not in r
