"""core/package_revocation.py — code-editing v1, Step 5: package revocation flagging.

When an allowlisted package is later found malicious/vulnerable, revoke_package records
it and FLAGS every agent_version whose baked requirements include it (so affected agents
can be rebuilt). is_revoked / revoked_pins let the redeploy orchestrator block NEW builds
from using a revoked package.

agent_versions.requirements is a JSON array of [name, version, [hashes]] (the validated
pins), so an element's ->>0 is the package name and ->>1 its version.
"""
from __future__ import annotations


async def revoke_package(conn, name: str, *, version: str | None = None,
                         reason: str | None = None) -> int:
    """Record the revocation and flag affected versions. Returns the count flagged.
    version=None revokes ALL versions of the package."""
    await conn.execute(
        """INSERT INTO revoked_packages (name, version, reason) VALUES ($1, $2, $3)
           ON CONFLICT (name, version) DO UPDATE SET reason = EXCLUDED.reason, revoked_at = NOW()""",
        name, version, reason)
    rows = await conn.fetch(
        """UPDATE agent_versions SET dep_flagged = TRUE
           WHERE requirements IS NOT NULL
             AND EXISTS (SELECT 1 FROM jsonb_array_elements(requirements) e
                         WHERE e->>0 = $1 AND ($2::text IS NULL OR e->>1 = $2))
           RETURNING id""", name, version)
    return len(rows)


async def is_revoked(conn, name: str, version: str | None = None) -> bool:
    """True if (name, version) is revoked — either a blanket revocation (version NULL)
    or one matching this exact version."""
    return bool(await conn.fetchval(
        """SELECT 1 FROM revoked_packages
           WHERE name = $1 AND (version IS NULL OR version = $2) LIMIT 1""",
        name, version))


async def revoked_pins(conn, pins) -> list:
    """Subset of pins (name, version, …) that are revoked — for redeploy rejection."""
    bad = []
    for p in pins:
        if await is_revoked(conn, p[0], p[1]):
            bad.append((p[0], p[1]))
    return bad


async def flagged_versions(conn) -> list:
    """All currently dep_flagged versions (agent_id, version_id, version_no) — for ops
    to find + rebuild affected agents."""
    rows = await conn.fetch(
        """SELECT agent_id, id AS version_id, version_no FROM agent_versions
           WHERE dep_flagged ORDER BY agent_id, version_no""")
    return [{"agent_id": str(r["agent_id"]), "version_id": str(r["version_id"]),
             "version_no": r["version_no"]} for r in rows]
