"""scripts/build_base_image.py — reproducibly bake the agent base image from BASE_DEPS.

The base image (AGENT_BASE_IMAGE) carries the full resolved dependency closure so that,
under gateway-only egress, agent runs need no run-time pip. This script bakes it FROM
services.agent_deps.BASE_DEPS — the same source the ship-gate verifies against — so the
wired image can't drift from the recipe (the ⚠ gap the flip-readiness check found: the
old image was baked by an uncommitted manual step).

Gated — does nothing unless BUILD_BASE_IMAGE=1 and E2B_API_KEY is set:

    BUILD_BASE_IMAGE=1 E2B_API_KEY=... [DEPS_MIRROR_URL=...] \
        /app/.venv/bin/python -m scripts.build_base_image

It builds in a mirror-only-egress sandbox, installs the hashed wheels-only closure,
ASSERTS the built `pip freeze` equals BASE_DEPS before snapshotting, then prints the ref.
It does NOT set AGENT_BASE_IMAGE and does NOT flip anything — wiring is a separate step.
"""
from __future__ import annotations

import os
import sys

from services.agent_deps import (
    base_deps_lock, base_deps_match, diff_against_base_deps, pip_install_command,
)

MIRROR_URL = os.environ.get("DEPS_MIRROR_URL", "https://pypi.org/simple")
MIRROR_HOSTS = os.environ.get("DEPS_MIRROR_HOSTS", "pypi.org,files.pythonhosted.org").split(",")
SNAPSHOT_NAME = os.environ.get("BASE_IMAGE_NAME", "wayforth-agent-base-v1")


def _net(allow):
    from e2b import SandboxNetworkOpts
    return SandboxNetworkOpts(deny_out=["0.0.0.0/0"], allow_out=list(allow))


def main() -> int:
    if os.environ.get("BUILD_BASE_IMAGE") != "1" or not os.environ.get("E2B_API_KEY"):
        print("build_base_image: skipped (set BUILD_BASE_IMAGE=1 and E2B_API_KEY).")
        return 0

    from e2b import Sandbox

    b = Sandbox.create(timeout=300, network=_net(MIRROR_HOSTS))
    try:
        b.files.write("/home/user/requirements.lock", base_deps_lock())
        r = b.commands.run(pip_install_command(MIRROR_URL) + " ; echo EXIT=$?", timeout=240)
        if "EXIT=0" not in (r.stdout or ""):
            print("FAIL: base install failed\n", (r.stderr or "")[-500:])
            return 1
        # the recipe guarantee: the built closure must EXACTLY equal BASE_DEPS before we bake
        fr = b.commands.run("python3 -m pip list --format=freeze")
        if not base_deps_match(fr.stdout or ""):
            print("FAIL: built closure != BASE_DEPS — not snapshotting.")
            print("  diff:", diff_against_base_deps(fr.stdout or ""))
            return 1
        snap = b.create_snapshot(name=SNAPSHOT_NAME)
        ref = getattr(snap, "snapshot_id", None) or getattr(snap, "template_id", None) or SNAPSHOT_NAME
        print("BASE IMAGE BUILT — closure matches BASE_DEPS exactly.")
        print(f"  name: {SNAPSHOT_NAME}")
        print(f"  ref:  {ref}")
        print("  next (manual, separate steps): set AGENT_BASE_IMAGE to this ref →")
        print("        run deps_live_proof (asserts wired image == BASE_DEPS) → flip egress flag.")
        return 0
    finally:
        b.kill()


if __name__ == "__main__":
    sys.exit(main())
