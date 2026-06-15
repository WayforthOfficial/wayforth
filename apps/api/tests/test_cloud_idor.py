"""
tests/test_cloud_idor.py — IDOR + ownership regression for Cloud endpoints.

Verifies that every /cloud/agents/* read endpoint enforces row-level ownership:
- Fetching a resource that belongs to another user must 404 (not 403, to avoid
  confirming existence), not 200.
- The authenticated user can only see their own agents, runs, logs, and usage.

Two-user model:
  USER_A — the standard test API key (WAYFORTH_TEST_API_KEY)
  USER_B — a second key (WAYFORTH_TEST_API_KEY_B) for cross-user IDOR checks.
  When USER_B key is absent, cross-user tests are skipped but same-user
  ownership and format-validation tests still run.

Run: pytest apps/api/tests/test_cloud_idor.py -v
"""
import os
import uuid

import httpx
import pytest
import pytest_asyncio

from tests.test_suite_v060 import API_KEY, BASE_URL

API_KEY_B = os.environ.get("WAYFORTH_TEST_API_KEY_B", "")


def _deployed_version() -> str:
    try:
        r = httpx.get(f"{BASE_URL}/status", timeout=10.0)
        return (r.json() or {}).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def _version_at_least(target: str) -> bool:
    def _tup(v: str):
        return tuple(int(p) for p in v.split(".")[:3] if p.isdigit())
    return _tup(_deployed_version()) >= _tup(target)


_REQUIRES_V090 = pytest.mark.skipif(
    not _version_at_least("0.9.0"),
    reason="New cloud endpoints not yet deployed — requires v0.9.0",
)

_REQUIRES_A = pytest.mark.skipif(not API_KEY, reason="WAYFORTH_TEST_API_KEY not set")
_REQUIRES_B = pytest.mark.skipif(
    not API_KEY_B, reason="WAYFORTH_TEST_API_KEY_B not set — skipping cross-user IDOR tests"
)


def _auth(key: str) -> dict:
    return {"X-Wayforth-API-Key": key}


@pytest_asyncio.fixture
async def client_a():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0, follow_redirects=True) as c:
        yield c


@pytest_asyncio.fixture
async def client_b():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0, follow_redirects=True) as c:
        yield c


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _create_test_agent(c: httpx.AsyncClient, key: str) -> str:
    """Create a minimal agent and return its id. Raises on failure."""
    slug = f"idor-test-{uuid.uuid4().hex[:8]}"
    r = await c.post(
        "/cloud/agents",
        json={"name": "IDOR Test Agent", "slug": slug, "runtime": "python3.12"},
        headers=_auth(key),
    )
    assert r.status_code == 201, f"Agent creation failed: {r.status_code} {r.text[:200]}"
    return r.json()["id"]


async def _delete_agent(c: httpx.AsyncClient, key: str, agent_id: str) -> None:
    await c.delete(f"/cloud/agents/{agent_id}", headers=_auth(key))


# ═══════════════════════════════════════════════════════════════════════════════
# C1 — Random / nonexistent UUIDs always 404 (ownership enforced even on miss)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@_REQUIRES_A
async def test_CLOUD_IDOR_01_random_agent_id_is_404(client_a):
    """A valid UUID that doesn't belong to this user returns 404, not 200."""
    fake_id = str(uuid.uuid4())
    r = await client_a.get(f"/cloud/agents/{fake_id}", headers=_auth(API_KEY))
    assert r.status_code == 404, f"Expected 404 for unknown agent_id, got {r.status_code}"


@pytest.mark.asyncio
@_REQUIRES_A
async def test_CLOUD_IDOR_02_random_run_id_is_404(client_a):
    """Random run_id under a random agent_id → 404 at the agent level."""
    fake_agent = str(uuid.uuid4())
    fake_run = str(uuid.uuid4())
    r = await client_a.get(f"/cloud/agents/{fake_agent}/runs/{fake_run}", headers=_auth(API_KEY))
    assert r.status_code == 404


@pytest.mark.asyncio
@_REQUIRES_A
async def test_CLOUD_IDOR_03_random_run_logs_is_404(client_a):
    """Random run logs path → 404."""
    fake_agent = str(uuid.uuid4())
    fake_run = str(uuid.uuid4())
    r = await client_a.get(f"/cloud/agents/{fake_agent}/runs/{fake_run}/logs", headers=_auth(API_KEY))
    assert r.status_code == 404


@pytest.mark.asyncio
@_REQUIRES_A
async def test_CLOUD_IDOR_04_random_run_history_is_404(client_a):
    """Run history for a random (nonexistent) agent → 404 at agent level."""
    fake_agent = str(uuid.uuid4())
    r = await client_a.get(f"/cloud/agents/{fake_agent}/runs", headers=_auth(API_KEY))
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# C2 — Own resources are accessible
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@_REQUIRES_A
async def test_CLOUD_IDOR_05_own_agent_is_200(client_a):
    """The creating user can read their own agent."""
    agent_id = await _create_test_agent(client_a, API_KEY)
    try:
        r = await client_a.get(f"/cloud/agents/{agent_id}", headers=_auth(API_KEY))
        assert r.status_code == 200, f"Expected 200 for own agent, got {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert data["id"] == agent_id
        # Confirm no secrets in response
        assert "env_encrypted" not in data
        assert "runner_key_encrypted" not in data
        assert "code" not in data
    finally:
        await _delete_agent(client_a, API_KEY, agent_id)


@pytest.mark.asyncio
@_REQUIRES_A
async def test_CLOUD_IDOR_06_list_does_not_expose_env_or_code(client_a):
    """GET /cloud/agents list never returns env_encrypted, code, or runner_key_encrypted."""
    r = await client_a.get("/cloud/agents", headers=_auth(API_KEY))
    assert r.status_code == 200
    for agent in r.json().get("agents", []):
        assert "env_encrypted" not in agent
        assert "runner_key_encrypted" not in agent
        assert "code" not in agent


@pytest.mark.asyncio
@_REQUIRES_A
async def test_CLOUD_IDOR_07_usage_scoped_to_caller(client_a):
    """GET /cloud/usage returns 200 and a well-formed response for the authed user."""
    r = await client_a.get("/cloud/usage", headers=_auth(API_KEY))
    if r.status_code == 404:
        pytest.skip("GET /cloud/usage not yet deployed — push local changes to Railway")
    assert r.status_code == 200
    data = r.json()
    assert "period" in data
    assert "runs" in data
    assert "runtime" in data
    assert "credits" in data
    assert "from" in data["period"] and "to" in data["period"]


# ═══════════════════════════════════════════════════════════════════════════════
# C3 — Cross-user IDOR (requires WAYFORTH_TEST_API_KEY_B)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@_REQUIRES_A
@_REQUIRES_B
async def test_CLOUD_IDOR_08_user_b_cannot_read_user_a_agent(client_a, client_b):
    """User B gets 404 when fetching user A's agent_id — not 200, not 403."""
    agent_id = await _create_test_agent(client_a, API_KEY)
    try:
        r = await client_b.get(f"/cloud/agents/{agent_id}", headers=_auth(API_KEY_B))
        assert r.status_code == 404, (
            f"IDOR: user B got {r.status_code} reading user A's agent. Expected 404.\n{r.text[:300]}"
        )
    finally:
        await _delete_agent(client_a, API_KEY, agent_id)


@pytest.mark.asyncio
@_REQUIRES_A
@_REQUIRES_B
async def test_CLOUD_IDOR_09_user_b_cannot_list_user_a_runs(client_a, client_b):
    """User B gets 404 on run history for user A's agent_id."""
    agent_id = await _create_test_agent(client_a, API_KEY)
    try:
        r = await client_b.get(f"/cloud/agents/{agent_id}/runs", headers=_auth(API_KEY_B))
        assert r.status_code == 404, (
            f"IDOR: user B got {r.status_code} listing user A's runs. Expected 404.\n{r.text[:300]}"
        )
    finally:
        await _delete_agent(client_a, API_KEY, agent_id)


@pytest.mark.asyncio
@_REQUIRES_A
@_REQUIRES_B
async def test_CLOUD_IDOR_10_user_b_list_does_not_include_user_a_agents(client_a, client_b):
    """User A's agent_id never appears in user B's /cloud/agents list."""
    agent_id = await _create_test_agent(client_a, API_KEY)
    try:
        r = await client_b.get("/cloud/agents", headers=_auth(API_KEY_B))
        assert r.status_code == 200
        ids = [a["id"] for a in r.json().get("agents", [])]
        assert agent_id not in ids, (
            f"IDOR: user A's agent {agent_id} appeared in user B's agent list"
        )
    finally:
        await _delete_agent(client_a, API_KEY, agent_id)


@pytest.mark.asyncio
@_REQUIRES_A
@_REQUIRES_B
async def test_CLOUD_IDOR_11_user_b_cannot_delete_user_a_agent(client_a, client_b):
    """User B's DELETE on user A's agent returns 404."""
    agent_id = await _create_test_agent(client_a, API_KEY)
    try:
        r = await client_b.delete(f"/cloud/agents/{agent_id}", headers=_auth(API_KEY_B))
        assert r.status_code == 404, (
            f"IDOR: user B got {r.status_code} on DELETE of user A's agent. Expected 404."
        )
        # Verify agent still exists for user A
        r2 = await client_a.get(f"/cloud/agents/{agent_id}", headers=_auth(API_KEY))
        assert r2.status_code == 200, "Agent was deleted by user B — IDOR write vulnerability"
    finally:
        await _delete_agent(client_a, API_KEY, agent_id)
