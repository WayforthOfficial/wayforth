"""core/params_eval.py — agent parameters v1: authoritative dispatch-time evaluator.

Given a compiled schema (core.params_schema) and a dict of raw user values, this is
the SERVER-SIDE authority: it resolves field visibility, applies defaults, coerces
types, enforces validation + required-ness, and returns the resolved params injected
into the run. The dashboard mirrors this for UX only; this module is the gate and is
never bypassed (the run dispatch always calls it). Pure: no DB, no FastAPI.

    validate_and_resolve(compiled, raw_values, today=None) -> (resolved, errors)
        resolved: {name: value} for VISIBLE fields only (hidden dropped, defaults
                  applied, types coerced). errors: [{field, code, message}].
        A non-empty errors list means the dispatch must be rejected (422).

Type-coercion table (raw form/JSON value → typed value; "" and None are ABSENT,
handled before coercion so defaults/required apply):
    string  : must be str.
    integer : int (not bool); float only if integral (5.0→5); str via int(), or a
              numeric str that is integral; NON-INTEGER IS REJECTED, never floored.
    number  : int/float (not bool); numeric str → int if integral-looking else float.
    boolean : bool, or the strings "true"/"false" (case-insensitive).
    date    : str matching YYYY-MM-DD.
    enum    : value must equal one of the declared option values (exact match).
"""
from __future__ import annotations

from datetime import date

import re

from core.params_schema import PREDICATE_COMBINATORS, _is_iso_date


def _is_absent(v) -> bool:
    return v is None or v == ""


# ── predicate evaluation ────────────────────────────────────────────────────────

def _eval_predicate(pred: dict, values: dict) -> bool:
    comb = set(pred) & PREDICATE_COMBINATORS
    if comb:
        c = comb.pop()
        if c == "all_of":
            return all(_eval_predicate(p, values) for p in pred[c])
        if c == "any_of":
            return any(_eval_predicate(p, values) for p in pred[c])
        return not _eval_predicate(pred[c], values)  # "not"

    field = pred["field"]
    op = (set(pred) - {"field"}).pop()
    actual = values.get(field)
    if op == "not_empty":
        return not _is_absent(actual)
    if op == "is_empty":
        return _is_absent(actual)
    target = pred[op]
    if op == "equals":
        return actual == target
    if op == "not_equals":
        return actual != target
    if op == "in":
        return actual in target
    if op == "not_in":
        return actual not in target
    if _is_absent(actual):
        return False  # gt/gte/lt/lte against an absent value is never true
    try:
        if op == "gt":
            return actual > target
        if op == "gte":
            return actual >= target
        if op == "lt":
            return actual < target
        if op == "lte":
            return actual <= target
    except TypeError:
        return False
    return False


# ── type coercion (the explicit table) ──────────────────────────────────────────

def _coerce(ftype: str, raw):
    """Return (value, error_message_or_None). raw is guaranteed non-absent."""
    if ftype == "string":
        return (raw, None) if isinstance(raw, str) else (None, "must be a string")

    if ftype == "integer":
        if isinstance(raw, bool):
            return None, "must be an integer"
        if isinstance(raw, int):
            return raw, None
        if isinstance(raw, float):
            return (int(raw), None) if raw.is_integer() else (None, "must be a whole number")
        if isinstance(raw, str):
            try:
                f = float(raw)
            except ValueError:
                return None, "must be an integer"
            return (int(f), None) if f.is_integer() else (None, "must be a whole number")
        return None, "must be an integer"

    if ftype == "number":
        if isinstance(raw, bool):
            return None, "must be a number"
        if isinstance(raw, (int, float)):
            return raw, None
        if isinstance(raw, str):
            try:
                f = float(raw)
            except ValueError:
                return None, "must be a number"
            return (int(f) if ("." not in raw and "e" not in raw.lower() and f.is_integer())
                    else f), None
        return None, "must be a number"

    if ftype == "boolean":
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true", None
        return None, "must be true or false"

    if ftype == "date":
        if isinstance(raw, str) and _is_iso_date(raw):
            return raw, None
        return None, "must be a date (YYYY-MM-DD)"

    # enum — membership checked in _validate_value; pass the raw value through.
    return raw, None


def _resolve_date_token(tok, today):
    if tok is None:
        return None
    if tok == "today":
        return (today or date.today()).isoformat()
    return tok


def _validate_value(f: dict, value, resolved: dict, today):
    """Return (code, message) on failure, else None."""
    v = f["validation"]
    t = f["type"]

    if t == "string":
        if "minLength" in v and len(value) < v["minLength"]:
            return ("minLength", f"must be at least {v['minLength']} characters")
        if "maxLength" in v and len(value) > v["maxLength"]:
            return ("maxLength", f"must be at most {v['maxLength']} characters")
        if "regex" in v and re.search(v["regex"], value) is None:
            return ("regex", "has an invalid format")

    elif t in ("number", "integer"):
        if "min" in v and value < v["min"]:
            return ("min", f"must be at least {v['min']}")
        if "max" in v and value > v["max"]:
            return ("max", f"must be at most {v['max']}")

    elif t == "enum":
        if value not in {o["value"] for o in v.get("options", [])}:
            return ("enum", "is not a valid option")

    elif t == "date":
        lo = _resolve_date_token(v.get("min"), today)
        hi = _resolve_date_token(v.get("max"), today)
        if lo is not None and value < lo:
            return ("min", f"must be on or after {lo}")
        if hi is not None and value > hi:
            return ("max", f"must be on or before {hi}")
        if "min_field" in v:
            other = resolved.get(v["min_field"])
            if other is not None and value < other:
                return ("min_field", f"must be on or after {v['min_field']}")
        if "max_field" in v:
            other = resolved.get(v["max_field"])
            if other is not None and value > other:
                return ("max_field", f"must be on or before {v['max_field']}")
    return None


# ── the authority ───────────────────────────────────────────────────────────────

def validate_and_resolve(compiled: dict, raw_values, today=None):
    """Resolve + validate user input against a compiled schema. The single gate."""
    if raw_values is None:
        raw_values = {}
    if not isinstance(raw_values, dict):
        return {}, [{"field": None, "code": "invalid_body", "message": "params must be an object"}]

    fields = {f["name"]: f for f in compiled["fields"]}
    errors: list = []

    for k in raw_values:
        if k not in fields:
            errors.append({"field": k, "code": "unknown_param",
                           "message": f"unknown parameter '{k}'"})

    resolved: dict = {}
    for name in compiled["order"]:
        f = fields[name]

        # 1) visibility — hidden fields are dropped entirely (no default, no validate)
        if f.get("show_if") is not None and not _eval_predicate(f["show_if"], resolved):
            continue

        raw = raw_values.get(name)

        # 2) absent (None / "") → default (visible only), else required check
        if _is_absent(raw):
            dflt = f.get("default")
            if dflt is not None:
                if f["type"] == "date" and dflt == "today":
                    dflt = (today or date.today()).isoformat()
                resolved[name] = dflt
                continue
            required = f["required"] or (
                f.get("required_if") is not None and _eval_predicate(f["required_if"], resolved))
            if required:
                errors.append({"field": name, "code": "required",
                               "message": f"'{f['label']}' is required"})
            continue

        # 3) present → coerce → validate
        value, cerr = _coerce(f["type"], raw)
        if cerr:
            errors.append({"field": name, "code": "type", "message": f"'{f['label']}' {cerr}"})
            continue
        verr = _validate_value(f, value, resolved, today)
        if verr:
            errors.append({"field": name, "code": verr[0], "message": f"'{f['label']}' {verr[1]}"})
            continue
        resolved[name] = value

    return resolved, errors
