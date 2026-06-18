"""AUTHZ-3 regression — /org/invite must not accept an arbitrary role.

Before the fix the invite body's `role` was inserted verbatim, so an admin could
invite a confederate as 'owner' or any arbitrary string. _validated_invite_role
now allow-lists {member, admin} and rejects everything else (esp. 'owner').

Run: uv run pytest tests/test_authz3_invite_role.py -v
"""
import pytest
from fastapi import HTTPException

from routers.org import _validated_invite_role


@pytest.mark.parametrize("raw,expected", [
    ("member", "member"),
    ("admin", "admin"),
    ("ADMIN", "admin"),     # normalised
    ("  member ", "member"),  # trimmed
    ("", "member"),          # default
    (None, "member"),
])
def test_valid_roles_normalised(raw, expected):
    assert _validated_invite_role(raw) == expected


@pytest.mark.parametrize("raw", ["owner", "Owner", "ceo", "superadmin", "admin'; DROP", "root"])
def test_privileged_or_unknown_roles_rejected(raw):
    with pytest.raises(HTTPException) as exc:
        _validated_invite_role(raw)
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "invalid_role"
