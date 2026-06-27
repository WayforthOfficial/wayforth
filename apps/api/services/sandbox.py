"""services/sandbox.py — Provider-agnostic sandbox interface for Wayforth Cloud.

Isolation model (approved):
  - E2B uses Firecracker microVMs — VM-level isolation, separate kernel per run
  - Each run is a fresh sandbox; no state persists between runs
  - Network: deny RFC-1918, link-local, and metadata endpoints; allow public internet
    (agents call gateway.wayforth.io via the public URL, not internal network)
  - Secrets decrypted at dispatch, injected as env vars, gone when sandbox exits

Self-hosted migration path (Wayforth One gate item):
  Implement FlyMachinesSandboxProvider (or FirecrackerSandboxProvider) with the
  same SandboxProvider ABC. hosted_agents.sandbox_provider column controls dispatch.
  No changes needed to routers/cloud.py or billing logic.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger("wayforth")

# Network: deny private + link-local + metadata to prevent SSRF against
# internal infrastructure. Public internet (including gateway.wayforth.io)
# is allowed for proxy calls, PyPI, and npm.
_DENY_EGRESS = [
    "10.0.0.0/8",       # RFC-1918
    "172.16.0.0/12",    # RFC-1918
    "192.168.0.0/16",   # RFC-1918
    "169.254.0.0/16",   # link-local / AWS+GCP metadata (169.254.169.254)
    "100.64.0.0/10",    # CGNAT (carrier-grade NAT)
    "127.0.0.0/8",      # loopback
    "::1/128",          # IPv6 loopback
    "fc00::/7",         # IPv6 ULA
]

# ── Step 2: run-sandbox egress lock (gateway-only) ──────────────────────────────
# Flag-gated cutover (default OFF), reversible like the run-token flip. When ON, a
# PYTHON run gets gateway-ONLY egress (deny everything, allow only the gateway — no
# exfiltration) and boots the pre-built BASE IMAGE (httpx/wayforth-sdk closure baked),
# so it runs WITHOUT the run-time `pip install` that gateway-only egress would break.
# Requires AGENT_BASE_IMAGE to be set (the built base snapshot); if unset, falls back
# to the current deny-list path so a run never breaks. node runs are NOT locked in v1
# (they npm-install at run time; a node deps pipeline is a follow-on).
_GATEWAY_HOST = os.environ.get("WAYFORTH_GATEWAY_HOST", "gateway.wayforth.io")


def _gateway_egress_enabled() -> bool:
    return os.environ.get("AGENT_GATEWAY_EGRESS_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _agent_base_image() -> str:
    return os.environ.get("AGENT_BASE_IMAGE", "").strip()


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    sandbox_id: str


class SandboxProvider(ABC):
    """Abstract sandbox provider. Add new providers by subclassing this."""

    @abstractmethod
    async def run(
        self,
        code: str,
        runtime: str,       # 'python3.12' | 'node20'
        env: dict[str, str],
        timeout_seconds: int,
        *,
        files: dict[str, str] | None = None,   # Step 4: multi-file version (path: content)
        image_ref: str | None = None,          # Step 4: per-version built image to boot
    ) -> SandboxResult:
        ...


class E2BSandboxProvider(SandboxProvider):
    """Firecracker-backed E2B sandbox provider.

    E2B API key must be set in E2B_API_KEY env var.
    Config: 1 vCPU + 512 MiB — $0.00001625/sec, $0.000975/min.
    We charge 1 credit/min (ceil), min 1 credit — 27.8% margin at Growth tier.
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("E2B_API_KEY", "")

    async def run(
        self,
        code: str,
        runtime: str,
        env: dict[str, str],
        timeout_seconds: int,
        *,
        files: dict[str, str] | None = None,
        image_ref: str | None = None,
    ) -> SandboxResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._run_sync, code, runtime, env, timeout_seconds, files, image_ref
        )

    def _run_sync(
        self,
        code: str,
        runtime: str,
        env: dict[str, str],
        timeout_seconds: int,
        files: dict[str, str] | None = None,
        image_ref: str | None = None,
    ) -> SandboxResult:
        from e2b import Sandbox, SandboxNetworkOpts

        # Step 4 versioned dispatch: a per-version image (image_ref) carries baked deps,
        # so it boots that image with gateway-ONLY egress and writes the version's files —
        # no run-time pip. Set only when AGENT_VERSIONED_DISPATCH_ENABLED is on AND the
        # version has a built image (callers fall back to the code path otherwise).
        versioned = bool(image_ref)

        # Egress lock applies to PYTHON only, and only when the base image is set
        # (deps must be baked since gateway-only egress makes run-time pip impossible).
        python_locked = (
            _gateway_egress_enabled()
            and runtime == "python3.12"
            and bool(_agent_base_image())
        )
        if _gateway_egress_enabled() and runtime == "python3.12" and not _agent_base_image():
            logger.error("AGENT_GATEWAY_EGRESS_ENABLED on but AGENT_BASE_IMAGE unset; "
                         "falling back to the deny-list path so the run doesn't break")

        create_kwargs = dict(timeout=timeout_seconds, envs=env, api_key=self._api_key or None)
        if versioned:
            create_kwargs["network"] = SandboxNetworkOpts(
                deny_out=["0.0.0.0/0"], allow_out=[_GATEWAY_HOST])
            create_kwargs["template"] = image_ref
        elif python_locked:
            create_kwargs["network"] = SandboxNetworkOpts(
                deny_out=["0.0.0.0/0"], allow_out=[_GATEWAY_HOST])
            create_kwargs["template"] = _agent_base_image()
        else:
            create_kwargs["network"] = SandboxNetworkOpts(deny_out=_DENY_EGRESS)

        t0 = time.monotonic()
        sbx = Sandbox.create(**create_kwargs)
        sandbox_id = sbx.sandbox_id
        try:
            if runtime == "python3.12":
                # multi-file version writes all files; single-file path writes agent.py
                for path, content in (files or {"agent.py": code}).items():
                    sbx.files.write(f"/home/user/{path}", content)
                if versioned or python_locked:
                    # deps are baked into the image — no run-time pip
                    cmd = "python3 /home/user/agent.py"
                else:
                    cmd = (
                        "pip install wayforth-sdk httpx -q --break-system-packages "
                        "2>/dev/null; python3 /home/user/agent.py"
                    )
            else:  # node20
                sbx.files.write("/home/user/agent.ts", code)
                # "type":"module" is required for top-level await in tsx/esbuild
                cmd = (
                    "cd /home/user && npm init -y -q 2>/dev/null"
                    " && node -e \"const p=require('./package.json');p.type='module';"
                    "require('fs').writeFileSync('package.json',JSON.stringify(p));\""
                    " && npm install tsx wayforth-sdk -q 2>/dev/null"
                    " && npx tsx agent.ts"
                )

            result = sbx.commands.run(cmd, timeout=float(timeout_seconds))
            duration_ms = int((time.monotonic() - t0) * 1000)
            return SandboxResult(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.exit_code,
                duration_ms=duration_ms,
                sandbox_id=sandbox_id,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("E2B sandbox error sandbox_id=%s: %s", sandbox_id, exc)
            return SandboxResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_ms=duration_ms,
                sandbox_id=sandbox_id,
            )
        finally:
            try:
                sbx.kill()
            except Exception:
                pass


def compute_credits_for_run(duration_ms: int) -> int:
    """Credits to charge for sandbox compute time.

    Rate: 1.5 credits per ACTUAL minute — the run's fractional duration is used
    directly (NOT rounded up to a whole minute first) — then ceil'd to integer
    credits with a 1-credit minimum. So a sub-~40s run = 1 credit, ~1.5/min beyond
    (e.g. 10 min = 15 credits), never a flat 1 credit/run. At Growth tier
    ($0.001246/credit) this stays above E2B cost ($0.000975/min) at every duration.
    """
    minutes = duration_ms / 60_000
    return max(1, math.ceil(1.5 * minutes))


def get_provider(name: str) -> SandboxProvider:
    """Resolve a sandbox_provider name to its implementation."""
    if name == "e2b":
        return E2BSandboxProvider()
    raise ValueError(f"Unknown sandbox provider: {name!r}")
