"""test_package_revocation.py — code-editing v1, Step 5 (revocation flagging).

Unit-level logic (revoked_pins / is_revoked semantics) with a fake conn; the SQL-level
flagging (revoke_package flags affected versions) is proven against real Postgres in
scripts/agent_redeploy_proof.py.
"""
from __future__ import annotations

import pytest

from core import package_revocation as pr


class RevConn:
    """is_revoked runs `SELECT 1 … WHERE name=$1 AND (version IS NULL OR version=$2)`.
    We model the revoked set as {(name, version_or_None)}."""
    def __init__(self, revoked):
        self.revoked = revoked

    async def fetchval(self, q, *a):
        name, version = a[0], a[1]
        for rn, rv in self.revoked:
            if rn == name and (rv is None or rv == version):
                return 1
        return None


@pytest.mark.asyncio
async def test_is_revoked_exact_version():
    c = RevConn({("evil", "1.0")})
    assert await pr.is_revoked(c, "evil", "1.0") is True
    assert await pr.is_revoked(c, "evil", "2.0") is False   # only 1.0 revoked
    assert await pr.is_revoked(c, "safe", "1.0") is False


@pytest.mark.asyncio
async def test_is_revoked_all_versions():
    c = RevConn({("evil", None)})                            # blanket revocation
    assert await pr.is_revoked(c, "evil", "1.0") is True
    assert await pr.is_revoked(c, "evil", "9.9") is True


@pytest.mark.asyncio
async def test_revoked_pins_filters_only_revoked():
    c = RevConn({("evil", "1.0")})
    pins = [("httpx", "0.28.1", ["sha256:x"]), ("evil", "1.0", ["sha256:y"]),
            ("evil", "2.0", ["sha256:z"])]
    bad = await pr.revoked_pins(c, pins)
    assert bad == [("evil", "1.0")]                          # 2.0 not revoked, httpx fine
