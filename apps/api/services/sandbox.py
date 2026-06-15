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
    ) -> SandboxResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._run_sync, code, runtime, env, timeout_seconds
        )

    def _run_sync(
        self,
        code: str,
        runtime: str,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> SandboxResult:
        from e2b import Sandbox, SandboxNetworkOpts

        network = SandboxNetworkOpts(deny_out=_DENY_EGRESS)
        t0 = time.monotonic()
        sbx = Sandbox.create(
            timeout=timeout_seconds,
            envs=env,
            network=network,
            api_key=self._api_key or None,
        )
        sandbox_id = sbx.sandbox_id
        try:
            if runtime == "python3.12":
                sbx.files.write("/home/user/agent.py", code)
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

    Rate: 1 credit/minute, ceil to nearest minute, minimum 1 credit.
    At Growth tier ($0.001246/credit): provably above E2B cost ($0.000975/min)
    at every duration. Math verified before implementation.
    """
    minutes = math.ceil(duration_ms / 60_000)
    return max(1, minutes)


def get_provider(name: str) -> SandboxProvider:
    """Resolve a sandbox_provider name to its implementation."""
    if name == "e2b":
        return E2BSandboxProvider()
    raise ValueError(f"Unknown sandbox provider: {name!r}")
