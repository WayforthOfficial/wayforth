"""scripts/deps_live_proof.py — §6 ship-gate for the dependency pipeline (LIVE).

Runs the real services.agent_deps pipeline against real E2B sandboxes + real pip.
These are must-pass-BEFORE-user-visible (same standard as the run-token rotation
proof) — they exercise behavior unit tests can't fake (pip's hash enforcement, a
booted snapshot, the two egress allowlists).

Gated — does nothing unless DEPS_LIVE_PROOF=1 and E2B_API_KEY is set:

    DEPS_LIVE_PROOF=1 E2B_API_KEY=... [DEPS_MIRROR_URL=...] \
        /app/.venv/bin/python -m scripts.deps_live_proof

Without a private mirror yet, PyPI stands in as the index (DEPS_MIRROR_URL /
allowed hosts default to PyPI). In prod these point at the private mirror.

Proves:
  #1 hash-mismatch → pip rejects the install (a swapped artifact is caught).
  #3 base deps build → snapshot → boot a run sandbox (gateway-only egress) with
     httpx + wayforth-sdk importable and PyPI unreachable (unblocks Step 2).
  §0 two-allowlist separation: build can't reach the gateway; run can't reach the mirror.
TODO(canary): a wheel that POSTs to an external host → blocked at run egress.
"""
from __future__ import annotations

import os
import sys

from services.agent_deps import (
    BASE_DEPS, _norm, build_requirements_lock, load_lockfile, pip_install_command,
)

MIRROR_URL = os.environ.get("DEPS_MIRROR_URL", "https://pypi.org/simple")
MIRROR_HOSTS = os.environ.get("DEPS_MIRROR_HOSTS", "pypi.org,files.pythonhosted.org").split(",")
GATEWAY_HOST = os.environ.get("WAYFORTH_GATEWAY_HOST", "gateway.wayforth.io")


def _net(allow):
    from e2b import SandboxNetworkOpts
    return SandboxNetworkOpts(deny_out=["0.0.0.0/0"], allow_out=list(allow))


def _run(sbx, cmd, timeout=150):
    return sbx.commands.run(cmd + " ; echo EXIT=$?", timeout=timeout)


def main() -> int:
    if os.environ.get("DEPS_LIVE_PROOF") != "1" or not os.environ.get("E2B_API_KEY"):
        print("deps_live_proof: skipped (set DEPS_LIVE_PROOF=1 and E2B_API_KEY).")
        return 0

    from e2b import Sandbox

    lock = load_lockfile()
    base = [(_norm(n), v, lock[_norm(n)][v]) for n, v in BASE_DEPS]
    good_lock = build_requirements_lock(base)
    bad_lock = build_requirements_lock([("httpx", "0.28.1", ["sha256:" + "0" * 64])])
    pip = pip_install_command(MIRROR_URL)
    failures = []

    # #1 — hash mismatch must be rejected
    b = Sandbox.create(timeout=180, network=_net(MIRROR_HOSTS))
    try:
        # §0: build sandbox cannot reach the gateway
        g = _run(b, f'curl -sS -o /dev/null -w "%{{http_code}}" --max-time 8 https://{GATEWAY_HOST}/status')
        if "ec=35" not in g.stdout and "000" not in g.stdout:
            failures.append(f"#0 build reached gateway: {g.stdout!r}")
        b.files.write("/home/user/requirements.lock", bad_lock)
        r = _run(b, pip)
        if "do not match" not in (r.stdout + r.stderr).lower():
            failures.append(f"#1 hash mismatch NOT rejected: {(r.stdout + r.stderr)[-300:]!r}")
        else:
            print("#1 PASS — pip rejected the tampered hash")
    finally:
        b.kill()

    # #3 — base build → snapshot → boot run sandbox, importable, PyPI blocked
    b2 = Sandbox.create(timeout=300, network=_net(MIRROR_HOSTS))
    sid = None
    try:
        b2.files.write("/home/user/requirements.lock", good_lock)
        r2 = _run(b2, pip, timeout=240)
        if "EXIT=0" not in r2.stdout:
            failures.append(f"#3 base install failed: {(r2.stderr or '')[-300:]!r}")
        snap = b2.create_snapshot(name="wf-deps-shipgate")
        sid = getattr(snap, "snapshot_id", None) or getattr(snap, "template_id", None)
    finally:
        b2.kill()

    if sid:
        rn = Sandbox.create(sid, timeout=120, network=_net([GATEWAY_HOST]))
        try:
            imp = _run(rn, 'python3 -c "import httpx;print(httpx.__version__)" && pip show wayforth-sdk | grep -i ^version')
            if "EXIT=0" not in imp.stdout:
                failures.append(f"#3 base deps not importable in run sandbox: {imp.stdout!r}")
            else:
                print(f"#3 PASS — base deps importable in run sandbox: {imp.stdout.splitlines()[:2]}")
            # §0: run sandbox cannot reach the mirror/PyPI
            pp = _run(rn, f'curl -sS -o /dev/null -w "%{{http_code}}" --max-time 8 {MIRROR_URL}')
            if "ec=35" not in pp.stdout and "000" not in pp.stdout:
                failures.append(f"#0 run reached mirror: {pp.stdout!r}")
            else:
                print("#0 PASS — run sandbox blocked from the mirror; build blocked from gateway")
        finally:
            rn.kill()
            try:
                Sandbox.delete_snapshot(sid)
            except Exception:
                pass

    if failures:
        print("DEPS LIVE PROOF: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("DEPS LIVE PROOF: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
