"""AUTHZ-2 regression — the last active CEO can't be demoted/deactivated.

/admin-api/team/{id} PATCH previously wrote role verbatim with no allow-list and
no protection against removing the last CEO (orphaning the admin plane). The
last-CEO guard is captured by _would_orphan_ceo(); role validation uses
ADMIN_ROLES.

Run: uv run pytest tests/test_authz2_last_ceo.py -v
"""
from routers.admin.dashboard import ADMIN_ROLES, _would_orphan_ceo


def test_demoting_last_ceo_blocked():
    assert _would_orphan_ceo("ceo", new_role="support", new_is_active=None, active_ceo_count=1) is True


def test_deactivating_last_ceo_blocked():
    assert _would_orphan_ceo("ceo", new_role=None, new_is_active=False, active_ceo_count=1) is True


def test_demoting_ceo_when_others_remain_allowed():
    assert _would_orphan_ceo("ceo", new_role="support", new_is_active=None, active_ceo_count=2) is False


def test_non_ceo_target_unaffected():
    assert _would_orphan_ceo("support", new_role="analytics", new_is_active=False, active_ceo_count=1) is False


def test_promoting_to_ceo_or_keeping_ceo_allowed():
    # role stays ceo / activating → never an orphan, even if count is 1.
    assert _would_orphan_ceo("ceo", new_role="ceo", new_is_active=None, active_ceo_count=1) is False
    assert _would_orphan_ceo("ceo", new_role=None, new_is_active=True, active_ceo_count=1) is False


def test_role_allowlist_excludes_arbitrary_strings():
    assert "ceo" in ADMIN_ROLES and "support" in ADMIN_ROLES
    assert "superadmin" not in ADMIN_ROLES and "owner" not in ADMIN_ROLES
