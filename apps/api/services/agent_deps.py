"""services/agent_deps.py — agent code-editing v1, Step 1: dependency pipeline.

THE install-time-RCE control. A user's requirements are validated against a curated,
pinned, hashed ALLOWLIST (agent_deps_lock.json — the only packages the private mirror
serves), then installed WHEELS-ONLY (no setup.py execution) in an isolated build
sandbox whose egress is restricted to the mirror only, then snapshotted into a
per-version image that run sandboxes boot from. Nothing here trusts user input.

Defense in depth (each layer independently reduces blast radius):
  • allowlist + private mirror           → only vetted packages can install at all
  • pinned (==) + --require-hashes        → exact, reproducible artifacts
  • --only-binary=:all:                   → no setup.py / build hooks run at install
  • --no-deps + locked closure            → no surprise transitive pulls
  • --index-url=<mirror>                  → resolver can't reach public PyPI
  • build-sandbox egress = mirror-only    → real network isolation at build (proven)
  • (Step 2) run-sandbox egress = gateway → no exfiltration at run

This module is pure logic + an injectable build orchestration; importing it is inert.
"""
from __future__ import annotations

import json
import os
import re
import shlex

logger_name = "wayforth"

# ── caps (anti-DoS / anti-bloat) ────────────────────────────────────────────────
MAX_DIRECT_DEPS = 20
BUILD_TIMEOUT_S = 180
MAX_IMAGE_DELTA_MB = 500

# Base closure baked into EVERY agent image. Gateway-only run egress (Step 2) makes
# run-time pip impossible, so the SDK + http client (and httpx's pinned closure) live
# in the image. All present in the lockfile.
# The FULL resolved closure (pip download wayforth-sdk httpx, with deps) — not a
# hand-list. The survival proof (a real httpx request, not just `import httpx`) caught
# that hand-listing missed httpcore + typing-extensions; httpx imports httpcore lazily
# only on the first request, so an import check alone passed while a real run failed.
BASE_DEPS = [
    ("anyio", "4.14.1"),
    ("certifi", "2026.6.17"),
    ("h11", "0.16.0"),
    ("httpcore", "1.0.9"),
    ("httpx", "0.28.1"),
    ("idna", "3.18"),
    ("typing-extensions", "4.15.0"),
    ("wayforth-sdk", "0.9.0"),
]

_REQS_PATH = "/home/user/requirements.lock"
_LOCK_PATH = os.path.join(os.path.dirname(__file__), "agent_deps_lock.json")
_LOCK_CACHE: dict | None = None

# A requirement must be EXACTLY name==version — nothing else (no ranges, markers,
# extras, urls, options, editable installs).
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


class DepsError(Exception):
    """Validation/build failure. `.errors` is a list of {field, code, message}."""

    def __init__(self, errors: list):
        self.errors = errors
        super().__init__("; ".join(e.get("message", "") for e in errors) or "deps error")


def _norm(name: str) -> str:
    """PEP 503 normalization for package names."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def load_lockfile() -> dict:
    """The curated allowlist: {normalized_name: {version: [sha256 hashes]}}."""
    global _LOCK_CACHE
    if _LOCK_CACHE is None:
        with open(_LOCK_PATH) as f:
            raw = json.load(f)
        _LOCK_CACHE = {_norm(k): v for k, v in raw.items()}
    return _LOCK_CACHE


# ── the gatekeeper ──────────────────────────────────────────────────────────────

def validate_requirements(text: str, lockfile: dict | None = None):
    """Validate a requirements.txt body against the allowlist. Returns (pins, errors).

    Each pin is (name, version, [hashes]). A non-empty `errors` list ⇒ reject (422).
    Fail-closed: anything not exactly `name==version` from the allowlist is rejected.
    """
    lock = lockfile if lockfile is not None else load_lockfile()
    pins: list = []
    errors: list = []
    seen: set = set()

    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]

    if len(lines) > MAX_DIRECT_DEPS:
        errors.append({"field": None, "code": "too_many",
                       "message": f"too many dependencies ({len(lines)} > {MAX_DIRECT_DEPS})"})

    for ln in lines:
        if ln.startswith("-") or "://" in ln or " @ " in ln:
            errors.append({"field": ln, "code": "unsupported",
                           "message": f"unsupported requirement line: {ln!r} "
                                      "(only 'name==version' is allowed)"})
            continue
        m = _PIN_RE.match(ln)
        if not m:
            errors.append({"field": ln, "code": "unpinned",
                           "message": f"requirement must be pinned 'name==version': {ln!r}"})
            continue
        name, version = _norm(m.group(1)), m.group(2)
        if name in seen:
            errors.append({"field": name, "code": "duplicate",
                           "message": f"duplicate requirement '{name}'"})
            continue
        seen.add(name)
        if name not in lock:
            errors.append({"field": name, "code": "not_allowed",
                           "message": f"'{name}' is not in the allowlist (request it for review)"})
            continue
        if version not in lock[name]:
            errors.append({"field": name, "code": "version_not_allowed",
                           "message": f"'{name}=={version}' is not an allowed version "
                                      f"(allowed: {sorted(lock[name])})"})
            continue
        pins.append((name, version, list(lock[name][version])))

    return pins, errors


def resolve_install_set(requirements_text: str, lockfile: dict | None = None):
    """Base closure + validated user pins, deduped. Returns (install_set, errors).

    A user pin that names a base dep at a different version is a conflict (base wins).
    """
    lock = lockfile if lockfile is not None else load_lockfile()
    user_pins, errors = validate_requirements(requirements_text, lock)

    base = {}
    for name, version in BASE_DEPS:
        n = _norm(name)
        base[n] = (n, version, list(lock[n][version]))

    out = dict(base)
    for name, version, hashes in user_pins:
        if name in base and base[name][1] != version:
            errors.append({"field": name, "code": "base_dep_conflict",
                           "message": f"'{name}' is a base dependency pinned to "
                                      f"{base[name][1]}; cannot override with {version}"})
            continue
        out[name] = (name, version, hashes)

    if errors:
        return [], errors
    return list(out.values()), []


# ── install command + egress (two-allowlist) ────────────────────────────────────

def build_requirements_lock(install_set) -> str:
    """`name==version --hash=sha256:… ` lines for --require-hashes."""
    lines = []
    for name, version, hashes in install_set:
        hs = " ".join(f"--hash={h}" for h in hashes)
        lines.append(f"{name}=={version} {hs}")
    return "\n".join(lines) + "\n"


def pip_install_command(mirror_url: str, reqs_path: str = _REQS_PATH) -> str:
    """The wheels-only, hashed, mirror-pinned, no-deps install — the install-RCE control."""
    return (
        "pip install --only-binary=:all: --require-hashes --no-deps --no-input -q "
        f"--index-url {shlex.quote(mirror_url)} -r {shlex.quote(reqs_path)} "
        "--break-system-packages"
    )


def build_egress(mirror_host: str) -> dict:
    """BUILD sandbox egress: deny all, allow ONLY the mirror (never the gateway)."""
    return {"deny_out": ["0.0.0.0/0"], "allow_out": [mirror_host]}


def run_egress(gateway_host: str = "gateway.wayforth.io") -> dict:
    """RUN sandbox egress: deny all, allow ONLY the gateway (never the mirror)."""
    return {"deny_out": ["0.0.0.0/0"], "allow_out": [gateway_host]}


# ── build orchestration (injectable sandbox factory for tests) ──────────────────

def _safe_rel_path(path: str) -> str:
    p = (path or "").strip().lstrip("/")
    if not p or ".." in p.split("/") or not _SAFE_PATH_RE.match(p):
        raise DepsError([{"field": path, "code": "bad_path",
                          "message": f"unsafe file path: {path!r}"}])
    return p


def _default_sandbox_factory(egress: dict, timeout_s: int):
    from e2b import Sandbox, SandboxNetworkOpts
    return Sandbox.create(
        timeout=timeout_s,
        network=SandboxNetworkOpts(deny_out=egress["deny_out"], allow_out=egress["allow_out"]),
    )


def build_agent_image(
    agent_id: str,
    version: int,
    files: dict,
    requirements_text: str,
    *,
    mirror_url: str,
    mirror_host: str,
    sandbox_factory=_default_sandbox_factory,
) -> str:
    """Validate → isolated mirror-only build → wheels-only hashed install → snapshot.

    Returns the per-version image ref (snapshot id). Raises DepsError on validation or
    install failure. The build sandbox holds NO Wayforth secrets and can reach only the
    mirror; it is always killed.
    """
    install_set, errors = resolve_install_set(requirements_text)
    if errors:
        raise DepsError(errors)

    reqs_lock = build_requirements_lock(install_set)
    cmd = pip_install_command(mirror_url)

    sbx = sandbox_factory(build_egress(mirror_host), BUILD_TIMEOUT_S)
    try:
        for path, content in (files or {}).items():
            sbx.files.write(f"/home/user/{_safe_rel_path(path)}", content)
        sbx.files.write(_REQS_PATH, reqs_lock)

        res = sbx.commands.run(cmd, timeout=float(BUILD_TIMEOUT_S))
        if getattr(res, "exit_code", 1) != 0:
            raise DepsError([{"field": None, "code": "install_failed",
                              "message": (getattr(res, "stderr", "") or "")[:500]}])

        # Image-size ceiling (approximate; site-packages growth).
        du = sbx.commands.run(
            "du -sm /usr/lib/python3*/site-packages /home/user 2>/dev/null "
            "| awk '{s+=$1} END{print s+0}'")
        if int((getattr(du, "stdout", "0") or "0").strip() or 0) > MAX_IMAGE_DELTA_MB:
            raise DepsError([{"field": None, "code": "image_too_large",
                              "message": f"image exceeds {MAX_IMAGE_DELTA_MB} MB cap"}])

        snap = sbx.create_snapshot(name=f"agent-{agent_id}-v{version}")
        image_ref = getattr(snap, "snapshot_id", None) or getattr(snap, "template_id", None)
        if not image_ref:
            raise DepsError([{"field": None, "code": "snapshot_failed",
                              "message": "no image ref returned"}])
        return image_ref
    finally:
        try:
            sbx.kill()
        except Exception:
            pass
