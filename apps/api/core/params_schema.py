"""core/params_schema.py — agent parameters v1: static schema extraction + validation.

An agent declares a top-level ``PARAMS`` literal in its code (single source of truth).
This module extracts it WITHOUT executing the agent (ast.literal_eval only — a hard
security boundary, especially with user-editable code coming), validates it against the
v1 meta-schema, builds the show_if/required_if dependency graph, and rejects cycles /
unknown refs / self-refs / dupes / malformed schemas at load time.

Pure: no DB, no FastAPI, no I/O. Nothing imports it yet (Step 1 is inert). Runtime
value evaluation/coercion + injection are Step 3; this module only concerns the SCHEMA.

Public API:
    compile_params(code, language="python") -> dict | None
        None when the agent declares no PARAMS (backward-compatible).
        Otherwise {"fields": [...normalized...], "order": [topo names], "deps": {...}}.
    extract_params(code, language) -> raw literal | None
    validate_schema(raw) -> compiled dict
    ParamsSchemaError — raised on any malformed/invalid schema, with a clear message.
"""
from __future__ import annotations

import ast
import re
from datetime import datetime

TYPES = {"string", "number", "integer", "boolean", "enum", "date"}

FIELD_KEYS = {
    "name", "type", "label", "help", "required", "default",
    "validation", "show_if", "required_if",
}

VALIDATION_KEYS_BY_TYPE = {
    "string":  {"minLength", "maxLength", "regex"},
    "number":  {"min", "max"},
    "integer": {"min", "max"},
    "boolean": set(),
    "enum":    {"options"},
    "date":    {"min", "max", "min_field", "max_field"},
}

PREDICATE_OPS = {
    "equals", "not_equals", "in", "not_in",
    "not_empty", "is_empty", "gt", "gte", "lt", "lte",
}
_OPS_NO_VALUE = {"not_empty", "is_empty"}
PREDICATE_COMBINATORS = {"all_of", "any_of", "not"}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ParamsSchemaError(Exception):
    """Raised when an agent's PARAMS schema is malformed or invalid (load-time)."""


# ── extraction (no execution) ───────────────────────────────────────────────────

def extract_params(code: str, language: str = "python"):
    """Return the raw PARAMS literal, or None if the agent declares none.

    Python: parse the AST, find the top-level ``PARAMS = <literal>`` assignment, and
    ast.literal_eval it. NEVER executes the code. A non-literal PARAMS (function call,
    name reference, f-string, comprehension) is rejected.
    """
    if language not in ("python",):
        raise ParamsSchemaError(
            f"params extraction is not supported for language '{language}' yet")
    try:
        tree = ast.parse(code or "")
    except SyntaxError as e:
        raise ParamsSchemaError(f"could not parse agent code: {e}")

    node = None
    for stmt in tree.body:  # top-level statements only
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "PARAMS":
                    node = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                and stmt.target.id == "PARAMS":
            node = stmt.value

    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        raise ParamsSchemaError(
            "PARAMS must be a pure literal (no function calls, names, f-strings, "
            "or comprehensions)")


# ── meta-schema validation ──────────────────────────────────────────────────────

def validate_schema(raw) -> dict:
    """Validate a raw PARAMS literal and return the compiled schema.

    Raises ParamsSchemaError on: non-list root, malformed field, bad name/type,
    unknown keys, duplicate names, invalid validation, bad predicate, unknown/self
    field reference, or a show_if/required_if dependency cycle.
    """
    if not isinstance(raw, list):
        raise ParamsSchemaError("PARAMS must be a list of field objects")

    fields, names = [], []
    for i, item in enumerate(raw):
        f = _validate_field(item, i)
        if f["name"] in names:
            raise ParamsSchemaError(f"duplicate field name '{f['name']}'")
        names.append(f["name"])
        fields.append(f)

    name_set = set(names)
    deps: dict[str, set] = {n: set() for n in names}

    for f in fields:
        for key in ("show_if", "required_if"):
            pred = f.get(key)
            if pred is not None:
                deps[f["name"]] |= _validate_predicate(pred, name_set, f["name"], key)
        # cross-field validation refs (date ordering) — must exist, not self
        for vk in ("min_field", "max_field"):
            ref = f["validation"].get(vk)
            if ref is not None:
                if ref not in name_set:
                    raise ParamsSchemaError(
                        f"field '{f['name']}' validation.{vk} references unknown field '{ref}'")
                if ref == f["name"]:
                    raise ParamsSchemaError(
                        f"field '{f['name']}' validation.{vk} cannot reference itself")

    order = _topo_order(names, deps)
    return {"fields": fields, "order": order,
            "deps": {k: sorted(v) for k, v in deps.items()}}


def compile_params(code: str, language: str = "python"):
    """Extract + validate. None if the agent declares no PARAMS."""
    raw = extract_params(code, language)
    if raw is None:
        return None
    return validate_schema(raw)


# ── field validation ────────────────────────────────────────────────────────────

def _validate_field(item, i: int) -> dict:
    if not isinstance(item, dict):
        raise ParamsSchemaError(f"field #{i} must be an object, got {type(item).__name__}")
    unknown = set(item) - FIELD_KEYS
    if unknown:
        raise ParamsSchemaError(f"field #{i} has unknown keys: {', '.join(sorted(unknown))}")

    name = item.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ParamsSchemaError(
            f"field #{i} has invalid or missing 'name' (must match ^[a-z][a-z0-9_]*$): {name!r}")
    ftype = item.get("type")
    if ftype not in TYPES:
        raise ParamsSchemaError(
            f"field '{name}' has invalid or missing 'type': {ftype!r} (allowed: {sorted(TYPES)})")

    for k in ("label", "help"):
        if k in item and item[k] is not None and not isinstance(item[k], str):
            raise ParamsSchemaError(f"field '{name}' {k} must be a string")
    if "required" in item and not isinstance(item["required"], bool):
        raise ParamsSchemaError(f"field '{name}' required must be a boolean")

    validation = item.get("validation") or {}
    if not isinstance(validation, dict):
        raise ParamsSchemaError(f"field '{name}' validation must be an object")
    _validate_validation(name, ftype, validation)

    if item.get("default") is not None:
        _validate_default(name, ftype, item["default"], validation)

    for k in ("show_if", "required_if"):
        if item.get(k) is not None and not isinstance(item[k], dict):
            raise ParamsSchemaError(f"field '{name}' {k} must be a predicate object")

    return {
        "name": name,
        "type": ftype,
        "label": item.get("label") or name,
        "help": item.get("help"),
        "required": bool(item.get("required", False)),
        "default": item.get("default"),
        "validation": validation,
        "show_if": item.get("show_if"),
        "required_if": item.get("required_if"),
    }


def _is_nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_iso_date(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _validate_validation(name: str, ftype: str, v: dict) -> None:
    unknown = set(v) - VALIDATION_KEYS_BY_TYPE[ftype]
    if unknown:
        raise ParamsSchemaError(
            f"field '{name}' ({ftype}) has invalid validation keys: {sorted(unknown)} "
            f"(allowed: {sorted(VALIDATION_KEYS_BY_TYPE[ftype])})")

    if ftype == "string":
        for k in ("minLength", "maxLength"):
            if k in v and not _is_nonneg_int(v[k]):
                raise ParamsSchemaError(f"field '{name}' {k} must be a non-negative integer")
        if "minLength" in v and "maxLength" in v and v["minLength"] > v["maxLength"]:
            raise ParamsSchemaError(f"field '{name}' minLength > maxLength")
        if "regex" in v:
            if not isinstance(v["regex"], str):
                raise ParamsSchemaError(f"field '{name}' regex must be a string")
            try:
                re.compile(v["regex"])
            except re.error as e:
                raise ParamsSchemaError(f"field '{name}' regex is invalid: {e}")

    elif ftype in ("number", "integer"):
        for k in ("min", "max"):
            if k in v and not _is_number(v[k]):
                raise ParamsSchemaError(f"field '{name}' {k} must be a number")
        if "min" in v and "max" in v and v["min"] > v["max"]:
            raise ParamsSchemaError(f"field '{name}' min > max")

    elif ftype == "enum":
        if "options" not in v:
            raise ParamsSchemaError(f"field '{name}' (enum) requires validation.options")
        opts = v["options"]
        if not isinstance(opts, list) or not opts:
            raise ParamsSchemaError(f"field '{name}' options must be a non-empty list")
        seen = set()
        for o in opts:
            if not isinstance(o, dict) or "value" not in o or "label" not in o:
                raise ParamsSchemaError(
                    f"field '{name}' each option must be an object with 'value' and 'label'")
            val = o["value"]
            if not isinstance(val, (str, int, float, bool)):
                raise ParamsSchemaError(f"field '{name}' option value must be a scalar: {val!r}")
            if val in seen:
                raise ParamsSchemaError(f"field '{name}' has duplicate option value {val!r}")
            seen.add(val)

    elif ftype == "date":
        for k in ("min", "max"):
            if k in v and not (v[k] == "today" or _is_iso_date(v[k])):
                raise ParamsSchemaError(
                    f"field '{name}' {k} must be an ISO date (YYYY-MM-DD) or 'today'")
        for k in ("min_field", "max_field"):
            if k in v and not isinstance(v[k], str):
                raise ParamsSchemaError(f"field '{name}' {k} must be a field name (string)")


def _validate_default(name: str, ftype: str, default, validation: dict) -> None:
    if ftype == "boolean":
        if not isinstance(default, bool):
            raise ParamsSchemaError(f"field '{name}' default must be a boolean")
    elif ftype == "integer":
        if not isinstance(default, int) or isinstance(default, bool):
            raise ParamsSchemaError(f"field '{name}' default must be an integer")
    elif ftype == "number":
        if not _is_number(default):
            raise ParamsSchemaError(f"field '{name}' default must be a number")
    elif ftype == "string":
        if not isinstance(default, str):
            raise ParamsSchemaError(f"field '{name}' default must be a string")
    elif ftype == "date":
        if not (default == "today" or _is_iso_date(default)):
            raise ParamsSchemaError(f"field '{name}' default must be an ISO date or 'today'")
    elif ftype == "enum":
        values = {o["value"] for o in validation.get("options", [])}
        if default not in values:
            raise ParamsSchemaError(
                f"field '{name}' default {default!r} is not one of its enum options")


# ── predicate validation → referenced field names ───────────────────────────────

def _validate_predicate(pred, name_set: set, current: str, ctx: str) -> set:
    """Validate a predicate tree; return the set of field names it references."""
    if not isinstance(pred, dict):
        raise ParamsSchemaError(f"field '{current}' {ctx} must be a predicate object")

    combinators = set(pred) & PREDICATE_COMBINATORS
    if combinators:
        if len(pred) != 1:
            raise ParamsSchemaError(
                f"field '{current}' {ctx} combinator must be the only key, got {sorted(pred)}")
        comb = combinators.pop()
        body = pred[comb]
        refs: set = set()
        if comb in ("all_of", "any_of"):
            if not isinstance(body, list) or not body:
                raise ParamsSchemaError(f"field '{current}' {ctx} '{comb}' must be a non-empty list")
            for sub in body:
                refs |= _validate_predicate(sub, name_set, current, ctx)
        else:  # not
            refs |= _validate_predicate(body, name_set, current, ctx)
        return refs

    # leaf predicate
    if "field" not in pred:
        raise ParamsSchemaError(f"field '{current}' {ctx} predicate must have a 'field' key")
    ref = pred["field"]
    ops = set(pred) - {"field"}
    if len(ops) != 1 or not (ops <= PREDICATE_OPS):
        raise ParamsSchemaError(
            f"field '{current}' {ctx} predicate needs exactly one operator from "
            f"{sorted(PREDICATE_OPS)}, got {sorted(ops)}")
    op = ops.pop()
    if op in ("in", "not_in") and not isinstance(pred[op], list):
        raise ParamsSchemaError(f"field '{current}' {ctx} '{op}' value must be a list")
    if not isinstance(ref, str) or ref not in name_set:
        raise ParamsSchemaError(
            f"field '{current}' {ctx} references unknown field '{ref}'")
    if ref == current:
        raise ParamsSchemaError(f"field '{current}' {ctx} cannot reference itself")
    return {ref}


# ── dependency graph → topological order (cycle detection) ───────────────────────

def _topo_order(names: list, deps: dict) -> list:
    """Kahn's algorithm. deps[F] = fields F depends on (must be evaluated first).
    Returns a deterministic topological order; raises on a cycle."""
    indeg = {n: len(deps[n]) for n in names}
    dependents: dict[str, list] = {n: [] for n in names}
    for f, ds in deps.items():
        for g in ds:
            dependents[g].append(f)

    queue = sorted(n for n in names if indeg[n] == 0)
    order: list = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for d in sorted(dependents[n]):
            indeg[d] -= 1
            if indeg[d] == 0:
                queue.append(d)

    if len(order) != len(names):
        cyclic = sorted(n for n in names if indeg[n] > 0)
        raise ParamsSchemaError(
            f"cyclic show_if/required_if dependency among fields: {', '.join(cyclic)}")
    return order
