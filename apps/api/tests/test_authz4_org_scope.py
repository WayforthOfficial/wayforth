"""AUTHZ-4 regression — org admin checks must be scoped to the target org.

Before the fix, _require_admin matched ANY membership row for the user, so a user
who was admin in org X but only a member in the org being operated on still
passed. Now the check is bound to (user_id, org_id).

Run: uv run pytest tests/test_authz4_org_scope.py -v
"""
import pytest
from fastapi import HTTPException

from routers.org import _require_admin

ADMIN_ORG = "org-A"   # user is admin here
MEMBER_ORG = "org-B"  # user is only a member here
USER = "11111111-1111-1111-1111-111111111111"


class _FakeDB:
    """Returns an admin row only for ADMIN_ORG — mirrors the scoped SQL filter."""
    async def fetchrow(self, _query, user_id, org_id):
        if org_id == ADMIN_ORG:
            return {"role": "admin"}
        if org_id == MEMBER_ORG:
            return {"role": "member"}
        return None


async def test_admin_in_target_org_passes():
    await _require_admin(_FakeDB(), USER, ADMIN_ORG)  # no raise


async def test_admin_elsewhere_cannot_act_on_member_org():
    # The crux of AUTHZ-4: admin in ADMIN_ORG must NOT grant authority over MEMBER_ORG.
    with pytest.raises(HTTPException) as exc:
        await _require_admin(_FakeDB(), USER, MEMBER_ORG)
    assert exc.value.status_code == 403


async def test_non_member_org_rejected():
    with pytest.raises(HTTPException) as exc:
        await _require_admin(_FakeDB(), USER, "org-unknown")
    assert exc.value.status_code == 403
