"""services/agent_redeploy.py — code-editing v1, Step 4: save→redeploy orchestration.

create version → validate (PARAMS extraction + requirements vs allowlist) → build image
(injectable build_fn — the Step-1 egress-locked pipeline in prod, a fake in tests) →
forward-only atomic activate.

Invariants (the ones reviewed hardest):
  • FAIL-CLOSED: any failure before activation leaves the prior active version serving —
    the active pointer never moves unless a fully-built, validated image exists.
  • FORWARD-ONLY ACTIVATE: activation only advances to a higher version_no, so a slow
    older build can never clobber a newer already-active version (last-submitted wins)
    WITHOUT holding a lock/transaction across the multi-second build.
  • No lock is held on the dispatch path — in-flight isolation comes from dispatch
    resolving the active version once and stamping agent_runs.version_id (Step 4 wiring).

Call sites are flag-gated (AGENT_VERSIONED_DISPATCH_ENABLED) — inert until flip + the
real mirror stands up (Step 4b).
"""
from __future__ import annotations

import logging

from asyncpg.exceptions import UniqueViolationError

from core.agent_versions import create_version, entrypoint_for_runtime
from core.params_schema import ParamsSchemaError, compile_params
from services.agent_deps import validate_requirements

logger = logging.getLogger("wayforth")


class RedeployError(Exception):
    """A redeploy failed at a named stage. The active version is unchanged."""
    def __init__(self, stage: str, message: str, errors: list | None = None):
        super().__init__(message)
        self.stage = stage            # 'files' | 'params' | 'requirements' | 'build'
        self.message = message
        self.errors = errors or []


def _extract_params_schema(runtime: str, entrypoint_code: str):
    """Static PARAMS extraction (python only; never executes code). Mirrors the upload
    path so a save and a redeploy validate identically."""
    if (runtime or "").startswith("python"):
        return compile_params(entrypoint_code, "python")
    return None


async def _create_building_version(pool, agent_id, files, pins, params_schema, *, tries=4):
    """Insert a building version; retry on the version_no race (two concurrent saves
    both compute MAX+1 → one hits UNIQUE(agent_id, version_no) → recompute + retry)."""
    for attempt in range(tries):
        try:
            async with pool.acquire() as conn:
                return await create_version(
                    conn, agent_id, files, requirements=pins,
                    params_schema=params_schema, status="building")
        except UniqueViolationError:
            if attempt == tries - 1:
                raise
            logger.info("redeploy: version_no race for agent=%s, retrying", agent_id)


async def _mark_version(pool, version_id, status, error=None):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_versions SET status = $2 WHERE id = $1::uuid", str(version_id), status)
    if error:
        logger.warning("redeploy: version %s -> %s: %s", version_id, status, error)


async def redeploy(pool, agent: dict, files: dict, requirements_text: str, *, build_fn) -> dict:
    """Run the full save→redeploy. Returns the version + activation outcome, or raises
    RedeployError (prior active version still serving)."""
    agent_id = str(agent["id"])
    runtime = agent["runtime"]
    entrypoint = entrypoint_for_runtime(runtime)

    # ── validate (no DB writes; fail-closed BEFORE spending a build) ──────────────
    if entrypoint not in files:
        raise RedeployError("files", f"missing entrypoint '{entrypoint}'")
    try:
        params_schema = _extract_params_schema(runtime, files[entrypoint])
    except ParamsSchemaError as e:
        raise RedeployError("params", str(e))
    pins, errors = validate_requirements(requirements_text or "")
    if errors:
        raise RedeployError("requirements", "requirements rejected", errors=errors)
    # a package can be allowlisted yet later REVOKED — block new builds that use one
    if pins:
        from core.package_revocation import revoked_pins
        async with pool.acquire() as conn:
            bad = await revoked_pins(conn, pins)
        if bad:
            raise RedeployError("requirements", "revoked package(s)", errors=[
                {"field": n, "code": "revoked", "message": f"'{n}=={v}' is revoked"}
                for n, v in bad])

    # ── create the building version (immutable attempt record) ───────────────────
    version = await _create_building_version(pool, agent_id, files, pins, params_schema)
    vid, vno = version["id"], version["version_no"]

    # ── build the image (injectable; NO db txn/lock held across the slow build) ──
    try:
        image_ref = await build_fn(agent=agent, version=version, files=files, requirements=pins)
    except Exception as e:                       # build/validation failure inside the pipeline
        await _mark_version(pool, vid, "failed", error=str(e))
        raise RedeployError("build", str(e))

    # ── forward-only atomic activate ─────────────────────────────────────────────
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE agent_versions SET image_ref = $2 WHERE id = $1::uuid", vid, image_ref)
            moved = await conn.fetchval(
                """UPDATE hosted_agents h
                   SET active_version_id = $2::uuid, updated_at = NOW()
                   WHERE h.id = $1::uuid
                     AND EXISTS (SELECT 1 FROM agent_versions av
                                 WHERE av.id = $2::uuid AND av.agent_id = $1::uuid)
                     AND (h.active_version_id IS NULL
                          OR (SELECT version_no FROM agent_versions
                              WHERE id = h.active_version_id) < $3)
                   RETURNING h.active_version_id""",
                agent_id, vid, vno)
            if moved:
                # new version active; demote whatever was active before
                await conn.execute(
                    """UPDATE agent_versions
                       SET status = CASE WHEN id = $2::uuid THEN 'active' ELSE 'superseded' END
                       WHERE agent_id = $1::uuid AND (id = $2::uuid OR status = 'active')""",
                    agent_id, vid)
            else:
                # lost the forward-only race to a newer version — built but never active
                await conn.execute(
                    "UPDATE agent_versions SET status = 'superseded' "
                    "WHERE id = $1::uuid AND status = 'building'", vid)

    return {"version_id": vid, "version_no": vno, "image_ref": image_ref,
            "activated": bool(moved), "status": "active" if moved else "superseded"}
