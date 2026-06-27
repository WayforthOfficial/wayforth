"""core/agent_versions.py — code-editing v1, Step 3: agent version data layer.

Read/write helpers over the agent_versions table + hosted_agents.active_version_id.
PURE DATA ACCESS — no build, no dispatch. Dispatch still reads hosted_agents.code and
is unaffected; Step 4 wires the save→redeploy flow to create + build + activate versions.
"""
from __future__ import annotations

import json

PYTHON_ENTRYPOINT = "agent.py"
NODE_ENTRYPOINT = "agent.ts"


def entrypoint_for_runtime(runtime: str) -> str:
    """The entrypoint filename for a runtime — the file dispatch runs + PARAMS extracts."""
    return NODE_ENTRYPOINT if (runtime or "").startswith("node") else PYTHON_ENTRYPOINT


def _parse(v):
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("files", "requirements", "params_schema"):
        if k in d:
            d[k] = _parse(d[k])
    for k in ("id", "agent_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    if d.get("created_at") is not None and hasattr(d["created_at"], "isoformat"):
        d["created_at"] = d["created_at"].isoformat()
    return d


async def next_version_no(db, agent_id) -> int:
    n = await db.fetchval(
        "SELECT COALESCE(MAX(version_no), 0) + 1 FROM agent_versions WHERE agent_id = $1::uuid",
        str(agent_id))
    return int(n or 1)


async def create_version(db, agent_id, files: dict, *, requirements=None, params_schema=None,
                         image_ref: str | None = None, status: str = "building") -> dict:
    """Insert a new immutable version (next version_no). Returns the parsed row."""
    version_no = await next_version_no(db, agent_id)
    row = await db.fetchrow(
        """INSERT INTO agent_versions
             (agent_id, version_no, files, requirements, params_schema, image_ref, status)
           VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
           RETURNING *""",
        str(agent_id), version_no, json.dumps(files),
        json.dumps(requirements) if requirements is not None else None,
        json.dumps(params_schema) if params_schema is not None else None,
        image_ref, status)
    return _row(row)


async def get_version(db, version_id) -> dict | None:
    return _row(await db.fetchrow(
        "SELECT * FROM agent_versions WHERE id = $1::uuid", str(version_id)))


async def get_active_version(db, agent_id) -> dict | None:
    """The agent's currently-active version (via hosted_agents.active_version_id)."""
    return _row(await db.fetchrow(
        """SELECT av.* FROM agent_versions av
           JOIN hosted_agents h ON h.active_version_id = av.id
           WHERE h.id = $1::uuid""", str(agent_id)))


async def list_versions(db, agent_id) -> list:
    """Version history (newest first), without the file bodies."""
    rows = await db.fetch(
        """SELECT id, version_no, status, image_ref, created_at
           FROM agent_versions WHERE agent_id = $1::uuid
           ORDER BY version_no DESC""", str(agent_id))
    return [_row(r) for r in rows]


async def activate_version(db, agent_id, version_id) -> None:
    """Repoint active_version_id (post-build activation or rollback). Verifies the
    version belongs to the agent, so one agent can't be pointed at another's version."""
    updated = await db.fetchval(
        """UPDATE hosted_agents h SET active_version_id = $2::uuid, updated_at = NOW()
           WHERE h.id = $1::uuid
             AND EXISTS (SELECT 1 FROM agent_versions av
                         WHERE av.id = $2::uuid AND av.agent_id = $1::uuid)
           RETURNING h.id""", str(agent_id), str(version_id))
    if not updated:
        raise ValueError(f"version {version_id} does not belong to agent {agent_id}")
